import unittest

from src.aggregation import (
    CandidateObservation,
    CanonicalRecognitionRun,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
    CALIBRATED_AGGREGATION_V2,
    ReentryReason,
    SplitReason,
    TransitionState,
    aggregate_families,
    aggregate_secondary_evidence,
    aggregate_temporally,
    aggregate_transitions,
    normalize_run,
)


def candidate(index, rank, family, offset=0, acrid=None):
    return CandidateObservation(
        rank=rank, score=50, acrid=acrid or f"{family}-{index}-{rank}", title=family,
        artists=(f"{family} artist",), play_offset=offset,
        raw_provenance=RawProvenancePointer("test", f"candidate-{index}-{rank}"),
    )


def pipeline(events, profile=None):
    windows = []
    for index, event in enumerate(events):
        start = index * 10
        if event is None:
            status, candidates = RecognitionWindowStatus.NO_RESULT_1001, ()
        elif isinstance(event, RecognitionWindowStatus):
            status, candidates = event, ()
        else:
            status, candidates = RecognitionWindowStatus.CANDIDATES_RETURNED, tuple(event)
        windows.append(RecognitionWindow(
            index=index, source_start=start, source_end=start + 10, actual_duration=10,
            status=status, candidates=candidates, raw_provenance=RawProvenancePointer("test", f"window-{index}"),
        ))
    canonical = CanonicalRecognitionRun(
        run_id="transition", source_id="source", source_duration=len(windows) * 10,
        window_size=10, step=10, windows=tuple(windows),
        source_provenance=RawProvenancePointer("test", "source"), source_raw_values={"logical_sources": []},
    )
    families = aggregate_families(aggregate_temporally(normalize_run(canonical)))
    if profile is None:
        return aggregate_transitions(aggregate_secondary_evidence(families))
    return aggregate_transitions(aggregate_secondary_evidence(families, profile), profile)


def mixed(index, top, secondary, offset=0):
    return [candidate(index, 1, top, offset), candidate(index, 2, secondary, offset)]


