"""Frozen native-query, resource and synthetic-document visibility measurements.

Uses existing benchmark API clients, scoring and isolated ContextBase session.
All actual model attempts are archived; no generation model is used.
"""

import argparse
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time

import bench
from audit_index import audit
from extend_retrieval_metrics import evaluate
from optimize_contextbase_native import Session
from run_native_weight_ablation import publish
from run_scale import http, save


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def proc_snapshot():
    rows = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            comm, rest = (path / "stat").read_text().rsplit(") ", 1)
            values = rest.split()
            if values[0] == "Z":
                continue
            rows[int(path.name)] = {
                "ppid": int(values[1]),
                "comm": comm.split("(", 1)[1],
                "ticks": int(values[11]) + int(values[12]),
                "rss": int(values[21]) * os.sysconf("SC_PAGE_SIZE"),
            }
        except (OSError, ValueError, IndexError):
            continue
    return rows


class Sampler:
    def __init__(self, roots, target):
        self.roots, self.target = roots, target
        self.stop_event = threading.Event()
        self.samples = []

    def snapshot(self):
        procs = proc_snapshot()
        groups = {}
        for name, root in self.roots.items():
            selected = (
                {p for p, row in procs.items() if row["comm"] == "postgres"}
                if name == "postgres_instance"
                else {root}
            )
            while True:
                children = {p for p, row in procs.items() if row["ppid"] in selected}
                if children <= selected:
                    break
                selected |= children
            groups[name] = {
                "pids": sorted(selected & procs.keys()),
                "rss_bytes_sum": sum(procs[p]["rss"] for p in selected if p in procs),
                "cpu_ticks": {
                    str(p): procs[p]["ticks"] for p in selected if p in procs
                },
            }
        return {
            "unix": time.time(),
            "groups": groups,
            "host_cgroup_memory_bytes": int(
                Path("/sys/fs/cgroup/memory.current").read_text()
            ),
        }

    def start(self):
        self.samples.append(self.snapshot())

        def loop():
            while not self.stop_event.wait(0.5):
                self.samples.append(self.snapshot())

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        self.thread.join()
        self.samples.append(self.snapshot())
        with self.target.open("w") as stream:
            for row in self.samples:
                stream.write(json.dumps(row) + "\n")
        summary = {}
        for name in self.roots:
            first = self.samples[0]["groups"][name]
            last = self.samples[-1]["groups"][name]
            summary[name] = {
                "baseline_rss_bytes_sum": first["rss_bytes_sum"],
                "peak_sampled_rss_bytes_sum": max(
                    s["groups"][name]["rss_bytes_sum"] for s in self.samples
                ),
                "cpu_seconds": sum(
                    max(0, ticks - first["cpu_ticks"].get(pid, 0))
                    for pid, ticks in last["cpu_ticks"].items()
                )
                / os.sysconf("SC_CLK_TCK"),
            }
        return {
            "sample_interval_s": 0.5,
            "components": summary,
            "peak_host_cgroup_memory_bytes": max(
                s["host_cgroup_memory_bytes"] for s in self.samples
            ),
            "limitations": "RSS sums may count shared pages more than once; PostgreSQL is instance-wide, not single-bank attribution. Shared model and idle peer API are reported separately. Peaks are sampled.",
        }


