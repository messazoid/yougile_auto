#!/usr/bin/env python3
"""Create a clean persistent volume without touching an existing deployment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3

from cisnet_store import baseline
from job_store import PipelineStore, SCHEMA_VERSION


DEFAULT_DATA_ROOT = Path("/opt/music-verifier/data")
MARKER = ".container-initialized"
DIRECTORIES = (
    "incoming", "audio", "events", "queue", "recognition-runs", "aggregation", "cisnet"
)
OPERATIONAL_TABLES = (
    "webhook_events", "source_jobs", "recognitions", "aggregation_runs",
    "polled_messages", "chat_notifications", "cisnet_runs", "cisnet_searches",
)


def _verify_database(database: Path, require_empty: bool) -> None:
    with sqlite3.connect(database) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Initialized SQLite database failed quick_check")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Initialized SQLite database failed foreign_key_check")
        version = db.execute("SELECT version FROM schema_info WHERE id=1").fetchone()
        if not version or version[0] != SCHEMA_VERSION:
            raise RuntimeError("Initialized SQLite database has an unexpected schema")
        if require_empty:
            populated = [
                table for table in OPERATIONAL_TABLES
                if db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            ]
            if populated:
                raise RuntimeError("Fresh SQLite database contains operational records")


def initialize_data(data_root: Path, uid: int, gid: int) -> Path:
    data_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    marker = data_root / MARKER
    first_start = not marker.exists()
    if first_start and any(data_root.iterdir()):
        raise RuntimeError("Refusing to initialize a non-empty unmarked data directory")
    database = data_root / "queue" / "pipeline.sqlite3"
    if not first_start and not database.is_file():
        raise RuntimeError("Initialized data volume is missing pipeline.sqlite3")
    for name in DIRECTORIES:
        (data_root / name).mkdir(mode=0o750, exist_ok=True)
    store = PipelineStore(database)
    store.initialize()
    baseline(store, initialize=True)
    _verify_database(database, require_empty=first_start)
    if first_start:
        marker.write_text(f"schema={SCHEMA_VERSION}\n", encoding="ascii")
        marker.chmod(0o600)
        for root, directories, files in os.walk(data_root):
            os.chown(root, uid, gid)
            for name in directories:
                os.chown(Path(root) / name, uid, gid)
            for name in files:
                os.chown(Path(root) / name, uid, gid)
    return database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()
    if args.data_root.resolve() != DEFAULT_DATA_ROOT:
        raise RuntimeError("Container initializer accepts only /opt/music-verifier/data")
    os.umask(0o077)
    database = initialize_data(
        args.data_root,
        int(os.environ.get("APP_UID", "10001")),
        int(os.environ.get("APP_GID", "10001")),
    )
    print(f"[INIT] clean data volume ready schema={SCHEMA_VERSION} database={database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
