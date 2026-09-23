"""Reusable, deterministic aggregation output contract (S8)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping

from .family import FamilyOccurrence, RecordingRelation, TrackFamily
from .features import AppearanceFeatureVector, FeatureExtractionResult, SourceFeatureContext
from .identity import RecordingIdentity
from .models import RawProvenancePointer, RecognitionWindowStatus
from .secondary import FamilyConflict, SecondaryEvidence
from .temporal import ExactRecordingOccurrence, TemporalGap
from .transitions import AppearanceSplit, DirectedTransition, TransitionState


AGGREGATED_RESULT_CONTRACT_VERSION = "aggregated-result/v2"
AGGREGATION_ENGINE_VERSION = "aggregation-engine/v2"


def _id(kind: str, material: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{kind}_{digest}"


class ConfidenceStatus(StrEnum):
    NOT_CALIBRATED = "not_calibrated"


@dataclass(frozen=True)
class ConfidencePlaceholder:
    """Reserved dimensions only; S8 deliberately does not score or classify."""

    status: ConfidenceStatus = ConfidenceStatus.NOT_CALIBRATED
    dimensions: tuple[str, ...] = ("presence", "family", "recording", "boundary")

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "dimensions": list(self.dimensions)}


@dataclass(frozen=True)
class WindowEvidenceReference:
    window_id: str
    index: int
    source_start: float | int
    source_end: float | int
    actual_duration: float | int
    status: RecognitionWindowStatus
    raw_provenance: RawProvenancePointer | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id, "index": self.index,
            "source_start": self.source_start, "source_end": self.source_end,
            "actual_duration": self.actual_duration, "status": self.status.value,
            "raw_provenance": self.raw_provenance.to_dict() if self.raw_provenance else None,
        }


@dataclass(frozen=True)
class ObservationEvidenceReference:
    observation_id: str
    window_id: str
    window_index: int
    rank: int
    recording_identity_id: str | None
    raw_provenance: RawProvenancePointer | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "window_id": self.window_id,
            "window_index": self.window_index, "rank": self.rank,
            "recording_identity_id": self.recording_identity_id,
            "raw_provenance": self.raw_provenance.to_dict() if self.raw_provenance else None,
        }


@dataclass(frozen=True)
class AppearanceGapReference:
    gap_id: str
    exact_occurrence_id: str
    kind: str
    window_id: str
    window_index: int
    source_start: float | int
    source_end: float | int
    bridged: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id, "exact_occurrence_id": self.exact_occurrence_id,
            "kind": self.kind, "window_id": self.window_id, "window_index": self.window_index,
            "source_start": self.source_start, "source_end": self.source_end, "bridged": self.bridged,
        }


@dataclass(frozen=True)
class OutputDiagnostic:
    diagnostic_id: str
    kind: str
    object_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"diagnostic_id": self.diagnostic_id, "kind": self.kind, "object_ids": list(self.object_ids)}


@dataclass(frozen=True)
class EvidenceAccountingSummary:
    canonical_observation_count: int
    evidence_record_count: int
    accounted_observation_count: int
    unassigned_observation_count: int
    appearance_support_observation_count: int

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class FinalAppearance:
    """One S6 appearance plus stable S0-S7 references, not a confidence decision."""

    appearance_id: str
    family_id: str
    member_recording_identity_ids: tuple[str, ...]
    family_occurrence_ids: tuple[str, ...]
    exact_occurrence_ids: tuple[str, ...]
    observed_start: float | int
    observed_end: float | int
    earliest_plausible_start: float | int
    latest_plausible_start: float | int
    earliest_plausible_end: float | int
    latest_plausible_end: float | int
    boundary_window_ids: tuple[str, ...]
    reentry: bool
    reentry_reason: str | None
    reentry_candidate: bool
    unresolved_separation_window_ids: tuple[str, ...]
    split_in_id: str | None
    repeated_family_appearance_index: int
    repeated_family_appearance_count: int
    family_ambiguity_flags: tuple[str, ...]
    recording_alternatives: tuple[Any, ...]
    preferred_recording_identity_id: str | None
    supporting_observation_ids: tuple[str, ...]
    supporting_window_ids: tuple[str, ...]
    same_family_secondary_observation_ids: tuple[str, ...]
    weak_alternative_observation_ids: tuple[str, ...]
    persistent_competitor_observation_ids: tuple[str, ...]
    gaps: tuple[AppearanceGapReference, ...]
    conflict_ids: tuple[str, ...]
    transition_in_id: str | None
    transition_out_id: str | None
    feature_vector: AppearanceFeatureVector
    confidence: ConfidencePlaceholder
    uncertainty_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "appearance_id": self.appearance_id, "family_id": self.family_id,
            "member_recording_identity_ids": list(self.member_recording_identity_ids),
            "family_occurrence_ids": list(self.family_occurrence_ids),
            "exact_occurrence_ids": list(self.exact_occurrence_ids),
            "observed_range": {"start": self.observed_start, "end": self.observed_end},
            "plausible_start": {"earliest": self.earliest_plausible_start, "latest": self.latest_plausible_start},
            "plausible_end": {"earliest": self.earliest_plausible_end, "latest": self.latest_plausible_end},
            "boundary_window_ids": list(self.boundary_window_ids),
            "reentry": self.reentry, "reentry_reason": self.reentry_reason,
            "reentry_candidate": self.reentry_candidate,
            "unresolved_separation_window_ids": list(self.unresolved_separation_window_ids),
            "split_in_id": self.split_in_id,
            "repeated_family_appearance_index": self.repeated_family_appearance_index,
            "repeated_family_appearance_count": self.repeated_family_appearance_count,
            "family_ambiguity_flags": list(self.family_ambiguity_flags),
            "recording_alternatives": [item.to_dict() for item in self.recording_alternatives],
            "preferred_recording_identity_id": self.preferred_recording_identity_id,
            "supporting_observation_ids": list(self.supporting_observation_ids),
            "supporting_window_ids": list(self.supporting_window_ids),
            "same_family_secondary_observation_ids": list(self.same_family_secondary_observation_ids),
            "weak_alternative_observation_ids": list(self.weak_alternative_observation_ids),
            "persistent_competitor_observation_ids": list(self.persistent_competitor_observation_ids),
            "gaps": [item.to_dict() for item in self.gaps], "conflict_ids": list(self.conflict_ids),
            "transition_in_id": self.transition_in_id, "transition_out_id": self.transition_out_id,
            "features": self.feature_vector.to_dict(), "confidence": self.confidence.to_dict(),
            "uncertainty_flags": list(self.uncertainty_flags),
        }


@dataclass(frozen=True)
class AggregatedResult:
    """Production-reusable deterministic S8 output; it has no reference input."""

    run_id: str
    source_id: str
    source_duration: float | int
    window_size: float | int
    step: float | int
    source_provenance: RawProvenancePointer | None
    source_mapping: Mapping[str, Any]
    profile_versions: tuple[tuple[str, str], ...]
    source_feature_context: SourceFeatureContext
    windows: tuple[WindowEvidenceReference, ...]
    observations: tuple[ObservationEvidenceReference, ...]
    recording_identities: tuple[RecordingIdentity, ...]
    recording_relations: tuple[RecordingRelation, ...]
    track_families: tuple[TrackFamily, ...]
    exact_occurrences: tuple[ExactRecordingOccurrence, ...]
    family_occurrences: tuple[FamilyOccurrence, ...]
    evidence: tuple[SecondaryEvidence, ...]
    conflicts: tuple[FamilyConflict, ...]
    transitions: tuple[DirectedTransition, ...]
    appearance_splits: tuple[AppearanceSplit, ...]
    appearances: tuple[FinalAppearance, ...]
    diagnostics: tuple[OutputDiagnostic, ...]
    evidence_accounting: EvidenceAccountingSummary
    contract_version: str = AGGREGATED_RESULT_CONTRACT_VERSION
    engine_version: str = AGGREGATION_ENGINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "engine_version": self.engine_version,
            "profile_versions": dict(self.profile_versions),
            "run": {"run_id": self.run_id, "source_id": self.source_id,
                    "source_duration": self.source_duration, "window_size": self.window_size, "step": self.step,
                    "source_provenance": self.source_provenance.to_dict() if self.source_provenance else None,
                    "source_mapping": dict(self.source_mapping)},
            "source_feature_context": self.source_feature_context.to_dict(),
            "evidence_catalog": {"windows": [item.to_dict() for item in self.windows],
                                 "observations": [item.to_dict() for item in self.observations]},
            "recording_identities": [item.to_dict() for item in self.recording_identities],
            "recording_relations": [item.to_dict() for item in self.recording_relations],
            "track_families": [item.to_dict() for item in self.track_families],
            "exact_occurrences": [item.to_dict() for item in self.exact_occurrences],
            "family_occurrences": [item.to_dict() for item in self.family_occurrences],
            "evidence": [item.to_dict() for item in self.evidence],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "transitions": [item.to_dict() for item in self.transitions],
            "appearance_splits": [item.to_dict() for item in self.appearance_splits],
            "appearances": [item.to_dict() for item in self.appearances],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "evidence_accounting": self.evidence_accounting.to_dict(),
        }


def _gap_reference(exact_occurrence_id: str, gap: TemporalGap) -> AppearanceGapReference:
    material = {"exact_occurrence_id": exact_occurrence_id, **gap.to_dict()}
    return AppearanceGapReference(
        gap_id=_id("appearance_gap", material), exact_occurrence_id=exact_occurrence_id,
        kind=gap.kind.value, window_id=gap.window_id, window_index=gap.window_index,
        source_start=gap.source_start, source_end=gap.source_end, bridged=gap.bridged,
    )


def _uncertainties(feature: AppearanceFeatureVector, transition_in: DirectedTransition | None, transition_out: DirectedTransition | None, reentry_candidate: bool) -> tuple[str, ...]:
    gap_types = dict(feature.gaps_by_type)
    flags = []
    if feature.singleton:
        flags.append("singleton")
    if feature.sparse_support:
        flags.append("sparse_support")
    if feature.recording_ambiguity:
        flags.append("recording_ambiguous")
    if feature.conflict_count:
        flags.append("family_conflict")
    if feature.persistent_competitor_count:
        flags.append("persistent_competitor")
    if feature.weak_alternative_count:
        flags.append("weak_alternative")
    if feature.boundary_start_uncertainty_seconds or feature.boundary_end_uncertainty_seconds:
        flags.append("boundary_uncertain")
    if feature.reentry:
        flags.append("reentry")
    if reentry_candidate:
        flags.append("unresolved_separation")
    if any(gap_types.get(name, 0) for name in ("processing_error", "not_submitted", "missing_raw")):
        flags.append("processing_uncertainty")
    if any(item and item.state is TransitionState.UNRESOLVED for item in (transition_in, transition_out)):
        flags.append("unresolved_transition")
    return tuple(flags)


def build_aggregated_result(features: FeatureExtractionResult) -> AggregatedResult:
    """Assemble S1-S7 evidence into a stable output without a confidence decision."""
    transition = features.original
    secondary = transition.original
    family = secondary.original
    temporal = family.original
    normalized = temporal.original
    canonical = normalized.original
    family_by_id = {item.family_id: item for item in family.families}
    family_occurrence_by_id = {item.family_occurrence_id: item for item in family.family_occurrences}
    exact_by_id = {item.occurrence_id: item for item in temporal.occurrences}
    feature_by_id = {item.appearance_id: item for item in features.appearance_features}
    local_evidence_by_id = {item.appearance_id: item for item in features.local_evidence}
    transitions_in = {item.to_appearance_id: item for item in transition.transitions}
    transitions_out = {item.from_appearance_id: item for item in transition.transitions}
    splits_in = {item.next_appearance_id: item for item in transition.splits}
    appearances = []
    for appearance in sorted(transition.appearances, key=lambda item: (item.observed_start, item.appearance_id)):
        feature = feature_by_id[appearance.appearance_id]
        local_evidence = local_evidence_by_id[appearance.appearance_id]
        track_family = family_by_id[appearance.family_id]
        exact_ids = tuple(dict.fromkeys(
            exact_id for family_occurrence_id in appearance.s4_family_occurrence_ids
            for exact_id in family_occurrence_by_id[family_occurrence_id].exact_occurrence_ids
        ))
        gaps = tuple(sorted(
            (_gap_reference(item.exact_occurrence_id, item.gap) for item in local_evidence.gaps),
            key=lambda item: (item.source_start, item.window_index, item.gap_id),
        ))
        transition_in, transition_out = transitions_in.get(appearance.appearance_id), transitions_out.get(appearance.appearance_id)
        appearances.append(FinalAppearance(
            appearance_id=appearance.appearance_id, family_id=appearance.family_id,
            member_recording_identity_ids=track_family.member_recording_identity_ids,
            family_occurrence_ids=appearance.s4_family_occurrence_ids, exact_occurrence_ids=exact_ids,
            observed_start=appearance.observed_start, observed_end=appearance.observed_end,
            earliest_plausible_start=appearance.earliest_plausible_start, latest_plausible_start=appearance.latest_plausible_start,
            earliest_plausible_end=appearance.earliest_plausible_end, latest_plausible_end=appearance.latest_plausible_end,
            boundary_window_ids=appearance.boundary_window_ids, reentry=appearance.reentry,
            reentry_reason=appearance.reentry_reason.value if appearance.reentry_reason else None,
            reentry_candidate=appearance.reentry_candidate,
            unresolved_separation_window_ids=appearance.unresolved_separation_window_ids,
            split_in_id=splits_in[appearance.appearance_id].split_id if appearance.appearance_id in splits_in else None,
            repeated_family_appearance_index=feature.repeated_family_appearance_index,
            repeated_family_appearance_count=feature.repeated_family_appearance_count,
            family_ambiguity_flags=track_family.ambiguity_flags,
            recording_alternatives=feature.recording_alternatives,
            preferred_recording_identity_id=feature.preferred_recording_identity_id,
            supporting_observation_ids=appearance.supporting_observation_ids,
            supporting_window_ids=appearance.supporting_window_ids,
            same_family_secondary_observation_ids=local_evidence.same_family_secondary_observation_ids,
            weak_alternative_observation_ids=local_evidence.weak_alternative_observation_ids,
            persistent_competitor_observation_ids=local_evidence.persistent_competitor_observation_ids,
            gaps=gaps, conflict_ids=local_evidence.conflict_ids,
            transition_in_id=transition_in.transition_id if transition_in else None,
            transition_out_id=transition_out.transition_id if transition_out else None,
            feature_vector=feature, confidence=ConfidencePlaceholder(),
            uncertainty_flags=_uncertainties(feature, transition_in, transition_out, appearance.reentry_candidate),
        ))

    windows = tuple(WindowEvidenceReference(
        window_id=item.window_id, index=item.index, source_start=item.source_start, source_end=item.source_end,
        actual_duration=item.actual_duration, status=item.status, raw_provenance=item.raw_provenance,
    ) for item in canonical.windows)
    observations = tuple(ObservationEvidenceReference(
        observation_id=item.observation_id, window_id=item.window_id, window_index=item.window_index,
        rank=item.original.rank, recording_identity_id=item.recording_identity_id,
        raw_provenance=item.original.raw_provenance,
    ) for window in normalized.windows for item in window.candidates)
    diagnostics = tuple(sorted(
        [OutputDiagnostic(item.diagnostic_id, "metadata_inconsistency", (item.identity_id, *item.evidence_observation_ids)) for item in normalized.diagnostics]
        + [OutputDiagnostic(item.diagnostic_id, f"family_relation_{item.kind}", (item.relation_id,)) for item in family.diagnostics],
        key=lambda item: item.diagnostic_id,
    ))
    profiles = tuple(sorted((
        ("temporal", temporal.profile.name), ("family", family.profile.name),
        ("secondary", secondary.profile.name), ("transitions", transition.profile.name),
    )))
    accounting = EvidenceAccountingSummary(
        canonical_observation_count=len(observations), evidence_record_count=len(secondary.evidence),
        accounted_observation_count=len({item.observation_id for item in secondary.evidence}),
        unassigned_observation_count=len(secondary.unassigned_observation_ids),
        appearance_support_observation_count=sum(len(item.supporting_observation_ids) for item in appearances),
    )
    return AggregatedResult(
        run_id=canonical.run_id, source_id=canonical.source_id, source_duration=canonical.source_duration,
        window_size=canonical.window_size, step=canonical.step, source_provenance=canonical.source_provenance,
        source_mapping=canonical.source_raw_values, profile_versions=profiles,
        source_feature_context=features.source_context, windows=windows, observations=observations,
        recording_identities=normalized.recording_identities, recording_relations=family.relations,
        track_families=family.families, exact_occurrences=temporal.occurrences,
        family_occurrences=family.family_occurrences, evidence=secondary.evidence,
        conflicts=secondary.conflicts, transitions=transition.transitions, appearance_splits=transition.splits,
        appearances=tuple(appearances),
        diagnostics=diagnostics, evidence_accounting=accounting,
    )


def serialize_aggregated_result(result: AggregatedResult) -> str:
    """Return deterministic, human-inspectable JSON without a runtime timestamp."""
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
