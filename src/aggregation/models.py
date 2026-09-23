"""Dependency-free canonical recognition contracts (S0).

These models deliberately contain observations only.  They do not infer track
families, confidence, or final verification outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping


CANONICAL_RECOGNITION_CONTRACT_VERSION = "canonical-recognition-run/v1"


def _canonical_json(value: Any) -> str:
    """Encode JSON-compatible identity material deterministically."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _semantic_id(kind: str, material: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return f"{kind}_{digest}"


class RecognitionWindowStatus(StrEnum):
    """Observed processing state of one recognition window."""

    CANDIDATES_RETURNED = "candidates_returned"
    NO_RESULT_1001 = "no_result_1001"
    PROCESSING_ERROR = "processing_error"
    NOT_SUBMITTED = "not_submitted"
    MISSING_RAW = "missing_raw"


@dataclass(frozen=True)
class RawProvenancePointer:
    """Opaque, adapter-supplied location of the original raw record."""

    adapter: str
    record_id: str
    locator: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.adapter or not self.record_id:
            raise ValueError("raw provenance requires adapter and record_id")

    def to_dict(self) -> dict[str, Any]:
        return {"adapter": self.adapter, "record_id": self.record_id, "locator": dict(self.locator)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RawProvenancePointer":
        return cls(value["adapter"], value["record_id"], value.get("locator", {}))


@dataclass(frozen=True)
class CandidateObservation:
    """One ordered candidate returned for a window; absent metadata is unknown."""

    rank: int
    score: float | int | None
    acrid: str | None = None
    isrc: str | None = None
    title: str | None = None
    artists: tuple[str, ...] = ()
    album: str | None = None
    label: str | None = None
    version: str | None = None
    play_offset: float | int | None = None
    raw_provenance: RawProvenancePointer | None = None
    raw_values: Mapping[str, Any] = field(default_factory=dict)
    candidate_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("candidate rank must be at least 1")
        object.__setattr__(self, "artists", tuple(self.artists))
        object.__setattr__(
            self,
            "candidate_id",
            _semantic_id(
                "candidate",
                {
                    "rank": self.rank,
                    "score": self.score,
                    "acrid": self.acrid,
                    "isrc": self.isrc,
                    "title": self.title,
                    "artists": self.artists,
                    "album": self.album,
                    "label": self.label,
                    "version": self.version,
                    "play_offset": self.play_offset,
                    "raw_provenance": self.raw_provenance.to_dict() if self.raw_provenance else None,
                    "raw_values": self.raw_values,
                },
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id, "rank": self.rank, "score": self.score,
            "acrid": self.acrid, "isrc": self.isrc, "title": self.title,
            "artists": list(self.artists), "album": self.album, "label": self.label,
            "version": self.version, "play_offset": self.play_offset,
            "raw_provenance": self.raw_provenance.to_dict() if self.raw_provenance else None,
            "raw_values": dict(self.raw_values),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateObservation":
        provenance = value.get("raw_provenance")
        return cls(
            rank=value["rank"], score=value.get("score"), acrid=value.get("acrid"),
            isrc=value.get("isrc"), title=value.get("title"), artists=tuple(value.get("artists", ())),
            album=value.get("album"), label=value.get("label"), version=value.get("version"),
            play_offset=value.get("play_offset"),
            raw_provenance=RawProvenancePointer.from_dict(provenance) if provenance else None,
            raw_values=value.get("raw_values", {}),
        )


@dataclass(frozen=True)
class RecognitionWindow:
    """One chronological, source-relative recognition attempt."""

    index: int
    source_start: float | int
    source_end: float | int
    actual_duration: float | int
    status: RecognitionWindowStatus
    candidates: tuple[CandidateObservation, ...] = ()
    raw_provenance: RawProvenancePointer | None = None
    raw_values: Mapping[str, Any] = field(default_factory=dict)
    window_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.index < 0 or self.source_start < 0 or self.source_end < self.source_start:
            raise ValueError("invalid chronological window bounds")
        if self.actual_duration < 0:
            raise ValueError("window actual_duration must not be negative")
        candidates = tuple(self.candidates)
        if tuple(candidate.rank for candidate in candidates) != tuple(range(1, len(candidates) + 1)):
            raise ValueError("candidate ranks must be ordered consecutively from 1")
        if self.status is RecognitionWindowStatus.CANDIDATES_RETURNED and not candidates:
            raise ValueError("candidates_returned requires at least one candidate")
        if self.status is not RecognitionWindowStatus.CANDIDATES_RETURNED and candidates:
            raise ValueError("only candidates_returned may carry candidates")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(
            self, "window_id", _semantic_id("window", {
                "index": self.index, "source_start": self.source_start, "source_end": self.source_end,
                "actual_duration": self.actual_duration,
            })
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id, "index": self.index, "source_start": self.source_start,
            "source_end": self.source_end, "actual_duration": self.actual_duration,
            "status": self.status.value, "candidates": [item.to_dict() for item in self.candidates],
            "raw_provenance": self.raw_provenance.to_dict() if self.raw_provenance else None,
            "raw_values": dict(self.raw_values),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecognitionWindow":
        provenance = value.get("raw_provenance")
        return cls(
            index=value["index"], source_start=value["source_start"], source_end=value["source_end"],
            actual_duration=value["actual_duration"], status=RecognitionWindowStatus(value["status"]),
            candidates=tuple(CandidateObservation.from_dict(item) for item in value.get("candidates", ())),
            raw_provenance=RawProvenancePointer.from_dict(provenance) if provenance else None,
            raw_values=value.get("raw_values", {}),
        )


@dataclass(frozen=True)
class CanonicalRecognitionRun:
    """Canonical input from an offline or production recognition adapter."""

    run_id: str
    source_id: str
    source_duration: float | int
    window_size: float | int
    step: float | int
    windows: tuple[RecognitionWindow, ...]
    contract_version: str = CANONICAL_RECOGNITION_CONTRACT_VERSION
    source_provenance: RawProvenancePointer | None = None
    source_raw_values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id or not self.source_id:
            raise ValueError("run_id and source_id are required")
        if self.source_duration < 0 or self.window_size <= 0 or self.step <= 0:
            raise ValueError("source_duration must be non-negative; window_size and step positive")
        windows = tuple(self.windows)
        if tuple(window.index for window in windows) != tuple(range(len(windows))):
            raise ValueError("windows must preserve chronological consecutive indexes from 0")
        if any(right.source_start < left.source_start for left, right in zip(windows, windows[1:])):
            raise ValueError("windows must be in chronological order")
        object.__setattr__(self, "windows", windows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "run_id": self.run_id, "source_id": self.source_id,
            "source_duration": self.source_duration, "window_size": self.window_size, "step": self.step,
            "windows": [window.to_dict() for window in self.windows],
            "source_provenance": self.source_provenance.to_dict() if self.source_provenance else None,
            "source_raw_values": dict(self.source_raw_values),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalRecognitionRun":
        provenance = value.get("source_provenance")
        return cls(
            contract_version=value.get("contract_version", CANONICAL_RECOGNITION_CONTRACT_VERSION),
            run_id=value["run_id"], source_id=value["source_id"], source_duration=value["source_duration"],
            window_size=value["window_size"], step=value["step"],
            windows=tuple(RecognitionWindow.from_dict(item) for item in value["windows"]),
            source_provenance=RawProvenancePointer.from_dict(provenance) if provenance else None,
            source_raw_values=value.get("source_raw_values", {}),
        )
