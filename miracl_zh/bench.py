"""Validate MIRACL zh/dev, capture native rankings, and score official passage IDs."""

import argparse
from collections import defaultdict
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
from tempfile import NamedTemporaryFile
import time
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
SOURCE = json.loads((HERE / "source.json").read_text(encoding="utf-8"))
PASSAGE_ID = re.compile(r"[0-9]+#[0-9]+").fullmatch


def check_fingerprint(path, expected):
    with path.open("rb") as file:
        if path.stat().st_size != expected["size"] or hashlib.file_digest(file, "sha256").hexdigest() != expected["sha256"]:
            raise ValueError(f"Official source fingerprint mismatch: {path}")
    return path


def checked_path(data, name):
    return check_fingerprint(data / name, SOURCE["files"][name])


def fetch(data):
    for name, entry in SOURCE["files"].items():
        path = data / name
        if path.exists():
            check_fingerprint(path, entry)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            try:
                with urlopen(entry["url"], timeout=60) as response:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        target.write(chunk)
                target.close()
                check_fingerprint(temporary, entry).replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        print(f"Fetched {name}", flush=True)


def topics_and_qrels(data):
    topics = dict(line.split("\t", 1) for line in checked_path(data, "topics.tsv").read_text(encoding="utf-8").splitlines())
    qrels = defaultdict(dict)
    for line in checked_path(data, "qrels.tsv").read_text(encoding="utf-8").splitlines():
        query, _, passage, score = line.split()
        if score not in ("0", "1") or not PASSAGE_ID(passage) or passage in qrels[query]:
            raise ValueError(f"Unexpected judgment for {query} / {passage}")
        qrels[query][passage] = int(score)
    if len(topics) != SOURCE["queries"] or sum(map(len, qrels.values())) != SOURCE["judgments"] or topics.keys() != qrels.keys():
        raise ValueError("Expected the complete MIRACL Chinese development split")
    if any(not any(judgments.values()) for judgments in qrels.values()):
        raise ValueError("Expected at least one positive per topic")
    return topics, qrels


def verify(data):
    topics, qrels = topics_and_qrels(data)
    judged = {passage for judgments in qrels.values() for passage in judgments}
    missing = judged.copy()
    shards = {}
    for name in SOURCE["files"]:
        if not name.startswith("corpus/"):
            continue
        count = 0
        with gzip.open(checked_path(data, name), "rt", encoding="utf-8") as file:
            for line in file:
                row = json.loads(line)
                if not PASSAGE_ID(row["docid"]) or not isinstance(row["text"], str) or not isinstance(row["title"], str):
                    raise ValueError(f"Invalid official passage schema in {name}")
                missing.discard(row["docid"])
                count += 1
        shards[name] = count
    total = sum(shards.values())
    if total != SOURCE["passages"] or missing:
        raise ValueError(f"Expected {SOURCE['passages']} passages; got {total} and {len(missing)} judged IDs missing")
    return {"dataset": SOURCE["dataset"], "queries": len(topics), "judgments": sum(map(len, qrels.values())),
            "positive_judgments": sum(sum(r.values()) for r in qrels.values()), "distinct_judged_passages": len(judged),
            "passages": total, "shards": shards}


