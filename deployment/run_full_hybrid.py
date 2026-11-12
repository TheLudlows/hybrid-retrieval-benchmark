"""Run mixed retrieval with Chinese lexical search and shared BGE reranking."""
from collections import Counter
from datetime import datetime, timezone
import asyncio
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import tiktoken

import run_eval as live
from run_native import cleanup, ready, start

root = live.ROOT
bench = live.bench
pilot_dataset = os.environ.get("BENCH_DATASET_DIR")
if pilot_dataset:
    bench.ROOT = Path(pilot_dataset).resolve()
run_id = datetime.now(timezone.utc).strftime("hybrid_%Y%m%d_%H%M%S")
run_dir = root / "results" / run_id
run_dir.mkdir(parents=True)
print("RUN", run_dir, flush=True)
questions = bench.read_rows(bench.ROOT / "questions.jsonl")
gold = {question["id"]: question for question in questions}
query_ids = {question["query"]: question["id"] for question in questions}
encoding = tiktoken.get_encoding("o200k_base")
os.environ["BENCH_HTTP_TIMEOUT_SECONDS"] = "600"
os.environ["BENCH_MEASUREMENT_DIR"] = str(run_dir)
bench.write_json(run_dir / "measurement-context.json", {})
original_request = live.request


def request(url, payload=None, token=None, method=None):
    if url.endswith("/knowledge-bases") and payload:
        payload = {**payload, "embedding_model_id": "benchmark-bge-m3"}
    return original_request(url, payload, token, method)


def start_configured(name, command, cwd, env):
    if name == "contextbase":
        patched = root / "contextbase-hybrid-audit"
        env = {**env, "PYTHONPATH": env["PYTHONPATH"].replace(str(root / "contextbase"), str(patched))}
        cwd = patched
        values = {
            "EMBEDDINGS_OPENAI_MODEL": "bge-m3",
            "EMBEDDINGS_OPENAI_DIMENSIONS": "1024",
            "TEXT_SEARCH_EXTENSION": "pg_search",
            "TEXT_SEARCH_EXTENSION_PG_SEARCH_TOKENIZER": "jieba",
            "ENABLE_TEXT_SEARCH": "true", "ENABLE_TEMPORAL_RETRIEVAL": "true",
            "ENABLE_GRAPH_RETRIEVAL": "true", "ENABLE_RERANKING": "true",
            "RERANKER_PROVIDER": "tei",
            "RERANKER_TEI_URL": live.MODEL_URL + "/contextbase/tei",
            "RERANKER_TEI_HTTP_TIMEOUT": "240", "RERANKER_MAX_CANDIDATES": "60",
            "RERANKER_MAX_CANDIDATES_HIGH": os.environ.get("BENCH_RERANK_HIGH", "0"),
        }
        for prefix in ("CONTEXTBASE_API_", "HINDSIGHT_API_"):
            env.update({prefix + key: value for key, value in values.items()})
    if name == "weknora":
        state = root / "state/weknora-hybrid"
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        models = state / "builtin-models.yaml"
        yaml = (root / "state/weknora/builtin-models.yaml").read_text()
        models.write_text(yaml.replace("benchmark-embedding", "benchmark-bge-m3")
                          .replace("multilingual-minilm", "bge-m3").replace("dimension: 384", "dimension: 1024")
                          + f"""  - id: benchmark-bge-reranker
    type: Rerank
    name: BAAI/bge-reranker-v2-m3
    source: remote
    is_default: true
    parameters:
      provider: cohere
      base_url: {live.MODEL_URL}/weknora/v1/rerank
      api_key: local-benchmark
      spec:
        compat:
          request_timeout_seconds: 300
""")
        env = {**env, "DB_PATH": str(state / "weknora.db"),
               "LOCAL_STORAGE_BASE_DIR": str(state / "files"), "BUILTIN_MODELS_CONFIG": str(models),
               "LOG_LEVEL": "info"}
    return start(name, command, cwd, env)


live.start = start_configured
live.request = request


