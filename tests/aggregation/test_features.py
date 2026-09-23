import unittest
from pathlib import Path

from offline.aggregation.archive_adapter import load_run
from src.aggregation import (
    CandidateObservation,
    CanonicalRecognitionRun,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
    aggregate_families,
    aggregate_secondary_evidence,
    aggregate_temporally,
    aggregate_transitions,
    extract_features,
    normalize_run,
    score_summary,
)


RAW_ROOT = Path(__file__).parents[2] / "offline" / "fixtures" / "raw"


def candidate(index, rank, acrid, title, *, artists=("Artist",), isrc=None, score=50, offset=None, version=None):
    return CandidateObservation(
        rank=rank, score=score, acrid=acrid, title=title, artists=artists,
        isrc=isrc, version=version, play_offset=offset,
        raw_provenance=RawProvenancePointer("test", f"candidate-{index}-{rank}"),
        raw_values={"play_offset_ms": offset} if offset is not None else {},
    )


def result_from_events(events):
    windows = []
    for index, event in enumerate(events):
        if event is None:
            status, candidates = RecognitionWindowStatus.NO_RESULT_1001, ()
        elif event == "error":
            status, candidates = RecognitionWindowStatus.PROCESSING_ERROR, ()
        elif event == "not_submitted":
            status, candidates = RecognitionWindowStatus.NOT_SUBMITTED, ()
        elif event == "missing_raw":
            status, candidates = RecognitionWindowStatus.MISSING_RAW, ()
        else:
            status, candidates = RecognitionWindowStatus.CANDIDATES_RETURNED, tuple(event)
        windows.append(RecognitionWindow(
            index=index, source_start=index * 10, source_end=index * 10 + 10,
            actual_duration=10, status=status, candidates=candidates,
            raw_provenance=RawProvenancePointer("test", f"window-{index}"),
        ))
    canonical = CanonicalRecognitionRun(
        run_id="features", source_id="source", source_duration=len(windows) * 10,
        window_size=10, step=10, windows=tuple(windows),
        source_provenance=RawProvenancePointer("test", "source"),
        source_raw_values={"logical_sources": []},
    )
    return extract_features(aggregate_transitions(aggregate_secondary_evidence(
        aggregate_families(aggregate_temporally(normalize_run(canonical)))
    )))


def vector(result, position=0):
    return result.appearance_features[position]


def local(result, position=0):
    return result.local_evidence[position]


