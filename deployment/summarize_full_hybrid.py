"""Recompute the document benchmark from full hybrid response captures."""
from collections import Counter
import json
from pathlib import Path
import sys

import tiktoken

KIT = Path(__file__).resolve().parent.parent / "doc_recall_v1"
sys.path.insert(0, str(KIT))
import bench


def evidence_at_budgets(rows, questions, budgets):
    encoder = tiktoken.get_encoding("o200k_base")
    totals = {str(budget): Counter() for budget in budgets}
    for row in rows:
        evidence = questions[row["id"]]["evidence"]
        if not evidence:
            continue
        for budget in budgets:
            metrics = totals[str(budget)]
            if budget == "native":
                passages = (candidate["text"] for candidate in row["candidates"])
            else:
                passages = []
                remaining = budget
                for candidate in row["candidates"]:
                    tokens = encoder.encode_ordinary(candidate["text"])
                    prefix = encoder.decode(tokens[:remaining]).rstrip("\ufffd")
                    while len(encoder.encode_ordinary(prefix)) > remaining:
                        prefix = prefix[:-1]
                    remaining -= len(encoder.encode_ordinary(prefix))
                    passages.append(prefix)
            found = set()
            for candidate, text in zip(row["candidates"], passages, strict=True):
                for entry in evidence:
                    if (entry["document_id"] == candidate["document_id"]
                            and bench.normalize(entry["quote"]) in bench.normalize(text)):
                        found.add(entry["id"])
            metrics["required_quotes"] += len(evidence)
            metrics["matched_quotes"] += len(found)
            metrics["complete_questions"] += len(found) == len(evidence)
            metrics["macro_recall_sum"] += len(found) / len(evidence)
            metrics["answerable_questions"] += 1
    return {
        budget: {**metrics, "macro_recall": metrics["macro_recall_sum"] / metrics["answerable_questions"]}
        for budget, metrics in totals.items()
    }


def summarize(product, run_dir, questions):
    rows = bench.read_rows(run_dir / f"{product}-capture.jsonl")
    assert len(rows) == 120 and {row["id"] for row in rows} == set(questions)
    assert all("error" not in row for row in rows)
    overall = bench.summarize(rows, questions)
    assert overall["unmapped_candidates"] == overall["candidates_without_source_text"] == 0
    by_category = {
        category: bench.summarize([row for row in rows if questions[row["id"]]["category"] == category], questions)
        for category in sorted({question["category"] for question in questions.values()})
    }
    evidence = evidence_at_budgets(rows, questions, (256, 512, 1024, 2048, 4096, "native"))
    assert evidence["1024"]["macro_recall"] == overall["evidence_recall_at_1024_tokens"]
    calls = bench.read_rows(run_dir / f"{product}-model-calls.jsonl")
    usage = {}
    for phase in ("ingest", "retrieve"):
        usage[phase] = {}
        for kind in sorted({row["kind"] for row in calls if row["phase"] == phase}):
            matching = [row for row in calls if row["phase"] == phase and row["kind"] == kind]
            assert all(row["status"] == "observed" for row in matching)
            if phase == "retrieve" and kind in ("embedding", "rerank"):
                assert {row["id"] for row in matching} == set(questions)
            usage[phase][kind] = {
                field: sum(row.get(field, 0) for row in matching)
                for field in ("calls", "input_tokens", "output_tokens", "pairs")
            }
    assert set(usage["retrieve"]) >= {"embedding", "rerank"}
    return {"overall": overall, "by_category": by_category, "evidence": evidence, "usage": usage}


def main():
    questions = {question["id"]: question for question in bench.read_rows(KIT / "questions.jsonl")}
    results = {product: summarize(product, Path(path), questions) for product, path in
               zip(("weknora", "contextbase"), sys.argv[1:], strict=True)}
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
