"""Basic rank-1 temporal aggregation for exact ACRID recording identities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any

from .models import RawProvenancePointer, RecognitionWindowStatus
from .normalization import NormalizedCandidateObservation, NormalizedRecognitionRun


TEMPORAL_OCCURRENCE_CONTRACT_VERSION = "exact-recording-occurrence/v1"


@dataclass(frozen=True)
class ExperimentalTemporalProfile:
    """Versioned, explicitly non-production parameters for S3 replay experiments."""

    name: str
    max_no_result_bridge_windows: int
    max_processing_error_bridge_windows: int
    not_submitted_closes_occurrence: bool
    missing_raw_closes_occurrence: bool
    close_after_incompatible_windows: int


OFFLINE_EXPERIMENTAL_V1 = ExperimentalTemporalProfile(
    name="offline-experimental-v1",
    max_no_result_bridge_windows=9,
    max_processing_error_bridge_windows=2,
    not_submitted_closes_occurrence=True,
    missing_raw_closes_occurrence=True,
    close_after_incompatible_windows=1,
)

CALIBRATED_TEMPORAL_V2 = ExperimentalTemporalProfile(
    name="calibrated-v2",
    max_no_result_bridge_windows=9,
    max_processing_error_bridge_windows=2,
    not_submitted_closes_occurrence=True,
    missing_raw_closes_occurrence=True,
    close_after_incompatible_windows=1,
)


class GapKind(StrEnum):
    NO_RESULT_1001 = "no_result_1001"
    PROCESSING_ERROR = "processing_error"
    NOT_SUBMITTED = "not_submitted"
    MISSING_RAW = "missing_raw"
    INCOMPATIBLE_RANK1 = "incompatible_rank1"


class FinalizationReason(StrEnum):
    SOURCE_END = "source_end"
    INCOMPATIBLE_RANK1 = "incompatible_rank1"
    NO_RESULT_HORIZON = "no_result_horizon"
    PROCESSING_ERROR_HORIZON = "processing_error_horizon"
    NOT_SUBMITTED = "not_submitted"
    MISSING_RAW = "missing_raw"


def _id(material: Any) -> str:
    return "occurrence_" + hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class OccurrenceSupport:
    """One rank-1 exact-identity observation retained as temporal evidence."""

    observation_id: str
    window_id: str
    window_index: int
    source_start: float | int
    source_end: float | int
    rank: int
    score: float | int | None
    play_offset: float | int | None
    raw_provenance: RawProvenancePointer | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "window_id": self.window_id,
            "window_index": self.window_index, "source_start": self.source_start,
            "source_end": self.source_end, "rank": self.rank, "score": self.score,
            "play_offset": self.play_offset,
            "raw_provenance": self.raw_provenance.to_dict() if self.raw_provenance else None,
        }


@dataclass(frozen=True)
class TemporalGap:
    """One observed non-supporting window between or after exact rank-1 supports."""

    kind: GapKind
    window_id: str
    window_index: int
    source_start: float | int
    source_end: float | int
    bridged: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "window_id": self.window_id, "window_index": self.window_index,
            "source_start": self.source_start, "source_end": self.source_end, "bridged": self.bridged,
        }


@dataclass(frozen=True)
class ExactRecordingOccurrence:
    """A provisional/finalized occurrence of exactly one ACRID identity."""

    occurrence_id: str
    recording_identity_id: str
    supports: tuple[OccurrenceSupport, ...]
    first_support_time: float | int
    last_support_time: float | int
    observed_envelope_start: float | int
    observed_envelope_end: float | int
    gaps: tuple[TemporalGap, ...]
    finalization_reason: FinalizationReason
    singleton: bool
    confidence_state: str
    source_provenance: RawProvenancePointer | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id, "recording_identity_id": self.recording_identity_id,
            "supports": [item.to_dict() for item in self.supports],
            "first_support_time": self.first_support_time, "last_support_time": self.last_support_time,
            "observed_envelope": {"start": self.observed_envelope_start, "end": self.observed_envelope_end},
            "gaps": [item.to_dict() for item in self.gaps],
            "finalization_reason": self.finalization_reason.value, "singleton": self.singleton,
            "confidence_state": self.confidence_state,
            "source_provenance": self.source_provenance.to_dict() if self.source_provenance else None,
        }


@dataclass(frozen=True)
class TemporalAggregationResult:
    """S3 output retaining the complete normalized input for lower-rank traceability."""

    original: NormalizedRecognitionRun
    occurrences: tuple[ExactRecordingOccurrence, ...]
    profile: ExperimentalTemporalProfile
    contract_version: str = TEMPORAL_OCCURRENCE_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "profile": self.profile.name,
            "original": self.original.to_dict(), "occurrences": [item.to_dict() for item in self.occurrences],
        }


def _gap_kind(status: RecognitionWindowStatus) -> GapKind:
    return GapKind(status.value)


def _support(window: Any, observation: NormalizedCandidateObservation) -> OccurrenceSupport:
    return OccurrenceSupport(
        observation_id=observation.observation_id, window_id=window.original.window_id,
        window_index=window.original.index, source_start=window.original.source_start,
        source_end=window.original.source_end, rank=observation.original.rank,
        score=observation.original.score, play_offset=observation.original.play_offset,
        raw_provenance=observation.original.raw_provenance,
    )


def _gap(window: Any, kind: GapKind, bridged: bool = False) -> TemporalGap:
    return TemporalGap(kind, window.original.window_id, window.original.index,
                       window.original.source_start, window.original.source_end, bridged)


def _pending_reason(pending: list[TemporalGap], profile: ExperimentalTemporalProfile) -> FinalizationReason | None:
    kinds = [item.kind for item in pending]
    if GapKind.NOT_SUBMITTED in kinds and profile.not_submitted_closes_occurrence:
        return FinalizationReason.NOT_SUBMITTED
    if GapKind.MISSING_RAW in kinds and profile.missing_raw_closes_occurrence:
        return FinalizationReason.MISSING_RAW
    if kinds.count(GapKind.NO_RESULT_1001) > profile.max_no_result_bridge_windows:
        return FinalizationReason.NO_RESULT_HORIZON
    if kinds.count(GapKind.PROCESSING_ERROR) > profile.max_processing_error_bridge_windows:
        return FinalizationReason.PROCESSING_ERROR_HORIZON
    if kinds.count(GapKind.INCOMPATIBLE_RANK1) >= profile.close_after_incompatible_windows:
        return FinalizationReason.INCOMPATIBLE_RANK1
    return None


def _finalize(active: dict[str, Any], reason: FinalizationReason, source_provenance: RawProvenancePointer | None) -> ExactRecordingOccurrence:
    supports = tuple(active["supports"])
    gaps = tuple(active["gaps"])
    return ExactRecordingOccurrence(
        occurrence_id=_id({"identity": active["identity"], "supports": [item.observation_id for item in supports], "gaps": [item.to_dict() for item in gaps]}),
        recording_identity_id=active["identity"], supports=supports,
        first_support_time=supports[0].source_start, last_support_time=supports[-1].source_end,
        observed_envelope_start=supports[0].source_start, observed_envelope_end=supports[-1].source_end,
        gaps=gaps, finalization_reason=reason, singleton=len(supports) == 1,
        confidence_state="not_calibrated", source_provenance=source_provenance,
    )


def aggregate_temporally(run: NormalizedRecognitionRun, profile: ExperimentalTemporalProfile = OFFLINE_EXPERIMENTAL_V1) -> TemporalAggregationResult:
    """Create exact-ACRID occurrences from rank-1 evidence only.

    Lower-rank observations stay intact under ``result.original`` but never open,
    resume, or conflict an S3 occurrence.
    """
    occurrences: list[ExactRecordingOccurrence] = []
    active: dict[str, Any] | None = None
    source_provenance = run.original.source_provenance
    for window in run.windows:
        rank_one = next((item for item in window.candidates if item.original.rank == 1), None)
        identity = rank_one.recording_identity_id if rank_one else None
        if identity:
            if active is None:
                active = {"identity": identity, "supports": [_support(window, rank_one)], "gaps": [], "pending": []}
                continue
            if active["identity"] == identity:
                reason = _pending_reason(active["pending"], profile)
                if reason is None:
                    active["gaps"].extend(TemporalGap(item.kind, item.window_id, item.window_index, item.source_start, item.source_end, True) for item in active["pending"])
                    active["pending"] = []
                    active["supports"].append(_support(window, rank_one))
                    continue
                active["gaps"].extend(active["pending"])
                occurrences.append(_finalize(active, reason, source_provenance))
            else:
                occurrences.append(_finalize(active, FinalizationReason.INCOMPATIBLE_RANK1, source_provenance))
            active = {"identity": identity, "supports": [_support(window, rank_one)], "gaps": [], "pending": []}
            continue
        if active is None:
            continue
        status = window.original.status
        if status is RecognitionWindowStatus.CANDIDATES_RETURNED:
            active["pending"].append(_gap(window, GapKind.INCOMPATIBLE_RANK1))
        else:
            active["pending"].append(_gap(window, _gap_kind(status)))
        reason = _pending_reason(active["pending"], profile)
        if reason is not None:
            active["gaps"].extend(active["pending"])
            occurrences.append(_finalize(active, reason, source_provenance))
            active = None
    if active is not None:
        occurrences.append(_finalize(active, FinalizationReason.SOURCE_END, source_provenance))
    return TemporalAggregationResult(run, tuple(occurrences), profile)
