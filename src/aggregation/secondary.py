"""Secondary-candidate evidence accounting and persistent family competitors (S5)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any

from .family import FamilyAggregationResult


SECONDARY_EVIDENCE_CONTRACT_VERSION = "secondary-family-evidence/v1"


@dataclass(frozen=True)
class AggregationProfile:
    """Versioned, non-production persistence settings for offline replay."""

    name: str
    minimum_persistent_competitor_support: int
    max_competitor_window_distance: int
    transition_lookaround_windows: int
    minimum_sustained_new_family_support: int
    repeated_appearance_separation_windows: int
    max_unsupported_continuation_windows: int
    boundary_lookaround_windows: int
    play_offset_timeline_tolerance_ms: int | None = None
    minimum_play_offset_timeline_support: int = 2


OFFLINE_AGGREGATION_EXPERIMENTAL_V1 = AggregationProfile(
    name="offline-experimental-v1",
    minimum_persistent_competitor_support=2,
    max_competitor_window_distance=3,
    transition_lookaround_windows=1,
    minimum_sustained_new_family_support=2,
    repeated_appearance_separation_windows=2,
    max_unsupported_continuation_windows=2,
    boundary_lookaround_windows=1,
)

CALIBRATED_AGGREGATION_V2 = AggregationProfile(
    name="calibrated-v2",
    minimum_persistent_competitor_support=2,
    max_competitor_window_distance=3,
    transition_lookaround_windows=1,
    minimum_sustained_new_family_support=2,
    repeated_appearance_separation_windows=2,
    max_unsupported_continuation_windows=2,
    boundary_lookaround_windows=1,
    play_offset_timeline_tolerance_ms=12000,
    minimum_play_offset_timeline_support=2,
)


class SecondaryEvidenceKind(StrEnum):
    DOMINANT_SUPPORT = "dominant_support"
    SAME_FAMILY_SECONDARY = "same_family_secondary"
    WEAK_ALTERNATIVE = "weak_alternative"
    PERSISTENT_COMPETITOR = "persistent_competitor"
    UNASSIGNED = "unassigned"


class ConflictKind(StrEnum):
    RANK_COMPETITION = "rank_competition"
    VERSION_CHURN = "version_churn"
    SAME_TITLE_DIFFERENT_ARTIST = "same_title_different_artist"
    IDENTIFIER_CONTRADICTION = "identifier_contradiction"
    CATALOGUE_DUPLICATE = "catalogue_duplicate"
    POSSIBLE_SAMPLE_OR_MASHUP = "possible_sample_or_mashup"
    SPARSE_UNRESOLVED = "sparse_unresolved"


class ResolutionState(StrEnum):
    RESOLVED = "resolved"
    FAMILY_RESOLVED_VERSION_AMBIGUOUS = "family_resolved_version_ambiguous"
    AMBIGUOUS = "ambiguous"
    COMPETING = "competing"
    POSSIBLE_OVERLAP = "possible_overlap"
    UNRESOLVED = "unresolved"


def _id(kind: str, material: Any) -> str:
    return f"{kind}_" + hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class SecondaryEvidence:
    observation_id: str
    window_id: str
    window_index: int
    source_start: float | int
    source_end: float | int
    family_id: str | None
    family_occurrence_id: str | None
    rank: int
    score: float | int | None
    kind: SecondaryEvidenceKind
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "window_id": self.window_id,
            "window_index": self.window_index, "family_id": self.family_id,
            "source_start": self.source_start, "source_end": self.source_end,
            "family_occurrence_id": self.family_occurrence_id, "rank": self.rank,
            "score": self.score, "kind": self.kind.value, "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class FamilyConflict:
    conflict_id: str
    kind: ConflictKind
    resolution_state: ResolutionState
    dominant_family_id: str | None
    competing_family_id: str | None
    family_occurrence_id: str | None
    observation_ids: tuple[str, ...]
    window_indices: tuple[int, ...]
    source_start: float | int
    source_end: float | int
    persistence_support_count: int
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id, "kind": self.kind.value,
            "resolution_state": self.resolution_state.value, "dominant_family_id": self.dominant_family_id,
            "competing_family_id": self.competing_family_id, "family_occurrence_id": self.family_occurrence_id,
            "observation_ids": list(self.observation_ids), "window_indices": list(self.window_indices),
            "source_start": self.source_start, "source_end": self.source_end,
            "persistence_support_count": self.persistence_support_count, "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class FamilyOccurrenceSecondaryEvidence:
    family_occurrence_id: str
    dominant_family_id: str
    secondary_observation_ids: tuple[str, ...]
    competitor_family_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    resolution_state: ResolutionState

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_occurrence_id": self.family_occurrence_id, "dominant_family_id": self.dominant_family_id,
            "secondary_observation_ids": list(self.secondary_observation_ids),
            "competitor_family_ids": list(self.competitor_family_ids), "conflict_ids": list(self.conflict_ids),
            "resolution_state": self.resolution_state.value,
        }


@dataclass(frozen=True)
class SecondaryEvidenceAggregationResult:
    """S5 result; original S0-S4 evidence remains available under ``original``."""

    original: FamilyAggregationResult
    evidence: tuple[SecondaryEvidence, ...]
    occurrence_evidence: tuple[FamilyOccurrenceSecondaryEvidence, ...]
    conflicts: tuple[FamilyConflict, ...]
    unassigned_observation_ids: tuple[str, ...]
    profile: AggregationProfile
    contract_version: str = SECONDARY_EVIDENCE_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "profile": self.profile.name,
            "original": self.original.to_dict(), "evidence": [item.to_dict() for item in self.evidence],
            "occurrence_evidence": [item.to_dict() for item in self.occurrence_evidence],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "unassigned_observation_ids": list(self.unassigned_observation_ids),
        }


def semantic_evidence_order_key(value: SecondaryEvidence) -> tuple:
    """Chronological adapter-neutral order for lower-rank evidence.

    ``observation_id`` is only a final fallback for observations that are
    otherwise semantically identical.  It must not choose the order of normal
    competitor evidence because it incorporates raw provenance.
    """
    return (
        value.window_index, value.source_start, value.source_end, value.rank,
        value.family_id or "", value.kind.value, value.reason_codes, value.observation_id,
    )


def semantic_conflict_order_key(
    value: FamilyConflict, evidence_by_observation: dict[str, SecondaryEvidence],
) -> tuple:
    """Stable presentation order without using the provenance-derived conflict hash."""
    evidence = tuple(
        semantic_evidence_order_key(evidence_by_observation[observation_id])
        for observation_id in value.observation_ids
    )
    return (
        value.source_start, value.source_end, value.dominant_family_id or "",
        value.competing_family_id or "", value.kind.value, value.resolution_state.value,
        value.window_indices, evidence, value.reason_codes, value.conflict_id,
    )


def _dominant_windows(result: FamilyAggregationResult) -> dict[int, tuple[str, str]]:
    exact = {item.occurrence_id: item for item in result.original.occurrences}
    windows = {}
    for family_occurrence in result.family_occurrences:
        for exact_id in family_occurrence.exact_occurrence_ids:
            for support in exact[exact_id].supports:
                windows[support.window_index] = (family_occurrence.family_id, family_occurrence.family_occurrence_id)
    return windows


def _persistent_series(items: list[SecondaryEvidence], profile: AggregationProfile) -> list[list[SecondaryEvidence]]:
    series, current = [], []
    for item in sorted(items, key=semantic_evidence_order_key):
        if current and item.window_index - current[-1].window_index > profile.max_competitor_window_distance:
            series.append(current)
            current = []
        current.append(item)
    if current:
        series.append(current)
    return series


def _conflict_kind(result: FamilyAggregationResult, dominant_family_id: str, competing_family_id: str) -> tuple[ConflictKind, tuple[str, ...]]:
    family_by_id = {item.family_id: item for item in result.families}
    dominant, competitor = family_by_id[dominant_family_id], family_by_id[competing_family_id]
    dominant_members = set(dominant.member_recording_identity_ids)
    competitor_members = set(competitor.member_recording_identity_ids)
    relations = [item for item in result.relations if {item.left_recording_identity_id, item.right_recording_identity_id} & dominant_members and {item.left_recording_identity_id, item.right_recording_identity_id} & competitor_members]
    if any("artists_incompatible" in item.evidence_fields for item in relations):
        return ConflictKind.SAME_TITLE_DIFFERENT_ARTIST, ("same_title_different_artist",)
    return ConflictKind.RANK_COMPETITION, ("repeated_incompatible_secondary",)


def aggregate_secondary_evidence(result: FamilyAggregationResult, profile: AggregationProfile = OFFLINE_AGGREGATION_EXPERIMENTAL_V1) -> SecondaryEvidenceAggregationResult:
    """Account for every candidate without converting lower-rank evidence into occurrences."""
    family_by_identity = {identity: family.family_id for family in result.families for identity in family.member_recording_identity_ids}
    dominant_windows = _dominant_windows(result)
    evidence: list[SecondaryEvidence] = []
    for window in result.original.original.windows:
        dominant = dominant_windows.get(window.original.index)
        for candidate in window.candidates:
            family_id = family_by_identity.get(candidate.recording_identity_id)
            if dominant and candidate.original.rank == 1 and family_id == dominant[0]:
                kind, reasons, occurrence_id = SecondaryEvidenceKind.DOMINANT_SUPPORT, ("rank_1_family_support",), dominant[1]
            elif dominant and family_id == dominant[0]:
                kind, reasons, occurrence_id = SecondaryEvidenceKind.SAME_FAMILY_SECONDARY, ("same_family_secondary",), dominant[1]
            elif dominant and family_id:
                kind, reasons, occurrence_id = SecondaryEvidenceKind.WEAK_ALTERNATIVE, ("incompatible_secondary",), dominant[1]
            else:
                kind, reasons, occurrence_id = SecondaryEvidenceKind.UNASSIGNED, ("no_dominant_family_occurrence",), None
            evidence.append(SecondaryEvidence(
                observation_id=candidate.observation_id, window_id=candidate.window_id, window_index=candidate.window_index,
                source_start=window.original.source_start, source_end=window.original.source_end,
                family_id=family_id, family_occurrence_id=occurrence_id, rank=candidate.original.rank,
                score=candidate.original.score, kind=kind, reason_codes=reasons,
            ))

    conflicts: list[FamilyConflict] = []
    updated: dict[str, SecondaryEvidenceKind] = {}
    weak_groups: dict[tuple[str, str], list[SecondaryEvidence]] = {}
    for item in evidence:
        if item.kind is SecondaryEvidenceKind.WEAK_ALTERNATIVE and item.family_id and item.family_occurrence_id:
            dominant = next(value.family_id for value in result.family_occurrences if value.family_occurrence_id == item.family_occurrence_id)
            weak_groups.setdefault((dominant, item.family_id), []).append(item)
    for (dominant, competitor), items in sorted(weak_groups.items()):
        for series in _persistent_series(items, profile):
            if len(series) < profile.minimum_persistent_competitor_support:
                continue
            for item in series:
                updated[item.observation_id] = SecondaryEvidenceKind.PERSISTENT_COMPETITOR
            kind, reasons = _conflict_kind(result, dominant, competitor)
            windows = tuple(item.window_index for item in series)
            occurrence_ids = {item.family_occurrence_id for item in series}
            occurrence_id = next(iter(occurrence_ids)) if len(occurrence_ids) == 1 else None
            conflicts.append(FamilyConflict(
                conflict_id=_id("family_conflict", {"occurrence": occurrence_id, "dominant": dominant, "competitor": competitor, "observations": [item.observation_id for item in series], "kind": kind.value}),
                kind=kind, resolution_state=ResolutionState.COMPETING,
                dominant_family_id=dominant, competing_family_id=competitor, family_occurrence_id=occurrence_id,
                observation_ids=tuple(item.observation_id for item in series), window_indices=windows,
                source_start=series[0].source_start, source_end=series[-1].source_end,
                persistence_support_count=len(series), reason_codes=reasons,
            ))
    evidence = [SecondaryEvidence(item.observation_id, item.window_id, item.window_index, item.source_start, item.source_end, item.family_id, item.family_occurrence_id, item.rank, item.score, updated.get(item.observation_id, item.kind), item.reason_codes) for item in evidence]
    evidence_by_observation = {item.observation_id: item for item in evidence}
    conflicts = sorted(
        conflicts,
        key=lambda item: semantic_conflict_order_key(item, evidence_by_observation),
    )
    occurrence_records = []
    for occurrence in result.family_occurrences:
        related = [item for item in evidence if item.family_occurrence_id == occurrence.family_occurrence_id and item.kind is not SecondaryEvidenceKind.DOMINANT_SUPPORT]
        occurrence_conflicts = [item for item in conflicts if item.family_occurrence_id == occurrence.family_occurrence_id]
        competitor_ids = tuple(sorted({item.competing_family_id for item in occurrence_conflicts if item.competing_family_id}))
        if occurrence_conflicts:
            state = ResolutionState.COMPETING
        elif any(item.kind is SecondaryEvidenceKind.SAME_FAMILY_SECONDARY for item in related):
            state = ResolutionState.FAMILY_RESOLVED_VERSION_AMBIGUOUS
        else:
            state = ResolutionState.RESOLVED
        occurrence_records.append(FamilyOccurrenceSecondaryEvidence(
            family_occurrence_id=occurrence.family_occurrence_id, dominant_family_id=occurrence.family_id,
            secondary_observation_ids=tuple(item.observation_id for item in related), competitor_family_ids=competitor_ids,
            conflict_ids=tuple(item.conflict_id for item in occurrence_conflicts), resolution_state=state,
        ))
    unassigned = tuple(item.observation_id for item in evidence if item.kind is SecondaryEvidenceKind.UNASSIGNED)
    return SecondaryEvidenceAggregationResult(result, tuple(evidence), tuple(occurrence_records), tuple(conflicts), unassigned, profile)