def start_postgres():
    import asyncpg

    bundle = root / "full-postgres"
    bin_dir = bundle / "postgres/lib/postgresql/18/bin"
    if not (bin_dir.parent / "lib/pg_search.so").exists():
        shutil.copy2(bundle / "extensions/lib/pg_search.so", bin_dir.parent / "lib")
        share = bundle / "postgres/share/postgresql/18/extension"
        for extension in (bundle / "extensions/share/extension").glob("pg_search*"):
            shutil.copy2(extension, share)
    state = Path(os.environ.get("BENCH_CONTEXTBASE_STATE", root / "state/contextbase-hybrid")).resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = state / "pgdata"
    password_file = state / "postgres-password"
    if not password_file.exists():
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            print(secrets.token_hex(24), file=out)
    if not (data / "PG_VERSION").exists():
        subprocess.run([bin_dir / "initdb", "-D", data, "-U", "postgres", "-A", "scram-sha-256",
                        "--pwfile", password_file, "--no-sync"], check=True)
    db_url = f"postgresql://postgres:{password_file.read_text().strip()}@127.0.0.1:15433/postgres"
    server = start("hybrid-postgres", [bin_dir / "postgres", "-D", data, "-c", "listen_addresses=127.0.0.1",
                   "-c", "port=15433", "-c", "unix_socket_directories=",
                   "-c", "shared_preload_libraries=pg_search", "-c", "jit=off"], root, os.environ)

    async def prepare_extensions():
        for _ in range(60):
            if server.poll() is not None:
                raise RuntimeError("PostgreSQL exited. Inspect hybrid-postgres-runtime.log")
            try:
                db = await asyncpg.connect(db_url, timeout=2)
                break
            except (OSError, asyncpg.CannotConnectNowError):
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError("PostgreSQL did not become available")
        try:
            for extension in ("vector", "pg_trgm", "btree_gin", "pg_search"):
                await db.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            records = await db.fetch("SELECT extname, extversion FROM pg_extension ORDER BY extname")
            versions = {record["extname"]: record["extversion"] for record in records}
            versions["postgres"] = await db.fetchval("SHOW server_version")
            bench.write_json(run_dir / "contextbase-postgres-versions.json", versions)
            print("READY postgres", versions, flush=True)
        finally:
            await db.close()

    asyncio.run(prepare_extensions())
    return state, db_url


def start_contextbase_hybrid(existing_bank=None):
    state, db_url = start_postgres()
    env = live.contextbase_environment()
    env.update(CONTEXTDB_DEPLOY_DIR=str(state), CONTEXTBASE_API_DATABASE_URL=db_url,
               HINDSIGHT_API_DATABASE_URL=db_url, CONTEXTBASE_API_RETAIN_EXTRACTION_MODE="chunks",
               CONTEXTBASE_API_RETAIN_CHUNK_SIZE="512")
    process = start_configured("contextbase", [str(root / "api-venv/bin/python"), "-m",
        "hindsight_ext_contextdb.local", "--component", "api", "--port", "18888"], root / "contextbase", env)
    url = live.CONTEXTBASE_URL
    ready("contextbase", process, url + "/health/ready", timeout=600)
    admin = json.loads((state / "admin-key.json").read_text())["secret"]
    bank = existing_bank or "recall_" + run_id
    if existing_bank is None:
        request(url + "/v1/default/banks/" + bank, {"name": bank}, admin, method="PUT")
    users = url + "/ext/contextdb/v1/users"
    user = request(users, {"name": "bench-" + run_id}, admin)["user_id"]
    key = request(f"{users}/{user}/keys", {"name": "document-hybrid-recall", "authorization": {
        "mode": "selected", "workspaces": [{"bank_id": bank, "permission": "read_write"}]}}, admin)["secret"]
    os.environ.update(CONTEXTBASE_URL=url, CONTEXTBASE_BANK_ID=bank, CONTEXTBASE_API_KEY=key)
    print("SCOPE contextbase", bank, flush=True)


