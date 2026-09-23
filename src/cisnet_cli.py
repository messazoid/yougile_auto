#!/usr/bin/env python3
"""CIS-Net search command for manual runs and completed aggregation exports.

This module intentionally owns selection and durable artifacts, while the
Playwright adapter owns only the browser interaction.  It never makes a rights
decision or downloads a package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone


class CisnetCommandError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


PLAYWRIGHT_LOG_PREFIX = "[CISNET-PLAYWRIGHT] "
PLAYWRIGHT_STEPS = {
    "adapter.init", "adapter.process", "browser.attach", "login.credentials",
    "login.form", "login.fill", "login.submit", "login.wait", "login.busy",
    "session.inspect", "session.finish", "mwi.open", "search.request",
    "search.fields", "search.submit", "search.wait_results", "search.capture",
}
PLAYWRIGHT_CODES = {
    "ELEMENT_COUNT", "CREDENTIALS_MISSING", "LOGIN_FORM_MISSING",
    "BUSY_SESSION", "LOGIN_TIMEOUT", "ALREADY_SIGNED_IN", "TAB_MISSING",
    "RESULTS_CONFLICT", "TIMEOUT", "CDP_ATTACH_FAILED", "INVALID_REQUEST_JSON",
    "UNEXPECTED", "NO_TRACE",
}
PLAYWRIGHT_EVENTS = {"begin", "ok", "failed"}


def safe_playwright_log_lines(stderr: str | None) -> list[str]:
    """Keep only structured adapter events; never forward raw browser stderr."""
    lines = []
    for line in (stderr or "").splitlines():
        if not line.startswith(PLAYWRIGHT_LOG_PREFIX):
            continue
        try:
            event = json.loads(line[len(PLAYWRIGHT_LOG_PREFIX):])
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or set(event) - {"step", "event", "code"}:
            continue
        step, status, code = event.get("step"), event.get("event"), event.get("code")
        if not isinstance(step, str) or step not in PLAYWRIGHT_STEPS:
            continue
        if not isinstance(status, str) or status not in PLAYWRIGHT_EVENTS:
            continue
        if code is not None and (not isinstance(code, str) or code not in PLAYWRIGHT_CODES):
            continue
        if status == "failed" and code is None:
            continue
        safe = {"step": step, "event": status}
        if code is not None:
            safe["code"] = code
        lines.append(PLAYWRIGHT_LOG_PREFIX + json.dumps(safe, separators=(",", ":")))
    return lines


def _run_adapter_streaming(command: list[str]) -> subprocess.CompletedProcess:
    """Run the browser adapter while forwarding only safe trace lines live."""
    process = subprocess.Popen(
        command,
        cwd="/opt/cisnet-playwright",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    safe_lines: list[str] = []

    def forward_stderr() -> None:
        assert process.stderr is not None
        for raw_line in process.stderr:
            for line in safe_playwright_log_lines(raw_line):
                safe_lines.append(line)
                print(line, file=sys.stderr, flush=True)

    thread = threading.Thread(target=forward_stderr, name="cisnet-adapter-stderr", daemon=True)
    thread.start()
    assert process.stdout is not None
    stdout = process.stdout.read()
    returncode = process.wait()
    thread.join()
    stderr = "\n".join(safe_lines)
    if returncode and not any(
        json.loads(line[len(PLAYWRIGHT_LOG_PREFIX):]).get("event") == "failed"
        for line in safe_lines
    ):
        fallback = PLAYWRIGHT_LOG_PREFIX + '{"step":"adapter.process","event":"failed","code":"NO_TRACE"}'
        print(fallback, file=sys.stderr, flush=True)
        safe_lines.append(fallback)
        stderr = "\n".join(safe_lines)
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


@dataclass(frozen=True)
class SearchRequest:
    source: str
    title: str
    performer: str
    run_name: str | None = None
    candidate_index: int | None = None
    period: dict | None = None
    iswc: str | None = None

    def payload(self) -> dict:
        payload = {
            "source": self.source,
            "title": self.title,
            "performer": self.performer,
        }
        if self.run_name is not None:
            payload["run_name"] = self.run_name
        if self.candidate_index is not None:
            payload["candidate_index"] = self.candidate_index
        if self.period is not None:
            payload["period"] = self.period
        if self.iswc is not None:
            payload["iswc"] = self.iswc
        return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _normalise_text(value: str, label: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise CisnetCommandError(f"{label} must not be empty")
    return value


def _usable_iswc(value: object) -> str | None:
    if isinstance(value, list):
        return next((code for item in value if (code := _usable_iswc(item))), None)
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if re.fullmatch(r"T-\d{3}\.\d{3}\.\d{3}-\d", code):
        return code
    if re.fullmatch(r"T\d{10}", code):
        digits = code[1:]
        return f"T-{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9]}"
    return None


def _json_load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CisnetCommandError(f"cannot read {path}: {error}") from error


def aggregation_runs(data_root: Path) -> list[str]:
    root = data_root / "aggregation"
    if not root.exists():
        return []
    runs = [
        item.name for item in root.iterdir()
        if item.is_dir() and (item / "result.json").is_file()
    ]

    def sort_key(name: str):
        parts = name.split("_")
        return tuple(int(part) for part in parts) if all(part.isdecimal() for part in parts) else (sys.maxsize, name)

    return sorted(runs, key=sort_key)


def requests_from_aggregation(data_root: Path, run_name: str, positions: list[int] | None) -> list[SearchRequest]:
    if Path(run_name).name != run_name or not run_name:
        raise CisnetCommandError("run name must be a single aggregation directory name")
    result_path = data_root / "aggregation" / run_name / "result.json"
    records = _json_load(result_path)
    if not isinstance(records, list):
        raise CisnetCommandError(f"{result_path} must contain a list of candidates")
    if positions is None:
        positions = list(range(1, len(records) + 1))
    if not positions:
        raise CisnetCommandError("aggregation run has no candidates")
    requests = []
    for position in positions:
        if position < 1 or position > len(records):
            raise CisnetCommandError(f"candidate {position} is outside 1..{len(records)}")
        record = records[position - 1]
        if not isinstance(record, dict):
            raise CisnetCommandError(f"candidate {position} is not an object")
        title = _normalise_text(str(record.get("title") or ""), f"candidate {position} title")
        artists = record.get("artist")
        if isinstance(artists, list):
            performer = "; ".join(str(item).strip() for item in artists if str(item).strip())
        else:
            performer = str(artists or "")
        requests.append(SearchRequest(
            source="aggregation",
            title=title,
            performer=_normalise_text(performer, f"candidate {position} performer"),
            run_name=run_name,
            candidate_index=position,
            period=record.get("period") if isinstance(record.get("period"), dict) else None,
            iswc=_usable_iswc(record.get("iswc")),
        ))
    return sorted(requests, key=lambda request: request.iswc is not None)


def manual_request(title: str, performer: str, iswc: str | None = None) -> SearchRequest:
    usable_iswc = _usable_iswc(iswc)
    if iswc and usable_iswc is None:
        raise CisnetCommandError("ISWC must have the form T-123.456.789-0")
    return SearchRequest(
        source="manual",
        title=_normalise_text(title, "title"),
        performer=_normalise_text(performer, "performer"),
        iswc=usable_iswc,
    )


def _query_key(request: SearchRequest) -> str:
    identity = request.payload()
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _root_name(request: SearchRequest, now: str) -> str:
    return f"{request.run_name}_CISNET" if request.run_name else f"MANUAL_{now}_CISNET"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _archive_artifact(job_dir: Path, filename: str, now: str) -> Path | None:
    source_path = job_dir / filename
    if not source_path.exists():
        return None
    attempts_root = job_dir / "attempts"
    attempt_dir = attempts_root / now
    suffix = 2
    while attempt_dir.exists():
        attempt_dir = attempts_root / f"{now}-{suffix}"
        suffix += 1
    attempt_dir.mkdir(parents=True)
    archived_path = attempt_dir / filename
    os.replace(source_path, archived_path)
    return archived_path


def create_job(data_root: Path, request: SearchRequest, now: str | None = None, overwrite_result: bool = False) -> tuple[Path, Path]:
    now = now or _utc_now()
    root = data_root / "cisnet" / _root_name(request, now)
    job_dir = root / _query_key(request)
    request_path = job_dir / "request.json"
    if (job_dir / "result.json").exists() and not overwrite_result:
        raise CisnetCommandError(f"CIS-Net result already exists for {job_dir.name}; it will not be overwritten")
    archived_error = _archive_artifact(job_dir, "error.json", now)
    if archived_error is not None:
        print(f"Previous CIS-Net error preserved: {archived_error}")
    payload = {"schema_version": "cisnet-search-request/v1", **request.payload()}
    _atomic_json(request_path, payload)
    return job_dir, request_path


def execute_search(request_path: Path, adapter: Path) -> dict:
    completed = _run_adapter_streaming(["node", str(adapter), "--request", str(request_path)])
    code = next((
        json.loads(line[len(PLAYWRIGHT_LOG_PREFIX):])["code"]
        for line in reversed(safe_playwright_log_lines(completed.stderr))
        if json.loads(line[len(PLAYWRIGHT_LOG_PREFIX):])["event"] == "failed"
    ), None)
    if completed.returncode:
        raise CisnetCommandError(f"Playwright search failed ({code})", code=code)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CisnetCommandError("Playwright adapter returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise CisnetCommandError("Playwright adapter returned a non-object result")
    return payload


def session_action(action: str, adapter: Path) -> None:
    completed = _run_adapter_streaming(["node", str(adapter), f"--{action}"])
    code = next((
        json.loads(line[len(PLAYWRIGHT_LOG_PREFIX):])["code"]
        for line in reversed(safe_playwright_log_lines(completed.stderr))
        if json.loads(line[len(PLAYWRIGHT_LOG_PREFIX):])["event"] == "failed"
    ), None)
    if completed.returncode:
        raise CisnetCommandError(f"CIS-Net {action} failed ({code})", code=code)


def _print_request(request: SearchRequest, destination: Path) -> None:
    print(f"Title: {request.title}")
    print(f"Performer: {request.performer}")
    if request.iswc:
        print(f"Search by ISWC: {request.iswc}")
    print(f"Destination: {destination}")


def run_requests(
    data_root: Path,
    requests: list[SearchRequest],
    execute: bool,
    adapter: Path,
    overwrite_results: bool = False,
    confirm_overwrite=None,
) -> int:
    pending: list[tuple[SearchRequest, str, bool]] = []
    seen_searches: set[tuple[str, str, str | None]] = set()
    for request in requests:
        identity = (request.title, request.performer, request.iswc)
        if identity in seen_searches:
            print(f"Skipped: identical CIS-Net search for candidate {request.candidate_index}")
            continue
        seen_searches.add(identity)
        now = _utc_now()
        destination = data_root / "cisnet" / _root_name(request, now) / _query_key(request)
        _print_request(request, destination)
        if not execute:
            print("Dry run: CIS-Net was not opened.")
            continue
        result_path = destination / "result.json"
        overwrite_result = result_path.exists() and (
            overwrite_results or (confirm_overwrite is not None and confirm_overwrite(request, result_path))
        )
        if result_path.exists() and not overwrite_result:
            print(f"Skipped: existing CIS-Net result preserved: {result_path}")
            continue
        pending.append((request, now, overwrite_result))
    if not execute or not pending:
        return 0
    session_started = False
    try:
        session_action("start", adapter)
        session_started = True
        for request, now, overwrite_result in pending:
            job_dir, request_path = create_job(data_root, request, now, overwrite_result=overwrite_result)
            result_path = job_dir / "result.json"
            try:
                result = execute_search(request_path, adapter)
            except CisnetCommandError as error:
                _atomic_json(job_dir / "error.json", {"message": str(error)})
                raise
            archived_result = _archive_artifact(job_dir, "result.json", now) if overwrite_result else None
            if archived_result is not None:
                print(f"Previous CIS-Net result preserved: {archived_result}")
            _atomic_json(result_path, result)
            print(f"Saved: {result_path}")
    finally:
        if session_started:
            session_action("finish", adapter)
    return 0


def _parse_positions(value: str) -> list[int] | None:
    if value.strip().lower() == "all":
        return None
    try:
        positions = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError("positions must be comma-separated positive numbers or all") from error
    if not positions or any(item < 1 for item in positions) or len(set(positions)) != len(positions):
        raise argparse.ArgumentTypeError("positions must be unique positive numbers or all")
    return positions


def _interactive(data_root: Path, adapter: Path, execute_allowed: bool) -> int:
    print("Source: [1] aggregation run  [2] manual title and performer")
    choice = input("> ").strip()
    if choice == "1":
        runs = aggregation_runs(data_root)
        if not runs:
            raise CisnetCommandError("no aggregation runs with result.json")
        print("Available aggregation runs:")
        print(" ".join(f"{index}){name}" for index, name in enumerate(runs, 1)))
        try:
            run_name = runs[int(input("Run number: ").strip()) - 1]
        except (ValueError, IndexError):
            raise CisnetCommandError("choose a listed run number") from None
        candidates = _json_load(data_root / "aggregation" / run_name / "result.json")
        if not isinstance(candidates, list):
            raise CisnetCommandError("aggregation result must be a list")
        for index, candidate in enumerate(candidates, 1):
            title = candidate.get("title", "") if isinstance(candidate, dict) else ""
            artist = candidate.get("artist", []) if isinstance(candidate, dict) else []
            print(f"{index}. {title} — {'; '.join(artist) if isinstance(artist, list) else artist}")
        requests = requests_from_aggregation(data_root, run_name, _parse_positions(input("Candidates (e.g. 1,3 or all): ")))
    elif choice == "2":
        requests = [manual_request(input("Title: "), input("Performer: "), input("ISWC (optional): ").strip() or None)]
    else:
        raise CisnetCommandError("choose 1 or 2")
    execute = execute_allowed and input("Run CIS-Net search? Type EXECUTE: ").strip() == "EXECUTE"
    if not execute_allowed:
        print("Dry run: restart with --execute to allow a CIS-Net search.")
    def confirm_overwrite(request: SearchRequest, result_path: Path) -> bool:
        answer = input(f"Result already exists for {request.title} — overwrite it? Type OVERWRITE: ").strip()
        return answer == "OVERWRITE"
    return run_requests(data_root, requests, execute, adapter, confirm_overwrite=confirm_overwrite)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manually stage selected aggregation candidates for CIS-Net search")
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--adapter", type=Path, default=Path("scripts/cisnet_search_works.js"))
    parser.add_argument("--execute", "--exe", dest="interactive_execute", action="store_true", help="for interactive mode, allow a live CIS-Net search")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="select candidates from one aggregation run")
    run.add_argument("run_name")
    run.add_argument("--candidates", required=True, type=_parse_positions, help="comma-separated candidate numbers or all")
    run.add_argument("--execute", "--exe", action="store_true", help="perform the CIS-Net searches")
    run.add_argument("--overwrite-results", action="store_true", help="overwrite existing results after preserving their previous files")
    manual = commands.add_parser("manual", help="search manually supplied title and performer")
    manual.add_argument("--title", required=True)
    manual.add_argument("--performer", required=True)
    manual.add_argument("--iswc", help="search by this ISWC instead of title and performer")
    manual.add_argument("--execute", "--exe", action="store_true", help="perform the CIS-Net search")
    manual.add_argument("--overwrite-results", action="store_true", help="overwrite an existing result after preserving its previous file")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    data_root = args.data_root.resolve()
    adapter = args.adapter.resolve()
    try:
        if args.command is None:
            if not sys.stdin.isatty():
                parser.error("choose run or manual when stdin is not interactive")
            return _interactive(data_root, adapter, args.interactive_execute)
        if args.command == "run":
            requests = requests_from_aggregation(data_root, args.run_name, args.candidates)
            return run_requests(data_root, requests, args.execute, adapter, overwrite_results=args.overwrite_results)
        request = manual_request(args.title, args.performer, args.iswc)
        return run_requests(data_root, [request], args.execute, adapter, overwrite_results=args.overwrite_results)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except (CisnetCommandError, OSError) as error:
        print(f"yougile-cisnet: {error}", file=sys.stderr)
        return 75 if isinstance(error, CisnetCommandError) and error.code == "BUSY_SESSION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
