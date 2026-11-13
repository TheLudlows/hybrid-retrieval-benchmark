"""Recompute weighted-stage diagnostic metrics from compact native traces.

Also verifies each retained full-response gzip hash using the private work folder. Makes no API requests or model calls and never changes service data.
"""

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

import bench
from diagnose_fusion_stages import list_quality


def main(args):
    repo = Path(__file__).resolve().parents[1]
    out = args.results.resolve()
    work = args.work.resolve()
    topics, qrels = bench.topics_and_qrels(args.data)
    summaries = {
        row["qid"]: row for row in json.loads((out / "summary.json").read_text())
    }
    assert len(summaries) == len(topics) == 393
    analysis = json.loads((out / "analysis.json").read_text())
    source_contributions = 0
    rrf_records = 0
    raw_bytes = 0
    compact_bytes = 0
    miss_detail = []
    head_miss = Counter()
    head_rank = Counter()
    cohorts = {
        "semantic_head_lost": [],
        "semantic_head_rescued": [],
        "neither_head_positive": [],
    }
    for qid, query in topics.items():
        p = out / "traces" / (qid.replace("#", "-") + ".json.gz")
        t = json.load(gzip.open(p, "rt"))
        assert t["query"]["query_text"] == query
        assert t["query"]["budget"] == 1000 and t["query"]["max_tokens"] == 8192
        raw = work / "weighted-stage-diagnosis-raw" / p.name
        h = hashlib.sha256()
        with raw.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        assert h.hexdigest() == t["raw_response_gzip_sha256"]
        raw_bytes += raw.stat().st_size
        compact_bytes += p.stat().st_size
        arms = {}
        for arm in t["retrieval_results"]:
            arms.setdefault(arm["method_name"], []).extend(arm["results"])
        ranks = {
            name: {
                row["node_id"]: i
                for i, row in enumerate(
                    sorted(rows, key=lambda x: x["score"], reverse=True), 1
                )
            }
            for name, rows in arms.items()
        }
        stage = {
            name: list(
                dict.fromkeys(
                    row["official_id"]
                    for row in sorted(rows, key=lambda x: x["score"], reverse=True)
                )
            )
            for name, rows in arms.items()
        }
        stage.update(
            fusion=list(dict.fromkeys(row["official_id"] for row in t["rrf_merged"])),
            post_scoring_candidates=list(
                dict.fromkeys(row["official_id"] for row in t["reranked"])
            ),
            final=list(dict.fromkeys(row["official_id"] for row in t["results"])),
        )
        for row in t["rrf_merged"]:
            expected = 0
            for key, rank in row["source_ranks"].items():
                arm = key.removesuffix("_rank")
                assert ranks[arm][row["node_id"]] == rank <= 100
                expected += analysis["weights"][arm] / (60 + rank)
                source_contributions += 1
            assert abs(row["rrf_score"] - expected) < 1e-12
            rrf_records += 1
        row = summaries[qid]
        for name, ranking in stage.items():
            actual = list_quality(ranking, qrels[qid])
            assert actual == row["stage_quality"][name], (
                qid,
                name,
                actual,
                row["stage_quality"][name],
            )
        final = row["stage_quality"]["final"]["hit_at_1"]
        sem = row["stage_quality"]["semantic"]["hit_at_1"]
        bm = row["stage_quality"].get("bm25", {}).get("hit_at_1", 0)
        if sem and not final:
            cohorts["semantic_head_lost"].append(qid)
        if not sem and final:
            cohorts["semantic_head_rescued"].append(qid)
        if not sem and not bm and not final:
            cohorts["neither_head_positive"].append(qid)
        if not final:
            first = stage["final"][0]
            labels = qrels[qid]
            article = first.split("#")[0]
            label = labels.get(first)
            same_article = any(
                value > 0 and p.split("#")[0] == article for p, value in labels.items()
            )
            best = row["stage_quality"]["semantic"]["first_positive_rank"]
            top_sem = stage["semantic"][0]
            head_miss["top1_judged_zero" if label == 0 else "top1_unjudged"] += 1
            if same_article:
                head_miss["first_returned_shares_article_with_positive"] += 1
            rank_group = (
                "rank_1"
                if best == 1
                else "rank_2_5"
                if best and best <= 5
                else "rank_6_10"
                if best and best <= 10
                else "rank_11_plus"
                if best
                else "not_in_raw_semantic"
            )
            head_rank[rank_group] += 1
            miss_detail.append(
                {
                    "qid": qid,
                    "query": query,
                    "first_returned_id": first,
                    "first_returned_label": label,
                    "raw_semantic_first_id": top_sem,
                    "raw_semantic_first_label": labels.get(top_sem),
                    "first_positive_raw_semantic_rank": best,
                    "first_returned_shares_article_with_positive": same_article,
                }
            )
    assert rrf_records == analysis["verified_rrf_entries"]
    raw_calls = list(
        map(
            json.loads,
            gzip.open(out / "model-calls.jsonl.gz", "rt").read().splitlines(),
        )
    )
    calls = Counter(
        x["id"]
        for x in raw_calls
        if x["phase"] == "diagnostic" and x["kind"] == "embedding"
    )
    assert set(calls) == set(topics)
    assert all(x["kind"] == "embedding" for x in raw_calls)
    formal = repo / "results/miracl-zh-rrf-config-formal-20261113"
    probe = json.loads((formal / "probe-configured-dense97.json").read_text())
    ct = json.load(
        gzip.open(out / "traces" / (probe["qid"].replace("#", "-") + ".json.gz"), "rt")
    )
    assert ct["query_embedding_sha256"] == probe["query_embedding_sha256"]
    verification = {
        "queries_verified": 393,
        "all_stage_metrics_independently_recomputed": True,
        "rrf_entries_verified": rrf_records,
        "raw_source_rank_contributions_verified": source_contributions,
        "all_source_ranks_match_recorded_raw_arms_and_cap100": True,
        "complete_raw_response_gzip_sha256_checked": 393,
        "raw_response_bytes": raw_bytes,
        "compact_trace_bytes": compact_bytes,
        "one_embedding_call_per_query_verified": all(n == 1 for n in calls.values()),
        "query_embedding_matches_formal_probe": True,
        "model_rerank_calls": 0,
        "head_miss_counts": dict(head_miss),
        "first_positive_raw_semantic_rank_among_final_top1_misses": dict(head_rank),
        "cohort_query_ids": cohorts,
    }
    (out / "final-verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2) + "\n"
    )
    (out / "head-miss-content-diagnostics.json").write_text(
        json.dumps(miss_detail, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(verification, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    main(parser.parse_args())
