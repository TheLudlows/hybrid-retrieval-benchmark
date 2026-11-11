"""Package the validated full hybrid benchmark, captures, and scoring inputs."""
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

from summarize_full_hybrid import KIT, bench, summarize

ROOT = Path(__file__).resolve().parent
LABELS = {"exact": "精确匹配", "paraphrase": "语义改写", "confusable": "易混淆",
          "long_doc": "长文定位", "multi_evidence": "跨证据"}


def make_report(results, runs):
    wk, cb = (results[key] for key in ("weknora", "contextbase"))
    lines = [
        "# WeKnora 与 ContextBase 中文文档召回复测",
        "",
        "2026 年 11 月 11 日在同一台 CPU 测试机上评估。两边分别索引同一份 30 篇合成中文运维文档，"
        "目标分块大小 512 字符，执行 120 道相同查询，其中 100 道有答案，20 道无答案。两边均使用 1024 维 "
        "BGE-M3 和 BGE Reranker v2 M3 的 ONNX INT8 版本，使用各自的原生检索和返回逻辑。",
        "",
        "| 设置 | WeKnora Lite | ContextBase |",
        "|---|---|---|",
        "| 原生入口 | `POST /api/v1/knowledge-search` | `POST /v1/default/banks/{bank_id}/memories/recall`，`include.chunks` |",
        "| 混合检索 | SQLite 向量、FTS5 中文二元切分、融合 | PostgreSQL 18.4 向量、pg_search 0.24.1 中文 jieba BM25、图、时间、RRF |",
        "| 重排 | BGE Cohere 兼容入口，目标深度 60 | BGE TEI 兼容入口，候选上限 60，Recall high 预算 |",
        "| 写入 | 文档库，关闭问题生成与画像 | Retain chunks，关闭自动观察整理 |",
        "",
        "ContextBase 运行于临时副本，Recall 内部从完整配置读取混合策略，业务 API Key 仍使用原有授权。"
        "四个探针均有语义、中文 BM25 和图候选。时间策略已启用，题集缺少专门的时间约束样本。",
        "",
        "| 指标 | WeKnora | ContextBase |",
        "|---|---:|---:|",
    ]
    for label, field in [
        ("证据召回 @1,024 Token", "evidence_recall_at_1024_tokens"),
        ("完整证据命中率 @1,024 Token", "complete_evidence_at_1024_tokens"),
        ("文档 Hit@1", "document_hit_at_1"),
        ("文档 Hit@5", "document_hit_at_5"),
        ("无答案题返回候选比例", "no_answer_candidate_return_rate"),
    ]:
        lines.append(f"| {label} | {wk['overall'][field]:.1%} | {cb['overall'][field]:.1%} |")
    for budget in ("256", "512", "2048", "4096", "native"):
        label = "原生响应全部正文" if budget == "native" else f"{int(budget):,} Token"
        cells = [f"{result['evidence'][budget]['macro_recall']:.1%}" for result in (wk, cb)]
        lines.append(f"| 证据召回（{label}） | {' | '.join(cells)} |")
    lines += ["", "证据召回按题平均。完整证据命中要求同题的所有标注原文进入共同预算。"
              "正文预算使用 o200k_base 词表。证据由原文匹配自动计分。", "",
              "| 题型（各 20 题） | WeKnora 证据召回 @1,024 | ContextBase 证据召回 @1,024 |",
              "|---|---:|---:|"]
    for category, label in LABELS.items():
        cells = [f"{result['by_category'][category]['evidence_recall_at_1024_tokens']:.1%}" for result in (wk, cb)]
        lines.append(f"| {label} | {' | '.join(cells)} |")
    lines += ["", "| 模型输入用量 | WeKnora | ContextBase |", "|---|---:|---:|"]
    for label, phase, kind in [
        ("入库 Embedding 输入 Token", "ingest", "embedding"),
        ("120 题查询 Embedding 输入 Token", "retrieve", "embedding"),
        ("120 题模型重排输入 Token", "retrieve", "rerank"),
        ("120 题重排 query × passage 对数", "retrieve", "rerank"),
    ]:
        field = "pairs" if label.endswith("对数") else "input_tokens"
        cells = [f"{result['usage'][phase][kind][field]:,}" for result in (wk, cb)]
        lines.append(f"| {label} | {' | '.join(cells)} |")
    lines += [
        "",
        "输入 Token 来自模型端实际 tokenizer 计数，重排包含每个 query 与候选的组合输入。"
        "正文预算使用独立词表。测试只到原文返回。请求包含完整重排，未测试带答案生成的链路。"
        "逐题延迟保留在结果中，CPU 本地重排时间和中断前后的资源波动影响可比性。",
        "",
        "WeKnora 在相同知识库上续跑。只有同时保留向量和重排用量的已完成题进入汇总。",
        "",
        f"- WeKnora Lite 0.9.0 提交 `cbdcebabf48c5ee2d6868b370326aa8341703f62`，结果 `{runs['weknora'].name}`。",
        f"- ContextBase 提交 `c1cc16269361f2ed0ff4d1cfcb20345137b3b820` 加临时配置读取修正，结果 `{runs['contextbase'].name}`。",
        "- Embedding `onnx-community/bge-m3-ONNX@25b9af8e87a38eb120cfe87125383677b9cd309e`。",
        "- 重排 `onnx-community/bge-reranker-v2-m3-ONNX@6f5ff65298512715a1e669753bc754d2bc8f367b`。",
        "",
        "解压后使用 Python 3 与 tiktoken 运行以下命令复算指标。",
        "",
        "```bash",
        "python deployment/summarize_full_hybrid.py results/weknora results/contextbase",
        "```",
    ]
    return "\n".join(lines) + "\n"


