"""Render a standalone HTML report from a MIRACL benchmark result directory."""

import argparse
from html import escape
import json
import math
from pathlib import Path
from string import Template


PRODUCTS = ("contextbase", "weknora")
QUALITY = (
    ("nDCG@10", "ndcg_at_10", ".4f", ""),
    ("Recall@10", "recall_at_10", ".2%", ""),
    ("MRR@10", "mrr_at_10", ".4f", ""),
    ("Hit@1", "hit_at_1", ".2%", ""),
    ("Precision@5", "precision_at_5", ".2%", ""),
    ("Precision@10", "precision_at_10", ".2%", ""),
    ("Recall@100", "recall_at_100", ".2%", ""),
)
PERFORMANCE = (
    ("p50 延迟", "serial_p50_ms", ",.2f", " ms"),
    ("p95 延迟", "serial_p95_ms", ",.2f", " ms"),
    ("p99 延迟", "serial_p99_ms", ",.2f", " ms"),
    ("成功 QPS", "successful_qps", ",.3f", ""),
    ("成功率", "success_rate", ".2%", ""),
    ("成功查询数", "successful_queries", ",d", ""),
    ("平均返回深度", "returned_depth.mean", ".2f", ""),
    ("最大返回深度", "returned_depth.max", ",d", ""),
)
IMPORT = (
    ("入库耗时", "first_submit_to_last_completion_s", ",.2f", " 秒"),
    ("入库 LLM 输入", "llm.input_tokens", ",d", " Token"),
    ("入库 LLM 输出", "llm.output_tokens", ",d", " Token"),
    ("入库 LLM 调用数", "usage.chat.calls", ",d", ""),
    ("入库 Embedding 输入", "embedding.input_tokens", ",d", " Token"),
    ("入库 Embedding 调用数", "usage.embedding.calls", ",d", ""),
)
QUERY_USAGE = (
    ("查询 Embedding 输入", "embedding.input_tokens", ",d", " Token"),
    ("查询 Embedding 调用数", "usage.embedding.calls", ",d", ""),
    ("查询重排输入", "rerank.input_tokens", ",d", " Token"),
    ("查询重排调用数", "usage.rerank.calls", ",d", ""),
    ("平均重排候选数／调用", "mean_rerank_pairs", ".2f", ""),
)


