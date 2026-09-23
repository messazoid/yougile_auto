import unittest
from pathlib import Path

from offline.aggregation.archive_adapter import load_run
from src.aggregation import (
    CandidateObservation,
    CanonicalRecognitionRun,
    FinalizationReason,
    GapKind,
    OFFLINE_EXPERIMENTAL_V1,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
    aggregate_temporally,
    normalize_run,
)


RAW_ROOT = Path(__file__).parents[2] / "offline" / "fixtures" / "raw"


def run_from_events(events):
    windows = []
    for index, event in enumerate(events):
        start = index * 10
        if isinstance(event, tuple):
            acrid, score, rank = event
            candidate = CandidateObservation(
                rank=rank, score=score, acrid=acrid, title="same title", artists=("artist",),
                play_offset=index, raw_provenance=RawProvenancePointer("test", f"candidate-{index}-{rank}"),
            )
            status, candidates = RecognitionWindowStatus.CANDIDATES_RETURNED, (candidate,)
        elif event == "lower":
            candidate = CandidateObservation(
                rank=1, score=50, acrid=None, title="unknown", raw_provenance=RawProvenancePointer("test", f"unknown-{index}"),
            )
            lower = CandidateObservation(
                rank=2, score=50, acrid="lower-only", raw_provenance=RawProvenancePointer("test", f"lower-{index}"),
            )
            status, candidates = RecognitionWindowStatus.CANDIDATES_RETURNED, (candidate, lower)
        else:
            status, candidates = RecognitionWindowStatus(event), ()
        windows.append(RecognitionWindow(
            index=index, source_start=start, source_end=start + 10, actual_duration=10,
            status=status, candidates=candidates, raw_provenance=RawProvenancePointer("test", f"window-{index}"),
        ))
    return CanonicalRecognitionRun(
        run_id="test-run", source_id="source", source_duration=len(windows) * 10,
        window_size=10, step=10, windows=tuple(windows),
        source_provenance=RawProvenancePointer("test", "source"), source_raw_values={"logical_sources": []},
    )


def temporal(events):
    return aggregate_temporally(normalize_run(run_from_events(events)))


