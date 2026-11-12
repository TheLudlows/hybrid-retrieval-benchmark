"""Recompute complete quality, latency, and observed model usage for both runs."""
import argparse
import csv
import json
from pathlib import Path

from bench import dataset_source, score, topics_and_qrels


def summarize(data, results):
    source = dataset_source(data)
    _, qrels = topics_and_qrels(data)
    summaries = {}
    for product in ("contextbase", "weknora"):
        capture = results / f"{product}-60.jsonl"
        quality = score(capture, qrels, None, source)
        rankings = [json.loads(line) for line in capture.read_text().splitlines()]
        first = rankings[0]
        start = first["completed_unix"] - first["latency_ms"] / 1000 - 0.01
        end = rankings[-1]["completed_unix"] + 0.01
        calls = [json.loads(line) for line in (results / f"{product}-model-calls.jsonl").read_text().splitlines()]
        formal = [c for c in calls if c["phase"] == "query" and start <= c["timestamp"] <= end]
        assert {c["id"] for c in formal} <= qrels.keys()
        ingest = [c for c in calls if c["phase"] == "ingest" and c["kind"] == "embedding"]
        query_embed = [c for c in formal if c["kind"] == "embedding"]
        rerank = [c for c in formal if c["kind"] == "rerank"]
        def usage(rows):
            return {"input_tokens": sum(c["input_tokens"] for c in rows),
                    "calls": sum(c["calls"] for c in rows), "pairs": sum(c.get("pairs", 0) for c in rows)}
        model = {"ingest_embedding": usage(ingest), "query_embedding": usage(query_embed),
                 "query_rerank": usage(rerank),
                 "mean_rerank_candidates_per_query": sum(c["pairs"] for c in rerank) / len(qrels),
                 "mean_rerank_candidates_per_call": sum(c["pairs"] for c in rerank) / max(1, len(rerank)),
                 "formal_window_unix": [start, end],
                 "excluded_nonformal_calls": len(calls) - len(formal) - len(ingest)}
        with (results / f"{product}-per-query-usage.csv").open("w") as stream:
            writer = csv.writer(stream)
            writer.writerow(["qid", "embedding_tokens", "embedding_calls", "rerank_tokens", "rerank_calls", "rerank_pairs"])
            for ranking in rankings:
                qid = ranking["qid"]
                e = usage([c for c in query_embed if c["id"] == qid])
                r = usage([c for c in rerank if c["id"] == qid])
                writer.writerow([qid, e["input_tokens"], e["calls"], r["input_tokens"], r["calls"], r["pairs"]])
        summaries[product] = {"quality_and_latency": quality, "model_usage": model}
    metrics = ("hit_at_1", "hit_at_5", "hit_at_10", "ndcg_at_10", "recall_at_100",
               "serial_p50_ms", "serial_p95_ms", "serial_p99_ms")
    delta = {m: summaries["weknora"]["quality_and_latency"][m] - summaries["contextbase"]["quality_and_latency"][m]
             for m in metrics}
    result = {"dataset": source["dataset"], "queries_per_product": len(qrels),
              "products": summaries, "delta_weknora_minus_contextbase": delta}
    (results / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    a = p.parse_args()
    result = summarize(a.data, a.results)
    print(json.dumps({"dataset": result["dataset"], "delta_weknora_minus_contextbase": result["delta_weknora_minus_contextbase"]}, indent=2))
