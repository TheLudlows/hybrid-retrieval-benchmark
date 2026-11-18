#!/usr/bin/env bash
# Prepare and run the pinned MIRACL LLM-ingest comparison on a local Linux host.
set -euo pipefail
umask 077

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
data="$repo/miracl_zh/data/miracl-zh-100k"
mode="${1:-smoke}"
python_bin="${BENCH_BOOTSTRAP_PYTHON:-python3}"

die() { printf '%s\n' "$*" >&2; exit 1; }

case "$mode" in
    verify|smoke|full) ;;
    *) die "Usage: bash miracl_zh/local_llm_ingest_rerank.sh [verify|smoke|full]" ;;
esac
[[ $# -le 1 ]] || die "Expected one mode. Set local configuration through environment variables."
command -v "$python_bin" >/dev/null || die "Python 3.11 or later is required."
"$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "Python 3.11 or later is required."
(cd "$data" && sha256sum -c SHA256SUMS)
"$python_bin" "$repo/miracl_zh/bench.py" --data "$data" verify
[[ "$mode" != verify ]] || exit 0

[[ "${BENCH_INGEST_LLM_URL:-}" && "${BENCH_INGEST_LLM_MODEL:-}" ]] \
    || die "Set BENCH_INGEST_LLM_URL to the OpenAI-compatible /v1 endpoint and BENCH_INGEST_LLM_MODEL to its model ID."
for command in git uv go make cc curl flock; do
    command -v "$command" >/dev/null || die "Local prerequisite is missing: $command"
done
[[ -r /sys/fs/cgroup/memory.current ]] || die "Resource sampling requires Linux cgroup v2."
if [[ -z "${BENCH_PG_DSN_FILE:-}" ]]; then
    command -v docker >/dev/null || die "Use local Docker or set BENCH_PG_DSN_FILE to a dedicated PostgreSQL DSN file."
    docker info --format '{{.ServerVersion}}' >/dev/null
fi

runtime="${BENCH_LOCAL_RUNTIME:-${XDG_DATA_HOME:-$HOME/.local/share}/miracl-llm-benchmark}"
runtime="$("$python_bin" -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$runtime")"
case "$runtime/" in
    "$repo/"*) die "Set BENCH_LOCAL_RUNTIME outside the Git checkout." ;;
esac
mkdir -p "$runtime"
exec 9>"$runtime/prepare.lock"
flock -n 9 || die "A benchmark is already preparing or running in $runtime."

contextbase="$runtime/contextbase"
weknora="$runtime/weknora"
model_venv="$runtime/model-venv"
ensure_source() {
    local directory="$1" remote="$2" revision="$3"
    if [[ ! -d "$directory/.git" ]]; then
        [[ ! -e "$directory" ]] || die "Expected an empty location for $directory."
        git init -q "$directory"
        git -C "$directory" remote add origin "$remote"
    fi
    [[ "$(git -C "$directory" remote get-url origin)" == "$remote" ]] \
        || die "Unexpected Git origin in $directory. Use a new BENCH_LOCAL_RUNTIME."
    if ! git -C "$directory" rev-parse --verify HEAD >/dev/null 2>&1; then
        git -C "$directory" fetch --depth=1 origin "$revision"
        git -C "$directory" checkout --detach FETCH_HEAD
    fi
    [[ "$(git -C "$directory" rev-parse HEAD)" == "$revision" ]] \
        || die "Unexpected revision in $directory. Use a new BENCH_LOCAL_RUNTIME."
    [[ -z "$(git -C "$directory" status --porcelain --untracked-files=no)" ]] \
        || die "Tracked source changes in $directory. Use a clean benchmark checkout."
}

ensure_source "$contextbase" https://github.com/RSIXDB/hindsight.git cdf6430e028ff456d490436ff99e50fcf4aba59c
ensure_source "$weknora" https://github.com/Tencent/WeKnora.git cbdcebabf48c5ee2d6868b370326aa8341703f62
UV_PROJECT_ENVIRONMENT="$contextbase/.venv" uv sync --project "$contextbase" \
    --locked --package hindsight-api-slim --no-dev --python 3.11
if [[ ! -x "$weknora/WeKnora-lite" ]]; then
    make -C "$weknora" build-lite SKIP_FRONTEND=1
