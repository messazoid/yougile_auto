#!/usr/bin/env python3
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time
import wave

import scan
from job_store import (
    PipelineStore, StoreError, TERMINAL_RECOGNITION_STATES, is_forced_run_prep_hash,
)
from aggregation import runner as aggregation_runner


BASE_DIR = Path(__file__).resolve().parent.parent
PIPELINE_DB = BASE_DIR / "data" / "queue" / "pipeline.sqlite3"
RUNS_DIR = BASE_DIR / "data" / "recognition-runs"
WORKER_LOCK = BASE_DIR / "data" / "queue" / "recognition-worker.lock"


class WorkerConfigError(RuntimeError):
    pass


class WorkerConfig:
    def __init__(self, enabled, access_key="", secret_key="", host=scan.DEFAULT_HOST,
                 runs_dir=RUNS_DIR, max_retries=2, retry_base_seconds=2.0,
                 retry_max_seconds=30.0):
        self.enabled = bool(enabled)
        self.access_key = access_key
        self.secret_key = secret_key
        self.host = host
        self.runs_dir = Path(runs_dir)
        self.max_retries = int(max_retries)
        self.retry_base_seconds = float(retry_base_seconds)
        self.retry_max_seconds = float(retry_max_seconds)
        self.validate()

    def __repr__(self):
        return (
            f"WorkerConfig(enabled={self.enabled},host={self.host!r},"
            "credentials='[REDACTED]')"
        )

    def validate(self):
        if not re.fullmatch(r"identify-[a-z0-9-]+\.acrcloud\.com", self.host):
            raise WorkerConfigError("ACR_HOST must be an identify-*.acrcloud.com hostname")
        if not 0 <= self.max_retries <= 5:
            raise WorkerConfigError("ACR_MAX_RETRIES must be between 0 and 5")
        if not 0 <= self.retry_base_seconds <= self.retry_max_seconds <= 300:
            raise WorkerConfigError("Invalid ACR retry delays")
        if not self.enabled:
            return
        for name, value in (("ACR_ACCESS_KEY", self.access_key), ("ACR_SECRET_KEY", self.secret_key)):
            if not value or not value.isascii() or any(char.isspace() for char in value):
                raise WorkerConfigError(f"{name} must be nonempty ASCII without whitespace")

    @classmethod
    def from_environment(cls):
        enabled = os.getenv("ACR_EXECUTE", "0") == "1"

        return cls(
            enabled=enabled,
            access_key=os.getenv("ACR_ACCESS_KEY", ""),
            secret_key=os.getenv("ACR_SECRET_KEY", ""),
            host=os.getenv("ACR_HOST", scan.DEFAULT_HOST),
            max_retries=os.getenv("ACR_MAX_RETRIES", "2"),
            retry_base_seconds=os.getenv("ACR_RETRY_BASE_SECONDS", "2"),
            retry_max_seconds=os.getenv("ACR_RETRY_MAX_SECONDS", "30"),
        )


def config_hash(config: dict) -> str:
    raw = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def scope_run_token(prep_config_hash: str) -> str | None:
    marker = ":scope-run:"
    return prep_config_hash.split(marker, 1)[1] if marker in prep_config_hash else None


def scanner_config_for_run(info: dict, cfg: WorkerConfig, sdk_info: dict, token=None) -> dict:
    persisted = scan.scanner_config(info, cfg.host, sdk_info)
    if token:
        persisted["pipeline_scope_run"] = token
    return persisted


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def export_links(store: PipelineStore, recognition_id: int, output: Path) -> None:
    links = store.links_for_recognition(recognition_id)
    scan.atomic_text(
        output / "message_links.jsonl",
        [json.dumps(link, ensure_ascii=False, sort_keys=True) + "\n" for link in links],
    )
    manifests = store.source_manifests_for_recognition(recognition_id)
    if manifests:
        atomic_json(output / "sources.json", {
            "schema_version": 1,
            "logical_sources": manifests,
        })
    store.mark_links_clean(recognition_id)


