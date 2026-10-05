"""The ten-second rule applies to total aggregated time per track family."""

import unittest

from aggregation.export import (
    COUNTED,
    NOT_COUNTED_SHORT,
    NOT_COUNTED_UNAVAILABLE,
    build_public_result,
    build_summary,
    build_summary_markdown,
    duration_decisions,
)


def appearance(family, start, end, candidate_id):
    return {
        "family_id": family,
        "observed_range": {"start": start, "end": end},
        "supporting_observation_ids": [candidate_id],
    }


class DurationPolicyTests(unittest.TestCase):
    def test_exactly_ten_seconds_is_not_counted(self):
        decisions = duration_decisions({"appearances": [appearance("song", 10, 20, "a")]})
        self.assertEqual(decisions["song"], {
            "count_status": NOT_COUNTED_SHORT,
            "total_duration_seconds": 10.0,
        })

    def test_separate_appearances_are_summed_by_family(self):
        result = {"appearances": [
            appearance("song", 0, 6, "a"),
            appearance("song", 30, 34, "b"),
            appearance("other", 0, 11, "c"),
        ]}
        decisions = duration_decisions(result)
        self.assertEqual(decisions["song"]["total_duration_seconds"], 10.0)
        self.assertEqual(decisions["song"]["count_status"], NOT_COUNTED_SHORT)
        self.assertEqual(decisions["other"]["count_status"], COUNTED)

    def test_overlapping_appearances_are_not_double_counted(self):
        decisions = duration_decisions({"appearances": [
            appearance("song", 0, 8, "a"),
            appearance("song", 5, 12, "b"),
        ]})
        self.assertEqual(decisions["song"]["total_duration_seconds"], 12.0)
        self.assertEqual(decisions["song"]["count_status"], COUNTED)

    def test_separate_appearances_over_ten_seconds_are_counted(self):
        decisions = duration_decisions({"appearances": [
            appearance("song", 0, 6, "a"),
            appearance("song", 30, 35, "b"),
        ]})
        self.assertEqual(decisions["song"]["total_duration_seconds"], 11.0)
        self.assertEqual(decisions["song"]["count_status"], COUNTED)

    def test_missing_duration_is_not_counted(self):
        decisions = duration_decisions({"appearances": [appearance("song", 0, None, "a")]})
        self.assertEqual(decisions["song"]["count_status"], NOT_COUNTED_UNAVAILABLE)

    def test_short_track_remains_internal_but_not_in_yougile_report(self):
        result = {
            "appearances": [
                appearance("short", 0, 10, "short-id"),
                appearance("long", 20, 32, "long-id"),
            ],
            "track_families": [
                {"family_id": "short", "preferred_core_title": "Short Song", "artists": ["A"]},
                {"family_id": "long", "preferred_core_title": "Long Song", "artists": ["B"]},
                {"family_id": "secondary", "preferred_core_title": "Secondary Song", "artists": ["C"]},
            ],
            "evidence": [],
        }
        canonical = {"windows": [{"candidates": [
            {"candidate_id": "short-id", "title": "Short Song", "artists": ["A"]},
            {"candidate_id": "long-id", "title": "Long Song", "artists": ["B"]},
        ]}]}
        public = build_public_result(result, canonical)
        self.assertEqual([item["count_status"] for item in public], [NOT_COUNTED_SHORT, COUNTED])
        summary = build_summary(result, 1, 1)
        markdown = build_summary_markdown(summary)
        self.assertEqual(summary["counted_appearances_count"], 1)
        self.assertNotIn("Short Song", markdown)
        self.assertNotIn("Secondary Song", markdown)
        self.assertIn("Long Song", markdown)

    def test_short_only_report_contains_no_track_name(self):
        result = {
            "appearances": [appearance("short", 0, 10, "short-id")],
            "track_families": [
                {"family_id": "short", "preferred_core_title": "Short Song", "artists": ["A"]},
            ],
            "evidence": [],
        }
        summary = build_summary(result, 1, 1)
        self.assertEqual(summary["counted_appearances_count"], 0)
        self.assertNotIn("Short Song", build_summary_markdown(summary))


if __name__ == "__main__":
    unittest.main()
