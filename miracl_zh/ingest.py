"""Stream MIRACL passages into a dedicated native test index."""

import argparse
from contextlib import closing
import gzip
from itertools import islice
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote, urlencode

from bench import PASSAGE_ID, SOURCE, checked_path, index_binding, prior_api


def passages(data):
    for name in SOURCE["files"]:
        if name.startswith("corpus/"):
            with gzip.open(checked_path(data, name), "rt", encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    if not PASSAGE_ID(row["docid"]):
                        raise ValueError(f"Invalid source passage ID in {name}")
                    yield row


def setup(db, binding):
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS miracl_ingest_run (
                      id INTEGER PRIMARY KEY CHECK (id = 1), binding TEXT NOT NULL, complete INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS passage_map (
                      passage_id TEXT PRIMARY KEY, remote_id TEXT UNIQUE, indexed INTEGER NOT NULL) WITHOUT ROWID""")
        db.execute("INSERT OR IGNORE INTO miracl_ingest_run VALUES (1, ?, 0)", (binding,))
        if db.execute("SELECT binding FROM miracl_ingest_run").fetchone() != (binding,):
            raise ValueError("Use the checkpoint with its original product, endpoint, scope and corpus revision")


def passage_content(row):
    return row["title"] + "\n\n" + row["text"]


def contextbase_batch(db, api, batch):
    items = [{"document_id": row["docid"], "content": passage_content(row), "update_mode": "replace",
              "timestamp": "unset"} for row in batch]
    api.request("/memories", {"items": items, "async": False})
    with db:
        db.executemany("INSERT INTO passage_map VALUES (?, ?, 1)",
                       ((row["docid"], row["docid"]) for row in batch))


def channel(passage):
    return "miracl-" + passage.replace("#", "-")


def recover(db, api, passage, remote):
    if not PASSAGE_ID(passage):
        raise ValueError("Invalid MIRACL passage ID")
    with db:
        if remote:
            data = api.request("/knowledge/" + quote(remote, safe=""))["data"]
            if data["id"] != remote or data["knowledge_base_id"] != api.scope or data["channel"] != channel(passage):
                raise ValueError("Remote knowledge does not match the original scope and passage marker")
            cursor = db.execute("UPDATE passage_map SET remote_id = ? WHERE passage_id = ? AND remote_id IS NULL",
                                (remote, passage))
        else:
            cursor = db.execute("DELETE FROM passage_map WHERE passage_id = ? AND remote_id IS NULL", (passage,))
        if cursor.rowcount != 1:
            raise ValueError("The passage has no interrupted WeKnora create to reconcile")


def weknora_create(db, api, row):
    passage = row["docid"]
    with db:
        db.execute("INSERT INTO passage_map VALUES (?, NULL, 0)", (passage,))
    # WeKnora assigns the ID. Keep the intent on disk until the reply yields its ID.
    try:
        data = api.request("/knowledge-bases/" + quote(api.scope, safe="") + "/knowledge/manual", {
            "title": row["title"], "content": passage_content(row), "status": "publish",
            "channel": channel(passage),
        })["data"]
        remote = data["id"]
        if not isinstance(remote, str) or not remote:
            raise ValueError("Missing WeKnora knowledge ID")
    except Exception as exc:
        raise RuntimeError(f"WeKnora create for {passage} is unresolved, marker {channel(passage)}") from exc
    with db:
        db.execute("UPDATE passage_map SET remote_id = ? WHERE passage_id = ?", (remote, passage))
    return remote


def wait_for_index(db, api, ids, timeout, interval):
    remaining = set(ids)
    deadline = time.monotonic() + timeout
    while remaining:
        query = urlencode({"kb_id": api.scope, "ids": sorted(remaining)}, doseq=True)
        rows = api.request("/knowledge/batch?" + query)["data"] or []
        indexed = set()
        for row in rows:
            remote, status = row["id"], row["parse_status"]
            if remote not in remaining:
                raise ValueError(f"Unexpected WeKnora knowledge ID {remote}")
            if status in {"failed", "cancelled", "deleting", "draft"}:
                raise RuntimeError(f"WeKnora knowledge {remote} entered {status}. Repair it in the test KB and resume")
            if status == "completed":
                indexed.add(remote)
        with db:
            db.executemany("UPDATE passage_map SET indexed = 1 WHERE remote_id = ?", ((i,) for i in indexed))
        remaining -= indexed
        if remaining:
            delay = deadline - time.monotonic()
            if delay <= 0:
                raise TimeoutError(f"WeKnora indexing still pending for {len(remaining)} passages. Resume with the same checkpoint")
            time.sleep(min(delay, interval))


def ingest(args, api, db):
    unresolved = db.execute("SELECT passage_id FROM passage_map WHERE remote_id IS NULL LIMIT 1").fetchone()
    if unresolved:
        passage = unresolved[0]
        raise RuntimeError(f"Unresolved create for {passage}, marker {channel(passage)}. Reconcile before continuing")
    rows = islice(passages(args.data), args.limit) if args.limit is not None else passages(args.data)
    seen = 0
    while batch := list(islice(rows, args.batch_size)):
        entries = [(row, db.execute("SELECT remote_id, indexed FROM passage_map WHERE passage_id = ?",
                                    (row["docid"],)).fetchone()) for row in batch]
        if args.product == "contextbase":
            new = [row for row, entry in entries if entry is None]
            if new:
                contextbase_batch(db, api, new)
        else:
            pending = [entry[0] if entry else weknora_create(db, api, row)
                       for row, entry in entries if entry is None or not entry[1]]
            wait_for_index(db, api, pending, args.index_timeout, args.poll_interval)
        seen += len(batch)
        if seen // 1000 != (seen - len(batch)) // 1000:
            print(f"Scanned {seen} passages", flush=True)

    expected = args.limit if args.limit is not None else SOURCE["passages"]
    if seen != expected:
        raise ValueError(f"Expected {expected} passages, read {seen}")
    indexed = db.execute("SELECT COUNT(*) FROM passage_map WHERE indexed = 1").fetchone()[0]
    if seen == SOURCE["passages"]:
        if indexed != seen:
            raise ValueError(f"Expected {seen} indexed passages, got {indexed}")
        with db:
            db.execute("UPDATE miracl_ingest_run SET complete = 1 WHERE id = 1")
    complete = bool(db.execute("SELECT complete FROM miracl_ingest_run").fetchone()[0])
    print(json.dumps({"indexed_passages": indexed, "full_corpus": complete}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("product", choices=("contextbase", "weknora"))
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int, help="Only load the first N passages for a smoke test")
    parser.add_argument("--index-timeout", type=float, default=900)
    parser.add_argument("--poll-interval", type=float, default=2)
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--recover", nargs=2, metavar=("PASSAGE", "KNOWLEDGE_ID"))
    recovery.add_argument("--retry-missing", metavar="PASSAGE", help="Clear an unresolved create after verifying absence")
    args = parser.parse_args()
    if not (1 <= args.batch_size <= 64 and args.index_timeout > 0 and args.poll_interval > 0):
        parser.error("Use a batch size of 1–64 and positive timing values")
    if args.limit is not None and not 1 <= args.limit <= SOURCE["passages"]:
        parser.error("The limit must be within the corpus size")
    if (args.recover or args.retry_missing) and args.product != "weknora":
        parser.error("Create reconciliation is only needed for WeKnora")
    api = prior_api(args.product)
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.checkpoint)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        setup(db, index_binding(api, args.product))
        if args.recover or args.retry_missing:
            recover(db, api, *(args.recover or (args.retry_missing, None)))
        else:
            ingest(args, api, db)


if __name__ == "__main__":
    main()
