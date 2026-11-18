"""Start dedicated local instances for the LLM import benchmark."""

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
from urllib.parse import urlsplit, urlunsplit

from run_support import save

REPO = Path(__file__).resolve().parents[1]
DEPLOYMENT = REPO / "deployment"
EMBEDDING = "benchmark-bge-m3"
RERANKER = "benchmark-bge-reranker"
CHAT = "benchmark-import-llm"
MODEL_HTTP_KEEPALIVE_S = 180
QUESTION_CONFIG = {"enabled": True, "model_id": CHAT, "question_count": 1}
CONTEXTBASE_CONFIG = {
    "retain_extraction_mode": "concise",
    "enable_text_search": True,
    "enable_reranking": True,
    "enable_graph_retrieval": False,
    "enable_temporal_retrieval": False,
    "recall_budget_function": "fixed",
    "recall_budget_fixed_high": 300,
    "recall_rrf_semantic_weight": 0.985,
    "recall_rrf_bm25_weight": 0.015,
}
WEKNORA_CONFIG = {
    "embedding_top_k": 60,
    "rerank_top_k": 60,
    "vector_threshold": 0,
    "keyword_threshold": 0,
    "rerank_threshold": 0,
    "rerank_model_id": RERANKER,
    "rrf_k": 30,
    "rrf_vector_weight": 0.7,
    "rrf_keyword_weight": 0.3,
}


def free_port(port):
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Port {port} is already in use")


def isolated_database(args):
    import psycopg2
    from psycopg2 import sql

    owner = args.work / "postgres-owner.json"
    database = "miracl_llm_" + args.run_name
    admin_dsn = args.pg_admin_dsn_file.read_text().strip()
    url = urlsplit(admin_dsn)
    if url.scheme not in {"postgres", "postgresql"}:
        raise ValueError("The PostgreSQL admin DSN file must contain a PostgreSQL URL")
    dsn = urlunsplit(url._replace(path="/" + database))
    binding = {"database": database, "host": url.hostname, "port": url.port}
    conn = psycopg2.connect(admin_dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
            exists = cur.fetchone()
            if exists and (
                not owner.exists() or json.loads(owner.read_text()) != binding
            ):
                raise ValueError(
                    "Existing database does not belong to this benchmark work directory"
                )
            if not exists:
                if owner.exists():
                    raise ValueError(
                        "Benchmark database is missing. Restore it or use a new work directory."
                    )
                cur.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
                )
                save(owner, binding)
    finally:
        conn.close()
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            for extension in ("pg_trgm", "btree_gin", "vector", "pg_search"):
                cur.execute(
                    sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                        sql.Identifier(extension)
                    )
                )
    finally:
        conn.close()
    return dsn


def request(url, data=None, token=None, method=None):
    from urllib.request import Request, urlopen

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(
        url, None if data is None else json.dumps(data).encode(), headers, method=method
    )
    with urlopen(req, timeout=15) as response:
        value = json.load(response)
    if value.get("success") is False:
        raise ValueError("Native API reported an unsuccessful operation")
    return value


def spawn(stack, args, name, command, cwd, env, health):
    import time
    from urllib.error import URLError

    output = stack.enter_context((args.work / (name + ".log")).open("a"))
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT
    )

    def stop():
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    stack.callback(stop)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"{name} exited; see {name}.log in the private work directory"
            )
        try:
            request(health)
            return process.pid
        except (URLError, TimeoutError):
            time.sleep(0.5)
    raise TimeoutError(f"{name} is not ready; see its private log")


