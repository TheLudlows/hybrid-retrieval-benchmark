"""Benchmark deployed ContextBase and WeKnora Lite over their native HTTP APIs."""

import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

import bench
import ingest
from run_llm_ingest_rerank import (
    CAP,
    CONCURRENCY,
    PRODUCTS,
    ImportAPI,
    Run,
    append_row,
    read_json,
)
from run_scale import save

DATA = Path(__file__).resolve().parent / "data/miracl-zh-100k"
MIRACL_SOURCE_FIELDS = (
    "retain_extraction_mode",
    "enable_reranking",
    "enable_text_search",
    "enable_graph_retrieval",
    "enable_temporal_retrieval",
    "recall_rrf_semantic_weight",
    "recall_rrf_bm25_weight",
    "recall_budget_fixed_high",
    "reranker_max_candidates",
    "reranker_max_candidates_high",
)


def records(api, product):
    offset, size = 0, 100
    while True:
        if product == "contextbase":
            response = api.request(f"/documents?limit={size}&offset={offset}")
            page = response["items"]
        else:
            response = api.request(
                f"/knowledge-bases/{quote(api.scope, safe='')}/knowledge"
                f"?page={offset // size + 1}&page_size={size}"
            )
            page = response["data"] or []
        yield from page
        offset += len(page)
        if offset >= response["total"]:
            return
        if len(page) != size:
            raise ValueError(f"{product} returned an inconsistent document listing")


