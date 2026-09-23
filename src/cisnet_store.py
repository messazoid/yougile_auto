"""Durable CIS-Net search state and a preview of the final YouGile message."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from cisnet_cli import SearchRequest, _query_key, _usable_iswc
from job_store import PipelineStore, utcnow


BUSY_SESSION_ERROR = "BusySession"


def search_identity(request: SearchRequest) -> str:
    """Exact search inputs, excluding time periods and duplicate positions."""
    value = json.dumps([request.title, request.performer, request.iswc], ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def baseline(store: PipelineStore, old_path: Path | None = None, *, initialize: bool = False) -> dict:
    with store.connect() as db:
        row = db.execute("SELECT * FROM cisnet_baseline WHERE id=1").fetchone()
        if row:
            return dict(row)
        if old_path is not None and old_path.is_file():
            old = json.loads(old_path.read_text(encoding="utf-8"))
            if old.get("schema_version") != "cisnet-automation-baseline/v1":
                raise ValueError("Unsupported CIS-Net file baseline")
            maximum = old["max_existing_run_id"]
            incomplete = old["preexisting_incomplete_run_ids"]
        elif initialize:
            maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM aggregation_runs").fetchone()[0]
            incomplete = [row[0] for row in db.execute(
                "SELECT id FROM aggregation_runs WHERE id<=? AND state!='complete' ORDER BY id", (maximum,)
            )]
        else:
            raise ValueError("CIS-Net baseline is missing; initialize it before automation")
        if not isinstance(maximum, int) or maximum < 0 or not isinstance(incomplete, list) or any(
            not isinstance(item, int) or item < 1 for item in incomplete
        ):
            raise ValueError("Invalid CIS-Net baseline")
        db.execute(
            "INSERT OR IGNORE INTO cisnet_baseline VALUES (1,?,?,?)",
            (maximum, json.dumps(incomplete), utcnow()),
        )
        db.commit()
        return dict(db.execute("SELECT * FROM cisnet_baseline WHERE id=1").fetchone())


def eligible_runs(store: PipelineStore) -> list[tuple[int, int]]:
    with store.connect() as db:
        row = db.execute("SELECT * FROM cisnet_baseline WHERE id=1").fetchone()
        if not row:
            raise ValueError("CIS-Net database baseline is missing")
        maximum = row["max_existing_run_id"]
        incomplete = set(json.loads(row["preexisting_incomplete_ids_json"]))
        return [(item["id"], item["recognition_id"]) for item in db.execute(
            "SELECT id,recognition_id FROM aggregation_runs WHERE state='complete' ORDER BY id"
        ) if item["id"] > maximum or item["id"] in incomplete]


def stage_run(store: PipelineStore, run_id: int, recognition_id: int,
              requests: list[SearchRequest]) -> None:
    now = utcnow()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO cisnet_runs VALUES (?,?,?,?,?,?)",
            (run_id, recognition_id, "pending" if requests else "empty", None, now, now),
        )
        for request in requests:
            if request.candidate_index is None:
                raise ValueError("CIS-Net aggregation candidate index is missing")
            identity = search_identity(request)
            db.execute(
                """INSERT OR IGNORE INTO cisnet_searches
                   (aggregation_run_id,query_hash,artifact_key,first_candidate_index,title,performer,
                    query_iswc,state,created_utc,updated_utc)
                   VALUES (?,?,?,?,?,?,?,'pending',?,?)""",
                (run_id, identity, _query_key(request), request.candidate_index,
                 request.title, request.performer, request.iswc, now, now),
            )
            search_id = db.execute(
                "SELECT id FROM cisnet_searches WHERE aggregation_run_id=? AND query_hash=?",
                (run_id, identity),
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO cisnet_candidates VALUES (?,?,?)",
                (run_id, request.candidate_index, search_id),
            )
        db.commit()


def searches(store: PipelineStore, run_id: int) -> list[dict]:
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM cisnet_searches WHERE aggregation_run_id=? ORDER BY first_candidate_index", (run_id,)
        )]


def record_result(store: PipelineStore, search_id: int, result_path: Path) -> None:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    works = result.get("works") if isinstance(result, dict) else None
    if not isinstance(works, list):
        raise ValueError("CIS-Net result has no works list")
    iswc = None
    for position, work in enumerate(works, 1):
        if not isinstance(work, dict):
            raise ValueError(f"CIS-Net work {position} is not an object")
        candidate_iswc = _usable_iswc(work.get("iswc"))
        if candidate_iswc:
            iswc = candidate_iswc
            break
    with store.connect() as db:
        db.execute(
            """UPDATE cisnet_searches SET state=?,selected_iswc=?,result_path=?,error_type=NULL,
               updated_utc=? WHERE id=? AND state='pending'""",
            ("yes" if iswc else "no", iswc, str(result_path), utcnow(), search_id),
        )
        db.commit()


def record_error(store: PipelineStore, search_id: int, error_type: str) -> None:
    with store.connect() as db:
        db.execute(
            "UPDATE cisnet_searches SET state='error',error_type=?,updated_utc=? WHERE id=? AND state='pending'",
            (error_type, utcnow(), search_id),
        )
        db.commit()


def defer_busy_session(store: PipelineStore, run_id: int) -> None:
    """Keep unfinished searches pending and start their shared retry delay."""
    now = utcnow()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """UPDATE cisnet_searches SET error_type=?,updated_utc=?
               WHERE aggregation_run_id=? AND state='pending'""",
            (BUSY_SESSION_ERROR, now, run_id),
        )
        db.execute(
            """UPDATE cisnet_runs SET state='pending',message_text=NULL,updated_utc=?
               WHERE aggregation_run_id=?""",
            (now, run_id),
        )
        db.commit()


def busy_retry_remaining(store: PipelineStore, run_id: int, delay_seconds: int = 300,
                         now: datetime | None = None) -> int:
    with store.connect() as db:
        row = db.execute(
            """SELECT MAX(updated_utc) AS deferred_utc FROM cisnet_searches
               WHERE aggregation_run_id=? AND state='pending' AND error_type=?""",
            (run_id, BUSY_SESSION_ERROR),
        ).fetchone()
    if not row or not row["deferred_utc"]:
        return 0
    deferred = datetime.fromisoformat(row["deferred_utc"].replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    return max(0, math.ceil(delay_seconds - (current - deferred).total_seconds()))


def clear_busy_session(store: PipelineStore, run_id: int) -> None:
    with store.connect() as db:
        db.execute(
            """UPDATE cisnet_searches SET error_type=NULL
               WHERE aggregation_run_id=? AND state='pending' AND error_type=?""",
            (run_id, BUSY_SESSION_ERROR),
        )
        db.commit()


def mark_run_error(store: PipelineStore, run_id: int, recognition_id: int) -> None:
    now = utcnow()
    with store.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO cisnet_runs VALUES (?,?,?,?,?,?)",
            (run_id, recognition_id, "error", None, now, now),
        )
        db.commit()


def finalize_run(store: PipelineStore, run_id: int) -> str:
    with store.connect() as db:
        rows = list(db.execute(
            "SELECT * FROM cisnet_searches WHERE aggregation_run_id=? ORDER BY first_candidate_index", (run_id,)
        ))
        if not rows:
            state, message = "empty", None
        elif any(row["state"] == "error" for row in rows):
            state, message = "error", None
        elif any(row["state"] == "pending" for row in rows):
            return "pending"
        else:
            lines = []
            seen_lines = set()
            for row in rows:
                identity = (row["title"], row["performer"], row["selected_iswc"])
                if identity in seen_lines:
                    continue
                seen_lines.add(identity)
                suffix = f"РАО ({row['selected_iswc']}) - да" if row["selected_iswc"] else "РАО - нет"
                lines.append(f"{len(lines) + 1}. {row['title']} от {row['performer']} - {suffix}")
            state, message = "ready", "\n".join(lines)
        db.execute(
            "UPDATE cisnet_runs SET state=?,message_text=?,updated_utc=? WHERE aggregation_run_id=?",
            (state, message, utcnow(), run_id),
        )
        db.commit()
        return state
