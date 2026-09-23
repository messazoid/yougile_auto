"""Adapt an unpacked offline ACRCloud run into S0 canonical contracts.

The authoritative input is ``responses.jsonl`` because it retains one raw ACR
response per submitted window.  ``local_windows.jsonl`` is a non-overlapping
ledger of windows skipped before submission.  ``matches.jsonl`` is derived
output and is deliberately not parsed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.aggregation import (
    CandidateObservation,
    CanonicalRecognitionRun,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
)


ADAPTER_NAME = "offline.acrcloud.archive/v1"


class ArchiveFormatError(ValueError):
    """The unpacked run does not have the confirmed offline archive format."""


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArchiveFormatError(f"cannot read {path.name}") from error
    if not isinstance(value, dict):
        raise ArchiveFormatError(f"{path.name} must contain an object")
    return value


def _read_jsonl(path: Path) -> Iterable[tuple[int, Mapping[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ArchiveFormatError(f"cannot read {path.name}") from error
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ArchiveFormatError(f"invalid JSON at {path.name}:{line_number}") from error
        if not isinstance(value, dict):
            raise ArchiveFormatError(f"{path.name}:{line_number} must contain an object")
        yield line_number, value


def _provenance(run: Path, filename: str, record_id: str, **locator: Any) -> RawProvenancePointer:
    return RawProvenancePointer(
        adapter=ADAPTER_NAME,
        record_id=f"{run.name}:{record_id}",
        locator={"run_directory": run.name, "file": filename, **locator},
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


def _candidate_observations(run: Path, window_index: int, payload: Mapping[str, Any]) -> tuple[CandidateObservation, ...]:
    metadata = payload.get("metadata")
    music = metadata.get("music") if isinstance(metadata, dict) else None
    if not isinstance(music, list):
        return ()
    observations = []
    for rank, raw in enumerate(music, start=1):
        if not isinstance(raw, dict):
            raise ArchiveFormatError(f"response window {window_index} has non-object music candidate")
        external_ids = raw.get("external_ids")
        isrc = external_ids.get("isrc") if isinstance(external_ids, dict) else None
        observations.append(CandidateObservation(
            rank=rank,
            score=raw.get("score"),
            acrid=raw.get("acrid"),
            isrc=isrc if isinstance(isrc, str) else None,
            title=raw.get("title") if isinstance(raw.get("title"), str) else None,
            artists=_artists(raw.get("artists")),
            album=_name(raw.get("album")),
            label=raw.get("label") if isinstance(raw.get("label"), str) else None,
            version=raw.get("version") if isinstance(raw.get("version"), str) else None,
            play_offset=raw.get("play_offset_ms"),
            raw_provenance=_provenance(
                run, "responses.jsonl", f"candidate:{window_index}:{rank}",
                window_index=window_index, candidate_rank=rank,
            ),
            raw_values=raw,
        ))
    return tuple(observations)


def _response_status(record: Mapping[str, Any], candidates: tuple[CandidateObservation, ...]) -> RecognitionWindowStatus:
    if record.get("state") != "done":
        return RecognitionWindowStatus.PROCESSING_ERROR
    if not isinstance(record.get("response_text"), str):
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


def _parse_response(run: Path, line_number: int, record: Mapping[str, Any]) -> tuple[int, RecognitionWindow]:
    index = record.get("window_index")
    start = record.get("start_seconds")
    duration = record.get("duration_seconds")
    if not isinstance(index, int) or not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
        raise ArchiveFormatError(f"responses.jsonl:{line_number} lacks window timing")
    payload: Mapping[str, Any] = {}
    response_text = record.get("response_text")
    if isinstance(response_text, str):
        try:
            decoded = json.loads(response_text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            payload = decoded
    candidates = _candidate_observations(run, index, payload)
    status = _response_status(record, candidates)
    if status is not RecognitionWindowStatus.CANDIDATES_RETURNED:
        candidates = ()
    return index, RecognitionWindow(
        index=index, source_start=start, source_end=start + duration, actual_duration=duration,
        status=status, candidates=candidates,
        raw_provenance=_provenance(run, "responses.jsonl", f"response:{index}", line=line_number, window_index=index),
        raw_values=record,
    )


def _expected_windows(config: Mapping[str, Any]) -> list[tuple[int, float, float]]:
    audio = config.get("audio")
    if not isinstance(audio, dict):
        raise ArchiveFormatError("job.json config.audio is required")
    frames, rate = audio.get("frames"), audio.get("sample_rate")
    window, step = config.get("window_seconds"), config.get("step_seconds")
    if not isinstance(frames, int) or not isinstance(rate, int) or not isinstance(window, (int, float)) or not isinstance(step, (int, float)):
        raise ArchiveFormatError("job.json has invalid audio or window configuration")
    if frames < 0 or rate <= 0 or window <= 0 or step <= 0:
        raise ArchiveFormatError("job.json has non-positive audio or window configuration")
    step_frames, window_frames = int(step * rate), int(window * rate)
    if step_frames <= 0 or window_frames <= 0:
        raise ArchiveFormatError("job.json window configuration cannot be represented in frames")
    return [(index, start / rate, min(window_frames, frames - start) / rate)
            for index, start in enumerate(range(0, frames, step_frames))]


def load_run(run_directory: str | Path) -> CanonicalRecognitionRun:
    """Load one unpacked offline run directory without touching production state."""
    run = Path(run_directory)
    job = _read_json(run / "job.json")
    sources = _read_json(run / "sources.json")
    config = job.get("config")
    if not isinstance(config, dict):
        raise ArchiveFormatError("job.json config is required")
    expected = _expected_windows(config)
    submitted: dict[int, RecognitionWindow] = {}
    for line_number, record in _read_jsonl(run / "responses.jsonl"):
        index, item = _parse_response(run, line_number, record)
        if index in submitted:
            raise ArchiveFormatError(f"duplicate response for window {index}")
        submitted[index] = item
    local: dict[int, Mapping[str, Any]] = {}
    for line_number, record in _read_jsonl(run / "local_windows.jsonl"):
        index = record.get("window_index")
        if not isinstance(index, int) or index in local:
            raise ArchiveFormatError(f"invalid local window at local_windows.jsonl:{line_number}")
        local[index] = record
    if set(submitted) & set(local):
        raise ArchiveFormatError("a window appears in both response and local ledgers")
    expected_indexes = {index for index, _, _ in expected}
    if not (set(submitted) | set(local)) <= expected_indexes:
        raise ArchiveFormatError("archive contains a window outside the persisted schedule")
    windows = []
    for index, start, duration in expected:
        if index in submitted:
            windows.append(submitted[index])
        elif index in local:
            record = local[index]
            local_start = record.get("start_seconds")
            local_duration = record.get("duration_seconds")
            if not isinstance(local_start, (int, float)) or not isinstance(local_duration, (int, float)):
                raise ArchiveFormatError(f"local window {index} lacks timing")
            windows.append(RecognitionWindow(
                index=index, source_start=local_start, source_end=local_start + local_duration,
                actual_duration=local_duration,
                status=RecognitionWindowStatus.NOT_SUBMITTED,
                raw_provenance=_provenance(run, "local_windows.jsonl", f"local:{index}", window_index=index),
                raw_values=record,
            ))
        else:
            windows.append(RecognitionWindow(
                index=index, source_start=start, source_end=start + duration, actual_duration=duration,
                status=RecognitionWindowStatus.MISSING_RAW,
                raw_provenance=_provenance(run, "derived", f"missing:{index}", window_index=index),
            ))
    audio = config["audio"]
    source_duration = audio.get("duration_seconds")
    source_id = job.get("audio_sha256") or audio.get("sha256")
    if not isinstance(source_duration, (int, float)) or not isinstance(source_id, str) or not source_id:
        raise ArchiveFormatError("job.json lacks source identity or duration")
    return CanonicalRecognitionRun(
        run_id=run.name, source_id=source_id, source_duration=source_duration,
        window_size=config["window_seconds"], step=config["step_seconds"], windows=tuple(windows),
        source_provenance=_provenance(run, "sources.json", "source-mapping"),
        source_raw_values=sources,
    )