class TemporalAggregationTests(unittest.TestCase):
    def test_contiguous_same_acrid_is_one_occurrence_with_source_end_finalization(self):
        result = temporal([("a", 20, 1), ("a", 30, 1), ("a", 40, 1)])
        occurrence = result.occurrences[0]
        self.assertEqual(len(result.occurrences), 1)
        self.assertEqual(len(occurrence.supports), 3)
        self.assertEqual(occurrence.finalization_reason, FinalizationReason.SOURCE_END)
        self.assertFalse(occurrence.singleton)

    def test_bounded_1001_gap_is_preserved_and_bridged(self):
        result = temporal([("a", 20, 1)] + ["no_result_1001"] * OFFLINE_EXPERIMENTAL_V1.max_no_result_bridge_windows + [("a", 20, 1)])
        occurrence = result.occurrences[0]
        self.assertEqual(len(result.occurrences), 1)
        self.assertEqual(len(occurrence.gaps), OFFLINE_EXPERIMENTAL_V1.max_no_result_bridge_windows)
        self.assertTrue(all(item.kind is GapKind.NO_RESULT_1001 and item.bridged for item in occurrence.gaps))

    def test_too_long_1001_gap_splits_occurrence(self):
        result = temporal([("a", 20, 1)] + ["no_result_1001"] * (OFFLINE_EXPERIMENTAL_V1.max_no_result_bridge_windows + 1) + [("a", 20, 1)])
        self.assertEqual(len(result.occurrences), 2)
        self.assertEqual(result.occurrences[0].finalization_reason, FinalizationReason.NO_RESULT_HORIZON)
        self.assertFalse(any(item.bridged for item in result.occurrences[0].gaps))

    def test_processing_error_is_typed_and_can_bridge_without_negative_evidence(self):
        result = temporal([("a", 20, 1), "processing_error", ("a", 20, 1)])
        self.assertEqual(len(result.occurrences), 1)
        self.assertEqual(result.occurrences[0].gaps[0].kind, GapKind.PROCESSING_ERROR)
        self.assertTrue(result.occurrences[0].gaps[0].bridged)

    def test_not_submitted_is_typed_and_not_1001(self):
        result = temporal([("a", 20, 1), "not_submitted", ("a", 20, 1)])
        self.assertEqual(len(result.occurrences), 2)
        self.assertEqual(result.occurrences[0].finalization_reason, FinalizationReason.NOT_SUBMITTED)
        self.assertEqual(result.occurrences[0].gaps[0].kind, GapKind.NOT_SUBMITTED)

    def test_missing_raw_is_typed_separately(self):
        result = temporal([("a", 20, 1), "missing_raw", ("a", 20, 1)])
        self.assertEqual(len(result.occurrences), 2)
        self.assertEqual(result.occurrences[0].finalization_reason, FinalizationReason.MISSING_RAW)
        self.assertEqual(result.occurrences[0].gaps[0].kind, GapKind.MISSING_RAW)

    def test_different_acrids_never_merge_and_lower_rank_does_not_open_occurrence(self):
        result = temporal([("a", 20, 1), ("b", 20, 1), "lower"])
        self.assertEqual([item.recording_identity_id for item in result.occurrences], [
            normalize_run(run_from_events([("a", 20, 1)])).windows[0].candidates[0].recording_identity_id,
            normalize_run(run_from_events([("b", 20, 1)])).windows[0].candidates[0].recording_identity_id,
        ])
        self.assertEqual(len(result.occurrences), 2)

    def test_low_and_high_score_singletons_are_retained_without_confirmation(self):
        for score in (19, 100):
            occurrence = temporal([("a", score, 1)]).occurrences[0]
            self.assertTrue(occurrence.singleton)
            self.assertEqual(occurrence.supports[0].score, score)
            self.assertEqual(occurrence.confidence_state, "not_calibrated")

    def test_ids_provenance_and_repeated_processing_are_deterministic(self):
        source = normalize_run(run_from_events([("a", 20, 1), "no_result_1001", ("a", 20, 1)]))
        first, second = aggregate_temporally(source), aggregate_temporally(source)
        support = first.occurrences[0].supports[0]
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(support.window_index, 0)
        self.assertEqual(support.play_offset, 0)
        self.assertEqual(support.raw_provenance.record_id, "candidate-0-1")
        self.assertEqual(first.occurrences[0].source_provenance.record_id, "source")

    def test_real_regression_gates(self):
        r02 = aggregate_temporally(normalize_run(load_run(RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465")))
        r01_r09 = aggregate_temporally(normalize_run(load_run(RAW_ROOT / "cce58d848c724a753a7c1314-dc50dd1106209666")))
        r08 = aggregate_temporally(normalize_run(load_run(RAW_ROOT / "8697845f62539aeda0cce0c0-16a928393d7a3ebc")))
        def by_title(result, title):
            items = {item.observation_id: item for frame in result.original.windows for item in frame.candidates}
            return [occurrence for occurrence in result.occurrences if any(items[support.observation_id].comparison.title == title for support in occurrence.supports)]
        regret = by_title(r01_r09, "regret")
        touch = by_title(r02, "touch")
        de_selby = by_title(r08, "de selby (part 2)")
        heaven = by_title(r01_r09, "heaven or las vegas")
        self.assertEqual([len(item.supports) for item in regret], [24])
        self.assertEqual(regret[0].gaps, ())
        self.assertEqual([len(item.supports) for item in touch], [6])
        self.assertEqual(len(touch[0].gaps), 9)
        self.assertTrue(all(item.bridged for item in touch[0].gaps))
        self.assertEqual([item.singleton for item in de_selby], [True])
        self.assertEqual([item.supports[0].score for item in de_selby], [19])
        self.assertEqual([item.singleton for item in heaven], [True])
        self.assertEqual([item.supports[0].score for item in heaven], [100])
        self.assertEqual(heaven[0].confidence_state, "not_calibrated")


if __name__ == "__main__":
    unittest.main()
