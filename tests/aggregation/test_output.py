import unittest
from dataclasses import replace
from pathlib import Path

from offline.aggregation.archive_adapter import load_run
from src.aggregation import (
    ValidationIssueCode,
    aggregate_families,
    aggregate_secondary_evidence,
    aggregate_temporally,
    aggregate_transitions,
    build_aggregated_result,
    extract_features,
    normalize_run,
    serialize_aggregated_result,
    SecondaryEvidenceKind,
    validate_aggregated_result,
)
from tests.aggregation.test_features import candidate, result_from_events


RAW_ROOT = Path(__file__).parents[2] / "offline" / "fixtures" / "raw"


def output_from_events(events):
    return build_aggregated_result(result_from_events(events))


def output_from_run(path):
    canonical = load_run(path)
    return build_aggregated_result(extract_features(aggregate_transitions(aggregate_secondary_evidence(
        aggregate_families(aggregate_temporally(normalize_run(canonical)))
    ))))


def output_with_normalized(path):
    canonical = load_run(path)
    normalized = normalize_run(canonical)
    result = build_aggregated_result(extract_features(aggregate_transitions(aggregate_secondary_evidence(
        aggregate_families(aggregate_temporally(normalized))
    ))))
    return result, {item.observation_id: item for window in normalized.windows for item in window.candidates}