class HybridAPI(bench.API):
    def __init__(self, product, *, tracking=True):
        super().__init__(product)
        self.product = product
        self.tracking = tracking
        self.probes = ({q["id"] for q in questions[:4]} if pilot_dataset
                       else {"alder-08", "elm-09", "birch-10", "alder-03"})
        self.seen_lexical = False
        self.done = 0

    def request(self, path, payload=None, *, method=None):
        if path not in ("/memories/recall", "/knowledge-search"):
            return super().request(path, payload, method=method)
        query_id = query_ids.get(payload["query"], "supplemental")
        if self.tracking:
            live.model_context(self.product, run_id, "retrieve", query_id)
        if self.product == "weknora":
            data = super().request(path, {**payload, "match_count": 60,
                "disable_vector_match": False, "disable_keywords_match": False,
                "vector_threshold": 0.0, "keyword_threshold": 0.0,
                "rerank": {"enabled": True, "model_id": "benchmark-bge-reranker", "top_k": 60, "threshold": 0.0}})
            meta = data.get("meta", {}).get("rerank") or {}
            if not meta.get("applied"):
                raise RuntimeError(f"WeKnora model reranking did not run for {query_id}: {meta}")
            if self.tracking:
                with (run_dir / "weknora-rerank-meta.jsonl").open("a") as out:
                    bench.write_row(out, {"id": query_id, **meta})
        else:
            data = super().request(path, {**payload, "budget": "high", "max_tokens": 8192,
                 "include": {"entities": None, "chunks": {"max_tokens": 16384}},
                 "trace": self.tracking and query_id in self.probes})
            if self.tracking and query_id in self.probes:
                bench.write_json(run_dir / f"contextbase-trace-{query_id}.json", data)
                trace = data["trace"]
                arms = [(arm["method_name"], len(arm["results"])) for arm in trace["retrieval_results"]]
                self.seen_lexical |= any(kind == "bm25" and count > 0 for kind, count in arms)
                print("PROBE", query_id, arms, flush=True)
        if self.tracking:
            self.done += 1
            if self.done % 10 == 0:
                print("QUERIES", self.product, self.done, flush=True)
        return data


