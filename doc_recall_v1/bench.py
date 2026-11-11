"""Collect native document retrieval results and score evidence annotations."""

import argparse
import gzip
import json
import math
import os
import random
import re
import statistics
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
PRODUCTS = ("weknora", "contextbase")
BUDGET = 1024


def read_rows(path):
    path = Path(path)
    if not path.exists() and path.suffix != ".gz":
        compressed = path.with_suffix(path.suffix + ".gz")
        if compressed.exists():
            path = compressed
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_row(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    stream.flush()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, product):
        prefix = product.upper()
        self.base = os.environ[prefix + "_URL"].rstrip("/")
        target = urlsplit(self.base)
        if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password or target.query or target.fragment:
            raise ValueError(f"{prefix}_URL must be an HTTP(S) API base URL")
        key = os.environ[prefix + "_API_KEY"]
        if not key.strip():
            raise ValueError(f"{prefix}_API_KEY must be set")
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if product == "weknora":
            self.headers["X-API-Key"] = key
            self.scope = os.environ["WEKNORA_KB_ID"]
            self.prefix = "/api/v1"
        else:
            self.headers["Authorization"] = f"Bearer {key}"
            self.scope = quote(os.environ["CONTEXTBASE_BANK_ID"], safe="")
            self.prefix = f"/v1/default/banks/{self.scope}"
        if not self.scope:
            raise ValueError("The benchmark KB or Bank ID must be set")
        self.opener = build_opener(NoRedirect())
        self.timeout = float(os.environ.get("BENCH_HTTP_TIMEOUT_SECONDS", "90"))

    def request(self, path, payload=None, *, method=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        req = Request(self.base + self.prefix + path, body, self.headers, method=method)
        with self.opener.open(req, timeout=self.timeout) as response:
            data = json.load(response)
        if not isinstance(data, dict) or data.get("success") is False:
            raise ValueError("Unsuccessful or unsupported response")
        return data


def ingest(api, product, mapping_path, *, document_context=None):
    """Import only into a dedicated benchmark scope. Resume recorded WeKnora uploads."""
    mapping = json.loads(mapping_path.read_text(encoding="utf-8")) if mapping_path.exists() else {}
    documents = sorted((ROOT / "corpus").glob("*.md"))
    if product == "contextbase":
        for path in documents:
            if path.stem not in mapping:
                if document_context is not None:
                    document_context(path.stem)
                api.request("/memories", {
                    "items": [{"document_id": path.stem, "content": path.read_text(encoding="utf-8"),
                               "update_mode": "replace", "timestamp": "unset"}],
                    "async": False,
                })
                mapping[path.stem] = path.stem
                write_json(mapping_path, mapping)
        return

    for path in documents:
        if path.stem in mapping:
            continue
        content = path.read_text(encoding="utf-8")
        if document_context is not None:
            document_context(path.stem)
        response = api.request(f"/knowledge-bases/{quote(api.scope, safe='')}/knowledge/manual", {
            "title": content.splitlines()[0].removeprefix("# "), "content": content, "status": "publish",
        })
        mapping[path.stem] = response["data"]["id"]
        write_json(mapping_path, mapping)
        if document_context is not None:
            wait_for_weknora_index(api, {mapping[path.stem]})

    if document_context is None:
        wait_for_weknora_index(api, set(mapping.values()))


def wait_for_weknora_index(api, pending):
    deadline = time.monotonic() + 900
    while pending:
        for identifier in sorted(pending):
            state = api.request("/knowledge/" + quote(identifier, safe=""))["data"]["parse_status"]
            if state == "completed":
                pending.remove(identifier)
            elif state in {"failed", "cancelled"}:
                raise RuntimeError(f"WeKnora indexing ended with status {state}")
        if pending and time.monotonic() >= deadline:
            raise TimeoutError(f"WeKnora indexing timed out with {len(pending)} documents pending")
        if pending:
            time.sleep(3)


def retrieve(api, product, query, source_ids):
    def document_id(remote_id):
        return source_ids.get(str(remote_id), f"unmapped:{remote_id}")

    if product == "weknora":
        data = api.request("/knowledge-search", {"query": query, "knowledge_base_id": api.scope})
        return [
            {"document_id": document_id(item.get("knowledge_id")), "text": item["content"]}
            for item in (data["data"] or [])
        ]

    data = api.request("/memories/recall", {
        "query": query, "budget": "mid", "types": ["world", "experience"], "max_tokens": 4096,
        "include": {"chunks": {"max_tokens": 8192}},
    })
    chunks = data.get("chunks") or {}
    if not isinstance(chunks, dict):
        raise ValueError("Unsupported ContextBase chunks format")
    passages = []
    for item in (data["results"] or []):
        chunk = chunks.get(item.get("chunk_id")) or chunks.get(item.get("id"))
        text = chunk if isinstance(chunk, str) else ((chunk or {}).get("text") or (chunk or {}).get("content"))
        passages.append({"document_id": document_id(item.get("document_id")), "text": text or ""})
    return passages


def normalize(text):
    return re.sub(r"[\s`]+", "", text)


def annotate(passages, evidence, encode, decode, *, budget=None):
    remaining = BUDGET if budget is None else budget
    seen = set()
    ranked = []
    for passage in passages:
        doc, text = passage["document_id"], passage["text"].strip()
        key = (doc, text)
        if key in seen:
            continue
        seen.add(key)
        tokens = encode(text)
        prefix = decode(tokens[:remaining]).rstrip("\ufffd")
        budget_tokens = len(encode(prefix))
        # Re-encoding a decoded prefix can change its token boundaries.
        while budget_tokens > remaining:
            prefix = prefix[:-1]
            budget_tokens = len(encode(prefix))
        remaining -= budget_tokens
        exact_matches = [
            entry["id"] for entry in evidence
            if doc == entry["document_id"] and normalize(entry["quote"]) in normalize(prefix)
        ]
        ranked.append({
            "document_id": doc, "text": text, "returned_tokens": len(tokens),
            "budget_text": prefix, "budget_tokens": budget_tokens, "matched_evidence_ids": exact_matches,
        })
    return ranked


def collect(api, product, mapping_path, output_path, questions, encode, decode, *, preserve_order=False):
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    expected = {p.stem for p in (ROOT / "corpus").glob("*.md")}
    if set(mapping) != expected or len(set(mapping.values())) != len(expected):
        raise ValueError("The document mapping must contain one ID for every fixture document")
    source_ids = {str(remote): source for source, remote in mapping.items()}
    shuffled = questions.copy()
    if not preserve_order:
        random.Random(20261110).shuffle(shuffled)
    with output_path.open("x", encoding="utf-8") as output:
        for q in shuffled:
            start = time.perf_counter()
            try:
                passages = retrieve(api, product, q["query"], source_ids)
                latency = (time.perf_counter() - start) * 1000
            except (HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError) as error:
                code = getattr(error, "code", None)
                write_row(output, {"id": q["id"], "error": f"HTTP {code}" if code else type(error).__name__})
                continue
            write_row(output, {"id": q["id"], "latency_ms": latency,
                               "top_5_evidence_ids": [e["id"] for e in q["evidence"]
                                   if any(p["document_id"] == e["document_id"] and normalize(e["quote"]) in normalize(p["text"])
                                          for p in passages[:5])],
                               "candidates": annotate(passages, q["evidence"], encode, decode),
                               "reviewed": not q["evidence"] or not passages})


def mean(values):
    return statistics.mean(values) if values else None


def percentile(values, fraction):
    return sorted(values)[math.ceil(fraction * len(values)) - 1] if values else None


def summarize(rows, questions):
    answerable = []
    no_answer = []
    latencies = []
    returned = []
    unmapped = 0
    missing_text = 0
    failed = 0
    for row in rows:
        question = questions[row["id"]]
        if "error" in row:
            failed += 1
            continue
        candidates = row["candidates"]
        unmapped += sum(item["document_id"].startswith("unmapped:") for item in candidates)
        missing_text += sum(not item["text"] for item in candidates)
        latencies.append(row["latency_ms"])
        returned.append(sum(item["returned_tokens"] for item in candidates))
        if not question["evidence"]:
            no_answer.append(bool(candidates))
            continue

        gold = {entry["id"]: entry["document_id"] for entry in question["evidence"]}
        gold_docs = set(gold.values())
        ranking = list(dict.fromkeys(item["document_id"] for item in candidates if item["document_id"]))
        found = set()
        for item in candidates:
            for evidence_id in item["matched_evidence_ids"]:
                if evidence_id not in gold or item["document_id"] != gold[evidence_id] or not item["budget_text"]:
                    raise ValueError(f"Invalid evidence annotation: {row['id']} / {evidence_id}")
                found.add(evidence_id)

        dcg = sum(1 / math.log2(rank + 2) for rank, doc in enumerate(ranking[:10]) if doc in gold_docs)
        ideal = sum(1 / math.log2(rank + 2) for rank in range(min(len(gold_docs), 10)))
        answerable.append({
            "document_hit_at_1": float(bool(gold_docs.intersection(ranking[:1]))),
            "document_hit_at_5": float(bool(gold_docs.intersection(ranking[:5]))),
            "document_ndcg_at_10": dcg / ideal,
            "evidence_recall_at_1024_tokens": len(found) / len(gold),
            "complete_evidence_at_1024_tokens": float(found == gold.keys()),
        })

    metric_names = (
        "document_hit_at_1", "document_hit_at_5", "document_ndcg_at_10",
        "evidence_recall_at_1024_tokens", "complete_evidence_at_1024_tokens",
    )
    return {
        "requests": len(rows), "failed": failed, "answerable_scored": len(answerable),
        "unmapped_candidates": unmapped, "candidates_without_source_text": missing_text,
        "no_answer_scored": len(no_answer), "no_answer_candidate_return_rate": mean(no_answer),
        "retrieval_ms_p50": percentile(latencies, 0.5), "retrieval_ms_p95": percentile(latencies, 0.95),
        "returned_tokens_mean": mean(returned), "returned_tokens_p95": percentile(returned, 0.95),
        **{metric: mean([result[metric] for result in answerable]) for metric in metric_names},
    }


def model_usage(path, question_ids):
    rows = read_rows(path) if path else []
    by_phase = {"ingest": {}, "retrieve": {}}
    for row in rows:
        phase, identifier = row["phase"], row["id"]
        expected = {"run"} if phase == "ingest" else question_ids
        target = by_phase[phase]
        if identifier not in expected or identifier in target:
            raise ValueError(f"Unexpected or duplicate usage row: {phase} / {identifier}")
        if row["status"] not in ("observed", "estimated", "unobserved"):
            raise ValueError(f"Invalid usage status: {row['status']}")
        if row["status"] != "unobserved":
            for field in ("input_tokens", "output_tokens", "calls"):
                value = row[field]
                if type(value) is not int or value < 0:
                    raise ValueError(f"Invalid {field} for {phase} / {identifier}")
        target[identifier] = row

    summary = {}
    for phase, target in by_phase.items():
        expected_count = 1 if phase == "ingest" else len(question_ids)
        summary[phase] = {"expected_rows": expected_count}
        for status in ("observed", "estimated"):
            selected = [row for row in target.values() if row["status"] == status]
            stats = {"rows": len(selected)}
            for field in ("input_tokens", "output_tokens", "calls"):
                values = [row[field] for row in selected]
                stats[field] = {"sum": sum(values), "mean": mean(values), "p95": percentile(values, 0.95)} if values else None
            summary[phase][status] = stats
        summary[phase]["unobserved_rows"] = expected_count - sum(
            summary[phase][status]["rows"] for status in ("observed", "estimated")
        )
    return summary


def score(path, questions, allow_unreviewed, usage_path):
    rows = read_rows(path)
    ids = [row["id"] for row in rows]
    if len(ids) != len(questions) or set(ids) != set(questions):
        raise ValueError("The capture must contain each question exactly once")
    outstanding = sum("error" not in row and not row["reviewed"] for row in rows)
    if outstanding and not allow_unreviewed:
        raise ValueError(f"{outstanding} queries still need evidence review")
    categories = sorted({q["category"] for q in questions.values()})
    print(json.dumps({
        "unreviewed": outstanding,
        "overall": summarize(rows, questions),
        "model_usage": model_usage(usage_path, set(questions)),
        "by_category": {
            category: summarize([row for row in rows if questions[row["id"]]["category"] == category], questions)
            for category in categories
        },
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "collect"):
        command = sub.add_parser(name)
        command.add_argument("product", choices=PRODUCTS)
        command.add_argument("run_dir", type=Path)
    scoring = sub.add_parser("score")
    scoring.add_argument("capture", type=Path)
    scoring.add_argument("--allow-unreviewed", action="store_true")
    scoring.add_argument("--usage", type=Path)
    args = parser.parse_args()
    questions = read_rows(ROOT / "questions.jsonl")
    if args.command == "score":
        score(args.capture, {q["id"]: q for q in questions}, args.allow_unreviewed, args.usage)
        return

    api = API(args.product)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = args.run_dir / f"{args.product}-documents.json"
    if args.command == "ingest":
        ingest(api, args.product, mapping_path)
        return

    import tiktoken

    tokenizer = tiktoken.get_encoding("o200k_base")
    collect(api, args.product, mapping_path, args.run_dir / f"{args.product}-capture.jsonl",
            questions, tokenizer.encode_ordinary, tokenizer.decode)


if __name__ == "__main__":
    main()