def read_object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise ValueError(f"Invalid JSON in {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def number_at(data, path):
    value = data
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def complete_usage(row, stage):
    usage = (row.get("usage") or {}).get(stage) or {}
    return usage if usage.get("unmetered_calls") == 0 else {}


def import_values(row):
    return {
        **row,
        "llm": row.get("llm_usage") or complete_usage(row, "chat"),
        "embedding": complete_usage(row, "embedding"),
    }


def query_values(row):
    rerank = complete_usage(row, "rerank")
    calls = number_at(rerank, "calls")
    pairs = number_at(rerank, "pairs")
    return {
        **row,
        "embedding": complete_usage(row, "embedding"),
        "rerank": rerank,
        "mean_rerank_pairs": pairs / calls if calls and pairs is not None else None,
    }


def comparison_table(title, metrics, records, scale_rows, *, fractional=False):
    rows = []
    for label, key, spec, unit in metrics:
        # Use one scale across the products and concurrency settings for this metric.
        limit = 1 if fractional or spec.endswith("%") else max(
            (number_at(row, key) or 0 for row in scale_rows), default=0
        )
        cells = []
        for product in PRODUCTS:
            value = number_at(records.get(product, {}), key)
            if value is None:
                cells.append('<td class="missing">N/A</td>')
                continue
            displayed = format(int(value) if spec.endswith("d") else value, spec)
            width = 100 * min(value / limit, 1) if limit else 0
            cells.append(
                f'<td class="{product}"><span>{displayed}{unit}</span>'
                f'<div aria-hidden="true" class="track"><i style="width:{width:.4f}%"></i></div></td>'
            )
        rows.append(f'<tr><th scope="row">{label}</th>{"".join(cells)}</tr>')
    return (
        f'<section class="panel"><h3>{title}</h3><table aria-label="{title}">'
        '<thead><tr><th scope="col">指标</th><th scope="col">ContextBase</th>'
        '<th scope="col">WeKnora Lite</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></section>'
    )


def render_report(root, output=None):
    plan = read_object(root / "plan.json")
    configured = plan.get("concurrency", [])
    if not isinstance(configured, list) or any(type(n) is not int or n < 1 for n in configured):
        raise ValueError(f"Invalid concurrency list in {root / 'plan.json'}")
    imports = {}
    windows = {}
    for product in PRODUCTS:
        folder = root / product
        row = read_object(folder / "import.json")
        if row:
            imports[product] = import_values(row)
        for path in folder.glob("c*/metrics.json"):
            suffix = path.parent.name[1:]
            if not suffix.isascii() or not suffix.isdecimal():
                continue
            level = int(suffix)
            if level < 1 or (configured and level not in configured):
                continue
            row = read_object(path)
            if row:
                windows.setdefault(level, {})[product] = query_values(row)
    if not imports and not windows:
        raise ValueError(f"No MIRACL import.json or concurrency metrics.json found in {root}")

    concurrency = configured or sorted(windows)
    scale_rows = [row for group in windows.values() for row in group.values()]
    default = next(
        (n for n in concurrency if len(windows.get(n, {})) == len(PRODUCTS)), None
    )
    if default is None and windows:
        default = min(windows)

    options = []
    sections = []
    for n in concurrency:
        rows = windows.get(n, {})
        selected = " selected" if n == default else ""
        options.append(f'<option value="{n}"{selected}>并发 {n}</option>')
        sections.append(
            f'<section data-window="{n}"><div class="section-header"><h2>并发 {n}</h2>'
            f'<p>已记录 {len(rows)} / {len(PRODUCTS)} 个产品的查询结果</p></div>'
            '<div class="grid">'
            + comparison_table("检索质量", QUALITY, rows, scale_rows, fractional=True)
            + comparison_table("查询性能与返回深度", PERFORMANCE, rows, scale_rows)
            + comparison_table("查询模型用量", QUERY_USAGE, rows, scale_rows)
            + "</div></section>"
        )

    scope = []
    passages = number_at(plan, "passages")
    if passages is not None:
        scope.append(f"{int(passages):,} 段")
    query_ids = plan.get("query_ids")
    if isinstance(query_ids, list):
        scope.append(f"{len(query_ids):,} 道查询")
    if plan.get("public_result") == "smoke only":
        scope.append("预检 · 用于验证流程")
    elif plan.get("public_result") == "query sample":
        scope.append("抽样查询")
    scope.append(
        f"查询结果 {len(scale_rows)}/{len(concurrency) * len(PRODUCTS)} 组"
        if concurrency
        else "暂无查询结果"
    )
    dataset = plan.get("dataset")
    if isinstance(dataset, str):
        scope.append(dataset)
    template = Template(Path(__file__).with_name("report_template.html").read_text(encoding="utf-8"))
    html = template.substitute(
        scope="".join(f"<span>{escape(value)}</span>" for value in scope),
        selector=(
            '<label id="window-control" hidden>查看并发档 <select id="window">'
            + "".join(options)
            + '<option value="all">全部</option></select></label>'
            if options
            else ""
        ),
        windows="".join(sections),
        imports=comparison_table("入库耗时与模型用量", IMPORT, imports, imports.values()),
    )
    target = output if output is not None else root / "REPORT.html"
    target.write_text(html, encoding="utf-8", newline="\n")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="Directory containing benchmark results")
    parser.add_argument("--output", type=Path, help="HTML path, defaults to RESULTS/REPORT.html")
    args = parser.parse_args()
    try:
        report = render_report(args.results.expanduser(), args.output and args.output.expanduser())
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(report)


if __name__ == "__main__":
    main()