class ExistingRun(Run):
    def service_plan(self, args):
        c = bench.prior_api("contextbase")
        w = bench.prior_api("weknora")
        config = c.request("/config")["config"]
        if config["retain_extraction_mode"] not in {
            "concise",
            "verbose",
            "custom",
            "verbatim",
        }:
            raise ValueError(
                "Enable LLM fact extraction on the dedicated ContextBase Bank"
            )
        if (
            config.get("enable_reranking") is not True
            or config.get("enable_text_search") is not True
        ):
            raise ValueError("Enable ContextBase model reranking and full text search")
        if any(
            config.get(key) is not False
            for key in ("enable_graph_retrieval", "enable_temporal_retrieval")
        ):
            raise ValueError(
                "Disable graph and temporal branches on the ContextBase benchmark Bank"
            )
        kb = w.request(f"/knowledge-bases/{quote(w.scope, safe='')}")["data"]
        questions = kb.get("question_generation_config") or {}
        if questions.get("enabled") is not True:
            raise ValueError(
                "Enable question generation on the dedicated WeKnora knowledge base"
            )
        if (kb.get("profile_config") or {}).get("enabled"):
            raise ValueError(
                "Disable knowledge-base profile generation for this benchmark"
            )
        return {
            "mode": "existing native APIs",
            "scopes": {"contextbase": [c.base, c.scope], "weknora": [w.base, w.scope]},
            "contextbase_readback": {
                key: config[key] for key in MIRACL_SOURCE_FIELDS if key in config
            },
            "weknora_readback": {
                "embedding_model_id": kb.get("embedding_model_id"),
                "question_generation_enabled": True,
                "question_count": questions.get("question_count"),
                "question_model_id": questions.get("model_id")
                or kb.get("summary_model_id"),
                "reranker_override": args.weknora_reranker_id or None,
            },
            "model_weights_verified": False,
        }

    def phase(self, product, phase, stage):
        return f"{self.run_id}/{product}/{stage}"

    def query_usage(self, run_id, attempt):
        return None

    def fetch_query(self, api, product, query):
        return bench.fetch_ranked(
            api, product, query, CAP, self.args.weknora_reranker_id
        )

    def verify_query_rerank(self, product, usage, nonempty):
        # WeKnora supplies per-response rerank metadata; ContextBase is probed separately.
        return nonempty > 0

    def import_corpus(self, product, api, runtime, folder):
        api.timeout = self.args.index_timeout
        checkpoint = self.args.work / f"{product}.sqlite"
        summary = folder / "import.json"
        journal = folder / "import-batches.jsonl"
        first = folder / "first-submission.json"
        with closing(sqlite3.connect(checkpoint)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            ingest.setup(db, bench.index_binding(api, product, self.source))
            if product == "weknora":
                pending = [
                    row[0]
                    for row in db.execute(
                        "SELECT passage_id FROM passage_map WHERE remote_id IS NULL"
                    )
                ]
                requested = self.args.recover_weknora
                if requested:
                    ingest.recover(db, api, *requested)
                    pending.remove(requested[0])
                if pending:
                    raise ValueError(
                        f"Interrupted WeKnora create for {pending[0]}. Find its existing knowledge ID "
                        "and rerun with --recover-weknora PASSAGE KNOWLEDGE_ID."
                    )
            indexed = db.execute(
                "SELECT count(*) FROM passage_map WHERE indexed=1"
            ).fetchone()[0]
            if not summary.exists():
                for batch in self.batches(self.corpus_rows(), self.args.batch_size):
                    todo = []
                    for row in batch:
                        entry = db.execute(
                            "SELECT remote_id,indexed FROM passage_map WHERE passage_id=?",
                            (row["docid"],),
                        ).fetchone()
                        if not entry or not entry[1]:
                            todo.append((row, entry[0] if entry else None))
                    if not todo:
                        continue
                    start = time.time()
                    if not first.exists():
                        save(first, {"started_unix": start})
                    tokens = None
                    if product == "contextbase":
                        response = ingest.contextbase_batch(
                            db, api, [row for row, _ in todo]
                        )
                        usage = response.get("usage") or {}
                        fields = ("input_tokens", "output_tokens")
                        if all(
                            type(usage.get(key)) is int and usage[key] >= 0
                            for key in fields
                        ):
                            tokens = {key: usage[key] for key in fields}
                    else:
                        ids = [
                            remote or ingest.weknora_create(db, ImportAPI(api), row)
                            for row, remote in todo
                        ]
                        ingest.wait_for_index(
                            db, api, ids, self.args.index_timeout, 0.5
                        )
                    indexed += len(todo)
                    append_row(
                        journal,
                        {
                            "started_unix": start,
                            "finished_unix": time.time(),
                            "documents": len(todo),
                            "indexed": indexed,
                            "llm_usage": tokens,
                        },
                    )
                    if (
                        indexed == len(todo)
                        or indexed == self.expected
                        or indexed // 128 != (indexed - len(todo)) // 128
                    ):
                        self.status(
                            state="importing",
                            product=product,
                            indexed=indexed,
                            target=self.expected,
                        )
            mapping = dict(
                db.execute(
                    "SELECT remote_id,passage_id FROM passage_map WHERE indexed=1"
                )
            )
            if len(mapping) != self.expected:
                raise ValueError(f"Incomplete {product} import checkpoint")
            with db:
                db.execute("UPDATE miracl_ingest_run SET complete=1")

        native = list(records(api, product))
        if len(native) != self.expected or {row["id"] for row in native} != set(
            mapping
        ):
            raise ValueError(
                f"{product} scope contains unexpected or missing documents"
            )
        coverage = {"native_documents": len(native), "source_passages": self.expected}
        if product == "contextbase":
            coverage["documents_with_fact_units"] = sum(
                row.get("memory_unit_count", 0) > 0 for row in native
            )
            if coverage["documents_with_fact_units"] != self.expected:
                raise ValueError(
                    "ContextBase reported documents without extracted fact units"
                )
        else:
            if any(row.get("parse_status") != "completed" for row in native):
                raise ValueError(
                    "WeKnora still has documents with unfinished processing"
                )
            ids = sorted(mapping, key=mapping.get)[:8]
            with_questions = 0
            for remote in ids:
                chunks = (
                    api.request(
                        f"/chunks/{quote(remote, safe='')}?page=1&page_size=100"
                    )["data"]
                    or []
                )
                for chunk in chunks:
                    metadata = chunk.get("metadata") or {}
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata)
                    if metadata.get("generated_questions"):
                        with_questions += 1
                        break
            coverage.update(
                question_documents_sampled=len(ids),
                sampled_documents_with_questions=with_questions,
            )
            if with_questions != len(ids):
                raise ValueError(
                    "WeKnora question generation is missing from sampled chunks"
                )
        save(folder / "index-coverage.json", coverage)
        if not summary.exists():
            batches = [json.loads(line) for line in journal.read_text().splitlines()]
            if sum(row["documents"] for row in batches) != self.expected:
                raise ValueError("Import timing journal is incomplete")
            observed = [
                row["llm_usage"] for row in batches if row["llm_usage"] is not None
            ]
            llm_usage = (
                {
                    key: sum(row[key] for row in observed)
                    for key in ("input_tokens", "output_tokens")
                }
                if len(observed) == len(batches)
                else None
            )
            save(
                summary,
                {
                    "indexed_documents": indexed,
                    "first_submit_to_last_completion_s": batches[-1]["finished_unix"]
                    - read_json(first)["started_unix"],
                    "active_batch_s": sum(
                        row["finished_unix"] - row["started_unix"] for row in batches
                    ),
                    "llm_usage": llm_usage,
                    "llm_metered_batches": len(observed),
                    "batches": len(batches),
                    "embedding_usage": None,
                    "index": coverage,
                },
            )
        return mapping

    def probe_contextbase(self):
        api = bench.prior_api("contextbase")
        result = api.request(
            "/memories/recall",
            {
                "query": next(iter(self.topics.values())),
                "budget": "high",
                "types": ["world", "experience"],
                "max_tokens": 8192,
                "trace": True,
                "include": {"entities": None, "chunks": {"max_tokens": 16384}},
            },
        )
        phases = result["trace"]["summary"]["phase_metrics"]
        details = next(
            (
                phase["details"]
                for phase in phases
                if phase["phase_name"] == "reranking"
            ),
            {},
        )
        if (
            details.get("reranker_type") != "cross-encoder"
            or not 0 < details.get("candidates_reranked", 0) <= CAP
        ):
            raise ValueError(
                "ContextBase trace did not confirm model reranking with at most 60 candidates"
            )
        save(self.args.results / "contextbase-rerank-probe.json", details)

    def run(self):
        for product in PRODUCTS:
            api = bench.prior_api(product)
            if not (self.args.work / f"{product}.sqlite").exists() and next(
                records(api, product), None
            ):
                raise ValueError(f"{product} scope must be empty on the first run")
        mappings = {}
        for product in PRODUCTS:
            folder = self.args.results / product
            folder.mkdir(exist_ok=True)
            self.status(state="importing", product=product, target=self.expected)
            mappings[product] = self.import_corpus(
                product, bench.prior_api(product), {}, folder
            )
        self.probe_contextbase()
        for concurrency in CONCURRENCY:
            for product in PRODUCTS if concurrency != 4 else reversed(PRODUCTS):
                self.retrieve(
                    product,
                    mappings[product],
                    concurrency,
                    {},
                    self.args.results / product / f"c{concurrency}",
                )
        self.report()
        self.status(state="complete", smoke=self.args.smoke)

    def report(self):
        root = self.args.results
        lines = [
            "# MIRACL 中文 API 直连对比",
            "",
            f"{self.expected:,} 段和 {len(self.topics)} 道查询。"
            + ("本次为预检。" if self.args.smoke else ""),
            "",
            "| 产品 | 导入墙钟秒 | 导入 LLM 输入 Token | 导入 LLM 输出 Token |",
            "| --- | ---: | ---: | ---: |",
        ]
        for product in PRODUCTS:
            row = read_json(root / product / "import.json")
            tokens = row["llm_usage"] or {}
            lines.append(
                f"| {product} | {row['first_submit_to_last_completion_s']:.2f} | "
                f"{tokens.get('input_tokens', 'N/A')} | {tokens.get('output_tokens', 'N/A')} |"
            )
        lines += [
            "",
            "| 产品 | 并发 | 请求成功率 | 成功 QPS | p95 ms | nDCG@10 | Recall@10 | MRR@10 | Hit@1 | P@5 | P@10 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for concurrency in CONCURRENCY:
            for product in PRODUCTS:
                m = read_json(root / product / f"c{concurrency}" / "metrics.json")
                lines.append(
                    f"| {product} | {concurrency} | {m['success_rate']:.2%} | "
                    f"{m['successful_qps']:.3f} | {m['serial_p95_ms']:.2f} | "
                    f"{m['ndcg_at_10']:.6f} | {m['recall_at_10']:.2%} | {m['mrr_at_10']:.6f} | "
                    f"{m['hit_at_1']:.2%} | {m['precision_at_5']:.2%} | {m['precision_at_10']:.2%} |"
                )
        lines += [
            "",
            "时延从测试客户端测得，包含网络。ContextBase 重排由 trace 抽查，WeKnora 重排由搜索响应验证。"
            "远端模型权重、查询模型 Token 和服务端资源需要各自的模型或主机监控。N/A 表示 API 未提供完整用量。",
            "",
        ]
        (root / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def configure_connection(parser, args):
    environment_fields = {
        "contextbase": {
            "base_url": "CONTEXTBASE_URL",
            "api_key": "CONTEXTBASE_API_KEY",
            "bank_id": "CONTEXTBASE_BANK_ID",
        },
        "weknora": {
            "base_url": "WEKNORA_URL",
            "api_key": "WEKNORA_API_KEY",
            "kb_id": "WEKNORA_KB_ID",
        },
    }
    file_reranker = ""
    if args.config is not None:
        if args.contextbase_url is not None or args.weknora_url is not None:
            parser.error("Use --config or the two positional service URLs")
        try:
            config = json.loads(args.config.expanduser().read_text(encoding="utf-8"))
        except OSError as error:
            parser.error(f"Cannot read the config file ({error.strerror})")
        except UnicodeError:
            parser.error("The config file must use UTF-8")
        except json.JSONDecodeError as error:
            parser.error(
                f"Invalid JSON in the config file at line {error.lineno}, column {error.colno}"
            )
        if not isinstance(config, dict) or set(config) != set(environment_fields):
            parser.error("The config file must contain contextbase and weknora objects")
        connection = {}
        for product, fields in environment_fields.items():
            values = config[product]
            allowed = set(fields) | ({"reranker_id"} if product == "weknora" else set())
            if not isinstance(values, dict) or values.keys() - allowed:
                parser.error(f"Invalid fields in the {product} config object")
            for field in allowed:
                if field in values and not isinstance(values[field], str):
                    parser.error(f"{product}.{field} must be a string")
            for field, env in fields.items():
                value = values.get(field, "").strip()
                if field == "api_key" and not value:
                    value = os.environ.get(env, "").strip()
                if not value:
                    parser.error(f"Set {product}.{field} in the config file")
                connection[env] = value
        file_reranker = config["weknora"].get("reranker_id", "").strip()
    else:
        if args.contextbase_url is None or args.weknora_url is None:
            parser.error("Supply --config or both service URLs")
        required = (
            "CONTEXTBASE_API_KEY",
            "CONTEXTBASE_BANK_ID",
            "WEKNORA_API_KEY",
            "WEKNORA_KB_ID",
        )
        if missing := [
            name for name in required if not os.environ.get(name, "").strip()
        ]:
            parser.error(
                "Set benchmark credentials and scopes in the environment: "
                + ", ".join(missing)
            )
        connection = {
            "CONTEXTBASE_URL": args.contextbase_url,
            "WEKNORA_URL": args.weknora_url,
        }
    os.environ.update(connection, BENCH_HTTP_TIMEOUT_SECONDS="600")
    if args.weknora_reranker_id is None:
        args.weknora_reranker_id = file_reranker or os.environ.get(
            "WEKNORA_RERANKER_ID", ""
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "contextbase_url", nargs="?", help="ContextBase native API base URL"
    )
    parser.add_argument("weknora_url", nargs="?", help="WeKnora native API base URL")
    parser.add_argument(
        "--config",
        type=Path,
        help="Local JSON file with service URLs, API keys and scope IDs",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run 100,000 documents and 393 queries; default is smoke",
    )
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument(
        "--output", type=Path, help="Use a dedicated path; reuse it to resume"
    )
    parser.add_argument(
        "--weknora-reranker-id", help="Override the configured WeKnora reranker ID"
    )
    parser.add_argument(
        "--recover-weknora", nargs=2, metavar=("PASSAGE", "KNOWLEDGE_ID")
    )
    args = parser.parse_args()
    configure_connection(parser, args)
    args.smoke = not args.full
    args.run_name = "api_smoke" if args.smoke else "api_full"
    args.batch_size = 4 if args.smoke else 16
    args.index_timeout = 3600
    args.query_timeout = 600
    args.results = (
        args.output or Path.home() / "miracl-api-benchmark" / args.run_name
    ).resolve()
    args.results.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.work = args.results / "checkpoints"
    args.work.mkdir(mode=0o700, exist_ok=True)
    with (args.work / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = ExistingRun(args)
        try:
            run.run()
        except BaseException as error:
            previous = read_json(args.results / "status.json")
            run.status(
                state="interrupted"
                if isinstance(error, KeyboardInterrupt)
                else "failed",
                error_type=type(error).__name__,
                error=str(error),
                previous=previous,
            )
            raise


if __name__ == "__main__":
    main()
