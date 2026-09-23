"""Deterministic, non-decisional evidence features for S6 family appearances (S7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import RecognitionWindowStatus
from .secondary import SecondaryEvidenceKind, semantic_conflict_order_key
from .temporal import GapKind
from .transitions import TransitionAggregationResult


FEATURE_EXTRACTION_CONTRACT_VERSION = "appearance-feature-extraction/v1.1-b"


@dataclass(frozen=True)
class ScoreSummary:
    count: int
    minimum: float | int | None
    first_quartile: float | None
    median: float | None
    third_quartile: float | None
    maximum: float | int | None

    def to_dict(self) -> dict[str, Any]:
        return {"count": self.count, "minimum": self.minimum, "first_quartile": self.first_quartile, "median": self.median, "third_quartile": self.third_quartile, "maximum": self.maximum}


@dataclass(frozen=True)
class RecordingAlternativeFeature:
    recording_identity_id: str
    support_count: int
    top_support_count: int
    secondary_support_count: int
    score_summary: ScoreSummary
    isrcs: tuple[str, ...]
    version_descriptors: tuple[str, ...]
    participating_window_indices: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"recording_identity_id": self.recording_identity_id, "support_count": self.support_count, "top_support_count": self.top_support_count, "secondary_support_count": self.secondary_support_count, "score_summary": self.score_summary.to_dict(), "isrcs": list(self.isrcs), "version_descriptors": list(self.version_descriptors), "participating_window_indices": list(self.participating_window_indices)}


@dataclass(frozen=True)
class SourceFeatureContext:
    total_windows: int
    submitted_windows: int
    no_result_1001_windows: int
    processing_error_windows: int
    not_submitted_windows: int
    missing_raw_windows: int
    candidate_windows: int
    candidate_observation_count: int
    processing_coverage: float | None
    candidate_window_density: float | None
    no_result_1001_ratio: float | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class AppearanceFeatureVector:
    appearance_id: str
    family_id: str
    distinct_supporting_windows: int
    top_support_windows: int
    same_family_secondary_windows: int
    total_supporting_observations: int
    support_span_seconds: float | int
    observed_envelope_seconds: float | int
    support_density: float | None
    total_internal_gaps: int
    gaps_by_type: tuple[tuple[str, int], ...]
    bridged_gap_count: int
    bounded_1001_count: int
    longest_gap_run_by_type: tuple[tuple[str, int], ...]
    total_gap_duration_seconds: float | int
    distinct_acrid_count: int
    unknown_acrid_count: int
    distinct_isrc_count: int
    unknown_isrc_count: int
    recording_alternatives_count: int
    version_descriptors: tuple[str, ...]
    acrid_switches: int
    isrc_switches: int
    rank_switches: int
    recording_ambiguity: bool
    top_score_summary: ScoreSummary
    secondary_score_summary: ScoreSummary
    weak_alternative_count: int
    persistent_competitor_count: int
    competitor_supporting_windows: int
    conflict_count: int
    conflicts_by_type: tuple[tuple[str, int], ...]
    conflicts_by_state: tuple[tuple[str, int], ...]
    contested_support_share: float | None
    transition_in_state: str | None
    transition_out_state: str | None
    reentry: bool
    repeated_family_appearance_index: int
    repeated_family_appearance_count: int
    boundary_start_uncertainty_seconds: float | int
    boundary_end_uncertainty_seconds: float | int
    boundary_evidence_count: int
    unsupported_separation_windows: int | None
    play_offset_comparable_pairs: int | None
    play_offset_coherent_pairs: int | None
    play_offset_discontinuity_pairs: int | None
    play_offset_delta_error_summary: ScoreSummary | None
    singleton: bool
    sparse_support: bool
    recording_alternatives: tuple[RecordingAlternativeFeature, ...]
    preferred_recording_identity_id: None = None

    def to_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        value["gaps_by_type"] = dict(self.gaps_by_type)
        value["longest_gap_run_by_type"] = dict(self.longest_gap_run_by_type)
        value["conflicts_by_type"] = dict(self.conflicts_by_type)
        value["conflicts_by_state"] = dict(self.conflicts_by_state)
        value["top_score_summary"] = self.top_score_summary.to_dict()
        value["secondary_score_summary"] = self.secondary_score_summary.to_dict()
        value["play_offset_delta_error_summary"] = self.play_offset_delta_error_summary.to_dict() if self.play_offset_delta_error_summary else None
        value["recording_alternatives"] = [item.to_dict() for item in self.recording_alternatives]
        return value


@dataclass(frozen=True)
class LocalGapEvidence:
    """One exact-occurrence gap proven internal to one final appearance."""

    exact_occurrence_id: str
    gap: Any

    def to_dict(self) -> dict[str, Any]:
        return {"exact_occurrence_id": self.exact_occurrence_id, "gap": self.gap.to_dict()}


@dataclass(frozen=True)
class AppearanceLocalEvidence:
    """Explicit evidence ownership after final S6 segmentation.

    Parent S4 occurrence IDs remain lineage on ``FamilyAppearance``; they are
    deliberately not an implicit source of this local set.
    """

    appearance_id: str
    top_observation_ids: tuple[str, ...]
    supporting_window_ids: tuple[str, ...]
    same_family_secondary_observation_ids: tuple[str, ...]
    weak_alternative_observation_ids: tuple[str, ...]
    persistent_competitor_observation_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    gaps: tuple[LocalGapEvidence, ...]
    recording_identity_ids: tuple[str, ...]
    boundary_window_ids: tuple[str, ...]
    transition_in_id: str | None
    transition_out_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "appearance_id": self.appearance_id,
            "top_observation_ids": list(self.top_observation_ids),
            "supporting_window_ids": list(self.supporting_window_ids),
            "same_family_secondary_observation_ids": list(self.same_family_secondary_observation_ids),
            "weak_alternative_observation_ids": list(self.weak_alternative_observation_ids),
            "persistent_competitor_observation_ids": list(self.persistent_competitor_observation_ids),
            "conflict_ids": list(self.conflict_ids),
            "gaps": [item.to_dict() for item in self.gaps],
            "recording_identity_ids": list(self.recording_identity_ids),
            "boundary_window_ids": list(self.boundary_window_ids),
            "transition_in_id": self.transition_in_id,
            "transition_out_id": self.transition_out_id,
        }


@dataclass(frozen=True)
class FeatureExtractionResult:
    original: TransitionAggregationResult
    source_context: SourceFeatureContext
    appearance_features: tuple[AppearanceFeatureVector, ...]
    local_evidence: tuple[AppearanceLocalEvidence, ...]
    contract_version: str = FEATURE_EXTRACTION_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"contract_version": self.contract_version, "original": self.original.to_dict(), "source_context": self.source_context.to_dict(), "appearance_features": [item.to_dict() for item in self.appearance_features], "local_evidence": [item.to_dict() for item in self.local_evidence]}


def _percentile(values: list[float | int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower))


def score_summary(values: Iterable[float | int | None]) -> ScoreSummary:
    usable = [value for value in values if isinstance(value, (int, float))]
    return ScoreSummary(len(usable), min(usable) if usable else None, _percentile(usable, 0.25), _percentile(usable, 0.5), _percentile(usable, 0.75), max(usable) if usable else None)


def _source_context(result: TransitionAggregationResult) -> SourceFeatureContext:
    windows = result.original.original.original.original.original.windows
    counts = {status: sum(window.status is status for window in windows) for status in RecognitionWindowStatus}
    total = len(windows)
    submitted = counts[RecognitionWindowStatus.CANDIDATES_RETURNED] + counts[RecognitionWindowStatus.NO_RESULT_1001] + counts[RecognitionWindowStatus.PROCESSING_ERROR]
    candidate_windows = counts[RecognitionWindowStatus.CANDIDATES_RETURNED]
    return SourceFeatureContext(
        total_windows=total, submitted_windows=submitted, no_result_1001_windows=counts[RecognitionWindowStatus.NO_RESULT_1001],
        processing_error_windows=counts[RecognitionWindowStatus.PROCESSING_ERROR], not_submitted_windows=counts[RecognitionWindowStatus.NOT_SUBMITTED], missing_raw_windows=counts[RecognitionWindowStatus.MISSING_RAW],
        candidate_windows=candidate_windows, candidate_observation_count=sum(len(window.candidates) for window in windows),
        processing_coverage=submitted / total if total else None, candidate_window_density=candidate_windows / total if total else None,
        no_result_1001_ratio=counts[RecognitionWindowStatus.NO_RESULT_1001] / submitted if submitted else None,
    )


def _longest_runs(gaps: list[Any]) -> tuple[tuple[str, int], ...]:
    output = {}
    for kind in GapKind:
        indexes = sorted(item.window_index for item in gaps if item.kind is kind)
        best = run = 0
        previous = None
        for index in indexes:
            run = run + 1 if previous is not None and index == previous + 1 else 1
            best, previous = max(best, run), index
        output[kind.value] = best
    return tuple(sorted(output.items()))


def _appearance_gaps(transition: TransitionAggregationResult, appearance: Any) -> tuple[LocalGapEvidence, ...]:
    """Return gaps internal to this appearance's own exact support series."""
    family = transition.original.original
    exact = {item.occurrence_id: item for item in family.original.occurrences}
    local_top = set(appearance.supporting_observation_ids)
    output = []
    for exact_id, occurrence in exact.items():
        local_indices = sorted(
            support.window_index for support in occurrence.supports
            if support.observation_id in local_top
        )
        if len(local_indices) < 2:
            continue
        for gap in occurrence.gaps:
            if (local_indices[0] < gap.window_index < local_indices[-1]
                    and appearance.observed_start <= gap.source_start
                    and gap.source_end <= appearance.observed_end):
                output.append(LocalGapEvidence(exact_id, gap))
    return tuple(sorted(output, key=lambda item: (item.gap.window_index, item.gap.window_id, item.exact_occurrence_id)))


