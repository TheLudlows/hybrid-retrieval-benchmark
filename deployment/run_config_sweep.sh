#!/usr/bin/env bash
set -euo pipefail

deployment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export BENCH_DATASET_DIR=${BENCH_DATASET_DIR:-"$deployment_dir/../taurusdb-pilot-corpus"}
export BENCH_SKIP_SUPPLEMENTAL=1
export TIKTOKEN_CACHE_DIR=${TIKTOKEN_CACHE_DIR:-/tmp/data-gym-cache}
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1 OTEL_SDK_DISABLED=true LITELLM_LOCAL_MODEL_COST_MAP=True
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1

python_bin="$deployment_dir/model-venv/bin/python"
if [[ ${BENCH_SWEEP_ISOLATED:-} != 1 ]]; then
    export BENCH_SWEEP_ISOLATED=1
    exec unshare --user --map-root-user --net "$python_bin" "$deployment_dir/run_loopback_only.py" "$(id -u)" "$(id -g)" bash "$0" "$@"
fi

cd "$deployment_dir"
updates_only=0
if [[ ${1:-} == --updates ]]; then
    shift
    updates_only=1
    if [[ $# == 0 || -z ${BENCH_SWEEP_OUTPUT:-} ]]; then
        echo "Updates need BENCH_SWEEP_OUTPUT and an indexed sweep case" >&2
        exit 2
    fi
elif [[ $# == 0 ]]; then
    set -- contextbase-60 contextbase-40 contextbase-30 weknora
fi
sweep_dir="${BENCH_SWEEP_OUTPUT:-"$deployment_dir/results/config-sweep-$(date -u +%Y%m%dT%H%M%SZ)"}"
mkdir -p "$sweep_dir"
for case_name in "$@"; do
    if (( updates_only )); then
        BENCH_LOAD_CONCURRENCY="${BENCH_LOAD_CONCURRENCY-}" "$python_bin" -u replay_config_updates.py "$sweep_dir" "$case_name" |
            tee "$sweep_dir/$case_name-updates.log"
        continue
    fi
    case "$case_name" in
        contextbase-60)
            product=contextbase
            export BENCH_RERANK_HIGH=0
            ;;
        contextbase-40|contextbase-30|contextbase-50)
            product=contextbase
            export BENCH_RERANK_HIGH="${case_name##*-}"
            ;;
        weknora)
            product=weknora
            ;;
        *)
            echo "Unknown benchmark case $case_name" >&2
            exit 2
            ;;
    esac
    export BENCH_CONTEXTBASE_STATE="$sweep_dir/state/$case_name"
    echo "CASE $case_name"
    "$python_bin" -u run_full_hybrid.py "$product" | tee "$sweep_dir/$case_name.log"
done
