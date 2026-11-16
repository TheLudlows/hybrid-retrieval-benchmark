"""Post-hoc numeric stability probe; never changes the benchmark configuration."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import math
from pathlib import Path
import threading

import bench
from run_scale import http, save


def vector_sha(vector):
    return hashlib.sha256(
        json.dumps(vector, separators=(",", ":")).encode()
    ).hexdigest()


def difference(a, b):
    cosine = math.fsum(x * y for x, y in zip(a, b, strict=True)) / math.sqrt(
        math.fsum(x * x for x in a) * math.fsum(x * x for x in b)
    )
    return {
        "same_sha256": vector_sha(a) == vector_sha(b),
        "cosine_similarity": cosine,
        "max_absolute_difference": max(abs(x - y) for x, y in zip(a, b, strict=True)),
    }


def main(args):
    out = args.results
    topics, _ = bench.topics_and_qrels(args.data)
    plan = json.loads((out / "experiment-plan.json").read_text())
    quality = {}
    for label in ("load100-r1-c1", "load100-r2-c4"):
        folder = out / "runs" / label / "contextbase"
        attempt = json.loads((folder / "metrics.json").read_text())["attempt_directory"]
        quality[label] = json.loads((folder / attempt / "quality.json").read_text())[
            "per_topic"
        ]
    qids = plan["load_query_ids"]
    worst = min(
        qids,
        key=lambda q: quality["load100-r2-c4"][q]["ndcg_at_10"]
        - quality["load100-r1-c1"][q]["ndcg_at_10"],
    )
    selected = list(
        dict.fromkeys(
            [
                worst,
                max(qids, key=lambda q: len(topics[q])),
                min(qids, key=lambda q: len(topics[q])),
            ]
            + qids
        )
    )[:4]
    context_path = args.measurement_dir / "measurement-context.json"
    old = context_path.read_bytes()
    runid = "metrics-suite-20261116/embedding-batch-diagnostic"
    records = []

    def request(qid):
        result = http(
            "http://127.0.0.1:18090/contextbase/v1/embeddings",
            {"model": "bge-m3", "input": [topics[qid]]},
        )
        return {
            "qid": qid,
            "vector": result["data"][0]["embedding"],
            "usage": result["usage"],
        }

    try:
        save(
            context_path,
            {
                "contextbase": {
                    "phase": "embedding_batch_diagnostic",
                    "id": "controlled-inputs",
                    "run_id": runid,
                }
            },
        )
        for rep in (1, 2):
            for qid in selected:
                records.append({"mode": "single", "round": rep, **request(qid)})
        for rep in (1, 2):
            barrier = threading.Barrier(4)

            def parallel(qid):
                barrier.wait()
                return {"mode": "concurrent_four", "round": rep, **request(qid)}

            with ThreadPoolExecutor(max_workers=4) as pool:
                records.extend(pool.map(parallel, selected))
        result = http(
            "http://127.0.0.1:18090/contextbase/v1/embeddings",
            {"model": "bge-m3", "input": [topics[q] for q in selected]},
        )
        for qid, row in zip(selected, result["data"], strict=True):
            records.append(
                {
                    "mode": "joint_http_batch",
                    "round": 1,
                    "qid": qid,
                    "vector": row["embedding"],
                    "batch_usage": result["usage"],
                }
            )
    finally:
        context_path.write_bytes(old)
    comparisons = {}
    for qid in selected:
        baseline = next(
            r["vector"]
            for r in records
            if r["mode"] == "single" and r["round"] == 1 and r["qid"] == qid
        )
        comparisons[qid] = {
            r["mode"] + "/" + str(r["round"]): difference(baseline, r["vector"])
            for r in records
            if r["qid"] == qid
        }
    folder = out / "embedding-batch-diagnostic"
    folder.mkdir(exist_ok=True)
    with gzip.open(folder / "vectors.json.gz", "wt") as stream:
        json.dump(records, stream)
    summary = {
        "post_hoc": True,
        "run_id": runid,
        "selected_query_ids": selected,
        "selection": "Largest observed nDCG loss in second concurrency4 ContextBase run, plus long/short queries and first remaining registered query; four examples, not a representative benchmark.",
        "unchanged_model_settings": http("http://127.0.0.1:18090/hybrid/info"),
        "comparisons": comparisons,
        "limitation": "Direct model-only diagnostic, separate from native benchmark. Does not assign all native ranking changes to a single cause. No tuning or main score replacement.",
    }
    save(folder / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--measurement-dir", type=Path, required=True)
    main(p.parse_args())
