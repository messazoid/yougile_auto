import unittest
from pathlib import Path

from offline.aggregation.archive_adapter import load_run
from src.aggregation import (
    CandidateObservation,
    CanonicalRecognitionRun,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
    OFFLINE_FAMILY_EXPERIMENTAL_V1,
    CALIBRATED_FAMILY_V2,
    RecordingRelationKind,
    aggregate_families,
    aggregate_temporally,
    normalize_run,
)


RAW_ROOT = Path(__file__).parents[2] / "offline" / "fixtures" / "raw"


def candidate(index, acrid, *, title="Song", artists=("Artist",), isrc="ISRC", version=None, offset=0):
    return CandidateObservation(
        rank=1, score=50, acrid=acrid, title=title, artists=artists, isrc=isrc,
        version=version, play_offset=offset,
        raw_provenance=RawProvenancePointer("test", f"candidate-{index}"),
        raw_values={"duration_ms": 100000},
    )


def family_result(specifications, profile=OFFLINE_FAMILY_EXPERIMENTAL_V1):
    windows = []
    for index, spec in enumerate(specifications):
        if spec is None:
            status, candidates = RecognitionWindowStatus.NO_RESULT_1001, ()
        else:
            status, candidates = RecognitionWindowStatus.CANDIDATES_RETURNED, (candidate(index, **spec),)
        windows.append(RecognitionWindow(
            index=index, source_start=index * 10, source_end=index * 10 + 10, actual_duration=10,
            status=status, candidates=candidates, raw_provenance=RawProvenancePointer("test", f"window-{index}"),
        ))
    source = CanonicalRecognitionRun(
        run_id="family-test", source_id="source", source_duration=len(windows) * 10,
        window_size=10, step=10, windows=tuple(windows),
        source_provenance=RawProvenancePointer("test", "source"), source_raw_values={"logical_sources": []},
    )
    return aggregate_families(aggregate_temporally(normalize_run(source)), profile)


