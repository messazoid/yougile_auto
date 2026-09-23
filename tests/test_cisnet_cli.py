import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import io
import contextlib
from io import StringIO

from cisnet_cli import (
    CisnetCommandError, _parse_positions, aggregation_runs, create_job, execute_search,
    main, manual_request, requests_from_aggregation, run_requests,
    safe_playwright_log_lines, session_action,
)


class CisnetCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        directory = self.root / "aggregation" / "1_1"
        directory.mkdir(parents=True)
        (directory / "result.json").write_text(json.dumps([
            {"title": "Example", "artist": ["Artist"], "period": {"start": 1, "end": 2}},
            {"title": "Second", "artist": ["Other"]},
        ]), encoding="utf-8")

    def test_playwright_log_filter_rejects_unstructured_and_private_text(self):
        raw = (
            'password=private-value\n'
            '[CISNET-PLAYWRIGHT] {"step":"login.wait","event":"failed","code":"TIMEOUT"}\n'
            '[CISNET-PLAYWRIGHT] {"step":"login.wait","event":[],"code":"TIMEOUT"}\n'
            '[CISNET-PLAYWRIGHT] {"step":"login.wait","event":"failed","code":"TIMEOUT","secret":"private-value"}\n'
            '[CISNET-PLAYWRIGHT] {"step":"login.wait","event":"failed","code":"PRIVATE_VALUE"}\n'
            '[CISNET-PLAYWRIGHT] {"step":"private.value","event":"ok"}\n'
        )
        self.assertEqual(safe_playwright_log_lines(raw), [
            '[CISNET-PLAYWRIGHT] {"step":"login.wait","event":"failed","code":"TIMEOUT"}'
        ])

    def test_session_failure_forwards_only_safe_playwright_trace(self):
        stderr = (
            '[CISNET-PLAYWRIGHT] {"step":"browser.attach","event":"begin"}\n'
            'password=private-value\n'
            '[CISNET-PLAYWRIGHT] {"step":"browser.attach","event":"failed","code":"CDP_ATTACH_FAILED"}\n'
        )
        output = io.StringIO()
        process = MagicMock()
        process.stdout = StringIO("")
        process.stderr = StringIO(stderr)
        process.wait.return_value = 2
        with patch('cisnet_cli.subprocess.Popen', return_value=process):
            with contextlib.redirect_stderr(output):
                with self.assertRaisesRegex(CisnetCommandError, 'CDP_ATTACH_FAILED'):
                    session_action('start', self.root / 'adapter.js')
        self.assertIn('"event":"begin"', output.getvalue())
        self.assertIn('"code":"CDP_ATTACH_FAILED"', output.getvalue())
        self.assertNotIn('private-value', output.getvalue())

    def test_busy_session_has_temporary_exit_code(self):
        error = CisnetCommandError("CIS-Net start failed (BUSY_SESSION)", code="BUSY_SESSION")
        with patch("cisnet_cli.run_requests", side_effect=error):
            self.assertEqual(main([
                "--data-root", str(self.root), "manual", "--title", "Title",
                "--performer", "Artist", "--exe",
            ]), 75)

    def test_search_failure_without_trace_has_safe_fallback(self):
        output = io.StringIO()
        process = MagicMock()
        process.stdout = StringIO("")
        process.stderr = StringIO("private-value")
        process.wait.return_value = 2
        with patch('cisnet_cli.subprocess.Popen', return_value=process):
            with contextlib.redirect_stderr(output):
                with self.assertRaisesRegex(CisnetCommandError, 'NO_TRACE'):
                    execute_search(self.root / 'request.json', self.root / 'adapter.js')
        self.assertIn('"code":"NO_TRACE"', output.getvalue())
        self.assertNotIn('private-value', output.getvalue())

    def test_selected_aggregation_candidate_uses_public_result_fields(self):
        request = requests_from_aggregation(self.root, "1_1", [1])[0]
        self.assertEqual(request.title, "Example")
        self.assertEqual(request.performer, "Artist")
        self.assertEqual(request.run_name, "1_1")

    def test_all_selects_every_aggregation_candidate(self):
        requests = requests_from_aggregation(self.root, "1_1", _parse_positions("all"))
        self.assertEqual([request.title for request in requests], ["Example", "Second"])

    def test_aggregation_iswc_is_used_only_when_valid(self):
        result_path = self.root / "aggregation" / "1_1" / "result.json"
        records = json.loads(result_path.read_text())
        records[0]["iswc"] = "T-901.302.614-7"
        records[1]["iswc"] = "unknown"
        result_path.write_text(json.dumps(records))
        without_iswc, with_iswc = requests_from_aggregation(self.root, "1_1", None)
        self.assertEqual((without_iswc.title, without_iswc.iswc), ("Second", None))
        self.assertEqual((with_iswc.title, with_iswc.iswc), ("Example", "T-901.302.614-7"))
        self.assertNotEqual(create_job(self.root, with_iswc)[0].name, "32de35ad7e38")

    def test_compact_acr_iswc_is_normalized_and_searched_after_titles(self):
        result_path = self.root / "aggregation" / "1_1" / "result.json"
        records = json.loads(result_path.read_text())
        records[0]["iswc"] = ["T9013026147"]
        result_path.write_text(json.dumps(records))

        without_iswc, with_iswc = requests_from_aggregation(self.root, "1_1", None)

        self.assertEqual((without_iswc.title, without_iswc.candidate_index), ("Second", 2))
        self.assertEqual((with_iswc.title, with_iswc.candidate_index), ("Example", 1))
        self.assertEqual(with_iswc.iswc, "T-901.302.614-7")

    def test_aggregation_uses_first_valid_iswc_from_public_list(self):
        result_path = self.root / "aggregation" / "1_1" / "result.json"
        records = json.loads(result_path.read_text())
        records[0]["iswc"] = ["unknown", "T-901.302.614-7", "T-123.456.789-0"]
        result_path.write_text(json.dumps(records))
        self.assertEqual(requests_from_aggregation(self.root, "1_1", [1])[0].iswc, "T-901.302.614-7")

    def test_manual_request_accepts_valid_iswc_and_rejects_invalid_code(self):
        self.assertEqual(manual_request("Title", "Artist", "t-901.904.474-7").iswc, "T-901.904.474-7")
        with self.assertRaises(CisnetCommandError):
            manual_request("Title", "Artist", "wrong code")

    def test_aggregation_runs_are_sorted_numerically(self):
        for name in ("10_10", "2_2"):
            directory = self.root / "aggregation" / name
            directory.mkdir()
            (directory / "result.json").write_text("[]")
        self.assertEqual(aggregation_runs(self.root), ["1_1", "2_2", "10_10"])

    def test_job_uses_cisnet_suffix_and_refuses_to_overwrite_result(self):
        request = requests_from_aggregation(self.root, "1_1", [1])[0]
        job, request_path = create_job(self.root, request, now="20260921T120000Z")
        self.assertEqual(job.parent.name, "1_1_CISNET")
        self.assertEqual(json.loads(request_path.read_text())["title"], "Example")
        (job / "result.json").write_text("{}")
        with self.assertRaises(CisnetCommandError):
            create_job(self.root, request, now="20260921T120001Z")

    def test_job_retries_after_error_and_preserves_the_error_file(self):
        request = requests_from_aggregation(self.root, "1_1", [1])[0]
        job, _ = create_job(self.root, request, now="20260921T120000Z")
        (job / "error.json").write_text('{"message":"CDP unavailable"}\n')

        retried_job, request_path = create_job(self.root, request, now="20260921T120001Z")

        self.assertEqual(retried_job, job)
        self.assertFalse((job / "error.json").exists())
        self.assertEqual(
            (job / "attempts" / "20260921T120001Z" / "error.json").read_text(),
            '{"message":"CDP unavailable"}\n',
        )
        self.assertEqual(json.loads(request_path.read_text())["title"], "Example")

    def test_overwrite_archives_existing_result_only_after_new_search_succeeds(self):
        request = requests_from_aggregation(self.root, "1_1", [1])[0]
        job, _ = create_job(self.root, request, now="20260921T120000Z")
        (job / "result.json").write_text('{"old":true}\n')

        with patch("cisnet_cli.session_action"), patch("cisnet_cli.execute_search", return_value={"works": []}):
            self.assertEqual(run_requests(self.root, [request], True, self.root / "adapter.js", overwrite_results=True), 0)

        archived = sorted((job / "attempts").glob("*/result.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(), '{"old":true}\n')
        self.assertEqual(json.loads((job / "result.json").read_text()), {"works": []})

    def test_existing_result_is_skipped_without_overwrite_permission(self):
        request = requests_from_aggregation(self.root, "1_1", [1])[0]
        job, _ = create_job(self.root, request, now="20260921T120000Z")
        (job / "result.json").write_text('{"old":true}\n')

        with patch("cisnet_cli.session_action") as session, patch("cisnet_cli.execute_search") as search:
            self.assertEqual(run_requests(self.root, [request], True, self.root / "adapter.js"), 0)

        search.assert_not_called()
        session.assert_not_called()
        self.assertEqual((job / "result.json").read_text(), '{"old":true}\n')

    def test_all_candidates_share_one_login_and_one_logout(self):
        requests = requests_from_aggregation(self.root, "1_1", None)
        with patch("cisnet_cli.session_action") as session, patch("cisnet_cli.execute_search", return_value={"works": []}) as search:
            self.assertEqual(run_requests(self.root, requests, True, self.root / "adapter.js"), 0)
        self.assertEqual(search.call_count, 2)
        self.assertEqual([call.args[0] for call in session.call_args_list], ["start", "finish"])

    def test_identical_candidate_search_is_performed_once(self):
        result_path = self.root / "aggregation" / "1_1" / "result.json"
        records = json.loads(result_path.read_text())
        records[1] = {**records[0], "period": {"start": 5, "end": 6}}
        result_path.write_text(json.dumps(records))
        requests = requests_from_aggregation(self.root, "1_1", None)
        with patch("cisnet_cli.session_action") as session, patch("cisnet_cli.execute_search", return_value={"works": []}) as search:
            self.assertEqual(run_requests(self.root, requests, True, self.root / "adapter.js"), 0)
        self.assertEqual(search.call_count, 1)
        self.assertEqual([call.args[0] for call in session.call_args_list], ["start", "finish"])
        self.assertEqual(len(list((self.root / "cisnet" / "1_1_CISNET").glob("*/result.json"))), 1)

    def test_different_iswc_is_not_an_identical_search(self):
        first = manual_request("Title", "Artist", "T-901.302.614-7")
        second = manual_request("Title", "Artist", "T-123.456.789-0")
        with patch("cisnet_cli.session_action"), patch("cisnet_cli.execute_search", return_value={"works": []}) as search:
            self.assertEqual(run_requests(self.root, [first, second], True, self.root / "adapter.js"), 0)
        self.assertEqual(search.call_count, 2)

    def test_title_case_difference_is_not_an_exact_duplicate(self):
        first = manual_request("Title", "Artist")
        second = manual_request("title", "Artist")
        with patch("cisnet_cli.session_action"), patch("cisnet_cli.execute_search", return_value={"works": []}) as search:
            self.assertEqual(run_requests(self.root, [first, second], True, self.root / "adapter.js"), 0)
        self.assertEqual(search.call_count, 2)

    def test_search_error_still_logs_out(self):
        request = requests_from_aggregation(self.root, "1_1", [1])[0]
        with patch("cisnet_cli.session_action") as session, patch("cisnet_cli.execute_search", side_effect=CisnetCommandError("search failed")):
            with self.assertRaises(CisnetCommandError):
                run_requests(self.root, [request], True, self.root / "adapter.js")
        self.assertEqual([call.args[0] for call in session.call_args_list], ["start", "finish"])

    def test_interactive_overwrite_requires_the_explicit_word(self):
        request = requests_from_aggregation(self.root, "1_1", [1])[0]
        job, _ = create_job(self.root, request, now="20260921T120000Z")
        (job / "result.json").write_text('{"old":true}\n')

        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["1", "1", "1", "EXECUTE", "OVERWRITE"]), patch("cisnet_cli.session_action"), patch("cisnet_cli.execute_search", return_value={"works": []}):
            self.assertEqual(main(["--data-root", str(self.root), "--exe"]), 0)

        self.assertEqual(json.loads((job / "result.json").read_text()), {"works": []})

    def test_manual_dry_run_never_creates_a_folder(self):
        request = manual_request("Manual title", "Manual artist")
        self.assertEqual(run_requests(self.root, [request], False, self.root / "adapter.js"), 0)
        self.assertFalse((self.root / "cisnet").exists())

    def test_rejects_missing_performer_from_public_result(self):
        directory = self.root / "aggregation" / "1_1"
        (directory / "result.json").write_text(json.dumps([{"title": "Example", "artist": []}]))
        with self.assertRaises(CisnetCommandError):
            requests_from_aggregation(self.root, "1_1", [1])

    def test_interactive_ctrl_c_is_quiet(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertEqual(main(["--data-root", str(self.root)]), 130)

    def test_interactive_run_selection_uses_a_number(self):
        output = io.StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["1", "1", "1", ""]), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--data-root", str(self.root)]), 0)
        self.assertIn("1)1_1", output.getvalue())
        self.assertIn("Dry run: CIS-Net was not opened.", output.getvalue())

    def test_interactive_execute_requires_the_top_level_flag(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["2", "Title", "Performer", "", "EXECUTE"]), patch("cisnet_cli.run_requests", return_value=0) as run:
            self.assertEqual(main(["--data-root", str(self.root), "--execute"]), 0)
        self.assertTrue(run.call_args.args[2])

    def test_interactive_exe_is_an_execute_alias(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["2", "Title", "Performer", "", "EXECUTE"]), patch("cisnet_cli.run_requests", return_value=0) as run:
            self.assertEqual(main(["--data-root", str(self.root), "--exe"]), 0)
        self.assertTrue(run.call_args.args[2])
