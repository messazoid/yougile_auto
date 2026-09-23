#!/usr/bin/env python3
"""Container-native replacement for the systemd CIS-Net timer."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import threading
import time

import cisnet_automation


HEARTBEAT = Path("/opt/music-verifier/data/cisnet/runner.heartbeat")
STOP = threading.Event()


def heartbeat() -> None:
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    while not STOP.is_set():
        temporary = HEARTBEAT.with_suffix(".tmp")
        temporary.write_text(str(int(time.time())) + "\n", encoding="ascii")
        temporary.replace(HEARTBEAT)
        STOP.wait(15)


def stop(signum, frame) -> None:
    del signum, frame
    STOP.set()


def main() -> int:
    interval = int(os.environ.get("CISNET_INTERVAL_SECONDS", "60"))
    if not 10 <= interval <= 3600:
        raise RuntimeError("CISNET_INTERVAL_SECONDS must be between 10 and 3600")
    if not os.environ.get("CISNET_EMAIL") or not os.environ.get("CISNET_PASSWORD"):
        raise RuntimeError("CIS-Net credentials are not configured")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    thread = threading.Thread(target=heartbeat, name="cisnet-heartbeat", daemon=True)
    thread.start()
    try:
        while not STOP.is_set():
            try:
                state = cisnet_automation.run_pending()
                if state:
                    print(f"[CISNET] loop_state=error exit_code={state}", flush=True)
            except Exception as error:
                print(f"[CISNET] loop_error type={type(error).__name__}", flush=True)
            STOP.wait(interval)
    finally:
        STOP.set()
        thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
