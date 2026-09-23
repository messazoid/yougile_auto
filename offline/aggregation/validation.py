"""Offline-only S9 comparison of S8 output with manual reference data.

Reference data is consumed here, after aggregation.  It never affects S0-S8.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import re
from typing import Any, Iterable, Mapping

from src.aggregation import AggregatedResult, ConfidenceStatus, version_signatures
from src.aggregation.normalization import normalize_text, version_descriptors
from src.aggregation.validation import validate_aggregated_result

from .reference_loader import OfflineReferenceDocument, ReferenceEntry, ReferenceTest


OFFLINE_REFERENCE_VALIDATION_CONTRACT_VERSION = "offline-reference-validation/v1.1-c"
_VERSION_WORDS = re.compile(r"\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|remix|live|acoustic|demo|mixed|edit)\b")
_BRACKETED_VERSION = re.compile(r"\([^)]*\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|remix|live|acoustic|demo|mixed|edit)\b[^)]*\)|\[[^]]*\b(?:radio\s+edit|extended(?:\s+(?:mix|version))?|remaster(?:ed)?|remix|live|acoustic|demo|mixed|edit)\b[^]]*\]")
_FEAT_BRACKET = re.compile(r"[\[(]\s*(?:feat\.?|featuring)\b[^\])]*[\]) ]", re.IGNORECASE)
_TITLE_SEPARATORS = re.compile(r"[\s()\[\]{}._-]+")
_ARTIST_SEPARATORS = re.compile(r"\s*(?:&|,|\band\b)\s*", re.IGNORECASE)


class MatchState(StrEnum):
    OCCURRENCE_DETECTED = "occurrence_detected"
    FAMILY_CANDIDATE_ONLY = "family_candidate_only"
    SECONDARY_ONLY = "secondary_only"
    AMBIGUOUS_MATCH = "ambiguous_match"
    MISSED_NO_USABLE_EVIDENCE = "missed_no_usable_evidence"
    MISSED_WITH_USABLE_EVIDENCE = "missed_with_usable_evidence"


class RegressionState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUABLE = "not_evaluable"


class DbReadinessState(StrEnum):
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class ReferenceAppearanceMatch:
    reference_entry_id: str
    state: MatchState
    candidate_family_ids: tuple[str, ...]
    matched_family_id: str | None
    appearance_ids: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_entry_id": self.reference_entry_id, "state": self.state.value,
            "candidate_family_ids": list(self.candidate_family_ids), "matched_family_id": self.matched_family_id,
            "appearance_ids": list(self.appearance_ids), "reason": self.reason,
        }


@dataclass(frozen=True)
class AdditionalAppearance:
    appearance_id: str
    family_id: str
    title: str | None
    artists: tuple[str, ...]
    observed_start: float | int
    observed_end: float | int
    top_support_windows: int
    total_internal_gaps: int
    distinct_acrid_count: int
    singleton: bool
    conflict_ids: tuple[str, ...]
    local_features: Mapping[str, Any]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "appearance_id": self.appearance_id, "family_id": self.family_id,
            "title": self.title, "artists": list(self.artists),
            "observed_start": self.observed_start, "observed_end": self.observed_end,
            "top_support_windows": self.top_support_windows,
            "total_internal_gaps": self.total_internal_gaps,
            "distinct_acrid_count": self.distinct_acrid_count,
            "singleton": self.singleton, "conflict_ids": list(self.conflict_ids),
            "local_features": dict(self.local_features), "reason": self.reason,
        }


@dataclass(frozen=True)
class FragmentationCandidate:
    family_id: str
    appearance_ids: tuple[str, ...]
    reference_entry_ids: tuple[str, ...]
    classification: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"family_id": self.family_id, "appearance_ids": list(self.appearance_ids),
                "reference_entry_ids": list(self.reference_entry_ids), "classification": self.classification,
                "reason": self.reason}


@dataclass(frozen=True)
class OvermergeCandidate:
    family_id: str
    reference_entry_ids: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"family_id": self.family_id, "reference_entry_ids": list(self.reference_entry_ids), "reason": self.reason}


@dataclass(frozen=True)
class DuplicateReferenceCase:
    reference_entry_ids: tuple[str, ...]
    reason: str = "duplicate_reference_entries_preserved_not_temporal_evidence"

    def to_dict(self) -> dict[str, Any]:
        return {"reference_entry_ids": list(self.reference_entry_ids), "reason": self.reason}


@dataclass(frozen=True)
class ValidationReconciliation:
    reference_total: int
    occurrence_detected: int
    family_candidate_only: int
    secondary_only: int
    ambiguous: int
    missed_no_usable_evidence: int
    missed_with_usable_evidence: int
    duplicate_reference_entry_count: int
    appearance_total: int
    reference_linked_appearance_count: int
    ambiguous_linked_appearance_count: int
    additional_appearance_count: int
    reference_reconciled: bool
    appearance_reconciled: bool

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class TestValidationResult:
    test_id: int
    run_id: str
    excluded: bool
    exclusion_reason: str | None
    reference_entries: tuple[ReferenceEntry, ...]
    reference_entry_count: int
    matches: tuple[ReferenceAppearanceMatch, ...]
    ambiguous_linked_appearance_ids: tuple[str, ...]
    additional_appearances: tuple[AdditionalAppearance, ...]
    fragmentation_candidates: tuple[FragmentationCandidate, ...]
    overmerge_candidates: tuple[OvermergeCandidate, ...]
    duplicate_reference_cases: tuple[DuplicateReferenceCase, ...]
    conflict_ids: tuple[str, ...]
    singleton_appearance_ids: tuple[str, ...]
    reentry_appearance_ids: tuple[str, ...]
    version_churn_appearance_ids: tuple[str, ...]
    bridged_gap_appearance_ids: tuple[str, ...]
    gap_fragmentation_appearance_ids: tuple[str, ...]
    transition_ids: tuple[str, ...]
    reconciliation: ValidationReconciliation

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id, "run_id": self.run_id, "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "reference_entries": [item.to_dict() for item in self.reference_entries],
            "reference_entry_count": self.reference_entry_count,
            "matches": [item.to_dict() for item in self.matches],
            "ambiguous_linked_appearance_ids": list(self.ambiguous_linked_appearance_ids),
            "additional_appearances": [item.to_dict() for item in self.additional_appearances],
            "fragmentation_candidates": [item.to_dict() for item in self.fragmentation_candidates],
            "overmerge_candidates": [item.to_dict() for item in self.overmerge_candidates],
            "duplicate_reference_cases": [item.to_dict() for item in self.duplicate_reference_cases],
            "conflict_ids": list(self.conflict_ids), "singleton_appearance_ids": list(self.singleton_appearance_ids),
            "reentry_appearance_ids": list(self.reentry_appearance_ids),
            "version_churn_appearance_ids": list(self.version_churn_appearance_ids),
            "bridged_gap_appearance_ids": list(self.bridged_gap_appearance_ids),
            "gap_fragmentation_appearance_ids": list(self.gap_fragmentation_appearance_ids),
            "transition_ids": list(self.transition_ids), "reconciliation": self.reconciliation.to_dict(),
        }


@dataclass(frozen=True)
class RegressionCheck:
    regression_id: str
    test_id: int
    state: RegressionState
    reason: str
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"regression_id": self.regression_id, "test_id": self.test_id, "state": self.state.value,
                "reason": self.reason, "evidence_ids": list(self.evidence_ids)}


@dataclass(frozen=True)
class DbReadinessGate:
    state: DbReadinessState
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "reason_codes": list(self.reason_codes)}


@dataclass(frozen=True)
class OfflineValidationSummary:
    comparable_test_count: int
    excluded_test_ids: tuple[int, ...]
    reference_entry_count: int
    occurrence_detected_count: int
    family_candidate_only_count: int
    secondary_only_count: int
    ambiguous_match_count: int
    missed_no_usable_evidence_count: int
    missed_with_usable_evidence_count: int
    additional_appearance_count: int
    ambiguous_linked_appearance_count: int
    fragmentation_candidate_count: int
    repeat_supported_count: int
    overmerge_candidate_count: int
    duplicate_reference_case_count: int
    conflict_count: int
    singleton_count: int
    reentry_count: int
    version_churn_appearance_count: int
    bridged_gap_appearance_count: int
    reconciliation_valid: bool

    @property
    def detected_count(self) -> int:  # compatibility for callers, not serialized ambiguity.
        return self.occurrence_detected_count

    @property
    def missed_count(self) -> int:
        return self.missed_no_usable_evidence_count + self.missed_with_usable_evidence_count

    def to_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        value["excluded_test_ids"] = list(self.excluded_test_ids)
        value["detected_count"] = self.detected_count
        value["missed_count"] = self.missed_count
        return value


@dataclass(frozen=True)
class ValidationResult:
    reference_document_id: str
    tests: tuple[TestValidationResult, ...]
    summary: OfflineValidationSummary
    regression_matrix: tuple[RegressionCheck, ...]
    db_readiness_gate: DbReadinessGate
    contract_version: str = OFFLINE_REFERENCE_VALIDATION_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"contract_version": self.contract_version, "reference_document_id": self.reference_document_id,
                "tests": [item.to_dict() for item in self.tests], "summary": self.summary.to_dict(),
                "regression_matrix": [item.to_dict() for item in self.regression_matrix],
                "db_readiness_gate": self.db_readiness_gate.to_dict()}


class ReferenceValidationError(ValueError):
    pass


def _core_title(value: str | None) -> str | None:
    if not value:
        return None
    core = _VERSION_WORDS.sub(" ", _BRACKETED_VERSION.sub(" ", _FEAT_BRACKET.sub(" ", value)))
    return _TITLE_SEPARATORS.sub(" ", core).strip() or None


def _artist_tokens(value: str | tuple[str, ...]) -> frozenset[str]:
    values = (value,) if isinstance(value, str) else value
    tokens = []
    for item in values:
        tokens.extend(_ARTIST_SEPARATORS.split(item))
    return frozenset(value for item in tokens if (value := normalize_text(item)))


def _compatible_artists(reference: ReferenceEntry, family: Any) -> bool:
    return bool(_artist_tokens(reference.artists) and _artist_tokens(family.artists)
                and _artist_tokens(reference.artists) & _artist_tokens(family.artists))


def _family_candidates(entry: ReferenceEntry, result: AggregatedResult) -> tuple[tuple[str, str, bool], ...]:
    core = _core_title(normalize_text(entry.title))
    reference_versions = set(version_descriptors(entry.title))
    reference_signatures = version_signatures(normalize_text(entry.title), normalize_text(entry.version_text))
    profile_versions = dict(getattr(result, "profile_versions", ()))
    use_signatures = profile_versions.get("family") == "calibrated-v2"
    candidates = []
    for family in result.track_families:
        if not (family_core := normalize_text(family.preferred_core_title)) or core != family_core:
            continue
        family_versions = set(family.version_alternatives)
        if reference_versions and not (reference_versions & family_versions):
            continue
        family_signatures = set(getattr(family, "version_signatures", ()))
        if use_signatures and reference_signatures and family_signatures and not (reference_signatures & family_signatures):
            continue
        if _compatible_artists(entry, family):
            candidates.append((family.family_id, "core_title_compatible_artist_and_version_alias" if reference_versions else "core_title_and_compatible_artist", True))
        elif not family.artists:
            candidates.append((family.family_id, "core_title_version_alias_with_unknown_family_artist", False))
    return tuple(sorted(candidates))


def _source_job_id(result: AggregatedResult) -> int:
    logical_sources = result.source_mapping.get("logical_sources") if isinstance(result.source_mapping, Mapping) else None
    source_ids = {item.get("source_job_id") for item in logical_sources if isinstance(item, Mapping) and isinstance(item.get("source_job_id"), int)} if isinstance(logical_sources, list) else set()
    if len(source_ids) != 1:
        raise ReferenceValidationError(f"run {result.run_id} has no unambiguous S1 source_job_id mapping")
    return next(iter(source_ids))


def _duplicate_cases(entries: tuple[ReferenceEntry, ...]) -> tuple[DuplicateReferenceCase, ...]:
    grouped: dict[tuple[str | None, frozenset[str]], list[str]] = {}
    for entry in entries:
        grouped.setdefault((_core_title(normalize_text(entry.title)), _artist_tokens(entry.artists)), []).append(entry.entry_id)
    return tuple(DuplicateReferenceCase(tuple(ids)) for _, ids in sorted(grouped.items(), key=lambda item: item[1]) if len(ids) > 1)


def _incompatible_entries(left: ReferenceEntry, right: ReferenceEntry) -> bool:
    return _core_title(normalize_text(left.title)) != _core_title(normalize_text(right.title)) or not (_artist_tokens(left.artists) & _artist_tokens(right.artists))


def _overmerge_candidates(detected_by_family: Mapping[str, list[ReferenceEntry]]) -> tuple[OvermergeCandidate, ...]:
    output = []
    for family_id, entries in sorted(detected_by_family.items()):
        if any(_incompatible_entries(left, right) for index, left in enumerate(entries) for right in entries[index + 1:]):
            output.append(OvermergeCandidate(family_id, tuple(item.entry_id for item in entries), "incompatible_reference_title_or_artist_entries_map_to_one_family"))
    return tuple(output)


def _usable_family_evidence(entry: ReferenceEntry, result: AggregatedResult) -> bool:
    """Evidence exists, but conservative title+artist linking did not succeed."""
    core = _core_title(normalize_text(entry.title))
    return any(normalize_text(family.preferred_core_title) == core for family in result.track_families)


def _secondary_only(family_id: str, result: AggregatedResult) -> bool:
    evidence = [item for item in result.evidence if item.family_id == family_id]
    return bool(evidence) and all(item.rank > 1 for item in evidence)


def _feature_excerpt(appearance: Any) -> dict[str, Any]:
    feature = appearance.feature_vector
    return {
        "top_support_windows": feature.top_support_windows,
        "total_supporting_observations": feature.total_supporting_observations,
        "support_density": feature.support_density,
        "total_internal_gaps": feature.total_internal_gaps,
        "distinct_acrid_count": feature.distinct_acrid_count,
        "conflict_count": feature.conflict_count,
        "singleton": feature.singleton,
    }


def _independent_repeat(family_id: str, appearances: tuple[Any, ...], result: AggregatedResult) -> bool:
    ids = {item.appearance_id for item in appearances}
    return any(split.previous_family_id == family_id and split.next_family_id == family_id
               and split.previous_appearance_id in ids and split.next_appearance_id in ids
               for split in result.appearance_splits)


def _test_validation(reference_test: ReferenceTest, result: AggregatedResult) -> TestValidationResult:
    appearances_by_family: dict[str, list[Any]] = {}
    for appearance in result.appearances:
        appearances_by_family.setdefault(appearance.family_id, []).append(appearance)
    matches = []
    for entry in reference_test.entries:
        candidates = _family_candidates(entry, result)
        family_ids = tuple(item[0] for item in candidates)
        strong = [item for item in candidates if item[2]]
        candidate_appearance_ids = tuple(sorted({appearance.appearance_id for family_id in family_ids for appearance in appearances_by_family.get(family_id, ())}))
        if len(candidates) == 1 and len(strong) == 1:
            family_id, reason, _ = candidates[0]
            if candidate_appearance_ids:
                matches.append(ReferenceAppearanceMatch(entry.entry_id, MatchState.OCCURRENCE_DETECTED, family_ids, family_id, candidate_appearance_ids, reason))
            elif _secondary_only(family_id, result):
                matches.append(ReferenceAppearanceMatch(entry.entry_id, MatchState.SECONDARY_ONLY, family_ids, family_id, (), "matching_family_has_secondary_evidence_only"))
            else:
                matches.append(ReferenceAppearanceMatch(entry.entry_id, MatchState.FAMILY_CANDIDATE_ONLY, family_ids, family_id, (), "matching_family_has_no_final_appearance"))
        elif candidates:
            matches.append(ReferenceAppearanceMatch(entry.entry_id, MatchState.AMBIGUOUS_MATCH, family_ids, None, candidate_appearance_ids,
                                                    "multiple_conservative_family_candidates" if len(candidates) > 1 else "title_alias_but_family_artist_unknown"))
        elif _usable_family_evidence(entry, result):
            matches.append(ReferenceAppearanceMatch(entry.entry_id, MatchState.MISSED_WITH_USABLE_EVIDENCE, (), None, (), "family_title_evidence_exists_but_conservative_linking_failed"))
        else:
            matches.append(ReferenceAppearanceMatch(entry.entry_id, MatchState.MISSED_NO_USABLE_EVIDENCE, (), None, (), "no_usable_family_or_candidate_evidence"))

    entry_by_id = {item.entry_id: item for item in reference_test.entries}
    detected_by_family: dict[str, list[ReferenceEntry]] = {}
    definitive_linked: set[str] = set()
    ambiguous_hypotheses: set[str] = set()
    for match in matches:
        if match.state is MatchState.OCCURRENCE_DETECTED:
            definitive_linked.update(match.appearance_ids)
            if match.matched_family_id:
                detected_by_family.setdefault(match.matched_family_id, []).append(entry_by_id[match.reference_entry_id])
        elif match.state is MatchState.AMBIGUOUS_MATCH:
            ambiguous_hypotheses.update(match.appearance_ids)
    ambiguous_linked = tuple(sorted(ambiguous_hypotheses - definitive_linked))
    additional_ids = {item.appearance_id for item in result.appearances} - definitive_linked - set(ambiguous_linked)
    family_by_id = {item.family_id: item for item in result.track_families}
    additional = tuple(AdditionalAppearance(
        appearance_id=item.appearance_id, family_id=item.family_id,
        title=family_by_id[item.family_id].preferred_core_title, artists=family_by_id[item.family_id].artists,
        observed_start=item.observed_start, observed_end=item.observed_end,
        top_support_windows=item.feature_vector.top_support_windows,
        total_internal_gaps=item.feature_vector.total_internal_gaps,
        distinct_acrid_count=item.feature_vector.distinct_acrid_count,
        singleton=item.feature_vector.singleton, conflict_ids=item.conflict_ids,
        local_features=_feature_excerpt(item), reason="no_detected_or_ambiguous_reference_link",
    ) for item in result.appearances if item.appearance_id in additional_ids)

    fragmentation = []
    for family_id, entries in sorted(detected_by_family.items()):
        family_appearances = tuple(sorted(appearances_by_family.get(family_id, ()), key=lambda item: (item.observed_start, item.appearance_id)))
        if len(family_appearances) > 1:
            independent = _independent_repeat(family_id, family_appearances, result)
            fragmentation.append(FragmentationCandidate(
                family_id, tuple(item.appearance_id for item in family_appearances), tuple(item.entry_id for item in entries),
                "repeat_supported" if independent else "possible_fragmentation",
                "independent_same_family_split_evidence" if independent else "multiple_appearances_without_independent_repeat_evidence",
            ))
    bridged_gap_ids = tuple(item.appearance_id for item in result.appearances if item.feature_vector.bridged_gap_count)
    gap_fragmentation_ids = tuple(sorted({appearance_id for candidate in fragmentation if candidate.classification == "possible_fragmentation" for appearance_id in candidate.appearance_ids if appearance_id in set(bridged_gap_ids)}))
    counts = {state: sum(match.state is state for match in matches) for state in MatchState}
    duplicate_entry_count = sum(len(case.reference_entry_ids) for case in _duplicate_cases(reference_test.entries))
    reconciliation = ValidationReconciliation(
        reference_total=len(reference_test.entries), occurrence_detected=counts[MatchState.OCCURRENCE_DETECTED],
        family_candidate_only=counts[MatchState.FAMILY_CANDIDATE_ONLY], secondary_only=counts[MatchState.SECONDARY_ONLY],
        ambiguous=counts[MatchState.AMBIGUOUS_MATCH], missed_no_usable_evidence=counts[MatchState.MISSED_NO_USABLE_EVIDENCE],
        missed_with_usable_evidence=counts[MatchState.MISSED_WITH_USABLE_EVIDENCE], duplicate_reference_entry_count=duplicate_entry_count,
        appearance_total=len(result.appearances), reference_linked_appearance_count=len(definitive_linked),
        ambiguous_linked_appearance_count=len(ambiguous_linked), additional_appearance_count=len(additional),
        reference_reconciled=sum(counts.values()) == len(reference_test.entries),
        appearance_reconciled=len(definitive_linked) + len(ambiguous_linked) + len(additional) == len(result.appearances),
    )
    return TestValidationResult(
        test_id=reference_test.test_id, run_id=result.run_id, excluded=reference_test.excluded,
        exclusion_reason=reference_test.exclusion_reason, reference_entries=reference_test.entries,
        reference_entry_count=len(reference_test.entries), matches=tuple(matches),
        ambiguous_linked_appearance_ids=ambiguous_linked, additional_appearances=additional,
        fragmentation_candidates=tuple(fragmentation), overmerge_candidates=_overmerge_candidates(detected_by_family),
        duplicate_reference_cases=_duplicate_cases(reference_test.entries),
        conflict_ids=tuple(item.conflict_id for item in result.conflicts),
        singleton_appearance_ids=tuple(item.appearance_id for item in result.appearances if item.feature_vector.singleton),
        reentry_appearance_ids=tuple(item.appearance_id for item in result.appearances if item.reentry),
        version_churn_appearance_ids=tuple(item.appearance_id for item in result.appearances if item.feature_vector.distinct_acrid_count > 1 or item.feature_vector.version_descriptors),
        bridged_gap_appearance_ids=bridged_gap_ids, gap_fragmentation_appearance_ids=gap_fragmentation_ids,
        transition_ids=tuple(item.transition_id for item in result.transitions), reconciliation=reconciliation,
    )


def _match_family(test: TestValidationResult, reference_test: ReferenceTest, title: str) -> str | None:
    entry = next((item for item in reference_test.entries if normalize_text(item.title) == normalize_text(title)), None)
    if entry is None:
        return None
    match = next(item for item in test.matches if item.reference_entry_id == entry.entry_id)
    return match.matched_family_id or (match.candidate_family_ids[0] if len(match.candidate_family_ids) == 1 else None)


def _check(state: bool | None, regression_id: str, test_id: int, reason: str, evidence_ids: Iterable[str]) -> RegressionCheck:
    return RegressionCheck(regression_id, test_id, RegressionState.NOT_EVALUABLE if state is None else (RegressionState.PASS if state else RegressionState.FAIL), reason, tuple(evidence_ids))


def _regression_matrix(reference: OfflineReferenceDocument, tests: Mapping[int, TestValidationResult], results: Mapping[int, AggregatedResult]) -> tuple[RegressionCheck, ...]:
    references = {item.test_id: item for item in reference.tests}
    def family(test_id: int, title: str) -> str | None:
        return _match_family(tests[test_id], references[test_id], title)
    def appearances(test_id: int, family_id: str | None) -> list[Any]:
        return [item for item in results[test_id].appearances if family_id and item.family_id == family_id]
    def exact_for_family(test_id: int, family_id: str | None) -> list[Any]:
        if not family_id:
            return []
        family_occurrences = [item for item in results[test_id].family_occurrences if item.family_id == family_id]
        exact_ids = {exact_id for item in family_occurrences for exact_id in item.exact_occurrence_ids}
        return [item for item in results[test_id].exact_occurrences if item.occurrence_id in exact_ids]
    def family_by_core(test_id: int, title: str) -> str | None:
        matches = [item.family_id for item in results[test_id].track_families if normalize_text(item.preferred_core_title) == _core_title(normalize_text(title))]
        return matches[0] if len(matches) == 1 else None
    def duplicate_single_appearance(test_id: int, title: str) -> tuple[bool, tuple[str, ...]]:
        """Reference duplication is validation provenance, never core repetition evidence."""
        test = tests[test_id]
        entries = tuple(item for item in references[test_id].entries if normalize_text(item.title) == normalize_text(title))
        matches = tuple(item for item in test.matches if item.reference_entry_id in {entry.entry_id for entry in entries})
        appearance_ids = tuple(sorted({appearance_id for item in matches for appearance_id in item.appearance_ids}))
        family_ids = {item.matched_family_id for item in matches if item.matched_family_id}
        family_appearances = tuple(
            item.appearance_id for item in results[test_id].appearances if item.family_id in family_ids
        )
        has_repeat_claim = any(
            candidate.classification == "repeat_supported" and family_ids == {candidate.family_id}
            for candidate in test.fragmentation_candidates
        )
        return (
            len(entries) > 1
            and len(matches) == len(entries)
            and all(item.state is MatchState.OCCURRENCE_DETECTED for item in matches)
            and len(appearance_ids) == 1
            and len(family_appearances) == 1
            and not has_repeat_claim,
            appearance_ids,
        )

    regret = family(4, "Regret"); touch = family(8, "Touch (feat. Dj K-Deucez)")
    prey = family(6, "The Prey (Mind Against Remix)"); pretty = family(6, "Pretty Eyes (Extended Mix)")
    mind = family(3, "On My Mind"); permanent, wants = family(1, "Permanent"), family(1, "Wants and Needs")
    neway, do_it = family(9, "N E Way"), family(9, "Do It Any Way You Wanna")
    selby, heaven = family(10, "De Selby (Part 2)"), family_by_core(4, "Heaven Or Las Vegas")
    regret_exact, touch_exact = exact_for_family(4, regret), exact_for_family(8, touch)
    pretty_node = next((item for item in results[6].track_families if item.family_id == pretty), None)
    mind_families = [item for item in results[3].track_families if normalize_text(item.preferred_core_title) == _core_title(normalize_text("On My Mind"))]
    permanent_evidence = [item for item in results[1].evidence if item.family_id == permanent]
    do_it_evidence = [item for item in results[9].evidence if item.family_id == do_it]
    sparse_context = results[10].source_feature_context
    sparse_windows = {item.window_id: item for item in results[10].windows}
    sparse_appearances = [
        item for item in results[10].appearances
        if item.observed_start == 1940 and item.observed_end == 2122 and item.feature_vector.top_support_windows == 4
    ]
    sparse_internal_no_results = 0
    if len(sparse_appearances) == 1:
        support_starts = [sparse_windows[item].source_start for item in sparse_appearances[0].supporting_window_ids]
        sparse_internal_no_results = sum(
            item.status.value == "no_result_1001" and min(support_starts) < item.source_start < max(support_starts)
            for item in results[10].windows
        )
    duplicate_turn_me_on, duplicate_turn_me_on_ids = duplicate_single_appearance(1, "Turn Me On")
    duplicate_sip_it, duplicate_sip_it_ids = duplicate_single_appearance(1, "Sip It")
    duplicate_touch, duplicate_touch_ids = duplicate_single_appearance(8, "Touch (feat. Dj K-Deucez)")
    test_two = tests[2]
    test_two_result = results[2]
    test_two_structural = validate_aggregated_result(test_two_result)
    checks = (
        _check(bool(regret) and len(appearances(4, regret)) == 1 and len(regret_exact) == 1 and len(regret_exact[0].supports) == 24 and not regret_exact[0].gaps, "R01", 4, "one_stable_final_appearance", [item.appearance_id for item in appearances(4, regret)]),
        _check(bool(touch) and len(appearances(8, touch)) == 1 and any(len(item.supports) == 6 and len(item.gaps) == 9 and all(gap.bridged for gap in item.gaps) for item in touch_exact), "R02", 8, "one_final_appearance_with_bounded_1001_series", [item.appearance_id for item in appearances(8, touch)]),
        _check(bool(prey) and len(appearances(6, prey)) == 1, "R03", 6, "one_continuous_final_family_appearance", [item.appearance_id for item in appearances(6, prey)]),
        _check(bool(pretty_node) and "ambiguous_recording" in pretty_node.ambiguity_flags and len(appearances(6, pretty)) == 1, "R04", 6, "one_final_appearance_recording_ambiguity_retained", [item.appearance_id for item in appearances(6, pretty)]),
        _check(bool(mind) and len(mind_families) > 1, "R05", 3, "same_title_not_overmerged_across_artists", [item.family_id for item in mind_families]),
        _check(bool(permanent and wants) and any(item.kind.value == "persistent_competitor" for item in permanent_evidence) and bool(results[1].conflicts), "R06", 1, "persistent_competitor_retained_in_final_output", [permanent, wants] if permanent and wants else ()),
        _check(bool(neway and do_it) and any(item.kind.value == "persistent_competitor" for item in do_it_evidence) and bool(results[9].conflicts) and not any(item.to_family_id == do_it and item.state.value == "directed" for item in results[9].transitions), "R07", 9, "competitor_retained_without_invented_occurrence_or_directed_transition", [neway, do_it] if neway and do_it else ()),
        _check(bool(appearances(10, selby)) and any(item.feature_vector.singleton and item.confidence.status is ConfidenceStatus.NOT_CALIBRATED for item in appearances(10, selby)), "R08", 10, "singleton_retained_not_calibrated", [item.appearance_id for item in appearances(10, selby)]),
        _check(bool(appearances(4, heaven)) and any(item.feature_vector.singleton and item.confidence.status is ConfidenceStatus.NOT_CALIBRATED for item in appearances(4, heaven)), "R09", 4, "high_score_singleton_not_auto_confirmed", [item.appearance_id for item in appearances(4, heaven)]),
        _check(
            sparse_context.total_windows == 650
            and sparse_context.no_result_1001_windows == 508
            and len(sparse_appearances) == 1
            and sparse_internal_no_results == 14
            and sparse_appearances[0].feature_vector.top_support_windows == 4
            and sparse_appearances[0].feature_vector.bounded_1001_count > 0,
            "R10", 10, "sparse_medley_retained_with_typed_gap_and_coverage_evidence",
            [item.appearance_id for item in sparse_appearances],
        ),
        _check(
            duplicate_turn_me_on and duplicate_sip_it and duplicate_touch,
            "R11", 0, "duplicate_reference_entries_link_to_one_temporal_appearance_without_repeat_claim",
            duplicate_turn_me_on_ids + duplicate_sip_it_ids + duplicate_touch_ids,
        ),
        _check(
            test_two.excluded
            and test_two.exclusion_reason == "reference_source_mismatch"
            and test_two.test_id not in tuple(item.test_id for item in tests.values() if not item.excluded)
            and test_two_structural.valid
            and test_two_result.evidence_accounting.canonical_observation_count == test_two_result.evidence_accounting.accounted_observation_count
            and test_two_result.evidence_accounting.canonical_observation_count == test_two_result.source_feature_context.candidate_observation_count,
            "R12", 2, "source_reference_mismatch_excluded_while_structural_evidence_is_conserved",
            [test_two_result.run_id],
        ),
    )
    return checks


def _db_readiness(summary: OfflineValidationSummary, checks: tuple[RegressionCheck, ...], results: Iterable[AggregatedResult]) -> DbReadinessGate:
    reasons = []
    defined = {item.regression_id for item in checks}
    missing = tuple(item for item in ("R10", "R11", "R12") if item not in defined)
    if missing:
        reasons.append("missing_approved_regression_definitions_" + "_".join(item.casefold() for item in missing))
    if not all(item.state is RegressionState.PASS for item in checks):
        reasons.append("final_regression_failure")
    if not summary.reconciliation_valid:
        reasons.append("validation_accounting_not_reconciled")
    if any(result.evidence_accounting.accounted_observation_count != result.evidence_accounting.canonical_observation_count for result in results):
        reasons.append("aggregation_evidence_not_conserved")
    return DbReadinessGate(DbReadinessState.PASS if not reasons else DbReadinessState.FAIL, tuple(reasons))


def validate_against_reference(reference: OfflineReferenceDocument, results: Iterable[AggregatedResult]) -> ValidationResult:
    """Compare completed S8 results with reference; never changes aggregation data."""
    by_test: dict[int, AggregatedResult] = {}
    for result in results:
        test_id = _source_job_id(result)
        if test_id in by_test:
            raise ReferenceValidationError(f"multiple results map to test {test_id}")
        by_test[test_id] = result
    if set(by_test) != set(range(1, 11)):
        raise ReferenceValidationError("S9 requires one S8 result for each reference test 1..10")
    test_results = tuple(_test_validation(item, by_test[item.test_id]) for item in reference.tests)
    comparable = [item for item in test_results if not item.excluded]
    matches = [match for item in comparable for match in item.matches]
    fragments = [item for test in comparable for item in test.fragmentation_candidates]
    counts = {state: sum(match.state is state for match in matches) for state in MatchState}
    summary = OfflineValidationSummary(
        comparable_test_count=len(comparable), excluded_test_ids=tuple(item.test_id for item in test_results if item.excluded),
        reference_entry_count=sum(item.reference_entry_count for item in comparable),
        occurrence_detected_count=counts[MatchState.OCCURRENCE_DETECTED], family_candidate_only_count=counts[MatchState.FAMILY_CANDIDATE_ONLY],
        secondary_only_count=counts[MatchState.SECONDARY_ONLY], ambiguous_match_count=counts[MatchState.AMBIGUOUS_MATCH],
        missed_no_usable_evidence_count=counts[MatchState.MISSED_NO_USABLE_EVIDENCE], missed_with_usable_evidence_count=counts[MatchState.MISSED_WITH_USABLE_EVIDENCE],
        additional_appearance_count=sum(len(item.additional_appearances) for item in comparable),
        ambiguous_linked_appearance_count=sum(len(item.ambiguous_linked_appearance_ids) for item in comparable),
        fragmentation_candidate_count=sum(item.classification == "possible_fragmentation" for item in fragments),
        repeat_supported_count=sum(item.classification == "repeat_supported" for item in fragments),
        overmerge_candidate_count=sum(len(item.overmerge_candidates) for item in comparable),
        duplicate_reference_case_count=sum(len(item.duplicate_reference_cases) for item in comparable),
        conflict_count=sum(len(item.conflict_ids) for item in comparable), singleton_count=sum(len(item.singleton_appearance_ids) for item in comparable),
        reentry_count=sum(len(item.reentry_appearance_ids) for item in comparable),
        version_churn_appearance_count=sum(len(item.version_churn_appearance_ids) for item in comparable),
        bridged_gap_appearance_count=sum(len(item.bridged_gap_appearance_ids) for item in comparable),
        reconciliation_valid=all(item.reconciliation.reference_reconciled and item.reconciliation.appearance_reconciled for item in comparable),
    )
    checks = _regression_matrix(reference, {item.test_id: item for item in test_results}, by_test)
    return ValidationResult(reference.document_id, test_results, summary, checks, _db_readiness(summary, checks, by_test.values()))


def serialize_validation_result(result: ValidationResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