def queue_recognition_notification(
    store: PipelineStore,
    recognition_id: int,
    kind: str,
    suffix: str,
    text: str,
) -> None:
    """Queue one idempotent status message for every linked YouGile chat."""
    for link in store.links_for_recognition(recognition_id):
        chat_id = link.get("chat_id")
        if not chat_id:
            continue
        try:
            store.enqueue_chat_notification(
                str(chat_id), kind, f"{kind}:{chat_id}:{recognition_id}:{suffix}", text
            )
        except Exception as error:
            print(
                f"[NOTIFY] recognition_id={recognition_id} kind={kind} "
                f"stage=enqueue_error type={type(error).__name__}",
                flush=True,
            )


def result_state(summary: dict) -> str:
    if summary.get("candidate_rows", 0) > 0:
        return "complete_candidates"
    if summary.get("no_match_windows", 0) > 0:
        return "complete_no_match"
    return "complete_local_only"


def stopped_state(db: sqlite3.Connection, summary: dict) -> tuple[str, str]:
    states = [row[0] for row in db.execute("SELECT state FROM attempts ORDER BY id")]
    if any(state in {"sending", "uncertain"} for state in states):
        return "uncertain", "UncertainAttempt"
    if any(state == "error" for state in states):
        return "api_error", "ApiResponseError"
    return "partial", "ScanStopped"


def schedule_aggregation(store: PipelineStore, recognition_id: int) -> None:
    """Schedule aggregation after an already durable successful recognition."""
    try:
        aggregation_runner.ensure_aggregation_for_recognition(store, recognition_id)
    except Exception as error:
        # This is deliberately outside recognition completion semantics: a
        # later reconciliation can repair the cross-database crash gap.
        print(
            f"[AGGREGATION] recognition_id={recognition_id} stage=schedule_error "
            f"error_type={type(error).__name__}",
            flush=True,
        )


def reconcile_aggregation(store: PipelineStore) -> None:
    try:
        aggregation_runner.reconcile_aggregation(store, limit=1)
    except Exception as error:
        print(f"[AGGREGATION] stage=reconcile_error error_type={type(error).__name__}", flush=True)