class AggregatedOutputTests(unittest.TestCase):
    def test_complete_result_is_valid_deterministic_and_reference_free(self):
        events = [[candidate(0, 1, "a", "A")], [candidate(1, 1, "a", "A")]]
        first, second = output_from_events(events), output_from_events(events)
        report = validate_aggregated_result(first)
        self.assertTrue(report.valid, report.to_dict())
        self.assertEqual(serialize_aggregated_result(first), serialize_aggregated_result(second))
        payload = serialize_aggregated_result(first)
        self.assertIn('"contract_version": "aggregated-result/v2"', payload)
        self.assertIn('"engine_version": "aggregation-engine/v2"', payload)
        self.assertNotIn("reference", payload.casefold())

    def test_evidence_catalog_provenance_and_accounting_are_preserved(self):
        result = output_from_events([
            [candidate(0, 1, "a", "A"), candidate(0, 2, "b", "B")],
            [candidate(1, 1, "a", "A"), candidate(1, 2, "b", "B")],
        ])
        self.assertTrue(result.source_provenance)
        self.assertEqual(result.evidence_accounting.canonical_observation_count, 4)
        self.assertEqual(result.evidence_accounting.accounted_observation_count, 4)
        self.assertTrue(all(item.raw_provenance for item in result.observations))
        self.assertTrue(result.appearances[0].persistent_competitor_observation_ids)
        self.assertTrue(result.appearances[0].conflict_ids)
        self.assertTrue(validate_aggregated_result(result).valid)

    def test_singleton_ambiguity_and_not_calibrated_confidence_are_preserved(self):
        singleton = output_from_events([[candidate(0, 1, "single", "Single", score=100)]])
        ambiguous = output_from_events([
            [candidate(0, 1, "a1", "Song")], [candidate(1, 1, "a2", "Song")],
        ])
        item = singleton.appearances[0]
        self.assertTrue(item.feature_vector.singleton)
        self.assertEqual(item.confidence.status.value, "not_calibrated")
        self.assertIsNone(item.preferred_recording_identity_id)
        self.assertIn("ambiguous_recording", ambiguous.appearances[0].family_ambiguity_flags)
        self.assertEqual(len(ambiguous.appearances[0].recording_alternatives), 2)

    def test_transition_and_repeated_appearances_are_preserved(self):
        result = output_from_events([
            [candidate(0, 1, "a", "A"), candidate(0, 2, "b", "B")],
            [candidate(1, 1, "a", "A"), candidate(1, 2, "b", "B")],
            *([None] * 13),
            [candidate(15, 1, "b", "B"), candidate(15, 2, "a", "A")],
            [candidate(16, 1, "b", "B"), candidate(16, 2, "a", "A")],
            *([None] * 13),
            [candidate(30, 1, "a", "A"), candidate(30, 2, "b", "B")],
            [candidate(31, 1, "a", "A"), candidate(31, 2, "b", "B")],
        ])
        self.assertEqual(len(result.appearances), 3)
        self.assertEqual(len(result.transitions), 2)
        self.assertTrue(result.appearances[2].reentry)
        self.assertTrue(result.appearances[0].transition_out_id)
        self.assertTrue(result.appearances[2].transition_in_id)
        self.assertNotEqual(result.appearances[0].appearance_id, result.appearances[2].appearance_id)
        self.assertEqual(len(result.appearance_splits), 2)
        self.assertTrue(all(item.profile_name == "offline-experimental-v1" for item in result.appearance_splits))
        self.assertTrue(all(item.supporting_window_ids for item in result.appearance_splits))

    def test_validator_reports_dangling_duplicate_chronology_and_reference_leak(self):
        result = output_from_events([
            [candidate(0, 1, "a", "A")], [candidate(1, 1, "b", "B")],
        ])
        dangling = replace(result.appearances[0], supporting_observation_ids=("missing-observation",))
        duplicate = replace(result, appearances=(result.appearances[0], result.appearances[0]))
        reordered = replace(result, appearances=tuple(reversed(result.appearances)))
        leaked = replace(result, source_mapping={"reference_tracks": []})
        local_bad = replace(result.appearances[0], feature_vector=replace(result.appearances[0].feature_vector, total_internal_gaps=99))
        self.assertIn(ValidationIssueCode.DANGLING_REFERENCE, {item.code for item in validate_aggregated_result(replace(result, appearances=(dangling,))).issues})
        self.assertIn(ValidationIssueCode.DUPLICATE_ID, {item.code for item in validate_aggregated_result(duplicate).issues})
        self.assertIn(ValidationIssueCode.CHRONOLOGY_VIOLATION, {item.code for item in validate_aggregated_result(reordered).issues})
        self.assertIn(ValidationIssueCode.REFERENCE_DATA_LEAK, {item.code for item in validate_aggregated_result(leaked).issues})
        self.assertIn(ValidationIssueCode.LOCAL_EVIDENCE_VIOLATION, {item.code for item in validate_aggregated_result(replace(result, appearances=(local_bad,))).issues})

    def test_read_only_full_replay_all_runs_is_valid_and_deterministic(self):
        results = []
        for path in sorted(item for item in RAW_ROOT.iterdir() if item.is_dir()):
            first, second = output_from_run(path), output_from_run(path)
            report = validate_aggregated_result(first)
            self.assertTrue(report.valid, report.to_dict())
            self.assertEqual(serialize_aggregated_result(first), serialize_aggregated_result(second))
            self.assertEqual(first.evidence_accounting.canonical_observation_count, first.source_feature_context.candidate_observation_count)
            self.assertEqual(first.evidence_accounting.accounted_observation_count, first.source_feature_context.candidate_observation_count)
            results.append(first)
        self.assertEqual(len(results), 10)
        self.assertEqual(sum(item.evidence_accounting.accounted_observation_count for item in results), 3777)

    def test_real_structural_regression_gates_survive_final_output(self):
        r02, touch_obs = output_with_normalized(RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465")
        r03, prey_obs = output_with_normalized(RAW_ROOT / "255989015b81f1b37dac84e1-c92a6cb78f1842e7")
        r05, mind_obs = output_with_normalized(RAW_ROOT / "72419c84ff3af62edba97588-2aa5547900c42d04")
        r06, permanent_obs = output_with_normalized(RAW_ROOT / "655d450a3d4b72fedf56f62d-ff355492aafb6552")
        r07, neway_obs = output_with_normalized(RAW_ROOT / "b170dcff0641c05ec9137670-0e58820e96b73825")
        r08, selby_obs = output_with_normalized(RAW_ROOT / "8697845f62539aeda0cce0c0-16a928393d7a3ebc")
        r01_r09, shared_obs = output_with_normalized(RAW_ROOT / "cce58d848c724a753a7c1314-dc50dd1106209666")

        def family_ids_for_title(result, observations, title):
            identities = {item.recording_identity_id for item in observations.values() if item.comparison.title == title}
            return {family.family_id for family in result.track_families if identities & set(family.member_recording_identity_ids)}

        def appearances_for_title(result, observations, title):
            observation_ids = {item.observation_id for item in observations.values() if item.comparison.title == title}
            return [item for item in result.appearances if observation_ids & set(item.supporting_observation_ids)]

        regret = appearances_for_title(r01_r09, shared_obs, "regret")
        touch_ids = {item.observation_id for item in touch_obs.values() if item.comparison.title == "touch"}
        touch = [item for item in r02.exact_occurrences if touch_ids & {support.observation_id for support in item.supports}]
        touch_families = family_ids_for_title(r02, touch_obs, "touch")
        touch_final = [item for item in r02.appearances if item.family_id in touch_families]
        selby = appearances_for_title(r08, selby_obs, "de selby (part 2)")
        heaven = appearances_for_title(r01_r09, shared_obs, "heaven or las vegas")
        self.assertEqual([len(item.supporting_observation_ids) for item in regret], [24])
        self.assertEqual([len(item.gaps) for item in touch], [9])
        self.assertTrue(all(gap.bridged for gap in touch[0].gaps))
        self.assertEqual(len(touch_final), 1)  # final S8 cardinality; no reference input is involved
        self.assertTrue(selby[0].feature_vector.singleton and heaven[0].feature_vector.singleton)
        self.assertTrue(all(item.confidence.status.value == "not_calibrated" for item in selby + heaven))
        prey_families = family_ids_for_title(r03, prey_obs, "the prey (mind against remix)")
        pretty_families = family_ids_for_title(r03, prey_obs, "pretty eyes")
        self.assertEqual(len(prey_families), 1)
        self.assertEqual(len([item for item in r03.family_occurrences if item.family_id in pretty_families]), 1)
        pretty_family = next(item for item in r03.track_families if item.family_id in pretty_families)
        prey_appearances = [item for item in r03.appearances if item.family_id in prey_families]
        pretty_appearances = [item for item in r03.appearances if item.family_id in pretty_families]
        self.assertEqual(len(prey_appearances), 1)
        self.assertEqual(len(pretty_appearances), 1)
        self.assertFalse(pretty_appearances[0].reentry)
        self.assertIn("ambiguous_recording", pretty_family.ambiguity_flags)
        self.assertTrue(any(len(item.recording_alternatives) > 1 for item in pretty_appearances))
        self.assertGreater(len(family_ids_for_title(r05, mind_obs, "on my mind")), 1)
        for result, observations, title in ((r06, permanent_obs, "permanent"), (r07, neway_obs, "do it any way you wanna")):
            ids = {item.observation_id for item in observations.values() if item.comparison.title == title}
            self.assertTrue(any(item.observation_id in ids and item.kind is SecondaryEvidenceKind.PERSISTENT_COMPETITOR for item in result.evidence))
            self.assertTrue(result.conflicts)


if __name__ == "__main__":
    unittest.main()
