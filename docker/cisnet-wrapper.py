#!/usr/bin/env python3
"""Execute one CIS-Net CLI command against the Compose browser."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sys
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

import cisnet_cli


def main(argv: list[str]) -> int:
    if not os.environ.get("CISNET_EMAIL") or not os.environ.get("CISNET_PASSWORD"):
        print("yougile-cisnet: CIS-Net credentials are not configured", file=sys.stderr)
        return 2
    endpoint = os.environ.get("CISNET_CDP_ENDPOINT", "")
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or not parsed.hostname or parsed.port != 9223:
        print("yougile-cisnet: invalid internal CDP endpoint", file=sys.stderr)
        return 2
    lock_path = Path("/opt/music-verifier/data/cisnet/session.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("yougile-cisnet: another CIS-Net search is running", file=sys.stderr)
            return 75
        try:
            with urlopen(urljoin(endpoint.rstrip("/") + "/", "json/version"), timeout=3):
                pass
        except OSError:
            print("yougile-cisnet: CIS-Net browser is unavailable", file=sys.stderr)
            return 2
        return cisnet_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