class TransitionTests(unittest.TestCase):
    def test_rank_switch_pattern_creates_directed_transition_with_uncertain_boundary(self):
        result = pipeline([mixed(0, "a", "b"), mixed(1, "a", "b"), mixed(2, "b", "a"), mixed(3, "b", "a")])
        self.assertEqual(len(result.appearances), 2)
        transition = result.transitions[0]
        self.assertEqual(transition.state, TransitionState.DIRECTED)
        self.assertLessEqual(transition.earliest_plausible_boundary, transition.latest_plausible_boundary)
        self.assertIn("window_level_boundary", transition.uncertainty_flags)

    def test_one_shared_window_and_persistent_competitor_do_not_create_transition(self):
        shared = pipeline([mixed(0, "a", "b")])
        persistent = pipeline([mixed(0, "a", "b"), mixed(1, "a", "b"), mixed(2, "a", "b")])
        self.assertFalse(shared.transitions)
        self.assertFalse(persistent.transitions)
        self.assertTrue(persistent.original.conflicts)

    def test_a_b_a_retains_repeated_family_appearances_and_two_transitions(self):
        result = pipeline([
            mixed(0, "a", "b"), mixed(1, "a", "b"),
            *([None] * 13),
            mixed(15, "b", "a"), mixed(16, "b", "a"),
            *([None] * 13),
            mixed(30, "a", "b"), mixed(31, "a", "b"),
        ])
        self.assertEqual([item.family_id for item in result.appearances][0], [item.family_id for item in result.appearances][2])
        self.assertEqual(len(result.appearances), 3)
        self.assertEqual(len(result.transitions), 2)
        self.assertTrue(result.appearances[2].reentry)
        self.assertEqual(result.appearances[2].reentry_reason, ReentryReason.SUSTAINED_INCOMPATIBLE_FAMILY)

    def test_short_1001_and_play_offset_jump_do_not_create_restart(self):
        short_gap = pipeline([[candidate(0, 1, "a", 1)], None, [candidate(2, 1, "a", 999)]])
        jump = pipeline([[candidate(0, 1, "a", 1)], [candidate(1, 1, "a", 999)], [candidate(2, 1, "a", 2)]])
        self.assertEqual(len(short_gap.appearances), 1)
        self.assertEqual(len(jump.appearances), 1)
        self.assertFalse(jump.appearances[0].reentry)

    def test_long_unsupported_gap_is_uncertainty_not_reentry(self):
        long_gap = pipeline([[candidate(0, 1, "a")], None, None, None, [candidate(4, 1, "a")]])
        self.assertEqual(len(long_gap.appearances), 1)
        self.assertFalse(long_gap.appearances[0].reentry)
        self.assertTrue(long_gap.appearances[0].reentry_candidate)
        self.assertFalse(long_gap.splits)

    def test_calibrated_timeline_does_not_split_same_family_on_offsets_alone(self):
        result = pipeline([
            [candidate(0, 1, "a", 0, acrid="same")],
            [candidate(1, 1, "a", 10000, acrid="same")],
            None,
            [candidate(3, 1, "a", 0, acrid="same")],
            [candidate(4, 1, "a", 10000, acrid="same")],
        ], CALIBRATED_AGGREGATION_V2)
        self.assertEqual(len(result.appearances), 1)
        self.assertFalse(result.splits)

    def test_calibrated_timeline_keeps_continuous_playback_across_no_result(self):
        result = pipeline([
            [candidate(0, 1, "a", 0, acrid="same")],
            [candidate(1, 1, "a", 10000, acrid="same")],
            None,
            [candidate(3, 1, "a", 30000, acrid="same")],
            [candidate(4, 1, "a", 40000, acrid="same")],
        ], CALIBRATED_AGGREGATION_V2)
        self.assertEqual(len(result.appearances), 1)
        self.assertFalse(result.splits)

    def test_calibrated_timeline_does_not_split_on_one_noisy_offset(self):
        result = pipeline([
            [candidate(0, 1, "a", 0, acrid="same")],
            [candidate(1, 1, "a", 10000, acrid="same")],
            None,
            [candidate(3, 1, "a", 0, acrid="same")],
        ], CALIBRATED_AGGREGATION_V2)
        self.assertEqual(len(result.appearances), 1)
        self.assertFalse(result.splits)

    def test_calibrated_timeline_keeps_isolated_competitor_when_restart_is_proven(self):
        result = pipeline([
            [candidate(0, 1, "a", 0, acrid="same-a")],
            [candidate(1, 1, "a", 10000, acrid="same-a")],
            [candidate(2, 1, "b", 0, acrid="single-b")],
            [candidate(3, 1, "a", 0, acrid="same-a")],
            [candidate(4, 1, "a", 10000, acrid="same-a")],
        ], CALIBRATED_AGGREGATION_V2)
        self.assertEqual([item.family_id for item in result.appearances][0], [item.family_id for item in result.appearances][2])
        self.assertEqual(len(result.appearances), 3)

    def test_calibrated_timeline_absorbs_isolated_competitor_during_continuous_playback(self):
        result = pipeline([
            [candidate(0, 1, "a", 0, acrid="same-a")],
            [candidate(1, 1, "a", 10000, acrid="same-a")],
            [candidate(2, 1, "b", 0, acrid="single-b")],
            [candidate(3, 1, "a", 30000, acrid="same-a")],
            [candidate(4, 1, "a", 40000, acrid="same-a")],
        ], CALIBRATED_AGGREGATION_V2)
        self.assertEqual(len(result.appearances), 1)
        self.assertTrue(result.appearances[0].unresolved_separation_window_ids)

    def test_sustained_incompatible_family_and_hard_break_create_explainable_reentry(self):
        sustained = pipeline([
            mixed(0, "a", "b"), mixed(1, "a", "b"), *([None] * 13),
            mixed(15, "b", "a"), mixed(16, "b", "a"), *([None] * 13),
            mixed(30, "a", "b"), mixed(31, "a", "b"),
        ])
        self.assertEqual(len([item for item in sustained.appearances if item.family_id == sustained.appearances[0].family_id]), 2)
        self.assertEqual(sustained.appearances[-1].reentry_reason, ReentryReason.SUSTAINED_INCOMPATIBLE_FAMILY)
        self.assertTrue(all(item.reason is SplitReason.SUSTAINED_DOMINANT_HANDOFF for item in sustained.splits))
        hard = pipeline([
            [candidate(0, 1, "a")], RecognitionWindowStatus.NOT_SUBMITTED,
            [candidate(2, 1, "a")], [candidate(3, 1, "a")],
        ])
        self.assertEqual(len(hard.appearances), 2)
        self.assertEqual(hard.appearances[1].reentry_reason, ReentryReason.HARD_PROCESSING_DISCONTINUITY)
        self.assertEqual(hard.splits[0].reason, SplitReason.HARD_PROCESSING_DISCONTINUITY)

    def test_persistent_competitor_does_not_split_or_direct_transition(self):
        result = pipeline([mixed(0, "a", "b"), mixed(1, "a", "b"), mixed(2, "a", "b"), mixed(3, "a", "b")])
        self.assertEqual(len(result.appearances), 1)
        self.assertFalse(result.transitions)
        self.assertTrue(result.original.conflicts)

    def test_boundary_ids_source_end_and_output_are_deterministic(self):
        first = pipeline([[candidate(0, 1, "a")], [candidate(1, 1, "a")]])
        second = pipeline([[candidate(0, 1, "a")], [candidate(1, 1, "a")]])
        appearance = first.appearances[0]
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(appearance.observed_end, 20)
        self.assertTrue(appearance.boundary_window_ids)
        original_ids = {item.observation_id for window in first.original.original.original.original.windows for item in window.candidates}
        evidence_ids = {item.observation_id for item in first.original.evidence}
        self.assertEqual(original_ids, evidence_ids)


if __name__ == "__main__":
    unittest.main()
