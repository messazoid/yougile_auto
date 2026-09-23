#!/usr/bin/env python3
"""Keep a disabled worker observable; exec the real worker when enabled."""

from __future__ import annotations

import os
import signal
import sys
import threading


def main() -> int:
    if os.environ.get("ACR_EXECUTE", "0") == "1":
        os.execv(sys.executable, [sys.executable, "/opt/music-verifier/src/recognition_worker.py"])
    stopped = threading.Event()

    def stop(signum, frame) -> None:
        del signum, frame
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("[WAIT] ACR worker is disabled; set ACR_EXECUTE=1 and recreate the container.", flush=True)
    while not stopped.wait(60):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
