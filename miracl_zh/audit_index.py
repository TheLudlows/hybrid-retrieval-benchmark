"""Audit checkpoint and live native index coverage against a verified subset."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from bench import corpus_rows, dataset_source


def audit(args):
    source = dataset_source(args.data)
    expected = {r["docid"]: r["title"] + "\n\n" + r["text"] for _, r in corpus_rows(args.data, source)}
    with sqlite3.connect(args.checkpoint.resolve().as_uri() + "?mode=ro", uri=True) as db:
        binding, complete = db.execute("SELECT binding,complete FROM miracl_ingest_run WHERE id=1").fetchone()
        product, endpoint, scope, fingerprints = json.loads(binding)
        assert product == args.product
        assert fingerprints == {n: e["sha256"] for n, e in source["files"].items() if n.startswith("corpus/")}
        mapping = dict(db.execute("SELECT remote_id,passage_id FROM passage_map WHERE indexed=1"))
        assert complete == 1 and set(mapping.values()) == expected.keys()
        assert len(mapping) == len(expected) and None not in mapping
    details = {}
    if product == "contextbase":
        import psycopg2
        assert re.fullmatch(r"[a-z0-9_]+", scope)
        schema = "ctxdb_ws_" + scope
        with psycopg2.connect(os.environ["AUDIT_POSTGRES_URL"]) as conn, conn.cursor() as cur:
            cur.execute(f'SELECT id,original_text FROM "{schema}".documents WHERE bank_id=%s', (scope,))
            contents = dict(cur.fetchall())
            cur.execute(f'SELECT DISTINCT document_id FROM "{schema}".memory_units WHERE embedding IS NOT NULL AND bank_id=%s', (scope,))
            vector_docs = {r[0] for r in cur}
            cur.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s", (schema,))
            details["indexes"] = dict(cur.fetchall())
        assert contents == expected, "Native stored content differs from official input"
        assert vector_docs == expected.keys(), "Missing native vector rows"
    else:
        assert args.weknora_db
        with sqlite3.connect(args.weknora_db.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT id,channel,parse_status,metadata FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL", (scope,)).fetchall()
            assert {r[0] for r in rows} == mapping.keys(), "Unexpected or missing native knowledge IDs"
            for remote, channel, status, metadata in rows:
                official = mapping[remote]
                assert channel == "miracl-" + official.replace("#", "-") and status == "completed"
                assert json.loads(metadata)["content"] == expected[official].strip(), "Stored manual content mismatch"
            vectors = {r[0] for r in db.execute("SELECT DISTINCT knowledge_id FROM lite_embeddings WHERE knowledge_base_id=? AND is_enabled=1", (scope,))}
            assert vectors == mapping.keys(), "Missing native vector/lexical entries"
            details["fts_rows"] = db.execute("SELECT COUNT(*) FROM lite_embeddings_fts").fetchone()[0]
            details["embedding_rows"] = db.execute("SELECT COUNT(*) FROM lite_embeddings WHERE knowledge_base_id=?", (scope,)).fetchone()[0]
            assert details["fts_rows"] >= details["embedding_rows"] > 0
            embedding_ids = {r[0] for r in db.execute("SELECT id FROM lite_embeddings WHERE knowledge_base_id=? AND is_enabled=1 AND dimension=1024", (scope,))}
            # vec0 is registered by the native Go process. Its ordinary shadow
            # row-ID table can also be checked by a read-only stdlib connection.
            vector_ids = {r[0] for r in db.execute("SELECT rowid FROM vec_embeddings_1024_rowids")}
            assert len(embedding_ids) == details["embedding_rows"] and embedding_ids <= vector_ids
            details["native_vector_rows"] = len(vector_ids)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.with_suffix(".mapping.csv").open("w") as stream:
        writer = csv.writer(stream); writer.writerow(["remote_id", "passage_id"])
        writer.writerows(sorted(mapping.items()))
    canonical = "".join(p + "\t" + hashlib.sha256(t.encode()).hexdigest() + "\n" for p, t in sorted(expected.items()))
    result = {"product": product, "dataset": source["dataset"], "endpoint": endpoint, "scope": scope,
              "official_ids": len(expected), "indexed_ids": len(mapping), "missing_ids": 0, "extra_ids": 0,
              "content_checked": True, "canonical_content_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
              "corpus_fingerprints": fingerprints, **details}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--product", choices=("contextbase", "weknora"), required=True)
    p.add_argument("--weknora-db", type=Path)
    p.add_argument("--output", type=Path, required=True)
    audit(p.parse_args())
