"""Internal structural validation for :mod:`aggregation.output` (S8)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .output import AggregatedResult, ConfidenceStatus
from .models import RawProvenancePointer


class ValidationIssueCode(StrEnum):
    DUPLICATE_ID = "duplicate_id"
    DANGLING_REFERENCE = "dangling_reference"
    ACCOUNTING_MISMATCH = "accounting_mismatch"
    CHRONOLOGY_VIOLATION = "chronology_violation"
    INVALID_PROVENANCE = "invalid_provenance"
    LOCAL_EVIDENCE_VIOLATION = "local_evidence_violation"
    REFERENCE_DATA_LEAK = "reference_data_leak"
    PREMATURE_CONFIDENCE = "premature_confidence"


@dataclass(frozen=True)
class StructuralValidationIssue:
    code: ValidationIssueCode
    object_type: str
    object_id: str | None
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value, "object_type": self.object_type,
            "object_id": self.object_id, "details": dict(self.details),
        }


@dataclass(frozen=True)
class StructuralValidationReport:
    valid: bool
    issues: tuple[StructuralValidationIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "issues": [item.to_dict() for item in self.issues]}


def _duplicates(items: Iterable[Any], attribute: str) -> set[str]:
    seen, duplicate = set(), set()
    for item in items:
        value = getattr(item, attribute)
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    return duplicate


def _has_reference_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            "reference" in str(key).casefold() or _has_reference_key(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_reference_key(item) for item in value)
    return False


def _valid_provenance(value: RawProvenancePointer | None) -> bool:
    return value is None or (
        isinstance(value, RawProvenancePointer) and bool(value.adapter) and bool(value.record_id)
    )


def validate_aggregated_result(result: AggregatedResult) -> StructuralValidationReport:
    """Check references, conservation, chronology, and S8 non-decision boundaries.

    It intentionally knows nothing about reference data or calibrated confidence.
    """
    issues: list[StructuralValidationIssue] = []

    def issue(code: ValidationIssueCode, object_type: str, object_id: str | None, **details: Any) -> None:
        issues.append(StructuralValidationIssue(code, object_type, object_id, details))

    collections = (
        ("window", result.windows, "window_id"),
        ("observation", result.observations, "observation_id"),
        ("recording_identity", result.recording_identities, "identity_id"),
        ("recording_relation", result.recording_relations, "relation_id"),
        ("track_family", result.track_families, "family_id"),
        ("exact_occurrence", result.exact_occurrences, "occurrence_id"),
        ("family_occurrence", result.family_occurrences, "family_occurrence_id"),
        ("conflict", result.conflicts, "conflict_id"),
        ("transition", result.transitions, "transition_id"),
        ("appearance_split", result.appearance_splits, "split_id"),
        ("appearance", result.appearances, "appearance_id"),
        ("diagnostic", result.diagnostics, "diagnostic_id"),
    )
    for object_type, items, attribute in collections:
        for value in sorted(_duplicates(items, attribute)):
            issue(ValidationIssueCode.DUPLICATE_ID, object_type, value)

    windows = {item.window_id: item for item in result.windows}
    observations = {item.observation_id: item for item in result.observations}
    identities = {item.identity_id: item for item in result.recording_identities}
    relations = {item.relation_id: item for item in result.recording_relations}
    families = {item.family_id: item for item in result.track_families}
    exact = {item.occurrence_id: item for item in result.exact_occurrences}
    family_occurrences = {item.family_occurrence_id: item for item in result.family_occurrences}
    conflicts = {item.conflict_id: item for item in result.conflicts}
    transitions = {item.transition_id: item for item in result.transitions}
    splits = {item.split_id: item for item in result.appearance_splits}
    appearances = {item.appearance_id: item for item in result.appearances}
    object_ids = set().union(windows, observations, identities, relations, families, exact, family_occurrences, conflicts, transitions, splits, appearances)

    if [item.index for item in result.windows] != list(range(len(result.windows))):
        issue(ValidationIssueCode.CHRONOLOGY_VIOLATION, "windows", None, reason="indexes_not_consecutive")
    if any(right.source_start < left.source_start for left, right in zip(result.windows, result.windows[1:])):
        issue(ValidationIssueCode.CHRONOLOGY_VIOLATION, "windows", None, reason="source_order")
    if list(result.appearances) != sorted(result.appearances, key=lambda item: (item.observed_start, item.appearance_id)):
        issue(ValidationIssueCode.CHRONOLOGY_VIOLATION, "appearances", None, reason="source_order")

    if not _valid_provenance(result.source_provenance):
        issue(ValidationIssueCode.INVALID_PROVENANCE, "source", result.source_id)
    for item in result.windows:
        if not _valid_provenance(item.raw_provenance):
            issue(ValidationIssueCode.INVALID_PROVENANCE, "window", item.window_id)
    for item in result.observations:
        window = windows.get(item.window_id)
        if window is None or window.index != item.window_index:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "observation", item.observation_id, reference="window", target=item.window_id)
        if item.recording_identity_id and item.recording_identity_id not in identities:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "observation", item.observation_id, reference="recording_identity", target=item.recording_identity_id)
        if not _valid_provenance(item.raw_provenance):
            issue(ValidationIssueCode.INVALID_PROVENANCE, "observation", item.observation_id)
    for item in result.recording_identities:
        for observation_id in item.observation_ids:
            if observation_id not in observations:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "recording_identity", item.identity_id, reference="observation", target=observation_id)
    for item in result.recording_relations:
        for identity_id in (item.left_recording_identity_id, item.right_recording_identity_id):
            if identity_id not in identities:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "recording_relation", item.relation_id, reference="recording_identity", target=identity_id)
    for item in result.track_families:
        for identity_id in item.member_recording_identity_ids:
            if identity_id not in identities:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "track_family", item.family_id, reference="recording_identity", target=identity_id)
        for relation_id in item.relation_ids:
            if relation_id not in relations:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "track_family", item.family_id, reference="recording_relation", target=relation_id)
    for item in result.exact_occurrences:
        if item.recording_identity_id not in identities:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "exact_occurrence", item.occurrence_id, reference="recording_identity", target=item.recording_identity_id)
        for support in item.supports:
            if support.observation_id not in observations or support.window_id not in windows:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "exact_occurrence", item.occurrence_id, reference="support", target=support.observation_id)
        for gap in item.gaps:
            if gap.window_id not in windows:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "exact_occurrence", item.occurrence_id, reference="gap_window", target=gap.window_id)
    for item in result.family_occurrences:
        if item.family_id not in families:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "family_occurrence", item.family_occurrence_id, reference="track_family", target=item.family_id)
        for exact_id in item.exact_occurrence_ids:
            if exact_id not in exact:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "family_occurrence", item.family_occurrence_id, reference="exact_occurrence", target=exact_id)
    for item in result.evidence:
        observation = observations.get(item.observation_id)
        if observation is None or item.window_id not in windows:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "secondary_evidence", item.observation_id, reference="canonical_evidence", target=item.window_id)
        elif observation.window_id != item.window_id or observation.window_index != item.window_index:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "secondary_evidence", item.observation_id, reference="observation_window", target=item.window_id)
        if item.family_id and item.family_id not in families:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "secondary_evidence", item.observation_id, reference="track_family", target=item.family_id)
        if item.family_occurrence_id and item.family_occurrence_id not in family_occurrences:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "secondary_evidence", item.observation_id, reference="family_occurrence", target=item.family_occurrence_id)
    for item in result.conflicts:
        for observation_id in item.observation_ids:
            if observation_id not in observations:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "conflict", item.conflict_id, reference="observation", target=observation_id)
        for index in item.window_indices:
            if not any(window.index == index for window in result.windows):
                issue(ValidationIssueCode.DANGLING_REFERENCE, "conflict", item.conflict_id, reference="window_index", target=index)
        for family_id in (item.dominant_family_id, item.competing_family_id):
            if family_id and family_id not in families:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "conflict", item.conflict_id, reference="track_family", target=family_id)
        if item.family_occurrence_id and item.family_occurrence_id not in family_occurrences:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "conflict", item.conflict_id, reference="family_occurrence", target=item.family_occurrence_id)
    for item in result.transitions:
        for appearance_id in (item.from_appearance_id, item.to_appearance_id):
            if appearance_id not in appearances:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "transition", item.transition_id, reference="appearance", target=appearance_id)
        for observation_id in item.evidence_observation_ids:
            if observation_id not in observations:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "transition", item.transition_id, reference="observation", target=observation_id)
        for window_id in item.evidence_window_ids:
            if window_id not in windows:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "transition", item.transition_id, reference="window", target=window_id)
    for item in result.appearance_splits:
        for appearance_id in (item.previous_appearance_id, item.next_appearance_id):
            if appearance_id not in appearances:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance_split", item.split_id, reference="appearance", target=appearance_id)
        for family_id in (item.previous_family_id, item.next_family_id, *item.intervening_family_ids):
            if family_id not in families:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance_split", item.split_id, reference="track_family", target=family_id)
        for window_id in item.supporting_window_ids:
            if window_id not in windows:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance_split", item.split_id, reference="window", target=window_id)
        for gap in item.typed_gap_evidence:
            if gap.window_id not in windows or windows[gap.window_id].status is not gap.status:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance_split", item.split_id, reference="typed_gap", target=gap.window_id)
        for observation_id in item.intervening_observation_ids + item.transition_evidence_observation_ids:
            if observation_id not in observations:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance_split", item.split_id, reference="observation", target=observation_id)
        if item.transition_id and item.transition_id not in transitions:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance_split", item.split_id, reference="transition", target=item.transition_id)
    for item in result.appearances:
        if item.family_id not in families:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="track_family", target=item.family_id)
        for identity_id in item.member_recording_identity_ids:
            if identity_id not in identities:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="recording_identity", target=identity_id)
        for family_occurrence_id in item.family_occurrence_ids:
            if family_occurrence_id not in family_occurrences:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="family_occurrence", target=family_occurrence_id)
        for exact_id in item.exact_occurrence_ids:
            if exact_id not in exact:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="exact_occurrence", target=exact_id)
        for observation_id in item.supporting_observation_ids + item.same_family_secondary_observation_ids + item.weak_alternative_observation_ids + item.persistent_competitor_observation_ids:
            if observation_id not in observations:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="observation", target=observation_id)
        support_indices = tuple(observations[observation_id].window_index for observation_id in item.supporting_observation_ids if observation_id in observations)
        if support_indices:
            first_support, last_support = min(support_indices), max(support_indices)
            local_observation_ids = item.same_family_secondary_observation_ids + item.weak_alternative_observation_ids + item.persistent_competitor_observation_ids
            for observation_id in local_observation_ids:
                observation = observations.get(observation_id)
                if observation and not first_support <= observation.window_index <= last_support:
                    issue(ValidationIssueCode.LOCAL_EVIDENCE_VIOLATION, "appearance", item.appearance_id,
                          reason="observation_outside_support_span", observation_id=observation_id)
            expected_supporting = set(item.supporting_observation_ids + item.same_family_secondary_observation_ids)
            expected_windows = {observations[observation_id].window_id for observation_id in expected_supporting if observation_id in observations}
            if (item.feature_vector.top_support_windows != len(item.supporting_observation_ids)
                    or item.feature_vector.total_supporting_observations != len(expected_supporting)
                    or item.feature_vector.distinct_supporting_windows != len(expected_windows)
                    or item.feature_vector.total_internal_gaps != len(item.gaps)):
                issue(ValidationIssueCode.LOCAL_EVIDENCE_VIOLATION, "appearance", item.appearance_id,
                      reason="feature_count_reconciliation")
        for window_id in item.supporting_window_ids + item.boundary_window_ids:
            if window_id not in windows:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="window", target=window_id)
        for gap in item.gaps:
            expected_gaps = {
                (source.kind.value, source.window_id, source.window_index, source.source_start, source.source_end, source.bridged)
                for source in exact.get(gap.exact_occurrence_id, ()).gaps
            } if gap.exact_occurrence_id in exact else set()
            actual_gap = (gap.kind, gap.window_id, gap.window_index, gap.source_start, gap.source_end, gap.bridged)
            if gap.exact_occurrence_id not in exact or gap.window_id not in windows or actual_gap not in expected_gaps:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance_gap", gap.gap_id, reference="gap_source", target=gap.window_id)
            elif (not support_indices or not first_support < gap.window_index < last_support
                  or gap.source_start < item.observed_start or gap.source_end > item.observed_end):
                issue(ValidationIssueCode.LOCAL_EVIDENCE_VIOLATION, "appearance", item.appearance_id,
                      reason="gap_not_internal", gap_id=gap.gap_id)
        for conflict_id in item.conflict_ids:
            if conflict_id not in conflicts:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="conflict", target=conflict_id)
            elif support_indices and not any(first_support <= index <= last_support for index in conflicts[conflict_id].window_indices):
                issue(ValidationIssueCode.LOCAL_EVIDENCE_VIOLATION, "appearance", item.appearance_id,
                      reason="conflict_outside_support_span", conflict_id=conflict_id)
        for transition_id in (item.transition_in_id, item.transition_out_id):
            if transition_id and transition_id not in transitions:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="transition", target=transition_id)
        if item.transition_in_id and transitions[item.transition_in_id].to_appearance_id != item.appearance_id:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="transition_in", target=item.transition_in_id)
        if item.transition_out_id and transitions[item.transition_out_id].from_appearance_id != item.appearance_id:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="transition_out", target=item.transition_out_id)
        if item.split_in_id and item.split_in_id not in splits:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="split_in", target=item.split_in_id)
        if item.split_in_id and splits[item.split_in_id].next_appearance_id != item.appearance_id:
            issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="split_in", target=item.split_in_id)
        for window_id in item.unresolved_separation_window_ids:
            if window_id not in windows:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "appearance", item.appearance_id, reference="unresolved_separation", target=window_id)
        if item.confidence.status is not ConfidenceStatus.NOT_CALIBRATED or item.confidence.dimensions != ("presence", "family", "recording", "boundary"):
            issue(ValidationIssueCode.PREMATURE_CONFIDENCE, "appearance", item.appearance_id, reason="confidence_placeholder_changed")

    canonical_ids = set(observations)
    evidence_ids = {item.observation_id for item in result.evidence}
    accounting = result.evidence_accounting
    if (len(canonical_ids) != accounting.canonical_observation_count
            or len(result.evidence) != accounting.evidence_record_count
            or len(result.evidence) != len(evidence_ids)
            or len(evidence_ids) != accounting.accounted_observation_count
            or canonical_ids != evidence_ids
            or result.source_feature_context.candidate_observation_count != len(canonical_ids)):
        issue(ValidationIssueCode.ACCOUNTING_MISMATCH, "evidence_accounting", None,
              canonical=len(canonical_ids), evidence=len(evidence_ids), declared=accounting.to_dict())
    if _has_reference_key(result.to_dict()):
        issue(ValidationIssueCode.REFERENCE_DATA_LEAK, "aggregated_result", None)
    for item in result.diagnostics:
        for object_id in item.object_ids:
            if object_id not in object_ids:
                issue(ValidationIssueCode.DANGLING_REFERENCE, "diagnostic", item.diagnostic_id, reference="object", target=object_id)

    ordered = tuple(sorted(issues, key=lambda item: (item.code.value, item.object_type, item.object_id or "", repr(sorted(item.details.items())))))
    return StructuralValidationReport(valid=not ordered, issues=ordered)