fi
if [[ ! -x "$model_venv/bin/python" ]]; then
    uv venv --python 3.12 "$model_venv"
fi
uv pip install --python "$model_venv/bin/python" \
    fastapi==0.162.5 uvicorn==0.54.0 onnxruntime==1.31.1 tokenizers==0.23.3 numpy==2.5.4
"$python_bin" "$repo/deployment/fetch_hybrid_models.py"

pg_dsn_file="${BENCH_PG_DSN_FILE:-$runtime/pg-dsn}"
if [[ -z "${BENCH_PG_DSN_FILE:-}" ]]; then
    pg_port="${BENCH_PG_PORT:-15443}"
    [[ "$pg_port" =~ ^[1-9][0-9]{0,4}$ ]] && ((pg_port <= 65535)) || die "Invalid BENCH_PG_PORT."
    owner="$(printf '%s' "$runtime" | sha256sum)"
    owner="${owner%% *}"
    container="miracl-llm-pg-${owner:0:12}"
    if docker container inspect "$container" >/dev/null 2>&1; then
        label="$(docker container inspect --format '{{index .Config.Labels "miracl-benchmark-owner"}}' "$container")"
        [[ "$label" == "$owner" && -f "$pg_dsn_file" ]] \
            || die "The PostgreSQL container and DSN do not belong to this runtime."
        [[ "$(docker container inspect --format '{{(index (index .HostConfig.PortBindings "5432/tcp") 0).HostPort}}' "$container")" == "$pg_port" ]] \
            || die "Set BENCH_PG_PORT to the original runtime port."
        docker start "$container" >/dev/null
    else
        if [[ ! -e "$pg_dsn_file" ]]; then
            "$python_bin" -c 'import secrets,sys; print(f"postgresql://postgres:{secrets.token_hex(24)}@127.0.0.1:{sys.argv[1]}/postgres")' \
                "$pg_port" > "$pg_dsn_file"
        fi
        pg_password="$("$python_bin" -c '
from pathlib import Path
from urllib.parse import urlsplit
import sys

dsn = urlsplit(Path(sys.argv[1]).read_text().strip())
if (dsn.scheme, dsn.hostname, dsn.port, dsn.username) != ("postgresql", "127.0.0.1", int(sys.argv[2]), "postgres") or not dsn.password:
    sys.exit("Use the PostgreSQL port and private DSN created for this runtime")
print(dsn.password)
' "$pg_dsn_file" "$pg_port")"
        POSTGRES_PASSWORD="$pg_password" docker run -d --name "$container" \
            --label "miracl-benchmark-owner=$owner" --shm-size=1g \
            -p "127.0.0.1:$pg_port:5432" -v "$container:/var/lib/postgresql" \
            -e POSTGRES_PASSWORD paradedb/paradedb:0.24.1-pg18 >/dev/null
        unset pg_password
    fi
    for ((attempt=0; attempt<120; attempt++)); do
        if docker exec "$container" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    ((attempt < 120)) || die "PostgreSQL startup timed out. Inspect Docker logs for $container."
fi

args=()
if [[ "$mode" == smoke ]]; then
    args+=(--smoke --batch-size 4)
fi
if [[ "${BENCH_LLM_ARTIFACT_SHA256:-}" ]]; then
    args+=(--llm-sha256 "$BENCH_LLM_ARTIFACT_SHA256")
fi
cd "$repo"
exec "$contextbase/.venv/bin/python" miracl_zh/run_llm_ingest_rerank.py \
    --data "$data" --work "$runtime/run-$mode" --results "$runtime/results-$mode" \
    --pg-admin-dsn-file "$pg_dsn_file" --run-name "zh_llm_$mode" \
    --contextbase-source "$contextbase" --contextbase-python "$contextbase/.venv/bin/python" \
    --weknora-source "$weknora" --weknora-binary "$weknora/WeKnora-lite" \
    --model-python "$model_venv/bin/python" \
    --llm-url "$BENCH_INGEST_LLM_URL" --llm-model "$BENCH_INGEST_LLM_MODEL" \
    --model-port "${BENCH_MODEL_PORT:-18090}" --contextbase-port "${BENCH_CONTEXTBASE_PORT:-18889}" \
    --weknora-port "${BENCH_WEKNORA_PORT:-18081}" "${args[@]}"
