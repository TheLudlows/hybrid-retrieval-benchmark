"""Bounded native hybrid optimization: tune100, freeze, then dev393/train200.

Keeps the source, models, corpus and 60-candidate ceiling fixed. Changes only
positive RRF weights and the hierarchical high-budget setting in an isolated bank.
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
from run_native_weight_ablation import FLAGS, SOURCE_COMMIT, publish, sha

QUALITY = (
    "hit_at_1",
    "hit_at_5",
    "hit_at_10",
    "ndcg_at_10",
    "recall_at_10",
    "recall_at_100",
)


def overrides(case):
    return {
        **FLAGS,
        "recall_rrf_semantic_weight": case["semantic_weight"],
        "recall_rrf_bm25_weight": case["bm25_weight"],
        "recall_rrf_graph_weight": 1.0,
        "recall_rrf_temporal_weight": 1.0,
        "recall_budget_function": "fixed",
        "recall_budget_fixed_high": case["high_budget"],
    }


def select_cases(cases, metrics):
    quality = max(
        range(len(cases)),
        key=lambda i: (
            metrics[cases[i]["name"]]["ndcg_at_10"],
            metrics[cases[i]["name"]]["hit_at_1"],
            -i,
        ),
    )
    best = metrics[cases[quality]["name"]]
    eligible = [
        i
        for i, case in enumerate(cases)
        if all(metrics[case["name"]][key] >= best[key] - 1e-12 for key in QUALITY)
    ]
    efficient = min(
        eligible,
        key=lambda i: (
            metrics[cases[i]["name"]]["serial_p95_ms"],
            metrics[cases[i]["name"]]["serial_p50_ms"],
            i,
        ),
    )
    return {
        "quality_case": cases[quality]["name"],
        "efficiency_case": cases[efficient]["name"],
        "assessment_cases": list(
            dict.fromkeys(
                [cases[0]["name"], cases[quality]["name"], cases[efficient]["name"]]
            )
        ),
        "efficiency_eligible_cases": [cases[i]["name"] for i in eligible],
    }


def render(out, plan):
    lines = [
        "# ContextBase 原生混合检索有界优化",
        "",
        "固定9组：三种语义权重0.98/0.985/0.99 × 三档high预算1000/500/300。全部保持向量+全文开启、关闭模型重排，候选上限60。先用既有100题调参集筛选，再冻结候选后复核393题与额外200题。所有这些查询此前已使用过，结果属于探索性，不称为盲测。",
        "",
    ]
    for split in ("tune100", "dev393", "fresh_train200"):
        lines += [
            "## " + split,
            "",
            "| 配置 | 题数 | Hit@1 | Hit@5 | Hit@10 | nDCG@10 | Recall@10 | Recall@100 | p50 ms | p95 ms | p99 ms |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for case in plan["cases"]:
            path = out / split / case["name"] / "metrics.json"
            if not path.exists():
                continue
            m = json.loads(path.read_text())
            label = f"{case['semantic_weight']}/{case['bm25_weight']}，预算{case['high_budget']}"
            lines.append(
                f"| [{label}]({split}/{case['name']}/metrics.json) | {m['queries']} | {m['hit_at_1']:.2%} | {m['hit_at_5']:.2%} | {m['hit_at_10']:.2%} | {m['ndcg_at_10']:.6f} | {m['recall_at_10']:.2%} | {m['recall_at_100']:.2%} | {m['serial_p50_ms']:.2f} | {m['serial_p95_ms']:.2f} | {m['serial_p99_ms']:.2f} |"
            )
        lines.append("")
    lines += [
        "固定条件：同一10万段、同版本BGE-M3，RRF k=60、每源入融合上限100、候选上限60、返回8192 Token、串行并发1、每组4次相同预热不计分。图/时间支路和模型重排关闭，保留原生混合/综合评分/Token过滤流程。",
        "",
        "质量候选仅按tune100的nDCG@10、Hit@1及注册顺序冻结；效率候选只在六项质量均不低于质量候选的配置中按p95/p50选取。393题和额外200题不用于再次扩展参数。不能保证在全部参数、数据集或硬件上最优。",
        "",
        "原服务和两端原库不修改，原索引/检查点/备份全部保留；结束后独立Bank恢复0.97/0.03、high=1000，并停止验证API。未重新入库；实际模型用量与各配置逐题结果一起保存。WeKnora本轮不重跑，不能改写此前同等调参预算的产品比较。",
        "",
        "见[预注册方案](experiment-plan.json)、[冻结选择](frozen-selection.json)和[运行状态](execution-status.json)。尚未完成的阶段没有成绩，缺失文件不代表零分。",
        "",
    ]
    (out / "REPORT.md").write_text("\n".join(lines))


class Session:
    def __init__(self, args, plan):
        self.args = args
        self.plan = plan
        self.repo = Path(__file__).resolve().parents[1]
        self.out = args.results.resolve()
        self.work = args.work.resolve()
        self.profile = self.work / "runtime"
        self.source = args.source.resolve()
        self.s = json.loads(args.settings.read_text())
        self.data = Path(self.s["data100k"])
        self.dataset = bench.dataset_source(self.data)
        self.topics, self.qrels = bench.topics_and_qrels(self.data)
        self.measure = Path(self.s["measurement_dir"])
        self.child = None
        self.lock = None
        self.log = None
        self.old_context = None
        self.original = None
        self.completed = []

    def start(self):
        assert sha(Path(__file__)) == self.plan["driver_sha256"]
        assert (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.source, text=True
            ).strip()
            == SOURCE_COMMIT
        )
        assert not subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=self.source
        ).strip()
        assert self.dataset["subset"]["file"]["sha256"] == self.plan["corpus_sha256"]
        assert (
            hashlib.sha256(json.dumps(list(self.topics)).encode()).hexdigest()
            == self.plan["query_order_sha256"]
        )
        bench.check_fingerprint(
            self.data / bench.SUBSET_SHARD, self.dataset["files"][bench.SUBSET_SHARD]
        )
        self.url = (self.work / "database-url").read_text()
        proof = json.loads((self.work / "restored.json").read_text())
        assert proof[
            "database"
        ] == "miracl_rrf_config_validation_20261113" and self.url.split("?", 1)[
            0
        ].endswith("/" + proof["database"])
        self.lock = (Path(self.s["work"]) / "worker.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        model = json.loads((self.repo / self.plan["model_fingerprints"]).read_text())
        for name, entry in model["files"].items():
            assert sha(self.repo / "deployment/models" / name) == entry["sha256"]
        save(self.out / "model-fingerprints.json", model)
        with urlopen("http://127.0.0.1:18090/hybrid/info", timeout=10) as response:
            info = json.load(response)
        assert info == json.loads(
            (self.repo / self.plan["model_info_reference"]).read_text()
        )
        save(self.out / "model-info.json", info)
        conn = json.loads(
            (
                Path(self.s["work"]) / "runtime-v2/contextbase-100k/connection.json"
            ).read_text()
        )
        key = json.loads((self.profile / "admin-key.json").read_text())["secret"]
        os.environ.update(
            CONTEXTBASE_URL="http://127.0.0.1:18889",
            CONTEXTBASE_API_KEY=key,
            CONTEXTBASE_BANK_ID=conn["CONTEXTBASE_BANK_ID"],
        )
        self.api = bench.prior_api("contextbase")
        self.checkpoint = self.profile / "ingest.sqlite"
        os.environ["AUDIT_POSTGRES_URL"] = self.url
        audit(
            Namespace(
                data=self.data,
                checkpoint=self.checkpoint,
                product="contextbase",
                weknora_db=None,
                output=self.out / "index-audit.json",
            )
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CONTEXTBASE_API_", "HINDSIGHT_API_"))
        }
        for line in (self.profile / "api.env").read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                words = shlex.split(value)
                env[key] = words[0] if words else ""
        fixed = {
            "DATABASE_URL": self.url,
            "PORT": "18889",
            "RECALL_MAX_CANDIDATES_PER_SOURCE": "100",
            "RERANKER_MAX_CANDIDATES": "60",
            "RERANKER_MAX_CANDIDATES_HIGH": "60",
            "RECALL_BUDGET_FUNCTION": "fixed",
            "RECALL_BUDGET_FIXED_HIGH": "1000",
            "RECALL_STRATEGY_BOOSTS": "",
            "ENABLE_GRAPH_RETRIEVAL": "false",
            "ENABLE_TEMPORAL_RETRIEVAL": "false",
            "ENABLE_RERANKING": "false",
        }
        for prefix in ("CONTEXTBASE_API_", "HINDSIGHT_API_"):
            env.update({prefix + k: v for k, v in fixed.items()})
        env.update(
            PYTHONPATH=str(self.source / "hindsight-extensions/contextdb-service")
            + ":"
            + str(self.source / "hindsight-api-slim"),
            CONTEXTDB_DEPLOY_DIR=str(self.profile),
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="2",
        )
        self.old_context = (self.measure / "measurement-context.json").read_bytes()
        try:
            with urlopen(self.api.base + "/health/ready", timeout=2):
                raise RuntimeError(
                    "Validation port occupied; do not stop another process"
                )
        except URLError:
            pass
        self.log = (self.profile / "native-optimization-20261114.log").open("a")
        self.child = subprocess.Popen(
            [
                str(self.args.python),
                "-m",
                "hindsight_ext_contextdb.local",
                "--component",
                "api",
                "--port",
                "18889",
            ],
            cwd=self.source,
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 180
        while True:
            if self.child.poll() is not None:
                raise RuntimeError("Validation API exited; see private log")
            try:
                with urlopen(self.api.base + "/health/ready", timeout=5) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Validation API readiness timeout")
            time.sleep(1)
        restored = {
            **FLAGS,
            "recall_rrf_semantic_weight": 0.97,
            "recall_rrf_bm25_weight": 0.03,
            "recall_rrf_graph_weight": 1.0,
            "recall_rrf_temporal_weight": 1.0,
            "recall_budget_function": "fixed",
            "recall_budget_fixed_high": 1000,
        }
        current = self.api.request("/config")["config"]
        original_path = self.out / "bank-original-config.json"
        if original_path.exists():
            assert json.loads(original_path.read_text()) == restored
            allowed = [restored] + [overrides(c) for c in self.plan["cases"]]
            assert any(
                all(current[k] == v for k, v in candidate.items())
                for candidate in allowed
            )
        else:
            assert all(current[k] == v for k, v in restored.items())
        self.original = restored
        save(original_path, restored)
        self.splits = {
            "dev393": self.topics,
            "tune100": {
                q: t
                for q, t in self.topics.items()
                if q in set(self.plan["tune_query_ids"])
            },
        }
        fresh_topics = dict(
            line.split("\t", 1)
            for line in (self.out / "fresh-topics.tsv").read_text().splitlines()
        )
        fresh_labels = {q: {} for q in fresh_topics}
        for line in (self.out / "fresh-qrels.tsv").read_text().splitlines():
            q, _, p, label = line.split()
            fresh_labels[q][p] = int(label)
        assert len(fresh_topics) == 200 and all(
            any(x.values()) for x in fresh_labels.values()
        )
        self.splits["fresh_train200"] = fresh_topics
        self.fresh_labels = fresh_labels

    def capture(self, case, split):
        topics = self.splits[split]
        qrels = (
            self.fresh_labels
            if split == "fresh_train200"
            else {q: self.qrels[q] for q in topics}
        )
        folder = self.out / split / case["name"]
        folder.mkdir(parents=True, exist_ok=True)
        config = {
            "case": case,
            "query_set": split,
            "query_ids_sha256": hashlib.sha256(
                json.dumps(list(topics)).encode()
            ).hexdigest(),
            "plan_sha256": sha(self.out / "experiment-plan.json"),
            "corpus_sha256": self.plan["corpus_sha256"],
            "source_commit": SOURCE_COMMIT,
            "candidate_ceiling": 60,
            "source_cap": 100,
            "max_tokens": 8192,
            "model_reranking": False,
            "concurrency": 1,
            "warmup": 4,
        }
        path = folder / "config.json"
        if path.exists():
            assert json.loads(path.read_text()) == config
        else:
            save(path, config)
        label = split + "/" + case["name"]
        if (folder / "metrics.json").exists():
            self.completed.append(label)
            return json.loads((folder / "metrics.json").read_text())
        self.api.request("/config", {"updates": overrides(case)}, method="PATCH")
        native = self.api.request("/config")["config"]
        assert all(native[k] == v for k, v in overrides(case).items())
        save(
            folder / "bank-config-readback.json", {k: native[k] for k in self.original}
        )
        runid = self.plan["run_prefix"] + label
        probe_qid = "4714933#0"
        save(
            self.measure / "measurement-context.json",
            {"contextbase": {"phase": "probe", "id": probe_qid, "run_id": runid}},
        )
        payload = self.api.request(
            "/memories/recall",
            {
                "query": self.topics[probe_qid],
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
        assert trace["query"]["budget"] == case["high_budget"]
        expected = {
            "semantic": case["semantic_weight"],
            "bm25": case["bm25_weight"],
            "graph": 1.0,
            "temporal": 1.0,
        }
        phases = {
            p["phase_name"]: p["details"] for p in trace["summary"]["phase_metrics"]
        }
        assert (
            phases["rrf_merge"]["rrf_weights"] == expected
            and phases["reranking"]["reranker_type"] == "rrf-passthrough"
        )
        assert any(
            a["method_name"] == "bm25" and a["results"]
            for a in trace["retrieval_results"]
        )
        for row in trace["rrf_merged"]:
            assert (
                abs(
                    row["rrf_score"]
                    - sum(
                        expected[k.removesuffix("_rank")] / (60 + rank)
                        for k, rank in row["source_ranks"].items()
                    )
                )
                < 1e-12
            )
        assert (
            hashlib.sha256(
                json.dumps(trace["query"]["query_embedding"]).encode()
            ).hexdigest()
            == self.plan["probe_embedding_sha256"]
        )
        save(
            folder / "probe-verification.json",
            {
                "budget": trace["query"]["budget"],
                "weights": expected,
                "bm25_returned": True,
                "rrf_entries_verified": len(trace["rrf_merged"]),
                "same_query_embedding": True,
            },
        )
        save(
            self.out / "execution-status.json",
            {
                "state": "querying",
                "query_set": split,
                "case": case["name"],
                "target_queries": len(topics),
                "completed_stages": self.completed,
            },
        )
        src = (
            {
                **self.dataset,
                "dataset": "miracl/zh/"
                + split
                + "/subset-"
                + self.plan["corpus_sha256"][:16],
            }
            if split != "dev393"
            else self.dataset
        )
        run = folder / "rankings.jsonl"
        bench.capture(
            Namespace(
                product="contextbase",
                data=self.data,
                run=run,
                passage_map=self.checkpoint,
                limit=60,
                reranker="benchmark-bge-reranker",
                resume=run.exists(),
                warmup=4,
                max_queries=None,
                smoke_index=False,
                config_sha256=sha(path),
                measurement_dir=self.measure,
                run_id=runid,
                max_tokens=8192,
                disable_rerank=True,
            ),
            topics,
            src,
        )
        metrics = bench.score(run, qrels, None, src)
        assert (
            metrics["queries"] == len(topics) and metrics["returned_depth"]["max"] <= 60
        )
        usage = Worker.archive_calls(
            Namespace(measure=self.measure),
            "contextbase",
            runid,
            folder / "model-calls.jsonl.gz",
        )
        assert not any(
            x.get("calls", 0) for k, x in usage.items() if k.endswith("/rerank")
        )
        calls = list(
            map(
                json.loads,
                gzip.open(folder / "model-calls.jsonl.gz", "rt").read().splitlines(),
            )
        )
        assert {
            x["id"] for x in calls if x["phase"] == "query" and x["kind"] == "embedding"
        } == set(topics)
        save(folder / "model-usage.json", usage)
        save(folder / "metrics.json", metrics)
        self.completed.append(label)
        render(self.out, self.plan)
        save(
            self.out / "execution-status.json",
            {
                "state": "stage_complete",
                "last_stage": label,
                "completed_stages": self.completed,
            },
        )
        publish(
            self.repo,
            self.out,
            "Archive native optimization " + label,
            self.args.publish,
        )
        print(
            "STAGE_COMPLETE",
            label,
            metrics["hit_at_1"],
            metrics["ndcg_at_10"],
            metrics["serial_p95_ms"],
            flush=True,
        )
        return metrics

    def close(self):
        restored = False
        if self.original and self.child and self.child.poll() is None:
            self.api.request("/config", {"updates": self.original}, method="PATCH")
            current = self.api.request("/config")["config"]
            assert all(current[k] == v for k, v in self.original.items())
            save(
                self.out / "restored-bank-config.json",
                {k: current[k] for k in self.original},
            )
            restored = True
        return restored

    def stop(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(30)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
        if self.log:
            self.log.close()
        if self.old_context is not None:
            (self.measure / "measurement-context.json").write_bytes(self.old_context)
        if self.lock:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()


def main(args):
    out = args.results.resolve()
    plan = json.loads((out / "experiment-plan.json").read_text())
    for name, value in plan["query_file_sha256"].items():
        assert sha(out / name) == value
    session = Session(args, plan)
    failure = None
    restored = False
    try:
        session.start()
        metrics = {c["name"]: session.capture(c, "tune100") for c in plan["cases"]}
        chosen = select_cases(plan["cases"], metrics)
        chosen["selection_only"] = "tune100"
        chosen["tuning_metrics_sha256"] = {
            c["name"]: sha(out / "tune100" / c["name"] / "metrics.json")
            for c in plan["cases"]
        }
        frozen = out / "frozen-selection.json"
        if frozen.exists():
            assert json.loads(frozen.read_text()) == chosen
        else:
            save(frozen, chosen)
        publish(
            session.repo,
            out,
            "Freeze native optimization candidates from tune100",
            args.publish,
        )
        print("FROZEN", json.dumps(chosen, ensure_ascii=False), flush=True)
        by_name = {c["name"]: c for c in plan["cases"]}
        for name in chosen["assessment_cases"]:
            for split in ("dev393", "fresh_train200"):
                session.capture(by_name[name], split)
    except Exception as error:
        failure = error
        save(
            out / "failure.json",
            {
                "type": type(error).__name__,
                "message": str(error),
                "completed_stages": session.completed,
                "recovery": "Rerun identical command; completed stages and fsynced queries are reused.",
            },
        )
    finally:
        try:
            restored = session.close()
        except Exception as error:
            save(
                out / "restoration-error.json",
                {"type": type(error).__name__, "message": str(error)},
            )
            if failure is None:
                failure = error
        session.stop()
    render(out, plan)
    save(
        out / "execution-status.json",
        {
            "state": "complete" if failure is None and restored else "failed",
            "completed_stages": session.completed,
            "restored_baseline97": restored,
            "original_service_untouched": True,
            "all_product_data_retained": True,
            "validation_api_stopped": True,
        },
    )
    publish(
        session.repo, out, "Finish bounded native hybrid optimization", args.publish
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
