"""Independently rescore the four frozen native ablations and compare query gains/losses."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

import bench
from run_scale import save

QUALITY = (
    "hit_at_1",
    "hit_at_5",
    "hit_at_10",
    "ndcg_at_10",
    "recall_at_10",
    "recall_at_100",
)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main(args):
    repo = Path(__file__).resolve().parents[1]
    out = args.results.resolve()
    plan = json.loads((out / "experiment-plan.json").read_text())
    state = json.loads((out / "execution-status.json").read_text())
    assert state["state"] == "complete" and state["restored_baseline97"]
    assert (
        sha(repo / "miracl_zh/run_native_weight_ablation.py") == plan["driver_sha256"]
    )
    topics, qrels = bench.topics_and_qrels(args.data)
    source = bench.dataset_source(args.data)
    assert source["subset"]["file"]["sha256"] == plan["corpus_sha256"]
    all_rows = {}
    metrics = {}
    usage_summary = {}
    verification = {}
    file_hashes = {}
    for case in plan["cases"]:
        name = case["name"]
        folder = out / name
        rows = list(
            map(json.loads, (folder / "rankings.jsonl").read_text().splitlines())
        )
        assert [row["qid"] for row in rows] == list(topics)
        scored = bench.score(folder / "rankings.jsonl", qrels, None, source)
        assert scored == json.loads((folder / "metrics.json").read_text())
        assert scored["queries"] == 393 and scored["returned_depth"]["max"] <= 60
        cfg = json.loads((folder / "config.json").read_text())
        assert cfg["case"] == case and cfg["plan_sha256"] == sha(
            out / "experiment-plan.json"
        )
        native = json.loads((folder / "bank-config-readback.json").read_text())
        assert native["enable_text_search"] == case["text_search"]
        assert native["recall_rrf_semantic_weight"] == case["semantic_weight"]
        assert native["recall_rrf_bm25_weight"] == case["bm25_weight"]
        assert (
            not native["enable_reranking"]
            and not native["enable_graph_retrieval"]
            and not native["enable_temporal_retrieval"]
        )
        probe = json.load(gzip.open(folder / "native-probe.json.gz", "rt"))["trace"]
        assert probe["query"]["budget"] == 1000 and probe["query"]["max_tokens"] == 8192
        assert (
            hashlib.sha256(
                json.dumps(probe["query"]["query_embedding"]).encode()
            ).hexdigest()
            == plan["probe_embedding_sha256"]
        )
        arms = {}
        for arm in probe["retrieval_results"]:
            arms.setdefault(arm["method_name"], []).extend(arm["results"])
        assert bool(arms.get("bm25")) == case["text_search"]
        ranks = {
            name: {
                row["node_id"]: i
                for i, row in enumerate(
                    sorted(values, key=lambda x: x["score"], reverse=True), 1
                )
            }
            for name, values in arms.items()
        }
        weights = {
            "semantic": case["semantic_weight"],
            "bm25": case["bm25_weight"],
            "graph": 1.0,
            "temporal": 1.0,
        }
        for row in probe["rrf_merged"]:
            expected = 0.0
            for key, rank in row["source_ranks"].items():
                arm = key.removesuffix("_rank")
                assert ranks[arm][row["node_id"]] == rank <= 100
                expected += weights[arm] / (60 + rank)
            assert abs(expected - row["rrf_score"]) < 1e-12
        calls = list(
            map(
                json.loads,
                gzip.open(folder / "model-calls.jsonl.gz", "rt").read().splitlines(),
            )
        )
        assert all(row["kind"] == "embedding" for row in calls)
        query_calls = [row for row in calls if row["phase"] == "query"]
        qids = Counter(row["id"] for row in query_calls)
        assert set(qids) == set(topics)
        usage_summary[name] = {
            "successful_queries": 393,
            "embedding_calls": sum(row["calls"] for row in query_calls),
            "embedding_input_tokens": sum(row["input_tokens"] for row in query_calls),
            "query_ids_with_extra_embedding_attempts": {
                qid: count for qid, count in qids.items() if count > 1
            },
            "rerank_calls": 0,
            "rerank_input_tokens": 0,
            "average_model_rerank_candidates": 0,
            "warmup_embedding_calls": sum(
                row["calls"] for row in calls if row["phase"] == "warmup"
            ),
            "warmup_embedding_tokens": sum(
                row["input_tokens"] for row in calls if row["phase"] == "warmup"
            ),
            "probe_embedding_calls": sum(
                row["calls"] for row in calls if row["phase"] == "probe"
            ),
            "probe_embedding_tokens": sum(
                row["input_tokens"] for row in calls if row["phase"] == "probe"
            ),
            "current_ingest_embedding_calls": 0,
            "current_ingest_embedding_tokens": 0,
        }
        verification[name] = {
            "queries": len(rows),
            "all_metrics_and_per_query_values_recomputed": True,
            "raw_native_probe_rrf_entries_checked": len(probe["rrf_merged"]),
            "native_text_search_verified": case["text_search"],
            "query_embedding_unchanged": True,
            "model_rerank_calls": 0,
        }
        metrics[name] = scored
        all_rows[name] = {row["qid"]: row for row in rows}
        file_hashes[name] = {
            p.name: sha(p)
            for p in (
                folder / "rankings.jsonl",
                folder / "metrics.json",
                folder / "config.json",
                folder / "model-calls.jsonl.gz",
            )
        }
    baseline = metrics["baseline97"]
    comparisons = {}
    delta_rows = []
    for case in plan["cases"][1:]:
        name = case["name"]
        gain = []
        loss = []
        for qid in topics:
            before = baseline["per_topic"][qid]
            after = metrics[name]["per_topic"][qid]
            if after["hit_at_1"] > before["hit_at_1"]:
                gain.append(qid)
            if after["hit_at_1"] < before["hit_at_1"]:
                loss.append(qid)
            delta_rows.append(
                {
                    "case": name,
                    "qid": qid,
                    "query": topics[qid],
                    "baseline_first_id": all_rows["baseline97"][qid]["passages"][0],
                    "case_first_id": all_rows[name][qid]["passages"][0],
                    "baseline_first_label": qrels[qid].get(
                        all_rows["baseline97"][qid]["passages"][0]
                    ),
                    "case_first_label": qrels[qid].get(
                        all_rows[name][qid]["passages"][0]
                    ),
                    "hit_at_1_delta": after["hit_at_1"] - before["hit_at_1"],
                    "ndcg_at_10_delta": after["ndcg_at_10"] - before["ndcg_at_10"],
                    "recall_at_10_delta": after["recall_at_10"]
                    - before["recall_at_10"],
                    "recall_at_100_delta": after["recall_at_100"]
                    - before["recall_at_100"],
                }
            )
        comparisons[name] = {
            "quality_delta_vs_baseline97": {
                key: metrics[name][key] - baseline[key] for key in QUALITY
            },
            "head_gained": len(gain),
            "head_lost": len(loss),
            "head_net_gain": len(gain) - len(loss),
            "gained_qids": gain,
            "lost_qids": loss,
            "same_top1_ids": sum(
                all_rows[name][q]["passages"][:1]
                == all_rows["baseline97"][q]["passages"][:1]
                for q in topics
            ),
            "same_top10_ids": sum(
                all_rows[name][q]["passages"][:10]
                == all_rows["baseline97"][q]["passages"][:10]
                for q in topics
            ),
            "same_full_returned_lists": sum(
                all_rows[name][q]["passages"] == all_rows["baseline97"][q]["passages"]
                for q in topics
            ),
        }
    previous = repo / plan["historical_baseline"] / "rankings.jsonl"
    old = {
        row["qid"]: row for row in map(json.loads, previous.read_text().splitlines())
    }
    verification["baseline_reproduction"] = {
        "queries": 393,
        "full_rankings_equal": sum(
            all_rows["baseline97"][q]["passages"] == old[q]["passages"] for q in topics
        ),
        "top10_equal": sum(
            all_rows["baseline97"][q]["passages"][:10] == old[q]["passages"][:10]
            for q in topics
        ),
    }
    verification["mixed99_vs_semantic_only"] = {
        "same_first_ids": sum(
            all_rows["mixed99"][q]["passages"][0]
            == all_rows["semantic_only"][q]["passages"][0]
            for q in topics
        ),
        "same_top10_lists": sum(
            all_rows["mixed99"][q]["passages"][:10]
            == all_rows["semantic_only"][q]["passages"][:10]
            for q in topics
        ),
        "same_full_lists": sum(
            all_rows["mixed99"][q]["passages"]
            == all_rows["semantic_only"][q]["passages"]
            for q in topics
        ),
    }
    save(out / "independent-verification.json", verification)
    save(out / "model-usage-summary.json", usage_summary)
    save(out / "comparison.json", comparisons)
    save(out / "per-query-deltas.json", delta_rows)
    save(out / "artifact-fingerprints.json", file_hashes)
    lines = [
        "",
        "## 相对同期0.97/0.03基线的损益",
        "",
        "| 配置 | 首位新增命中 | 首位失去命中 | 首位净增 | ΔnDCG@10 | ΔRecall@10（百分点） |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for case in plan["cases"][1:]:
        item = comparisons[case["name"]]
        delta = item["quality_delta_vs_baseline97"]
        lines.append(
            f"| {case['label']} | {item['head_gained']} | {item['head_lost']} | {item['head_net_gain']:+d} | {delta['ndcg_at_10']:+.6f} | {100 * delta['recall_at_10']:+.3f} |"
        )
    lines += [
        "",
        "所有指标由逐题结果独立复算；收益/损失题目见per-query-deltas.json，模型成本见model-usage-summary.json。没有新增语料Embedding或入库耗时；每组预热和探针均排除在正式查询指标之外。",
        "",
        f"同期基线完整排名与此前归档一致{verification['baseline_reproduction']['full_rankings_equal']}/393题。0.99/0.01与纯向量消融的首位ID一致{verification['mixed99_vs_semantic_only']['same_first_ids']}/393题、Top10列表一致{verification['mixed99_vs_semantic_only']['same_top10_lists']}/393题。",
        "",
        "以上是固定候选配置的探索性实测，不作盲测或普适最优声明。原产品默认值、代码和原服务均未变更；不能把纯向量消融包装成混合检索成绩。",
        "",
        "## 复跑",
        "",
        "先按正式配置报告保留独立恢复库及私有设置，使用本轮冻结的experiment-plan.json与同版本源码。运行：",
        "",
        "```bash",
        "python miracl_zh/run_native_weight_ablation.py \\",
        "  --settings /private/runtime-settings.json --work /private/rrf-validation \\",
        "  --source /path/to/contextbase --python /path/to/venv/bin/python \\",
        "  --results results/native-weight-ablation-local",
        "python miracl_zh/analyze_native_weight_ablation.py \\",
        "  --data /path/to/miracl-zh-100k --results results/native-weight-ablation-local",
        "```",
        "",
        "本地目录需先复制本轮experiment-plan.json；脚本核验驱动及配置指纹。--publish仅用于明确要求在评测分支归档时启用，本地复跑默认不提交。未完成题持续fsync，原参数重跑可续跑；异常状态及恢复方式写入execution-status.json，私有API日志保留在验证运行目录。",
        "",
    ]
    report = out / "REPORT.md"
    original = (
        report.read_text().split("\n## 相对同期0.97/0.03基线的损益", 1)[0].rstrip()
    )
    report.write_text(original + "\n" + "\n".join(lines))
    print(
        json.dumps(
            {"comparisons": comparisons, "verification": verification},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    main(parser.parse_args())
