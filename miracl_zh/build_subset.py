"""Build a reproducible MIRACL subset that preserves every official development judgment."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import random
import shutil
from tempfile import TemporaryDirectory

from bench import SOURCE, SUBSET_SHARD, article_id, corpus_rows, dataset_source, topics_and_qrels


def build(data, output, unit, count, seed, include_subset=None, extra_passages=None):
    if (data / "subset.json").exists():
        raise ValueError("Build from the official full corpus")
    if output.exists():
        raise FileExistsError(f"Choose a new output directory: {output}")
    _, qrels = topics_and_qrels(data)
    judged = {p for judgments in qrels.values() for p in judgments}
    mandatory = {article_id(p) for p in judged} if unit == "articles" else judged
    original_judged = judged.copy()
    parent = None
    if include_subset or extra_passages:
        if unit != "passages":
            raise ValueError("Nested subsets and extra mandatory IDs require passage sampling")
        mandatory = mandatory.copy()
        if include_subset:
            parent_source = dataset_source(include_subset)
            if not parent_source.get("subset"):
                raise ValueError("The parent must be a verified subset")
            parent = {"passages": parent_source["passages"], "sha256": parent_source["subset"]["file"]["sha256"]}
            mandatory.update(row["docid"] for _, row in corpus_rows(include_subset, parent_source))
        if extra_passages:
            from bench import PASSAGE_ID
            extra = set(extra_passages.read_text().splitlines())
            if not all(PASSAGE_ID(p) for p in extra):
                raise ValueError("Invalid extra mandatory passage ID")
            mandatory.update(extra)
    rng = random.Random(seed)
    extra_count = count - len(mandatory)
    if extra_count < 0:
        raise ValueError(f"At least {len(mandatory)} {unit} are needed to preserve all judgments")

    if unit == "articles":
        population = {article_id(row["docid"]) for _, row in corpus_rows(data, SOURCE)}
        if not mandatory <= population:
            raise ValueError("Some judged articles are absent from the official corpus")
        candidates = sorted(population - mandatory)
    else:
        candidates = range(SOURCE["passages"] - len(mandatory))
    if extra_count > len(candidates):
        raise ValueError(f"The official corpus has fewer than {count} {unit}")
    selected = set(rng.sample(candidates, extra_count))
    if unit == "articles":
        selected.update(mandatory)

    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as tmp:
        stage = Path(tmp) / "dataset"
        shard = stage / SUBSET_SHARD
        shard.parent.mkdir(parents=True)
        kept = seen = unjudged = 0
        missing = mandatory.copy() if unit == "passages" else judged.copy()
        with shard.open("wb") as blob, gzip.GzipFile(fileobj=blob, mode="wb", filename="", mtime=0) as gz:
            with io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as stream:
                for _, row in corpus_rows(data, SOURCE):
                    passage = row["docid"]
                    seen += 1
                    if unit == "articles":
                        keep = article_id(passage) in selected
                    elif passage in mandatory:
                        keep = True
                    else:
                        keep = unjudged in selected
                        unjudged += 1
                    if keep:
                        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")), file=stream)
                        missing.discard(passage)
                        kept += 1
        if seen != SOURCE["passages"] or missing or (unit == "passages" and kept != count):
            raise ValueError(f"Invalid subset coverage: scanned {seen}, kept {kept}, missing {len(missing)} judged IDs")
        with shard.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        manifest = {"unit": unit, "count": count, "seed": seed, "passages": kept, "judged_passages": len(original_judged),
                    "file": {"size": shard.stat().st_size, "sha256": digest},
                    "source": {name: entry["sha256"] for name, entry in SOURCE["files"].items()}}
        if parent:
            manifest["parent_subset"] = parent
        if extra_passages:
            manifest["extra_mandatory_ids"] = {"count": len(extra), "sha256": hashlib.sha256(extra_passages.read_bytes()).hexdigest()}
        for name in ("topics.tsv", "qrels.tsv"):
            shutil.copyfile(data / name, stage / name)
        (stage / "subset.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        stage.rename(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="Verified official full corpus directory")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--unit", choices=("articles", "passages"), required=True)
    parser.add_argument("--count", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--include-subset", type=Path, help="Preserve every passage in this parent subset")
    parser.add_argument("--extra-passages", type=Path, help="UTF-8 official passage IDs, one per line, also to preserve")
    args = parser.parse_args()
    result = build(args.data, args.output, args.unit, args.count, args.seed, args.include_subset, args.extra_passages)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
