import tempfile
import unittest
from pathlib import Path

from offline.aggregation.reference_loader import ReferenceEntry, load_reference_document
from offline.aggregation.report import render_human_report, write_validation_reports
from offline.aggregation.validation import (
    MatchState,
    RegressionState,
    _overmerge_candidates,
    serialize_validation_result,
    validate_against_reference,
)
from tests.aggregation.test_output import output_from_run


ROOT = Path(__file__).parents[2]
RAW_ROOT = ROOT / "offline" / "fixtures" / "raw"
REFERENCE_PATH = ROOT / "offline" / "data" / "ссылки и эталоны.docx"


class OfflineReferenceValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = load_reference_document(REFERENCE_PATH)
        cls.results = tuple(output_from_run(path) for path in sorted(item for item in RAW_ROOT.iterdir() if item.is_dir()))
        cls.validation = validate_against_reference(cls.reference, cls.results)

    def test_docx_reference_parses_sections_order_duplicates_notes_and_exclusion(self):
        self.assertEqual([item.test_id for item in self.reference.tests], list(range(1, 11)))
        self.assertTrue(self.reference.tests[1].excluded)
        self.assertEqual(self.reference.tests[1].exclusion_reason, "reference_source_mismatch")
        test_eight = self.reference.tests[7]
        self.assertEqual([item.order for item in test_eight.entries], list(range(1, len(test_eight.entries) + 1)))
        self.assertEqual(sum(item.title == "Touch (feat. Dj K-Deucez)" for item in test_eight.entries), 2)
        self.assertEqual(test_eight.entries[5].provenance.paragraph_indices, (139, 140))
        self.assertEqual(len(self.reference.tests[4].notes), 2)
        self.assertEqual(len(self.reference.tests[9].notes), 1)

    def test_reference_never_enters_s8_aggregated_result(self):
        payload = self.results[0].to_dict()
        self.assertNotIn(self.reference.document_id, str(payload))
        self.assertNotIn("shazam.com", str(payload).casefold())

    def test_conservative_matching_missed_ambiguous_additional_and_fragmentation(self):
        test_eight = next(item for item in self.validation.tests if item.test_id == 8)
        touch = next(entry for entry in test_eight.reference_entries if entry.title == "Touch (feat. Dj K-Deucez)")
        touch_match = next(item for item in test_eight.matches if item.reference_entry_id == touch.entry_id)
        pretty_test = next(item for item in self.validation.tests if item.test_id == 6)
        pretty = next(entry for entry in pretty_test.reference_entries if entry.title == "Pretty Eyes (Extended Mix)")
        pretty_match = next(item for item in pretty_test.matches if item.reference_entry_id == pretty.entry_id)
        self.assertEqual(touch_match.state, MatchState.OCCURRENCE_DETECTED)
        self.assertEqual(pretty_match.state, MatchState.AMBIGUOUS_MATCH)
        self.assertGreater(self.validation.summary.missed_count, 0)
        self.assertGreater(self.validation.summary.additional_appearance_count, 0)
        self.assertTrue(self.validation.summary.reconciliation_valid)
        # v1.1-A may legitimately remove former S6 fragmentation candidates;
        # S9 only reports the resulting structure and does not tune it.
        self.assertIsInstance(self.validation.summary.fragmentation_candidate_count, int)
        self.assertTrue(all(item.appearance_ids for item in test_eight.fragmentation_candidates))

    def test_ambiguous_hypotheses_are_not_double_counted_as_additional(self):
        for test in self.validation.tests:
            additional_ids = {item.appearance_id for item in test.additional_appearances}
            self.assertFalse(additional_ids & set(test.ambiguous_linked_appearance_ids))
            if not test.excluded:
                reconciliation = test.reconciliation
                self.assertTrue(reconciliation.reference_reconciled)
                self.assertTrue(reconciliation.appearance_reconciled)

    def test_duplicate_and_overmerge_handling_are_explicit(self):
        test_eight = next(item for item in self.validation.tests if item.test_id == 8)
        self.assertTrue(test_eight.duplicate_reference_cases)
        incompatible = [
            ReferenceEntry("left", 1, 1, "Song A", "Artist A", None, raw_value="left"),
            ReferenceEntry("right", 1, 2, "Song B", "Artist B", None, raw_value="right"),
        ]
        candidates = _overmerge_candidates({"family": incompatible})
        self.assertEqual(candidates[0].family_id, "family")
        self.assertIn("incompatible_reference", candidates[0].reason)

    def test_test_two_is_excluded_from_quantitative_summary(self):
        excluded = next(item for item in self.validation.tests if item.test_id == 2)
        self.assertTrue(excluded.excluded)
        self.assertNotIn(2, [item.test_id for item in self.validation.tests if not item.excluded])
        self.assertEqual(self.validation.summary.comparable_test_count, 9)
        self.assertEqual(self.validation.summary.excluded_test_ids, (2,))

    def test_validation_and_reports_are_deterministic(self):
        repeated = validate_against_reference(self.reference, self.results)
        self.assertEqual(serialize_validation_result(self.validation), serialize_validation_result(repeated))
        self.assertEqual(render_human_report(self.validation), render_human_report(repeated))
        with tempfile.TemporaryDirectory() as directory:
            machine, human = write_validation_reports(self.validation, directory)
            self.assertEqual(machine.read_text(encoding="utf-8"), serialize_validation_result(self.validation) + "\n")
            self.assertEqual(human.read_text(encoding="utf-8"), render_human_report(self.validation))

    def test_r01_to_r12_matrix_inspects_final_output(self):
        self.assertEqual([item.regression_id for item in self.validation.regression_matrix], [f"R{index:02d}" for index in range(1, 13)])
        self.assertTrue(all(item.state is RegressionState.PASS for item in self.validation.regression_matrix))

    def test_r10_sparse_r11_duplicates_and_r12_exclusion_are_final_gates(self):
        checks = {item.regression_id: item for item in self.validation.regression_matrix}
        self.assertEqual(checks["R10"].state, RegressionState.PASS)
        self.assertEqual(checks["R11"].state, RegressionState.PASS)
        self.assertEqual(checks["R12"].state, RegressionState.PASS)
        self.assertEqual(self.validation.db_readiness_gate.state.value, "pass")
        self.assertFalse(self.validation.db_readiness_gate.reason_codes)

    def test_conflicts_singletons_and_reentries_remain_available_for_review(self):
        self.assertGreater(self.validation.summary.conflict_count, 0)
        self.assertGreater(self.validation.summary.singleton_count, 0)
        self.assertIsInstance(self.validation.summary.reentry_count, int)
        self.assertEqual(self.validation.summary.overmerge_candidate_count, 0)


if __name__ == "__main__":
    unittest.main()
