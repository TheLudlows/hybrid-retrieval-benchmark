"""Recompute standard precision/MRR and qrels coverage without altering frozen scorers."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics

import bench
from run_scale import save


def evaluate(run, qrels, source):
    legacy = bench.score(run, qrels, None, source)
    per_query = {}
    for line in run.read_text().splitlines():
        row = json.loads(line)
        qid, passages = row["qid"], row["passages"]
        labels = qrels[qid]
        result = dict(legacy["per_topic"][qid])
        for k in (5, 10):
            top = passages[:k]
            positive = sum(labels.get(p, 0) > 0 for p in top)
            negative = sum(p in labels and labels[p] == 0 for p in top)
            unjudged = sum(p not in labels for p in top)
            result.update(
                {
                    f"precision_at_{k}": positive / k,
                    f"unjudged_at_{k}": unjudged / len(top) if top else None,
                    f"missing_slots_at_{k}": (k - len(top)) / k,
                    f"top{k}_counts": {
                        "positive": positive,
                        "judged_zero": negative,
                        "unjudged": unjudged,
                        "returned": len(top),
                    },
                }
            )
        first = next(
            (i for i, p in enumerate(passages[:10], 1) if labels.get(p, 0) > 0), None
        )
        result["mrr_at_10"] = 1 / first if first else 0.0
        per_query[qid] = result
    extra = {}
    for key in (
        "precision_at_5",
        "precision_at_10",
        "mrr_at_10",
        "unjudged_at_5",
        "unjudged_at_10",
        "missing_slots_at_5",
        "missing_slots_at_10",
    ):
        values = [row[key] for row in per_query.values() if row[key] is not None]
        extra[key] = statistics.mean(values) if values else None
    return {
        **legacy,
        **extra,
        "per_topic": per_query,
        "rankings_sha256": hashlib.sha256(run.read_bytes()).hexdigest(),
    }


def main(args):
    repo = Path(__file__).resolve().parents[1]
    source = bench.dataset_source(args.data)
    _, qrels = bench.topics_and_qrels(args.data)
    paths = {
        "contextbase": "miracl-zh-contextbase-native-optimize-20261114/dev393/w985_pool300",
        "weknora": "miracl-zh-native-no-rerank-20261113/100k/weknora/rrf_k30-dev393-cap60",
    }
    metrics = {}
    for product, relative in paths.items():
        folder = repo / "results" / relative
        value = evaluate(folder / "rankings.jsonl", qrels, source)
        original = json.loads((folder / "metrics.json").read_text())
        for key, expected in original.items():
            if key != "per_topic":
                assert value[key] == expected
        save(args.results / "quality" / product / "metrics.json", value)
        metrics[product] = value
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
    summary = {
        "dataset": source["dataset"],
        "queries_per_product": len(qrels),
        "source_rankings": paths,
        "metrics": {p: {key: m[key] for key in keys} for p, m in metrics.items()},
        "contextbase_minus_weknora": {
            key: metrics["contextbase"][key] - metrics["weknora"][key] for key in keys
        },
        "protocol": {
            "precision": "Positive qrels count among first K, divided by K; empty slots and unjudged score zero.",
            "mrr_at_10": "Reciprocal rank of first positive qrel within ten, otherwise zero; macro average.",
            "unjudged_at_k": "Unjudged among actually returned first K, divided by actual returned count; null if empty.",
            "limitation": "Qrels are incomplete. Judged precision is not a human judgment that unjudged items are irrelevant.",
            "comparison": "ContextBase received additional tuning and feature-source changes; historical windows differ.",
        },
    }
    save(args.results / "quality-comparison.json", summary)
    rows = [
        "# 知识库评测指标补全",
        "",
        "同一MIRACL中文10万段、393题，以官方段落ID从已归档排名复算；未改写旧评分脚本或旧成绩。ContextBase使用0.985/0.015、high=300候选，WeKnora使用k30、0.7/0.3历史选定配置。两端均关闭模型重排。",
        "",
        "## 检索质量",
        "",
        "| 指标 | ContextBase | WeKnora Lite | 差值 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key in keys:
        a, b = metrics["contextbase"][key], metrics["weknora"][key]
        if key in ("ndcg_at_10", "mrr_at_10"):
            rows.append(f"| {key} | {a:.6f} | {b:.6f} | {a - b:+.6f} |")
        else:
            rows.append(f"| {key} | {a:.2%} | {b:.2%} | {100 * (a - b):+.3f}个百分点 |")
    rows += [
        "",
        "Precision采用固定K作分母，未标注按0计分；未标注率采用实际返回数量作分母。未标注并不等于人工判定不相关，应与Precision一起解读。当前每题至少返回10条，前5/10没有缺位。",
        "",
        "Recall@100按实际返回不足100条的列表计分。原有时延来自不同窗口，不用它代替同期并发吞吐实测。ContextBase存在追加调优，质量表不是新的等调参预算结论。",
        "",
    ]
    (args.results / "QUALITY.md").write_text("\n".join(rows))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    main(parser.parse_args())
