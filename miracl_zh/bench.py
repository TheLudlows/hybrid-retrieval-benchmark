"""Validate MIRACL zh/dev, capture native rankings, and score official passage IDs."""

import argparse
from collections import defaultdict
import gzip
import hashlib
import importlib.util
import json
import math
import os
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

SUBSET_SHARD = "corpus/subset.jsonl.gz"


def dataset_source(data):
    path = data / "subset.json"
    if not path.exists():
        return SOURCE
    subset = json.loads(path.read_text(encoding="utf-8"))
    original = {name: entry["sha256"] for name, entry in SOURCE["files"].items()}
    if (subset["source"] != original or subset["unit"] not in ("articles", "passages")
            or not 1 <= subset["count"] <= subset["passages"] <= SOURCE["passages"]
            or (subset["unit"] == "passages" and subset["count"] != subset["passages"])):
        raise ValueError("Invalid subset manifest or MIRACL source version")
    entry = subset["file"]
    if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or entry["size"] <= 0:
        raise ValueError("Invalid subset shard fingerprint")
    return {**SOURCE, "dataset": f"{SOURCE['dataset']}/subset-{entry['sha256'][:16]}",
            "passages": subset["passages"], "subset": subset,
            "files": {"topics.tsv": SOURCE["files"]["topics.tsv"], "qrels.tsv": SOURCE["files"]["qrels.tsv"],
                      SUBSET_SHARD: entry}}


def article_id(passage):
    return passage.split("#", 1)[0]


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


