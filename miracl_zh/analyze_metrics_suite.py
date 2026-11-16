"""Verify saved native metrics windows and render the complete comparison."""

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics
from urllib.request import urlopen

import bench
from extend_retrieval_metrics import evaluate
from run_scale import save


def load(path):
    return json.loads(path.read_text())


def percentile(values, p):
    return sorted(values)[math.ceil(p * len(values)) - 1]


def main(args):
    out = args.results.resolve()
    repo = Path(__file__).resolve().parents[1]
    plan = load(out / "experiment-plan.json")
    state = load(out / "execution-status.json")
    assert state["state"] == "complete" and state["restored_contextbase_baseline97"]
    for relative, expected in plan["code_sha256"].items():
        assert hashlib.sha256((repo / relative).read_bytes()).hexdigest() == expected
    source = bench.dataset_source(args.data)
    topics, qrels = bench.topics_and_qrels(args.data)
    schedule = [
        {"product": p, "label": "serial393", "concurrency": 1}
        for p in ("contextbase", "weknora")
    ] + plan["load_schedule"]
    all_stages = {}
    stage_rows = {}
    for entry in schedule:
        label, product = entry["label"], entry["product"]
        folder = out / "runs" / label / product
        m = load(folder / "metrics.json")
        attempt = folder / m["attempt_directory"]
        config = load(attempt / "config.json")
        assert (
            config["plan_sha256"]
            == hashlib.sha256((out / "experiment-plan.json").read_bytes()).hexdigest()
        )
        qids = list(topics) if label == "serial393" else plan["load_query_ids"]
        assert (
            config["query_ids"] == qids
            and config["concurrency"] == entry["concurrency"]
        )
        rows = [
            json.loads(line)
            for line in (attempt / "rankings.jsonl").read_text().splitlines()
        ]
        assert len(rows) == len(qids) and {r["qid"] for r in rows} == set(qids)
        recomputed = evaluate(
            attempt / "rankings.jsonl", {q: qrels[q] for q in qids}, source
        )
        assert recomputed == load(attempt / "quality.json")
        for key, value in recomputed.items():
            if key != "per_topic":
                assert m[key] == value
        assert m["successful_queries"] == sum(row["success"] for row in rows)
        assert m["successful_qps"] == m["successful_queries"] / m["active_window_s"]
        assert m["active_window_s"] + 0.01 >= max(
            r["completed_unix"] for r in rows
        ) - min(r["begin_unix"] for r in rows)
        all_stages[label + "/" + product] = m
        stage_rows[label + "/" + product] = rows

    quality = {
        p: load(
            out
            / "runs/serial393"
            / p
            / all_stages["serial393/" + p]["attempt_directory"]
            / "quality.json"
        )
        for p in ("contextbase", "weknora")
    }
    keys = (
        "hit_at_1",
        "hit_at_5",
        "hit_at_10",
        "ndcg_at_10",
        "recall_at_10",
        "recall_at_100",
        "mrr_at_10",
        "precision_at_5",
        "precision_at_10",
        "unjudged_at_5",
        "unjudged_at_10",
    )
    archived = load(out / "quality-comparison.json")
    historic = archived["metrics"]
    consistency = {}
    for product, relative in archived["source_rankings"].items():
        previous = {
            r["qid"]: r["passages"]
            for r in map(
                json.loads,
                (repo / "results" / relative / "rankings.jsonl")
                .read_text()
                .splitlines(),
            )
        }
        current = {r["qid"]: r["passages"] for r in stage_rows["serial393/" + product]}
        assert previous.keys() == current.keys()
        consistency[product] = {
            "queries": len(current),
            "same_top1": sum(current[q][:1] == previous[q][:1] for q in current),
            "same_top10": sum(current[q][:10] == previous[q][:10] for q in current),
            "same_full_lists": sum(current[q] == previous[q] for q in current),
        }
    save(out / "serial-vs-archive-consistency.json", consistency)
    save(
        out / "current-quality-comparison.json",
        {
            "metrics": {p: {k: q[k] for k in keys} for p, q in quality.items()},
            "same_all_aggregate_metrics_as_archived": {
                p: all(q[k] == historic[p][k] for k in keys) for p, q in quality.items()
            },
        },
    )

    load_summary = {}
    for c in (1, 2, 4):
        for product in ("contextbase", "weknora"):
            names = [f"load100-r{rep}-c{c}/{product}" for rep in (1, 2)]
            windows = [all_stages[name] for name in names]
            rows = [row for name in names for row in stage_rows[name]]
            success = sum(row["success"] for row in rows)
            lat = [row["latency_ms"] for row in rows]
            load_summary[f"c{c}/" + product] = {
                "queries": len(rows),
                "successes": success,
                "success_rate": success / len(rows),
                "combined_successful_qps": success
                / sum(m["active_window_s"] for m in windows),
                "per_round_successful_qps": [m["successful_qps"] for m in windows],
                "request_p50_ms": percentile(lat, 0.5),
                "request_p95_ms": percentile(lat, 0.95),
                "request_p99_ms": percentile(lat, 0.99),
                "per_round_ndcg_at_10": [m["ndcg_at_10"] for m in windows],
                "per_round_hit_at_1": [m["hit_at_1"] for m in windows],
                "per_round_recall_at_10": [m["recall_at_10"] for m in windows],
                "limitations": "Two100-query closed-loop windows; time-weighted combined QPS; not393 distinct queries or a sustainable capacity guarantee.",
            }
    save(out / "load-comparison.json", load_summary)
    batch_diagnostic = None
    vector_path = out / "embedding-batch-diagnostic/vectors.json.gz"
    if vector_path.exists():
        vectors = json.load(gzip.open(vector_path, "rt"))
        summary = load(out / "embedding-batch-diagnostic/summary.json")
        checks = []
        cosines = []
        for qid in summary["selected_query_ids"]:
            values = {
                (v["mode"], v["round"]): v["vector"] for v in vectors if v["qid"] == qid
            }
            assert values[("single", 1)] == values[("single", 2)]
            assert (
                values[("concurrent_four", 1)]
                == values[("concurrent_four", 2)]
                == values[("joint_http_batch", 1)]
            )
            assert values[("single", 1)] != values[("concurrent_four", 1)]
            checks.append(qid)
            a, b = values[("single", 1)], values[("concurrent_four", 1)]
            cosines.append(
                math.fsum(x * y for x, y in zip(a, b, strict=True))
                / math.sqrt(math.fsum(x * x for x in a) * math.fsum(x * x for x in b))
            )
        batch_diagnostic = {
            "post_hoc": True,
            "verified_queries": checks,
            "all_single_repeats_equal": True,
            "all_concurrent_equals_joint_batch": True,
            "all_batch_vectors_differ_from_single": True,
            "single_vs_batch_cosine_min": min(cosines),
            "single_vs_batch_cosine_max": max(cosines),
            "scope": "Four selected examples prove batch-dependent service outputs, not full causality for every native rank change. No benchmark parameter or main score replaced.",
        }
        save(
            out / "embedding-batch-diagnostic/independent-verification.json",
            batch_diagnostic,
        )

    calls = []
    with gzip.open(out / "model-calls-all.jsonl.gz", "wt") as dst:
        for line in (args.measurement_dir / "model-usage.jsonl").open():
            row = json.loads(line)
            if not row.get("run_id", "").startswith("metrics-suite-20261116/"):
                continue
            assert row["kind"] == "embedding"
            calls.append(row)
            dst.write(line)
    usage = {}
    for row in calls:
        key = row["product"] + "/" + row["phase"]
        group = usage.setdefault(key, {"calls": 0, "input_tokens": 0})
        group["calls"] += row["calls"]
        group["input_tokens"] += row["input_tokens"]
    save(
        out / "model-usage-complete.json",
        {
            "by_product_phase": usage,
            "model_rerank_calls": 0,
            "generation_calls": 0,
            "average_model_rerank_candidates": None,
            "reranking_status": "disabled; no model rerank calls",
            "note": "Visibility combines synthetic document embedding and native query probes. All retained timing attempts and probes are included, not just successful rows.",
        },
    )

    visibility = {}
    for product in ("contextbase", "weknora"):
        folder = out / "visibility" / product
        assert (
            load(folder / "complete.json")["repetitions"]
            == plan["visibility_repetitions"]
        )
        docs = [load(path) for path in sorted(folder.glob("document-*.json"))]
        assert len(docs) == plan["visibility_repetitions"] and all(
            d["complete"] for d in docs
        )
        for op in ("create", "update", "delete"):
            rows = [v for d in docs for v in d["operations"] if v["operation"] == op]
            assert len(rows) == len(docs) and all(
                v["observations"][-1]["visible"] for v in rows
            )
            visibility[product + "/" + op] = {
                "samples": len(rows),
                "write_ack_p50_ms": statistics.median(r["write_ack_ms"] for r in rows),
                "ack_to_first_observed_p50_ms": statistics.median(
                    r["ack_to_first_observed_visible_ms"] for r in rows
                ),
                "submit_to_first_observed_p50_ms": statistics.median(
                    r["submit_to_first_observed_visible_ms"] for r in rows
                ),
                "submit_to_first_observed_max_ms": max(
                    r["submit_to_first_observed_visible_ms"] for r in rows
                ),
            }
    save(out / "visibility-comparison.json", visibility)
    fingerprints = [
        load(out / name)["canonical_content_sha256"]
        for name in (
            "contextbase-final-audit.json",
            "weknora-final-audit.json",
            "contextbase-original-final-audit.json",
            "weknora-original-final-audit.json",
        )
    ]
    assert set(fingerprints) == {plan["canonical_corpus_sha256"]}
    model_info = load(out / "model-info.json")
    with urlopen("http://127.0.0.1:18090/hybrid/info", timeout=10) as response:
        assert json.load(response) == model_info
    for name, entry in load(out / "model-fingerprints.json")["files"].items():
        with (repo / "deployment/models" / name).open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == entry["sha256"]
    verify = {
        "completed_query_windows": len(schedule),
        "formal_attempts": sum(m["attempted_queries"] for m in all_stages.values()),
        "successes": sum(m["successful_queries"] for m in all_stages.values()),
        "all_rankings_rescored": True,
        "all_four_indexes_same_canonical_corpus_sha256": fingerprints[0],
        "generation_model_used": False,
        "model_rerank_calls": 0,
        "model_files_and_runtime_unchanged": True,
        "restored_contextbase_baseline97": state["restored_contextbase_baseline97"],
        "visibility_operations_verified": sum(
            v["samples"] for v in visibility.values()
        ),
    }
    assert verify["formal_attempts"] == plan["expected_formal_query_attempts"]
    verify["serial_rankings_vs_archive"] = consistency
    save(out / "independent-verification.json", verify)

    rows = [
        "# 知识库指标完整实测：ContextBase 与 WeKnora Lite",
        "",
        "同一MIRACL v1.0中文10万段、393题，官方段落ID计分。共享同版本BGE-M3 ONNX INT8 CPU，关闭模型重排；按用户要求不使用生成模型、不评测答案正确率。ContextBase此前追加调优并使用cdf6430特性源码，本轮冻结两端配置后复测，不再选参。",
        "",
        "主要观察：ContextBase前列排序指标领先，Precision@10差距较小；本轮SQLite版WeKnora的并发吞吐较早饱和。并发下两端质量均有波动，事后诊断发现共享INT8模型服务的批次相关输出变化，因此不能把吞吐提升称为质量完全无损的扩展。ContextBase原生数据库体积更大，资源成本单列。",
        "",
        "## 同期393题质量",
        "",
        "| 指标 | ContextBase | WeKnora Lite | 差值 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key in keys:
        a, b = quality["contextbase"][key], quality["weknora"][key]
        cells = (
            f"{a:.6f} | {b:.6f} | {a - b:+.6f}"
            if key in ("ndcg_at_10", "mrr_at_10")
            else f"{a:.2%} | {b:.2%} | {100 * (a - b):+.3f}个百分点"
        )
        title = {
            "hit_at_1": "Hit@1",
            "hit_at_5": "Hit@5",
            "hit_at_10": "Hit@10",
            "ndcg_at_10": "nDCG@10",
            "recall_at_10": "Recall@10",
            "recall_at_100": "Recall@100",
            "mrr_at_10": "MRR@10",
            "precision_at_5": "Precision@5",
            "precision_at_10": "Precision@10",
            "unjudged_at_5": "未标注率@5",
            "unjudged_at_10": "未标注率@10",
        }[key]
        rows.append(f"| {title} | {cells} |")
    ceiling = {
        str(k): statistics.mean(
            min(k, sum(labels.values())) / k for labels in qrels.values()
        )
        for k in (5, 10)
    }
    save(
        out / "qrels-precision-ceilings.json",
        {
            "judged_positive_pairs": sum(sum(v.values()) for v in qrels.values()),
            "qrels_precision_upper_bound": ceiling,
            "limitation": "Upper bound under existing qrels convention, not the true relevance precision of unjudged documents.",
        },
    )
    rows += [
        "",
        f"Precision按固定K作分母，未标注按0计分；当前qrels中共有994条正例，按现有标注可达到的P@5/P@10上限分别为{ceiling['5']:.2%}/{ceiling['10']:.2%}。这解释了P@10数值较低的一部分原因，并非真实人工相关率上限。未标注率单列，不把未标注等同于人工判定不相关。",
        "",
        f"串行复测与此前归档的完整段落ID排名一致：ContextBase {consistency['contextbase']['same_full_lists']}/393题、WeKnora {consistency['weknora']['same_full_lists']}/393题；不只是总分相同，见serial-vs-archive-consistency.json。",
        "",
        "## 同期393题串行性能",
        "",
        "| 项目 | ContextBase | WeKnora Lite |",
        "| --- | ---: | ---: |",
    ]
    for title, key in (
        ("请求p50 ms", "serial_p50_ms"),
        ("请求p95 ms", "serial_p95_ms"),
        ("请求p99 ms", "serial_p99_ms"),
        ("成功QPS", "successful_qps"),
        ("成功查询数", "successful_queries"),
        ("成功率", "success_rate"),
        ("超时数", "timeout_queries"),
    ):
        vals = [all_stages["serial393/" + p][key] for p in ("contextbase", "weknora")]
        if key == "success_rate":
            cells = [f"{v:.2%}" for v in vals]
        elif key in ("successful_queries", "timeout_queries"):
            cells = [str(v) for v in vals]
        else:
            cells = [f"{v:.3f}" for v in vals]
        rows.append(f"| {title} | {cells[0]} | {cells[1]} |")
    rows += ["", "| 产品 | 平均返回段落数 | 最小—最大 |", "| --- | ---: | ---: |"]
    for product, q in quality.items():
        d = q["returned_depth"]
        rows.append(f"| {product} | {d['mean']:.3f} | {d['min']}—{d['max']} |")
    rows += [
        "",
        "Recall@100按实际不足100条的返回列表计分。每阶段相同4次预热，排除在正式指标之外；不把历史窗口耗时混入本表。",
        "",
        "## 固定100题、两轮并发负载",
        "",
        "| 并发 | 产品 | 总请求数 | 成功率 | 时间加权QPS | p50 ms | p95 ms | p99 ms | 两轮QPS |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for key, m in load_summary.items():
        c, product = key.split("/")
        per = "/".join(f"{v:.3f}" for v in m["per_round_successful_qps"])
        rows.append(
            f"| {c[1:]} | {product} | {m['queries']} | {m['success_rate']:.2%} | {m['combined_successful_qps']:.3f} | {m['request_p50_ms']:.2f} | {m['request_p95_ms']:.2f} | {m['request_p99_ms']:.2f} | {per} |"
        )
    rows += [
        "",
        "| 并发 | 产品 | 第1/2轮nDCG@10 | 第1/2轮Recall@10 | 第1/2轮Hit@1 |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for key, m in load_summary.items():
        c, product = key.split("/")
        ndcg = "/".join(f"{v:.6f}" for v in m["per_round_ndcg_at_10"])
        recall = "/".join(f"{v:.2%}" for v in m["per_round_recall_at_10"])
        head = "/".join(f"{v:.2%}" for v in m["per_round_hit_at_1"])
        rows.append(f"| {c[1:]} | {product} | {ndcg} | {recall} | {head} |")
    rows += [
        "",
        "并发下检索质量出现波动，不能把上述吞吐提升称为质量完全无损的扩展。主质量表仍使用两端完整393题串行结果；并发100题成绩不替代主成绩。",
    ]
    if batch_diagnostic:
        rows += [
            "",
            f"事后模型诊断确认：4个选定问题的单请求重复向量完全相同；4并发与单HTTP四条批量请求的向量完全相同，但均不同于单请求向量，单请求/批次向量余弦相似度为{batch_diagnostic['single_vs_batch_cosine_min']:.6f}—{batch_diagnostic['single_vs_batch_cosine_max']:.6f}。当前共享ONNX INT8服务存在批次相关输出变化。原始向量和独立核验保存在embedding-batch-diagnostic目录；未更改已测参数、未替换主成绩，也未对每个排名变化做完整因果归因。",
            "",
            "因此，这些并发结果是当前动态批处理配置下的端到端观测，不是对固定查询向量的纯检索器隔离比较。若需后者，应另行冻结关闭跨请求批处理或固定逐题向量的方案后复测，本轮未将该未验证方案混入结果。",
        ]
    rows += [
        "",
        "同一100题、相同顺序，两轮反转产品及并发档位顺序。闭环负载的QPS包含派发和结果落盘；请求耗时不含工作线程启动前的排队，包含失败请求。每格200次请求，只有100个不同问题；p99样本有限。各轮质量保存在load-comparison.json及逐题文件，不假设负载下质量必然不变。",
        "",
        "本轮WeKnora使用SQLite Lite后端。所用源码中的SQLite主连接池限制为1，与本轮吞吐饱和、排队延迟上升的现象一致；源码及构建证据见weknora-sqlite-pool-evidence.json。没有单独剖析数据库等待占比，不将全部耗时差归因于连接池，也不外推到WeKnora的其他数据库后端。",
        "",
        "## 资源与原生数据库体积",
        "",
        "| 同期393题组件 | ContextBase阶段峰值RSS MiB | WeKnora阶段峰值RSS MiB |",
        "| --- | ---: | ---: |",
    ]
    resources = {}
    for product in ("contextbase", "weknora"):
        m = all_stages["serial393/" + product]
        resources[product] = load(
            out / "runs/serial393" / product / m["attempt_directory"] / "resources.json"
        )
    for component in (
        "contextbase_api",
        "weknora_api",
        "shared_model",
        "postgres_instance",
    ):
        a, b = [
            resources[p]["components"][component]["peak_sampled_rss_bytes_sum"] / 2**20
            for p in ("contextbase", "weknora")
        ]
        rows.append(f"| {component} | {a:.2f} | {b:.2f} |")
    storage = load(out / "storage.json")
    save(
        out / "resource-comparison.json",
        {"serial393": resources, "native_storage": storage},
    )
    rows += [
        "",
        "API进程树、共享模型、PG实例分开列；闲置对端API及PG在两个阶段均保持运行。RSS可能重复计算共享页，PG是整个实例口径，采样间隔0.5秒。这不是精确独占内存或容器PSS比较。共享模型服务同时载入Embedding和未调用的重排权重，不把该RSS称为单个BGE-M3的独占内存。采样轨迹及每档资源均在各attempt/resources.json与resources.jsonl中。",
        "",
        "| 393题窗口CPU秒 | ContextBase阶段 | WeKnora阶段 |",
        "| --- | ---: | ---: |",
    ]
    for component in (
        "contextbase_api",
        "weknora_api",
        "shared_model",
        "postgres_instance",
    ):
        a, b = [
            resources[p]["components"][component]["cpu_seconds"]
            for p in ("contextbase", "weknora")
        ]
        rows.append(f"| {component} | {a:.2f} | {b:.2f} |")
    rows += [
        "",
        f"ContextBase验证数据库总计{storage['contextbase']['database_total_bytes'] / 2**30:.3f} GiB，其中工作区关系索引{storage['contextbase']['workspace_indexes_bytes'] / 2**30:.3f} GiB；WeKnora SQLite主文件{storage['weknora']['sqlite_main_file_bytes'] / 2**30:.3f} GiB，WAL为{storage['weknora']['sqlite_wal_bytes']}字节。均在合成文档测试前测量，存储引擎不同，不能称为相同格式索引的压缩率。",
        "",
        "## 入库和模型成本",
        "",
        "| 项目 | ContextBase | WeKnora Lite |",
        "| --- | ---: | ---: |",
    ]
    history = load(out / "historical-ingestion-cost.json")
    for title, fn in (
        ("历史入库累计批次秒", lambda p: history[p]["recorded_batch_wall_s"]),
        (
            "历史入库Embedding输入Token",
            lambda p: history[p]["embedding"]["input_tokens"],
        ),
        ("历史入库Embedding调用数", lambda p: history[p]["embedding"]["calls"]),
        (
            "本轮正式查询Embedding输入Token",
            lambda p: usage.get(p + "/query", {}).get("input_tokens", 0),
        ),
        (
            "本轮正式查询Embedding调用数",
            lambda p: usage.get(p + "/query", {}).get("calls", 0),
        ),
    ):
        vals = [fn(p) for p in ("contextbase", "weknora")]
        cells = [f"{v:.3f}" if isinstance(v, float) else f"{v:,}" for v in vals]
        rows.append(f"| {title} | {cells[0]} | {cells[1]} |")
    rows += [
        "",
        "本轮复用完整语料索引，没有重新进行10万段入库。历史时间仅来自可核验的历史批次日志，不含停机时间。模型重排和生成调用均为0；预热、探针、合成文档测试及事后模型诊断额外用量在model-usage-complete.json中单列。异步可见性测试开销合计，不伪造写入与探针查询的逐条关联。",
        "",
        "## 新增、替换和删除可见性",
        "",
        "ContextBase请求使用async=false同步入库，WeKnora手工知识publish使用原生异步入库。ACK口径不同，主要比较提交至首次外部检索可见的观测值。",
        "",
        "| 操作 | 产品 | 样本数 | 写入ACK中位ms | ACK后首次观察中位ms | 提交到首次观察中位ms | 最慢ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, m in visibility.items():
        product, op = key.split("/")
        rows.append(
            f"| {op} | {product} | {m['samples']} | {m['write_ack_p50_ms']:.2f} | {m['ack_to_first_observed_p50_ms']:.2f} | {m['submit_to_first_observed_p50_ms']:.2f} | {m['submit_to_first_observed_max_ms']:.2f} |"
        )
    rows += [
        "",
        "这些是包含原生检索探针耗时、0.25秒轮询等待的首次外部观察值，不是内部提交时间。每项10次，不作为生产SLA。仅操作独立副本中的临时合成文档；清理后两个副本和两个原库均重新通过100000个官方ID、原文及映射审计，正文指纹一致。",
        "",
        "## 核验与限制",
        "",
        f"共{verify['formal_attempts']}次正式查询，成功{verify['successes']}次，{verify['visibility_operations_verified']}次合成文档操作核验。模型文件/运行设置前后相同，独立ContextBase Bank恢复0.97/0.03、high1000，两个验证API已停止，原始数据与备份保留。",
        "",
        "本结果适用于本次10万段中文子集、固定配置与CPU环境；这些题目已被历史实验使用，不是盲测。追加调参投入差异仍存在，不能将同期重测解释为相同研发投入的产品比较。",
        "",
        "执行方案、状态、原始排名、请求耗时、模型日志与资源轨迹均在本目录；复跑入口见README.md。",
    ]
    (out / "REPORT.md").write_text("\n".join(rows) + "\n")
    print(json.dumps(verify, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--measurement-dir", type=Path, required=True)
    main(parser.parse_args())