class Suite:
    def __init__(self, args):
        self.args = args
        self.out = args.results.resolve()
        self.work = args.private_work.resolve()
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.repo = Path(__file__).resolve().parents[1]
        self.plan = read_json(self.out / "experiment-plan.json")
        assert digest(Path(__file__)) == self.plan["runner_sha256"]
        for relative, expected in self.plan["code_sha256"].items():
            assert digest(self.repo / relative) == expected
        self.settings = read_json(args.settings)
        self.data = Path(self.settings["data100k"])
        self.measure = Path(self.settings["measurement_dir"])
        self.topics, self.qrels = bench.topics_and_qrels(self.data)
        self.source = bench.dataset_source(self.data)
        assert self.source["subset"]["file"]["sha256"] == self.plan["corpus_sha256"]
        self.c = None
        self.wchild = None
        self.completed = []
        self.mapping = {}
        self.model_pid = read_json(self.work / "model-pid.json")["pid"]

    def status(self, **values):
        save(
            self.out / "execution-status.json",
            {"completed_stages": self.completed, **values},
        )

    def context(self, product, phase, runid):
        save(
            self.measure / "measurement-context.json",
            {product: {"phase": phase, "id": "stage-batch", "run_id": runid}},
        )

    def archive_usage(self, runid, folder):
        totals = {}
        with gzip.open(folder / "model-calls.jsonl.gz", "wt") as dst:
            for line in (self.measure / "model-usage.jsonl").open():
                row = json.loads(line)
                if row.get("run_id") != runid:
                    continue
                dst.write(line)
                key = row["phase"] + "/" + row["kind"]
                entry = totals.setdefault(
                    key, {"calls": 0, "input_tokens": 0, "pairs": 0}
                )
                for field in entry:
                    entry[field] += row.get(field, 0)
        assert all(not key.endswith(("/rerank", "/chat")) for key in totals)
        save(folder / "model-usage.json", totals)
        return totals

    def start(self):
        previous = self.repo / "results/miracl-zh-contextbase-native-optimize-20261114"
        for name in ("fresh-topics.tsv", "fresh-qrels.tsv"):
            shutil.copyfile(previous / name, self.out / name)
        carg = Namespace(
            settings=self.args.settings,
            results=self.out,
            work=Path("/workspace/work/rrf-config-validation"),
            source=Path("/workspace/work/contextbase-rrf-config"),
            python=Path("/workspace/work/rrf-config-venv/bin/python"),
        )
        self.c = Session(carg, read_json(previous / "experiment-plan.json"))
        self.c.start()
        updates = read_json(previous / "recommended-config.json")["bank_config_updates"]
        self.c.api.request("/config", {"updates": updates}, method="PATCH")
        cfg = self.c.api.request("/config")["config"]
        assert all(cfg[k] == v for k, v in updates.items())
        save(self.out / "contextbase-config.json", {k: cfg[k] for k in updates})
        self.context("contextbase", "probe", "metrics-suite-20261116/probe")
        probe = self.c.api.request(
            "/memories/recall",
            {
                "query": self.topics["4714933#0"],
                "budget": "high",
                "max_tokens": 8192,
                "types": ["world", "experience"],
                "trace": True,
            },
        )
        with gzip.open(self.out / "contextbase-probe.json.gz", "wt") as stream:
            json.dump(probe, stream, ensure_ascii=False)
        trace = probe["trace"]
        assert trace["query"]["budget"] == 300
        assert any(
            r["method_name"] == "bm25" and r["results"]
            for r in trace["retrieval_results"]
        )
        phases = {
            r["phase_name"]: r["details"] for r in trace["summary"]["phase_metrics"]
        }
        assert phases["reranking"]["reranker_type"] == "rrf-passthrough"
        self.start_weknora()
        for product, checkpoint in (
            ("contextbase", self.c.checkpoint),
            ("weknora", self.work / "weknora/ingest.sqlite"),
        ):
            with sqlite3.connect(
                checkpoint.resolve().as_uri() + "?mode=ro", uri=True
            ) as conn:
                self.mapping[product] = dict(
                    conn.execute(
                        "SELECT remote_id,passage_id FROM passage_map WHERE indexed=1"
                    )
                )
            assert len(self.mapping[product]) == 100000
        self.roots = {
            "contextbase_api": self.c.child.pid,
            "weknora_api": self.wchild.pid,
            "shared_model": self.model_pid,
            "postgres_instance": 0,
        }
        self.storage()

    def start_weknora(self):
        original = Path(self.settings["work"]) / "runtime-v2/weknora-100k"
        profile = self.work / "weknora"
        profile.mkdir(mode=0o700, exist_ok=True)
        marker = profile / "snapshot-complete.json"
        if not marker.exists():
            for name in ("weknora.db", "ingest.sqlite"):
                with (
                    sqlite3.connect(
                        (original / name).resolve().as_uri() + "?mode=ro", uri=True
                    ) as src,
                    sqlite3.connect(profile / name) as dst,
                ):
                    src.backup(dst)
                (profile / name).chmod(0o600)
            for name in ("credentials.json", "builtin-models.yaml"):
                shutil.copyfile(original / name, profile / name)
                (profile / name).chmod(0o600)
            shutil.copytree(original / "files", profile / "files", dirs_exist_ok=True)
            save(
                marker,
                {"source": "preserved original 100k SQLite", "snapshot_complete": True},
            )
        auth = read_json(profile / "credentials.json")
        env = os.environ.copy()
        env.update(
            DB_DRIVER="sqlite",
            DB_PATH=str(profile / "weknora.db"),
            RETRIEVE_DRIVER="sqlite",
            STORAGE_TYPE="local",
            LOCAL_STORAGE_BASE_DIR=str(profile / "files"),
            STREAM_MANAGER_TYPE="memory",
            JWT_SECRET=auth["jwt"],
            SYSTEM_AES_KEY=auth["aes"],
            BUILTIN_MODELS_CONFIG=str(profile / "builtin-models.yaml"),
            WEKNORA_SANDBOX_MODE="disabled",
            WEKNORA_SANDBOX_DOCKER_ENABLED="false",
            SSRF_WHITELIST="127.0.0.1",
            SERVER_HOST="127.0.0.1",
            SERVER_PORT="18081",
            GIN_MODE="release",
            LOG_LEVEL="warn",
            AUTO_MIGRATE="true",
            NEO4J_ENABLE="false",
            ENABLE_GRAPH_RAG="false",
            DISABLE_REGISTRATION="false",
        )
        env.pop("REDIS_ADDR", None)
        self.wlog = (profile / "service.log").open("a")
        self.wchild = subprocess.Popen(
            [str(self.repo / "deployment/weknora-lite")],
            cwd=self.repo / "deployment/weknora",
            env=env,
            stdout=self.wlog,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 120
        while True:
            if self.wchild.poll() is not None:
                raise RuntimeError(
                    "WeKnora clone startup exited; inspect private service log"
                )
            try:
                http("http://127.0.0.1:18081/health")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("WeKnora clone readiness timeout")
                time.sleep(1)
        base = "http://127.0.0.1:18081"
        login = http(
            base + "/api/v1/auth/login",
            {"email": "benchmark@weknora.test", "password": auth["password"]},
        )
        cfg = {
            "embedding_top_k": 60,
            "rerank_top_k": 60,
            "vector_threshold": 0,
            "keyword_threshold": 0,
            "rerank_threshold": 0,
            "rerank_model_id": "benchmark-bge-reranker",
            "rrf_k": 30,
            "rrf_vector_weight": 0.7,
            "rrf_keyword_weight": 0.3,
        }
        result = http(
            base + "/api/v1/tenants/kv/retrieval-config", cfg, login["token"], "PUT"
        )
        assert result["data"] == cfg
        save(self.out / "weknora-config.json", cfg)
        connection = read_json(original / "connection.json")
        connection["WEKNORA_URL"] = base
        os.environ.update(connection)
        self.wapi = bench.prior_api("weknora")
        with contextlib.redirect_stdout(io.StringIO()):
            audit(
                Namespace(
                    data=self.data,
                    checkpoint=profile / "ingest.sqlite",
                    product="weknora",
                    weknora_db=profile / "weknora.db",
                    output=self.out / "weknora-clone-audit.json",
                )
            )

    def storage(self):
        import psycopg2

        with psycopg2.connect(self.c.url) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT sum(pg_table_size(c.oid)),sum(pg_indexes_size(c.oid)) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='r'",
                ("ctxdb_ws_miracl_scale_100k",),
            )
            table, indexes = cur.fetchone()
        db = self.work / "weknora/weknora.db"
        save(
            self.out / "storage.json",
            {
                "measured_before_synthetic_mutations": True,
                "contextbase": {
                    "database_total_bytes": total,
                    "workspace_tables_including_toast_bytes": int(table),
                    "workspace_indexes_bytes": int(indexes),
                },
                "weknora": {
                    "sqlite_main_file_bytes": db.stat().st_size,
                    "sqlite_wal_bytes": db.with_name(db.name + "-wal").stat().st_size
                    if db.with_name(db.name + "-wal").exists()
                    else 0,
                },
                "limitation": "Native database footprints, not normalized storage engines; PostgreSQL shared cluster/runtime files are outside per-database size.",
            },
        )

    def phase(self, product, label, qids, concurrency):
        for relative, expected in self.plan["code_sha256"].items():
            assert digest(self.repo / relative) == expected
        folder = self.out / "runs" / label / product
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / "metrics.json").exists():
            self.completed.append(label + "/" + product)
            return
        attempts = sorted(folder.glob("attempt-*"))
        active = folder / f"attempt-{len(attempts) + 1:03d}"
        active.mkdir()
        runid = "metrics-suite-20261116/" + label + "/" + product + "/" + active.name
        save(
            active / "config.json",
            {
                "query_ids": qids,
                "concurrency": concurrency,
                "plan_sha256": digest(self.out / "experiment-plan.json"),
                "product": product,
                "run_id": runid,
                "timeout_s": 90,
                "automatic_client_retries": 0,
            },
        )
        self.context(product, "warmup", runid)
        api = bench.prior_api(product)
        for i in range(4):
            bench.fetch_ranked(
                api,
                product,
                f"检索预热：第{i + 1}次中文文档检索",
                60,
                "benchmark-bge-reranker",
                8192,
                True,
            )
        self.context(product, "query", runid)

        def query(qid):
            client = bench.prior_api(product)
            start = time.perf_counter()
            begin_unix = time.time()
            try:
                remote = bench.fetch_ranked(
                    client,
                    product,
                    self.topics[qid],
                    60,
                    "benchmark-bge-reranker",
                    8192,
                    True,
                )
                latency = 1000 * (time.perf_counter() - start)
                ranked = [self.mapping[product][r] for r in dict.fromkeys(remote)]
                assert len(ranked) <= 60 and all(bench.PASSAGE_ID(p) for p in ranked)
                return {
                    "dataset": self.source["dataset"],
                    "qid": qid,
                    "passages": ranked,
                    "latency_ms": latency,
                    "success": True,
                    "begin_unix": begin_unix,
                    "completed_unix": time.time(),
                }
            except Exception as error:
                return {
                    "dataset": self.source["dataset"],
                    "qid": qid,
                    "passages": [],
                    "latency_ms": 1000 * (time.perf_counter() - start),
                    "success": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "begin_unix": begin_unix,
                    "completed_unix": time.time(),
                }

        sampler = Sampler(self.roots, active / "resources.jsonl")
        self.status(
            state="querying",
            stage=label,
            product=product,
            completed_queries=0,
            target_queries=len(qids),
        )
        sampler.start()
        start = time.perf_counter()
        rows = []
        with (
            (active / "rankings.jsonl").open("w") as output,
            ThreadPoolExecutor(max_workers=concurrency) as pool,
        ):
            futures = [pool.submit(query, q) for q in qids]
            for future in as_completed(futures):
                row = future.result()
                rows.append(row)
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
                if len(rows) % 25 == 0 or len(rows) == len(qids):
                    self.status(
                        state="querying",
                        stage=label,
                        product=product,
                        completed_queries=len(rows),
                        target_queries=len(qids),
                    )
                    print("PROGRESS", label, product, len(rows), len(qids), flush=True)
        wall = time.perf_counter() - start
        resources = sampler.finish()
        save(active / "resources.json", resources)
        score = evaluate(
            active / "rankings.jsonl", {q: self.qrels[q] for q in qids}, self.source
        )
        usage = self.archive_usage(runid, active)
        success = sum(r["success"] for r in rows)
        metrics = {
            **{k: v for k, v in score.items() if k != "per_topic"},
            "concurrency": concurrency,
            "attempted_queries": len(rows),
            "successful_queries": success,
            "success_rate": success / len(rows),
            "timeout_queries": sum(
                r.get("error_type") in ("TimeoutError", "timeout") for r in rows
            ),
            "active_window_s": wall,
            "successful_qps": success / wall,
            "attempted_qps": len(rows) / wall,
            "attempt_directory": active.name,
            "model_usage": usage,
            "measurement": "Closed-loop workers; queue wait before worker starts excluded from request latency. QPS includes dispatch and durable result logging. Latencies include failures.",
        }
        save(active / "quality.json", score)
        save(folder / "metrics.json", metrics)
        self.completed.append(label + "/" + product)
        self.status(state="stage_complete", stage=label, product=product)
        print(
            "STAGE_COMPLETE",
            label,
            product,
            "QPS",
            metrics["successful_qps"],
            flush=True,
        )
        publish(
            self.repo,
            self.out,
            "Archive metrics suite " + label + "/" + product,
            self.args.publish,
        )

    def search_rows(self, product, query):
        if product == "contextbase":
            value = self.c.api.request(
                "/memories/recall",
                {
                    "query": query,
                    "budget": "high",
                    "types": ["world", "experience"],
                    "max_tokens": 8192,
                    "include": {"chunks": {"max_tokens": 16384}},
                },
            )
            return [
                (row["document_id"], row.get("text", "") + row.get("content", ""))
                for row in value["results"]
            ]
        value = self.wapi.request(
            "/knowledge-search",
            {
                "query": query,
                "knowledge_base_id": self.wapi.scope,
                "match_count": 60,
                "disable_vector_match": False,
                "disable_keywords_match": False,
                "vector_threshold": 0.0,
                "keyword_threshold": 0.0,
                "rerank": {
                    "enabled": False,
                    "model_id": "benchmark-bge-reranker",
                    "top_k": 60,
                    "threshold": 0.0,
                },
            },
        )
        return [
            (row["knowledge_id"], row.get("content", "")) for row in value["data"] or []
        ]

    def visibility(self, product):
        folder = self.out / "visibility" / product
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / "complete.json").exists():
            return
        runid = "metrics-suite-20261116/visibility/" + product
        self.context(product, "visibility", runid)
        api = self.c.api if product == "contextbase" else self.wapi
        for i in range(self.plan["visibility_repetitions"]):
            path = folder / f"document-{i:02d}.json"
            if path.exists():
                if read_json(path).get("complete"):
                    continue
                raise RuntimeError(
                    "Unresolved synthetic document; preserve its journal and reconcile before continuing"
                )
            identifier = f"metrics-suite-20261116-canary-{product}-{i:02d}"
            query = f"知识库可见性探针编号 {identifier} 的校验词是什么？"
            journal = {
                "synthetic_identifier": identifier,
                "product": product,
                "operations": [],
            }
            save(path, journal)
            remote = identifier if product == "contextbase" else None
            for operation, marker in (
                ("create", "初版校验词青松"),
                ("update", "新版校验词紫竹"),
                ("delete", ""),
            ):
                content = f"# 独立知识库测量文档 {identifier}\n探针编号 {identifier}。当前校验词：{marker}。本条为性能测量的临时合成文档。"
                self.status(
                    state="visibility",
                    product=product,
                    repetition=i,
                    operation=operation,
                )
                start = time.perf_counter()
                if product == "contextbase":
                    if operation == "delete":
                        assert remote == identifier and identifier.startswith(
                            "metrics-suite-20261116-canary-"
                        )
                        api.request("/documents/" + remote, method="DELETE")
                    else:
                        api.request(
                            "/memories",
                            {
                                "items": [
                                    {
                                        "document_id": remote,
                                        "content": content,
                                        "update_mode": "replace",
                                        "timestamp": "unset",
                                    }
                                ],
                                "async": False,
                            },
                        )
                elif operation == "create":
                    remote = api.request(
                        f"/knowledge-bases/{api.scope}/knowledge/manual",
                        {
                            "title": identifier,
                            "content": content,
                            "status": "publish",
                            "channel": identifier,
                        },
                    )["data"]["id"]
                elif operation == "update":
                    api.request(
                        "/knowledge/manual/" + remote,
                        {"title": identifier, "content": content, "status": "publish"},
                        method="PUT",
                    )
                else:
                    assert journal.get("remote_id") == remote and journal[
                        "synthetic_identifier"
                    ].startswith("metrics-suite-20261116-canary-")
                    assert (
                        api.request("/knowledge/" + remote)["data"]["title"]
                        == identifier
                    )
                    api.request("/knowledge/" + remote, method="DELETE")
                ack = time.perf_counter()
                journal["remote_id"] = remote
                save(path, journal)
                observations = []
                deadline = ack + 180
                while True:
                    found = [
                        (rid, text)
                        for rid, text in self.search_rows(product, query)
                        if rid == remote
                    ]
                    observed = time.perf_counter()
                    # Confirm updated stored source separately: retrieved chunks may
                    # omit a marker even when the document itself is visible.
                    source_ok = True
                    if operation != "delete":
                        if product == "contextbase":
                            stored = api.request("/documents/" + remote).get(
                                "original_text", ""
                            )
                        else:
                            stored = (
                                api.request("/knowledge/" + remote)["data"].get(
                                    "metadata"
                                )
                                or {}
                            )
                            if isinstance(stored, str):
                                stored = json.loads(stored)
                            stored = stored.get("content", "")
                        source_ok = marker in stored
                    visible = (
                        (not found)
                        if operation == "delete"
                        else bool(found)
                        and source_ok
                        and any(marker in text for _, text in found)
                    )
                    observations.append(
                        {
                            "after_ack_ms": 1000 * (observed - ack),
                            "found": bool(found),
                            "source_marker_matches": source_ok,
                            "visible": visible,
                        }
                    )
                    if visible:
                        break
                    if observed > deadline:
                        journal["failure"] = {
                            "operation": operation,
                            "observations": observations,
                        }
                        save(path, journal)
                        raise TimeoutError(
                            "Synthetic mutation was not visible within 180 seconds; document journal retained"
                        )
                    time.sleep(0.25)
                journal["operations"].append(
                    {
                        "operation": operation,
                        "write_ack_ms": 1000 * (ack - start),
                        "ack_to_first_observed_visible_ms": 1000 * (observed - ack),
                        "submit_to_first_observed_visible_ms": 1000
                        * (observed - start),
                        "observations": observations,
                    }
                )
                save(path, journal)
            journal["complete"] = True
            save(path, journal)
            print("VISIBILITY_COMPLETE", product, i + 1, flush=True)
        self.archive_usage(runid, folder)
        save(
            folder / "complete.json",
            {
                "repetitions": self.plan["visibility_repetitions"],
                "operations": ["create", "update", "delete"],
                "poll_sleep_s": 0.25,
                "limitation": "Measured first observation includes native search/probe latency. Not a millisecond-accurate internal commit timestamp; all mutations are synthetic documents in isolated copies.",
            },
        )
        publish(
            self.repo,
            self.out,
            "Archive metrics suite visibility " + product,
            self.args.publish,
        )

    def audit_retention(self):
        for product, checkpoint, wdb in (
            ("contextbase", self.c.checkpoint, None),
            (
                "weknora",
                self.work / "weknora/ingest.sqlite",
                self.work / "weknora/weknora.db",
            ),
        ):
            os.environ["AUDIT_POSTGRES_URL"] = self.c.url
            with contextlib.redirect_stdout(io.StringIO()):
                audit(
                    Namespace(
                        data=self.data,
                        checkpoint=checkpoint,
                        product=product,
                        weknora_db=wdb,
                        output=self.out / (product + "-final-audit.json"),
                    )
                )
        import psycopg2.extensions

        dsn = psycopg2.extensions.parse_dsn(self.settings["postgres_url"])
        dsn["dbname"] = "miracl_scale_100k_v2"
        os.environ["AUDIT_POSTGRES_URL"] = psycopg2.extensions.make_dsn(**dsn)
        for product in ("contextbase", "weknora"):
            profile = Path(self.settings["work"]) / "runtime-v2" / (product + "-100k")
            with contextlib.redirect_stdout(io.StringIO()):
                audit(
                    Namespace(
                        data=self.data,
                        checkpoint=profile / "ingest.sqlite",
                        product=product,
                        weknora_db=profile / "weknora.db"
                        if product == "weknora"
                        else None,
                        output=self.out / (product + "-original-final-audit.json"),
                    )
                )
        hashes = {
            read_json(self.out / name)["canonical_content_sha256"]
            for name in (
                "contextbase-final-audit.json",
                "weknora-final-audit.json",
                "contextbase-original-final-audit.json",
                "weknora-original-final-audit.json",
            )
        }
        assert len(hashes) == 1
        save(
            self.out / "retention.json",
            {
                "original_data_and_backups_not_deleted": True,
                "original_apis_not_reconfigured_or_restarted": True,
                "isolated_corpus_reaudited_after_synthetic_document_cleanup": True,
                "official_passages_each": 100000,
                "independent_weknora_snapshot_retained": True,
            },
        )

    def close(self):
        restored = False
        if self.c:
            try:
                restored = self.c.close()
            finally:
                self.c.stop()
        if self.wchild and self.wchild.poll() is None:
            self.wchild.terminate()
            self.wchild.wait(30)
            self.wlog.close()
        return restored

    def run(self):
        failure = None
        restored = False
        try:
            self.start()
            for product in ("contextbase", "weknora"):
                self.phase(product, "serial393", list(self.topics), 1)
            for entry in self.plan["load_schedule"]:
                self.phase(
                    entry["product"],
                    entry["label"],
                    self.plan["load_query_ids"],
                    entry["concurrency"],
                )
            for product in ("contextbase", "weknora"):
                self.visibility(product)
            self.audit_retention()
        except Exception as error:
            failure = error
            save(
                self.out / "failure.json",
                {
                    "type": type(error).__name__,
                    "message": str(error),
                    "completed_stages": self.completed,
                    "recovery": "Complete phases are reused. Interrupted timing attempts are retained; fresh contiguous timing attempts are measured separately. Unresolved mutation journals require reconciliation without touching original documents.",
                },
            )
        finally:
            restored = self.close()
            self.status(
                state="failed" if failure else "complete",
                restored_contextbase_baseline97=restored,
                generation_model_used=False,
                owned_validation_apis_stopped=True,
            )
            publish(
                self.repo, self.out, "Finish native metrics suite", self.args.publish
            )
        if failure:
            raise failure


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--private-work", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    Suite(parser.parse_args()).run()
