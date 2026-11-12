"""Summarize completed TaurusDB candidate-cap runs and choose a cap."""

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

import tiktoken

from pilot_metrics import evidence_ids, score_pilot
from run_eval import bench


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_case(deployment_dir, sweep_dir, case, questions, encode):
    product = case.split("-")[0]
    archive = sweep_dir / case
    if archive.is_dir():
        run_dir = archive
        run_id = read_json(archive / "run.json")["id"]
    else:
        case_log = (sweep_dir / (case + ".log")).read_text()
        run_path, = [Path(line.split(" ", 1)[1]) for line in case_log.splitlines() if line.startswith("RUN ")]
        run_dir = deployment_dir / "results" / run_path.name
        run_id = run_path.name
    rows = bench.read_rows(run_dir / (product + "-capture.jsonl"))
    calls = bench.read_rows(run_dir / (product + "-model-calls.jsonl"))
    quality = score_pilot(rows, questions, encode, calls)
    if len(rows) != len(questions) or {r["id"] for r in rows} != questions.keys():
        raise ValueError(f"{case} has incomplete or duplicate captures")
    if quality["errors"]:
        raise ValueError(f"{case} has query failures")
    phases = {phase: [r for r in calls if r["phase"] == phase] for phase in ("ingest", "retrieve")}
    counts = Counter(row["kind"] for row in phases["retrieve"])
    if any(counts[kind] != len(questions) for kind in ("embedding", "rerank")):
        raise ValueError(f"{case} is missing measured embedding/rerank calls")
    if any(row["status"] != "observed" for phase in phases.values() for row in phase):
        raise ValueError(f"{case} has estimated model usage")
    if any(row["kind"] == "chat" for phase in phases.values() for row in phase):
        raise ValueError(f"{case} made unexpected generation calls")
    rerank_calls = [row for row in phases["retrieve"] if row["kind"] == "rerank"]
    candidate_counts = [row["pairs"] for row in rerank_calls]
    if product == "contextbase" and max(candidate_counts) != int(case.split("-")[1]):
        raise ValueError(f"{case} did not exercise the expected candidate cap")
    ingest_usage = quality["usage"]["ingest"]
    latencies = [row["latency_ms"] for row in rows]
    calls_by_query = {kind: {r["id"]: r for r in phases["retrieve"] if r["kind"] == kind}
                      for kind in ("embedding", "rerank")}
    evidence_by_query = {}
    query_measurements = {}
    for row in rows:
        identifier = row["id"]
        question = questions[identifier]
        embedding = calls_by_query["embedding"][identifier]
        rerank = calls_by_query["rerank"][identifier]
        query_measurements[identifier] = {
            "latency_ms": row["latency_ms"],
            "returned_passages": len(row["candidates"]),
            "embedding_input_tokens": embedding["input_tokens"],
            "rerank_input_tokens": rerank["input_tokens"],
            "rerank_pairs": rerank["pairs"],
            "rerank_ms": rerank["elapsed_ms"],
        }
        expected = question["evidence"]
        if expected:
            evidence_by_query[identifier] = {
                "found_by_budget": {
                    str(n): sorted(evidence_ids(row["candidates"], expected, encode, n))
                    for n in quality["budgets"]
                },
                "expected": sorted(entry["id"] for entry in expected),
                "hit_at_5": bool(row["top_5_evidence_ids"]),
                "cross_document": len({entry["document_id"] for entry in expected}) > 1,
            }
    return {
        "run": run_id,
        "requests": len(rows),
        "failures": quality["errors"],
        "answerable": quality["answerable"],
        "hit_at_5_count": sum(query["hit_at_5"] for query in evidence_by_query.values()),
        "cross_document_questions": quality["cross_document"],
        "evidence_recall": {str(n): value["evidence_recall"] for n, value in quality["budgets"].items()},
        "cross_complete_2000": quality["budgets"][2000]["cross_document_complete"],
        "p50_c1_ms": bench.percentile(latencies, 0.5),
        "p95_c1_ms": bench.percentile(latencies, 0.95),
        "latency_sum_c1_s": sum(latencies) / 1000,
        "query_input_tokens_mean": sum(r["input_tokens"] for r in phases["retrieve"]) / len(rows),
        "query_output_tokens_total": sum(r["output_tokens"] for r in phases["retrieve"]),
        "ingest_input_tokens_mean": statistics.mean(r["input_tokens"] for r in ingest_usage.values()),
        "ingest_output_tokens_total": sum(r["output_tokens"] for r in phases["ingest"]),
        "documents_with_ingest_model_usage": len(ingest_usage),
        "rerank_pairs_mean": statistics.mean(candidate_counts),
        "rerank_pairs_min": min(candidate_counts),
        "rerank_pairs_max": max(candidate_counts),
        "rerank_input_tokens_mean": statistics.mean(r["input_tokens"] for r in rerank_calls),
        "rerank_ms_total": sum(r["elapsed_ms"] for r in rerank_calls),
        "no_answer_candidates": quality["no_answer_with_candidates"],
        "no_answer_questions": quality["no_answer"],
        "unmapped_candidates": sum(c["document_id"].startswith("unmapped:") for r in rows for c in r["candidates"]),
        "missing_source_passages": sum(not c["text"] for r in rows for c in r["candidates"]),
        "evidence_by_query": evidence_by_query,
        "query_measurements": query_measurements,
        "ingest_usage_by_document": ingest_usage,
    }


def choose(cases):
    baseline = cases.get("contextbase-60")
    if baseline is None:
        return None
    candidates = []
    for case, result in cases.items():
        if not case.startswith("contextbase-") or case == "contextbase-60":
            continue
        if (result["evidence_recall"]["2000"] >= baseline["evidence_recall"]["2000"]
                and result["cross_complete_2000"] >= baseline["cross_complete_2000"]
                and result["hit_at_5_count"] >= baseline["hit_at_5_count"]
                and result["query_input_tokens_mean"] < baseline["query_input_tokens_mean"]
                and result["p95_c1_ms"] < baseline["p95_c1_ms"]):
            candidates.append(case)
    return min(candidates, key=lambda case: int(case.split("-")[1])) if candidates else "contextbase-60"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_dir", type=Path)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("cases", nargs="+")
    args = parser.parse_args()
    deployment = Path(__file__).resolve().parent
    questions = {row["id"]: row for row in bench.read_rows(args.dataset / "questions.jsonl")}
    encode = tiktoken.get_encoding("o200k_base")
    cases = {case: summarize_case(deployment, args.sweep_dir, case, questions, encode) for case in args.cases}
    print(json.dumps({"recommended_case": choose(cases), "cases": cases}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
