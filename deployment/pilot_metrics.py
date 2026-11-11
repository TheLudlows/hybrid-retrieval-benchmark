"""Score the real-document pilot and measure query load and document updates."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import time
from urllib.error import URLError
from urllib.parse import quote

from run_eval import bench

BUDGETS = (256, 512, 2000)


def evidence_ids(passages, evidence, encoding, budget):
    ranked = bench.annotate(passages, evidence, encoding.encode_ordinary, encoding.decode, budget=budget)
    return {evidence_id for row in ranked for evidence_id in row["matched_evidence_ids"]}


def score_pilot(rows, questions, encoding, calls):
    answerable = [r for r in rows if "error" not in r and questions[r["id"]]["evidence"]]
    cross = [r for r in answerable if len({e["document_id"] for e in questions[r["id"]]["evidence"]}) >= 2]
    no_answer = [r for r in rows if "error" not in r and not questions[r["id"]]["evidence"]]
    result = {
        "questions": len(rows),
        "errors": sum("error" in r for r in rows),
        "answerable": len(answerable),
        "cross_document": len(cross),
        "no_answer": len(no_answer),
        "evidence_hit_at_5": bench.mean([bool(r["top_5_evidence_ids"]) for r in answerable]),
        "no_answer_with_candidates": sum(bool(r["candidates"]) for r in no_answer),
        "budgets": {},
    }
    for budget in BUDGETS:
        hits = {r["id"]: evidence_ids(r["candidates"], questions[r["id"]]["evidence"], encoding, budget)
                for r in answerable}
        recalls = [len(hits[r["id"]]) / len(questions[r["id"]]["evidence"]) for r in answerable]
        all_cross = sum(len(hits[r["id"]]) == len(questions[r["id"]]["evidence"]) for r in cross)
        result["budgets"][budget] = {"evidence_recall": bench.mean(recalls), "cross_document_complete": all_cross}
    usage = defaultdict(lambda: defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0}))
    for call in calls:
        if call["phase"] not in {"ingest", "retrieve"}:
            continue
        group = usage[call["phase"]][call["id"]]
        for key in ("calls", "input_tokens", "output_tokens"):
            group[key] += call[key]
    result["usage"] = usage
    return result


def run_load(api, product, questions, source_ids, concurrency):
    def sample(question):
        client = type(api)(product, tracking=False)
        start = time.perf_counter()
        try:
            bench.retrieve(client, product, question["query"], source_ids)
        except (OSError, URLError, ValueError, TypeError, KeyError, RuntimeError) as error:
            return {"id": question["id"], "error": type(error).__name__,
                    "timeout": isinstance(error, TimeoutError) or "timeout" in str(error).lower()
                               or getattr(error, "code", None) == 504,
                    "latency_ms": (time.perf_counter() - start) * 1000}
        return {"id": question["id"], "latency_ms": (time.perf_counter() - start) * 1000}

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows = list(pool.map(sample, questions))
    elapsed = time.perf_counter() - started
    successful = [r["latency_ms"] for r in rows if "error" not in r]
    return {"concurrency": concurrency, "elapsed_s": elapsed, "requests": len(rows),
            "errors": len(rows) - len(successful), "timeouts": sum(r.get("timeout", False) for r in rows),
            "p95_ms": bench.percentile(successful, 0.95), "rows": rows}


def put_version(api, product, doc, text, remote=None, *, asynchronous=False):
    if product == "contextbase":
        api.request("/memories", {"items": [{"document_id": doc, "content": text,
                    "update_mode": "replace", "timestamp": "unset"}], "async": asynchronous})
        return doc
    payload = {"title": text.splitlines()[0].removeprefix("# "), "content": text, "status": "publish"}
    if remote:
        api.request(f"/knowledge/manual/{quote(remote, safe='')}", payload, method="PUT")
        return remote
    response = api.request(f"/knowledge-bases/{quote(api.scope, safe='')}/knowledge/manual", payload)
    return response["data"]["id"]


def measure_update(api, product, fixture, source_ids, encoding, poll_interval=2, timeout=180):
    doc = fixture["document_id"]
    v1, v2 = fixture["before"], fixture["after"]
    evidence = [{"id": v["version"], "quote": v["quote"], "document_id": doc} for v in (v1, v2)]
    remote = put_version(api, product, doc, v1["text"])
    source_ids[remote] = doc
    if product == "weknora":
        bench.wait_for_weknora_index(api, {remote})

    def recalled():
        passages = bench.retrieve(api, product, fixture["query"], source_ids)
        return evidence_ids(passages, evidence, encoding, 2000)

    if recalled() != {"v1"}:
        return {"id": doc, "status": "v1_precondition_failed"}
    start = time.perf_counter()
    put_version(api, product, doc, v2["text"], remote, asynchronous=True)
    acknowledged = time.perf_counter() - start
    new_time = old_time = None
    observations = []
    while time.perf_counter() - start < timeout:
        try:
            found = recalled()
        except (OSError, URLError, ValueError, TypeError, KeyError, RuntimeError) as error:
            observations.append({"elapsed_s": time.perf_counter() - start, "error": type(error).__name__})
        else:
            elapsed = time.perf_counter() - start
            observations.append({"elapsed_s": elapsed, "evidence": sorted(found)})
            if "v2" in found and new_time is None:
                new_time = elapsed
            if "v1" not in found and old_time is None:
                old_time = elapsed
            if found == {"v2"}:
                break
        time.sleep(poll_interval)
    return {"id": doc, "acknowledged_s": acknowledged, "new_first_seen_s": new_time,
            "old_first_absent_s": old_time, "poll_interval_s": poll_interval,
            "status": "confirmed" if observations and observations[-1].get("evidence") == ["v2"] else "timed_out",
            "observations": observations}


def run_supplemental(api, product, run_id, run_dir, questions, updates, source_ids, bench_module, live):
    from os import environ

    for concurrency in (int(value) for value in environ.get("BENCH_LOAD_CONCURRENCY", "8").split(",") if value):
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        live.model_context(product, run_id, "load", f"concurrency-{concurrency}")
        metrics = run_load(api, product, questions, source_ids, concurrency)
        bench_module.write_json(run_dir / f"{product}-load-c{concurrency}.json", metrics)
        print("LOAD", product, concurrency, metrics["p95_ms"], flush=True)
    tokenizer = __import__("tiktoken").get_encoding("o200k_base")
    for fixture in updates:
        doc = fixture["document_id"]
        live.model_context(product, run_id, "update", doc)
        try:
            metrics = measure_update(api, product, fixture, source_ids, tokenizer)
        except (OSError, URLError, ValueError, TypeError, KeyError, RuntimeError) as error:
            metrics = {"id": doc, "status": "error", "error": type(error).__name__}
        with (run_dir / f"{product}-updates.jsonl").open("a", encoding="utf-8") as stream:
            bench_module.write_row(stream, metrics)
        print("UPDATE", product, doc, metrics["status"], flush=True)