def _alternatives(observations: list[Any]) -> tuple[RecordingAlternativeFeature, ...]:
    grouped = {}
    for item in observations:
        if item.recording_identity_id:
            grouped.setdefault(item.recording_identity_id, []).append(item)
    output = []
    for identity, items in sorted(grouped.items()):
        output.append(RecordingAlternativeFeature(
            recording_identity_id=identity, support_count=len(items), top_support_count=sum(item.original.rank == 1 for item in items),
            secondary_support_count=sum(item.original.rank > 1 for item in items), score_summary=score_summary(item.original.score for item in items),
            isrcs=tuple(sorted({item.comparison.isrc for item in items if item.comparison.isrc})),
            version_descriptors=tuple(sorted({value for item in items for value in item.comparison.version_descriptors})),
            participating_window_indices=tuple(sorted({item.window_index for item in items})),
        ))
    return tuple(output)


def _play_offset_features(top_observations: list[tuple[Any, Any]]) -> tuple[int | None, int | None, int | None, ScoreSummary | None]:
    """Describe offsets only where successive rank-1 supports are comparable.

    The expected delta comes from canonical source-window geometry.  It is not
    used to alter occurrence boundaries or infer a restart.
    """
    comparable, coherent, discontinuous, errors = 0, 0, 0, []
    for (previous, previous_window), (current, current_window) in zip(top_observations, top_observations[1:]):
        if previous.original.play_offset is None or current.original.play_offset is None:
            continue
        comparable += 1
        actual = current.original.play_offset - previous.original.play_offset
        expected = (current_window.original.source_start - previous_window.original.source_start) * 1000
        error = actual - expected
        errors.append(error)
        geometry = max(previous_window.original.actual_duration, current_window.original.actual_duration) * 1000
        if abs(error) <= geometry:
            coherent += 1
        else:
            discontinuous += 1
    return (comparable, coherent, discontinuous, score_summary(errors)) if comparable else (None, None, None, None)


