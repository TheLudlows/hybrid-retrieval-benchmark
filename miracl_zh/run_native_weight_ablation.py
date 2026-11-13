"""Fixed native weight/semantic-only ablations on the retained isolated 100k index.

Reuses bench.capture, official-ID scoring, model-call archival and durable JSON
checkpoints. No code changes, index rebuilding, model reranking or original-bank
writes. --publish explicitly opts into archival commits on the benchmark branch.
"""

import argparse
from argparse import Namespace
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

import bench
from audit_index import audit
from run_scale import Worker, save

SOURCE_COMMIT = "cdf6430e028ff456d490436ff99e50fcf4aba59c"
FLAGS = {
    "enable_text_search": True,
    "enable_graph_retrieval": False,
    "enable_temporal_retrieval": False,
    "enable_reranking": False,
}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def overrides(case):
    return {
        **FLAGS,
        "enable_text_search": case["text_search"],
        "recall_rrf_semantic_weight": case["semantic_weight"],
        "recall_rrf_bm25_weight": case["bm25_weight"],
        "recall_rrf_graph_weight": 1.0,
        "recall_rrf_temporal_weight": 1.0,
    }


def render(out, plan):
    lines = [
        "# 原生权重与纯向量追加对照",
        "",
        "只展示已完成落盘的配置。四个方案在运行前冻结；全部393题已被历史实验使用，不称为盲测。本轮仅调整独立验证Bank配置，不改源码或原库。纯向量是消融，不替代混合检索主成绩。",
        "",
        "| 配置 | 查询数 | Hit@1 | Hit@5 | Hit@10 | nDCG@10 | Recall@10 | Recall@100 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    completed = {}
    for case in plan["cases"]:
        folder = out / case["name"]
        path = folder / "metrics.json"
        if not path.exists():
            continue
        metric = json.loads(path.read_text())
        completed[case["name"]] = metric
        lines.append(
            f"| [{case['label']}]({case['name']}/metrics.json) | {metric['queries']} | {metric['hit_at_1']:.2%} | {metric['hit_at_5']:.2%} | {metric['hit_at_10']:.2%} | {metric['ndcg_at_10']:.6f} | {metric['recall_at_10']:.2%} | {metric['recall_at_100']:.2%} |"
        )
    lines += [
        "",
        "| 配置 | p50 ms | p95 ms | p99 ms | 平均返回深度 | 查询Embedding次数/Token | 重排次数/Token |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for case in plan["cases"]:
        if case["name"] not in completed:
            continue
        metric = completed[case["name"]]
        usage = json.loads((out / case["name"] / "model-usage.json").read_text())
        query = usage.get("query/embedding", {})
        lines.append(
            f"| {case['label']} | {metric['serial_p50_ms']:.2f} | {metric['serial_p95_ms']:.2f} | {metric['serial_p99_ms']:.2f} | {metric['returned_depth']['mean']:.2f} | {query.get('calls', 0)}/{query.get('input_tokens', 0)} | 0/0 |"
        )
    lines += [
        "",
        "共同条件：相同10万段语料、相同BGE-M3，Recall high=1000、每源入融合上限100、候选上限60、RRF k=60、返回8192 Token、并发1，每组4次相同预热不计入指标。图/时间支路及模型重排关闭。纯向量通过enable_text_search=false原生开关实现，不使用非法的零权重。",
        "",
        "逐题排名按官方段落ID计分并fsync；配置指纹、原生读回、有效性trace、实际模型调用均归档。Recall@100按实际不足100条的返回计分。不同配置按注册顺序串行运行，耗时仅描述本窗口，不作普适性能保证。",
        "",
        "原始两端数据库、索引、检查点及备份全部保留。本轮结束后恢复独立Bank的0.97/0.03并停止验证API；原服务未切换新代码。WeKnora本轮未重跑，不能据此重写此前等预算产品对比。",
        "",
        "执行方案见[experiment-plan.json](experiment-plan.json)，当前进度见[execution-status.json](execution-status.json)。",
        "",
    ]
    (out / "REPORT.md").write_text("\n".join(lines))
    return completed


def publish(repo, out, message, enabled):
    if not enabled:
        return
    assert (
        subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=repo, text=True
        ).strip()
        == "miracl-zh-benchmark"
    )
    relative = str(out.relative_to(repo))
    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"], cwd=repo, text=True
    ).splitlines()
    if any(not name.startswith(relative + "/") for name in staged):
        save(out / "publish-status.json", {"state": "blocked_external_staging"})
        return
    subprocess.run(["git", "add", relative], cwd=repo, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
        return
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repo, check=True)
    pushed = subprocess.run(["git", "push", "origin", "miracl-zh-benchmark"], cwd=repo)
    if pushed.returncode:
        save(
            out / "publish-status.json",
            {
                "state": "push_pending",
                "recovery": "git push origin miracl-zh-benchmark",
            },
        )


