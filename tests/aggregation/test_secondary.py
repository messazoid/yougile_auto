import unittest
from pathlib import Path

from offline.aggregation.archive_adapter import load_run
from src.aggregation import (
    CandidateObservation,
    CanonicalRecognitionRun,
    ConflictKind,
    OFFLINE_AGGREGATION_EXPERIMENTAL_V1,
    RawProvenancePointer,
    RecognitionWindow,
    RecognitionWindowStatus,
    ResolutionState,
    SecondaryEvidenceKind,
    aggregate_families,
    aggregate_secondary_evidence,
    aggregate_temporally,
    normalize_run,
)


RAW_ROOT = Path(__file__).parents[2] / "offline" / "fixtures" / "raw"


def candidate(index, rank, acrid, title, artists=("Artist",), isrc=None, score=50):
    return CandidateObservation(
        rank=rank, score=score, acrid=acrid, title=title, artists=artists, isrc=isrc,
        raw_provenance=RawProvenancePointer("test", f"candidate-{index}-{rank}"),
    )


def result_from_windows(specifications):
    windows = []
    for index, candidates in enumerate(specifications):
        windows.append(RecognitionWindow(
            index=index, source_start=index * 10, source_end=index * 10 + 10, actual_duration=10,
            status=RecognitionWindowStatus.CANDIDATES_RETURNED, candidates=tuple(candidates),
            raw_provenance=RawProvenancePointer("test", f"window-{index}"),
        ))
    canonical = CanonicalRecognitionRun(
        run_id="secondary", source_id="source", source_duration=len(windows) * 10,
        window_size=10, step=10, windows=tuple(windows),
        source_provenance=RawProvenancePointer("test", "source"), source_raw_values={"logical_sources": []},
    )
    return aggregate_secondary_evidence(aggregate_families(aggregate_temporally(normalize_run(canonical))))


def pair(index, top_acrid="top", secondary_acrid="secondary", *, top_title="Top", secondary_title="Secondary", top_artists=("Top Artist",), secondary_artists=("Secondary Artist",)):
    return [
        candidate(index, 1, top_acrid, top_title, top_artists),
        candidate(index, 2, secondary_acrid, secondary_title, secondary_artists),
    ]


def provenance_variant(namespace):
    """Same observations, intentionally different opaque provenance identities."""
    windows = []
    for index in range(2):
        candidates = (
            CandidateObservation(1, 50, acrid="top", title="Top", artists=("Top",), raw_provenance=RawProvenancePointer(namespace, f"top-{index}")),
            CandidateObservation(2, 50, acrid="secondary-b", title="Secondary B", artists=("B",), raw_provenance=RawProvenancePointer(namespace, f"b-{index}")),
            CandidateObservation(3, 50, acrid="secondary-c", title="Secondary C", artists=("C",), raw_provenance=RawProvenancePointer(namespace, f"c-{index}")),
        )
        windows.append(RecognitionWindow(
            index=index, source_start=index * 10, source_end=index * 10 + 10,
            actual_duration=10, status=RecognitionWindowStatus.CANDIDATES_RETURNED,
            candidates=candidates, raw_provenance=RawProvenancePointer(namespace, f"window-{index}"),
        ))
    run = CanonicalRecognitionRun(
        run_id="secondary", source_id="source", source_duration=20, window_size=10, step=10,
        windows=tuple(windows), source_provenance=RawProvenancePointer(namespace, "source"),
    )
    return aggregate_secondary_evidence(aggregate_families(aggregate_temporally(normalize_run(run))))


def semantic_conflicts(result):
    observations = {
        item.observation_id: (window.original.index, item.original.rank, item.comparison.acrid)
        for window in result.original.original.original.windows for item in window.candidates
    }
    return [
        (item.kind.value, item.resolution_state.value, item.source_start, item.source_end,
         item.window_indices, tuple(observations[observation_id] for observation_id in item.observation_ids))
        for item in result.conflicts
    ]