def extract_features(result: TransitionAggregationResult) -> FeatureExtractionResult:
    """Extract explainable counts/ratios only; no confidence or preference decision."""
    normalized = result.original.original.original.original
    family_result = result.original.original
    family_by_identity = {identity: family.family_id for family in family_result.families for identity in family.member_recording_identity_ids}
    candidates_by_id = {item.observation_id: item for window in normalized.windows for item in window.candidates}
    evidence_by_observation = {item.observation_id: item for item in result.original.evidence}
    candidates_by_window = {window.original.index: list(window.candidates) for window in normalized.windows}
    windows_by_index = {window.original.index: window for window in normalized.windows}
    transitions_in = {item.to_appearance_id: item.state.value for item in result.transitions}
    transitions_out = {item.from_appearance_id: item.state.value for item in result.transitions}
    transition_in_ids = {item.to_appearance_id: item.transition_id for item in result.transitions}
    transition_out_ids = {item.from_appearance_id: item.transition_id for item in result.transitions}
    by_family = {}
    for appearance in result.appearances:
        by_family.setdefault(appearance.family_id, []).append(appearance)
    vectors = []
    local_sets = []
    for appearance in result.appearances:
        indices = set(appearance.supporting_window_indices)
        top = [candidates_by_id[item] for item in appearance.supporting_observation_ids]
        same_secondary = [candidate for index in indices for candidate in candidates_by_window[index] if candidate.original.rank > 1 and family_by_identity.get(candidate.recording_identity_id) == appearance.family_id]
        supporting = top + same_secondary
        local_gap_records = _appearance_gaps(result, appearance)
        gaps = [item.gap for item in local_gap_records]
        gap_counts = tuple(sorted((kind.value, sum(gap.kind is kind for gap in gaps)) for kind in GapKind))
        first_index, last_index = min(indices), max(indices)
        related_evidence = [
            item for item in result.original.evidence
            if first_index <= item.window_index <= last_index
        ]
        weak = [item for item in related_evidence if item.kind is SecondaryEvidenceKind.WEAK_ALTERNATIVE]
        persistent = [item for item in related_evidence if item.kind is SecondaryEvidenceKind.PERSISTENT_COMPETITOR]
        relevant_conflicts = [
            item for item in result.original.conflicts
            if item.window_indices and any(first_index <= index <= last_index for index in item.window_indices)
        ]
        relevant_conflicts = sorted(
            relevant_conflicts,
            key=lambda item: semantic_conflict_order_key(item, evidence_by_observation),
        )
        ranks = [min(candidate.original.rank for candidate in candidates_by_window[index] if family_by_identity.get(candidate.recording_identity_id) == appearance.family_id) for index in sorted(indices)]
        isrc_sequence = [candidate.comparison.isrc for candidate in top]
        acrid_sequence = [candidate.recording_identity_id for candidate in top]
        family_appearances = sorted(by_family[appearance.family_id], key=lambda item: item.observed_start)
        appearance_index = family_appearances.index(appearance) + 1
        prior = family_appearances[appearance_index - 2] if appearance_index > 1 else None
        play = _play_offset_features([(candidate, windows_by_index[candidate.window_index]) for candidate in top])
        vectors.append(AppearanceFeatureVector(
            appearance_id=appearance.appearance_id, family_id=appearance.family_id,
            distinct_supporting_windows=len({item.window_index for item in supporting}), top_support_windows=len(top),
            same_family_secondary_windows=len({item.window_index for item in same_secondary}), total_supporting_observations=len(supporting),
            support_span_seconds=appearance.observed_end - appearance.observed_start, observed_envelope_seconds=appearance.observed_end - appearance.observed_start,
            support_density=len({item.window_index for item in supporting}) / (appearance.observed_end - appearance.observed_start) if appearance.observed_end > appearance.observed_start else None,
            total_internal_gaps=len(gaps), gaps_by_type=gap_counts, bridged_gap_count=sum(gap.bridged for gap in gaps), bounded_1001_count=sum(gap.kind is GapKind.NO_RESULT_1001 and gap.bridged for gap in gaps),
            longest_gap_run_by_type=_longest_runs(gaps), total_gap_duration_seconds=sum(gap.source_end - gap.source_start for gap in gaps),
            distinct_acrid_count=len({item.recording_identity_id for item in supporting if item.recording_identity_id}), unknown_acrid_count=sum(item.recording_identity_id is None for item in supporting),
            distinct_isrc_count=len({item.comparison.isrc for item in supporting if item.comparison.isrc}), unknown_isrc_count=sum(item.comparison.isrc is None for item in supporting),
            recording_alternatives_count=len(_alternatives(supporting)), version_descriptors=tuple(sorted({value for item in supporting for value in item.comparison.version_descriptors})),
            acrid_switches=sum(left != right for left, right in zip(acrid_sequence, acrid_sequence[1:])), isrc_switches=sum(left is not None and right is not None and left != right for left, right in zip(isrc_sequence, isrc_sequence[1:])), rank_switches=sum(left != right for left, right in zip(ranks, ranks[1:])),
            recording_ambiguity=any(
                "ambiguous_recording" in family.ambiguity_flags
                for family in family_result.families
                if family.family_id == appearance.family_id
            ),
            top_score_summary=score_summary(item.original.score for item in top), secondary_score_summary=score_summary(item.original.score for item in same_secondary),
            weak_alternative_count=len(weak), persistent_competitor_count=len(persistent), competitor_supporting_windows=len({item.window_index for item in weak + persistent}), conflict_count=len(relevant_conflicts),
            conflicts_by_type=tuple(sorted((key, sum(item.kind.value == key for item in relevant_conflicts)) for key in {item.kind.value for item in relevant_conflicts})), conflicts_by_state=tuple(sorted((key, sum(item.resolution_state.value == key for item in relevant_conflicts)) for key in {item.resolution_state.value for item in relevant_conflicts})),
            contested_support_share=len({item.window_index for item in weak + persistent}) / len({item.window_index for item in supporting}) if supporting else None,
            transition_in_state=transitions_in.get(appearance.appearance_id), transition_out_state=transitions_out.get(appearance.appearance_id),
            reentry=appearance.reentry, repeated_family_appearance_index=appearance_index, repeated_family_appearance_count=len(family_appearances),
            boundary_start_uncertainty_seconds=appearance.latest_plausible_start - appearance.earliest_plausible_start, boundary_end_uncertainty_seconds=appearance.latest_plausible_end - appearance.earliest_plausible_end,
            boundary_evidence_count=len(appearance.boundary_window_ids), unsupported_separation_windows=appearance.supporting_window_indices[0] - prior.supporting_window_indices[-1] if prior else None,
            play_offset_comparable_pairs=play[0], play_offset_coherent_pairs=play[1], play_offset_discontinuity_pairs=play[2], play_offset_delta_error_summary=play[3],
            singleton=len(top) == 1, sparse_support=len(top) == 1, recording_alternatives=_alternatives(supporting),
        ))
        local_sets.append(AppearanceLocalEvidence(
            appearance_id=appearance.appearance_id,
            top_observation_ids=appearance.supporting_observation_ids,
            supporting_window_ids=appearance.supporting_window_ids,
            same_family_secondary_observation_ids=tuple(item.observation_id for item in same_secondary),
            weak_alternative_observation_ids=tuple(item.observation_id for item in weak),
            persistent_competitor_observation_ids=tuple(item.observation_id for item in persistent),
            conflict_ids=tuple(item.conflict_id for item in relevant_conflicts),
            gaps=local_gap_records,
            recording_identity_ids=tuple(sorted({item.recording_identity_id for item in supporting if item.recording_identity_id})),
            boundary_window_ids=appearance.boundary_window_ids,
            transition_in_id=transition_in_ids.get(appearance.appearance_id),
            transition_out_id=transition_out_ids.get(appearance.appearance_id),
        ))
    return FeatureExtractionResult(result, _source_context(result), tuple(vectors), tuple(local_sets))