class FeatureExtractionTests(unittest.TestCase):
    def test_dense_support_and_bridged_1001_are_described_without_decision(self):
        dense = vector(result_from_events([[candidate(0, 1, "a", "A")], [candidate(1, 1, "a", "A")]]))
        gap = vector(result_from_events([[candidate(0, 1, "a", "A")], None, [candidate(2, 1, "a", "A")]]))
        self.assertEqual((dense.distinct_supporting_windows, dense.top_support_windows), (2, 2))
        self.assertEqual(dense.support_density, 0.1)
        self.assertEqual(dict(gap.gaps_by_type)["no_result_1001"], 1)
        self.assertEqual((gap.bridged_gap_count, gap.bounded_1001_count), (1, 1))
        self.assertIsNone(gap.preferred_recording_identity_id)

    def test_processing_errors_and_not_submitted_are_separate(self):
        error = vector(result_from_events([[candidate(0, 1, "a", "A")], "error", [candidate(2, 1, "a", "A")]]))
        not_submitted = result_from_events([[candidate(0, 1, "a", "A")], "not_submitted", [candidate(2, 1, "a", "A")]])
        missing = result_from_events([[candidate(0, 1, "a", "A")], "missing_raw", [candidate(2, 1, "a", "A")]])
        self.assertEqual(dict(error.gaps_by_type)["processing_error"], 1)
        # Hard gaps can explain an S6 split, but are not inherited as an
        # internal gap by either child appearance.
        self.assertEqual(sum(dict(item.gaps_by_type)["not_submitted"] for item in not_submitted.appearance_features), 0)
        self.assertEqual(sum(dict(item.gaps_by_type)["missing_raw"] for item in missing.appearance_features), 0)
        self.assertEqual(not_submitted.source_context.not_submitted_windows, 1)
        self.assertEqual(not_submitted.source_context.processing_error_windows, 0)
        self.assertEqual(missing.source_context.missing_raw_windows, 1)

    def test_singleton_scores_stay_descriptive(self):
        low = vector(result_from_events([[candidate(0, 1, "low", "Low", score=19)]]))
        high = vector(result_from_events([[candidate(0, 1, "high", "High", score=100)]]))
        self.assertTrue(low.singleton)
        self.assertTrue(high.singleton)
        self.assertEqual((low.top_score_summary.minimum, high.top_score_summary.maximum), (19, 100))
        self.assertIsNone(high.preferred_recording_identity_id)

    def test_identifier_churn_and_same_family_secondary_support(self):
        result = result_from_events([
            [candidate(0, 1, "a1", "Song", isrc="ISRC1"), candidate(0, 2, "a2", "Song", isrc="ISRC1")],
            [candidate(1, 1, "a2", "Song", isrc="ISRC2"), candidate(1, 2, "a1", "Song", isrc="ISRC2")],
        ])
        item = vector(result)
        self.assertEqual(item.distinct_acrid_count, 2)
        self.assertEqual(item.distinct_isrc_count, 2)
        self.assertEqual((item.acrid_switches, item.isrc_switches), (1, 1))
        self.assertGreaterEqual(item.same_family_secondary_windows, 2)
        self.assertEqual(item.conflict_count, 0)
        self.assertEqual(item.recording_alternatives_count, 2)

    def test_persistent_competitor_and_all_secondary_evidence_are_retained(self):
        weak = vector(result_from_events([[candidate(0, 1, "a", "A"), candidate(0, 2, "b", "B")]]))
        result = result_from_events([
            [candidate(0, 1, "a", "A"), candidate(0, 2, "b", "B", score=21)],
            [candidate(1, 1, "a", "A"), candidate(1, 2, "b", "B", score=22)],
        ])
        item = vector(result)
        self.assertEqual((weak.weak_alternative_count, weak.persistent_competitor_count), (1, 0))
        self.assertEqual(item.persistent_competitor_count, 2)
        self.assertEqual(item.conflict_count, 1)
        self.assertEqual(dict(item.conflicts_by_state)["competing"], 1)
        self.assertEqual(result.source_context.candidate_observation_count, 4)
        self.assertEqual(len(result.original.original.evidence), 4)

    def test_transition_reentry_and_boundary_features(self):
        result = result_from_events([
            [candidate(0, 1, "a", "A"), candidate(0, 2, "b", "B")],
            [candidate(1, 1, "a", "A"), candidate(1, 2, "b", "B")],
            *([None] * 13),
            [candidate(15, 1, "b", "B"), candidate(15, 2, "a", "A")],
            [candidate(16, 1, "b", "B"), candidate(16, 2, "a", "A")],
            *([None] * 13),
            [candidate(30, 1, "a", "A"), candidate(30, 2, "b", "B")],
            [candidate(31, 1, "a", "A"), candidate(31, 2, "b", "B")],
        ])
        self.assertEqual(len(result.appearance_features), 3)
        self.assertEqual(vector(result).transition_out_state, "directed")
        self.assertEqual(vector(result, 2).transition_in_state, "directed")
        self.assertTrue(vector(result, 2).reentry)
        self.assertGreater(vector(result).boundary_start_uncertainty_seconds, 0)
        self.assertGreater(vector(result).boundary_evidence_count, 0)

    def test_split_children_do_not_inherit_parent_gaps_scores_or_identifier_churn(self):
        result = result_from_events([
            [candidate(0, 1, "a1", "Song", isrc="ISRC-A", score=10, offset=1000)],
            "not_submitted",
            [candidate(2, 1, "a2", "Song", isrc="ISRC-B", score=90, offset=21000)],
            [candidate(3, 1, "a2", "Song", isrc="ISRC-B", score=80, offset=31000)],
        ])
        self.assertEqual(len(result.original.appearances), len(result.appearance_features))
        self.assertEqual(len(result.appearance_features), 2)
        first, second = vector(result), vector(result, 1)
        self.assertEqual((first.total_internal_gaps, second.total_internal_gaps), (0, 0))
        self.assertEqual((first.top_score_summary.maximum, second.top_score_summary.minimum), (10, 80))
        self.assertEqual((first.distinct_acrid_count, second.distinct_acrid_count), (1, 1))
        self.assertEqual((first.distinct_isrc_count, second.distinct_isrc_count), (1, 1))
        self.assertIsNone(first.play_offset_comparable_pairs)
        self.assertEqual(second.play_offset_comparable_pairs, 1)
        self.assertTrue(result.original.appearances[0].s4_family_occurrence_ids)
        self.assertEqual(local(result).top_observation_ids, result.original.appearances[0].supporting_observation_ids)

    def test_merged_appearance_uses_its_own_internal_gaps_and_local_conflicts(self):
        merged = result_from_events([
            [candidate(0, 1, "a", "Song")], None, None,
            [candidate(3, 1, "a", "Song")],
        ])
        item = vector(merged)
        self.assertEqual(len(merged.appearance_features), 1)
        self.assertEqual(item.total_internal_gaps, 2)
        self.assertEqual({gap.gap.window_index for gap in local(merged).gaps}, {1, 2})

        split = result_from_events([
            [candidate(0, 1, "a", "Song"), candidate(0, 2, "b", "Other")],
            [candidate(1, 1, "a", "Song"), candidate(1, 2, "b", "Other")],
            "not_submitted",
            [candidate(3, 1, "a", "Song")], [candidate(4, 1, "a", "Song")],
        ])
        self.assertEqual(len(split.appearance_features), 2)
        self.assertGreater(vector(split).conflict_count, 0)
        self.assertEqual(vector(split, 1).conflict_count, 0)
        self.assertTrue(local(split).conflict_ids)
        self.assertFalse(local(split, 1).conflict_ids)

    def test_local_feature_sets_are_deterministic_and_reconcile_to_local_refs(self):
        events = [
            [candidate(0, 1, "a", "Song", score=10), candidate(0, 2, "a2", "Song", score=20)],
            [candidate(1, 1, "a", "Song", score=30), candidate(1, 2, "a2", "Song", score=40)],
        ]
        first, second = result_from_events(events), result_from_events(events)
        self.assertEqual(first.to_dict(), second.to_dict())
        item, evidence = vector(first), local(first)
        self.assertEqual(item.top_score_summary.count, len(evidence.top_observation_ids))
        self.assertEqual(item.total_supporting_observations, len(evidence.top_observation_ids) + len(evidence.same_family_secondary_observation_ids))
        self.assertEqual(item.distinct_supporting_windows, len(set(evidence.supporting_window_ids)))

    def test_play_offset_is_descriptive_and_unknown_remains_unknown(self):
        coherent = vector(result_from_events([
            [candidate(0, 1, "a", "A", offset=1000)],
            [candidate(1, 1, "a", "A", offset=11000)],
        ]))
        jump = vector(result_from_events([
            [candidate(0, 1, "a", "A", offset=1000)],
            [candidate(1, 1, "a", "A", offset=41000)],
        ]))
        unknown = vector(result_from_events([[candidate(0, 1, "a", "A", isrc=None)]]))
        self.assertEqual((coherent.play_offset_comparable_pairs, coherent.play_offset_coherent_pairs), (1, 1))
        self.assertEqual((jump.play_offset_comparable_pairs, jump.play_offset_discontinuity_pairs), (1, 1))
        self.assertIsNone(unknown.play_offset_comparable_pairs)
        self.assertEqual(unknown.unknown_isrc_count, 1)

    def test_statistics_and_repeated_processing_are_deterministic(self):
        summary = score_summary([1, 2, 3, 4])
        self.assertEqual((summary.first_quartile, summary.median, summary.third_quartile), (1.75, 2.5, 3.25))
        events = [[candidate(0, 1, "a", "A", score=1)], [candidate(1, 1, "a", "A", score=4)]]
        self.assertEqual(result_from_events(events).to_dict(), result_from_events(events).to_dict())

    def test_read_only_replay_all_runs_conserves_evidence_and_is_deterministic(self):
        results = []
        for path in sorted(item for item in RAW_ROOT.iterdir() if item.is_dir()):
            canonical = load_run(path)
            first = extract_features(aggregate_transitions(aggregate_secondary_evidence(
                aggregate_families(aggregate_temporally(normalize_run(canonical)))
            )))
            second = extract_features(aggregate_transitions(aggregate_secondary_evidence(
                aggregate_families(aggregate_temporally(normalize_run(load_run(path))))
            )))
            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertEqual(first.source_context.candidate_observation_count, sum(len(window.candidates) for window in canonical.windows))
            self.assertEqual(len(first.original.original.evidence), first.source_context.candidate_observation_count)
            results.append(first)
        self.assertEqual(len(results), 10)
        self.assertEqual(sum(item.source_context.candidate_observation_count for item in results), 3777)


if __name__ == "__main__":
    unittest.main()