class SecondaryEvidenceTests(unittest.TestCase):
    def test_same_family_secondary_is_support_not_conflict(self):
        result = result_from_windows([[
            candidate(0, 1, "one", "Song", ("Artist",)),
            candidate(0, 2, "two", "Song", ("Artist",)),
        ]])
        self.assertIn(SecondaryEvidenceKind.SAME_FAMILY_SECONDARY, [item.kind for item in result.evidence])
        self.assertFalse(result.conflicts)

    def test_single_incompatible_secondary_is_weak_alternative(self):
        result = result_from_windows([pair(0)])
        self.assertIn(SecondaryEvidenceKind.WEAK_ALTERNATIVE, [item.kind for item in result.evidence])
        self.assertFalse(result.conflicts)

    def test_repeated_incompatible_secondary_becomes_persistent_competitor(self):
        result = result_from_windows([pair(0), pair(1)])
        self.assertEqual(sum(item.kind is SecondaryEvidenceKind.PERSISTENT_COMPETITOR for item in result.evidence), 2)
        self.assertEqual(result.conflicts[0].kind, ConflictKind.RANK_COMPETITION)
        self.assertEqual(result.conflicts[0].resolution_state, ResolutionState.COMPETING)

    def test_short_interruption_preserves_persistence(self):
        result = result_from_windows([pair(0), [candidate(1, 1, "top", "Top", ("Top Artist",))], pair(2)])
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].persistence_support_count, 2)

    def test_same_family_rank_switching_has_no_family_conflict(self):
        result = result_from_windows([
            [candidate(0, 1, "one", "Song", ("Artist",)), candidate(0, 2, "two", "Song", ("Artist",))],
            [candidate(1, 1, "two", "Song", ("Artist",)), candidate(1, 2, "one", "Song", ("Artist",))],
        ])
        self.assertFalse(result.conflicts)
        self.assertTrue(all(item.kind in {SecondaryEvidenceKind.DOMINANT_SUPPORT, SecondaryEvidenceKind.SAME_FAMILY_SECONDARY} for item in result.evidence))

    def test_repeated_rank_swaps_are_competing_without_overlap_claim(self):
        result = result_from_windows([
            pair(0, "a", "b", top_title="A", secondary_title="B"),
            pair(1, "b", "a", top_title="B", secondary_title="A"),
            pair(2, "a", "b", top_title="A", secondary_title="B"),
            pair(3, "b", "a", top_title="B", secondary_title="A"),
        ])
        self.assertTrue(result.conflicts)
        self.assertTrue(all(item.resolution_state is ResolutionState.COMPETING for item in result.conflicts))
        self.assertFalse(any(item.resolution_state is ResolutionState.POSSIBLE_OVERLAP for item in result.conflicts))

    def test_same_title_different_artist_uses_typed_conflict(self):
        result = result_from_windows([
            pair(0, top_title="Song", secondary_title="Song", top_artists=("A",), secondary_artists=("B",)),
            pair(1, top_title="Song", secondary_title="Song", top_artists=("A",), secondary_artists=("B",)),
        ])
        self.assertEqual(result.conflicts[0].kind, ConflictKind.SAME_TITLE_DIFFERENT_ARTIST)

    def test_all_evidence_is_accounted_and_deterministic(self):
        first = result_from_windows([pair(0), pair(1)])
        second = result_from_windows([pair(0), pair(1)])
        original_ids = {item.observation_id for window in first.original.original.original.windows for item in window.candidates}
        evidence_ids = {item.observation_id for item in first.evidence}
        self.assertEqual(original_ids, evidence_ids)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_provenance_does_not_choose_secondary_or_conflict_order(self):
        left, right = provenance_variant("offline-opaque"), provenance_variant("production-opaque")
        self.assertNotEqual(
            {item.observation_id for item in left.evidence},
            {item.observation_id for item in right.evidence},
        )
        self.assertEqual(semantic_conflicts(left), semantic_conflicts(right))
        self.assertEqual(
            [
                (item.window_index, item.rank, item.family_id, item.kind.value)
                for item in left.evidence
            ],
            [
                (item.window_index, item.rank, item.family_id, item.kind.value)
                for item in right.evidence
            ],
        )
        persistent = lambda result: {
            (item.window_index, item.rank, item.family_id)
            for item in result.evidence if item.kind is SecondaryEvidenceKind.PERSISTENT_COMPETITOR
        }
        self.assertEqual(persistent(left), persistent(right))
        self.assertEqual(len(left.conflicts), 2)

    def test_real_regression_gates_and_s4_regressions(self):
        r06 = aggregate_secondary_evidence(aggregate_families(aggregate_temporally(normalize_run(load_run(RAW_ROOT / "655d450a3d4b72fedf56f62d-ff355492aafb6552")))))
        r07 = aggregate_secondary_evidence(aggregate_families(aggregate_temporally(normalize_run(load_run(RAW_ROOT / "b170dcff0641c05ec9137670-0e58820e96b73825")))))
        def evidence_for_title(result, title):
            observations = {item.observation_id: item for window in result.original.original.original.windows for item in window.candidates}
            return [item for item in result.evidence if observations[item.observation_id].comparison.title == title]
        permanent = evidence_for_title(r06, "permanent")
        wants = evidence_for_title(r06, "wants and needs")
        ne_way = evidence_for_title(r07, "n e way")
        do_it = evidence_for_title(r07, "do it any way you wanna")
        self.assertTrue(any(item.kind is SecondaryEvidenceKind.PERSISTENT_COMPETITOR for item in permanent))
        self.assertTrue(any(item.kind is SecondaryEvidenceKind.DOMINANT_SUPPORT for item in wants))
        self.assertTrue(any(item.kind is SecondaryEvidenceKind.PERSISTENT_COMPETITOR for item in do_it))
        self.assertTrue(any(item.kind is SecondaryEvidenceKind.DOMINANT_SUPPORT for item in ne_way))
        self.assertTrue(r06.conflicts and r07.conflicts)
        self.assertFalse(any(item.resolution_state is ResolutionState.POSSIBLE_OVERLAP for item in r06.conflicts + r07.conflicts))
        self.assertEqual(len({item.observation_id for item in r06.evidence}), sum(len(window.candidates) for window in r06.original.original.original.windows))


if __name__ == "__main__":
    unittest.main()
