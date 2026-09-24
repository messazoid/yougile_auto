import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 12
MIGRATABLE_SCHEMA_VERSIONS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, SCHEMA_VERSION}
TERMINAL_RECOGNITION_STATES = {
    "complete_candidates",
    "complete_no_match",
    "complete_local_only",
    "partial",
    "api_error",
    "limit",
    "uncertain",
}
TERMINAL_SOURCE_STATES = TERMINAL_RECOGNITION_STATES | {
    "audio_error",
    "recognition_error",
}

AGGREGATION_RUN_STATES = {"pending", "running", "complete", "error"}
CHAT_NOTIFICATION_PREFIX = "Сервер: "
FORCED_RUN_MARKER = ":forced-run:"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_hash(*values: object) -> str:
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def forced_run_prep_hash(prep_hash: str, task_id: str, chat_id: str, message_id: str) -> str:
    """Create one idempotent, explicitly separate preparation generation."""
    if not all(isinstance(value, str) and value for value in (prep_hash, task_id, chat_id, message_id)):
        raise ValueError("Invalid forced-run identity")
    return f"{prep_hash}{FORCED_RUN_MARKER}{stable_hash(task_id, chat_id, message_id)}"


def is_forced_run_prep_hash(prep_hash: str) -> bool:
    return FORCED_RUN_MARKER in prep_hash


def canonical_json_text(value: str | object) -> str:
    """Return deterministic JSON suitable for durable semantic hashes."""
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid JSON value") from error
    return json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_sha256(value: str | object) -> str:
    return hashlib.sha256(canonical_json_text(value).encode("utf-8")).hexdigest()


def normalize_source(source) -> dict:
    if isinstance(source, tuple) and len(source) == 2:
        source = {"kind": source[0], "url": source[1]}
    if not isinstance(source, dict):
        raise ValueError("Invalid source description")
    kind = source.get("kind")
    url = source.get("url")
    item_path = source.get("item_path")
    items = source.get("items")
    if not isinstance(kind, str) or not kind or not isinstance(url, str) or not url:
        raise ValueError("Invalid source description")
    if item_path is not None and not isinstance(item_path, str):
        raise ValueError("Invalid source item path")
    if items is not None:
        if kind != "yandex_disk_group" or not isinstance(items, list) or not items:
            raise ValueError("Invalid grouped source items")
        normalized_items = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid grouped source item")
            path = item.get("item_path")
            filename = item.get("filename")
            if not isinstance(path, str) or not path or not isinstance(filename, str) or not filename:
                raise ValueError("Invalid grouped source item")
            normalized_items.append({
                "item_path": path,
                "filename": filename,
                "size": int(item.get("size") or 0),
                "unlimited": bool(item.get("unlimited", True)),
            })
        items = normalized_items
    identity = url if item_path is None else json.dumps(
        [url, item_path], ensure_ascii=False, separators=(",", ":")
    )
    return {
        "kind": kind,
        "url": url,
        "item_path": item_path,
        "filename": source.get("filename") if isinstance(source.get("filename"), str) else None,
        "size": int(source.get("size") or 0),
        "unlimited": bool(source.get("unlimited")),
        "file_id": source.get("file_id") if isinstance(source.get("file_id"), str) else None,
        "items_json": (
            json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if items is not None else None
        ),
        "source_hash": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    }


class StoreError(RuntimeError):
    pass


class PipelineStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with self.connect() as db:
            existing_version = None
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_info'"
            ).fetchone():
                row = db.execute("SELECT version FROM schema_info WHERE id=1").fetchone()
                existing_version = row[0] if row else None
                if existing_version not in MIGRATABLE_SCHEMA_VERSIONS:
                    raise StoreError("Unsupported pipeline database schema; no automatic migration performed.")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_key TEXT NOT NULL UNIQUE,
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    event_name TEXT,
                    company_id TEXT,
                    chat_id TEXT,
                    message_id TEXT,
                    stage TEXT NOT NULL,
                    error_type TEXT,
                    delivery_count INTEGER NOT NULL DEFAULT 1,
                    received_utc TEXT NOT NULL,
                    last_received_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    started_utc TEXT,
                    completed_utc TEXT,
                    claim_token TEXT,
                    claim_until REAL
                );
                CREATE TABLE IF NOT EXISTS source_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_hash TEXT NOT NULL,
                    prep_config_hash TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error_type TEXT,
                    audio_attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_epoch REAL NOT NULL DEFAULT 0,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    started_utc TEXT,
                    audio_completed_utc TEXT,
                    completed_utc TEXT,
                    claim_token TEXT,
                    claim_until REAL,
                    source_filename TEXT,
                    source_size INTEGER,
                    source_item_path TEXT,
                    source_items_json TEXT,
                    source_manifest_json TEXT,
                    source_unlimited INTEGER NOT NULL DEFAULT 0,
                    local_source_path TEXT,
                    wav_path TEXT,
                    wav_sha256 TEXT,
                    wav_size INTEGER,
                    wav_duration REAL,
                    wav_deleted_utc TEXT,
                    result_dir TEXT,
                    recognition_id INTEGER,
                    UNIQUE(source_hash, prep_config_hash),
                    FOREIGN KEY(recognition_id) REFERENCES recognitions(id)
                );
                CREATE TABLE IF NOT EXISTS webhook_sources (
                    webhook_event_id INTEGER NOT NULL,
                    source_job_id INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    PRIMARY KEY(webhook_event_id, source_job_id),
                    FOREIGN KEY(webhook_event_id) REFERENCES webhook_events(id),
                    FOREIGN KEY(source_job_id) REFERENCES source_jobs(id)
                );
                CREATE TABLE IF NOT EXISTS message_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_key TEXT NOT NULL,
                    webhook_event_id INTEGER NOT NULL,
                    source_job_id INTEGER NOT NULL,
                    recognition_id INTEGER,
                    company_id TEXT,
                    task_id TEXT,
                    chat_id TEXT,
                    message_id TEXT,
                    source_hash TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    UNIQUE(message_key, source_hash),
                    FOREIGN KEY(webhook_event_id) REFERENCES webhook_events(id),
                    FOREIGN KEY(source_job_id) REFERENCES source_jobs(id),
                    FOREIGN KEY(recognition_id) REFERENCES recognitions(id)
                );
                CREATE TABLE IF NOT EXISTS recognitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audio_sha256 TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    audio_path TEXT NOT NULL,
                    result_dir TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error_type TEXT,
                    per_job_limit INTEGER NOT NULL,
                    attempts_used INTEGER NOT NULL DEFAULT 0,
                    total_windows INTEGER,
                    completed_windows INTEGER,
                    candidate_rows INTEGER,
                    no_match_windows INTEGER,
                    summary_json TEXT,
                    links_dirty INTEGER NOT NULL DEFAULT 0,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    started_utc TEXT,
                    completed_utc TEXT,
                    audio_deleted_utc TEXT,
                    results_deleted_utc TEXT,
                    resume_limited INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    claim_until REAL,
                    UNIQUE(audio_sha256, config_hash)
                );
                CREATE TABLE IF NOT EXISTS worker_budget (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    request_limit INTEGER NOT NULL,
                    used INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recognition_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    reserved_utc TEXT NOT NULL,
                    completed_utc TEXT,
                    FOREIGN KEY(recognition_id) REFERENCES recognitions(id)
                );
                CREATE TABLE IF NOT EXISTS aggregation_inputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recognition_id INTEGER NOT NULL,
                    canonical_schema_version TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(recognition_id, input_hash),
                    FOREIGN KEY(recognition_id) REFERENCES recognitions(id)
                );
                CREATE TABLE IF NOT EXISTS aggregation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recognition_id INTEGER NOT NULL,
                    aggregation_input_id INTEGER NOT NULL,
                    engine_version TEXT NOT NULL,
                    result_schema_version TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','running','complete','error')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error_class TEXT,
                    last_error_message TEXT,
                    result_json TEXT,
                    result_digest TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(aggregation_input_id, engine_version, result_schema_version, profile_hash),
                    FOREIGN KEY(recognition_id) REFERENCES recognitions(id),
                    FOREIGN KEY(aggregation_input_id) REFERENCES aggregation_inputs(id)
                );
                CREATE TABLE IF NOT EXISTS polled_yougile_files (
                    task_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    source_job_id INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    PRIMARY KEY(task_id, chat_id, message_id, file_id),
                    FOREIGN KEY(source_job_id) REFERENCES source_jobs(id)
                );
                CREATE TABLE IF NOT EXISTS polled_message_sources (
                    task_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_job_id INTEGER NOT NULL,
                    file_id TEXT,
                    created_utc TEXT NOT NULL,
                    PRIMARY KEY(task_id, chat_id, message_id, source_hash),
                    FOREIGN KEY(source_job_id) REFERENCES source_jobs(id)
                );
                CREATE TABLE IF NOT EXISTS yougile_api_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requested_epoch REAL NOT NULL,
                    request_kind TEXT NOT NULL,
                    created_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS yougile_rate_limit (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    blocked_until REAL NOT NULL DEFAULT 0,
                    updated_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS poller_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    round_robin_cursor INTEGER NOT NULL DEFAULT 0,
                    updated_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS polled_messages (
                    task_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    checked_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processed',
                    reason_type TEXT,
                    reason_code TEXT,
                    pipeline_status TEXT,
                    pipeline_updated_utc TEXT,
                    PRIMARY KEY(task_id,chat_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS poller_chat_state (
                    task_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    next_offset INTEGER NOT NULL DEFAULT 0,
                    last_message_id TEXT,
                    updated_utc TEXT NOT NULL,
                    PRIMARY KEY(task_id,chat_id)
                );
                CREATE TABLE IF NOT EXISTS poller_chat_baselines (
                    task_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message_order TEXT,
                    high_water_message_id TEXT,
                    history_count INTEGER NOT NULL DEFAULT 0,
                    started_utc TEXT,
                    completed_utc TEXT,
                    updated_utc TEXT NOT NULL,
                    PRIMARY KEY(task_id,chat_id)
                );
                CREATE TABLE IF NOT EXISTS poller_scope_runs (
                    task_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    trigger_message_id TEXT NOT NULL,
                    run_token TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    PRIMARY KEY(task_id,chat_id)
                );
                CREATE TABLE IF NOT EXISTS chat_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    attachment_path TEXT,
                    attachment_ref TEXT,
                    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','error')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_epoch REAL NOT NULL DEFAULT 0,
                    error_type TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    sent_utc TEXT,
                    claim_token TEXT,
                    claim_until REAL
                );
                CREATE TABLE IF NOT EXISTS cisnet_baseline (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    max_existing_run_id INTEGER NOT NULL,
                    preexisting_incomplete_ids_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cisnet_runs (
                    aggregation_run_id INTEGER PRIMARY KEY,
                    recognition_id INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending','ready','empty','error')),
                    message_text TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    FOREIGN KEY(aggregation_run_id) REFERENCES aggregation_runs(id)
                );
                CREATE TABLE IF NOT EXISTS cisnet_searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    aggregation_run_id INTEGER NOT NULL,
                    query_hash TEXT NOT NULL,
                    artifact_key TEXT NOT NULL,
                    first_candidate_index INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    performer TEXT NOT NULL,
                    query_iswc TEXT,
                    state TEXT NOT NULL CHECK (state IN ('pending','yes','no','error')),
                    selected_iswc TEXT,
                    result_path TEXT,
                    error_type TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    UNIQUE(aggregation_run_id,query_hash),
                    FOREIGN KEY(aggregation_run_id) REFERENCES cisnet_runs(aggregation_run_id)
                );
                CREATE TABLE IF NOT EXISTS cisnet_candidates (
                    aggregation_run_id INTEGER NOT NULL,
                    candidate_index INTEGER NOT NULL,
                    search_id INTEGER NOT NULL,
                    PRIMARY KEY(aggregation_run_id,candidate_index),
                    FOREIGN KEY(aggregation_run_id) REFERENCES cisnet_runs(aggregation_run_id),
                    FOREIGN KEY(search_id) REFERENCES cisnet_searches(id)
                );
                CREATE INDEX IF NOT EXISTS webhook_stage_idx ON webhook_events(stage, id);
                CREATE INDEX IF NOT EXISTS source_stage_idx ON source_jobs(stage, id);
                CREATE INDEX IF NOT EXISTS recognition_state_idx ON recognitions(state, id);
                CREATE INDEX IF NOT EXISTS aggregation_input_recognition_idx
                    ON aggregation_inputs(recognition_id);
                CREATE INDEX IF NOT EXISTS aggregation_input_hash_idx
                    ON aggregation_inputs(input_hash);
                CREATE INDEX IF NOT EXISTS aggregation_run_recognition_idx
                    ON aggregation_runs(recognition_id, id);
                CREATE INDEX IF NOT EXISTS aggregation_run_claim_idx
                    ON aggregation_runs(state, next_attempt_at, lease_expires_at, id);
                CREATE INDEX IF NOT EXISTS polled_yougile_source_idx
                    ON polled_yougile_files(source_job_id);
                CREATE INDEX IF NOT EXISTS polled_message_source_idx
                    ON polled_message_sources(source_job_id);
                CREATE INDEX IF NOT EXISTS yougile_request_epoch_idx
                    ON yougile_api_requests(requested_epoch);
                CREATE INDEX IF NOT EXISTS chat_notification_claim_idx
                    ON chat_notifications(state,next_attempt_epoch,claim_until,id);
                CREATE INDEX IF NOT EXISTS cisnet_searches_run_idx
                    ON cisnet_searches(aggregation_run_id,first_candidate_index);
                CREATE TRIGGER IF NOT EXISTS aggregation_inputs_no_update
                BEFORE UPDATE ON aggregation_inputs
                BEGIN
                    SELECT RAISE(ABORT, 'aggregation_inputs are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS aggregation_inputs_no_delete
                BEFORE DELETE ON aggregation_inputs
                BEGIN
                    SELECT RAISE(ABORT, 'aggregation_inputs are immutable');
                END;
                """
            )
            source_columns = {
                row[1] for row in db.execute("PRAGMA table_info(source_jobs)").fetchall()
            }
            if "source_item_path" not in source_columns:
                db.execute("ALTER TABLE source_jobs ADD COLUMN source_item_path TEXT")
            if "source_items_json" not in source_columns:
                db.execute("ALTER TABLE source_jobs ADD COLUMN source_items_json TEXT")
            if "source_manifest_json" not in source_columns:
                db.execute("ALTER TABLE source_jobs ADD COLUMN source_manifest_json TEXT")
            if "source_unlimited" not in source_columns:
                db.execute(
                    "ALTER TABLE source_jobs ADD COLUMN source_unlimited INTEGER NOT NULL DEFAULT 0"
                )
            if "local_source_path" not in source_columns:
                db.execute("ALTER TABLE source_jobs ADD COLUMN local_source_path TEXT")
            if "audio_attempts" not in source_columns:
                db.execute(
                    "ALTER TABLE source_jobs "
                    "ADD COLUMN audio_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "next_attempt_epoch" not in source_columns:
                db.execute(
                    "ALTER TABLE source_jobs "
                    "ADD COLUMN next_attempt_epoch REAL NOT NULL DEFAULT 0"
                )
            if "wav_deleted_utc" not in source_columns:
                db.execute("ALTER TABLE source_jobs ADD COLUMN wav_deleted_utc TEXT")
            recognition_columns = {
                row[1] for row in db.execute("PRAGMA table_info(recognitions)").fetchall()
            }
            if "results_deleted_utc" not in recognition_columns:
                db.execute("ALTER TABLE recognitions ADD COLUMN results_deleted_utc TEXT")
            if "audio_deleted_utc" not in recognition_columns:
                db.execute("ALTER TABLE recognitions ADD COLUMN audio_deleted_utc TEXT")
            if "resume_limited" not in recognition_columns:
                db.execute(
                    "ALTER TABLE recognitions ADD COLUMN resume_limited INTEGER NOT NULL DEFAULT 0"
                )
            message_columns = {
                row[1] for row in db.execute("PRAGMA table_info(message_sources)").fetchall()
            }
            if "task_id" not in message_columns:
                db.execute("ALTER TABLE message_sources ADD COLUMN task_id TEXT")
            polled_columns = {
                row[1] for row in db.execute("PRAGMA table_info(polled_messages)").fetchall()
            }
            if "status" not in polled_columns:
                db.execute(
                    "ALTER TABLE polled_messages "
                    "ADD COLUMN status TEXT NOT NULL DEFAULT 'processed'"
                )
            if "reason_type" not in polled_columns:
                db.execute("ALTER TABLE polled_messages ADD COLUMN reason_type TEXT")
            if "reason_code" not in polled_columns:
                db.execute("ALTER TABLE polled_messages ADD COLUMN reason_code TEXT")
            if "pipeline_status" not in polled_columns:
                db.execute("ALTER TABLE polled_messages ADD COLUMN pipeline_status TEXT")
            if "pipeline_updated_utc" not in polled_columns:
                db.execute("ALTER TABLE polled_messages ADD COLUMN pipeline_updated_utc TEXT")
            db.execute(
                """
                INSERT OR IGNORE INTO polled_message_sources
                    (task_id,chat_id,message_id,source_hash,source_job_id,file_id,created_utc)
                SELECT p.task_id,p.chat_id,p.message_id,s.source_hash,p.source_job_id,p.file_id,p.created_utc
                FROM polled_yougile_files p
                JOIN source_jobs s ON s.id=p.source_job_id
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO polled_messages (task_id,chat_id,message_id,checked_utc)
                SELECT DISTINCT task_id,chat_id,message_id,created_utc
                FROM polled_message_sources
                """
            )
            db.execute(
                """
                UPDATE message_sources
                SET task_id=COALESCE(
                    (SELECT p.task_id FROM polled_message_sources p
                     WHERE p.source_job_id=message_sources.source_job_id
                       AND p.message_id=message_sources.message_id
                     ORDER BY p.created_utc LIMIT 1),
                    chat_id
                )
                WHERE task_id IS NULL
                """
            )
            row = db.execute("SELECT version FROM schema_info WHERE id=1").fetchone()
            if not row:
                db.execute("INSERT INTO schema_info VALUES (1, ?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                db.execute("UPDATE schema_info SET version=? WHERE id=1", (SCHEMA_VERSION,))
            self._refresh_all_polled_message_statuses_db(db, utcnow())
            db.commit()
        try:
            os.chmod(self.path, 0o600)
        except FileNotFoundError:
            pass

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _derive_pipeline_status(rows: list[sqlite3.Row], now_epoch: float) -> str | None:
        if not rows:
            return None
        stages = [str(row["stage"]) for row in rows]
        effective_states = [
            str(row["recognition_state"])
            if row["stage"] == "recognition_linked" and row["recognition_state"]
            else str(row["stage"])
            for row in rows
        ]
        active = [state for state in effective_states if state not in TERMINAL_SOURCE_STATES]
        failures = {"audio_error", "recognition_error", "partial", "api_error", "limit", "uncertain"}
        has_failure = any(state in failures for state in effective_states)
        if active:
            if has_failure:
                return "processing_with_errors"
            if "queued" in active:
                queued_rows = [row for row in rows if row["stage"] == "queued"]
                if queued_rows and all(
                    float(row["next_attempt_epoch"] or 0) > now_epoch for row in queued_rows
                ):
                    return "audio_retry_wait"
                return "queued"
            if "preparing_audio" in active:
                return "preparing_audio"
            if "audio_ready" in active:
                return "recognition_pending"
            if "attaching_recognition" in active:
                return "recognition_pending"
            recognition_states = {
                str(row["recognition_state"])
                for row in rows
                if row["stage"] == "recognition_linked" and row["recognition_state"]
            }
            if "running" in recognition_states:
                return "recognizing"
            return "recognition_pending"
        if has_failure:
            return "completed_with_errors"
        if "complete_candidates" in effective_states:
            return "complete_candidates"
        if "complete_no_match" in effective_states:
            return "complete_no_match"
        if effective_states and all(state == "complete_local_only" for state in effective_states):
            return "complete_local_only"
        return "completed"

    def _refresh_polled_message_status_db(
        self, db: sqlite3.Connection, task_id: str, chat_id: str,
        message_id: str, now: str,
    ) -> None:
        rows = db.execute(
            """
            SELECT s.stage,s.next_attempt_epoch,r.state AS recognition_state
            FROM polled_message_sources p
            JOIN source_jobs s ON s.id=p.source_job_id
            LEFT JOIN recognitions r ON r.id=s.recognition_id
            WHERE p.task_id=? AND p.chat_id=? AND p.message_id=?
            ORDER BY s.id
            """,
            (task_id, chat_id, message_id),
        ).fetchall()
        status = self._derive_pipeline_status(rows, time.time())
        db.execute(
            "UPDATE polled_messages SET pipeline_status=?,pipeline_updated_utc=? "
            "WHERE task_id=? AND chat_id=? AND message_id=?",
            (status, now if status else None, task_id, chat_id, message_id),
        )

    def _refresh_polled_messages_for_source_db(
        self, db: sqlite3.Connection, source_id: int, now: str,
    ) -> None:
        messages = db.execute(
            "SELECT DISTINCT task_id,chat_id,message_id FROM polled_message_sources "
            "WHERE source_job_id=?",
            (source_id,),
        ).fetchall()
        for message in messages:
            self._refresh_polled_message_status_db(
                db, message["task_id"], message["chat_id"], message["message_id"], now
            )

    def _refresh_polled_messages_for_recognition_db(
        self, db: sqlite3.Connection, recognition_id: int, now: str,
    ) -> None:
        source_ids = db.execute(
            "SELECT id FROM source_jobs WHERE recognition_id=?",
            (recognition_id,),
        ).fetchall()
        for source in source_ids:
            self._refresh_polled_messages_for_source_db(db, source["id"], now)

    def _refresh_all_polled_message_statuses_db(
        self, db: sqlite3.Connection, now: str,
    ) -> None:
        messages = db.execute(
            "SELECT task_id,chat_id,message_id FROM polled_messages"
        ).fetchall()
        for message in messages:
            self._refresh_polled_message_status_db(
                db, message["task_id"], message["chat_id"], message["message_id"], now
            )

    def recover_receiver_claims(self) -> None:
        """Reset only receiver-owned stages after the receiver process restarts."""
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE webhook_events SET stage='queued',claim_token=NULL,claim_until=NULL,updated_utc=? "
                "WHERE stage='expanding'",
                (now,),
            )
            db.execute(
                "UPDATE source_jobs SET stage='queued',claim_token=NULL,claim_until=NULL,updated_utc=? "
                "WHERE stage='preparing_audio'",
                (now,),
            )
            self._refresh_all_polled_message_statuses_db(db, now)
            db.commit()

    def recover_worker_claims(self) -> None:
        """Called only while holding the global recognition-worker lock."""
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE source_jobs SET stage='audio_ready',claim_token=NULL,claim_until=NULL,updated_utc=? "
                "WHERE stage='attaching_recognition'",
                (now,),
            )
            db.execute(
                "UPDATE recognitions SET state='pending',claim_token=NULL,claim_until=NULL,updated_utc=? "
                "WHERE state='running'",
                (now,),
            )
            self._refresh_all_polled_message_statuses_db(db, now)
            db.commit()

    def enqueue_chat_notification(
        self,
        chat_id: str,
        kind: str,
        dedupe_key: str,
        text: str,
        attachment_path: Path | None = None,
    ) -> bool:
        """Persist one idempotent outbound chat notification without sending it."""
        if not chat_id or not kind or not dedupe_key or not text:
            raise ValueError("Invalid chat notification")
        if attachment_path is not None and not isinstance(attachment_path, Path):
            raise ValueError("Invalid chat notification attachment")
        if not text.startswith(CHAT_NOTIFICATION_PREFIX):
            text = CHAT_NOTIFICATION_PREFIX + text
        now = utcnow()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO chat_notifications
                    (dedupe_key,chat_id,kind,text,attachment_path,state,created_utc,updated_utc)
                VALUES (?,?,?,?,?,'pending',?,?)
                """,
                (dedupe_key, chat_id, kind, text, str(attachment_path) if attachment_path else None,
                 now, now),
            )
            db.commit()
            return cursor.rowcount == 1

    def claim_chat_notification(self, owner: str, lease_seconds: int = 300) -> dict | None:
        if not owner or lease_seconds < 1:
            raise ValueError("Invalid chat notification claim")
        now_epoch = time.time()
        now = utcnow()
        token = owner + ":" + uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT * FROM chat_notifications
                WHERE (state IN ('pending','error') AND next_attempt_epoch<=?)
                   OR (state='sending' AND claim_until<?)
                ORDER BY id LIMIT 1
                """,
                (now_epoch, now_epoch),
            ).fetchone()
            if not row:
                db.commit()
                return None
            updated = db.execute(
                """
                UPDATE chat_notifications
                SET state='sending',attempts=attempts+1,claim_token=?,claim_until=?,updated_utc=?
                WHERE id=? AND state=?
                """,
                (token, now_epoch + lease_seconds, now, row["id"], row["state"]),
            )
            if updated.rowcount != 1:
                db.rollback()
                return None
            claimed = dict(db.execute("SELECT * FROM chat_notifications WHERE id=?", (row["id"],)).fetchone())
            db.commit()
            return claimed

    def set_chat_notification_attachment(self, notification_id: int, token: str, attachment_ref: str) -> None:
        if not attachment_ref.startswith("/user-data/"):
            raise ValueError("Invalid uploaded YouGile file reference")
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE chat_notifications SET attachment_ref=?,updated_utc=?
                WHERE id=? AND state='sending' AND claim_token=?
                """,
                (attachment_ref, utcnow(), notification_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Chat notification claim was lost")
            db.commit()

    def complete_chat_notification(self, notification_id: int, token: str) -> None:
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE chat_notifications
                SET state='sent',error_type=NULL,sent_utc=?,updated_utc=?,claim_token=NULL,claim_until=NULL
                WHERE id=? AND state='sending' AND claim_token=?
                """,
                (now, now, notification_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Chat notification claim was lost")
            db.commit()

    def retry_chat_notification(self, notification_id: int, token: str, error_type: str, delay_seconds: float) -> None:
        if not error_type:
            raise ValueError("Chat notification error type is required")
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE chat_notifications
                SET state='error',error_type=?,next_attempt_epoch=?,updated_utc=?,claim_token=NULL,claim_until=NULL
                WHERE id=? AND state='sending' AND claim_token=?
                """,
                (error_type, time.time() + max(1.0, delay_seconds), now, notification_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Chat notification claim was lost")
            db.commit()

    def record_webhook(
        self,
        payload: dict,
        event_name: str | None,
        company_id: str | None,
        chat_id: str | None,
        message_id: str | None,
    ) -> tuple[int, bool]:
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        delivery_key = stable_hash(event_name, company_id, chat_id, message_id, payload_sha)
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO webhook_events
                    (delivery_key,payload_sha256,payload_json,event_name,company_id,chat_id,message_id,
                     stage,received_utc,last_received_utc,updated_utc)
                VALUES (?,?,?,?,?,?,?,'queued',?,?,?)
                """,
                (delivery_key, payload_sha, payload_json, event_name, company_id, chat_id, message_id,
                 now, now, now),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                db.execute(
                    "UPDATE webhook_events SET delivery_count=delivery_count+1,last_received_utc=?,updated_utc=? "
                    "WHERE delivery_key=?",
                    (now, now, delivery_key),
                )
            event_id = db.execute(
                "SELECT id FROM webhook_events WHERE delivery_key=?", (delivery_key,)
            ).fetchone()[0]
            db.commit()
        return event_id, inserted

    def _claim(self, table: str, ready: str, active: str, owner: str, lease_seconds: int):
        if table not in {"webhook_events", "source_jobs", "recognitions"}:
            raise ValueError("Unsupported claim table")
        now_epoch = time.time()
        now = utcnow()
        token = owner + ":" + uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT * FROM {table} WHERE stage=? OR (stage=? AND claim_until<?) ORDER BY id LIMIT 1"
                if table != "recognitions" else
                "SELECT * FROM recognitions WHERE state=? OR (state=? AND claim_until<?) ORDER BY id LIMIT 1",
                (ready, active, now_epoch),
            ).fetchone()
            if not row:
                db.commit()
                return None
            state_column = "state" if table == "recognitions" else "stage"
            updated = db.execute(
                f"UPDATE {table} SET {state_column}=?,claim_token=?,claim_until=?,updated_utc=?,"
                "started_utc=COALESCE(started_utc,?) WHERE id=? AND " + state_column + "=?",
                (active, token, now_epoch + lease_seconds, now, now, row["id"], row[state_column]),
            )
            if updated.rowcount != 1:
                db.rollback()
                return None
            if table == "source_jobs":
                self._refresh_polled_messages_for_source_db(db, row["id"], now)
            elif table == "recognitions":
                self._refresh_polled_messages_for_recognition_db(db, row["id"], now)
            claimed = dict(db.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone())
            db.commit()
            return claimed

    def claim_webhook(self, owner: str, lease_seconds: int = 300):
        return self._claim("webhook_events", "queued", "expanding", owner, lease_seconds)

    def claim_source(self, owner: str, lease_seconds: int = 900):
        now_epoch = time.time()
        now = utcnow()
        token = owner + ":" + uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM source_jobs WHERE stage='preparing_audio' AND claim_until>=? LIMIT 1",
                (now_epoch,),
            ).fetchone():
                db.commit()
                return None
            row = db.execute(
                "SELECT * FROM source_jobs WHERE (stage='queued' AND next_attempt_epoch<=?) "
                "OR (stage='preparing_audio' AND claim_until<?) "
                "ORDER BY CASE stage WHEN 'preparing_audio' THEN 0 ELSE 1 END,id LIMIT 1",
                (now_epoch, now_epoch),
            ).fetchone()
            if not row:
                db.commit()
                return None
            updated = db.execute(
                "UPDATE source_jobs SET stage='preparing_audio',claim_token=?,claim_until=?,"
                "updated_utc=?,started_utc=COALESCE(started_utc,?),"
                "audio_attempts=audio_attempts+1,next_attempt_epoch=0 "
                "WHERE id=? AND stage=?",
                (token, now_epoch + lease_seconds, now, now, row["id"], row["stage"]),
            )
            if updated.rowcount != 1:
                db.rollback()
                return None
            self._refresh_polled_messages_for_source_db(db, row["id"], now)
            claimed = dict(db.execute("SELECT * FROM source_jobs WHERE id=?", (row["id"],)).fetchone())
            db.commit()
            return claimed

    def renew_source_claim(
        self, source_id: int, token: str, lease_seconds: int = 900,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("Invalid source lease duration")
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                "UPDATE source_jobs SET claim_until=?,updated_utc=? "
                "WHERE id=? AND claim_token=? AND stage='preparing_audio'",
                (time.time() + lease_seconds, now, source_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Audio claim was lost")
            db.commit()

    def claim_recognition(self, owner: str, lease_seconds: int = 300):
        return self._claim("recognitions", "pending", "running", owner, lease_seconds)

    def complete_webhook(self, event_id: int, token: str, sources: list, prep_hash: str) -> list[int]:
        now = utcnow()
        job_ids = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute(
                "SELECT * FROM webhook_events WHERE id=? AND claim_token=?", (event_id, token)
            ).fetchone()
            if not event:
                raise StoreError("Webhook claim was lost")
            message_key = stable_hash(
                event["company_id"], event["chat_id"], event["message_id"] or f"event:{event_id}"
            )
            for source_value in sources:
                source = normalize_source(source_value)
                db.execute(
                    """
                    INSERT OR IGNORE INTO source_jobs
                        (source_hash,prep_config_hash,source_kind,source_url,source_item_path,
                         source_items_json,source_unlimited,source_filename,source_size,
                         stage,created_utc,updated_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?)
                    """,
                    (source["source_hash"], prep_hash, source["kind"], source["url"],
                     source["item_path"], source["items_json"], int(source["unlimited"]),
                     source["filename"], source["size"], now, now),
                )
                source_row = db.execute(
                    "SELECT id,recognition_id FROM source_jobs WHERE source_hash=? AND prep_config_hash=?",
                    (source["source_hash"], prep_hash),
                ).fetchone()
                source_id = source_row["id"]
                job_ids.append(source_id)
                db.execute(
                    "INSERT OR IGNORE INTO webhook_sources VALUES (?,?,?)", (event_id, source_id, now)
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO message_sources
                        (message_key,webhook_event_id,source_job_id,recognition_id,company_id,
                         task_id,chat_id,message_id,source_hash,created_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (message_key, event_id, source_id, source_row["recognition_id"], event["company_id"],
                     event["chat_id"], event["chat_id"], event["message_id"],
                     source["source_hash"], now),
                )
                if source_row["recognition_id"]:
                    db.execute(
                        "UPDATE recognitions SET links_dirty=1,updated_utc=? WHERE id=?",
                        (now, source_row["recognition_id"]),
                    )
            db.execute(
                "UPDATE webhook_events SET stage=?,error_type=NULL,completed_utc=?,updated_utc=?,"
                "claim_token=NULL,claim_until=NULL WHERE id=?",
                ("expanded" if sources else "no_supported_url", now, now, event_id),
            )
            db.commit()
        return job_ids

    def enqueue_polled_yougile_file(
        self,
        task_id: str,
        chat_id: str,
        message_id: str,
        file_id: str,
        file_path: str,
        source_filename: str,
        prep_hash: str,
    ) -> tuple[int, bool]:
        results = self.enqueue_polled_sources(
            task_id,
            chat_id,
            message_id,
            [{
                "kind": "yougile_file",
                "url": file_path,
                "filename": source_filename,
                "file_id": file_id,
            }],
            prep_hash,
        )
        return results[0]

    def enqueue_manual_wav(
        self, source_hash: str, prep_hash: str, filename: str, wav_path: Path, info: dict,
    ) -> tuple[int, bool]:
        """Register an already-normalized local WAV at the audio_ready boundary."""
        now = utcnow()
        manifest = json.dumps({
            "schema_version": 1,
            "ordering": "single_file",
            "inter_source_silence_seconds": 0,
            "items": [{
                "order": 1, "filename": filename, "item_path": None,
                "source_size": info["size_bytes"], "start_seconds": 0.0,
                "duration_seconds": info["duration_seconds"],
                "end_seconds": info["duration_seconds"],
            }],
        }, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            inserted = db.execute(
                """
                INSERT OR IGNORE INTO source_jobs
                    (source_hash,prep_config_hash,source_kind,source_url,source_filename,source_size,
                     source_manifest_json,stage,wav_path,wav_sha256,wav_size,wav_duration,
                     created_utc,updated_utc,audio_completed_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (source_hash, prep_hash, "manual_wav", f"manual-wav:{source_hash}", filename,
                 info["size_bytes"], manifest, "audio_ready", str(wav_path), info["sha256"],
                 info["size_bytes"], info["duration_seconds"], now, now, now),
            )
            row = db.execute(
                "SELECT id FROM source_jobs WHERE source_hash=? AND prep_config_hash=?",
                (source_hash, prep_hash),
            ).fetchone()
            db.commit()
        return int(row["id"]), inserted.rowcount == 1

    def enqueue_polled_sources(
        self,
        task_id: str,
        chat_id: str,
        message_id: str,
        sources: list,
        prep_hash: str,
    ) -> list[tuple[int, bool]]:
        """Atomically link every source and mark the polled message complete."""
        now = utcnow()
        event_name = "chat_message-polled"
        payload = {
            "event": event_name,
            "payload": {"chatId": chat_id, "id": message_id, "taskId": task_id},
        }
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        delivery_key = stable_hash(event_name, task_id, chat_id, message_id)
        message_key = stable_hash(task_id, chat_id, message_id, prep_hash)
        results = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT OR IGNORE INTO webhook_events
                    (delivery_key,payload_sha256,payload_json,event_name,company_id,chat_id,message_id,
                     stage,error_type,delivery_count,received_utc,last_received_utc,updated_utc,completed_utc)
                VALUES (?,?,?,?,NULL,?,?,'expanded',NULL,1,?,?,?,?)
                """,
                (delivery_key, payload_sha, payload_json, event_name, chat_id, message_id,
                 now, now, now, now),
            )
            event_id = db.execute(
                "SELECT id FROM webhook_events WHERE delivery_key=?", (delivery_key,)
            ).fetchone()[0]
            for source_value in sources:
                description = normalize_source(source_value)
                existing = db.execute(
                    "SELECT source_job_id FROM polled_message_sources "
                    "WHERE task_id=? AND chat_id=? AND message_id=? AND source_hash=?",
                    (task_id, chat_id, message_id, description["source_hash"]),
                ).fetchone()
                if existing:
                    results.append((existing["source_job_id"], False))
                    continue
                if description["kind"] == "yandex_disk_group":
                    legacy = db.execute(
                        "SELECT p.source_job_id FROM polled_message_sources p "
                        "JOIN source_jobs s ON s.id=p.source_job_id "
                        "WHERE p.task_id=? AND p.chat_id=? AND p.message_id=? "
                        "AND s.source_url=? ORDER BY p.created_utc,p.source_job_id LIMIT 1",
                        (task_id, chat_id, message_id, description["url"]),
                    ).fetchone()
                    if legacy:
                        # Do not reprocess recently polled folders after this migration.
                        results.append((legacy["source_job_id"], False))
                        continue
                inserted_source = db.execute(
                    """
                    INSERT OR IGNORE INTO source_jobs
                        (source_hash,prep_config_hash,source_kind,source_url,source_item_path,
                         source_items_json,source_unlimited,source_filename,source_size,
                         stage,created_utc,updated_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?)
                    """,
                    (description["source_hash"], prep_hash, description["kind"],
                     description["url"], description["item_path"], description["items_json"],
                     int(description["unlimited"]), description["filename"],
                     description["size"], now, now),
                )
                source_created = inserted_source.rowcount == 1
                source = db.execute(
                    "SELECT id,recognition_id FROM source_jobs "
                    "WHERE source_hash=? AND prep_config_hash=?",
                    (description["source_hash"], prep_hash),
                ).fetchone()
                source_id = source["id"]
                db.execute(
                    "INSERT OR IGNORE INTO webhook_sources VALUES (?,?,?)",
                    (event_id, source_id, now),
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO message_sources
                        (message_key,webhook_event_id,source_job_id,recognition_id,company_id,
                         task_id,chat_id,message_id,source_hash,created_utc)
                    VALUES (?,?,?,?,NULL,?,?,?,?,?)
                    """,
                    (message_key, event_id, source_id, source["recognition_id"], task_id,
                     chat_id, message_id, description["source_hash"], now),
                )
                db.execute(
                    """
                    INSERT INTO polled_message_sources
                        (task_id,chat_id,message_id,source_hash,source_job_id,file_id,created_utc)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (task_id, chat_id, message_id, description["source_hash"], source_id,
                     description["file_id"], now),
                )
                if description["kind"] == "yougile_file" and description["file_id"]:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO polled_yougile_files
                            (task_id,chat_id,message_id,file_id,file_path,source_filename,
                             source_job_id,created_utc)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (task_id, chat_id, message_id, description["file_id"],
                         description["url"], description["filename"], source_id, now),
                    )
                if source["recognition_id"]:
                    db.execute(
                        "UPDATE recognitions SET links_dirty=1,updated_utc=? WHERE id=?",
                        (now, source["recognition_id"]),
                    )
                results.append((source_id, source_created))
            status = "queued" if any(inserted for _, inserted in results) else "deduplicated"
            db.execute(
                "INSERT OR IGNORE INTO polled_messages "
                "(task_id,chat_id,message_id,checked_utc,status) VALUES (?,?,?,?,?)",
                (task_id, chat_id, message_id, now, status),
            )
            self._refresh_polled_message_status_db(db, task_id, chat_id, message_id, now)
            db.commit()
            return results

    def rearm_scope_run(
        self, task_id: str, chat_id: str, trigger_message_id: str
    ) -> bool:
        """Start one idempotent processing generation for an allowed-column entry."""
        if not task_id or not chat_id or not trigger_message_id:
            raise ValueError("Invalid scope run identity")
        now = utcnow()
        run_token = stable_hash(task_id, chat_id, trigger_message_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT trigger_message_id FROM poller_scope_runs "
                "WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            ).fetchone()
            if current and current["trigger_message_id"] == trigger_message_id:
                db.commit()
                return False
            db.execute(
                "DELETE FROM polled_yougile_files WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            )
            db.execute(
                "DELETE FROM polled_message_sources WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            )
            db.execute(
                "DELETE FROM polled_messages WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            )
            db.execute(
                "DELETE FROM poller_chat_state WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            )
            db.execute(
                "DELETE FROM poller_chat_baselines WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            )
            db.execute(
                """
                INSERT INTO poller_scope_runs
                    (task_id,chat_id,trigger_message_id,run_token,updated_utc)
                VALUES (?,?,?,?,?)
                ON CONFLICT(task_id,chat_id) DO UPDATE SET
                    trigger_message_id=excluded.trigger_message_id,
                    run_token=excluded.run_token,
                    updated_utc=excluded.updated_utc
                """,
                (task_id, chat_id, trigger_message_id, run_token, now),
            )
            db.commit()
            return True

    def scope_run_prep_hash(self, task_id: str, chat_id: str, base_hash: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT run_token FROM poller_scope_runs WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            ).fetchone()
        return f"{base_hash}:scope-run:{row['run_token']}" if row else base_hash

    def recognition_scope_run_token(self, recognition_id: int) -> str | None:
        marker = ":scope-run:"
        with self.connect() as db:
            values = {
                str(row[0]).split(marker, 1)[1]
                for row in db.execute(
                    "SELECT DISTINCT prep_config_hash FROM source_jobs WHERE recognition_id=?",
                    (recognition_id,),
                )
                if marker in str(row[0])
            }
        if len(values) > 1:
            raise StoreError("Recognition combines multiple scope runs")
        return next(iter(values), None)

    def polled_yougile_context(self, source_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT p.task_id,p.chat_id,p.message_id,p.file_id,s.source_filename "
                "FROM polled_message_sources p JOIN source_jobs s ON s.id=p.source_job_id "
                "WHERE p.source_job_id=? ORDER BY p.created_utc LIMIT 1",
                (source_id,),
            ).fetchone()
            return dict(row) if row else None

    def chat_links_for_source(self, source_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT DISTINCT task_id,chat_id,message_id,source_hash
                FROM message_sources WHERE source_job_id=?
                  AND task_id IS NOT NULL AND chat_id IS NOT NULL
                ORDER BY task_id,chat_id,message_id
                """,
                (source_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def resume_limited_recognitions(self, task_id: str, chat_id: str) -> list[int]:
        """Requeue only durable ACR 3003 stops linked to one task chat.

        This deliberately retains the recognition row and its scanner directory.
        The scanner resumes from completed-window state, rather than creating a
        new source, WAV, or result directory.
        """
        if not task_id or not chat_id:
            raise ValueError("Invalid retry-limited task chat")
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT DISTINCT r.id,r.result_dir
                FROM recognitions r
                JOIN message_sources ms ON ms.recognition_id=r.id
                WHERE ms.task_id=? AND ms.chat_id=? AND r.state='api_error'
                ORDER BY r.id
                """,
                (task_id, chat_id),
            ).fetchall()
        eligible = []
        for row in rows:
            scan_path = Path(row["result_dir"]) / "scan.sqlite3"
            try:
                uri = f"file:{scan_path}?mode=ro"
                with sqlite3.connect(uri, uri=True) as scan_db:
                    found = scan_db.execute(
                        "SELECT 1 FROM attempts WHERE state='error' AND acr_code=3003 LIMIT 1"
                    ).fetchone()
            except sqlite3.Error:
                continue
            if found:
                eligible.append(int(row["id"]))
        if not eligible:
            return []
        now = utcnow()
        placeholders = ",".join("?" for _ in eligible)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                f"""
                UPDATE recognitions
                SET state='pending',error_type=NULL,resume_limited=1,claim_token=NULL,claim_until=NULL,
                    updated_utc=?,completed_utc=NULL
                WHERE id IN ({placeholders}) AND state='api_error'
                """,
                (now, *eligible),
            )
            db.execute(
                f"""
                UPDATE source_jobs
                SET stage='recognition_linked',error_type=NULL,completed_utc=NULL,updated_utc=?
                WHERE recognition_id IN ({placeholders})
                """,
                (now, *eligible),
            )
            for recognition_id in eligible:
                self._refresh_polled_messages_for_recognition_db(db, recognition_id, now)
            db.commit()
        return eligible

    def polled_message_ids(self, task_id: str, chat_id: str) -> set[str]:
        """Return messages whose complete source set was durably recorded."""
        with self.connect() as db:
            return {
                str(row[0]) for row in db.execute(
                    "SELECT message_id FROM polled_messages WHERE task_id=? AND chat_id=?",
                    (task_id, chat_id),
                )
            }

    def polled_message_records(self, task_id: str, chat_id: str) -> dict[str, dict]:
        """Return durable message dispositions without message bodies or source URLs."""
        with self.connect() as db:
            return {
                str(row["message_id"]): dict(row)
                for row in db.execute(
                    "SELECT message_id,checked_utc,status,status AS ingest_status,"
                    "reason_type,reason_code,pipeline_status,pipeline_updated_utc "
                    "FROM polled_messages WHERE task_id=? AND chat_id=?",
                    (task_id, chat_id),
                )
            }

    def task_has_linked_source(self, task_id: str, chat_id: str, sources: list) -> bool:
        """Whether this task has already linked the selected source identity."""
        source_hashes = {normalize_source(source)["source_hash"] for source in sources}
        if not source_hashes:
            return False
        placeholders = ",".join("?" for _ in source_hashes)
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM message_sources WHERE task_id=? AND chat_id=? "
                f"AND source_hash IN ({placeholders}) LIMIT 1",
                (task_id, chat_id, *sorted(source_hashes)),
            ).fetchone() is not None

    def mark_polled_message(
        self,
        task_id: str,
        chat_id: str,
        message_id: str,
        status: str = "processed",
        reason_type: str | None = None,
        reason_code: str | None = None,
    ) -> bool:
        now = utcnow()
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO polled_messages "
                "(task_id,chat_id,message_id,checked_utc,status,reason_type,reason_code) "
                "VALUES (?,?,?,?,?,?,?)",
                (task_id, chat_id, message_id, now, status, reason_type, reason_code),
            )
            db.commit()
            return cursor.rowcount == 1

    def baseline_state(self, task_id: str, chat_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM poller_chat_baselines WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            ).fetchone()
            return dict(row) if row else {
                "task_id": task_id,
                "chat_id": chat_id,
                "status": "required",
                "message_order": None,
                "high_water_message_id": None,
                "history_count": 0,
                "started_utc": None,
                "completed_utc": None,
                "updated_utc": None,
            }

    def require_baseline(self, task_id: str, chat_id: str) -> dict:
        current = self.baseline_state(task_id, chat_id)
        if current["status"] in {"complete", "in_progress"}:
            return current
        now = utcnow()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO poller_chat_baselines "
                "(task_id,chat_id,status,history_count,updated_utc) "
                "VALUES (?,?,'required',0,?)",
                (task_id, chat_id, now),
            )
            db.commit()
        return self.baseline_state(task_id, chat_id)

    def activate_chat_from_start(self, task_id: str, chat_id: str) -> dict:
        """Enable first-scope processing without marking existing messages as seen."""
        now = utcnow()
        activated = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM poller_chat_baselines WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            ).fetchone()
            status = row["status"] if row else "required"
            if status not in {"complete", "in_progress"}:
                db.execute(
                    """
                    INSERT INTO poller_chat_baselines
                        (task_id,chat_id,status,message_order,high_water_message_id,
                         history_count,started_utc,completed_utc,updated_utc)
                    VALUES (?,?,'complete','oldest_first',NULL,0,?,?,?)
                    ON CONFLICT(task_id,chat_id) DO UPDATE SET
                        status='complete',message_order='oldest_first',
                        high_water_message_id=NULL,history_count=0,
                        started_utc=COALESCE(poller_chat_baselines.started_utc,excluded.started_utc),
                        completed_utc=excluded.completed_utc,updated_utc=excluded.updated_utc
                    WHERE poller_chat_baselines.status NOT IN ('complete','in_progress')
                    """,
                    (task_id, chat_id, now, now, now),
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO poller_chat_state
                        (task_id,chat_id,next_offset,last_message_id,updated_utc)
                    VALUES (?,?,0,NULL,?)
                    """,
                    (task_id, chat_id, now),
                )
                activated = True
            db.commit()
        result = self.baseline_state(task_id, chat_id)
        result["activated"] = activated
        return result

    def begin_baseline(self, task_id: str, chat_id: str) -> dict:
        current = self.baseline_state(task_id, chat_id)
        if current["status"] == "complete":
            return current
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO poller_chat_baselines
                    (task_id,chat_id,status,history_count,started_utc,updated_utc)
                VALUES (?,?,'in_progress',0,?,?)
                ON CONFLICT(task_id,chat_id) DO UPDATE SET
                    status='in_progress',started_utc=COALESCE(started_utc,excluded.started_utc),
                    updated_utc=excluded.updated_utc
                """,
                (task_id, chat_id, now, now),
            )
            db.commit()
        return self.baseline_state(task_id, chat_id)

    def complete_baseline(
        self,
        task_id: str,
        chat_id: str,
        message_ids: list[str],
        message_order: str,
    ) -> dict:
        if message_order != "oldest_first":
            raise ValueError("Unsupported YouGile message order")
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("Duplicate message IDs in baseline snapshot")
        now = utcnow()
        high_water = message_ids[-1] if message_ids else None
        inserted = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for message_id in message_ids:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO polled_messages "
                    "(task_id,chat_id,message_id,checked_utc,status,reason_code) "
                    "VALUES (?,?,?,?,'baseline','preexisting_history')",
                    (task_id, chat_id, message_id, now),
                )
                inserted += int(cursor.rowcount == 1)
            db.execute(
                """
                INSERT INTO poller_chat_baselines
                    (task_id,chat_id,status,message_order,high_water_message_id,
                     history_count,started_utc,completed_utc,updated_utc)
                VALUES (?,?,'complete',?,?,?,?,?,?)
                ON CONFLICT(task_id,chat_id) DO UPDATE SET
                    status='complete',message_order=excluded.message_order,
                    high_water_message_id=excluded.high_water_message_id,
                    history_count=excluded.history_count,
                    started_utc=COALESCE(poller_chat_baselines.started_utc,excluded.started_utc),
                    completed_utc=excluded.completed_utc,updated_utc=excluded.updated_utc
                """,
                (task_id, chat_id, message_order, high_water, len(message_ids), now, now, now),
            )
            db.execute(
                """
                INSERT INTO poller_chat_state
                    (task_id,chat_id,next_offset,last_message_id,updated_utc)
                VALUES (?,?,?,?,?)
                ON CONFLICT(task_id,chat_id) DO UPDATE SET
                    next_offset=excluded.next_offset,last_message_id=excluded.last_message_id,
                    updated_utc=excluded.updated_utc
                """,
                (task_id, chat_id, len(message_ids), high_water, now),
            )
            db.commit()
        result = self.baseline_state(task_id, chat_id)
        result["inserted"] = inserted
        return result

    def poller_chat_position(self, task_id: str, chat_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT next_offset,last_message_id,updated_utc FROM poller_chat_state "
                "WHERE task_id=? AND chat_id=?",
                (task_id, chat_id),
            ).fetchone()
            return dict(row) if row else {
                "next_offset": 0, "last_message_id": None, "updated_utc": None,
            }

    def set_poller_chat_position(
        self, task_id: str, chat_id: str, next_offset: int, last_message_id: str | None
    ) -> None:
        if next_offset < 0:
            raise ValueError("Invalid poller chat offset")
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO poller_chat_state
                    (task_id,chat_id,next_offset,last_message_id,updated_utc)
                VALUES (?,?,?,?,?)
                ON CONFLICT(task_id,chat_id) DO UPDATE SET
                    next_offset=excluded.next_offset,
                    last_message_id=excluded.last_message_id,
                    updated_utc=excluded.updated_utc
                """,
                (task_id, chat_id, int(next_offset), last_message_id, now),
            )
            db.commit()

    def fail_webhook(self, event_id: int, token: str, error_type: str) -> None:
        self._finish_claim("webhook_events", event_id, token, "error", error_type)

    def retry_webhook(self, event_id: int, token: str, error_type: str) -> None:
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                "UPDATE webhook_events SET stage='queued',error_type=?,updated_utc=?,"
                "claim_token=NULL,claim_until=NULL WHERE id=? AND claim_token=? "
                "AND stage='expanding'",
                (error_type, now, event_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Webhook claim was lost")
            db.commit()

    def set_audio_plan(
        self,
        source_id: int,
        token: str,
        filename: str,
        size: int,
        wav_path: Path,
        local_source_path: Path | None = None,
    ) -> None:
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                "UPDATE source_jobs SET source_filename=?,source_size=?,wav_path=?,local_source_path=?,updated_utc=? "
                "WHERE id=? AND claim_token=? AND stage='preparing_audio'",
                (filename, size, str(wav_path),
                 str(local_source_path) if local_source_path else None, now, source_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Audio claim was lost")
            db.commit()

    def clear_local_source(self, source_id: int, token: str) -> None:
        with self.connect() as db:
            updated = db.execute(
                "UPDATE source_jobs SET local_source_path=NULL,updated_utc=? "
                "WHERE id=? AND claim_token=? AND stage='preparing_audio'",
                (utcnow(), source_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Audio claim was lost")
            db.commit()

    def set_source_manifest(self, source_id: int, token: str, manifest: dict) -> None:
        now = utcnow()
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            updated = db.execute(
                "UPDATE source_jobs SET source_manifest_json=?,updated_utc=? "
                "WHERE id=? AND claim_token=? AND stage='preparing_audio'",
                (encoded, now, source_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Audio claim was lost")
            db.commit()

    def complete_audio(self, source_id: int, token: str, info: dict) -> None:
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE source_jobs SET stage='audio_ready',wav_sha256=?,wav_size=?,wav_duration=?,
                    error_type=NULL,audio_completed_utc=?,updated_utc=?,claim_token=NULL,claim_until=NULL,
                    next_attempt_epoch=0,wav_deleted_utc=NULL
                WHERE id=? AND claim_token=? AND stage='preparing_audio'
                """,
                (info["sha256"], info["size_bytes"], info["duration_seconds"], now, now,
                 source_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Audio claim was lost")
            self._refresh_polled_messages_for_source_db(db, source_id, now)
            db.commit()

    def fail_audio(self, source_id: int, token: str, error_type: str) -> None:
        self._finish_claim("source_jobs", source_id, token, "audio_error", error_type)

    def retry_audio(
        self, source_id: int, token: str, error_type: str, delay_seconds: float = 0,
    ) -> None:
        now = utcnow()
        next_attempt = time.time() + max(float(delay_seconds), 0.0)
        with self.connect() as db:
            updated = db.execute(
                "UPDATE source_jobs SET stage='queued',error_type=?,updated_utc=?,"
                "next_attempt_epoch=?,claim_token=NULL,claim_until=NULL "
                "WHERE id=? AND claim_token=? "
                "AND stage='preparing_audio'",
                (error_type, now, next_attempt, source_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Audio claim was lost")
            self._refresh_polled_messages_for_source_db(db, source_id, now)
            db.commit()

    def requeue_audio_errors(self, source_ids: list[int]) -> list[int]:
        """Explicitly return selected terminal audio failures to the receiver queue.

        This is an operator action, not an automatic retry: only rows still in
        ``audio_error`` are eligible, and completed recognitions are untouched.
        """
        ids = sorted(set(source_ids))
        if not ids or any(type(source_id) is not int or source_id < 1 for source_id in ids):
            raise ValueError("Invalid source job IDs")
        now = utcnow()
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                f"SELECT id FROM source_jobs WHERE id IN ({placeholders}) AND stage='audio_error'",
                ids,
            ).fetchall()
            found = sorted(row["id"] for row in rows)
            if found != ids:
                raise StoreError("Only terminal audio errors can be requeued")
            db.execute(
                f"UPDATE source_jobs SET stage='queued',error_type=NULL,completed_utc=NULL,"
                f"updated_utc=?,next_attempt_epoch=0,claim_token=NULL,claim_until=NULL "
                f"WHERE id IN ({placeholders})",
                [now, *ids],
            )
            for source_id in ids:
                self._refresh_polled_messages_for_source_db(db, source_id, now)
            db.commit()
        return ids

    def create_operator_replay(self, source_id: int) -> int:
        """Queue an explicitly new full replay while retaining the original run.

        The replay has a fresh forced-run identity, so it gets a new WAV,
        recognition directory and ACRCloud request history.  Existing message
        links are copied for notifications, but poller deduplication remains
        attached to the original source job.
        """
        if type(source_id) is not int or source_id < 1:
            raise ValueError("Invalid source job ID")
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute("SELECT * FROM source_jobs WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise StoreError("Source job does not exist")
            if source["stage"] not in TERMINAL_SOURCE_STATES or source["claim_token"]:
                raise StoreError("Only an unclaimed terminal source job can be replayed")
            prep_hash = f"{source['prep_config_hash']}{FORCED_RUN_MARKER}operator-{uuid.uuid4().hex}"
            inserted = db.execute(
                """
                INSERT INTO source_jobs
                    (source_hash,prep_config_hash,source_kind,source_url,source_item_path,
                     source_items_json,source_unlimited,source_filename,source_size,
                     stage,created_utc,updated_utc)
                VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?)
                """,
                (
                    source["source_hash"], prep_hash, source["source_kind"], source["source_url"],
                    source["source_item_path"], source["source_items_json"], source["source_unlimited"],
                    source["source_filename"], source["source_size"], now, now,
                ),
            )
            replay_id = int(inserted.lastrowid)
            links = db.execute(
                "SELECT * FROM message_sources WHERE source_job_id=? ORDER BY id", (source_id,)
            ).fetchall()
            for link in links:
                db.execute(
                    """
                    INSERT INTO message_sources
                        (message_key,webhook_event_id,source_job_id,recognition_id,company_id,
                         task_id,chat_id,message_id,source_hash,created_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stable_hash("operator-replay", source_id, replay_id, link["id"]),
                        link["webhook_event_id"], replay_id, None, link["company_id"],
                        link["task_id"], link["chat_id"], link["message_id"], link["source_hash"], now,
                    ),
                )
            events = db.execute(
                "SELECT DISTINCT webhook_event_id FROM webhook_sources WHERE source_job_id=?", (source_id,)
            ).fetchall()
            for event in events:
                db.execute(
                    "INSERT OR IGNORE INTO webhook_sources VALUES (?,?,?)",
                    (event["webhook_event_id"], replay_id, now),
                )
            db.commit()
        return replay_id

    def resume_limited_recognition(self, recognition_id: int) -> bool:
        """Resume exactly one ACRCloud-3003 recognition from saved windows."""
        if type(recognition_id) is not int or recognition_id < 1:
            raise ValueError("Invalid recognition ID")
        with self.connect() as db:
            row = db.execute(
                "SELECT id,result_dir FROM recognitions WHERE id=? AND state='api_error'", (recognition_id,)
            ).fetchone()
        if not row:
            return False
        scan_path = Path(row["result_dir"]) / "scan.sqlite3"
        try:
            with sqlite3.connect(f"file:{scan_path}?mode=ro", uri=True) as scan_db:
                limited = scan_db.execute(
                    "SELECT 1 FROM attempts WHERE state='error' AND acr_code=3003 LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            return False
        if not limited:
            return False
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                """
                UPDATE recognitions
                SET state='pending',error_type=NULL,resume_limited=1,claim_token=NULL,claim_until=NULL,
                    updated_utc=?,completed_utc=NULL
                WHERE id=? AND state='api_error'
                """, (now, recognition_id),
            )
            if updated.rowcount != 1:
                db.rollback()
                return False
            db.execute(
                "UPDATE source_jobs SET stage='recognition_linked',error_type=NULL,completed_utc=NULL,updated_utc=? "
                "WHERE recognition_id=?", (now, recognition_id),
            )
            self._refresh_polled_messages_for_recognition_db(db, recognition_id, now)
            db.commit()
        return True

    def fail_recognition_attachment(self, source_id: int, token: str, error_type: str) -> None:
        self._finish_claim("source_jobs", source_id, token, "recognition_error", error_type)

    def _finish_claim(self, table: str, item_id: int, token: str, state: str, error_type: str | None) -> None:
        column = "state" if table == "recognitions" else "stage"
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                f"UPDATE {table} SET {column}=?,error_type=?,updated_utc=?,completed_utc=?,"
                "claim_token=NULL,claim_until=NULL WHERE id=? AND claim_token=?",
                (state, error_type, now, now, item_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Claim was lost")
            if table == "source_jobs":
                self._refresh_polled_messages_for_source_db(db, item_id, now)
            elif table == "recognitions":
                self._refresh_polled_messages_for_recognition_db(db, item_id, now)
            db.commit()

    def claim_audio_for_recognition(self, owner: str, lease_seconds: int = 300):
        return self._claim("source_jobs", "audio_ready", "attaching_recognition", owner, lease_seconds)

    def attach_recognition(
        self,
        source_id: int,
        token: str,
        config_hash: str,
        result_dir: Path,
    ) -> int:
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                "SELECT * FROM source_jobs WHERE id=? AND claim_token=? AND stage='attaching_recognition'",
                (source_id, token),
            ).fetchone()
            if not source or not source["wav_sha256"] or not source["wav_path"]:
                raise StoreError("Recognition attachment claim was lost or WAV is incomplete")
            db.execute(
                """
                INSERT OR IGNORE INTO recognitions
                    (audio_sha256,config_hash,audio_path,result_dir,state,per_job_limit,created_utc,updated_utc)
                VALUES (?,?,?,?,'pending',?,?,?)
                """,
                (source["wav_sha256"], config_hash, source["wav_path"], str(result_dir),
                 0, now, now),
            )
            recognition = db.execute(
                "SELECT * FROM recognitions WHERE audio_sha256=? AND config_hash=?",
                (source["wav_sha256"], config_hash),
            ).fetchone()
            recognition_id = recognition["id"]
            stage = recognition["state"] if recognition["state"] in TERMINAL_RECOGNITION_STATES else "recognition_linked"
            db.execute(
                "UPDATE source_jobs SET stage=?,recognition_id=?,result_dir=?,updated_utc=?,"
                "claim_token=NULL,claim_until=NULL WHERE id=?",
                (stage, recognition_id, recognition["result_dir"], now, source_id),
            )
            db.execute(
                "UPDATE message_sources SET recognition_id=? WHERE source_job_id=?",
                (recognition_id, source_id),
            )
            db.execute(
                "UPDATE recognitions SET links_dirty=1,updated_utc=? WHERE id=?",
                (now, recognition_id),
            )
            self._refresh_polled_messages_for_source_db(db, source_id, now)
            db.commit()
            return recognition_id

    def recognition(self, recognition_id: int):
        with self.connect() as db:
            row = db.execute("SELECT * FROM recognitions WHERE id=?", (recognition_id,)).fetchone()
            return dict(row) if row else None

    def source(self, source_id: int):
        with self.connect() as db:
            row = db.execute("SELECT * FROM source_jobs WHERE id=?", (source_id,)).fetchone()
            return dict(row) if row else None

    def get_aggregation_input(self, recognition_id: int, input_hash: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM aggregation_inputs WHERE recognition_id=? AND input_hash=?",
                (recognition_id, input_hash),
            ).fetchone()
            return dict(row) if row else None

    def ensure_aggregation_input(
        self,
        recognition_id: int,
        canonical_schema_version: str,
        adapter_version: str,
        canonical_json: str | object,
        input_hash: str | None = None,
    ) -> dict:
        """Persist one immutable canonical input, returning an existing semantic match."""
        if not canonical_schema_version or not adapter_version:
            raise ValueError("Canonical schema and adapter versions are required")
        canonical = canonical_json_text(canonical_json)
        digest = json_sha256(canonical)
        if input_hash is not None and input_hash != digest:
            raise ValueError("Aggregation input hash does not match canonical JSON")
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM recognitions WHERE id=?", (recognition_id,)).fetchone():
                raise StoreError("Aggregation input recognition does not exist")
            row = db.execute(
                "SELECT * FROM aggregation_inputs WHERE recognition_id=? AND input_hash=?",
                (recognition_id, digest),
            ).fetchone()
            if row:
                if (
                    row["canonical_schema_version"] != canonical_schema_version
                    or row["adapter_version"] != adapter_version
                    or row["canonical_json"] != canonical
                ):
                    raise StoreError("Aggregation input hash collides with different semantic input")
                db.commit()
                return dict(row)
            cursor = db.execute(
                """
                INSERT INTO aggregation_inputs
                    (recognition_id,canonical_schema_version,adapter_version,input_hash,canonical_json,created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (recognition_id, canonical_schema_version, adapter_version, digest, canonical, now),
            )
            row = db.execute("SELECT * FROM aggregation_inputs WHERE id=?", (cursor.lastrowid,)).fetchone()
            db.commit()
            return dict(row)

    def ensure_aggregation_run(
        self,
        aggregation_input_id: int,
        engine_version: str,
        result_schema_version: str,
        profile_name: str,
        profile_hash: str,
    ) -> dict:
        """Create the idempotent run record; this does not execute aggregation."""
        if not all((engine_version, result_schema_version, profile_name, profile_hash)):
            raise ValueError("Aggregation engine, result schema and profile identity are required")
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            input_row = db.execute(
                "SELECT recognition_id FROM aggregation_inputs WHERE id=?", (aggregation_input_id,)
            ).fetchone()
            if not input_row:
                raise StoreError("Aggregation input does not exist")
            db.execute(
                """
                INSERT OR IGNORE INTO aggregation_runs
                    (recognition_id,aggregation_input_id,engine_version,result_schema_version,
                     profile_name,profile_hash,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'pending',?,?)
                """,
                (input_row["recognition_id"], aggregation_input_id, engine_version,
                 result_schema_version, profile_name, profile_hash, now, now),
            )
            row = db.execute(
                """
                SELECT * FROM aggregation_runs WHERE aggregation_input_id=? AND engine_version=?
                  AND result_schema_version=? AND profile_hash=?
                """,
                (aggregation_input_id, engine_version, result_schema_version, profile_hash),
            ).fetchone()
            db.commit()
            return dict(row)

    def aggregation_input(self, aggregation_input_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM aggregation_inputs WHERE id=?", (aggregation_input_id,)
            ).fetchone()
            return dict(row) if row else None

    def aggregation_run(self, aggregation_run_id: int) -> dict | None:
        """Return one aggregation run without changing its durable state."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM aggregation_runs WHERE id=?", (aggregation_run_id,)
            ).fetchone()
            return dict(row) if row else None

    def claim_aggregation_run(
        self, owner: str, lease_seconds: int = 300, now_epoch: float | None = None,
        engine_version: str | None = None, result_schema_version: str | None = None,
        profile_hash: str | None = None,
    ) -> dict | None:
        if not owner or lease_seconds < 1:
            raise ValueError("Aggregation claim owner and positive lease duration are required")
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        now = utcnow()
        token = owner + ":" + uuid.uuid4().hex
        version_predicate = ""
        version_values = []
        for column, value in (
            ("engine_version", engine_version),
            ("result_schema_version", result_schema_version),
            ("profile_hash", profile_hash),
        ):
            if value is not None:
                version_predicate += f" AND {column}=?"
                version_values.append(value)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT * FROM aggregation_runs
                WHERE ((state='pending' AND next_attempt_at<=?)
                   OR (state='error' AND next_attempt_at<=?)
                   OR (state='running' AND lease_expires_at<?))
                """ + version_predicate + " ORDER BY id LIMIT 1",
                (now_epoch, now_epoch, now_epoch, *version_values),
            ).fetchone()
            if not row:
                db.commit()
                return None
            updated = db.execute(
                """
                UPDATE aggregation_runs
                SET state='running',attempts=attempts+1,lease_token=?,lease_expires_at=?,
                    updated_at=?,last_error_class=NULL,last_error_message=NULL
                WHERE id=? AND state=?
                """,
                (token, now_epoch + lease_seconds, now, row["id"], row["state"]),
            )
            if updated.rowcount != 1:
                db.rollback()
                return None
            claimed = db.execute("SELECT * FROM aggregation_runs WHERE id=?", (row["id"],)).fetchone()
            db.commit()
            return dict(claimed)

    def complete_aggregation_run(self, run_id: int, lease_token: str, result_json: str | object) -> str:
        """Persist a deterministic full AggregatedResult JSON and return its digest."""
        canonical = canonical_json_text(result_json)
        digest = json_sha256(canonical)
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE aggregation_runs
                SET state='complete',result_json=?,result_digest=?,updated_at=?,completed_at=?,
                    lease_token=NULL,lease_expires_at=NULL,next_attempt_at=0,
                    last_error_class=NULL,last_error_message=NULL
                WHERE id=? AND state='running' AND lease_token=?
                """,
                (canonical, digest, now, now, run_id, lease_token),
            )
            if updated.rowcount != 1:
                raise StoreError("Aggregation claim was lost")
            db.commit()
        return digest

    def fail_aggregation_run(
        self, run_id: int, lease_token: str, error_class: str, error_message: str,
        retry_delay_seconds: float = 0,
    ) -> None:
        if not error_class:
            raise ValueError("Aggregation error class is required")
        now_epoch = time.time()
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE aggregation_runs
                SET state='error',next_attempt_at=?,last_error_class=?,last_error_message=?,
                    updated_at=?,lease_token=NULL,lease_expires_at=NULL
                WHERE id=? AND state='running' AND lease_token=?
                """,
                (now_epoch + max(0.0, retry_delay_seconds), error_class, error_message[:1000],
                 now, run_id, lease_token),
            )
            if updated.rowcount != 1:
                raise StoreError("Aggregation claim was lost")
            db.commit()

    def release_aggregation_claim(
        self, run_id: int, lease_token: str, retry_delay_seconds: float = 0,
    ) -> None:
        self.fail_aggregation_run(
            run_id, lease_token, "ClaimReleased", "Aggregation claim released without result",
            retry_delay_seconds,
        )

    def recover_expired_aggregation_leases(self, now_epoch: float | None = None) -> int:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        now = utcnow()
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE aggregation_runs
                SET state='error',next_attempt_at=?,last_error_class='LeaseExpired',
                    last_error_message='Aggregation lease expired before completion',updated_at=?,
                    lease_token=NULL,lease_expires_at=NULL
                WHERE state='running' AND lease_expires_at<?
                """,
                (now_epoch, now, now_epoch),
            )
            db.commit()
            return updated.rowcount

    def aggregation_runs_for_recognition(self, recognition_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM aggregation_runs WHERE recognition_id=? ORDER BY id", (recognition_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def recognition_ids_missing_aggregation_run(
        self, states: set[str] | tuple[str, ...], engine_version: str,
        result_schema_version: str, profile_hash: str, limit: int = 1,
        require_no_prior_aggregation: bool = False,
    ) -> list[int]:
        """Return completed recognition rows that need this aggregation contract."""
        if not states or limit < 1:
            return []
        placeholders = ",".join("?" for _ in states)
        prior_clause = """
                  AND NOT EXISTS (
                    SELECT 1 FROM aggregation_runs prior
                    WHERE prior.recognition_id=r.id
                  )
        """ if require_no_prior_aggregation else ""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT r.id FROM recognitions r
                WHERE r.state IN (""" + placeholders + """)
                  AND r.results_deleted_utc IS NULL
                """ + prior_clause + """
                  AND NOT EXISTS (
                    SELECT 1 FROM aggregation_runs ar
                    WHERE ar.recognition_id=r.id AND ar.engine_version=?
                      AND ar.result_schema_version=? AND ar.profile_hash=?
                  )
                ORDER BY r.id LIMIT ?
                """,
                (*sorted(states), engine_version, result_schema_version, profile_hash, limit),
            ).fetchall()
            return [row["id"] for row in rows]

    def request_usage(self) -> int:
        """Return persistent request usage; zero request_limit means uncapped."""
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request_limit,used FROM worker_budget WHERE id=1").fetchone()
            if not row:
                db.execute("INSERT INTO worker_budget VALUES (1,0,0,?,?)", (now, now))
                used = 0
            else:
                used = row["used"]
                if row["request_limit"] != 0:
                    db.execute(
                        "UPDATE worker_budget SET request_limit=0,updated_utc=? WHERE id=1",
                        (now,),
                    )
            db.commit()
            return used

    def reserve_request(self, recognition_id: int) -> int:
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT used FROM worker_budget WHERE id=1").fetchone()
            if not row:
                db.execute("INSERT INTO worker_budget VALUES (1,0,0,?,?)", (now, now))
            db.execute("UPDATE worker_budget SET used=used+1,updated_utc=? WHERE id=1", (now,))
            cursor = db.execute(
                "INSERT INTO worker_attempts (recognition_id,state,reserved_utc) VALUES (?,'sending',?)",
                (recognition_id, now),
            )
            attempt_id = cursor.lastrowid
            db.commit()
            return attempt_id

    def finish_request(self, attempt_id: int, state: str) -> None:
        if state not in {"done", "uncertain"}:
            raise ValueError("Invalid worker attempt state")
        with self.connect() as db:
            db.execute(
                "UPDATE worker_attempts SET state=?,completed_utc=? WHERE id=?",
                (state, utcnow(), attempt_id),
            )
            db.commit()

    def complete_recognition(self, recognition_id: int, token: str, state: str, summary: dict, error_type=None) -> None:
        if state not in TERMINAL_RECOGNITION_STATES:
            raise ValueError("Invalid terminal recognition state")
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempts = summary.get("attempts")
            if attempts is None:
                attempts = db.execute(
                    "SELECT COUNT(*) FROM worker_attempts WHERE recognition_id=?", (recognition_id,)
                ).fetchone()[0]
            updated = db.execute(
                """
                UPDATE recognitions SET state=?,error_type=?,resume_limited=0,attempts_used=?,total_windows=?,
                    completed_windows=?,candidate_rows=?,no_match_windows=?,summary_json=?,updated_utc=?,
                    completed_utc=?,links_dirty=1,claim_token=NULL,claim_until=NULL
                WHERE id=? AND claim_token=? AND state='running'
                """,
                (state, error_type, attempts, summary.get("total_windows"),
                 summary.get("completed_windows"), summary.get("candidate_rows"),
                 summary.get("no_match_windows"), json.dumps(summary, ensure_ascii=False, sort_keys=True),
                 now, now, recognition_id, token),
            )
            if updated.rowcount != 1:
                raise StoreError("Recognition claim was lost")
            db.execute(
                "UPDATE source_jobs SET stage=?,error_type=?,updated_utc=?,completed_utc=? "
                "WHERE recognition_id=?",
                (state, error_type, now, now, recognition_id),
            )
            self._refresh_polled_messages_for_recognition_db(db, recognition_id, now)
            db.commit()

    def dirty_recognition(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM recognitions WHERE links_dirty=1 "
                "AND results_deleted_utc IS NULL AND state IN ("
                + ",".join("?" for _ in TERMINAL_RECOGNITION_STATES)
                + ") ORDER BY id LIMIT 1",
                tuple(sorted(TERMINAL_RECOGNITION_STATES)),
            ).fetchone()
            return dict(row) if row else None

    def mark_links_clean(self, recognition_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE recognitions SET links_dirty=0,updated_utc=? WHERE id=?",
                (utcnow(), recognition_id),
            )
            db.commit()

    def links_for_recognition(self, recognition_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT ms.company_id,ms.task_id,ms.chat_id,ms.message_id,ms.source_hash,
                       ms.webhook_event_id,sj.id AS source_job_id,sj.source_kind,
                       sj.wav_path,sj.wav_sha256,r.id AS recognition_id,r.result_dir,r.state
                FROM message_sources ms
                JOIN source_jobs sj ON sj.id=ms.source_job_id
                JOIN recognitions r ON r.id=ms.recognition_id
                WHERE ms.recognition_id=? ORDER BY ms.id
                """,
                (recognition_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def source_ids_for_recognition(self, recognition_id: int) -> list[int]:
        with self.connect() as db:
            return [
                row[0] for row in db.execute(
                    "SELECT id FROM source_jobs WHERE recognition_id=? ORDER BY id",
                    (recognition_id,),
                )
            ]

    def source_manifests_for_recognition(self, recognition_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,source_kind,source_filename,source_manifest_json "
                "FROM source_jobs WHERE recognition_id=? AND source_manifest_json IS NOT NULL "
                "ORDER BY id",
                (recognition_id,),
            ).fetchall()
        manifests = []
        for row in rows:
            manifest = json.loads(row["source_manifest_json"])
            manifest["source_job_id"] = row["id"]
            manifest["source_kind"] = row["source_kind"]
            manifest["source_filename"] = row["source_filename"]
            manifests.append(manifest)
        return manifests

    def reserve_yougile_request(
        self,
        request_kind: str,
        max_requests: int = 40,
        window_seconds: int = 60,
    ) -> float:
        if max_requests < 1 or window_seconds < 1:
            raise ValueError("Invalid YouGile request limit")
        now_epoch = time.time()
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute(
                "SELECT blocked_until FROM yougile_rate_limit WHERE id=1"
            ).fetchone()
            if state and state["blocked_until"] > now_epoch:
                db.commit()
                return state["blocked_until"] - now_epoch
            cutoff = now_epoch - window_seconds
            db.execute("DELETE FROM yougile_api_requests WHERE requested_epoch<?", (now_epoch - 3600,))
            rows = db.execute(
                "SELECT requested_epoch FROM yougile_api_requests "
                "WHERE requested_epoch>? ORDER BY requested_epoch",
                (cutoff,),
            ).fetchall()
            if len(rows) >= max_requests:
                wait_seconds = rows[len(rows) - max_requests]["requested_epoch"] + window_seconds - now_epoch
                db.commit()
                return max(wait_seconds, 0.01)
            db.execute(
                "INSERT INTO yougile_api_requests(requested_epoch,request_kind,created_utc) "
                "VALUES (?,?,?)",
                (now_epoch, request_kind, now),
            )
            db.commit()
            return 0.0

    def defer_yougile_requests(self, retry_after_seconds: float) -> None:
        blocked_until = time.time() + max(float(retry_after_seconds), 1.0)
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO yougile_rate_limit(id,blocked_until,updated_utc) VALUES (1,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    blocked_until=MAX(blocked_until,excluded.blocked_until),
                    updated_utc=excluded.updated_utc
                """,
                (blocked_until, now),
            )
            db.commit()

    def yougile_request_stats(self, window_seconds: int = 60) -> dict:
        now_epoch = time.time()
        with self.connect() as db:
            rows = db.execute(
                "SELECT request_kind,COUNT(*) AS count FROM yougile_api_requests "
                "WHERE requested_epoch>? GROUP BY request_kind ORDER BY request_kind",
                (now_epoch - window_seconds,),
            ).fetchall()
            total = sum(row["count"] for row in rows)
            state = db.execute(
                "SELECT blocked_until FROM yougile_rate_limit WHERE id=1"
            ).fetchone()
            return {
                "window_seconds": window_seconds,
                "total": total,
                "by_kind": {row["request_kind"]: row["count"] for row in rows},
                "blocked_seconds": max(0.0, (state["blocked_until"] if state else 0) - now_epoch),
            }

    def round_robin_chats(self, chat_ids: list[str], max_chats: int) -> list[str]:
        chats = sorted(set(chat_ids))
        if not chats or max_chats < 1:
            return []
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT round_robin_cursor FROM poller_state WHERE id=1"
            ).fetchone()
            cursor = (row["round_robin_cursor"] if row else 0) % len(chats)
            count = min(max_chats, len(chats))
            selected = [chats[(cursor + offset) % len(chats)] for offset in range(count)]
            next_cursor = (cursor + count) % len(chats)
            db.execute(
                """
                INSERT INTO poller_state(id,round_robin_cursor,updated_utc) VALUES (1,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    round_robin_cursor=excluded.round_robin_cursor,
                    updated_utc=excluded.updated_utc
                """,
                (next_cursor, now),
            )
            db.commit()
            return selected

    def counts(self) -> dict:
        now_epoch = time.time()
        with self.connect() as db:
            return {
                "webhooks_queued": db.execute(
                    "SELECT COUNT(*) FROM webhook_events WHERE stage IN ('queued','expanding')"
                ).fetchone()[0],
                "audio_queued": db.execute(
                    "SELECT COUNT(*) FROM source_jobs WHERE stage='preparing_audio' "
                    "OR (stage='queued' AND next_attempt_epoch<=?)",
                    (now_epoch,),
                ).fetchone()[0],
                "audio_retry_wait": db.execute(
                    "SELECT COUNT(*) FROM source_jobs "
                    "WHERE stage='queued' AND next_attempt_epoch>?",
                    (now_epoch,),
                ).fetchone()[0],
                "recognition_queued": db.execute(
                    "SELECT COUNT(*) FROM source_jobs WHERE stage='audio_ready'"
                ).fetchone()[0] + db.execute(
                    "SELECT COUNT(*) FROM recognitions WHERE state IN ('pending','running')"
                ).fetchone()[0],
            }

    def task_status_counts(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT pipeline_status,COUNT(*) AS count FROM polled_messages "
                "WHERE pipeline_status IS NOT NULL GROUP BY pipeline_status "
                "ORDER BY pipeline_status"
            ).fetchall()
            return {str(row["pipeline_status"]): int(row["count"]) for row in rows}
