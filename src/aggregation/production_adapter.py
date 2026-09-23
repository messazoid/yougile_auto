"""Read production recognition ledgers into the S0 canonical contract.

This module deliberately reads the authoritative pipeline and scanner SQLite
databases.  It does not consume derived JSONL exports and never mutates a
recognition artifact.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .models import (
    CANONICAL_RECOGNITION_CONTRACT_VERSION,
    CandidateObservation,
    CanonicalRecognitionRun,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
)

if TYPE_CHECKING:
    from job_store import PipelineStore


ADAPTER_VERSION = "production.pipeline.scan/v1"


class ProductionAdapterError(ValueError):
    """The durable production recognition data cannot form a canonical run."""


def serialize_canonical_run(run: CanonicalRecognitionRun) -> str:
    """Stable semantic JSON for an immutable aggregation input snapshot."""
    return json.dumps(run.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_run_hash(run: CanonicalRecognitionRun) -> str:
    return hashlib.sha256(serialize_canonical_run(run).encode("utf-8")).hexdigest()


def _provenance(recognition_id: int, record_id: str, **locator: Any) -> RawProvenancePointer:
    return RawProvenancePointer(
        adapter=ADAPTER_VERSION,
        record_id=f"recognition:{recognition_id}:{record_id}",
        locator={"recognition_id": recognition_id, "database": "scan.sqlite3", **locator},
    )


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return value if isinstance(value, str) else None


def _artists(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(name for item in value if (name := _name(item)) is not None)


def _payload(raw: bytes | None) -> tuple[str | None, Mapping[str, Any]]:
    if raw is None:
        return None, {}
    text = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    return text, value if isinstance(value, dict) else {}


def _candidate_observations(
    recognition_id: int, attempt_id: int, window_index: int, payload: Mapping[str, Any],
) -> tuple[CandidateObservation, ...]:
    metadata = payload.get("metadata")
    music = metadata.get("music") if isinstance(metadata, dict) else None
    if not isinstance(music, list):
        return ()
    observations = []
    for rank, raw in enumerate(music, start=1):
        if not isinstance(raw, dict):
            raise ProductionAdapterError(
                f"attempt {attempt_id} window {window_index} has non-object music candidate"
            )
        external_ids = raw.get("external_ids")
        isrc = external_ids.get("isrc") if isinstance(external_ids, dict) else None
        observations.append(CandidateObservation(
            rank=rank,
            score=raw.get("score"),
            acrid=raw.get("acrid") if isinstance(raw.get("acrid"), str) else None,
            isrc=isrc if isinstance(isrc, str) else None,
            title=raw.get("title") if isinstance(raw.get("title"), str) else None,
            artists=_artists(raw.get("artists")),
            album=_name(raw.get("album")),
            label=raw.get("label") if isinstance(raw.get("label"), str) else None,
            version=raw.get("version") if isinstance(raw.get("version"), str) else None,
            play_offset=raw.get("play_offset_ms"),
            raw_provenance=_provenance(
                recognition_id, f"candidate:{attempt_id}:{rank}", table="attempts",
                attempt_id=attempt_id, window_index=window_index, candidate_rank=rank,
            ),
            raw_values=raw,
        ))
    return tuple(observations)


def _response_status(
    record: Mapping[str, Any], response_text: str | None,
    candidates: tuple[CandidateObservation, ...],
) -> RecognitionWindowStatus:
    if record.get("state") != "done":
        return RecognitionWindowStatus.PROCESSING_ERROR
    if not isinstance(response_text, str):
        return RecognitionWindowStatus.MISSING_RAW
    if record.get("http_status") != 200:
        return RecognitionWindowStatus.PROCESSING_ERROR
    if record.get("acr_code") == 1001:
        return RecognitionWindowStatus.NO_RESULT_1001
    if record.get("acr_code") == 0 and candidates:
        return RecognitionWindowStatus.CANDIDATES_RETURNED
    if record.get("acr_code") == 0:
        return RecognitionWindowStatus.MISSING_RAW
    return RecognitionWindowStatus.PROCESSING_ERROR


def _expected_windows(config: Mapping[str, Any]) -> list[tuple[int, float, float]]:
    audio = config.get("audio")
    if not isinstance(audio, dict):
        raise ProductionAdapterError("scan metadata lacks audio configuration")
    frames, rate = audio.get("frames"), audio.get("sample_rate")
    window, step = config.get("window_seconds"), config.get("step_seconds")
    if not isinstance(frames, int) or not isinstance(rate, int) or not isinstance(window, (int, float)) or not isinstance(step, (int, float)):
        raise ProductionAdapterError("scan metadata has invalid window configuration")
    if frames < 0 or rate <= 0 or window <= 0 or step <= 0:
        raise ProductionAdapterError("scan metadata has non-positive audio or window configuration")
    step_frames, window_frames = int(step * rate), int(window * rate)
    if step_frames <= 0 or window_frames <= 0:
        raise ProductionAdapterError("scan metadata window configuration cannot be represented in frames")
    return [
        (index, start / rate, min(window_frames, frames - start) / rate)
        for index, start in enumerate(range(0, frames, step_frames))
    ]


def _source_mapping(store: "PipelineStore", recognition_id: int) -> Mapping[str, Any]:
    return {"schema_version": 1, "logical_sources": store.source_manifests_for_recognition(recognition_id)}


def load_production_run(store: "PipelineStore", recognition_id: int) -> CanonicalRecognitionRun:
    """Build a canonical run from pipeline metadata and authoritative scan SQLite."""
    recognition = store.recognition(recognition_id)
    if not recognition:
        raise ProductionAdapterError("recognition does not exist")
    result_dir = Path(recognition["result_dir"])
    scan_path = result_dir / "scan.sqlite3"
    if not scan_path.is_file():
        raise ProductionAdapterError("recognition scan.sqlite3 is unavailable")
    try:
        db = sqlite3.connect(scan_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise ProductionAdapterError("cannot open recognition scan.sqlite3 read-only") from error
    try:
        db.row_factory = sqlite3.Row
        metadata = db.execute("SELECT config FROM metadata WHERE id=1").fetchone()
        if not metadata:
            raise ProductionAdapterError("scan metadata is missing")
        try:
            config = json.loads(metadata["config"])
        except json.JSONDecodeError as error:
            raise ProductionAdapterError("scan metadata is invalid JSON") from error
        if not isinstance(config, dict):
            raise ProductionAdapterError("scan metadata config is not an object")
        expected = _expected_windows(config)
        expected_indexes = {index for index, _, _ in expected}
        attempts: dict[int, dict[str, Any]] = {}
        for raw_row in db.execute("SELECT * FROM attempts ORDER BY id"):
            row = dict(raw_row)
            index = row.get("window_index")
            if not isinstance(index, int) or index not in expected_indexes:
                raise ProductionAdapterError("scan attempt has an invalid window index")
            attempts[index] = row  # Later retries supersede earlier attempts for the same window.
        local: dict[int, dict[str, Any]] = {}
        for raw_row in db.execute("SELECT * FROM local_windows ORDER BY window_index"):
            row = dict(raw_row)
            index = row.get("window_index")
            if not isinstance(index, int) or index not in expected_indexes:
                raise ProductionAdapterError("local window has an invalid window index")
            local[index] = row
        if set(attempts) & set(local):
            raise ProductionAdapterError("a window appears in both attempts and local_windows")
        windows = []
        for index, derived_start, derived_duration in expected:
            if index in attempts:
                row = attempts[index]
                response_raw = row.pop("response_raw")
                response_text, payload = _payload(response_raw)
                row["response_text"] = response_text
                start, duration = row.get("start_seconds"), row.get("duration_seconds")
                if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
                    raise ProductionAdapterError(f"attempt {row['id']} lacks window timing")
                candidates = _candidate_observations(recognition_id, row["id"], index, payload)
                status = _response_status(row, response_text, candidates)
                if status is not RecognitionWindowStatus.CANDIDATES_RETURNED:
                    candidates = ()
                windows.append(RecognitionWindow(
                    index=index, source_start=start, source_end=start + duration,
                    actual_duration=duration, status=status, candidates=candidates,
                    raw_provenance=_provenance(
                        recognition_id, f"attempt:{row['id']}", table="attempts",
                        attempt_id=row["id"], window_index=index,
                    ),
                    raw_values=row,
                ))
            elif index in local:
                row = local[index]
                start, duration = row.get("start_seconds"), row.get("duration_seconds")
                if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
                    raise ProductionAdapterError(f"local window {index} lacks timing")
                windows.append(RecognitionWindow(
                    index=index, source_start=start, source_end=start + duration,
                    actual_duration=duration, status=RecognitionWindowStatus.NOT_SUBMITTED,
                    raw_provenance=_provenance(
                        recognition_id, f"local:{index}", table="local_windows", window_index=index,
                    ),
                    raw_values=row,
                ))
            else:
                windows.append(RecognitionWindow(
                    index=index, source_start=derived_start, source_end=derived_start + derived_duration,
                    actual_duration=derived_duration, status=RecognitionWindowStatus.MISSING_RAW,
                    raw_provenance=_provenance(
                        recognition_id, f"missing:{index}", table="derived", window_index=index,
                    ),
                ))
    finally:
        db.close()
    audio = config.get("audio")
    source_duration = audio.get("duration_seconds") if isinstance(audio, dict) else None
    if not isinstance(source_duration, (int, float)):
        raise ProductionAdapterError("scan metadata lacks source duration")
    return CanonicalRecognitionRun(
        run_id=result_dir.name, source_id=recognition["audio_sha256"], source_duration=source_duration,
        window_size=config["window_seconds"], step=config["step_seconds"], windows=tuple(windows),
        contract_version=CANONICAL_RECOGNITION_CONTRACT_VERSION,
        source_provenance=_provenance(
            recognition_id, "source-mapping", table="source_jobs", result_directory=result_dir.name,
        ),
        source_raw_values=_source_mapping(store, recognition_id),
    )
