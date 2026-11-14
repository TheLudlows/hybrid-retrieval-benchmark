"""Recompute bounded native optimization, audit frozen selection, report tradeoffs."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

import bench
from optimize_contextbase_native import QUALITY, select_cases
from run_scale import save


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
        sha(repo / "miracl_zh/optimize_contextbase_native.py") == plan["driver_sha256"]
    )
    topics, qrels = bench.topics_and_qrels(args.data)
    source = bench.dataset_source(args.data)
    assert source["subset"]["file"]["sha256"] == plan["corpus_sha256"]
    for name, digest in plan["query_file_sha256"].items():
        assert sha(out / name) == digest
    tune = {q: t for q, t in topics.items() if q in set(plan["tune_query_ids"])}
    fresh = dict(
        line.split("\t", 1)
        for line in (out / "fresh-topics.tsv").read_text().splitlines()
    )
    fresh_labels = {q: {} for q in fresh}
    for line in (out / "fresh-qrels.tsv").read_text().splitlines():
        q, _, p, label = line.split()
        fresh_labels[q][p] = int(label)
    sets = {"tune100": tune, "dev393": topics, "fresh_train200": fresh}
    all_metrics = {}
    all_rows = {}
    usage = {}
    verification = {}
    cases = {case["name"]: case for case in plan["cases"]}
    for split, subset in sets.items():
        for case in plan["cases"]:
            folder = out / split / case["name"]
            path = folder / "metrics.json"
            if not path.exists():
                continue
            labels = (
                fresh_labels
                if split == "fresh_train200"
                else {q: qrels[q] for q in subset}
            )
            src = (
                {
                    **source,
                    "dataset": "miracl/zh/"
                    + split
                    + "/subset-"
                    + plan["corpus_sha256"][:16],
                }
                if split != "dev393"
                else source
            )
            rows = list(
                map(json.loads, (folder / "rankings.jsonl").read_text().splitlines())
            )
            assert [r["qid"] for r in rows] == list(subset)
            assert all(0 < len(row["passages"]) <= 60 for row in rows)
            actual = bench.score(folder / "rankings.jsonl", labels, None, src)
            assert actual == json.loads(path.read_text())
            label = split + "/" + case["name"]
            all_metrics[label] = actual
            all_rows[label] = {row["qid"]: row for row in rows}
            config = json.loads((folder / "config.json").read_text())
            assert config["case"] == case and config["plan_sha256"] == sha(
                out / "experiment-plan.json"
            )
            native = json.loads((folder / "bank-config-readback.json").read_text())
            assert native["enable_text_search"] and not native["enable_reranking"]
            assert (
                native["recall_rrf_semantic_weight"] == case["semantic_weight"]
                and native["recall_rrf_bm25_weight"] == case["bm25_weight"]
            )
            assert (
                native["recall_budget_fixed_high"] == case["high_budget"]
                and native["recall_budget_function"] == "fixed"
            )
            t = json.load(gzip.open(folder / "native-probe.json.gz", "rt"))["trace"]
            assert t["query"]["budget"] == case["high_budget"]
            phases = {
                phase["phase_name"]: phase["details"]
                for phase in t["summary"]["phase_metrics"]
            }
            assert phases["reranking"]["reranker_type"] == "rrf-passthrough"
            assert (
                hashlib.sha256(
                    json.dumps(t["query"]["query_embedding"]).encode()
                ).hexdigest()
                == plan["probe_embedding_sha256"]
            )
            arms = {}
            for arm in t["retrieval_results"]:
                assert len(arm["results"]) <= case["high_budget"]
                arms.setdefault(arm["method_name"], []).extend(arm["results"])
            assert arms["bm25"]
            ranks = {
                name: {
                    row["node_id"]: i
                    for i, row in enumerate(
                        sorted(values, key=lambda r: r["score"], reverse=True), 1
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
            assert phases["rrf_merge"]["rrf_weights"] == weights
            for row in t["rrf_merged"]:
                expected = 0
                for name, rank in row["source_ranks"].items():
                    arm = name.removesuffix("_rank")
                    assert ranks[arm][row["node_id"]] == rank <= 100
                    expected += weights[arm] / (60 + rank)
                assert abs(row["rrf_score"] - expected) < 1e-12
            calls = list(
                map(
                    json.loads,
                    gzip.open(folder / "model-calls.jsonl.gz", "rt")
                    .read()
                    .splitlines(),
                )
            )
            assert all(row["kind"] == "embedding" for row in calls)
            query_calls = [row for row in calls if row["phase"] == "query"]
            counts = Counter(row["id"] for row in query_calls)
            assert set(counts) == set(subset)
            usage[label] = {
                "successful_queries": len(rows),
                "query_embedding_calls": sum(row["calls"] for row in query_calls),
                "query_embedding_input_tokens": sum(
                    row["input_tokens"] for row in query_calls
                ),
                "rerank_calls": 0,
                "rerank_input_tokens": 0,
                "average_model_rerank_candidates": 0,
                "warmup_calls": sum(
                    row["calls"] for row in calls if row["phase"] == "warmup"
                ),
                "warmup_tokens": sum(
                    row["input_tokens"] for row in calls if row["phase"] == "warmup"
                ),
                "probe_calls": sum(
                    row["calls"] for row in calls if row["phase"] == "probe"
                ),
                "probe_tokens": sum(
                    row["input_tokens"] for row in calls if row["phase"] == "probe"
                ),
                "extra_model_attempt_query_ids": {
                    qid: n for qid, n in counts.items() if n > 1
                },
                "current_ingest_embedding_calls": 0,
                "current_ingest_embedding_tokens": 0,
            }
            verification[label] = {
                "successful_queries": len(rows),
                "all_metrics_independently_recomputed": True,
                "native_budget_verified": case["high_budget"],
                "native_weight_verified": True,
                "bm25_active_in_probe": True,
                "query_embedding_unchanged": True,
                "rrf_entries_verified": len(t["rrf_merged"]),
                "raw_probe_arm_depths": {
                    name: len(rows) for name, rows in arms.items()
                },
                "model_rerank_calls": 0,
                "returned_depth": actual["returned_depth"],
                "rankings_sha256": sha(folder / "rankings.jsonl"),
            }
    tune_metrics = {name: all_metrics["tune100/" + name] for name in cases}
    expected = select_cases(plan["cases"], tune_metrics)
    frozen = json.loads((out / "frozen-selection.json").read_text())
    assert all(expected[k] == frozen[k] for k in expected)
    assert all(
        frozen["tuning_metrics_sha256"][name]
        == sha(out / "tune100" / name / "metrics.json")
        for name in cases
    )
    assert set(all_metrics) == {"tune100/" + name for name in cases} | {
        split + "/" + name
        for split in ("dev393", "fresh_train200")
        for name in frozen["assessment_cases"]
    }
    baseline = plan["cases"][0]["name"]
    deltas = {}
    per_query = []
    for split in ("dev393", "fresh_train200"):
        base = all_metrics[split + "/" + baseline]
        for name in frozen["assessment_cases"]:
            m = all_metrics[split + "/" + name]
            gained = []
            lost = []
            same = 0
            for qid in sets[split]:
                b = base["per_topic"][qid]
                a = m["per_topic"][qid]
                if a["hit_at_1"] > b["hit_at_1"]:
                    gained.append(qid)
                if a["hit_at_1"] < b["hit_at_1"]:
                    lost.append(qid)
                before = all_rows[split + "/" + baseline][qid]["passages"]
                after = all_rows[split + "/" + name][qid]["passages"]
                same += int(before == after)
                per_query.append(
                    {
                        "query_set": split,
                        "case": name,
                        "qid": qid,
                        "hit_at_1_delta": a["hit_at_1"] - b["hit_at_1"],
                        "ndcg_at_10_delta": a["ndcg_at_10"] - b["ndcg_at_10"],
                        "recall_at_10_delta": a["recall_at_10"] - b["recall_at_10"],
                        "recall_at_100_delta": a["recall_at_100"] - b["recall_at_100"],
                        "baseline_top1": before[0],
                        "case_top1": after[0],
                    }
                )
            deltas[split + "/" + name] = {
                "quality_delta": {k: m[k] - base[k] for k in QUALITY},
                "head_gained": len(gained),
                "head_lost": len(lost),
                "head_net_gain": len(gained) - len(lost),
                "ndcg_gained_queries": sum(
                    m["per_topic"][q]["ndcg_at_10"]
                    > base["per_topic"][q]["ndcg_at_10"] + 1e-12
                    for q in sets[split]
                ),
                "ndcg_lost_queries": sum(
                    m["per_topic"][q]["ndcg_at_10"]
                    < base["per_topic"][q]["ndcg_at_10"] - 1e-12
                    for q in sets[split]
                ),
                "gained_qids": gained,
                "lost_qids": lost,
                "same_full_rankings": same,
                "p50_ms_delta": m["serial_p50_ms"] - base["serial_p50_ms"],
                "p95_ms_delta": m["serial_p95_ms"] - base["serial_p95_ms"],
                "p99_ms_delta": m["serial_p99_ms"] - base["serial_p99_ms"],
            }
    result = {
        "frozen_selection_verified": True,
        "assessed_configs": frozen["assessment_cases"],
        "stage_verification": verification,
        "successful_queries": sum(x["queries"] for x in all_metrics.values()),
        "query_embedding_calls": sum(
            x["query_embedding_calls"] for x in usage.values()
        ),
        "query_embedding_input_tokens": sum(
            x["query_embedding_input_tokens"] for x in usage.values()
        ),
        "model_rerank_calls": 0,
        "restored_baseline97": True,
    }
    save(out / "independent-verification.json", result)
    save(out / "assessment-comparison.json", deltas)
    save(out / "model-usage-summary.json", usage)
    model_total = {
        key: sum(stage[key] for stage in usage.values())
        for key in (
            "successful_queries",
            "query_embedding_calls",
            "query_embedding_input_tokens",
            "rerank_calls",
            "rerank_input_tokens",
            "warmup_calls",
            "warmup_tokens",
            "probe_calls",
            "probe_tokens",
            "current_ingest_embedding_calls",
            "current_ingest_embedding_tokens",
        )
    }
    model_total["average_model_rerank_candidates"] = 0
    model_total["all_embedding_calls_including_warmup_and_probe"] = sum(
        model_total[k] for k in ("query_embedding_calls", "warmup_calls", "probe_calls")
    )
    model_total["all_embedding_tokens_including_warmup_and_probe"] = sum(
        model_total[k]
        for k in ("query_embedding_input_tokens", "warmup_tokens", "probe_tokens")
    )
    save(out / "model-usage-total.json", model_total)
    save(out / "per-query-deltas.json", per_query)
    # The full dev score contains the screened 100 queries. Show its complement
    # as a descriptive check, without selecting again or calling it blind data.
    cohorts = {
        "screened_dev100": list(tune),
        "remaining_dev293": [qid for qid in topics if qid not in tune],
        "historical_train200": list(fresh),
    }
    cohort_results = {}
    for cohort, qids in cohorts.items():
        split = "fresh_train200" if cohort == "historical_train200" else "dev393"
        base = all_metrics[split + "/" + baseline]["per_topic"]
        for name in frozen["assessment_cases"]:
            rows = all_metrics[split + "/" + name]["per_topic"]
            means = {
                key: sum(rows[q][key] for q in qids) / len(qids) for key in QUALITY
            }
            cohort_results[cohort + "/" + name] = {
                "queries": len(qids),
                "quality": means,
                "quality_delta_vs_baseline": {
                    key: sum(rows[q][key] - base[q][key] for q in qids) / len(qids)
                    for key in QUALITY
                },
                "head_gained": sum(
                    rows[q]["hit_at_1"] > base[q]["hit_at_1"] for q in qids
                ),
                "head_lost": sum(
                    rows[q]["hit_at_1"] < base[q]["hit_at_1"] for q in qids
                ),
            }
    save(out / "assessment-cohorts.json", cohort_results)
    budget_comparison = {}
    for split in ("dev393", "fresh_train200"):
        quality = all_metrics[split + "/" + frozen["quality_case"]]
        efficient = all_metrics[split + "/" + frozen["efficiency_case"]]
        qrows = all_rows[split + "/" + frozen["quality_case"]]
        erows = all_rows[split + "/" + frozen["efficiency_case"]]
        budget_comparison[split] = {
            "queries": len(sets[split]),
            "same_aggregate_quality": all(quality[k] == efficient[k] for k in QUALITY),
            "same_per_query_quality": all(
                quality["per_topic"][q][k] == efficient["per_topic"][q][k]
                for q in sets[split]
                for k in QUALITY
            ),
            "same_top10_lists": sum(
                qrows[q]["passages"][:10] == erows[q]["passages"][:10]
                for q in sets[split]
            ),
            "same_full_lists": sum(
                qrows[q]["passages"] == erows[q]["passages"] for q in sets[split]
            ),
            "p95_reduction_fraction": 1
            - efficient["serial_p95_ms"] / quality["serial_p95_ms"],
        }
    save(out / "budget-comparison.json", budget_comparison)
    lines = [
        "",
        "## 冻结候选与复核差值",
        "",
        f"100题筛选得到质量候选 `{frozen['quality_case']}`、效率候选 `{frozen['efficiency_case']}`。选择规则、9组筛选数据指纹及候选名单已在393题/额外200题请求前写入frozen-selection.json并归档。",
        "",
        "| 查询集 | 配置 | ΔHit@1（百分点） | ΔnDCG@10 | ΔRecall@10（百分点） | 首位净增题 | Δp95 ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for split in ("dev393", "fresh_train200"):
        for name in frozen["assessment_cases"]:
            d = deltas[split + "/" + name]
            q = d["quality_delta"]
            lines.append(
                f"| {split} | {name} | {100 * q['hit_at_1']:+.3f} | {q['ndcg_at_10']:+.6f} | {100 * q['recall_at_10']:+.3f} | {d['head_net_gain']:+d} | {d['p95_ms_delta']:+.2f} |"
            )
    lines += [
        "",
        f"本轮成功采集{result['successful_queries']}次正式请求，实际查询Embedding {result['query_embedding_calls']}次、输入{result['query_embedding_input_tokens']} Token，模型重排调用/Token均为0。预热与探针单列在model-usage-summary.json，不计入检索指标。入库Embedding本轮新增0；历史入库记录仍保留，不重报为本轮新耗时。",
        "",
        "本轮“最优”仅限注册9组参数及本次子集/环境。100题筛选候选不保证在393题或额外200题上仍占优；两套复核结果均公开，不只挑高分展示。额外200题是此前已使用的官方train查询样本，不是盲测或模型从未见过的保证。",
        "",
        "### 分开查看调参题与其余开发题",
        "",
        "393题成绩包含本轮筛选用的100题。以下只从已采集结果分组复算，不追加请求或改变冻结候选；其余293题也被历史实验使用过，不称为盲测。",
        "",
        "| 题目分组 | 配置 | 题数 | Hit@1 | nDCG@10 | Recall@10 | ΔnDCG@10 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for cohort in cohorts:
        for name in frozen["assessment_cases"]:
            c = cohort_results[cohort + "/" + name]
            q = c["quality"]
            lines.append(
                f"| {cohort} | {name} | {c['queries']} | {q['hit_at_1']:.2%} | {q['ndcg_at_10']:.6f} | {q['recall_at_10']:.2%} | {c['quality_delta_vs_baseline']['ndcg_at_10']:+.6f} |"
            )
    lines.append("")
    lines += [
        "## 预算复核与实际返回深度",
        "",
        "| 查询集 | 配置 | 平均深度 | 最小—最大 | Embedding调用/输入Token | 模型重排调用/Token |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for split in ("dev393", "fresh_train200"):
        for name in frozen["assessment_cases"]:
            m = all_metrics[split + "/" + name]
            depth = m["returned_depth"]
            u = usage[split + "/" + name]
            lines.append(
                f"| {split} | {name} | {depth['mean']:.3f} | {depth['min']}—{depth['max']} | {u['query_embedding_calls']}/{u['query_embedding_input_tokens']} | 0/0 |"
            )
    lines += [
        "",
        "Recall@100按不足100条的实际返回列表计分，并不表示接口返回了100条。",
        "",
        "| 查询集 | 两候选逐题六项质量一致 | Top10列表一致 | 完整列表一致 | 效率候选p95下降 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for split, check in budget_comparison.items():
        n = check["queries"]
        lines.append(
            f"| {split} | {check['same_per_query_quality']} | {check['same_top10_lists']}/{n} | {check['same_full_lists']}/{n} | {check['p95_reduction_fraction']:.2%} |"
        )
    lines += [
        "",
        "质量指标相同不代表全部段落ID排名相同。各配置按固定顺序串行运行，耗时仅描述本轮窗口，没有交错重复试验或普适加速保证。",
        "",
        "## 本轮建议",
        "",
    ]
    efficient = frozen["efficiency_case"]
    em = all_metrics["dev393/" + efficient]
    bm = all_metrics["dev393/" + baseline]
    if all(c["same_per_query_quality"] for c in budget_comparison.values()):
        lines.append(
            f"在已冻结候选范围内，建议把`{efficient}`作为下一次部署验证的候选，见[具体配置](recommended-config.json)。它在393题和额外200题上的逐题六项质量与质量候选一致；393题p95为{em['serial_p95_ms']:.2f}ms，相对同期基线下降{1 - em['serial_p95_ms'] / bm['serial_p95_ms']:.2%}。本轮没有将其应用到原服务。"
        )
    else:
        lines.append(
            "效率候选未完全保持逐题质量，应结合上表损益取舍，不能宣称其无损替代质量候选。"
        )
    lines += [
        "",
        f"相对同期0.99/0.01基线，393题nDCG@10变化{em['ndcg_at_10'] - bm['ndcg_at_10']:+.6f}、Recall@10变化{100 * (em['recall_at_10'] - bm['recall_at_10']):+.3f}个百分点，Hit@1变化{100 * (em['hit_at_1'] - bm['hit_at_1']):+.3f}个百分点。收益很小，不能写成首位命中大幅提升或全局最优。",
        "",
        f"相对基线，393题中有{deltas['dev393/' + efficient]['ndcg_gained_queries']}题nDCG@10上升、{deltas['dev393/' + efficient]['ndcg_lost_queries']}题下降；额外200题中有{deltas['fresh_train200/' + efficient]['ndcg_gained_queries']}题上升、{deltas['fresh_train200/' + efficient]['ndcg_lost_queries']}题下降。逐题损益完整保存，不只展示获益题。",
        "",
        "本轮使用独立恢复库和固定特性源码cdf6430；可配置RRF权重依赖[草稿PR #108](https://github.com/RSIXDB/hindsight/pull/108)，原服务没有切换该版本。原数据、索引、检查点和备份保留；索引保留复核见data-retention.json及两端retained-original-*-audit.json。",
        "",
        "开始时发现原模型/API进程及数据库容器已停止，原因未确定。按保留配置恢复进程后，模型设置/文件指纹核对通过，按同一预注册方案重新开始；此前预检失败没有发出正式查询。见service-recovery.json。运行器中original_service_untouched仅指评测执行期间未切换原服务参数/源码，不表示预检前未恢复进程。",
        "",
    ]
    historical = {
        "ContextBase 0.95/0.05（历史）": "miracl-zh-native-weight-ablation-20261113/mixed95",
        "ContextBase 0.97/0.03（历史）": "miracl-zh-native-weight-ablation-20261113/baseline97",
        "WeKnora Lite k30（历史）": "miracl-zh-native-no-rerank-20261113/100k/weknora/rrf_k30-dev393-cap60",
    }
    snapshots = {}
    lines += [
        "## 与已归档混合检索结果对照",
        "",
        "相同10万段及393题、均关闭模型重排；历史排名在此重新计分，但本轮没有重发这些配置或WeKnora请求。ContextBase经过追加调优及RRF配置源码改动，因此这不是两产品等调参预算的新比较；跨窗口时延不放在此表中比较。",
        "",
        "| 配置 | Hit@1 | Hit@5 | Hit@10 | nDCG@10 | Recall@10 | Recall@100 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, relative in historical.items():
        folder = repo / "results" / relative
        m = bench.score(folder / "rankings.jsonl", qrels, None, source)
        assert m == json.loads((folder / "metrics.json").read_text())
        snapshots[label] = {
            "source": "results/" + relative + "/metrics.json",
            "queries": m["queries"],
            **{key: m[key] for key in QUALITY},
        }
        lines.append(
            f"| [{label}](../{relative}/metrics.json) | {m['hit_at_1']:.2%} | {m['hit_at_5']:.2%} | {m['hit_at_10']:.2%} | {m['ndcg_at_10']:.6f} | {m['recall_at_10']:.2%} | {m['recall_at_100']:.2%} |"
        )
    lines += [
        f"| ContextBase {efficient}（本轮） | {em['hit_at_1']:.2%} | {em['hit_at_5']:.2%} | {em['hit_at_10']:.2%} | {em['ndcg_at_10']:.6f} | {em['recall_at_10']:.2%} | {em['recall_at_100']:.2%} |",
        "",
        "本轮候选更偏首位/前列排序；此前0.95/0.05及0.97/0.03的Recall@10仍较高，不能宣称新配置在所有质量指标上最好。历史入库耗时和入库Embedding用量只引用[可核验历史记录](../miracl-zh-native-no-rerank-20261113/REPORT.md)，本轮新增入库为0。",
        "",
    ]
    save(out / "historical-comparison.json", snapshots)
    report = out / "REPORT.md"
    original = report.read_text().split("\n## 冻结候选与复核差值", 1)[0].rstrip()
    report.write_text(original + "\n" + "\n".join(lines))
    print(
        json.dumps(
            {
                "selection": frozen,
                "assessment_deltas": deltas,
                "summary": {
                    k: v for k, v in result.items() if k != "stage_verification"
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    main(parser.parse_args())
