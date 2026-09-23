#!/usr/bin/env python3
"""Readable, privacy-safe view of the music-verifier systemd journals."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import subprocess
import sys


UNITS = (
    "yougile-receiver.service",
    "music-verifier-worker.service",
    "music-verifier-cisnet.service",
)
COMPONENTS = {
    "yougile-receiver.service": "RECEIVER",
    "music-verifier-worker.service": "WORKER",
    "music-verifier-cisnet.service": "CIS-NET",
}
WEBHOOK_PATH_RE = re.compile(r"(/webhooks/yougile/)[^\s?'\"<>]+", re.IGNORECASE)
BEARER_RE = re.compile(r"(?i)(authorization[^\n]*?bearer\s+)[^\s,;]+")


def scrub(message: str) -> str:
    message = WEBHOOK_PATH_RE.sub(r"\1<hidden>", message)
    return BEARER_RE.sub(r"\1<hidden>", message)


def _json_after(message: str, prefix: str) -> dict | None:
    if not message.startswith(prefix):
        return None
    try:
        value = json.loads(message[len(prefix):])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def readable_message(message: str) -> str:
    http = _json_after(message, "[YOUGILE-HTTP] ")
    if http is not None:
        direction = "YouGile -> server" if http.get("direction") == "in" else "server -> YouGile"
        status = http.get("status") if http.get("status") is not None else "NO RESPONSE"
        duration = http.get("duration_ms", "?")
        result = (
            f"{direction} | {http.get('method', '?')} {http.get('route', '/<redacted>')} "
            f"| HTTP {status} | {duration} ms | {http.get('kind', 'unknown')}"
        )
        if http.get("error_type"):
            result += f" | error={http['error_type']}"
        return result

    playwright = _json_after(message, "[CISNET-PLAYWRIGHT] ")
    if playwright is not None:
        event = {"begin": "START", "ok": "OK", "failed": "ERROR"}.get(
            playwright.get("event"), str(playwright.get("event") or "?").upper()
        )
        result = f"Playwright {playwright.get('step', 'unknown')} | {event}"
        if playwright.get("code"):
            result += f" | {playwright['code']}"
        return result

    return scrub(message)


def _component(record: dict, message: str) -> str:
    candidates = (str(record.get("_SYSTEMD_UNIT") or ""), str(record.get("UNIT") or ""), message)
    for unit, label in COMPONENTS.items():
        if any(unit in candidate for candidate in candidates):
            return label
    identifier = str(record.get("SYSLOG_IDENTIFIER") or "").lower()
    if identifier in {"python", "uvicorn"}:
        return "APP"
    return identifier.upper()[:9] or "SYSTEM"


def _timestamp(record: dict) -> str:
    try:
        value = int(record["__REALTIME_TIMESTAMP"]) / 1_000_000
    except (KeyError, TypeError, ValueError):
        return "-- --:--:--"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%m-%d %H:%M:%S")


def format_record(
    record: dict, show_all: bool = False, http_only: bool = False
) -> str | None:
    raw = record.get("MESSAGE")
    if not isinstance(raw, str):
        return None
    if http_only and not raw.startswith("[YOUGILE-HTTP] "):
        return None
    identifier = str(record.get("SYSLOG_IDENTIFIER") or "").lower()
    if not show_all:
        if raw.startswith("INFO:"):
            return None
        if "pam_unix(runuser:session)" in raw:
            return None
        if identifier == "systemd" and not re.search(
            r"failed|failure|error|exited|timeout", raw, re.IGNORECASE
        ):
            return None
    return f"{_timestamp(record)}  {_component(record, raw):<9}  {readable_message(raw)}"


def journal_command(follow: bool, http_only: bool = False) -> list[str]:
    command = ["journalctl"]
    for unit in UNITS[:1] if http_only else UNITS:
        command.extend(("-u", unit))
    command.extend(("-n", "100" if follow else "200", "--no-pager", "-o", "json"))
    if follow:
        command.append("-f")
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Readable music-verifier logs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--follow", action="store_true", help="follow new events (default)")
    mode.add_argument("--once", action="store_true", help="show recent events and exit")
    parser.add_argument("--all", action="store_true", help="include normal systemd and Uvicorn noise")
    parser.add_argument("--http-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    follow = not args.once
    try:
        process = subprocess.Popen(
            journal_command(follow, args.http_only),
            stdout=subprocess.PIPE, text=True, encoding="utf-8"
        )
        assert process.stdout is not None
        for line in process.stdout:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rendered = format_record(record, args.all, args.http_only)
            if rendered:
                print(rendered, flush=True)
        return process.wait()
    except KeyboardInterrupt:
        if "process" in locals():
            process.terminate()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