class FamilyAggregationTests(unittest.TestCase):
    def test_compatible_same_isrc_creates_family_without_merging_exact_identities(self):
        result = family_result([
            {"acrid": "one", "isrc": "AA-111", "offset": 1},
            {"acrid": "two", "isrc": "AA111", "offset": 2},
        ])
        self.assertIn(RecordingRelationKind.SAME_ISRC_COMPATIBLE, [item.kind for item in result.relations])
        family = next(item for item in result.families if len(item.member_recording_identity_ids) == 2)
        self.assertIsNone(family.preferred_recording_identity_id)
        self.assertEqual(len(result.family_occurrences), 1)

    def test_same_isrc_with_incompatible_metadata_does_not_blindly_merge(self):
        result = family_result([
            {"acrid": "one", "title": "One", "artists": ("A",), "isrc": "AA111"},
            {"acrid": "two", "title": "Two", "artists": ("B",), "isrc": "AA111"},
        ])
        self.assertEqual(len(result.families), 2)
        self.assertEqual(result.relations[0].kind, RecordingRelationKind.INCOMPATIBLE)

    def test_same_title_and_artist_is_family_relation_but_not_exact_merge(self):
        result = family_result([{"acrid": "one", "isrc": None}, {"acrid": "two", "isrc": None}])
        self.assertIn(RecordingRelationKind.SAME_TITLE_ARTIST_COMPATIBLE, [item.kind for item in result.relations])
        self.assertEqual(len(result.families), 1)
        self.assertEqual(len(result.families[0].member_recording_identity_ids), 2)

    def test_same_title_different_artists_does_not_title_only_merge(self):
        result = family_result([
            {"acrid": "one", "artists": ("A",), "isrc": None},
            {"acrid": "two", "artists": ("B",), "isrc": None},
        ])
        self.assertEqual(len(result.families), 2)
        self.assertEqual(result.relations[0].kind, RecordingRelationKind.INCOMPATIBLE)

    def test_explicit_version_lineage_is_distinct_recordings_one_family_and_one_adjacent_occurrence(self):
        result = family_result([
            {"acrid": "one", "title": "Song (Remastered)", "version": "Remastered", "isrc": None},
            {"acrid": "two", "title": "Song (Radio Edit)", "version": "Radio Edit", "isrc": None},
        ])
        self.assertIn(RecordingRelationKind.VERSION_RELATED, [item.kind for item in result.relations])
        self.assertEqual(len(result.families), 1)
        self.assertEqual(len(result.family_occurrences), 1)
        self.assertEqual(len(result.family_occurrences[0].recording_identity_ids), 2)

    def test_remix_conflict_is_not_automatically_a_family(self):
        result = family_result([
            {"acrid": "one", "title": "Song", "version": None, "isrc": None},
            {"acrid": "two", "title": "Song (Remix)", "version": "Remix", "isrc": None},
        ])
        self.assertEqual(len(result.families), 2)
        self.assertEqual(result.relations[0].kind, RecordingRelationKind.INCOMPATIBLE)

    def test_calibrated_profile_keeps_differently_named_remixes_separate(self):
        result = family_result([
            {"acrid": "one", "title": "Song (Alpha Remix)", "version": "Alpha Remix", "isrc": None},
            {"acrid": "two", "title": "Song (Beta Remix)", "version": "Beta Remix", "isrc": None},
        ], CALIBRATED_FAMILY_V2)
        self.assertEqual(len(result.families), 2)
        self.assertEqual(result.relations[0].kind, RecordingRelationKind.INCOMPATIBLE)
        self.assertIn("version_signature_conflict", result.relations[0].evidence_fields)

    def test_calibrated_profile_merges_same_named_mix_across_acrids(self):
        result = family_result([
            {"acrid": "one", "title": "Song (UK Mix)", "version": "UK Mix", "isrc": None},
            {"acrid": "two", "title": "Song (UK Mix)", "version": "UK Mix", "isrc": None},
        ], CALIBRATED_FAMILY_V2)
        self.assertEqual(len(result.families), 1)
        self.assertEqual(result.families[0].preferred_core_title, "song")

    def test_calibrated_profile_strips_feature_credit_for_family_comparison(self):
        result = family_result([
            {"acrid": "one", "title": "Song (feat. Singer)", "artists": ("Artist", "Singer"), "isrc": None},
            {"acrid": "two", "title": "Song", "artists": ("Artist",), "isrc": None},
        ], CALIBRATED_FAMILY_V2)
        self.assertEqual(len(result.families), 1)

    def test_distant_appearances_of_one_family_remain_separate_and_offsets_do_not_decide_relation(self):
        result = family_result([
            {"acrid": "one", "offset": 1, "isrc": None}, {"acrid": "two", "offset": 999, "isrc": None},
            *([None] * OFFLINE_FAMILY_EXPERIMENTAL_V1.max_continuity_window_separation),
            {"acrid": "one", "offset": 3, "isrc": None},
        ])
        self.assertEqual(len(result.families), 1)
        self.assertEqual(len(result.family_occurrences), 2)
        self.assertIn(RecordingRelationKind.SAME_TITLE_ARTIST_COMPATIBLE, [item.kind for item in result.relations])

    def test_ids_and_evidence_provenance_are_deterministic(self):
        first = family_result([{"acrid": "one"}, {"acrid": "two"}])
        second = family_result([{"acrid": "one"}, {"acrid": "two"}])
        relation = first.relations[0]
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertTrue(relation.evidence_observation_ids)
        self.assertTrue(first.original.occurrences[0].supports[0].raw_provenance)

    def test_real_regression_gates(self):
        r03 = aggregate_families(aggregate_temporally(normalize_run(load_run(RAW_ROOT / "255989015b81f1b37dac84e1-c92a6cb78f1842e7"))))
        r05 = aggregate_families(aggregate_temporally(normalize_run(load_run(RAW_ROOT / "72419c84ff3af62edba97588-2aa5547900c42d04"))))
        def occurrences_for_title(result, title):
            observations = {item.observation_id: item for window in result.original.original.windows for item in window.candidates}
            return [item for item in result.original.occurrences if any(observations[support.observation_id].comparison.title == title for support in item.supports)]
        def observations_for_title(result, title):
            return [item for window in result.original.original.windows for item in window.candidates if title in (item.comparison.title or "")]
        prey = occurrences_for_title(r03, "the prey")
        pretty = occurrences_for_title(r03, "pretty eyes")
        on_my_mind = occurrences_for_title(r05, "on my mind")
        family_by_identity = {identity: family for family in r03.families for identity in family.member_recording_identity_ids}
        prey_families = {family_by_identity[item.recording_identity_id] for item in prey}
        self.assertEqual(len(prey_families), 1)
        self.assertEqual(len([item for item in r03.family_occurrences if item.family_id in {family.family_id for family in prey_families}]), 1)
        pretty_ids = {item.recording_identity_id for item in observations_for_title(r03, "pretty eyes")}
        pretty_families = {family_by_identity[item] for item in pretty_ids}
        self.assertEqual(len(pretty_families), 1)
        self.assertEqual(len(pretty_ids), 7)
        self.assertEqual(len({item.family_id for item in r03.family_occurrences if item.family_id in {family.family_id for family in pretty_families}}), 1)
        self.assertTrue(any("ambiguous_recording" in family.ambiguity_flags for family in pretty_families))
        self.assertTrue(all(family.preferred_recording_identity_id is None for family in pretty_families))
        r05_family_by_identity = {identity: family for family in r05.families for identity in family.member_recording_identity_ids}
        self.assertGreater(len({r05_family_by_identity[item.recording_identity_id].family_id for item in on_my_mind}), 1)


if __name__ == "__main__":
    unittest.main()
