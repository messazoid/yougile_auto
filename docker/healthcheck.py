#!/usr/bin/env python3
"""Container-local health checks that never contact external services."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import time
from urllib.request import urlopen


DATABASE = Path("/opt/music-verifier/data/queue/pipeline.sqlite3")
HEARTBEAT = Path("/opt/music-verifier/data/cisnet/runner.heartbeat")


def check_database() -> None:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=2)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed")
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("receiver", "database", "cisnet-runner"))
    args = parser.parse_args()
    if args.mode == "receiver":
        with urlopen("http://127.0.0.1:8080/health", timeout=3) as response:
            if response.status != 200:
                raise RuntimeError("Receiver health endpoint failed")
        return 0
    check_database()
    if args.mode == "cisnet-runner":
        if not HEARTBEAT.is_file() or time.time() - HEARTBEAT.stat().st_mtime > 120:
            raise RuntimeError("CIS-Net runner heartbeat is stale")
        endpoint = os.environ.get("CISNET_CDP_ENDPOINT")
        if endpoint != "http://127.0.0.1:9223":
            raise RuntimeError("CIS-Net CDP endpoint must use the shared browser loopback")
        with urlopen(f"{endpoint}/json/version", timeout=3) as response:
            if response.status != 200:
                raise RuntimeError("CIS-Net browser CDP endpoint failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