@contextmanager
def services(args):
    for port in (args.contextbase_port, args.weknora_port, args.model_port):
        free_port(port)
    dsn = isolated_database(args)
    model = f"http://127.0.0.1:{args.model_port}"
    cbase = f"http://127.0.0.1:{args.contextbase_port}"
    wbase = f"http://127.0.0.1:{args.weknora_port}"
    cprofile = args.work / "contextbase"
    wprofile = args.work / "weknora"
    for directory in (cprofile, wprofile):
        directory.mkdir(mode=0o700, exist_ok=True)
    cenv = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CONTEXTBASE_API_", "HINDSIGHT_API_", "CONTEXTDB_"))
    }
    cenv.update(
        {
            "PYTHONPATH": os.pathsep.join(
                str(args.contextbase_source / p)
                for p in (
                    "hindsight-extensions/contextdb-service",
                    "hindsight-api-slim",
                )
            ),
            "CONTEXTDB_DEPLOY_DIR": str(cprofile),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "2",
        }
    )
    native = {
        "HOST": "127.0.0.1",
        "DATABASE_URL": dsn,
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": args.llm_model,
        "LLM_BASE_URL": model + "/contextbase/v1",
        "LLM_API_KEY": "local-benchmark",
        "LLM_MAX_CONCURRENT": "1",
        "LLM_MAX_RETRIES": "1",
        "LLM_TIMEOUT": "600",
        "SKIP_LLM_VERIFICATION": "true",
        "RETAIN_MAX_COMPLETION_TOKENS": "2048",
        "RETAIN_CHUNK_SIZE": "512",
        "STORE_DOCUMENT_TEXT": "true",
        "ENABLE_OBSERVATIONS": "false",
        "ENABLE_AUTO_CONSOLIDATION": "false",
        "EMBEDDINGS_PROVIDER": "openai",
        "EMBEDDINGS_OPENAI_MODEL": "bge-m3",
        "EMBEDDINGS_OPENAI_DIMENSIONS": "1024",
        "EMBEDDINGS_OPENAI_API_KEY": "local-benchmark",
        "EMBEDDINGS_OPENAI_BASE_URL": model + "/contextbase/v1",
        "TEXT_SEARCH_EXTENSION": "pg_search",
        "TEXT_SEARCH_EXTENSION_PG_SEARCH_TOKENIZER": "jieba",
        "RECALL_MAX_CANDIDATES_PER_SOURCE": "100",
        "RECALL_STRATEGY_BOOSTS": "",
        "RERANKER_PROVIDER": "tei",
        "RERANKER_TEI_URL": model + "/contextbase/tei",
        "RERANKER_MAX_CANDIDATES": "60",
        "RERANKER_MAX_CANDIDATES_HIGH": "60",
        "RERANKER_TEI_HTTP_TIMEOUT": "180",
        "RERANKER_TEI_MAX_CONCURRENT": "8",
        **{k.upper(): str(v).lower() for k, v in CONTEXTBASE_CONFIG.items()},
    }
    for prefix in ("CONTEXTBASE_API_", "HINDSIGHT_API_"):
        cenv.update({prefix + k: v for k, v in native.items()})
    auth_path = wprofile / "credentials.json"
    if not auth_path.exists():
        save(
            auth_path,
            {
                "password": "Aa1" + secrets.token_urlsafe(15),
                "jwt": secrets.token_hex(32),
                "aes": secrets.token_hex(16),
            },
        )
        auth_path.chmod(0o600)
    auth = json.loads(auth_path.read_text())

    def model_definition(identifier, kind, name, base_url, provider, extra):
        return {
            "id": identifier,
            "type": kind,
            "name": name,
            "source": "remote",
            "is_default": True,
            "parameters": {
                "provider": provider,
                "base_url": base_url,
                "api_key": "local-benchmark",
                **extra,
            },
        }

    models = [
        model_definition(
            EMBEDDING,
            "Embedding",
            "bge-m3",
            model + "/weknora/v1",
            "openai",
            {"embedding_parameters": {"dimension": 1024}},
        ),
        model_definition(
            CHAT,
            "KnowledgeQA",
            args.llm_model,
            model + "/weknora/v1",
            "openai",
            {"context_window": 8192},
        ),
        model_definition(
            RERANKER,
            "Rerank",
            "BAAI/bge-reranker-v2-m3",
            model + "/weknora/v1/rerank",
            "cohere",
            {"spec": {"compat": {"request_timeout_seconds": 180}}},
        ),
    ]
    model_config = wprofile / "builtin-models.yaml"
    # JSON is a YAML subset and keeps model names properly escaped.
    save(model_config, {"builtin_models": models})
    wenv = os.environ.copy()
    wenv.pop("REDIS_ADDR", None)
    wenv.update(
        DB_DRIVER="sqlite",
        DB_PATH=str(wprofile / "weknora.db"),
        RETRIEVE_DRIVER="sqlite",
        STORAGE_TYPE="local",
        LOCAL_STORAGE_BASE_DIR=str(wprofile / "files"),
        STREAM_MANAGER_TYPE="memory",
        JWT_SECRET=auth["jwt"],
        SYSTEM_AES_KEY=auth["aes"],
        BUILTIN_MODELS_CONFIG=str(model_config),
        WEKNORA_SANDBOX_MODE="disabled",
        SSRF_WHITELIST="127.0.0.1",
        SERVER_HOST="127.0.0.1",
        SERVER_PORT=str(args.weknora_port),
        GIN_MODE="release",
        LOG_LEVEL="warn",
        AUTO_MIGRATE="true",
        NEO4J_ENABLE="false",
        ENABLE_GRAPH_RAG="false",
        DISABLE_REGISTRATION="false",
        GENERATE_QUESTIONS_MAX_TOKENS="256",
    )
    menv = os.environ.copy()
    menv.update(
        BENCH_INGEST_LLM_MODEL=args.llm_model,
        BENCH_INGEST_LLM_URL=args.llm_url,
        BENCH_MEASUREMENT_DIR=str(args.work),
        BENCH_EMBEDDING_BATCH_WINDOW_MS="0",
        BENCH_ONNX_THREADS=str(args.onnx_threads),
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="2",
    )
    os.environ["BENCH_HTTP_TIMEOUT_SECONDS"] = str(args.query_timeout)
    with ExitStack() as stack:
        roots = {}
        roots["shared_bge"] = spawn(
            stack,
            args,
            "models",
            [
                str(args.model_python),
                "-m",
                "uvicorn",
                "model_server_ingest_rerank:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.model_port),
                "--timeout-keep-alive",
                str(MODEL_HTTP_KEEPALIVE_S),
            ],
            DEPLOYMENT,
            menv,
            model + "/health",
        )
        roots["contextbase_api"] = spawn(
            stack,
            args,
            "contextbase",
            [
                str(args.contextbase_python),
                "-m",
                "hindsight_ext_contextdb.local",
                "--component",
                "api",
                "--port",
                str(args.contextbase_port),
            ],
            args.contextbase_source,
            cenv,
            cbase + "/health/ready",
        )
        roots["postgres_instance"] = 0
        roots["weknora_api"] = spawn(
            stack,
            args,
            "weknora",
            [str(args.weknora_binary)],
            args.weknora_source,
            wenv,
            wbase + "/health",
        )
        ctoken = json.loads((cprofile / "admin-key.json").read_text())["secret"]
        bank = "miracl_llm_" + args.run_name
        settings = args.work / "scope.json"
        scope = json.loads(settings.read_text()) if settings.exists() else {}
        if "bank" not in scope:
            request(f"{cbase}/v1/default/banks/{bank}", {"name": bank}, ctoken, "PUT")
            scope["bank"] = bank
            save(settings, scope)
        config_url = f"{cbase}/v1/default/banks/{bank}/config"
        request(config_url, {"updates": CONTEXTBASE_CONFIG}, ctoken, "PATCH")
        cread = request(config_url, token=ctoken)["config"]
        if not all(cread.get(k) == v for k, v in CONTEXTBASE_CONFIG.items()):
            raise ValueError("ContextBase config readback does not match")
        login = {"email": "benchmark@weknora.test", "password": auth["password"]}
        from urllib.error import HTTPError

        try:
            session = request(wbase + "/api/v1/auth/login", login)
        except HTTPError as error:
            if error.code != 401:
                raise
            request(wbase + "/api/v1/auth/register", {"username": "Benchmark", **login})
            session = request(wbase + "/api/v1/auth/login", login)
        wtoken = session["token"]
        if "kb" not in scope:
            kb = request(
                wbase + "/api/v1/knowledge-bases",
                {
                    "name": bank,
                    "type": "document",
                    "embedding_model_id": EMBEDDING,
                    "chunking_config": {"chunk_size": 512, "chunk_overlap": 0},
                    "question_generation_config": QUESTION_CONFIG,
                    "profile_config": {"enabled": False},
                },
                wtoken,
            )["data"]["id"]
            scope["kb"] = kb
            save(settings, scope)
        kb = scope["kb"]
        wread = request(f"{wbase}/api/v1/knowledge-bases/{kb}", token=wtoken)["data"]
        if not all(
            wread["question_generation_config"].get(k) == v
            for k, v in QUESTION_CONFIG.items()
        ):
            raise ValueError(
                "WeKnora question generation config readback does not match"
            )
        readback = request(
            wbase + "/api/v1/tenants/kv/retrieval-config", WEKNORA_CONFIG, wtoken, "PUT"
        )["data"]
        if readback != WEKNORA_CONFIG:
            raise ValueError("WeKnora retrieval config readback does not match")
        if "weknora_key" not in scope:
            tenant = session["active_tenant"]["id"]
            scope["weknora_key"] = request(
                f"{wbase}/api/v1/tenants/{tenant}/api-keys",
                {"name": bank, "full_access": True},
                wtoken,
            )["data"]["token"]
            save(settings, scope)
            settings.chmod(0o600)
        os.environ.update(
            CONTEXTBASE_URL=cbase,
            CONTEXTBASE_BANK_ID=bank,
            CONTEXTBASE_API_KEY=ctoken,
            WEKNORA_URL=wbase,
            WEKNORA_KB_ID=kb,
            WEKNORA_API_KEY=scope["weknora_key"],
        )
        yield {
            "postgres_dsn": dsn,
            "weknora_db": wprofile / "weknora.db",
            "roots": roots,
            "models": request(model + "/hybrid/info"),
            "readback": {
                "contextbase": CONTEXTBASE_CONFIG,
                "weknora": {
                    "question_generation": QUESTION_CONFIG,
                    "retrieval": readback,
                },
            },
        }