def run_product(product):
    resume = os.environ.get("BENCH_RESUME_WEKNORA") if product == "weknora" and not pilot_dataset else None
    completed = []
    mapping = run_dir / f"{product}-documents.json"
    model_log = run_dir / "model-usage.jsonl"
    if product == "contextbase":
        start_contextbase_hybrid()
    elif resume:
        prior = Path(resume).resolve()
        completed = bench.read_rows(prior / "weknora-capture.jsonl")
        if any("error" in row for row in completed):
            raise RuntimeError("Resume requires a capture containing only completed requests")
        previous_id = prior.name
        prior_log = prior / "model-usage.jsonl"
        calls = [row for row in bench.read_rows(prior_log if prior_log.exists() else root / "model-usage.jsonl")
                 if row.get("run_id") == previous_id]
        covered = {
            kind: {row["id"] for row in calls if row["phase"] == "retrieve" and row["kind"] == kind}
            for kind in ("embedding", "rerank")
        }
        ids = covered["embedding"] & covered["rerank"]
        completed = [row for row in completed if row["id"] in ids]
        ids = {row["id"] for row in completed}
        previous_scope = os.environ["BENCH_RESUME_WEKNORA_SCOPE"]
        live.start_weknora(run_id, existing_knowledge=previous_scope)
        shutil.copy2(prior / mapping.name, mapping)
        with model_log.open("a", encoding="utf-8") as out:
            for row in calls:
                if row["phase"] in ("ingest", "probe") or row["id"] in ids:
                    bench.write_row(out, {**row, "run_id": run_id,
                                         "source_run_id": row.get("source_run_id", previous_id)})
        with (run_dir / "weknora-rerank-meta.jsonl").open("w", encoding="utf-8") as out:
            for row in bench.read_rows(prior / "weknora-rerank-meta.jsonl"):
                if row["id"] in ids:
                    bench.write_row(out, row)
    else:
        live.start_weknora(run_id)
    api = HybridAPI(product)
    if not resume:
        live.model_context(product, run_id, "ingest", "run")
        document_context = (lambda identifier: live.model_context(product, run_id, "ingest", identifier)) if pilot_dataset else None
        bench.ingest(api, product, mapping, document_context=document_context)
        print("INDEXED", product, flush=True)
        source_ids = {remote: local for local, remote in json.loads(mapping.read_text()).items()}
        warmups = [q for q in questions if q["id"] in api.probes] if pilot_dataset else [gold["alder-08"]]
        for warmup in warmups:
            bench.retrieve(api, product, warmup["query"], source_ids)
        if pilot_dataset:
            api.probes.clear()
        # Keep probe usage outside the measured workload.
        rows = bench.read_rows(model_log)
        with model_log.open("w", encoding="utf-8") as out:
            for row in rows:
                if row.get("run_id") == run_id and row.get("product") == product and row["phase"] == "retrieve":
                    row["phase"] = "probe"
                bench.write_row(out, row)
        if product == "weknora":
            (run_dir / "weknora-rerank-meta.jsonl").unlink()
    else:
        print("RESUME", product, len(completed), flush=True)
    api.done = len(completed)
    capture = run_dir / f"{product}-capture.jsonl"
    completed_ids = {row["id"] for row in completed}
    order = questions.copy()
    random.Random(20261110).shuffle(order)
    pending = [question for question in order if question["id"] not in completed_ids]
    part = run_dir / f"{product}-capture-resumed.jsonl" if resume else capture
    try:
        bench.collect(api, product, mapping, part, pending, encoding.encode_ordinary, encoding.decode, preserve_order=True)
    finally:
        if resume:
            by_id = {row["id"]: row for row in completed + bench.read_rows(part) if "error" not in row}
            with capture.open("w", encoding="utf-8") as out:
                for question in order:
                    if question["id"] in by_id:
                        bench.write_row(out, by_id[question["id"]])
    assert api.done == len(questions)
    if product == "contextbase":
        assert api.seen_lexical, "No Chinese lexical hit was observed"
    usage = live.model_totals(run_dir, product, run_id)
    raw_calls = bench.read_rows(run_dir / f"{product}-model-calls.jsonl")
    for kind in ("embedding", "rerank"):
        covered = {row["id"] for row in raw_calls if row["phase"] == "retrieve" and row["kind"] == kind}
        assert covered == set(gold), (product, kind, len(covered))
    rows = bench.read_rows(capture)
    assert len(rows) == len(questions) and not any("error" in row for row in rows)
    from contextlib import redirect_stdout
    with (run_dir / f"{product}-summary.json").open("w", encoding="utf-8") as out, redirect_stdout(out):
        bench.score(capture, gold, True, usage)
    print("DONE", product, bench.summarize(rows, gold), flush=True)
    if pilot_dataset:
        from pilot_metrics import run_supplemental, score_pilot
        bench.write_json(run_dir / f"{product}-pilot-quality.json",
                         score_pilot(rows, gold, encoding, raw_calls))
        if os.environ.get("BENCH_SKIP_SUPPLEMENTAL") != "1":
            run_supplemental(HybridAPI(product, tracking=False), product, run_id, run_dir,
                             questions, bench.read_rows(bench.ROOT / "updates.jsonl"), source_ids, bench, live)


def start_models():
    env = dict(os.environ, HF_HUB_DISABLE_XET="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="2")
    command = [str(root / "model-venv/bin/python"), "-m", "uvicorn", "model_server_hybrid:app",
               "--host", "127.0.0.1", "--port", "18090"]
    model = start("models", command, root, env)
    ready("models", model, live.MODEL_URL + "/health", timeout=600)


def main():
    try:
        start_models()
        for product in sys.argv[1:] or ("contextbase", "weknora"):
            run_product(product)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
