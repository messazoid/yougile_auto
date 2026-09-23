import asyncio
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cisnet_automation import initialize, run_pending
from cisnet_cli import _query_key, requests_from_aggregation
from job_store import PipelineStore, SCHEMA_VERSION
import receiver


class CisnetAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "queue" / "pipeline.sqlite3"
        self.store = PipelineStore(self.db_path)
        self.store.initialize()
        self._aggregation(1, "complete")
        self._aggregation(2, "pending")
        old = self.root / "cisnet" / "automation"
        old.mkdir(parents=True)
        (old / "baseline.json").write_text(json.dumps({
            "schema_version": "cisnet-automation-baseline/v1",
            "max_existing_run_id": 1,
            "preexisting_incomplete_run_ids": [2],
        }))
        initialize(self.db_path, self.root)

    def _aggregation(self, run_id, state):
        now = "2026-09-22T00:00:00Z"
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO recognitions
                   (id,audio_sha256,config_hash,audio_path,result_dir,state,per_job_limit,created_utc,updated_utc)
                   VALUES (?,?,?,?,?,'complete_candidates',1,?,?)""",
                (run_id, f"audio{run_id}", f"config{run_id}", "audio.wav", "runs", now, now),
            )
            db.execute(
                """INSERT INTO aggregation_inputs
                   (id,recognition_id,canonical_schema_version,adapter_version,input_hash,canonical_json,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, run_id, "input", "adapter", f"hash{run_id}", "{}", now),
            )
            db.execute(
                """INSERT INTO aggregation_runs
                   (id,recognition_id,aggregation_input_id,engine_version,result_schema_version,
                    profile_name,profile_hash,state,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, run_id, run_id, "engine", "result", "profile", f"profile{run_id}", state, now, now),
            )
            db.commit()

    def _complete(self, run_id=2):
        with self.store.connect() as db:
            db.execute("UPDATE aggregation_runs SET state='complete' WHERE id=?", (run_id,))
            db.commit()

    def _export(self, run_name, records):
        destination = self.root / "aggregation" / run_name
        destination.mkdir(parents=True)
        (destination / "result.json").write_text(json.dumps(records))

    def _result(self, run_name, request, works):
        destination = self.root / "cisnet" / f"{run_name}_CISNET" / _query_key(request)
        destination.mkdir(parents=True)
        (destination / "result.json").write_text(json.dumps({"works": works}))

    def _db_rows(self, table):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]

    def _link_chat(self, recognition_id, chat_id, suffix):
        now = "2026-09-22T00:00:00Z"
        with self.store.connect() as db:
            webhook_id = db.execute(
                """INSERT INTO webhook_events
                   (delivery_key,payload_sha256,payload_json,stage,received_utc,last_received_utc,updated_utc)
                   VALUES (?,?,?,'complete',?,?,?)""",
                (f"delivery-{suffix}", f"payload-{suffix}", "{}", now, now, now),
            ).lastrowid
            source_id = db.execute(
                """INSERT INTO source_jobs
                   (source_hash,prep_config_hash,source_kind,source_url,stage,created_utc,updated_utc)
                   VALUES (?,?,?,?, 'complete',?,?)""",
                (f"source-{suffix}", "prep", "file", "https://example.invalid/audio.wav", now, now),
            ).lastrowid
            db.execute(
                """INSERT INTO message_sources
                   (message_key,webhook_event_id,source_job_id,recognition_id,chat_id,source_hash,created_utc)
                   VALUES (?,?,?,?,?,?,?)""",
                (f"message-{suffix}", webhook_id, source_id, recognition_id, chat_id,
                 f"source-{suffix}", now),
            )
            db.commit()

    def test_baseline_preserves_old_boundary_and_pending_run(self):
        self.assertEqual(self._db_rows("cisnet_baseline")[0]["max_existing_run_id"], 1)
        self.assertEqual(json.loads(self._db_rows("cisnet_baseline")[0]["preexisting_incomplete_ids_json"]), [2])
        self.assertEqual(self._db_rows("schema_info")[0]["version"], SCHEMA_VERSION)
        self._export("1_1", [{"title": "Old", "artist": ["Artist"]}])
        self._complete()
        self._export("2_2", [])
        with patch("cisnet_automation._run_wrapper") as run:
            self.assertEqual(run_pending(self.db_path, self.root), 0)
            run.assert_not_called()
        self.assertEqual([row["aggregation_run_id"] for row in self._db_rows("cisnet_runs")], [2])
        self.assertEqual(self._db_rows("cisnet_runs")[0]["state"], "empty")

    def test_first_available_iswc_and_no_duplicate_message_lines(self):
        self._complete()
        self._export("2_2", [
            {"title": "Angel Baby", "artist": ["Troye Sivan"], "period": {"start": 0, "end": 10}},
            {"title": "Angel Baby", "artist": ["Troye Sivan"], "period": {"start": 20, "end": 30}},
            {"title": "Blue", "artist": ["BUKO"]},
        ])
        first, _, blue = requests_from_aggregation(self.root, "2_2", None)

        def browser(*args, **kwargs):
            self._result("2_2", first, [
                {"title": "ANGEL BABY", "iswc": "T-307.792.406-8"},
                {"title": "ANGEL BABY", "iswc": "T-999.999.999-9"},
            ])
            self._result("2_2", blue, [
                {"title": "BLUE"},
                {"title": "BLUE", "iswc": "not-an-iswc"},
                {"title": "BLUE", "iswc": "T-999.999.999-9"},
            ])
            return subprocess.CompletedProcess([], 0)

        with patch("cisnet_automation._run_wrapper", side_effect=browser) as run:
            self.assertEqual(run_pending(self.db_path, self.root), 0)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(len(self._db_rows("cisnet_searches")), 2)
        self.assertEqual(len(self._db_rows("cisnet_candidates")), 3)
        item = self._db_rows("cisnet_runs")[0]
        self.assertEqual(item["state"], "ready")
        self.assertEqual(item["message_text"],
                         "1. Angel Baby от Troye Sivan - РАО (T-307.792.406-8) - да\n"
                         "2. Blue от BUKO - РАО (T-999.999.999-9) - да")
        with patch("cisnet_automation._run_wrapper") as repeat:
            self.assertEqual(run_pending(self.db_path, self.root), 0)
            repeat.assert_not_called()

    def test_ready_result_is_sent_once_per_linked_chat_and_late_links_are_recovered(self):
        self._complete()
        self._export("2_2", [{"title": "Angel Baby", "artist": ["Troye Sivan"]}])
        request = requests_from_aggregation(self.root, "2_2", None)[0]
        self._link_chat(2, "chat-1", "first")
        self._link_chat(2, "chat-1", "duplicate")

        def browser(*args, **kwargs):
            self._result("2_2", request, [{"iswc": "T-307.792.406-8"}])
            return subprocess.CompletedProcess([], 0)

        with patch("cisnet_automation._run_wrapper", side_effect=browser):
            self.assertEqual(run_pending(self.db_path, self.root), 0)
        notifications = self._db_rows("chat_notifications")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["chat_id"], "chat-1")
        self.assertEqual(notifications[0]["kind"], "cisnet_result")
        self.assertEqual(notifications[0]["text"],
                         "Сервер: Результаты CIS-Net:\n"
                         "1. Angel Baby от Troye Sivan - РАО (T-307.792.406-8) - да")

        self._link_chat(2, "chat-2", "late")
        with patch("cisnet_automation._run_wrapper") as repeat:
            self.assertEqual(run_pending(self.db_path, self.root), 0)
            self.assertEqual(run_pending(self.db_path, self.root), 0)
            repeat.assert_not_called()
        self.assertEqual(len(self._db_rows("chat_notifications")), 2)

        delivered = []

        async def post(path, payload, *, request_kind):
            delivered.append((path, payload["text"], request_kind))
            return {"id": "message"}

        self.assertTrue(asyncio.run(receiver.flush_one_chat_notification(
            self.store, post_message=post
        )))
        self.assertTrue(asyncio.run(receiver.flush_one_chat_notification(
            self.store, post_message=post
        )))
        self.assertFalse(asyncio.run(receiver.flush_one_chat_notification(
            self.store, post_message=post
        )))
        self.assertEqual([item[0] for item in delivered],
                         ["/chats/chat-1/messages", "/chats/chat-2/messages"])
        self.assertTrue(all(item[2] == "notification_message" for item in delivered))
        self.assertTrue(all(item["state"] == "sent" for item in self._db_rows("chat_notifications")))
        self.assertTrue(receiver.is_pipeline_notification_message({"text": notifications[0]["text"]}))

    def test_first_live_search_event_queues_one_started_notification(self):
        self._complete()
        self._export("2_2", [{"title": "Angel Baby", "artist": ["Troye Sivan"]}])
        request = requests_from_aggregation(self.root, "2_2", None)[0]
        self._link_chat(2, "chat-1", "started")

        def browser(*args, **kwargs):
            on_log = args[2]
            line = '[CISNET-PLAYWRIGHT] {"step":"search.request","event":"begin"}'
            on_log(line)
            on_log(line)
            self._result("2_2", request, [{"iswc": "T-307.792.406-8"}])
            return subprocess.CompletedProcess([], 0)

        with patch("cisnet_automation._run_wrapper", side_effect=browser):
            self.assertEqual(run_pending(self.db_path, self.root), 0)
        notifications = self._db_rows("chat_notifications")
        self.assertEqual([item["kind"] for item in notifications], ["cisnet_started", "cisnet_result"])
        self.assertEqual(notifications[0]["text"], "Сервер: Поиск CIS-Net начался")

    def test_no_results_means_no_and_browser_error_is_not_no(self):
        self._complete()
        self._export("2_2", [{"title": "Missing", "artist": ["Artist"]}])
        request = requests_from_aggregation(self.root, "2_2", None)[0]

        def browser(*args, **kwargs):
            self._result("2_2", request, [])
            return subprocess.CompletedProcess([], 0)

        with patch("cisnet_automation._run_wrapper", side_effect=browser):
            self.assertEqual(run_pending(self.db_path, self.root), 0)
        self.assertEqual(self._db_rows("cisnet_runs")[0]["message_text"],
                         "1. Missing от Artist - РАО - нет")

    def test_results_without_iswc_mean_no(self):
        self._complete()
        self._export("2_2", [{"title": "Missing", "artist": ["Artist"]}])
        request = requests_from_aggregation(self.root, "2_2", None)[0]

        def browser(*args, **kwargs):
            self._result("2_2", request, [
                {"title": "MISSING"},
                {"title": "MISSING", "iswc": "not-an-iswc"},
            ])
            return subprocess.CompletedProcess([], 0)

        with patch("cisnet_automation._run_wrapper", side_effect=browser):
            self.assertEqual(run_pending(self.db_path, self.root), 0)
        self.assertEqual(self._db_rows("cisnet_runs")[0]["message_text"],
                         "1. Missing от Artist - РАО - нет")

    def test_message_omits_identical_selected_lines_from_distinct_queries(self):
        self._complete()
        self._export("2_2", [
            {"title": "Same", "artist": ["Artist"], "iswc": ["T-111.111.111-1"]},
            {"title": "Same", "artist": ["Artist"], "iswc": ["T-222.222.222-2"]},
        ])
        requests = requests_from_aggregation(self.root, "2_2", None)

        def browser(*args, **kwargs):
            for request in requests:
                self._result("2_2", request, [{"title": "SAME", "iswc": "T-333.333.333-3"}])
            return subprocess.CompletedProcess([], 0)

        with patch("cisnet_automation._run_wrapper", side_effect=browser):
            self.assertEqual(run_pending(self.db_path, self.root), 0)
        self.assertEqual(len(self._db_rows("cisnet_searches")), 2)
        self.assertEqual(self._db_rows("cisnet_runs")[0]["message_text"],
                         "1. Same от Artist - РАО (T-333.333.333-3) - да")

    def test_failed_browser_does_not_prepare_false_no_or_retry(self):
        self._complete()
        self._export("2_2", [{"title": "Missing", "artist": ["Artist"]}])
        self._link_chat(2, "chat-1", "failed")
        stderr = ('[CISNET-PLAYWRIGHT] {"step":"mwi.open","event":"failed","code":"TIMEOUT"}\n'
                  'password=private-value\n')
        output = io.StringIO()
        def failed_browser(*args, **kwargs):
            on_log = args[2]
            for line in stderr.splitlines():
                if line.startswith("[CISNET-PLAYWRIGHT] "):
                    on_log(line)
            return subprocess.CompletedProcess([], 2, '', stderr)

        with patch("cisnet_automation._run_wrapper", side_effect=failed_browser) as run:
            with contextlib.redirect_stdout(output):
                self.assertEqual(run_pending(self.db_path, self.root), 2)
                self.assertEqual(run_pending(self.db_path, self.root), 0)
            self.assertEqual(run.call_count, 1)
        self.assertIn('"step":"mwi.open","event":"failed","code":"TIMEOUT"', output.getvalue())
        self.assertNotIn('private-value', output.getvalue())
        item = self._db_rows("cisnet_runs")[0]
        self.assertEqual(item["state"], "error")
        self.assertIsNone(item["message_text"])
        self.assertEqual(self._db_rows("chat_notifications"), [])

    def test_busy_browser_leaves_pending_for_later(self):
        self._complete()
        self._export("2_2", [{"title": "Pending", "artist": ["Artist"]}])
        with patch("cisnet_automation._run_wrapper", return_value=subprocess.CompletedProcess([], 75)) as run:
            self.assertEqual(run_pending(self.db_path, self.root), 0)
            self.assertEqual(run_pending(self.db_path, self.root), 0)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self._db_rows("cisnet_runs")[0]["state"], "pending")
        search = self._db_rows("cisnet_searches")[0]
        self.assertEqual(search["state"], "pending")
        self.assertEqual(search["error_type"], "BusySession")

        with self.store.connect() as db:
            db.execute(
                "UPDATE cisnet_searches SET updated_utc='2026-09-22T00:00:00Z' WHERE id=?",
                (search["id"],),
            )
            db.commit()
        request = requests_from_aggregation(self.root, "2_2", None)[0]

        def browser(*args, **kwargs):
            self._result("2_2", request, [{"iswc": "T-307.792.406-8"}])
            return subprocess.CompletedProcess([], 0)

        with patch("cisnet_automation._run_wrapper", side_effect=browser) as retry:
            self.assertEqual(run_pending(self.db_path, self.root), 0)
            self.assertEqual(retry.call_count, 1)
        self.assertEqual(self._db_rows("cisnet_runs")[0]["state"], "ready")
        self.assertIsNone(self._db_rows("cisnet_searches")[0]["error_type"])

    def test_schema_eleven_migrates_without_losing_aggregation(self):
        with self.store.connect() as db:
            db.execute("DROP TABLE cisnet_candidates")
            db.execute("DROP TABLE cisnet_searches")
            db.execute("DROP TABLE cisnet_runs")
            db.execute("DROP TABLE cisnet_baseline")
            db.execute("UPDATE schema_info SET version=11 WHERE id=1")
            db.commit()
        self.store.initialize()
        self.assertEqual(self._db_rows("schema_info")[0]["version"], SCHEMA_VERSION)
        self.assertEqual(len(self._db_rows("aggregation_runs")), 2)
        self.assertEqual(len(self._db_rows("cisnet_searches")), 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])


if __name__ == "__main__":
    unittest.main()
