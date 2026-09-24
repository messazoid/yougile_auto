#!/usr/bin/env python3
"""Show Compose logs without exposing webhook secrets or URL query strings."""

from __future__ import annotations

import argparse
import json
import re
import sys


PREFIX = re.compile(r"^([\w-]+)\s+\|\s+(.*)$")
WEBHOOK = re.compile(r"(/webhooks/yougile/)[^\s?'\"<>]+", re.I)
URL_QUERY = re.compile(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]+", re.I)
AUTH = re.compile(r"(?i)(\b(?:authorization|cookie|set-cookie)\s*[:=]\s*).*$")
BEARER = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")


def scrub(value: str) -> str:
    value = WEBHOOK.sub(r"\1<hidden>", value)
    value = URL_QUERY.sub(r"\1?<hidden>", value)
    value = AUTH.sub(r"\1<hidden>", value)
    return BEARER.sub(r"\1<hidden>", value)


def render(line: str, *, http_only: bool = False, show_all: bool = False) -> str | None:
    match = PREFIX.match(line.rstrip("\n"))
    if not match:
        return None
    service, message = match.groups()
    if http_only and not message.startswith("[YOUGILE-HTTP] "):
        return None
    if not show_all and (message.startswith("INFO:") or "pam_unix(runuser:session)" in message):
        return None
    if message.startswith("[YOUGILE-HTTP] "):
        try:
            event = json.loads(message[len("[YOUGILE-HTTP] "):])
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict):
            direction = "YouGile -> server" if event.get("direction") == "in" else "server -> YouGile"
            message = (
                f"{direction} | {event.get('method', '?')} {event.get('route', '/<hidden>')} "
                f"| HTTP {event.get('status') if event.get('status') is not None else 'NO RESPONSE'} "
                f"| {event.get('duration_ms', '?')} ms | {event.get('kind', 'unknown')}"
            )
    return f"{service:<18} {scrub(message)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-only", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    try:
        for line in sys.stdin:
            output = render(line, http_only=args.http_only, show_all=args.all)
            if output is not None:
                print(output, flush=True)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
