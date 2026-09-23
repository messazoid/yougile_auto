import unittest
from pathlib import Path

from offline.aggregation.archive_adapter import load_run
from src.aggregation import (
    CandidateObservation,
    CanonicalRecognitionRun,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
    normalize_run,
)


RAW_ROOT = Path(__file__).parents[2] / "offline" / "fixtures" / "raw"


def observation(rank, acrid, **changes):
    values = {
        "rank": rank, "score": 90, "acrid": acrid, "isrc": "US-ABC-12-34567",
        "title": "  CAFE\u0301 SONG (Radio Edit) ", "artists": ("  THE  ARTIST ",),
        "album": " Album ", "label": " Label ", "version": "Remastered",
        "play_offset": 321, "raw_provenance": RawProvenancePointer("test", f"candidate-{rank}"),
        "raw_values": {"duration_ms": 200000, "unmodeled": "kept"},
    }
    values.update(changes)
    return CandidateObservation(**values)


def canonical(*windows):
    return CanonicalRecognitionRun(
        run_id="run", source_id="source", source_duration=30, window_size=10, step=10,
        windows=tuple(windows), source_provenance=RawProvenancePointer("test", "source"),
        source_raw_values={"logical_sources": [{"items": ["kept"]}]},
    )


def window(index, candidates):
    return RecognitionWindow(
        index=index, source_start=index * 10, source_end=index * 10 + 10, actual_duration=10,
        status=RecognitionWindowStatus.CANDIDATES_RETURNED, candidates=tuple(candidates),
        raw_provenance=RawProvenancePointer("test", f"window-{index}"),
    )


class NormalizationTests(unittest.TestCase):
    def test_normalizes_comparison_values_but_preserves_original_raw_values(self):
        result = normalize_run(canonical(window(0, [observation(1, " ACR-ID ")])))
        item = result.windows[0].candidates[0]
        self.assertEqual(item.comparison.title, "café song (radio edit)")
        self.assertEqual(item.comparison.artists, ("the artist",))
        self.assertEqual(item.comparison.acrid, "acr-id")
        self.assertEqual(item.comparison.isrc, "USABC1234567")
        self.assertEqual(item.comparison.version_descriptors, ("radio_edit", "remaster"))
        self.assertEqual(item.original.title, "  CAFE\u0301 SONG (Radio Edit) ")
        self.assertEqual(item.original.raw_values["unmodeled"], "kept")

    def test_same_acrid_keeps_all_ranks_observations_and_creates_one_identity(self):
        result = normalize_run(canonical(
            window(0, [observation(1, "same"), observation(2, "same", raw_provenance=RawProvenancePointer("test", "candidate-2"))]),
            window(1, [observation(1, "same", raw_provenance=RawProvenancePointer("test", "candidate-3"))]),
        ))
        observations = [item for frame in result.windows for item in frame.candidates]
        self.assertEqual(len(observations), 3)
        self.assertEqual(len(result.recording_identities), 1)
        self.assertEqual({item.recording_identity_id for item in observations}, {result.recording_identities[0].identity_id})
        self.assertEqual([item.original.rank for item in result.windows[0].candidates], [1, 2])

    def test_no_cross_acrid_merge_and_missing_acrid_has_no_identity(self):
        result = normalize_run(canonical(window(0, [
            observation(1, "one"), observation(2, "two", title="  CAFE\u0301 SONG (Radio Edit) ", raw_provenance=RawProvenancePointer("test", "two")),
        ]), window(1, [observation(1, None, raw_provenance=RawProvenancePointer("test", "missing"), isrc=None, title=None, artists=(), album=None, label=None, version=None)])))
        all_items = [item for frame in result.windows for item in frame.candidates]
        self.assertEqual(len(result.recording_identities), 2)
        self.assertIsNone(all_items[-1].recording_identity_id)
        self.assertIsNone(all_items[-1].comparison.title)
        self.assertEqual(all_items[-1].comparison.artists, ())

    def test_conflicting_metadata_under_same_acrid_is_diagnostic_not_split(self):
        result = normalize_run(canonical(window(0, [observation(1, "same")]), window(1, [observation(
            1, "same", title="Different", raw_provenance=RawProvenancePointer("test", "different"),
            raw_values={"duration_ms": 201000},
        )])))
        fields = {item.field for item in result.diagnostics}
        self.assertEqual(len(result.recording_identities), 1)
        self.assertTrue({"title", "duration_ms"} <= fields)
        self.assertTrue(all(item.evidence_observation_ids for item in result.diagnostics))

    def test_deterministic_processing_preserves_window_play_offset_and_provenance(self):
        source = canonical(window(0, [observation(1, "same")]))
        first, second = normalize_run(source), normalize_run(source)
        item = first.windows[0].candidates[0]
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(item.window_index, 0)
        self.assertEqual(item.original.play_offset, 321)
        self.assertEqual(item.original.raw_provenance.record_id, "candidate-1")
        self.assertEqual(first.original.source_provenance.record_id, "source")

    def test_read_only_replay_all_archives_conserves_evidence_and_exact_identity_rules(self):
        for path in sorted(item for item in RAW_ROOT.iterdir() if item.is_dir()):
            canonical_run = load_run(path)
            first, second = normalize_run(canonical_run), normalize_run(canonical_run)
            original = [candidate for frame in canonical_run.windows for candidate in frame.candidates]
            normalized = [candidate for frame in first.windows for candidate in frame.candidates]
            identities = {item.identity_id: item.acrid for item in first.recording_identities}
            self.assertEqual(len(normalized), len(original), path.name)
            self.assertEqual(first.to_dict(), second.to_dict(), path.name)
            self.assertEqual([frame.original.index for frame in first.windows], [frame.index for frame in canonical_run.windows], path.name)
            self.assertEqual([item.original.rank for item in normalized], [item.rank for item in original], path.name)
            self.assertTrue(all(item.recording_identity_id in identities for item in normalized if item.comparison.acrid), path.name)
            self.assertTrue(all(item.recording_identity_id is None for item in normalized if not item.comparison.acrid), path.name)
            self.assertEqual(len(identities), len(set(identities.values())), path.name)


if __name__ == "__main__":
    unittest.main()
