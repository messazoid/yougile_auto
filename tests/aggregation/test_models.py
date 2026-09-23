import unittest

from src.aggregation import (
    CandidateObservation,
    CanonicalRecognitionRun,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
)


def candidate(rank=1, **kwargs):
    values = {
        "rank": rank, "score": 95, "acrid": "acr-1", "isrc": "ISRC-1", "title": "Track",
        "artists": ("Artist",),
        "raw_provenance": RawProvenancePointer("fixture", f"candidate-{rank}"),
        "raw_values": {"provider_extra": {"kept": True}},
    }
    values.update(kwargs)
    return CandidateObservation(**values)


def window(index, status, candidates=()):
    return RecognitionWindow(
        index=index, source_start=index * 10, source_end=index * 10 + 10,
        actual_duration=10, status=status, candidates=candidates,
        raw_provenance=RawProvenancePointer("fixture", f"window-{index}"),
        raw_values={"status": {"code": 0 if candidates else 1001}},
    )


class CanonicalModelsTests(unittest.TestCase):
    def test_candidate_window_preserves_observation_and_raw_values(self):
        item = window(0, RecognitionWindowStatus.CANDIDATES_RETURNED, (candidate(),))
        self.assertEqual(item.candidates[0].title, "Track")
        self.assertEqual(item.candidates[0].raw_values["provider_extra"]["kept"], True)
        self.assertEqual(item.raw_provenance.record_id, "window-0")

    def test_multiple_candidate_ranks_preserve_rank_order(self):
        first, second = candidate(1), candidate(2, title="Alternate", acrid="acr-2")
        item = window(0, RecognitionWindowStatus.CANDIDATES_RETURNED, (first, second))
        self.assertEqual([entry.rank for entry in item.candidates], [1, 2])
        self.assertEqual([entry.title for entry in item.candidates], ["Track", "Alternate"])

    def test_non_candidate_states_include_1001_processing_error_and_missing_raw(self):
        for status in (RecognitionWindowStatus.NO_RESULT_1001, RecognitionWindowStatus.PROCESSING_ERROR,
                       RecognitionWindowStatus.NOT_SUBMITTED, RecognitionWindowStatus.MISSING_RAW):
            self.assertEqual(window(0, status).status, status)

    def test_missing_optional_metadata_is_unknown_not_a_mismatch(self):
        item = candidate(acrid=None, isrc=None, title=None, artists=(), album=None, label=None, version=None)
        self.assertIsNone(item.isrc)
        self.assertEqual(item.artists, ())
        self.assertIsNone(item.album)

    def test_semantic_ids_are_deterministic(self):
        self.assertEqual(candidate().candidate_id, candidate().candidate_id)
        self.assertEqual(
            window(0, RecognitionWindowStatus.CANDIDATES_RETURNED, (candidate(),)).window_id,
            window(0, RecognitionWindowStatus.CANDIDATES_RETURNED, (candidate(),)).window_id,
        )

    def test_run_preserves_chronological_order_and_round_trips(self):
        run = CanonicalRecognitionRun(
            run_id="run-a", source_id="source-a", source_duration=20, window_size=10, step=10,
            windows=(
                window(0, RecognitionWindowStatus.CANDIDATES_RETURNED, (candidate(),)),
                window(1, RecognitionWindowStatus.NO_RESULT_1001),
            ),
        )
        restored = CanonicalRecognitionRun.from_dict(run.to_dict())
        self.assertEqual([item.index for item in restored.windows], [0, 1])
        self.assertEqual(restored.to_dict(), run.to_dict())


if __name__ == "__main__":
    unittest.main()
