"""Conservative comparison normalization and S2 output contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any

from .identity import MetadataInconsistency, RecordingIdentity, build_identities, recording_identity_id
from .models import CandidateObservation, CanonicalRecognitionRun, RecognitionWindow


NORMALIZED_RECOGNITION_CONTRACT_VERSION = "normalized-recognition-run/v1"
_SPACE = re.compile(r"\s+")
_VERSION_PATTERNS = (
    ("radio_edit", re.compile(r"\bradio\s+edit\b")),
    ("extended", re.compile(r"\bextended(?:\s+(?:mix|version))?\b")),
    ("remaster", re.compile(r"\bremaster(?:ed)?\b")),
    ("remix", re.compile(r"\bremix\b")),
    ("live", re.compile(r"\blive\b")),
    ("acoustic", re.compile(r"\bacoustic\b")),
    ("demo", re.compile(r"\bdemo\b")),
    ("mixed", re.compile(r"\bmixed\b")),
    ("edit", re.compile(r"\bedit\b")),
)


def normalize_text(value: str | None) -> str | None:
    """Comparison-only Unicode, whitespace, and case normalization."""
    if value is None:
        return None
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", value).strip()).casefold() or None


def normalize_identifier(value: str | None, *, isrc: bool = False) -> str | None:
    normalized = normalize_text(value)
    if normalized is None:
        return None
    if isrc:
        return re.sub(r"[^0-9a-z]", "", normalized).upper() or None
    return re.sub(r"\s+", "", normalized) or None


def version_descriptors(*values: str | None) -> tuple[str, ...]:
    text = " ".join(value for value in (normalize_text(value) for value in values) if value)
    found = [name for name, pattern in _VERSION_PATTERNS if pattern.search(text)]
    if "radio_edit" in found:
        found.remove("edit")
    return tuple(found)


@dataclass(frozen=True)
class ComparisonMetadata:
    """Non-destructive comparison fields; ``None`` means unknown, not mismatch."""

    title: str | None
    artists: tuple[str, ...]
    acrid: str | None
    isrc: str | None
    album: str | None
    label: str | None
    version: str | None
    version_descriptors: tuple[str, ...]
    duration_ms: int | float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "artists": list(self.artists), "acrid": self.acrid,
            "isrc": self.isrc, "album": self.album, "label": self.label,
            "version": self.version, "version_descriptors": list(self.version_descriptors),
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class NormalizedCandidateObservation:
    """One original candidate plus comparison fields and an optional exact identity link."""

    observation_id: str
    window_id: str
    window_index: int
    original: CandidateObservation
    comparison: ComparisonMetadata
    recording_identity_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "window_id": self.window_id,
            "window_index": self.window_index, "rank": self.original.rank,
            "score": self.original.score, "play_offset": self.original.play_offset,
            "original": self.original.to_dict(), "comparison": self.comparison.to_dict(),
            "recording_identity_id": self.recording_identity_id,
        }


@dataclass(frozen=True)
class NormalizedRecognitionWindow:
    """Original chronological window with ordered normalized observations."""

    original: RecognitionWindow
    candidates: tuple[NormalizedCandidateObservation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"original": self.original.to_dict(), "candidates": [item.to_dict() for item in self.candidates]}


@dataclass(frozen=True)
class NormalizedRecognitionRun:
    """S2 input for future stages; no temporal or family aggregation is performed."""

    original: CanonicalRecognitionRun
    windows: tuple[NormalizedRecognitionWindow, ...]
    recording_identities: tuple[RecordingIdentity, ...]
    diagnostics: tuple[MetadataInconsistency, ...]
    contract_version: str = NORMALIZED_RECOGNITION_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "original": self.original.to_dict(),
            "windows": [item.to_dict() for item in self.windows],
            "recording_identities": [item.to_dict() for item in self.recording_identities],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def _comparison(candidate: CandidateObservation) -> ComparisonMetadata:
    duration = candidate.raw_values.get("duration_ms")
    return ComparisonMetadata(
        title=normalize_text(candidate.title),
        artists=tuple(value for artist in candidate.artists if (value := normalize_text(artist)) is not None),
        acrid=normalize_identifier(candidate.acrid),
        isrc=normalize_identifier(candidate.isrc, isrc=True),
        album=normalize_text(candidate.album), label=normalize_text(candidate.label),
        version=normalize_text(candidate.version),
        version_descriptors=version_descriptors(candidate.title, candidate.version),
        duration_ms=duration if isinstance(duration, (int, float)) else None,
    )


def normalize_run(run: CanonicalRecognitionRun) -> NormalizedRecognitionRun:
    """Normalize observations and create exact ACRID-only identity evidence nodes."""
    windows, all_observations = [], []
    for window in run.windows:
        candidates = []
        for candidate in window.candidates:
            comparison = _comparison(candidate)
            normalized = NormalizedCandidateObservation(
                observation_id=candidate.candidate_id,
                window_id=window.window_id,
                window_index=window.index,
                original=candidate,
                comparison=comparison,
                recording_identity_id=recording_identity_id(comparison.acrid),
            )
            candidates.append(normalized)
            all_observations.append(normalized)
        windows.append(NormalizedRecognitionWindow(window, tuple(candidates)))
    identities, diagnostics = build_identities(all_observations)
    return NormalizedRecognitionRun(run, tuple(windows), identities, diagnostics)