def main():
    output, wk_run, cb_run = map(Path, sys.argv[1:])
    runs = {"weknora": wk_run, "contextbase": cb_run}
    questions = {question["id"]: question for question in bench.read_rows(KIT / "questions.jsonl")}
    results = {product: summarize(product, run, questions) for product, run in runs.items()}
    inputs = sorted((KIT / "corpus").glob("*.md")) + [KIT / "questions.jsonl"]
    hashes = {str(path.relative_to(KIT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("README.md", make_report(results, runs))
        archive.writestr("results/comparison.json", json.dumps(results, ensure_ascii=False, indent=2))
        archive.writestr("fixtures-sha256.json", json.dumps(hashes, ensure_ascii=False, indent=2))
        for path in inputs + [KIT / "bench.py", KIT / "build_corpus.py"]:
            archive.write(path, "doc_recall_v1/" + str(path.relative_to(KIT)))
        for product, run in runs.items():
            for path in sorted(run.glob(f"{product}-*")):
                if path.name.endswith("-capture-resumed.jsonl"):
                    continue
                archive.write(path, f"results/{product}/{path.name}")
        for name in ("run_full_hybrid.py", "summarize_full_hybrid.py", "package_full_hybrid.py",
                     "run_eval.py", "run_native.py", "model_server.py", "model_server_hybrid.py", "fetch_hybrid_models.py"):
            archive.write(ROOT / name, f"deployment/{name}")
        engine = Path("hindsight-api-slim/hindsight_api/engine/memory_engine.py")
        import difflib
        original = (ROOT / "contextbase" / engine).read_text().splitlines(keepends=True)
        patched = (ROOT / "contextbase-hybrid-audit" / engine).read_text().splitlines(keepends=True)
        archive.writestr("contextbase-recall-config.patch", "".join(difflib.unified_diff(
            original, patched, "a/" + str(engine), "b/" + str(engine))))
    with ZipFile(output) as archive:
        assert archive.testzip() is None
    print(output.resolve())


if __name__ == "__main__":
    main()
