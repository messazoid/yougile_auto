#!/usr/bin/env python3
"""Process new aggregation exports and queue completed CIS-Net results for YouGile.

This module does not post to YouGile directly. The receiver delivers queued
notifications when its service is running.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess

from cisnet_cli import CisnetCommandError, requests_from_aggregation, safe_playwright_log_lines
from cisnet_store import (
    baseline, busy_retry_remaining, clear_busy_session, defer_busy_session,
    eligible_runs, finalize_run, mark_run_error, record_error, record_result,
    searches, stage_run,
)
from job_store import PipelineStore


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_ROOT = BASE_DIR / "data"
DB_PATH = DATA_ROOT / "queue" / "pipeline.sqlite3"
WRAPPER = BASE_DIR / "yougile-cisnet"


def initialize(db_path: Path, data_root: Path) -> dict:
    store = PipelineStore(db_path)
    store.initialize()
    return baseline(store, data_root / "cisnet" / "automation" / "baseline.json", initialize=True)


def _sync_artifacts(store: PipelineStore, data_root: Path, run_name: str, run_id: int) -> None:
    for item in searches(store, run_id):
        if item["state"] != "pending":
            continue
        result_path = data_root / "cisnet" / f"{run_name}_CISNET" / item["artifact_key"] / "result.json"
        if not result_path.is_file():
            continue
        try:
            record_result(store, item["id"], result_path)
        except (OSError, ValueError) as error:
            record_error(store, item["id"], type(error).__name__)


def queue_ready_notifications(store: PipelineStore, run_id: int) -> int:
    """Queue one result per linked chat, including after an interrupted run."""
    with store.connect() as db:
        row = db.execute(
            "SELECT recognition_id,state,message_text FROM cisnet_runs WHERE aggregation_run_id=?",
            (run_id,),
        ).fetchone()
    if not row or row["state"] != "ready":
        return 0
    if not row["message_text"]:
        raise ValueError("Ready CIS-Net run has no message")
    chat_ids = sorted({
        str(link["chat_id"]) for link in store.links_for_recognition(row["recognition_id"])
        if link.get("chat_id")
    })
    text = "Результаты CIS-Net:\n" + row["message_text"]
    return sum(store.enqueue_chat_notification(
        chat_id, "cisnet_result", f"cisnet-result:{run_id}:{chat_id}", text
    ) for chat_id in chat_ids)


def queue_started_notifications(store: PipelineStore, run_id: int) -> int:
    """Queue one start notice when the first real CIS-Net search begins."""
    with store.connect() as db:
        row = db.execute(
            "SELECT recognition_id FROM cisnet_runs WHERE aggregation_run_id=?",
            (run_id,),
        ).fetchone()
    if not row:
        return 0
    chat_ids = sorted({
        str(link["chat_id"]) for link in store.links_for_recognition(row["recognition_id"])
        if link.get("chat_id")
    })
    return sum(store.enqueue_chat_notification(
        chat_id, "cisnet_started", f"cisnet-started:{run_id}:{chat_id}",
        "Поиск CIS-Net начался",
    ) for chat_id in chat_ids)


def _run_wrapper(wrapper: Path, run_name: str, on_log) -> subprocess.CompletedProcess:
    """Run the CLI and forward its allow-listed Playwright events as they arrive."""
    command = [str(wrapper), "run", run_name, "--candidates", "all", "--exe"]
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    safe_lines: list[str] = []
    assert process.stderr is not None
    for raw_line in process.stderr:
        for line in safe_playwright_log_lines(raw_line):
            safe_lines.append(line)
            on_log(line)
    returncode = process.wait()
    return subprocess.CompletedProcess(command, returncode, "", "\n".join(safe_lines))


def run_pending(db_path: Path = DB_PATH, data_root: Path = DATA_ROOT,
                wrapper: Path = WRAPPER) -> int:
    store = PipelineStore(db_path)
    for run_id, recognition_id in eligible_runs(store):
        with store.connect() as db:
            row = db.execute("SELECT state FROM cisnet_runs WHERE aggregation_run_id=?", (run_id,)).fetchone()
        if row and row["state"] in {"ready", "empty", "error"}:
            if row["state"] == "ready":
                queue_ready_notifications(store, run_id)
            continue
        run_name = f"{recognition_id}_{run_id}"
        if not (data_root / "aggregation" / run_name / "result.json").is_file():
            continue
        try:
            requests = requests_from_aggregation(data_root, run_name, None)
        except CisnetCommandError as error:
            if str(error) == "aggregation run has no candidates":
                requests = []
            else:
                mark_run_error(store, run_id, recognition_id)
                print(f"[CISNET] run={run_name} state=error stage=prepare", flush=True)
                continue
        stage_run(store, run_id, recognition_id, requests)
        _sync_artifacts(store, data_root, run_name, run_id)
        state = finalize_run(store, run_id)
        if state != "pending":
            if state == "ready":
                queue_ready_notifications(store, run_id)
            print(f"[CISNET] run={run_name} state={state}", flush=True)
            continue
        retry_in = busy_retry_remaining(store, run_id)
        if retry_in:
            print(f"[CISNET] run={run_name} state=deferred_busy retry_in_seconds={retry_in}", flush=True)
            return 0
        clear_busy_session(store, run_id)
        # One login/browser session per run; the existing command skips files
        # already captured before an interruption.
        started_notified = False

        def on_playwright_log(line: str) -> None:
            nonlocal started_notified
            print(line, flush=True)
            event = json.loads(line[len("[CISNET-PLAYWRIGHT] "):])
            if not started_notified and event == {"step": "search.request", "event": "begin"}:
                queue_started_notifications(store, run_id)
                started_notified = True

        completed = _run_wrapper(wrapper, run_name, on_playwright_log)
        _sync_artifacts(store, data_root, run_name, run_id)
        if completed.returncode == 75:
            defer_busy_session(store, run_id)
            print(f"[CISNET] run={run_name} state=deferred_busy retry_in_seconds=300", flush=True)
            return 0
        for item in searches(store, run_id):
            if item["state"] == "pending":
                record_error(store, item["id"], "SearchCommandFailed" if completed.returncode else "MissingResult")
        state = finalize_run(store, run_id)
        if state == "ready":
            queue_ready_notifications(store, run_id)
        print(f"[CISNET] run={run_name} state={state} exit_code={completed.returncode}", flush=True)
        if state == "error":
            return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and process CIS-Net checks for new aggregation exports")
    parser.add_argument("--initialize", action="store_true", help="migrate CIS-Net state and preserve the old baseline")
    args = parser.parse_args()
    try:
        if args.initialize:
            state = initialize(DB_PATH, DATA_ROOT)
            print(f"CIS-Net database baseline ready at aggregation run {state['max_existing_run_id']}")
            return 0
        return run_pending()
    except (CisnetCommandError, OSError, sqlite3.Error, ValueError, KeyError) as error:
        print(f"[CISNET] automation_error type={type(error).__name__}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