def main(args):
    repo = Path(__file__).resolve().parents[1]
    out = args.results.resolve()
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "experiment-plan.json"
    plan = json.loads(plan_path.read_text())
    assert sha(Path(__file__)) == plan["driver_sha256"], "Frozen driver changed"
    settings = json.loads(args.settings.read_text())
    work = args.work.resolve()
    profile = work / "runtime"
    source = args.source.resolve()
    assert (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
        == SOURCE_COMMIT
    )
    assert not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=source
    ).strip()
    proof = json.loads((work / "restored.json").read_text())
    assert proof["database"] == "miracl_rrf_config_validation_20261113"
    url = (work / "database-url").read_text()
    assert url.split("?", 1)[0].endswith("/" + proof["database"])
    data = Path(settings["data100k"])
    dataset = bench.dataset_source(data)
    topics, qrels = bench.topics_and_qrels(data)
    assert dataset["subset"]["file"]["sha256"] == plan["corpus_sha256"]
    assert (
        hashlib.sha256(json.dumps(list(topics)).encode()).hexdigest()
        == plan["query_order_sha256"]
    )
    bench.check_fingerprint(
        data / bench.SUBSET_SHARD, dataset["files"][bench.SUBSET_SHARD]
    )
    lock = (Path(settings["work"]) / "worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    model = json.loads((repo / plan["model_fingerprints"]).read_text())
    for name, entry in model["files"].items():
        assert sha(repo / "deployment/models" / name) == entry["sha256"]
    save(out / "model-fingerprints.json", model)
    with urlopen("http://127.0.0.1:18090/hybrid/info", timeout=10) as response:
        info = json.load(response)
    assert info["embedding_intra_op_threads"] == 3 and info["inter_op_threads"] == 1
    assert info["embedding_truncation"]["max_length"] == 512
    save(out / "model-info.json", info)
    conn = json.loads(
        (
            Path(settings["work"]) / "runtime-v2/contextbase-100k/connection.json"
        ).read_text()
    )
    key = json.loads((profile / "admin-key.json").read_text())["secret"]
    os.environ.update(
        CONTEXTBASE_URL="http://127.0.0.1:18889",
        CONTEXTBASE_API_KEY=key,
        CONTEXTBASE_BANK_ID=conn["CONTEXTBASE_BANK_ID"],
    )
    api = bench.prior_api("contextbase")
    checkpoint = profile / "ingest.sqlite"
    os.environ["AUDIT_POSTGRES_URL"] = url
    audit(
        Namespace(
            data=data,
            checkpoint=checkpoint,
            product="contextbase",
            weknora_db=None,
            output=out / "index-audit.json",
        )
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CONTEXTBASE_API_", "HINDSIGHT_API_"))
    }
    for line in (profile / "api.env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            words = shlex.split(value)
            env[name] = words[0] if words else ""
    controlled = {
        "DATABASE_URL": url,
        "PORT": "18889",
        "RECALL_MAX_CANDIDATES_PER_SOURCE": "100",
        "RERANKER_MAX_CANDIDATES": "60",
        "RERANKER_MAX_CANDIDATES_HIGH": "60",
        "RECALL_BUDGET_FUNCTION": "fixed",
        "RECALL_BUDGET_FIXED_HIGH": "1000",
        "RECALL_STRATEGY_BOOSTS": "",
        "ENABLE_RERANKING": "false",
        "ENABLE_GRAPH_RETRIEVAL": "false",
        "ENABLE_TEMPORAL_RETRIEVAL": "false",
    }
    for prefix in ("CONTEXTBASE_API_", "HINDSIGHT_API_"):
        env.update({prefix + name: value for name, value in controlled.items()})
    env.update(
        PYTHONPATH=str(source / "hindsight-extensions/contextdb-service")
        + ":"
        + str(source / "hindsight-api-slim"),
        CONTEXTDB_DEPLOY_DIR=str(profile),
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="2",
    )
    measurement = Path(settings["measurement_dir"])
    old_context = (measurement / "measurement-context.json").read_bytes()
    baseline = overrides(plan["cases"][0])
    child = None
    original = None
    failure = None
    restored = False
    completed = []
    log = (profile / "native-weight-ablation.log").open("a")
    try:
        try:
            with urlopen(api.base + "/health/ready", timeout=2):
                raise RuntimeError(
                    "Port 18889 is occupied; do not stop another process"
                )
        except URLError:
            pass
        child = subprocess.Popen(
            [
                str(args.python),
                "-m",
                "hindsight_ext_contextdb.local",
                "--component",
                "api",
                "--port",
                "18889",
            ],
            cwd=source,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 180
        while True:
            if child.poll() is not None:
                raise RuntimeError("Validation API startup failed; inspect private log")
            try:
                with urlopen(api.base + "/health/ready", timeout=5) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Validation API readiness timed out")
            time.sleep(1)
        readback = api.request("/config")["config"]
        original_path = out / "bank-original-config.json"
        if original_path.exists():
            original = json.loads(original_path.read_text())
            assert original == baseline
            assert any(
                all(readback[name] == value for name, value in overrides(case).items())
                for case in plan["cases"]
            ), "Unregistered bank changes found"
        else:
            assert all(readback[name] == value for name, value in baseline.items()), (
                "Expected retained 0.97/0.03 baseline"
            )
            original = baseline
            save(original_path, original)
        for case in plan["cases"]:
            folder = out / case["name"]
            folder.mkdir(exist_ok=True)
            config = {
                "case": case,
                "plan_sha256": sha(plan_path),
                "corpus_sha256": plan["corpus_sha256"],
                "source_commit": SOURCE_COMMIT,
                "candidate_ceiling": 60,
                "max_tokens": 8192,
                "high_budget": 1000,
                "source_cap": 100,
                "query_order_sha256": plan["query_order_sha256"],
                "model_reranking": False,
                "warmup_queries": 4,
                "concurrency": 1,
            }
            config_path = folder / "config.json"
            if config_path.exists():
                assert json.loads(config_path.read_text()) == config
            else:
                save(config_path, config)
            if (folder / "metrics.json").exists():
                completed.append(case["name"])
                print("Reused completed case", case["name"], flush=True)
                continue
            api.request("/config", {"updates": overrides(case)}, method="PATCH")
            native = api.request("/config")["config"]
            assert all(native[name] == value for name, value in overrides(case).items())
            save(
                folder / "bank-config-readback.json",
                {name: native[name] for name in baseline},
            )
            run_id = plan["run_prefix"] + case["name"]
            qid = "4714933#0"
            save(
                measurement / "measurement-context.json",
                {"contextbase": {"phase": "probe", "id": qid, "run_id": run_id}},
            )
            payload = api.request(
                "/memories/recall",
                {
                    "query": topics[qid],
                    "budget": "high",
                    "types": ["world", "experience"],
                    "trace": True,
                    "max_tokens": 8192,
                    "include": {"entities": None, "chunks": {"max_tokens": 16384}},
                },
            )
            with gzip.open(folder / "native-probe.json.gz", "wt") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            trace = payload["trace"]
            expected = {
                "semantic": case["semantic_weight"],
                "bm25": case["bm25_weight"],
                "graph": 1.0,
                "temporal": 1.0,
            }
            phase = {
                row["phase_name"]: row["details"]
                for row in trace["summary"]["phase_metrics"]
            }
            assert phase["rrf_merge"]["rrf_weights"] == expected
            assert phase["reranking"]["reranker_type"] == "rrf-passthrough"
            keyword_hits = any(
                arm["method_name"] == "bm25" and arm["results"]
                for arm in trace["retrieval_results"]
            )
            assert keyword_hits == case["text_search"]
            for row in trace["rrf_merged"]:
                value = sum(
                    expected[name.removesuffix("_rank")] / (60 + rank)
                    for name, rank in row["source_ranks"].items()
                )
                assert abs(value - row["rrf_score"]) < 1e-12
            vector_hash = hashlib.sha256(
                json.dumps(trace["query"]["query_embedding"]).encode()
            ).hexdigest()
            assert vector_hash == plan["probe_embedding_sha256"]
            save(
                folder / "probe-verification.json",
                {
                    "qid": qid,
                    "rrf_entries_verified": len(trace["rrf_merged"]),
                    "bm25_has_results": keyword_hits,
                    "expected_text_search": case["text_search"],
                    "query_embedding_sha256": vector_hash,
                    "native_passthrough_candidates": phase["reranking"][
                        "candidates_reranked"
                    ],
                    "model_rerank_calls_expected": 0,
                },
            )
            save(
                out / "execution-status.json",
                {
                    "state": "querying",
                    "case": case["name"],
                    "target_queries": 393,
                    "completed_cases": completed,
                },
            )
            run = folder / "rankings.jsonl"
            bench.capture(
                Namespace(
                    product="contextbase",
                    data=data,
                    run=run,
                    passage_map=checkpoint,
                    limit=60,
                    reranker="benchmark-bge-reranker",
                    resume=run.exists(),
                    warmup=4,
                    max_queries=None,
                    smoke_index=False,
                    config_sha256=sha(config_path),
                    measurement_dir=measurement,
                    run_id=run_id,
                    max_tokens=8192,
                    disable_rerank=True,
                ),
                topics,
                dataset,
            )
            metric = bench.score(run, qrels, None, dataset)
            assert metric["queries"] == 393 and metric["returned_depth"]["max"] <= 60
            usage = Worker.archive_calls(
                Namespace(measure=measurement),
                "contextbase",
                run_id,
                folder / "model-calls.jsonl.gz",
            )
            assert not any(
                value.get("calls", 0)
                for name, value in usage.items()
                if name.endswith("/rerank")
            )
            calls = list(
                map(
                    json.loads,
                    gzip.open(folder / "model-calls.jsonl.gz", "rt")
                    .read()
                    .splitlines(),
                )
            )
            assert {
                row["id"]
                for row in calls
                if row["phase"] == "query" and row["kind"] == "embedding"
            } == set(topics)
            save(folder / "model-usage.json", usage)
            # metrics.json is the completion marker, written after all other artifacts.
            save(folder / "metrics.json", metric)
            completed.append(case["name"])
            render(out, plan)
            save(
                out / "execution-status.json",
                {
                    "state": "case_complete",
                    "case": case["name"],
                    "queries": 393,
                    "completed_cases": completed,
                },
            )
            publish(
                repo,
                out,
                "Archive native weight ablation " + case["name"] + " dev393",
                args.publish,
            )
            print(
                "CASE_COMPLETE",
                case["name"],
                metric["hit_at_1"],
                metric["ndcg_at_10"],
                flush=True,
            )
    except Exception as error:
        failure = error
        save(
            out / "execution-status.json",
            {
                "state": "failed",
                "completed_cases": completed,
                "error_type": type(error).__name__,
                "message": str(error),
                "recovery": "Rerun the identical command; completed cases and per-query checkpoints are reused.",
            },
        )
    finally:
        if original and child and child.poll() is None:
            try:
                api.request("/config", {"updates": original}, method="PATCH")
                native = api.request("/config")["config"]
                assert all(native[name] == value for name, value in original.items())
                save(
                    out / "restored-bank-config.json",
                    {name: native[name] for name in original},
                )
                restored = True
            except Exception as error:
                save(
                    out / "restoration-error.json",
                    {"type": type(error).__name__, "message": str(error)},
                )
                if failure is None:
                    failure = error
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        log.close()
        (measurement / "measurement-context.json").write_bytes(old_context)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    render(out, plan)
    save(
        out / "execution-status.json",
        {
            "state": "complete" if failure is None and restored else "failed",
            "completed_cases": completed,
            "restored_baseline97": restored,
            "original_service_untouched": True,
            "all_product_data_retained": True,
            "validation_api_stopped": True,
        },
    )
    publish(
        repo,
        out,
        "Complete fixed native weight and semantic-only ablations",
        args.publish,
    )
    if failure is not None:
        raise failure


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    main(parser.parse_args())
