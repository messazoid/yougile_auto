"""Evidence-based family appearances and transition hypotheses (S6).

An unsupported interval is uncertainty, not an occurrence boundary. A new
appearance needs sustained rank-one handoff evidence or a hard processing break
followed by sustained new-context support.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any

from .models import RecognitionWindowStatus
from .secondary import (
    AggregationProfile,
    OFFLINE_AGGREGATION_EXPERIMENTAL_V1,
    SecondaryEvidenceAggregationResult,
    SecondaryEvidenceKind,
)


TRANSITION_CONTRACT_VERSION = "family-transition-aggregation/v1.1-a"


class TransitionState(StrEnum):
    DIRECTED = "directed"
    POSSIBLE = "possible"
    UNRESOLVED = "unresolved"


class ReentryReason(StrEnum):
    SUSTAINED_INCOMPATIBLE_FAMILY = "sustained_incompatible_family"
    HARD_PROCESSING_DISCONTINUITY = "hard_processing_discontinuity"
    # Legacy-readable only: v1.1-A never emits this for unsupported distance.
    LONG_UNSUPPORTED_INTERVAL = "long_unsupported_interval"


class SplitReason(StrEnum):
    SUSTAINED_DOMINANT_HANDOFF = "sustained_dominant_handoff"
    HARD_PROCESSING_DISCONTINUITY = "hard_processing_discontinuity"
    ISOLATED_RANK_ONE_EVIDENCE = "isolated_rank_one_evidence"


def _id(kind: str, material: Any) -> str:
    return f"{kind}_" + hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class SplitGapEvidence:
    """A typed canonical window that explains a hard S6 boundary."""

    window_id: str
    status: RecognitionWindowStatus
    source_start: float | int
    source_end: float | int

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id, "status": self.status.value,
            "source_start": self.source_start, "source_end": self.source_end,
        }


@dataclass(frozen=True)
class FamilyAppearance:
    """One observed family appearance; not a composition or exact song boundary."""

    appearance_id: str
    family_id: str
    supporting_observation_ids: tuple[str, ...]
    supporting_window_ids: tuple[str, ...]
    supporting_window_indices: tuple[int, ...]
    s4_family_occurrence_ids: tuple[str, ...]
    observed_start: float | int
    observed_end: float | int
    earliest_plausible_start: float | int
    latest_plausible_start: float | int
    earliest_plausible_end: float | int
    latest_plausible_end: float | int
    boundary_window_ids: tuple[str, ...]
    boundary_uncertainty_reason: str
    reentry: bool
    reentry_reason: ReentryReason | None
    reentry_candidate: bool
    unresolved_separation_window_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "appearance_id": self.appearance_id, "family_id": self.family_id,
            "supporting_observation_ids": list(self.supporting_observation_ids),
            "supporting_window_ids": list(self.supporting_window_ids),
            "supporting_window_indices": list(self.supporting_window_indices),
            "s4_family_occurrence_ids": list(self.s4_family_occurrence_ids),
            "observed_range": {"start": self.observed_start, "end": self.observed_end},
            "plausible_boundary": {"earliest_start": self.earliest_plausible_start, "latest_start": self.latest_plausible_start, "earliest_end": self.earliest_plausible_end, "latest_end": self.latest_plausible_end},
            "boundary_window_ids": list(self.boundary_window_ids),
            "boundary_uncertainty_reason": self.boundary_uncertainty_reason,
            "reentry": self.reentry, "reentry_reason": self.reentry_reason.value if self.reentry_reason else None,
            "reentry_candidate": self.reentry_candidate,
            "unresolved_separation_window_ids": list(self.unresolved_separation_window_ids),
        }


@dataclass(frozen=True)
class DirectedTransition:
    transition_id: str
    from_appearance_id: str
    to_appearance_id: str
    from_family_id: str
    to_family_id: str
    evidence_observation_ids: tuple[str, ...]
    evidence_window_ids: tuple[str, ...]
    earliest_plausible_boundary: float | int
    latest_plausible_boundary: float | int
    state: TransitionState
    reason_codes: tuple[str, ...]
    uncertainty_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id, "from_appearance_id": self.from_appearance_id,
            "to_appearance_id": self.to_appearance_id, "from_family_id": self.from_family_id,
            "to_family_id": self.to_family_id, "evidence_observation_ids": list(self.evidence_observation_ids),
            "evidence_window_ids": list(self.evidence_window_ids),
            "earliest_plausible_boundary": self.earliest_plausible_boundary,
            "latest_plausible_boundary": self.latest_plausible_boundary, "state": self.state.value,
            "reason_codes": list(self.reason_codes), "uncertainty_flags": list(self.uncertainty_flags),
        }


@dataclass(frozen=True)
class AppearanceSplit:
    """Explainable boundary for every real S6 split/re-entry."""

    split_id: str
    previous_appearance_id: str
    next_appearance_id: str
    previous_family_id: str
    next_family_id: str
    reason: SplitReason
    supporting_window_ids: tuple[str, ...]
    typed_gap_evidence: tuple[SplitGapEvidence, ...]
    intervening_family_ids: tuple[str, ...]
    intervening_observation_ids: tuple[str, ...]
    transition_evidence_observation_ids: tuple[str, ...]
    transition_id: str | None
    profile_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "split_id": self.split_id,
            "previous_appearance_id": self.previous_appearance_id,
            "next_appearance_id": self.next_appearance_id,
            "previous_family_id": self.previous_family_id,
            "next_family_id": self.next_family_id,
            "reason": self.reason.value,
            "supporting_window_ids": list(self.supporting_window_ids),
            "typed_gap_evidence": [item.to_dict() for item in self.typed_gap_evidence],
            "intervening_family_ids": list(self.intervening_family_ids),
            "intervening_observation_ids": list(self.intervening_observation_ids),
            "transition_evidence_observation_ids": list(self.transition_evidence_observation_ids),
            "transition_id": self.transition_id,
            "profile_name": self.profile_name,
        }


@dataclass(frozen=True)
class TransitionAggregationResult:
    """S6 output retaining all S0-S5 observations/conflicts under ``original``."""

    original: SecondaryEvidenceAggregationResult
    appearances: tuple[FamilyAppearance, ...]
    transitions: tuple[DirectedTransition, ...]
    splits: tuple[AppearanceSplit, ...]
    profile: AggregationProfile
    contract_version: str = TRANSITION_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "profile": self.profile.name,
            "original": self.original.to_dict(), "appearances": [item.to_dict() for item in self.appearances],
            "transitions": [item.to_dict() for item in self.transitions],
            "splits": [item.to_dict() for item in self.splits],
        }


def _dominant_supports(result: SecondaryEvidenceAggregationResult) -> list[tuple[Any, str | None]]:
    family_by_identity = {identity: family.family_id for family in result.original.families for identity in family.member_recording_identity_ids}
    supports = []
    for window in result.original.original.original.windows:
        rank_one = next((item for item in window.candidates if item.original.rank == 1), None)
        family = family_by_identity.get(rank_one.recording_identity_id) if rank_one else None
        supports.append((window, family))
    return supports


def _s4_occurrence_ids(result: SecondaryEvidenceAggregationResult, indices: set[int], family_id: str) -> tuple[str, ...]:
    exact = {item.occurrence_id: item for item in result.original.original.occurrences}
    return tuple(sorted(
        item.family_occurrence_id for item in result.original.family_occurrences
        if item.family_id == family_id and any(support.window_index in indices for exact_id in item.exact_occurrence_ids for support in exact[exact_id].supports)
    ))


def _rank_one_observation_id(window: Any) -> str:
    return next(item.observation_id for item in window.candidates if item.original.rank == 1)


def _raw_segments(result: SecondaryEvidenceAggregationResult) -> list[dict[str, Any]]:
    """Chronological rank-one runs retaining the canonical gaps before each."""
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    leading_gaps: list[Any] = []
    for window, family_id in _dominant_supports(result):
        if family_id is None:
            leading_gaps.append(window)
            continue
        if current is not None and current["family_id"] == family_id and not leading_gaps:
            current["windows"].append(window)
            continue
        if current is not None:
            segments.append(current)
        current = {"family_id": family_id, "windows": [window], "leading_gaps": tuple(leading_gaps)}
        leading_gaps = []
    if current is not None:
        segments.append(current)
    return segments


def _hard_gaps(windows: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(item for item in windows if item.original.status in {
        RecognitionWindowStatus.NOT_SUBMITTED, RecognitionWindowStatus.MISSING_RAW,
    })


def _new_builder(segment: dict[str, Any], split: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"family_id": segment["family_id"], "windows": list(segment["windows"]),
            "last_segment_length": len(segment["windows"]),
            "last_segment_windows": list(segment["windows"]),
            "unresolved_windows": [], "split": split}


def _stable_play_offset_timelines(windows: list[Any], profile: AggregationProfile) -> dict[str, float]:
    """Return reliable source-to-track timelines for exact recording identities."""
    tolerance = profile.play_offset_timeline_tolerance_ms
    if tolerance is None:
        return {}
    grouped: dict[str, list[float]] = {}
    for window in windows:
        candidate = next((item for item in window.candidates if item.original.rank == 1), None)
        if not candidate or not candidate.recording_identity_id or candidate.original.play_offset is None:
            continue
        intercept = float(candidate.original.play_offset) - float(window.original.source_start) * 1000
        grouped.setdefault(candidate.recording_identity_id, []).append(intercept)
    result = {}
    for identity, values in grouped.items():
        if len(values) < profile.minimum_play_offset_timeline_support:
            continue
        ordered = sorted(values)
        median = ordered[len(ordered) // 2]
        if max(abs(value - median) for value in values) <= tolerance:
            result[identity] = median
    return result


def _play_offset_restart(
    active: dict[str, Any], segment: dict[str, Any], profile: AggregationProfile,
) -> bool:
    """Use play offsets only when both sides independently prove stable timelines."""
    before = _stable_play_offset_timelines(active["last_segment_windows"], profile)
    after = _stable_play_offset_timelines(list(segment["windows"]), profile)
    common = set(before) & set(after)
    tolerance = profile.play_offset_timeline_tolerance_ms
    return bool(
        tolerance is not None and common
        and any(abs(before[identity] - after[identity]) > tolerance for identity in common)
    )


def _overlaps_active_s4_occurrence(
    result: SecondaryEvidenceAggregationResult, active: dict[str, Any], segment: dict[str, Any],
) -> bool:
    """Whether a rank switch remains inside an already continuous S4 series.

    S5 competitors can temporarily become rank one.  They do not close the
    dominant S6 series while their S4 family occurrence overlaps it; this uses
    existing temporal evidence, not a new duration threshold.
    """
    exact = {item.occurrence_id: item for item in result.original.original.occurrences}

    def family_occurrences(family_id: str, windows: list[Any]) -> list[Any]:
        indices = {item.original.index for item in windows}
        return [
            item for item in result.original.family_occurrences if item.family_id == family_id
            and any(support.window_index in indices for exact_id in item.exact_occurrence_ids for support in exact[exact_id].supports)
        ]

    next_occurrences = family_occurrences(segment["family_id"], segment["windows"])
    # Mere adjacent recognition-window geometry is not coexistence. There must
    # be actual rank-one support for the active family inside B's S4 envelope.
    # That makes B a competing/churn signal within the ongoing series, rather
    # than a self-sufficient A-to-B handoff.
    active_support_times = [
        window.original.source_start for window, family_id in _dominant_supports(result)
        if family_id == active["family_id"]
    ]
    return any(
        right.source_start < support_time < right.source_end
        for right in next_occurrences for support_time in active_support_times
    )


def _build_appearances(
    result: SecondaryEvidenceAggregationResult, profile: AggregationProfile,
) -> tuple[tuple[FamilyAppearance, ...], tuple[dict[str, Any], ...]]:
    """Materialise only explainable S6 boundaries.

    ``repeated_appearance_separation_windows`` remains an uncertainty detection
    horizon; it never closes an active appearance by itself.
    """
    builders: list[dict[str, Any]] = []
    seen_families: set[str] = set()
    active: dict[str, Any] | None = None
    for segment in _raw_segments(result):
        family_id, segment_windows, leading_gaps = segment["family_id"], segment["windows"], segment["leading_gaps"]
        hard_gaps = _hard_gaps(leading_gaps)
        sustained = len(segment_windows) >= profile.minimum_sustained_new_family_support
        restart_after_isolated = False
        if active is None:
            active = _new_builder(segment)
            builders.append(active)
            seen_families.add(family_id)
            continue
        # An isolated different-family rank-one appearance is not a separator
        # for a previously active family. If that family resumes, retain the
        # singleton as unresolved evidence and continue the original series.
        # This is the crucial distinction between a candidate relation and a
        # sustained intervening handoff.
        if family_id != active["family_id"] and active["last_segment_length"] < profile.minimum_sustained_new_family_support and family_id in seen_families:
            prior = next((item for item in reversed(builders[:-1]) if item["family_id"] == family_id), None)
            if prior is not None:
                restart_after_isolated = _play_offset_restart(prior, segment, profile)
                if not restart_after_isolated:
                    prior["unresolved_windows"].extend((*active["windows"], *leading_gaps))
                    builders.pop()
                    active = prior
        if family_id == active["family_id"]:
            if hard_gaps and sustained:
                split = {"previous_builder_index": len(builders) - 1,
                         "reason": SplitReason.HARD_PROCESSING_DISCONTINUITY,
                         "gap_windows": hard_gaps, "intervening_family_ids": (),
                         "intervening_observation_ids": ()}
                active = _new_builder(segment, split)
                builders.append(active)
                continue
            if leading_gaps and (len(leading_gaps) > profile.repeated_appearance_separation_windows or hard_gaps):
                active["unresolved_windows"].extend(leading_gaps)
            active["windows"].extend(segment_windows)
            active["last_segment_length"] = len(segment_windows)
            active["last_segment_windows"] = list(segment_windows)
            continue

        # A one-window *rank-one* hit is still a retained singleton appearance
        # (S3 invariant), but it does not create a directed handoff. A later
        # return to an already-separated family is allowed because the
        # sustained intervening family is the separator.
        reentry_after_separator = family_id in seen_families and active["last_segment_length"] >= profile.minimum_sustained_new_family_support
        overlapping_s4 = _overlaps_active_s4_occurrence(result, active, segment)
        if ((sustained or reentry_after_separator) and (not overlapping_s4 or restart_after_isolated)) or len(segment_windows) == 1:
            reason = (SplitReason.SUSTAINED_DOMINANT_HANDOFF if (sustained or reentry_after_separator) and (not overlapping_s4 or restart_after_isolated)
                      else SplitReason.ISOLATED_RANK_ONE_EVIDENCE)
            split = {"previous_builder_index": len(builders) - 1,
                     "reason": reason,
                     "gap_windows": hard_gaps, "intervening_family_ids": (family_id,),
                     "intervening_observation_ids": tuple(_rank_one_observation_id(item) for item in segment_windows)}
            active = _new_builder(segment, split)
            builders.append(active)
            seen_families.add(family_id)
            continue
        active["unresolved_windows"].extend((*leading_gaps, *segment_windows))

    all_windows = [item[0] for item in _dominant_supports(result)]
    appearances: list[FamilyAppearance] = []
    prior_by_family: dict[str, FamilyAppearance] = {}
    for builder in builders:
        family_id, member_windows = builder["family_id"], builder["windows"]
        first, last = member_windows[0], member_windows[-1]
        first_index, last_index = first.original.index, last.original.index
        start_context = all_windows[max(0, first_index - profile.boundary_lookaround_windows)]
        end_context = all_windows[min(len(all_windows) - 1, last_index + profile.boundary_lookaround_windows)]
        observation_ids = tuple(_rank_one_observation_id(window) for window in member_windows)
        indices = {window.original.index for window in member_windows}
        split, prior = builder["split"], prior_by_family.get(family_id)
        reentry_reason = None
        if prior is not None and split is not None:
            if split["reason"] is SplitReason.HARD_PROCESSING_DISCONTINUITY:
                reentry_reason = ReentryReason.HARD_PROCESSING_DISCONTINUITY
            else:
                reentry_reason = ReentryReason.SUSTAINED_INCOMPATIBLE_FAMILY
        unresolved = tuple(dict.fromkeys(item.original.window_id for item in builder["unresolved_windows"]))
        appearance = FamilyAppearance(
            appearance_id=_id("family_appearance", {"family": family_id, "supports": observation_ids}), family_id=family_id,
            supporting_observation_ids=observation_ids,
            supporting_window_ids=tuple(window.original.window_id for window in member_windows),
            supporting_window_indices=tuple(window.original.index for window in member_windows),
            s4_family_occurrence_ids=_s4_occurrence_ids(result, indices, family_id),
            observed_start=first.original.source_start, observed_end=last.original.source_end,
            earliest_plausible_start=start_context.original.source_start, latest_plausible_start=first.original.source_end,
            earliest_plausible_end=last.original.source_start, latest_plausible_end=end_context.original.source_end,
            boundary_window_ids=tuple(dict.fromkeys((start_context.original.window_id, first.original.window_id, last.original.window_id, end_context.original.window_id))),
            boundary_uncertainty_reason="recognition_window_geometry",
            reentry=reentry_reason is not None, reentry_reason=reentry_reason,
            reentry_candidate=bool(unresolved), unresolved_separation_window_ids=unresolved,
        )
        appearances.append(appearance)
        prior_by_family[family_id] = appearance
    return tuple(appearances), tuple(builder["split"] for builder in builders if builder["split"] is not None)


def _transitions(
    result: SecondaryEvidenceAggregationResult, appearances: tuple[FamilyAppearance, ...], profile: AggregationProfile,
) -> tuple[DirectedTransition, ...]:
    evidence_by_window: dict[int, list[Any]] = {}
    for item in result.evidence:
        evidence_by_window.setdefault(item.window_index, []).append(item)
    transitions = []
    for previous, current in zip(appearances, appearances[1:]):
        if previous.family_id == current.family_id:
            continue
        lower = previous.supporting_window_indices[-1] - profile.transition_lookaround_windows
        upper = current.supporting_window_indices[0] + profile.transition_lookaround_windows
        switch = [item for index in range(lower, upper + 1) for item in evidence_by_window.get(index, ())
                  if item.family_id in {previous.family_id, current.family_id}
                  and item.kind in {SecondaryEvidenceKind.WEAK_ALTERNATIVE, SecondaryEvidenceKind.PERSISTENT_COMPETITOR}]
        sustained = len(current.supporting_observation_ids) >= profile.minimum_sustained_new_family_support
        # Secondary evidence upgrades a separately observed rank-one handoff;
        # it cannot create a split or a directed transition on its own.
        if not sustained:
            # Isolated rank-one evidence is a retained appearance, not an
            # inferred transition/handoff.
            continue
        if sustained and switch:
            state, reasons = TransitionState.DIRECTED, ("sustained_dominant_handoff", "mixed_rank_evidence")
        elif sustained:
            state, reasons = TransitionState.POSSIBLE, ("sustained_dominant_handoff",)
        else:
            state, reasons = TransitionState.UNRESOLVED, ("reentry_after_sustained_separator",)
        observation_ids = tuple(dict.fromkeys(previous.supporting_observation_ids[-1:] + tuple(item.observation_id for item in switch) + current.supporting_observation_ids[:1]))
        window_ids = tuple(dict.fromkeys(previous.supporting_window_ids[-1:] + tuple(item.window_id for item in switch) + current.supporting_window_ids[:1]))
        earliest, latest = sorted((previous.earliest_plausible_end, current.latest_plausible_start))
        transitions.append(DirectedTransition(
            transition_id=_id("directed_transition", {"from": previous.appearance_id, "to": current.appearance_id, "evidence": observation_ids, "state": state.value}),
            from_appearance_id=previous.appearance_id, to_appearance_id=current.appearance_id,
            from_family_id=previous.family_id, to_family_id=current.family_id,
            evidence_observation_ids=observation_ids, evidence_window_ids=window_ids,
            earliest_plausible_boundary=earliest, latest_plausible_boundary=latest, state=state,
            reason_codes=reasons, uncertainty_flags=("window_level_boundary", "no_overlap_claim"),
        ))
    return tuple(transitions)


def _build_splits(
    appearances: tuple[FamilyAppearance, ...], drafts: tuple[dict[str, Any], ...],
    transitions: tuple[DirectedTransition, ...], profile: AggregationProfile,
) -> tuple[AppearanceSplit, ...]:
    transition_by_pair = {(item.from_appearance_id, item.to_appearance_id): item for item in transitions}
    splits = []
    for draft in drafts:
        previous = appearances[draft["previous_builder_index"]]
        current = appearances[draft["previous_builder_index"] + 1]
        transition = transition_by_pair.get((previous.appearance_id, current.appearance_id))
        gap_evidence = tuple(SplitGapEvidence(item.original.window_id, item.original.status,
                                               item.original.source_start, item.original.source_end)
                             for item in draft["gap_windows"])
        supporting_windows = tuple(dict.fromkeys(
            previous.supporting_window_ids[-1:] + tuple(item.window_id for item in gap_evidence)
            + current.supporting_window_ids[:profile.minimum_sustained_new_family_support]
        ))
        material = {"previous": previous.appearance_id, "next": current.appearance_id,
                    "reason": draft["reason"].value, "supporting_windows": supporting_windows,
                    "gaps": [item.to_dict() for item in gap_evidence], "profile": profile.name}
        splits.append(AppearanceSplit(
            split_id=_id("appearance_split", material), previous_appearance_id=previous.appearance_id,
            next_appearance_id=current.appearance_id, previous_family_id=previous.family_id,
            next_family_id=current.family_id, reason=draft["reason"], supporting_window_ids=supporting_windows,
            typed_gap_evidence=gap_evidence, intervening_family_ids=draft["intervening_family_ids"],
            intervening_observation_ids=draft["intervening_observation_ids"],
            transition_evidence_observation_ids=transition.evidence_observation_ids if transition else (),
            transition_id=transition.transition_id if transition else None, profile_name=profile.name,
        ))
    return tuple(splits)


def aggregate_transitions(
    result: SecondaryEvidenceAggregationResult,
    profile: AggregationProfile = OFFLINE_AGGREGATION_EXPERIMENTAL_V1,
) -> TransitionAggregationResult:
    """Interpret rank-one chronology without changing S2-S5 identity or conflicts."""
    appearances, split_drafts = _build_appearances(result, profile)
    transitions = _transitions(result, appearances, profile)
    return TransitionAggregationResult(result, appearances, transitions,
                                       _build_splits(appearances, split_drafts, transitions, profile), profile)