def prior_api(product):
    path = HERE.parent / "doc_recall_v1" / "bench.py"
    spec = importlib.util.spec_from_file_location("taurus_native_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.API(product)


def index_binding(api, product):
    corpus = {name: entry["sha256"] for name, entry in SOURCE["files"].items() if name.startswith("corpus/")}
    return json.dumps([product, api.base, api.scope, corpus], sort_keys=True)


def validate_index_checkpoint(db, api, product):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'miracl_ingest_run'").fetchone():
        return
    if db.execute("SELECT binding, complete FROM miracl_ingest_run WHERE id = 1").fetchone() != (index_binding(api, product), 1):
        raise ValueError("The checkpoint must match this endpoint and scope and contain a completed full-corpus import")


def fetch_ranked(api, product, query, limit, reranker):
    if product == "contextbase":
        data = api.request("/memories/recall", {
            "query": query, "budget": "high", "types": ["world", "experience"], "max_tokens": 8192,
            "include": {"entities": None, "chunks": {"max_tokens": 16384}},
        })
        return [row["document_id"] for row in data["results"]]
    data = api.request("/knowledge-search", {
        "query": query, "knowledge_base_id": api.scope, "match_count": limit,
        "disable_vector_match": False, "disable_keywords_match": False,
        "vector_threshold": 0.0, "keyword_threshold": 0.0,
        "rerank": {"enabled": True, "model_id": reranker, "top_k": limit, "threshold": 0.0},
    })
    results = data["data"] or []
    if results and not (data.get("meta", {}).get("rerank") or {}).get("applied"):
        raise ValueError("Native WeKnora reranking did not run")
    return [row["knowledge_id"] for row in results]


def capture(args, topics):
    if args.product == "weknora" and not args.passage_map:
        raise ValueError("WeKnora requires its knowledge-ID to MIRACL passage-ID map")
    db = None
    try:
        api = prior_api(args.product)
        if args.passage_map:
            db = sqlite3.connect(args.passage_map.resolve().as_uri() + "?mode=ro", uri=True)
            validate_index_checkpoint(db, api, args.product)
        with args.run.open("x", encoding="utf-8") as output:
            for qid, query in topics.items():
                start = time.perf_counter()
                ranked = fetch_ranked(api, args.product, query, args.limit, args.reranker)
                latency = 1000 * (time.perf_counter() - start)
                passages = []
                for remote in dict.fromkeys(ranked):
                    passage = remote
                    if db is not None:
                        row = db.execute("SELECT passage_id FROM passage_map WHERE remote_id = ?", (remote,)).fetchone()
                        passage = row[0] if row else None
                    if not isinstance(passage, str) or not PASSAGE_ID(passage):
                        raise ValueError(f"Result without a valid source passage ID for query {qid}")
                    passages.append(passage)
                output.write(json.dumps({"qid": qid, "passages": list(dict.fromkeys(passages)), "latency_ms": latency}) + "\n")
                output.flush()
    finally:
        if db is not None:
            db.close()


def score(run, qrels, trec_output):
    def dcg(binary_labels):
        return sum(value / math.log2(i + 2) for i, value in enumerate(binary_labels))

    per_topic = {}
    for line in run.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        qid, ranked, latency = row["qid"], row["passages"], row["latency_ms"]
        if qid not in qrels or qid in per_topic or len(set(ranked)) != len(ranked):
            raise ValueError(f"Invalid or repeated query results for {qid}")
        if any(not isinstance(p, str) or not PASSAGE_ID(p) for p in ranked) or not math.isfinite(latency) or latency < 0:
            raise ValueError(f"Invalid passage ID or latency for {qid}")
        labels = qrels[qid]
        relevant = sum(labels.values())
        matches = [labels.get(p, 0) for p in ranked[:100]]
        per_topic[qid] = {"hit_at_5": int(any(matches[:5])),
                          "ndcg_at_10": dcg(matches[:10]) / dcg([1] * min(relevant, 10)),
                          "recall_at_100": sum(matches) / relevant, "depth": len(ranked), "latency_ms": latency}
    if per_topic.keys() != qrels.keys():
        raise ValueError("Missing official queries. Preserve explicit empty rankings in the capture")
    if trec_output:
        with trec_output.open("x", encoding="utf-8") as out:
            for line in run.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                for rank, passage in enumerate(row["passages"], 1):
                    print(row["qid"], "Q0", passage, rank, -rank, "native", file=out)
    rows = list(per_topic.values())
    latencies = sorted(r["latency_ms"] for r in rows)
    return {"dataset": SOURCE["dataset"], "queries": len(rows),
            **{metric: statistics.mean(r[metric] for r in rows)
               for metric in ("hit_at_5", "ndcg_at_10", "recall_at_100")},
            "returned_depth": {"mean": statistics.mean(r["depth"] for r in rows),
                               "min": min(r["depth"] for r in rows), "max": max(r["depth"] for r in rows),
                               "queries_at_least_100": sum(r["depth"] >= 100 for r in rows)},
            "serial_p95_ms": latencies[math.ceil(0.95 * len(rows)) - 1], "per_topic": per_topic}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch")
    commands.add_parser("verify")
    collect = commands.add_parser("capture")
    collect.add_argument("product", choices=("contextbase", "weknora"))
    collect.add_argument("run", type=Path)
    collect.add_argument("--passage-map", type=Path)
    collect.add_argument("--limit", type=int, choices=(40, 60, 100), default=60, help="WeKnora requested depth")
    collect.add_argument("--reranker", default="benchmark-bge-reranker")
    evaluate = commands.add_parser("score")
    evaluate.add_argument("run", type=Path)
    evaluate.add_argument("--trec-output", type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(args.data)
        return
    if args.command == "verify":
        print(json.dumps(verify(args.data), indent=2))
        return
    topics, qrels = topics_and_qrels(args.data)
    if args.command == "capture":
        capture(args, topics)
    else:
        print(json.dumps(score(args.run, qrels, args.trec_output), indent=2))


if __name__ == "__main__":
    main()