def corpus_rows(data, source):
    for name, entry in source["files"].items():
        if not name.startswith("corpus/"):
            continue
        with gzip.open(check_fingerprint(data / name, entry), "rt", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if not PASSAGE_ID(row["docid"]) or not isinstance(row["text"], str) or not isinstance(row["title"], str):
                    raise ValueError(f"Invalid passage schema in {name}")
                yield name, row


def verify(data, source):
    topics, qrels = topics_and_qrels(data)
    judged = {passage for judgments in qrels.values() for passage in judgments}
    missing = judged.copy()
    shards = defaultdict(int)
    subset = source.get("subset")
    articles = set() if subset and subset["unit"] == "articles" else None
    for name, row in corpus_rows(data, source):
        passage = row["docid"]
        missing.discard(passage)
        shards[name] += 1
        if articles is not None:
            articles.add(article_id(passage))
    total = sum(shards.values())
    if total != source["passages"] or missing:
        raise ValueError(f"Expected {source['passages']} passages; got {total} and {len(missing)} judged IDs missing")
    if articles is not None and len(articles) != subset["count"]:
        raise ValueError("Article count does not match the subset manifest")
    result = {"dataset": source["dataset"], "queries": len(topics), "judgments": sum(map(len, qrels.values())),
              "positive_judgments": sum(sum(r.values()) for r in qrels.values()), "distinct_judged_passages": len(judged),
              "passages": total, "shards": shards}
    if subset:
        result["subset"] = {key: subset[key] for key in ("unit", "count", "seed")}
    return result


def prior_api(product):
    path = HERE.parent / "doc_recall_v1" / "bench.py"
    spec = importlib.util.spec_from_file_location("taurus_native_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.API(product)


def index_binding(api, product, source):
    corpus = {name: entry["sha256"] for name, entry in source["files"].items() if name.startswith("corpus/")}
    return json.dumps([product, api.base, api.scope, corpus], sort_keys=True)


def validate_index_checkpoint(db, api, product, source, smoke=False):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'miracl_ingest_run'").fetchone():
        if source.get("subset"):
            raise ValueError("Use the subset checkpoint created by ingest.py")
        return
    record = db.execute("SELECT binding, complete FROM miracl_ingest_run WHERE id = 1").fetchone()
    if not record or record[0] != index_binding(api, product, source) or (not smoke and record[1] != 1):
        raise ValueError("The checkpoint must match this endpoint and scope and contain a completed dataset import")


def fetch_ranked(api, product, query, limit, reranker, max_tokens=8192):
    if product == "contextbase":
        data = api.request("/memories/recall", {
            "query": query, "budget": "high", "types": ["world", "experience"], "max_tokens": max_tokens,
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
    rerank_meta = data.get("meta", {}).get("rerank") or {}
    if results and not rerank_meta.get("applied"):
        raise ValueError("Native WeKnora reranking did not run")
    if rerank_meta.get("candidate_count", 0) > limit:
        raise ValueError("Native WeKnora rerank input exceeded the requested candidate ceiling")
    api.last_native_meta = data.get("meta", {})
    return [row["knowledge_id"] for row in results]


def capture(args, topics, source):
    if (args.product == "weknora" or source.get("subset")) and not args.passage_map:
        raise ValueError("Provide the passage-map checkpoint for WeKnora or for a subset run")
    db = None
    try:
        api = prior_api(args.product)
        if args.passage_map:
            db = sqlite3.connect(args.passage_map.resolve().as_uri() + "?mode=ro", uri=True)
            if args.smoke_index and (args.max_queries is None or args.max_queries > 4):
                raise ValueError("Incomplete-index smoke capture is limited to four queries")
            validate_index_checkpoint(db, api, args.product, source, args.smoke_index)
        binding = {"index": index_binding(api, args.product, source), "limit": args.limit,
                   "reranker": args.reranker, "budget": "high", "config_sha256": args.config_sha256,
                   "smoke_index": args.smoke_index}
        if getattr(args, "max_tokens", 8192) != 8192:
            binding["max_tokens"] = args.max_tokens
        meta = args.run.with_suffix(args.run.suffix + ".config.json")
        completed = set()
        if args.resume and args.run.exists():
            if json.loads(meta.read_text()) != binding:
                raise ValueError("Resume configuration differs from the original run")
            for line in args.run.read_text().splitlines():
                row = json.loads(line)
                if row["dataset"] != source["dataset"] or row["qid"] not in topics or row["qid"] in completed:
                    raise ValueError("Invalid completed query in resume file")
                completed.add(row["qid"])
        else:
            with meta.open("x") as stream:
                json.dump(binding, stream, indent=2)
        if len(completed) == len(topics):
            print(f"Already complete: {len(completed)}/{len(topics)}; no requests sent", flush=True)
            return
        def context(phase, qid):
            if args.measurement_dir:
                target = args.measurement_dir / "measurement-context.json"
                temporary = target.with_suffix(".tmp")
                entry = {"phase": phase, "id": qid}
                if getattr(args, "run_id", None):
                    entry["run_id"] = args.run_id
                temporary.write_text(json.dumps({args.product: entry}))
                temporary.replace(target)
        for i in range(args.warmup):
            context("warmup", str(i))
            fetch_ranked(api, args.product, f"检索预热：第{i + 1}次中文文档检索", args.limit, args.reranker, getattr(args, "max_tokens", 8192))
        with args.run.open("a" if args.resume else "x", encoding="utf-8") as output:
            for qid, query in topics.items():
                if qid in completed:
                    continue
                if args.max_queries is not None and len(completed) >= args.max_queries:
                    break
                context("smoke_query" if args.smoke_index else "query", qid)
                start = time.perf_counter()
                ranked = fetch_ranked(api, args.product, query, args.limit, args.reranker, getattr(args, "max_tokens", 8192))
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
                output.write(json.dumps({"dataset": source["dataset"], "qid": qid,
                                         "passages": list(dict.fromkeys(passages)), "latency_ms": latency,
                                         "remote_ids": list(dict.fromkeys(ranked)),
                                         "native_meta": getattr(api, "last_native_meta", {}),
                                         "completed_unix": time.time()}) + "\n")
                output.flush()
                os.fsync(output.fileno())
                completed.add(qid)
                print(f"Completed {len(completed)}/{len(topics)}: {qid}", flush=True)
    finally:
        if db is not None:
            db.close()


def score(run, qrels, trec_output, source=SOURCE):
    def dcg(binary_labels):
        return sum(value / math.log2(i + 2) for i, value in enumerate(binary_labels))

    per_topic = {}
    for line in run.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        qid, ranked, latency = row["qid"], row["passages"], row["latency_ms"]
        if row.get("dataset", SOURCE["dataset"]) != source["dataset"]:
            raise ValueError("Capture dataset does not match the scoring corpus")
        if qid not in qrels or qid in per_topic or len(set(ranked)) != len(ranked):
            raise ValueError(f"Invalid or repeated query results for {qid}")
        if any(not isinstance(p, str) or not PASSAGE_ID(p) for p in ranked) or not math.isfinite(latency) or latency < 0:
            raise ValueError(f"Invalid passage ID or latency for {qid}")
        labels = qrels[qid]
        relevant = sum(labels.values())
        matches = [labels.get(p, 0) for p in ranked[:100]]
        per_topic[qid] = {"hit_at_1": int(any(matches[:1])), "hit_at_10": int(any(matches[:10])),
                          "hit_at_5": int(any(matches[:5])),
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
    return {"dataset": source["dataset"], "queries": len(rows),
            **{metric: statistics.mean(r[metric] for r in rows)
               for metric in ("hit_at_1", "hit_at_5", "hit_at_10", "ndcg_at_10", "recall_at_100")},
            "returned_depth": {"mean": statistics.mean(r["depth"] for r in rows),
                               "min": min(r["depth"] for r in rows), "max": max(r["depth"] for r in rows),
                               "queries_at_least_100": sum(r["depth"] >= 100 for r in rows)},
            **{f"serial_p{p}_ms": latencies[math.ceil(p / 100 * len(rows)) - 1] for p in (50, 95, 99)},
            "per_topic": per_topic}


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
    collect.add_argument("--resume", action="store_true")
    collect.add_argument("--smoke-index", action="store_true", help="Restart test only; at most four queries")
    collect.add_argument("--warmup", type=int, default=4)
    collect.add_argument("--max-queries", type=int)
    collect.add_argument("--config-sha256", help="SHA-256 of the frozen product/model/hardware configuration")
    collect.add_argument("--measurement-dir", type=Path)
    collect.add_argument("--run-id", help="Attribute model calls to an experiment stage")
    collect.add_argument("--max-tokens", type=int, default=8192, help="ContextBase native returned-result token budget")
    evaluate = commands.add_parser("score")
    evaluate.add_argument("run", type=Path)
    evaluate.add_argument("--trec-output", type=Path)
    args = parser.parse_args()
    source = dataset_source(args.data)
    if args.command == "fetch":
        if source.get("subset"):
            parser.error("Fetch the official corpus into a separate directory")
        fetch(args.data)
        return
    if args.command == "verify":
        print(json.dumps(verify(args.data, source), indent=2))
        return
    topics, qrels = topics_and_qrels(args.data)
    if args.command == "capture":
        capture(args, topics, source)
    else:
        print(json.dumps(score(args.run, qrels, args.trec_output, source), indent=2))


if __name__ == "__main__":
    main()
