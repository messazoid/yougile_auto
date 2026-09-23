import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_store import PipelineStore, utcnow
from yougile_runs import RunConsole, RunConsoleError, TerminalScreen, _display_list, main


class YougileRunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = PipelineStore(self.root / "queue" / "pipeline.sqlite3")
        self.store.initialize()
        self.audio = self.root / "audio" / "sample.wav"
        self.audio.parent.mkdir()
        self.audio.write_bytes(b"wav")
        self.result_dir = self.root / "recognition-runs" / "sample"
        self.result_dir.mkdir(parents=True)
        (self.result_dir / "scan.sqlite3").write_bytes(b"scan")
        (self.result_dir / "responses.jsonl").write_text('{"status": {}}\n')
        self.export = self.root / "aggregation" / "1_1"
        self.export.mkdir(parents=True)
        (self.export / "result.json").write_text("{}\n")
        now = "2026-09-15T12:34:56Z"
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO recognitions
                (id,audio_sha256,config_hash,audio_path,result_dir,state,per_job_limit,created_utc,updated_utc)
                VALUES (1,'a','c',? ,?,'complete_candidates',0,?,?)""",
                (str(self.audio), str(self.result_dir), now, now),
            )
            db.execute(
                """INSERT INTO source_jobs
                (id,source_hash,prep_config_hash,source_kind,source_url,stage,created_utc,updated_utc,
                 source_filename,source_size,wav_path,wav_size,result_dir,recognition_id)
                VALUES (1,'source','prep','yandex_disk','https://disk.yandex.ru/i/reference',
                        'complete_candidates',?,?, 'source.mp4',2048,?,3072,?,1)""",
                (now, now, str(self.audio), str(self.result_dir)),
            )
            db.execute(
                """INSERT INTO aggregation_inputs
                (id,recognition_id,canonical_schema_version,adapter_version,input_hash,canonical_json,created_at)
                VALUES (1,1,'v','v','h','{}',?)""", (now,),
            )
            db.execute(
                """INSERT INTO aggregation_runs
                (id,recognition_id,aggregation_input_id,engine_version,result_schema_version,profile_name,
                 profile_hash,state,created_at,updated_at)
                VALUES (1,1,1,'v','v','default','h','complete',?,?)""", (now, now),
            )
            db.commit()
        self.console = RunConsole(self.store.path)

    def test_list_and_show_are_read_only(self):
        self.assertEqual(self.console.runs()[0]["source_job_id"], 1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.store.path), "show", "S1"]), 0)
        text = output.getvalue()
        self.assertIn("S1", text)
        self.assertIn(str(self.audio), text)
        self.assertIn(str(self.export / "result.json"), text)
        self.assertIn("source.mp4 (2.0 KiB)", text)
        self.assertIn(f"{self.audio} (3.0 KiB)", text)
        self.assertTrue(self.audio.exists())

    def test_list_columns_align_with_the_header(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _display_list(self.console)
        header, row = output.getvalue().splitlines()
        self.assertEqual(header.index("date"), row.index("15.09.2026 12:34"))
        self.assertEqual(header.index("selector"), row.index("S1/R1"))
        self.assertEqual(header.index("status"), row.index("complete_candidates"))
        self.assertEqual(header.index("name"), row.index("source.mp4 (2.0 KiB)"))

    def test_list_uses_a_short_label_for_remote_ffmpeg_error(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE source_jobs SET recognition_id=NULL,stage='audio_error',"
                "error_type='RemoteMediaProcessError' WHERE id=1"
            )
            db.commit()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _display_list(self.console)
        self.assertIn("audio_error (remote_ffmpeg)", output.getvalue())
        self.assertNotIn("RemoteMediaProcessError", output.getvalue())

    def test_view_aggregation_and_build_chat_link(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.console.view(1, "aggregation")
        self.assertIn(str(self.export / "result.json"), output.getvalue())
        self.console.company_id = "12345678-1234-1234-1234-abcdefghijkl"
        self.assertEqual(
            self.console.chat_link({"chat_id": "87654321-4321-4321-4321-zyxwvutsrqpo"}),
            "https://yougile.com/team/abcdefghijkl/#chat:zyxwvutsrqpo",
        )

    def test_view_aggregation_supports_the_public_result_format(self):
        (self.export / "result.json").write_text(json.dumps([
            {
                "period": {"start": 12, "end": 34},
                "title": "Example title",
                "artist": ["Example artist"],
                "iswc": ["T1234567890"],
            },
            {
                "period": {"start": 40, "end": 50},
                "title": "No ISWC",
                "artist": [],
            },
        ]) + "\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.console.view(1, "aggregation")
        text = output.getvalue()
        self.assertIn("Appearances: 2", text)
        self.assertIn("12.0–34.0 (22.0 s) Example title — Example artist; ISWC: T1234567890", text)
        self.assertIn("40.0–50.0 (10.0 s) No ISWC", text)

    def test_view_without_a_limit_includes_every_response_row(self):
        (self.result_dir / "responses.jsonl").write_text(
            "\n".join(
                json.dumps({"window_index": index, "start_seconds": index, "http_status": 200,
                            "acr_code": 1001, "response_text": "{}"})
                for index in range(3)
            ) + "\n"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.console.view(1, "responses", limit=None)
        self.assertEqual(output.getvalue().count("window="), 3)

    def test_archive_all_moves_only_run_artifacts_and_marks_tombstones(self):
        moved = self.console.archive(1, "all")
        self.assertEqual(len(moved), 3)
        self.assertFalse(self.audio.exists())
        self.assertFalse(self.result_dir.exists())
        self.assertFalse(self.export.exists())
        self.assertTrue(all(path.exists() for path in moved))
        with self.store.connect() as db:
            source = db.execute("SELECT wav_deleted_utc FROM source_jobs WHERE id=1").fetchone()
            recognition = db.execute(
                "SELECT audio_deleted_utc,results_deleted_utc FROM recognitions WHERE id=1"
            ).fetchone()
        self.assertIsNotNone(source[0])
        self.assertIsNotNone(recognition[0])
        self.assertIsNotNone(recognition[1])

    def test_archive_refuses_a_shared_recognition(self):
        now = utcnow()
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO source_jobs
                (id,source_hash,prep_config_hash,source_kind,source_url,stage,created_utc,updated_utc,
                 source_filename,wav_path,result_dir,recognition_id)
                VALUES (2,'source-2','prep','yandex_disk','https://disk.yandex.ru/i/other',
                        'complete_candidates',?,?, 'other.mp4',?,?,1)""",
                (now, now, str(self.root / "audio" / "other.wav"), str(self.result_dir)),
            )
            db.commit()
        with self.assertRaisesRegex(RunConsoleError, "shared"):
            self.console.archive(1, "results")

    def test_cli_requires_exact_confirmation(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(main(["--db", str(self.store.path), "trash-audio", "S1", "--confirm", "no"]), 2)
        self.assertIn("TRASH S1", output.getvalue())
        self.assertTrue(self.audio.exists())

    def test_full_replay_creates_a_new_source_and_retains_the_selected_run(self):
        replay_id = self.console.full_replay(1)
        self.assertNotEqual(replay_id, 1)
        self.assertEqual(self.console.detail(1)["source"]["stage"], "complete_candidates")
        replay = self.console.detail(replay_id)["source"]
        self.assertEqual(replay["stage"], "queued")
        self.assertIn(":forced-run:", replay["prep_config_hash"])

    def test_continue_requeues_an_audio_error(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE source_jobs SET stage='audio_error',error_type='fixture' WHERE id=1"
            )
            db.commit()
        self.assertIn("S1 queued", self.console.continue_run(1))
        self.assertEqual(self.console.detail(1)["source"]["stage"], "queued")

    def test_continue_is_available_only_for_an_eligible_run(self):
        self.assertFalse(self.console.can_continue(1))
        with self.store.connect() as db:
            db.execute("UPDATE source_jobs SET stage='audio_error' WHERE id=1")
            db.commit()
        self.assertTrue(self.console.can_continue(1))

    def test_replay_cli_requires_exact_confirmation(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(main(["--db", str(self.store.path), "replay", "S1", "--confirm", "no"]), 2)
        self.assertIn("REPLAY S1", output.getvalue())
        self.assertEqual(len(self.console.runs()), 1)

    def test_interactive_back_returns_to_the_list(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=["1", "p", "q"]):
            self.assertEqual(main(["--db", str(self.store.path)]), 0)
        self.assertGreaterEqual(output.getvalue().count("selector"), 2)

    def test_interactive_hides_continue_for_a_completed_run(self):
        prompts = []
        answers = iter(("1", "p", "q"))

        def prompt(message):
            prompts.append(message)
            return next(answers)

        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=prompt):
            self.assertEqual(main(["--db", str(self.store.path)]), 0)
        self.assertNotIn("[c]ontinue", prompts[1])

    def test_interactive_enter_at_action_keeps_the_selected_run_open(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=["1", "", "p", "q"]):
            self.assertEqual(main(["--db", str(self.store.path)]), 0)
        self.assertGreaterEqual(output.getvalue().count("Source job: S1"), 2)

    def test_view_menu_supports_previous_and_quit(self):
        prompts = []
        answers = iter(("1", "v", "p", "p", "q"))

        def prompt(message):
            prompts.append(message)
            return next(answers)

        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=prompt):
            self.assertEqual(main(["--db", str(self.store.path)]), 0)
        self.assertIn(
            "View: [s]can, [r]esponses, or [a]ggregation\nNavigation: [p]revious [q]uit\n",
            prompts,
        )

        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=("1", "v", "q")):
            self.assertEqual(main(["--db", str(self.store.path)]), 0)

        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=("1", "v", "s", "p", "p", "q")), \
             patch.object(RunConsole, "view") as view:
            self.assertEqual(main(["--db", str(self.store.path)]), 0)
        view.assert_called_once()

    def test_blank_selection_stays_in_the_list(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=["", "q"]):
            self.assertEqual(main(["--db", str(self.store.path)]), 0)
        self.assertIn("enter a list number", output.getvalue())

    def test_ctrl_c_exits_without_traceback(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertEqual(main(["--db", str(self.store.path)]), 130)
        self.assertEqual(output.getvalue(), "Cancelled.\n")

    def test_terminal_screen_restores_previous_terminal(self):
        output = io.StringIO()
        with TerminalScreen(output, enabled=True) as screen:
            screen.clear()
        self.assertEqual(output.getvalue(), "\x1b[?1049h\x1b[2J\x1b[H\x1b[2J\x1b[H\x1b[?1049l")

    def test_terminal_screen_wraps_to_current_width(self):
        output = io.StringIO()
        with TerminalScreen(output, enabled=True) as screen, \
             patch("yougile_runs.shutil.get_terminal_size", return_value=os.terminal_size((20, 24))):
            screen.set_renderer(lambda: print("abcdefghijklmnopqrstuv"))
            screen.redraw()
        self.assertIn("abcdefghijklmnopqrs\ntuv\n", output.getvalue())

    def test_terminal_screen_keeps_the_footer_on_the_last_row(self):
        output = io.StringIO()
        with TerminalScreen(output, enabled=True) as screen, \
             patch("yougile_runs.shutil.get_terminal_size", return_value=os.terminal_size((20, 4))):
            screen.set_renderer(lambda: print("\n".join(f"line {number}" for number in range(10))))
            screen.set_footer("Action: [q]uit")
            screen.redraw()
        rendered = output.getvalue()
        self.assertIn("\x1b[4;1H\x1b[2KAction: [q]uit", rendered)
        self.assertNotIn("line 9", rendered)

    def test_terminal_screen_reserves_explicit_footer_rows(self):
        output = io.StringIO()
        with TerminalScreen(output, enabled=True) as screen, \
             patch("yougile_runs.shutil.get_terminal_size", return_value=os.terminal_size((30, 5))):
            screen.set_renderer(lambda: print("\n".join(f"line {number}" for number in range(10))))
            screen.set_footer("[v]iew [f]ull replay\nArchive: [a]udio [r]esults\nAction: ")
            screen.redraw()
        rendered = output.getvalue()
        self.assertIn("\x1b[3;1H\x1b[2K[v]iew [f]ull replay", rendered)
        self.assertIn("\x1b[5;1H\x1b[2KAction: ", rendered)
        self.assertNotIn("line 3", rendered)

    def test_resize_signal_only_marks_a_safe_redraw(self):
        output = io.StringIO()
        screen = TerminalScreen(output, enabled=False)
        screen.set_renderer(lambda: print("screen"))
        screen._on_resize(None, None)
        screen._on_resize(None, None)
        self.assertTrue(screen.resize_pending)
        with contextlib.redirect_stdout(output):
            screen.redraw()
        self.assertFalse(screen.resize_pending)

    def test_resize_discards_the_interrupted_prompt_value(self):
        output = io.StringIO()
        screen = TerminalScreen(output, enabled=False)
        screen.set_renderer(lambda: None)
        with patch("builtins.input", side_effect=["stale", "fresh"]):
            screen.resize_pending = True
            from yougile_runs import _prompt
            self.assertEqual(_prompt(screen, "prompt: "), "fresh")

    def test_prompt_redraws_on_resize_before_terminal_input(self):
        output = io.StringIO()
        stdin = io.StringIO("S1\n")
        screen = TerminalScreen(output, enabled=True)
        screen.set_renderer(lambda: print("page"))
        from yougile_runs import _prompt
        with patch("yougile_runs.sys.stdin", stdin), \
             patch("yougile_runs.select.select", side_effect=[([], [], []), ([stdin], [], [])]):
            screen.resize_pending = True
            self.assertEqual(_prompt(screen, "prompt: "), "S1")
        self.assertIn("page\n\x1b[24;1H\x1b[2Kprompt: ", output.getvalue())

    def test_prompt_ignores_non_latin_characters_and_decode_errors(self):
        output = io.StringIO()
        screen = TerminalScreen(output, enabled=False)
        screen.set_renderer(lambda: None)
        from yougile_runs import _prompt
        decoding_error = UnicodeDecodeError("utf-8", b"\xd0", 0, 1, "invalid continuation byte")
        with patch("builtins.input", side_effect=[decoding_error, "Sрус13!?"]):
            self.assertEqual(_prompt(screen, "prompt: "), "S13")

    def test_terminal_prompt_retains_latin_command_after_a_bad_utf8_byte(self):
        output = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(b"\xd0f\n"), encoding="utf-8")
        screen = TerminalScreen(output, enabled=True)
        screen.set_renderer(lambda: print("page"))
        from yougile_runs import _prompt
        with patch("yougile_runs.sys.stdin", stdin), \
             patch("yougile_runs.select.select", return_value=([stdin], [], [])):
            self.assertEqual(_prompt(screen, "prompt: "), "f")

    def test_raw_terminal_input_ignores_cyrillic_bytes_without_erasing_commands(self):
        from yougile_runs import _consume_terminal_input
        buffer = bytearray()
        self.assertFalse(_consume_terminal_input(buffer, "ф".encode("utf-8")))
        self.assertFalse(_consume_terminal_input(buffer, b"\x7f"))
        self.assertTrue(_consume_terminal_input(buffer, b"f\r"))
        self.assertEqual(bytes(buffer), b"f")

    def test_viewer_navigation_decodes_arrows_and_control_arrows(self):
        from yougile_runs import _consume_viewer_keys
        pending = bytearray()
        keys = _consume_viewer_keys(
            pending, b"\x1b[A\x1b[B\x1b[1;5A\x1b[1;5BpQ"
        )
        self.assertEqual(keys, ["up", "down", "fast_up", "fast_down", "p"])
