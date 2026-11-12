"""Measure update visibility against an indexed configuration from the sweep."""

import argparse
import json
import os
from pathlib import Path

from pilot_metrics import run_supplemental


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_dir", type=Path)
    parser.add_argument("case", choices=("contextbase-60", "contextbase-50", "contextbase-40", "contextbase-30", "weknora"))
    args = parser.parse_args()
    case = args.case
    product = case.split("-")[0]
    lines = (args.sweep_dir / (case + ".log")).read_text().splitlines()
    previous_run, = [Path(line.split(maxsplit=1)[1]).name for line in lines if line.startswith("RUN ")]
    scope, = [line.split()[2] for line in lines if line.startswith("SCOPE " + product + " ")]
    if product == "contextbase":
        os.environ["BENCH_CONTEXTBASE_STATE"] = str(args.sweep_dir / "state" / case)
        os.environ["BENCH_RERANK_HIGH"] = "0" if case == "contextbase-60" else case.split("-")[1]

    import run_full_hybrid as run

    print(f"REPLAY {case} using indexed scope from {previous_run}", flush=True)
    mapping_path = run.root / "results" / previous_run / (product + "-documents.json")
    mapping = json.loads(mapping_path.read_text())
    source_ids = {str(remote): local for local, remote in mapping.items()}
    try:
        run.start_models()
        if product == "contextbase":
            run.start_contextbase_hybrid(existing_bank=scope)
        else:
            run.live.start_weknora(run.run_id, existing_knowledge=scope)
        api = run.HybridAPI(product, tracking=False)
        for question in run.questions[:4]:
            run.bench.retrieve(api, product, question["query"], source_ids)
        run_supplemental(api, product, run.run_id, run.run_dir, run.questions,
                         run.bench.read_rows(run.bench.ROOT / "updates.jsonl"), source_ids, run.bench, run.live)
    finally:
        run.cleanup()


if __name__ == "__main__":
    main()
