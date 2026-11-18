"""Reproduce the MIRACL LLM-import and native reranking comparison on Linux."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import platform
import re
import signal
import sqlite3
import subprocess
import time
import uuid
from urllib.error import URLError

import bench
import ingest
from extend_retrieval_metrics import evaluate
from llm_ingest_services import (
    CONTEXTBASE_CONFIG,
    MODEL_HTTP_KEEPALIVE_S,
    QUESTION_CONFIG,
    RERANKER,
    WEKNORA_CONFIG,
    services,
)
from render_report import render_report
from run_support import Sampler, run_lock, save

CONCURRENCY = (1, 4, 8)
PRODUCTS = ("contextbase", "weknora")
CAP = 60
SMOKE_TOPICS = 8


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def append_row(path, row):
    import os

    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class ImportAPI:
    """Use the original idempotency journal with WeKnora's import overrides."""

    def __init__(self, api):
        self.api = api
        self.scope = api.scope

    def request(self, path, data=None):
        if path.endswith("/knowledge/manual"):
            data = {**data, "process_config": {"summary_enabled": False}}
        return self.api.request(path, data)


class Run:
    def __init__(self, args):
        self.args = args
        self.source = bench.dataset_source(args.data)
        self.topics, self.qrels = bench.topics_and_qrels(args.data)
        files = self.source["files"]
        self.corpus = {
            name: row["sha256"]
            for name, row in files.items()
            if name.startswith("corpus/")
        }
        self.rows = None
        if args.smoke:
            self.topics = dict(list(self.topics.items())[:SMOKE_TOPICS])
            ids = {
                next(p for p, v in self.qrels[qid].items() if v == 1)
                for qid in self.topics
            }
            self.rows = [
                row
                for _, row in bench.corpus_rows(args.data, self.source)
                if row["docid"] in ids
            ]
            if {row["docid"] for row in self.rows} != ids:
                raise ValueError("Smoke positives are missing from the corpus")
            self.source = {
                **self.source,
                "dataset": self.source["dataset"] + "/selected-smoke",
            }
        self.qrels = {qid: self.qrels[qid] for qid in self.topics}
        self.expected = (
            len(self.rows) if self.rows is not None else self.source["passages"]
        )
        self.plan = {
            "run_name": args.run_name,
            "dataset": self.source["dataset"],
            "corpus_sha256": self.corpus,
            "passages": self.expected,
            "query_ids": list(self.topics),
            "smoke_ids": [row["docid"] for row in self.rows]
            if self.rows is not None
            else None,
            **self.service_plan(args),
            "query_timeout_s": args.query_timeout,
            "concurrency": list(CONCURRENCY),
            "warmups_per_window": 1 if args.smoke else 4,
            "candidate_limit": CAP,
            "ingest_batch_size": args.batch_size,
            "public_result": "smoke only" if args.smoke else "formal",
        }
        plan_path = args.results / "plan.json"
        if plan_path.exists() and read_json(plan_path) != self.plan:
            raise ValueError(
                "The existing results belong to a different experiment plan"
            )
        owner = args.work / "run-owner.json"
        if owner.exists():
            identity = read_json(owner)
            if identity["plan"] != self.plan or identity["results"] != str(
                args.results
            ):
                raise ValueError(
                    "Use the original work and result directories for this experiment"
                )
        else:
            if plan_path.exists():
                raise ValueError("The work directory for these results is missing")
            identity = {
                "plan": self.plan,
                "results": str(args.results),
                "run_id": uuid.uuid4().hex,
            }
            save(owner, identity)
        self.run_id = identity["run_id"]
        save(plan_path, self.plan)
        self.status(state="starting")

    def service_plan(self, args):
        model_files = Path(__file__).resolve().parents[1] / "deployment/models"
        return {
            "contextbase_commit": self.revision(args.contextbase_source),
            "weknora_commit": self.revision(args.weknora_source),
            "weknora_binary_sha256": sha256(args.weknora_binary),
            "contextbase": CONTEXTBASE_CONFIG,
            "weknora_retrieval": WEKNORA_CONFIG,
            "weknora_question_generation": QUESTION_CONFIG,
            "weknora_summary": False,
            "llm_model": args.llm_model,
            "llm_artifact_sha256": args.llm_sha256,
            "bge_artifact_sha256": {
                f"{folder}/{name}": sha256(model_files / folder / name)
                for folder in ("full-embedding", "full-reranker")
                for name in ("model_quantized.onnx", "tokenizer.json")
            },
            "onnx_threads": args.onnx_threads,
            "embedding_http_batch_ms": 0,
        }

    @staticmethod
    def revision(source):
        return subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()

    def status(self, **fields):
        save(
            self.args.results / "status.json",
            {"run_id": self.run_id, "updated_unix": time.time(), **fields},
        )
        print(json.dumps(fields), flush=True)

    def corpus_rows(self):
        if self.rows is not None:
            yield from self.rows
        else:
            yield from (
                row for _, row in bench.corpus_rows(self.args.data, self.source)
            )

    def phase(self, product, phase, stage):
        run_id = f"{self.run_id}/{product}/{stage}"
        save(
            self.args.work / "measurement-context.json",
            {product: {"phase": phase, "id": stage, "run_id": run_id}},
        )
        return run_id

    def usage(self, run_id, target):
        totals = {}
        path = self.args.work / "model-usage.jsonl"
        with gzip.open(target, "wt", encoding="utf-8") as out:
            if path.exists():
                for line in path.open(encoding="utf-8"):
                    row = json.loads(line)
                    if row.get("run_id") != run_id:
                        continue
                    out.write(line)
                    entry = totals.setdefault(
                        row["kind"],
                        {
                            "calls": 0,
                            "observed_calls": 0,
                            "unmetered_calls": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "pairs": 0,
                            "max_pairs_per_call": 0,
                        },
                    )
                    entry["calls"] += row["calls"]
                    if row["status"] == "observed":
                        entry["observed_calls"] += row["calls"]
                        for key in ("input_tokens", "output_tokens", "pairs"):
                            entry[key] += row.get(key, 0)
                        entry["max_pairs_per_call"] = max(
                            entry["max_pairs_per_call"], row.get("pairs", 0)
                        )
                    else:
                        entry["unmetered_calls"] += row["calls"]
        return totals

    def index_state(self, product, api, runtime):
        if product == "contextbase":
            import psycopg2
            from psycopg2 import sql

            schema = sql.Identifier("ctxdb_ws_" + api.scope)
            with (
                closing(psycopg2.connect(runtime["postgres_dsn"])) as conn,
                conn.cursor() as cursor,
            ):

                def rows(table, columns, condition=""):
                    cursor.execute(
                        sql.SQL(
                            f"SELECT {columns} FROM {{}}.{table} WHERE bank_id=%s{condition}"
                        ).format(schema),
                        (api.scope,),
                    )
                    return cursor.fetchall()

                docs = dict(rows("documents", "id, original_text"))
                vector_rows = rows(
                    "memory_units", "document_id", " AND embedding IS NOT NULL"
                )
                vectors = {row[0] for row in vector_rows}
                cursor.execute("SELECT pg_database_size(current_database())")
                storage = {"database_bytes": cursor.fetchone()[0]}
                cursor.execute(
                    "SELECT COALESCE(sum(pg_indexes_size(c.oid)),0) "
                    "FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
                    "WHERE n.nspname=%s AND c.relkind='r'",
                    ("ctxdb_ws_" + api.scope,),
                )
                storage["bank_index_bytes"] = int(cursor.fetchone()[0])
            return docs, vectors, {"vector_rows": len(vector_rows), "storage": storage}

        path = runtime["weknora_db"]
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            parameters = (api.scope,)
            docs = {
                row[0]: json.loads(row[1])["content"]
                for row in conn.execute(
                    "SELECT id,metadata FROM knowledges WHERE knowledge_base_id=? "
                    "AND deleted_at IS NULL AND parse_status='completed' AND pending_subtasks_count=0",
                    parameters,
                )
            }
            has_vectors = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='vec_embeddings_1024_rowids'"
            ).fetchone()
            vectors = (
                conn.execute(
                    "SELECT e.knowledge_id, e.source_id, e.chunk_id FROM lite_embeddings e "
                    "JOIN vec_embeddings_1024_rowids v ON v.rowid=e.id "
                    "JOIN lite_embeddings_fts_docsize f ON f.id=e.id "
                    "WHERE e.knowledge_base_id=? AND e.is_enabled=1 AND e.dimension=1024 AND e.source_type=0",
                    parameters,
                ).fetchall()
                if has_vectors
                else []
            )
        questions = {k for k, source, chunk in vectors if source != chunk}
        wal = path.with_name(path.name + "-wal")
        return (
            docs,
            {k for k, source, chunk in vectors if source == chunk},
            {
                "question_documents": questions,
                "question_index_rows": sum(s != c for _, s, c in vectors),
                "vector_rows": len(vectors),
                "storage": {
                    "sqlite_main_bytes": path.stat().st_size,
                    "sqlite_wal_bytes": wal.stat().st_size if wal.exists() else 0,
                },
            },
        )

    @staticmethod
    def batches(rows, size):
        # Keep Python 3.11 support for the pinned ContextBase venv.
        from itertools import islice

        iterator = iter(rows)
        while batch := list(islice(iterator, size)):
            yield batch

    def import_corpus(self, product, api, runtime, folder):
        args = self.args
        api.timeout = args.index_timeout
        checkpoint = args.work / (product + ".sqlite")
        timing = folder / "import-batches.jsonl"
        first_submission = folder / "first-submission.json"
        summary = folder / "import.json"
        run_id = self.phase(product, "ingest", "import")
        started = time.time()
        new_checkpoint = not checkpoint.exists()
        if new_checkpoint:
            if product == "weknora":
                database = runtime["weknora_db"]
                with closing(
                    sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                ) as native:
                    has_documents = native.execute(
                        "SELECT 1 FROM knowledges "
                        "WHERE knowledge_base_id=? AND deleted_at IS NULL LIMIT 1",
                        (api.scope,),
                    ).fetchone()
            else:
                has_documents, _, _ = self.index_state(product, api, runtime)
            if has_documents:
                raise ValueError(
                    "Index already contains documents without a matching import journal"
                )
        with closing(sqlite3.connect(checkpoint)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            ingest.setup(db, bench.index_binding(api, product, self.source))
            pending_creates = db.execute(
                "SELECT passage_id FROM passage_map WHERE remote_id IS NULL"
            ).fetchall()
            if pending_creates:
                if product != "weknora":
                    raise ValueError(
                        "Invalid unresolved entry in the ContextBase checkpoint"
                    )
                database = runtime["weknora_db"]
                with closing(
                    sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                ) as native:
                    for (passage,) in pending_creates:
                        matches = native.execute(
                            "SELECT id FROM knowledges WHERE knowledge_base_id=? "
                            "AND channel=? AND deleted_at IS NULL",
                            (api.scope, ingest.channel(passage)),
                        ).fetchall()
                        if len(matches) > 1:
                            raise ValueError(
                                f"Ambiguous interrupted creates for {passage}"
                            )
                        ingest.recover(
                            db, api, passage, matches[0][0] if matches else None
                        )
                        append_row(
                            folder / "reconciled-creates.jsonl",
                            {"passage": passage, "recovered_existing": bool(matches)},
                        )
            indexed = db.execute(
                "SELECT COUNT(*) FROM passage_map WHERE indexed=1"
            ).fetchone()[0]
            if summary.exists():
                if indexed != self.expected:
                    raise ValueError(
                        "The completed import journal disagrees with its result"
                    )
            else:
                for batch in self.batches(self.corpus_rows(), args.batch_size):
                    entries = [
                        (
                            row,
                            db.execute(
                                "SELECT remote_id,indexed FROM passage_map WHERE passage_id=?",
                                (row["docid"],),
                            ).fetchone(),
                        )
                        for row in batch
                    ]
                    pending = [
                        (row, entry)
                        for row, entry in entries
                        if not entry or not entry[1]
                    ]
                    if not pending:
                        continue
                    before = time.time()
                    if not first_submission.exists() and not timing.exists():
                        save(first_submission, {"started_unix": before})
                    if product == "contextbase":
                        ingest.contextbase_batch(db, api, [row for row, _ in pending])
                    else:
                        remote_ids = [
                            entry[0]
                            if entry
                            else ingest.weknora_create(db, ImportAPI(api), row)
                            for row, entry in pending
                        ]
                        ingest.wait_for_index(
                            db, api, remote_ids, args.index_timeout, 0.5
                        )
                    after = time.time()
                    indexed += len(pending)
                    append_row(
                        timing,
                        {
                            "started_unix": before,
                            "finished_unix": after,
                            "documents": len(pending),
                            "indexed": indexed,
                        },
                    )
                    if (
                        indexed == self.expected
                        or (indexed // 128) != ((indexed - len(pending)) // 128)
                        or indexed <= args.batch_size
                    ):
                        self.status(
                            state="importing",
                            product=product,
                            indexed=indexed,
                            target=self.expected,
                        )
                if indexed != self.expected:
                    raise ValueError(
                        "Import journal does not cover the selected corpus"
                    )
                with db:
                    db.execute("UPDATE miracl_ingest_run SET complete=1")
            mapping = dict(
                db.execute(
                    "SELECT remote_id,passage_id FROM passage_map WHERE indexed=1"
                )
            )
        docs, vectors, detail = self.index_state(product, api, runtime)
        expected_content = {
            row["docid"]: ingest.passage_content(row).strip()
            for row in self.corpus_rows()
        }
        if set(docs) != set(mapping) or set(mapping.values()) != set(expected_content):
            raise ValueError("Native documents, source IDs and checkpoint disagree")
        if any(
            docs[key].strip() != expected_content[value]
            for key, value in mapping.items()
        ):
            raise ValueError("Native original text differs from the corpus")
        missing = set(mapping) - vectors
        question_docs = detail.pop("question_documents", set())
        missing_questions = (
            set(mapping) - question_docs if product == "weknora" else set()
        )
        coverage = {
            "documents": len(docs),
            "documents_with_vectors": len(vectors),
            "documents_with_indexed_generated_questions": len(question_docs)
            if product == "weknora"
            else None,
            "missing_vector_passages": sorted(mapping[key] for key in missing),
            "missing_question_passages": sorted(
                mapping[key] for key in missing_questions
            ),
            **detail,
        }
        save(folder / "index-coverage.json", coverage)
        if not summary.exists():
            batches = [json.loads(line) for line in timing.read_text().splitlines()]
            usage = self.usage(run_id, folder / "import-model-calls.jsonl.gz")
            if sum(row["documents"] for row in batches) != self.expected:
                raise ValueError(
                    "The timing journal is incomplete; this import is not comparable"
                )
            if not usage.get("chat", {}).get("observed_calls") or not usage.get(
                "embedding", {}
            ).get("observed_calls"):
                raise ValueError("Missing observed LLM or embedding evidence")
            save(
                summary,
                {
                    "indexed_documents": indexed,
                    "first_submit_to_last_completion_s": batches[-1]["finished_unix"]
                    - (
                        read_json(first_submission)["started_unix"]
                        if first_submission.exists()
                        else batches[0]["started_unix"]
                    ),
                    "active_batch_s": sum(
                        row["finished_unix"] - row["started_unix"] for row in batches
                    ),
                    "last_invocation_elapsed_s": time.time() - started,
                    "usage": usage,
                    "index": coverage,
                    "observed_full_llm_usage": usage["chat"]["unmetered_calls"] == 0,
                },
            )
        if missing or missing_questions:
            raise ValueError(
                f"{product} enrichment coverage failed: {len(missing)} without vectors, "
                f"{len(missing_questions)} without indexed questions; see {folder.name}/index-coverage.json"
            )
        return mapping

    def retrieve(self, product, mapping, concurrency, runtime, folder):
        folder.mkdir(exist_ok=True, parents=True)
        if (folder / "metrics.json").exists():
            return
        number = len(list(folder.glob("attempt-*"))) + 1
        attempt = folder / f"attempt-{number:03d}"
        attempt.mkdir()
        self.phase(product, "warmup", f"c{concurrency}-{number}-warmup")
        for query in list(self.topics.values())[: self.plan["warmups_per_window"]]:
            self.fetch_query(bench.prior_api(product), product, query)
        run_id = self.phase(product, "query", f"c{concurrency}-{number}")
        lock_ranks = attempt / "rankings.jsonl"
        roots = runtime.get("roots")
        sampler = Sampler(roots, attempt / "resources.jsonl") if roots else None
        self.status(
            state="querying",
            product=product,
            concurrency=concurrency,
            target=len(self.topics),
        )

        def query(qid, text):
            api = bench.prior_api(product)
            started = time.perf_counter()
            try:
                ranked = self.fetch_query(api, product, text)
                latency = time.perf_counter() - started
                passages = list(dict.fromkeys(mapping[remote] for remote in ranked))
                return {
                    "qid": qid,
                    "passages": passages,
                    "latency_ms": latency * 1000,
                    "success": True,
                    "dataset": self.source["dataset"],
                }
            except Exception as exc:
                reason = exc.reason if isinstance(exc, URLError) else exc
                return {
                    "qid": qid,
                    "passages": [],
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "success": False,
                    "error_type": type(exc).__name__,
                    "timeout": isinstance(reason, TimeoutError),
                    "dataset": self.source["dataset"],
                }

        if sampler:
            sampler.start()
        started = time.perf_counter()
        records = []
        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [
                    pool.submit(query, qid, text) for qid, text in self.topics.items()
                ]
                for future in as_completed(futures):
                    record = future.result()
                    append_row(lock_ranks, record)
                    records.append(record)
                    if len(records) % 25 == 0:
                        self.status(
                            state="querying",
                            product=product,
                            concurrency=concurrency,
                            completed=len(records),
                            target=len(self.topics),
                        )
        finally:
            wall = time.perf_counter() - started
            resource = sampler.finish() if sampler else None
        usage = self.query_usage(run_id, attempt)
        success = sum(row["success"] for row in records)
        nonempty = sum(row["success"] and bool(row["passages"]) for row in records)
        result = evaluate(lock_ranks, self.qrels, self.source)
        verified = self.verify_query_rerank(product, usage, nonempty)
        result.update(
            attempt=attempt.name,
            concurrency=concurrency,
            wall_s=wall,
            successful_queries=success,
            success_rate=success / len(records),
            timeout_queries=sum(row.get("timeout", False) for row in records),
            successful_qps=success / wall,
            usage=usage,
            resources=resource,
            rerank_observed=verified,
        )
        save(attempt / "metrics.json", result)
        if not verified:
            raise ValueError(
                "Rerank calls or the zero-query-generation requirement could not be verified"
            )
        save(folder / "metrics.json", result)

    def fetch_query(self, api, product, query):
        return bench.fetch_ranked(api, product, query, CAP, RERANKER)

    def query_usage(self, run_id, attempt):
        return self.usage(run_id, attempt / "query-model-calls.jsonl.gz")

    def verify_query_rerank(self, product, usage, nonempty):
        rerank = usage.get("rerank", {})
        return (
            "chat" not in usage
            and nonempty > 0
            and rerank.get("observed_calls", 0) >= nonempty
            and 0 < rerank.get("max_pairs_per_call", 0) <= CAP
        )

    def run(self):
        save(self.args.work / "measurement-context.json", {})
        with services(self.args) as runtime:
            save(
                self.args.results / "runtime.json",
                {
                    "models": runtime["models"],
                    "readback": runtime["readback"],
                    "machine": platform.machine(),
                    "kernel": platform.release(),
                    "cpu_quota": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
                    "model_http_keepalive_s": MODEL_HTTP_KEEPALIVE_S,
                },
            )
            mappings = {}
            for product in PRODUCTS:
                folder = self.args.results / product
                folder.mkdir(exist_ok=True)
                self.status(state="importing", product=product, target=self.expected)
                mappings[product] = self.import_corpus(
                    product, bench.prior_api(product), runtime, folder
                )
            for concurrency in CONCURRENCY:
                for product in PRODUCTS if concurrency != 4 else reversed(PRODUCTS):
                    self.retrieve(
                        product,
                        mappings[product],
                        concurrency,
                        runtime,
                        self.args.results / product / f"c{concurrency}",
                    )
        self.report()
        self.status(state="complete", smoke=self.args.smoke)

    def report(self):
        root = self.args.results
        lines = [
            "# MIRACL 中文 LLM 入库与模型重排",
            "",
            f"{self.expected:,} 个段落、{len(self.topics)} 道查询。"
            + (
                "此运行仅用于预检，查询的已知正例参与了样本抽取。"
                if self.args.smoke
                else "各并发档使用完整开发集。"
            ),
            "",
            "| 产品 | 导入墙钟秒 | LLM 调用 | LLM 输入 Token | LLM 输出 Token | Embedding 输入 Token |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for product in PRODUCTS:
            imported = read_json(root / product / "import.json")
            usage = imported["usage"]
            chat, embedding = usage["chat"], usage["embedding"]
            lines.append(
                f"| {product} | {imported['first_submit_to_last_completion_s']:.2f} "
                f"| {chat['calls']:,} | {chat['input_tokens']:,} | {chat['output_tokens']:,} "
                f"| {embedding['input_tokens']:,} |"
            )
            if chat["unmetered_calls"]:
                lines.append(
                    f"| {product} 缺失 usage 的 LLM 调用 | | {chat['unmetered_calls']} | | | |"
                )
        lines += [
            "",
            "| 产品 | 并发 | 成功数 | 成功 QPS | p95 ms | nDCG@10 | Recall@10 | MRR@10 | Hit@1 | P@5 | P@10 | 重排输入 Token |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for concurrency in CONCURRENCY:
            for product in PRODUCTS:
                m = read_json(root / product / f"c{concurrency}" / "metrics.json")
                lines.append(
                    f"| {product} | {concurrency} | {m['successful_queries']} | "
                    f"{m['successful_qps']:.3f} | {m['serial_p95_ms']:.2f} | {m['ndcg_at_10']:.6f} | "
                    f"{m['recall_at_10']:.2%} | {m['mrr_at_10']:.6f} | {m['hit_at_1']:.2%} | "
                    f"{m['precision_at_5']:.2%} | {m['precision_at_10']:.2%} | "
                    f"{m['usage']['rerank']['input_tokens']:,} |"
                )
        lines += [
            "",
            "各档完整延迟分位数、返回深度、未标注率、资源数据和模型用量见对应 metrics.json。",
            "",
        ]
        (root / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        render_report(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "data",
        "work",
        "results",
        "pg-admin-dsn-file",
        "contextbase-source",
        "contextbase-python",
        "weknora-source",
        "weknora-binary",
        "model-python",
    ):
        parser.add_argument(
            "--" + name,
            type=lambda value: Path(value).expanduser().absolute(),
            required=True,
        )
    parser.add_argument(
        "--run-name",
        required=True,
        help="Unique name for a new dedicated database and KB",
    )
    parser.add_argument(
        "--llm-url", required=True, help="OpenAI-compatible base URL, including /v1"
    )
    parser.add_argument("--llm-model", required=True)
    parser.add_argument(
        "--llm-sha256", help="Fingerprint of a locally hosted model artifact"
    )
    parser.add_argument("--model-port", type=int, default=18090)
    parser.add_argument("--contextbase-port", type=int, default=18889)
    parser.add_argument("--weknora-port", type=int, default=18081)
    parser.add_argument("--onnx-threads", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--index-timeout", type=float, default=3600)
    parser.add_argument("--query-timeout", type=float, default=600)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Eight selected examples, not a quality benchmark",
    )
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[a-z0-9_]{1,24}", args.run_name)
        or not 1 <= args.batch_size <= 64
    ):
        parser.error(
            "Use a run name of 1–24 lowercase letters/digits/underscores and batch size 1–64"
        )
    if args.onnx_threads < 1 or min(args.index_timeout, args.query_timeout) <= 0:
        parser.error("Thread count and timeouts must be positive")
    if (
        args.work.resolve().is_relative_to(Path(__file__).resolve().parents[1])
        or args.work == args.results
    ):
        parser.error(
            "Keep private runtime files outside the repository and separate from results"
        )
    args.work.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.results.mkdir(parents=True, exist_ok=True)

    def interrupt(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    with run_lock(args.work / "run.lock"):
        run = Run(args)
        try:
            run.run()
        except BaseException as exc:
            previous = read_json(args.results / "status.json")
            run.status(
                state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                error_type=type(exc).__name__,
                error=str(exc),
                previous=previous,
            )
            raise


if __name__ == "__main__":
    main()
