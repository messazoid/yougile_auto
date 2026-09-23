import json
import tempfile
import unittest
from pathlib import Path

from offline.aggregation.archive_adapter import load_run
from src.aggregation import RecognitionWindowStatus


RAW_ROOT = Path(__file__).parents[2] / "offline" / "fixtures" / "raw"


class ArchiveAdapterTests(unittest.TestCase):
    def test_real_run_preserves_candidate_chronology_ranks_and_provenance(self):
        run = load_run(RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465")
        candidate_window = next(item for item in run.windows if item.status is RecognitionWindowStatus.CANDIDATES_RETURNED)
        multi_rank_window = next(item for item in run.windows if len(item.candidates) > 1)
        candidate = candidate_window.candidates[0]
        self.assertEqual([item.index for item in run.windows], list(range(len(run.windows))))
        self.assertEqual([item.rank for item in multi_rank_window.candidates], list(range(1, len(multi_rank_window.candidates) + 1)))
        self.assertEqual(candidate_window.raw_provenance.locator["file"], "responses.jsonl")
        self.assertEqual(candidate.raw_provenance.locator["candidate_rank"], 1)
        self.assertEqual(candidate.acrid, candidate.raw_values["acrid"])
        self.assertEqual(candidate.score, candidate.raw_values["score"])
        self.assertEqual(candidate.play_offset, candidate.raw_values.get("play_offset_ms"))
        self.assertEqual(candidate.album, candidate.raw_values["album"].get("name"))

    def test_real_run_preserves_1001_and_deterministic_parse(self):
        path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        first, second = load_run(path), load_run(path)
        self.assertIn(RecognitionWindowStatus.NO_RESULT_1001, [item.status for item in first.windows])
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_all_fixture_runs_use_confirmed_format(self):
        runs = [load_run(path) for path in sorted(RAW_ROOT.iterdir()) if path.is_dir()]
        self.assertEqual(len(runs), 10)
        self.assertTrue(all(run.windows for run in runs))
        self.assertTrue(all(run.source_provenance.locator["file"] == "sources.json" for run in runs))
        self.assertTrue(all(run.source_raw_values["logical_sources"] for run in runs))

    def test_real_local_window_is_not_an_acr_1001_and_sources_are_preserved(self):
        run = load_run(RAW_ROOT / "cce58d848c724a753a7c1314-dc50dd1106209666")
        local = [item for item in run.windows if item.status is RecognitionWindowStatus.NOT_SUBMITTED]
        no_result = [item for item in run.windows if item.status is RecognitionWindowStatus.NO_RESULT_1001]
        self.assertEqual(len(local), 1)
        self.assertTrue(no_result)
        self.assertNotEqual(local[0].raw_provenance.locator["file"], "responses.jsonl")
        self.assertEqual(local[0].raw_provenance.locator["file"], "local_windows.jsonl")
        self.assertEqual(len(run.source_raw_values["logical_sources"][0]["items"]), 1)

    def test_response_candidates_match_derived_matches_jsonl_without_reading_it(self):
        path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        run = load_run(path)
        parsed_count = sum(len(item.candidates) for item in run.windows)
        with (path / "matches.jsonl").open(encoding="utf-8") as matches:
            matches_count = sum(1 for _ in matches)
        self.assertEqual(parsed_count, matches_count)

    def test_minimal_confirmed_format_covers_errors_unknown_metadata_and_no_reference_leakage(self):
        with tempfile.TemporaryDirectory() as root:
            run = Path(root) / "fixture-run"
            run.mkdir()
            job = {"audio_sha256": "audio", "config": {"audio": {"frames": 30, "sample_rate": 1, "duration_seconds": 30}, "window_seconds": 10, "step_seconds": 10}}
            response = {"window_index": 0, "start_seconds": 0, "duration_seconds": 10, "state": "done", "http_status": 200, "acr_code": 0, "response_text": json.dumps({"status": {"code": 0}, "metadata": {"music": [{"acrid": "one", "score": 90, "artists": [{"name": "A"}]}, {"acrid": "two", "score": 80}]}})}
            error = {"window_index": 1, "start_seconds": 10, "duration_seconds": 10, "state": "failed", "http_status": 503, "acr_code": None, "response_text": None}
            (run / "job.json").write_text(json.dumps(job), encoding="utf-8")
            (run / "sources.json").write_text(json.dumps({"schema_version": 1, "logical_sources": [{"items": []}]}), encoding="utf-8")
            (run / "responses.jsonl").write_text("\n".join(json.dumps(item) for item in (response, error)) + "\n", encoding="utf-8")
            (run / "local_windows.jsonl").write_text("", encoding="utf-8")
            (run / "reference.json").write_text('{"must_not_be_read": true}', encoding="utf-8")
            parsed = load_run(run)
            (run / "reference.json").write_text('{"changed": ["unrelated"]}', encoding="utf-8")
            reparsed = load_run(run)
        self.assertEqual([item.status for item in parsed.windows], [RecognitionWindowStatus.CANDIDATES_RETURNED, RecognitionWindowStatus.PROCESSING_ERROR, RecognitionWindowStatus.MISSING_RAW])
        self.assertEqual(parsed.to_dict(), reparsed.to_dict())
        self.assertEqual([item.rank for item in parsed.windows[0].candidates], [1, 2])
        self.assertIsNone(parsed.windows[0].candidates[1].title)
        self.assertEqual(parsed.windows[0].candidates[0].artists, ("A",))


if __name__ == "__main__":
    unittest.main()