def process_recognition(store: PipelineStore, claimed: dict, cfg: WorkerConfig,
                        transport=scan.send_sample, fingerprinter=scan.fingerprint_sample,
                        max_requests=None) -> str:
    recognition_id = claimed["id"]
    links = store.links_for_recognition(recognition_id)
    source_ids = [str(source_id) for source_id in store.source_ids_for_recognition(recognition_id)]
    task_ids = sorted({str(link["task_id"]) for link in links if link.get("task_id")})
    message_ids = sorted({str(link["message_id"]) for link in links if link.get("message_id")})
    context = (
        f" recognition_id={recognition_id} source_job={','.join(source_ids) or '-'}"
        f" task_id={','.join(task_ids) or '-'} message_id={','.join(message_ids) or '-'}"
    )
    print(f"[RECOGNITION]{context} stage=started", flush=True)
    resume_limited = bool(claimed.get("resume_limited"))
    queue_recognition_notification(
        store,
        recognition_id,
        "scanning",
        f"resume-{claimed.get('attempts_used', 0)}" if resume_limited else "initial",
        "Сканирование",
    )
    audio = Path(claimed["audio_path"]).resolve(strict=True)
    output = Path(claimed["result_dir"]).resolve()
    runs_root = cfg.runs_dir.resolve()
    if output.parent != runs_root:
        raise StoreError("Recognition output is outside the automated runs directory")
    os.umask(0o077)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with audio.open("rb") as source:
        info = scan.inspect_audio(source)
        _, sdk_info = scan.load_sdk()
        persisted_config = scanner_config_for_run(
            info, cfg, sdk_info, store.recognition_scope_run_token(recognition_id)
        )
        if config_hash(persisted_config) != claimed["config_hash"]:
            raise StoreError("Recognition configuration changed after enqueue")
        scan.check_output(output, persisted_config)
        total = len(list(scan.windows(info["frames"], info["sample_rate"])))
        with (output / "scan.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreError("Recognition output is already locked") from None
            with contextlib.closing(sqlite3.connect(output / "scan.sqlite3")) as db:
                db.row_factory = sqlite3.Row
                scan.initialize_db(db, persisted_config)
                already = db.execute(
                    "SELECT COUNT(*) FROM (SELECT window_index FROM attempts WHERE state='done' "
                    "UNION SELECT window_index FROM local_windows)"
                ).fetchone()[0]
                if already == total:
                    summary = scan.export_results(db, output, total)
                    state = result_state(summary)
                    store.complete_recognition(recognition_id, claimed["claim_token"], state, summary)
                    schedule_aggregation(store, recognition_id)
                    export_links(store, recognition_id, output)
                    atomic_json(output / "job.json", {
                        "recognition_id": recognition_id, "state": state,
                        "audio_sha256": claimed["audio_sha256"], "config": persisted_config,
                        "candidates_verified": False,
                    })
                    return state

                def budgeted_transport(host, fingerprint, key, secret):
                    attempt_id = store.reserve_request(recognition_id)
                    try:
                        response = transport(host, fingerprint, key, secret)
                    except BaseException:
                        store.finish_request(attempt_id, "uncertain")
                        raise
                    store.finish_request(attempt_id, "done")
                    return response

                try:
                    source.seek(0)
                    with wave.open(source, "rb") as reader:
                        scan.scan(
                            db, reader, persisted_config, cfg.access_key, cfg.secret_key,
                            max_requests, resume_limited, transport=budgeted_transport,
                            fingerprinter=fingerprinter,
                            max_retries=0 if resume_limited else cfg.max_retries,
                            retry_base_seconds=cfg.retry_base_seconds,
                            retry_max_seconds=cfg.retry_max_seconds,
                            log_context=context,
                        )
                    summary = scan.export_results(db, output, total)
                    state, error = result_state(summary), None
                except scan.ScanStop:
                    summary = scan.export_results(db, output, total)
                    state, error = stopped_state(db, summary)
                    latest_error = db.execute(
                        "SELECT acr_code FROM attempts WHERE state='error' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    limited = bool(
                        latest_error
                        and latest_error[0] == scan.ACR_REQUEST_COUNT_LIMIT_CODE
                    )
                store.complete_recognition(
                    recognition_id, claimed["claim_token"], state, summary, error
                )
                if state == "api_error" and limited:
                    queue_recognition_notification(
                        store,
                        recognition_id,
                        "acr_limit",
                        f"3003-{summary.get('attempts', 0)}",
                        "Исчерпан лимит ACRCloud",
                    )
                if state in aggregation_runner.SUCCESSFUL_RECOGNITION_STATES:
                    schedule_aggregation(store, recognition_id)
                export_links(store, recognition_id, output)
                atomic_json(output / "job.json", {
                    "recognition_id": recognition_id, "state": state,
                    "audio_sha256": claimed["audio_sha256"], "config": persisted_config,
                    "summary": summary, "candidates_verified": False,
                })
                print(
                    f"[RECOGNITION]{context} stage={state} "
                    f"windows={summary.get('completed_windows')} attempts={summary.get('attempts')}",
                    flush=True,
                )
                return state


def attach_one_audio(store: PipelineStore, cfg: WorkerConfig) -> str:
    source = store.claim_audio_for_recognition("acr-worker")
    if not source:
        return "idle"
    try:
        audio = Path(source["wav_path"]).resolve(strict=True)
        with audio.open("rb") as handle:
            info = scan.inspect_audio(handle)
        _, sdk_info = scan.load_sdk()
        persisted_config = scanner_config_for_run(
            info, cfg, sdk_info, scope_run_token(source["prep_config_hash"])
        )
        forced_replay = is_forced_run_prep_hash(source["prep_config_hash"])
        if forced_replay:
            persisted_config["pipeline_forced_replay_source_job"] = source["id"]
        digest = config_hash(persisted_config)
        suffix = f"-forced-replay-{source['id']}" if forced_replay else ""
        output = cfg.runs_dir / f"{info['sha256'][:24]}-{digest[:16]}{suffix}"
        recognition_id = store.attach_recognition(
            source["id"], source["claim_token"], digest, output
        )
        print(
            f"[RECOGNITION] recognition_id={recognition_id} source_job={source['id']} "
            "stage=attached",
            flush=True,
        )
        recognition = store.recognition(recognition_id)
        if recognition["state"] in TERMINAL_RECOGNITION_STATES:
            export_links(store, recognition_id, Path(recognition["result_dir"]))
        return "attached"
    except Exception as error:
        store.fail_recognition_attachment(
            source["id"], source["claim_token"], type(error).__name__
        )
        return "recognition_error"


def run_once(store: PipelineStore, cfg: WorkerConfig, transport=scan.send_sample,
             fingerprinter=scan.fingerprint_sample, max_requests=None) -> str:
    if not cfg.enabled:
        return "disabled"
    store.request_usage()
    reconcile_aggregation(store)
    attachment = attach_one_audio(store, cfg)
    claimed = store.claim_recognition("acr-worker", lease_seconds=86400)
    if not claimed:
        dirty = store.dirty_recognition()
        if dirty:
            export_links(store, dirty["id"], Path(dirty["result_dir"]))
            return "links_refreshed"
        if attachment == "recognition_error":
            return attachment
        if attachment == "attached":
            return "linked"
        return aggregation_runner.run_one_aggregation(store)
    try:
        state = process_recognition(
            store, claimed, cfg, transport, fingerprinter, max_requests=max_requests
        )
        aggregation_runner.run_one_aggregation(store)
        return state
    except Exception as error:
        summary = {
            "schema_version": 2, "data_type": "fingerprint",
            "total_windows": None, "completed_windows": 0, "processed_windows": 0,
            "locally_skipped_windows": 0, "remaining_windows": None, "attempts": 0,
            "windows_with_music_candidates": 0, "no_match_windows": 0,
            "candidate_rows": 0, "complete": False, "all_windows_submitted": False,
            "note": "Recognition stopped before a complete scanner export; no automatic retry.",
        }
        output = Path(claimed["result_dir"])
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        scan.atomic_text(output / "summary.json", [json.dumps(summary, ensure_ascii=False, indent=2) + "\n"])
        store.complete_recognition(
            claimed["id"], claimed["claim_token"], "partial", summary, type(error).__name__
        )
        export_links(store, claimed["id"], output)
        atomic_json(output / "job.json", {
            "recognition_id": claimed["id"], "state": "partial",
            "audio_sha256": claimed["audio_sha256"],
            "error_type": type(error).__name__, "candidates_verified": False,
        })
        return "partial"


def stop_signal(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Persistent ACRCloud recognition worker")
    parser.add_argument("--once", action="store_true", help="Process at most one available recognition")
    parser.add_argument(
        "--max-requests", type=int,
        help="One-shot total request cap for a controlled live test",
    )
    args = parser.parse_args(argv)
    if args.max_requests is not None and (not args.once or args.max_requests < 1):
        parser.error("--max-requests requires --once and a positive value")
    cfg = WorkerConfig.from_environment()
    store = PipelineStore(PIPELINE_DB)
    store.initialize()
    WORKER_LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    with WORKER_LOCK.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerConfigError("Another recognition worker holds the global lock") from None
        store.recover_worker_claims()
        if not cfg.enabled:
            print("[WAIT] ACR worker is disabled; ACR_EXECUTE is not 1.", flush=True)
            return 0
        signal.signal(signal.SIGTERM, stop_signal)
        signal.signal(signal.SIGHUP, stop_signal)
        while True:
            state = run_once(store, cfg, max_requests=args.max_requests)
            if args.once:
                print(f"[WORKER] state={state}", flush=True)
                return 0
            if state == "idle":
                time.sleep(2)
            else:
                print(f"[WORKER] state={state}", flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[STOP] Worker interrupted; persisted state will be inspected before any retry.", flush=True)
        sys.exit(130)
    except (WorkerConfigError, StoreError) as error:
        print(f"[STOP] {type(error).__name__}; manual review required.", flush=True)
        sys.exit(2)
    except Exception as error:
        print(f"[STOP] {type(error).__name__}; no automatic retry.", flush=True)
        sys.exit(2)
