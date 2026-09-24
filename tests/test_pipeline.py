import asyncio
import contextlib
import io
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import job_store
import receiver
import recognition_worker as worker
import scan


SDK_INFO = {
    "package": "pyacrcloud",
    "version": "1.0.12",
    "native_version": "fixture-native",
    "binary_sha256": "f" * 64,
    "function": "create_fingerprint_by_filebuffer",
    "options": dict(scan.FINGERPRINT_OPTIONS),
}


def make_wav(path: Path, seconds=2.0, rate=8000, value=b"\x01\x00"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(value * round(seconds * rate))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = job_store.PipelineStore(self.root / "queue" / "pipeline.sqlite3")
        self.store.initialize()
        self.cfg = worker.WorkerConfig(
            enabled=True,
            access_key="fixture-key",
            secret_key="fixture-secret",
            runs_dir=self.root / "runs",
            retry_base_seconds=0,
            retry_max_seconds=0,
        )

    def complete_baseline(self, task_id="task", chat_id="chat", message_ids=None):
        self.store.begin_baseline(task_id, chat_id)
        self.store.complete_baseline(
            task_id, chat_id, list(message_ids or []), "oldest_first"
        )

    def payload(self, message="message-1", text="https://disk.yandex.ru/d/source-one"):
        return {
            "event": "chat_message-created",
            "payload": {
                "companyId": "api-test",
                "chatId": "chat-1",
                "id": message,
                "text": text,
            },
        }

    def test_webhook_secret_must_be_explicitly_configured(self):
        for value in ("", "   ", "change-me-before-start", "replace-with-a-long-random-secret"):
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "WEBHOOK_SECRET must be explicitly configured"
            ):
                receiver.validate_webhook_secret(value)
        self.assertEqual(receiver.validate_webhook_secret(" fixture-secret "), "fixture-secret")

    def test_concurrent_webhooks_write_latest_copy_without_shared_temp_file(self):
        events = self.root / "events"
        barrier = threading.Barrier(2)
        original_replace = Path.replace

        def replace_together(source, target):
            if Path(target) == events / "latest.json":
                barrier.wait(timeout=5)
            return original_replace(source, target)

        with patch.object(receiver, "EVENTS_DIR", events), \
             patch.object(Path, "replace", replace_together), \
             ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(receiver.write_latest_event, {"event": "first"})
            second = pool.submit(receiver.write_latest_event, {"event": "second"})
            first.result(timeout=10)
            second.result(timeout=10)

        self.assertIn(json.loads((events / "latest.json").read_text())["event"],
                      {"first", "second"})
        self.assertEqual(list(events.glob("latest.*.json.tmp")), [])

    def record(self, payload):
        return self.store.record_webhook(
            payload,
            payload.get("event"),
            receiver.event_company_id(payload),
            receiver.event_chat_id(payload),
            receiver.event_message_id(payload),
        )

    def expand(self):
        async def no_network(payload):
            return payload
        async def allow_scope(force=False):
            del force
            return {"chat-1"}, {"chat-1"}

        async def expand_yandex(url):
            return [{"kind": "yandex_disk", "url": url}], []

        async def collect(message, task_id, message_id):
            return await receiver.collect_message_sources(
                message, task_id, message_id, expand_yandex
            )
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(receiver.process_one_webhook(
                self.store, no_network, allow_scope, collect
            ))

    def test_webhook_refreshes_stale_scope_before_rejecting_moved_card(self):
        payload = self.payload(message="move-event", text="moved")
        self.record(payload)
        scope_calls = []

        async def scope(force=False):
            scope_calls.append(force)
            if force:
                return {"chat-1"}, {"chat-1"}
            return set(), set()

        async def no_network(value):
            return value

        async def no_sources(message, task_id, message_id):
            return []

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(asyncio.run(receiver.process_one_webhook(
                self.store, no_network, scope, no_sources
            )))
        self.assertEqual(scope_calls, [False, True])
        self.assertNotIn("reason=scope", output.getvalue())
        with self.store.connect() as db:
            row = db.execute(
                "SELECT stage FROM webhook_events WHERE message_id='move-event'"
            ).fetchone()
        self.assertEqual(row[0], "no_supported_url")

    def test_system_move_chat_event_does_not_repeat_task_moved_poll(self):
        payload = self.payload(message="move-1", text="-")
        payload["payload"]["properties"] = {
            "move": True,
            "fromSystem": True,
            "taskId": "chat-1",
            "from": "outside",
            "to": "allowed-column",
        }
        self.record(payload)
        polls = []

        async def scope(force=False):
            del force
            return {"chat-1"}, {"chat-1"}

        async def no_network(value):
            return value

        async def no_sources(message, task_id, message_id):
            return []

        async def poll(store, fetch, task_id, chat_id):
            polls.append((task_id, chat_id))
            return receiver.PollResult(
                0, messages_read=0, messages_skipped=0, messages_new=0
            )

        async def inline(function, *args):
            return function(*args)

        with patch.object(receiver, "YOUGILE_ALLOWED_COLUMN_IDS", {"allowed-column"}), \
             patch.object(receiver.asyncio, "to_thread", new=inline), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_webhook(
                self.store, no_network, scope, no_sources, poll
            )))
        self.assertEqual(polls, [])
        self.assertEqual(
            self.store.scope_run_prep_hash("chat-1", "chat-1", receiver.PREP_CONFIG_HASH),
            receiver.PREP_CONFIG_HASH,
        )
        with self.store.connect() as db:
            row = db.execute(
                "SELECT stage FROM webhook_events WHERE message_id='move-1'"
            ).fetchone()
            self.assertEqual(row["stage"], "no_supported_url")
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM chat_notifications").fetchone()[0], 0
            )

    def test_task_moved_webhook_uses_only_target_column_and_polls_card(self):
        payload = {
            "event": "task-moved",
            "payload": {
                "companyId": "api-test",
                "id": "chat-1",
                "columnId": "allowed-column",
            },
        }
        self.record(payload)
        polls = []

        async def no_network(value):
            return value

        async def no_sources(*args):
            raise AssertionError("task-moved must inspect the card, not the event body")

        async def poll(store, fetch, task_id, chat_id):
            polls.append((task_id, chat_id))
            return receiver.PollResult(
                0, messages_read=0, messages_skipped=0, messages_new=0
            )

        async def unexpected_task_lookup(task_id):
            raise AssertionError(f"target column should decide scope for {task_id}")

        with patch.object(receiver, "YOUGILE_ALLOWED_COLUMN_IDS", {"allowed-column"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_webhook(
                self.store, no_network, None, no_sources, poll, unexpected_task_lookup
            )))
        self.assertEqual(polls, [("chat-1", "chat-1")])
        with self.store.connect() as db:
            event = db.execute("SELECT chat_id,stage FROM webhook_events").fetchone()
        self.assertEqual((event["chat_id"], event["stage"]), ("chat-1", "no_supported_url"))

    def test_task_moved_webhook_ignores_other_target_column(self):
        payload = {
            "event": "task-moved",
            "payload": {
                "companyId": "api-test",
                "taskId": "chat-1",
                "to": "other-column",
            },
        }
        self.record(payload)
        polls = []

        async def poll(*args):
            polls.append(args)

        with patch.object(receiver, "YOUGILE_ALLOWED_COLUMN_IDS", {"allowed-column"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_webhook(
                self.store, move_poll=poll
            )))
        self.assertEqual(polls, [])
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT stage FROM webhook_events").fetchone()[0],
                "no_supported_url",
            )

    def test_webhook_task_scope_checks_only_the_event_task(self):
        calls = []

        async def get_json(path, *, request_kind):
            calls.append((path, request_kind))
            return {"id": "chat-1", "columnId": "allowed-column"}

        with patch.object(receiver, "YOUGILE_ALLOWED_COLUMN_IDS", {"allowed-column"}), \
             patch.object(receiver, "YOUGILE_ALLOWED_BOARD_IDS", set()), \
             patch.object(receiver, "YOUGILE_ALLOWED_TASK_IDS", set()), \
             patch.object(receiver, "YOUGILE_ALLOWED_CHAT_IDS", set()):
            self.assertTrue(asyncio.run(receiver.webhook_task_in_scope("chat-1", get_json)))
        self.assertEqual(calls, [("/tasks/chat-1", "scope_task")])

    def test_pipeline_notification_does_not_rearm_or_poll_chat(self):
        payload = self.payload(
            message="server-message",
            text="Сервер: Ссылка получена",
        )
        payload["payload"]["properties"] = {
            "move": True,
            "fromSystem": True,
            "taskId": "chat-1",
            "from": "outside",
            "to": "allowed-column",
        }
        self.record(payload)
        polls = []

        async def scope(force=False):
            del force
            return {"chat-1"}, {"chat-1"}

        async def no_network(value):
            return value

        async def no_sources(*args):
            raise AssertionError("pipeline notification must not collect sources")

        async def poll(*args):
            polls.append(args)

        with patch.object(receiver, "YOUGILE_ALLOWED_COLUMN_IDS", {"allowed-column"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_webhook(
                self.store, no_network, scope, no_sources, poll
            )))

        self.assertEqual(polls, [])
        self.assertEqual(
            self.store.scope_run_prep_hash("chat-1", "chat-1", receiver.PREP_CONFIG_HASH),
            receiver.PREP_CONFIG_HASH,
        )

    def test_same_file_is_not_reprocessed_after_second_scope_entry(self):
        self.complete_baseline("task", "chat")
        messages = [{
            "id": "message-1",
            "text": "/root/#file:/user-data/file-id/audio.wav",
        }]

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        calls = []

        def transport(*args):
            calls.append(1)
            return 200, b'{"status":{"code":1001}}'

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20
            )), 1)
        self.assertTrue(self.prepare_next())
        self.assertEqual(self.run_worker(transport=transport), "complete_no_match")

        self.assertTrue(self.store.rearm_scope_run("task", "chat", "move-2"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20
            )), 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 1)
        self.assertEqual(len(calls), 1)

    def prepare_next(self, wav_bytes_value=b"\x01\x00", seconds=2.0):
        async def fake_prepare(job, store):
            path = self.root / "audio" / f"job-{job['id']}.wav"
            store.set_audio_plan(job["id"], job["claim_token"], "fixture.mp4", 123, path)
            make_wav(path, seconds=seconds, value=wav_bytes_value)
            with path.open("rb") as source:
                return path, scan.inspect_audio(source)
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(receiver.process_one_audio(self.store, fake_prepare))

    def run_worker(self, cfg=None, transport=None, fingerprinter=None):
        cfg = cfg or self.cfg
        transport = transport or (lambda *args: (200, b'{"status":{"code":1001}}'))
        fingerprinter = fingerprinter or (lambda sample: b"fixture-fingerprint")
        with patch.object(worker.scan, "load_sdk", return_value=(object(), SDK_INFO)), \
             contextlib.redirect_stdout(io.StringIO()):
            return worker.run_once(self.store, cfg, transport, fingerprinter)

    def seed_ready_audio(self, payload=None, value=b"\x01\x00", seconds=2.0):
        self.record(payload or self.payload())
        self.assertTrue(self.expand())
        self.assertTrue(self.prepare_next(value, seconds))

    def test_full_webhook_to_saved_candidate_result_and_message_link(self):
        self.seed_ready_audio()
        raw = json.dumps({
            "status": {"code": 0},
            "metadata": {"music": [{"title": "fixture", "acrid": "one"}]},
        }).encode()
        self.assertEqual(self.run_worker(transport=lambda *args: (200, raw)), "complete_candidates")
        with self.store.connect() as db:
            event = db.execute("SELECT * FROM webhook_events").fetchone()
            source = db.execute("SELECT * FROM source_jobs").fetchone()
            recognition = db.execute("SELECT * FROM recognitions").fetchone()
        self.assertEqual((event["company_id"], event["chat_id"], event["message_id"]),
                         ("api-test", "chat-1", "message-1"))
        self.assertEqual(source["stage"], "complete_candidates")
        self.assertIsNotNone(source["audio_completed_utc"])
        self.assertIsNotNone(source["completed_utc"])
        self.assertIsNotNone(source["wav_sha256"])
        self.assertIsNotNone(source["result_dir"])
        self.assertEqual(recognition["candidate_rows"], 1)
        result = Path(recognition["result_dir"])
        self.assertTrue((result / "scan.sqlite3").is_file())
        self.assertTrue((result / "message_links.jsonl").is_file())
        job = json.loads((result / "job.json").read_text())
        self.assertFalse(job["candidates_verified"])
        link = json.loads((result / "message_links.jsonl").read_text())
        self.assertEqual(link["message_id"], "message-1")
        self.assertEqual(link["task_id"], "chat-1")
        self.assertEqual(link["source_job_id"], source["id"])
        self.assertEqual(link["recognition_id"], recognition["id"])
        runs = self.store.aggregation_runs_for_recognition(recognition["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["state"], runs[0]["attempts"]), ("complete", 1))

    def test_complete_no_match_is_aggregated_without_candidates(self):
        self.seed_ready_audio()
        self.assertEqual(self.run_worker(), "complete_no_match")
        recognition = self.store.recognition(1)
        runs = self.store.aggregation_runs_for_recognition(recognition["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["state"], "complete")
        result = json.loads(runs[0]["result_json"])
        self.assertEqual(result["appearances"], [])
        self.assertEqual(result["evidence_catalog"]["observations"], [])
        self.assertEqual(len(result["evidence_catalog"]["windows"]), 1)
        self.assertEqual(result["evidence_catalog"]["windows"][0]["status"], "no_result_1001")
        self.assertEqual(self.run_worker(), "idle")
        self.assertEqual(self.store.aggregation_runs_for_recognition(recognition["id"])[0]["attempts"], 1)

    def test_next_worker_iteration_reconciles_post_completion_aggregation_gap(self):
        self.seed_ready_audio()
        with patch.object(worker, "schedule_aggregation"):
            self.assertEqual(self.run_worker(), "complete_no_match")
        recognition = self.store.recognition(1)
        self.assertEqual(self.store.aggregation_runs_for_recognition(recognition["id"]), [])
        self.assertEqual(self.run_worker(), "complete")
        runs = self.store.aggregation_runs_for_recognition(recognition["id"])
        self.assertEqual((len(runs), runs[0]["state"], runs[0]["attempts"]), (1, "complete", 1))
        self.assertEqual(self.run_worker(), "idle")
        self.assertEqual(len(self.store.aggregation_runs_for_recognition(recognition["id"])), 1)

    def test_duplicate_webhook_processes_all_yandex_links_once(self):
        payload = self.payload(text="https://disk.yandex.ru/d/one https://disk.yandex.ru/d/two")
        first_id, first_inserted = self.record(payload)
        second_id, second_inserted = self.record(payload)
        self.assertTrue(first_inserted)
        self.assertFalse(second_inserted)
        self.assertEqual(first_id, second_id)
        self.expand()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT delivery_count FROM webhook_events").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_sources").fetchone()[0], 2)

    def test_collect_message_sources_ignores_metadata_and_keeps_all_yandex_links(self):
        calls = []

        async def expand(url):
            calls.append(url)
            return [{"kind": "yandex_disk", "url": url}], []

        sources = asyncio.run(receiver.collect_message_sources(
            {"text": (
                "https://disk.yandex.ru/d/first "
                "https://www.shazam.com/track/123/reference "
                "https://disk.yandex.ru/d/last"
            )},
            "task", "message", expand,
        ))
        self.assertEqual(calls, [
            "https://disk.yandex.ru/d/first",
            "https://disk.yandex.ru/d/last",
        ])
        self.assertEqual(sources, [
            {"kind": "yandex_disk", "url": "https://disk.yandex.ru/d/first"},
            {"kind": "yandex_disk", "url": "https://disk.yandex.ru/d/last"},
        ])

    def test_forced_webhook_queues_same_link_as_separate_replay(self):
        url = "https://disk.yandex.ru/d/replay-source"
        self.record(self.payload("initial", url))
        self.assertTrue(self.expand())
        self.record(self.payload("forced", f"{url} --forced"))
        self.assertTrue(self.expand())
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id,prep_config_hash FROM source_jobs ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertFalse(job_store.is_forced_run_prep_hash(rows[0]["prep_config_hash"]))
        self.assertTrue(job_store.is_forced_run_prep_hash(rows[1]["prep_config_hash"]))

    def test_new_link_queues_received_notification_before_audio_processing(self):
        self.record(self.payload())
        self.assertTrue(self.expand())
        with self.store.connect() as db:
            row = db.execute(
                "SELECT kind,text,state FROM chat_notifications WHERE kind='link_received'"
            ).fetchone()
        self.assertEqual(tuple(row), ("link_received", "Сервер: Ссылка получена", "pending"))
        self.assertTrue(self.prepare_next())
        with self.store.connect() as db:
            accepted = db.execute(
                "SELECT COUNT(*) FROM chat_notifications WHERE kind='accepted'"
            ).fetchone()[0]
        self.assertEqual(accepted, 0)

    def test_yougile_file_extensions_are_case_insensitive_for_video_and_wav(self):
        for suffix in ("mp4", "MP4", "mov", "MOV", "wav", "WAV"):
            with self.subTest(suffix=suffix):
                refs = receiver.extract_yougile_file_refs({
                    "id": "message-1",
                    "text": f"/root/#file:/user-data/file-id/video.{suffix}",
                })
                self.assertEqual(len(refs), 1)
                self.assertEqual(refs[0]["file_id"], "file-id")
                self.assertEqual(refs[0]["filename"], f"video.{suffix}")
        with self.assertRaises(ValueError):
            receiver.extract_yougile_file_refs({
                "id": "message-2",
                "text": "/root/#file:/user-data/file-id/document.txt",
            })

    def test_baseline_marks_initial_history_without_source_jobs_and_is_idempotent(self):
        messages = [{"id": f"history-{index:02d}", "text": "ignored"} for index in range(3)]
        calls = []

        async def fetch(chat_id, limit, offset):
            calls.append(offset)
            return messages[offset:offset + limit]

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            first = asyncio.run(receiver.baseline_yougile_chat(
                self.store, "task", "chat", 20, fetch
            ))
            second = asyncio.run(receiver.baseline_yougile_chat(
                self.store, "task", "chat", 20,
                lambda *args: self.fail("completed baseline must not call YouGile"),
            ))
        self.assertEqual(first["messages_seen"], 3)
        self.assertEqual(first["messages_marked"], 3)
        self.assertEqual(first["source_jobs_created"], 0)
        self.assertEqual(second["status"], "already_complete")
        self.assertEqual(second["messages_marked"], 0)
        self.assertEqual(calls, [0])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM polled_messages WHERE status='baseline'"
            ).fetchone()[0], 3)

    def test_new_mp4_mov_and_wav_are_queued_after_baseline(self):
        self.complete_baseline()
        messages = [
            {"id": "message-01", "text": "/root/#file:/user-data/one/video.MP4"},
            {"id": "message-02", "text": "/root/#file:/user-data/two/video.mov"},
            {"id": "message-03", "text": "/root/#file:/user-data/three/audio.WAV"},
        ]

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        with contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20
            ))
        self.assertEqual(result, 3)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 3)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM polled_messages WHERE status='queued'"
            ).fetchone()[0], 3)

    def test_unsupported_attachment_is_terminal_once(self):
        self.complete_baseline()
        messages = [{
            "id": "message-01",
            "text": "/root/#file:/user-data/file-id/document.txt",
        }]
        collected = []

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, current_id):
            collected.append(current_id)
            return await receiver.collect_message_sources(message, task_id, current_id)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 0)
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 0)
        self.assertEqual(collected, ["message-01"])
        record = self.store.polled_message_records("task", "chat")["message-01"]
        self.assertEqual(record["status"], "terminal_skipped")
        self.assertEqual(record["reason_type"], "ValueError")
        self.assertEqual(record["reason_code"], "unsupported_source")

    def test_terminal_value_error_does_not_block_following_message(self):
        self.complete_baseline()
        messages = [
            {"id": "message-01", "text": "broken"},
            {"id": "message-02", "text": "valid"},
        ]

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, current_id):
            if current_id == "message-01":
                raise ValueError("Invalid file reference")
            return [{"kind": "direct_video", "url": "https://files.example/video.mp4"}]

        with contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            ))
        self.assertEqual(result, 1)
        records = self.store.polled_message_records("task", "chat")
        self.assertEqual(records["message-01"]["status"], "terminal_skipped")
        self.assertEqual(records["message-02"]["status"], "queued")
        self.assertEqual(self.store.poller_chat_position("task", "chat")["next_offset"], 2)

    def test_baseline_stabilizes_addition_and_deletion_between_pages(self):
        messages = [{"id": f"message-{index:02d}", "text": "history"} for index in range(23)]
        calls = 0

        async def fetch(chat_id, limit, offset):
            nonlocal calls
            calls += 1
            page = list(messages[offset:offset + limit])
            if calls == 1:
                del messages[5]
            elif calls == 3:
                messages.append({"id": "message-23", "text": "history"})
            return page

        with contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(receiver.baseline_yougile_chat(
                self.store, "task", "chat", 20, fetch
            ))
        self.assertGreaterEqual(result["passes"], 3)
        self.assertEqual(result["messages_seen"], 23)
        self.assertEqual(set(self.store.polled_message_ids("task", "chat")), {
            message["id"] for message in messages
        })
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 0)

    def test_poller_stabilizes_addition_and_deletion_between_pages(self):
        baseline = [{"id": f"message-{index:02d}", "text": "history"} for index in range(20)]
        self.store.begin_baseline("task", "chat")
        self.store.complete_baseline(
            "task", "chat", [message["id"] for message in baseline], "oldest_first"
        )
        messages = baseline + [
            {"id": f"message-{index:02d}", "text": f"https://files.example/{index}.mp4"}
            for index in range(20, 43)
        ]
        calls = 0

        async def fetch(chat_id, limit, offset):
            nonlocal calls
            calls += 1
            page = list(messages[offset:offset + limit])
            if calls == 1:
                del messages[5]
            elif calls == 4:
                messages.append({
                    "id": "message-43", "text": "https://files.example/43.mp4"
                })
            return page

        async def collect(message, task_id, current_id):
            return [{"kind": "direct_video", "url": message["text"]}]

        with contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            ))
        self.assertEqual(result, 24)
        records = self.store.polled_message_records("task", "chat")
        self.assertTrue(all(f"message-{index:02d}" in records for index in range(20, 44)))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 24)

    def test_newly_scoped_chat_processes_existing_history_once(self):
        output = io.StringIO()
        messages = [
            {"id": "message-00", "text": "card created"},
            {"id": "message-01", "text": "https://files.example/existing.mp4"},
            {"id": "message-02", "text": "card moved into trigger column"},
        ]
        calls = []

        async def fetch(chat_id, limit, offset):
            calls.append((chat_id, limit, offset))
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            if message_id != "message-01":
                return []
            return [{"kind": "direct_video", "url": message["text"]}]

        with contextlib.redirect_stdout(output):
            result = asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "new-task", "new-chat", 20, collect
            ))
            second = asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "new-task", "new-chat", 20, collect
            ))
        self.assertEqual(result, 1)
        self.assertEqual(second, 0)
        self.assertFalse(result.baseline_required)
        self.assertIn("state=activated_from_start", output.getvalue())
        self.assertEqual(self.store.baseline_state("new-task", "new-chat")["status"], "complete")
        self.assertEqual(set(self.store.polled_message_ids("new-task", "new-chat")), {
            "message-01", "message-02",
        })
        self.assertEqual(calls, [("new-chat", 20, 0), ("new-chat", 20, 0)])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 1)

    def test_scope_entry_queues_only_newest_history_source(self):
        messages = [
            {"id": "message-01", "text": "https://files.example/old.mp4"},
            {"id": "message-02", "text": "https://files.example/new.mov"},
        ]
        collected = []

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            collected.append(message_id)
            return [{"kind": "direct_video", "url": message["text"]}]

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        self.assertEqual(collected, ["message-02"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT source_url FROM source_jobs").fetchone()[0], messages[1]["text"])

    def test_scope_entry_ignores_comments_after_newest_source(self):
        messages = [
            {"id": "message-01", "text": "https://files.example/old.mp4"},
            {"id": "message-02", "text": "https://files.example/new.mp4"},
            {"id": "message-03", "text": "thanks"},
        ]
        collected = []

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            collected.append(message_id)
            return [] if message_id == "message-03" else [{"kind": "direct_video", "url": message["text"]}]

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        self.assertEqual(collected, ["message-03", "message-02"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT source_url FROM source_jobs").fetchone()[0], messages[1]["text"])

    def test_scope_reentry_only_queues_a_newer_source(self):
        messages = [{"id": "message-01", "text": "https://files.example/old.mp4"}]

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            return [{"kind": "direct_video", "url": message["text"]}]

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        self.assertTrue(self.store.rearm_scope_run("task", "chat", "move-2"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 0)
        messages.append({"id": "message-02", "text": "https://files.example/new.mp4"})
        self.assertTrue(self.store.rearm_scope_run("task", "chat", "move-3"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT source_url FROM source_jobs ORDER BY id DESC").fetchone()[0], messages[-1]["text"])

    def test_new_source_is_polled_while_task_remains_in_scope(self):
        messages = [{"id": "first", "text": "https://files.example/first.mp4"}]

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            return [{"kind": "direct_video", "url": message["text"]}]

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        messages.append({"id": "second", "text": "https://files.example/second.mp4"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 2)

    def test_scope_entry_treats_yandex_folder_as_one_source(self):
        messages = [{"id": "folder", "text": "folder"}]
        folder = {
            "kind": "yandex_disk_group", "url": "https://disk.yandex.ru/d/folder",
            "filename": "folder", "items": [
                {"item_path": "disk:/folder/one.mp4", "filename": "one.mp4"},
                {"item_path": "disk:/folder/two.mov", "filename": "two.mov"},
            ],
        }

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            return [folder]

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT source_kind FROM source_jobs").fetchone()[0], "yandex_disk_group")

    def test_manual_baseline_in_progress_is_not_auto_activated(self):
        self.store.begin_baseline("new-task", "new-chat")

        async def forbidden_fetch(*args):
            self.fail("poller must not race an in-progress manual baseline")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = asyncio.run(receiver.poll_yougile_chat_once(
                self.store, forbidden_fetch, "new-task", "new-chat", 20
            ))
        self.assertEqual(result, 0)
        self.assertTrue(result.baseline_required)
        self.assertIn("state=baseline_in_progress queued=0", output.getvalue())

    def test_poller_queues_only_configured_chat_files_once_across_restart(self):
        calls = []

        messages = [
                {"id": "message-00", "text": "ordinary text"},
                {"id": "message-01", "text": "/root/#file:/user-data/file-one/video.MP4"},
                {"id": "message-02", "text": "/root/#file:/user-data/file-two/video.mov"},
        ]

        async def fake_fetch(chat_id, limit, offset):
            calls.append((chat_id, limit, offset))
            return messages[offset:offset + limit]

        self.complete_baseline("target-task", "target-chat")
        args = (self.store, fake_fetch, "target-task", "target-chat", 20)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(*args)), 2)
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(*args)), 0)
        reopened = job_store.PipelineStore(self.store.path)
        reopened.initialize()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                reopened, fake_fetch, "target-task", "target-chat", 20
            )), 0)
        self.assertTrue(all(call[:2] == ("target-chat", 20) for call in calls))
        with reopened.connect() as db:
            files = db.execute(
                "SELECT task_id,chat_id,message_id,file_id,file_path FROM polled_yougile_files "
                "ORDER BY message_id"
            ).fetchall()
            sources = db.execute(
                "SELECT source_kind,source_url,stage FROM source_jobs ORDER BY id"
            ).fetchall()
            links = db.execute("SELECT COUNT(*) FROM message_sources").fetchone()[0]
        self.assertEqual(len(files), 2)
        self.assertTrue(all(row["task_id"] == "target-task" for row in files))
        self.assertTrue(all(row["chat_id"] == "target-chat" for row in files))
        self.assertEqual([row["file_id"] for row in files], ["file-one", "file-two"])
        self.assertEqual([row["source_kind"] for row in sources], ["yougile_file"] * 2)
        self.assertTrue(all(row["source_url"].startswith("/user-data/") for row in sources))
        self.assertTrue(all(row["stage"] == "queued" for row in sources))
        self.assertEqual(links, 2)

    def test_poller_paginates_more_than_twenty_messages(self):
        messages = [
            {"id": f"message-{index:02d}",
             "text": f"/root/#file:/user-data/file-{index:02d}/video-{index:02d}.mp4"}
            for index in range(23)
        ]
        calls = []

        async def fetch(chat_id, limit, offset):
            calls.append((limit, offset))
            return messages[offset:offset + limit]

        self.complete_baseline()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20
            )), 23)
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20
            )), 0)
        self.assertIn((20, 0), calls)
        self.assertIn((20, 20), calls)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 23)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM polled_messages").fetchone()[0], 23)
            state = db.execute("SELECT * FROM poller_chat_state").fetchone()
        self.assertEqual(state["next_offset"], 23)
        self.assertEqual(state["last_message_id"], "message-22")

    def test_completed_polled_message_skips_external_source_collection(self):
        messages = [{"id": "old-message", "text": "https://disk.yandex.ru/d/old"}]
        collected = []

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            collected.append(message_id)
            return [{"kind": "yandex_disk", "url": message["text"]}]

        self.complete_baseline()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        self.store.set_poller_chat_position("task", "chat", 0, None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 0)
        self.assertEqual(collected, ["old-message"])
        self.assertNotIn("[QUEUE]", output.getvalue())
        self.assertIn("messages_new=0 queued=0", output.getvalue())

    def test_polled_message_with_error_is_retried(self):
        messages = [{"id": "retry-message", "text": "fixture"}]
        attempts = []

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            attempts.append(message_id)
            if len(attempts) == 1:
                raise OSError("temporary source lookup failure")
            return [{"kind": "direct_video", "url": "https://files.example/video.mp4"}]

        self.complete_baseline()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 0)
        self.assertNotIn("retry-message", self.store.polled_message_ids("task", "chat"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 1)
        self.assertEqual(attempts, ["retry-message", "retry-message"])
        self.assertIn("retry-message", self.store.polled_message_ids("task", "chat"))

    def test_multiple_sources_are_all_linked_before_message_is_complete(self):
        messages = [{"id": "multi-message", "text": "fixture"}]
        attempts = 0

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary collection failure")
            return [
                {"kind": "direct_video", "url": "https://files.example/one.mp4"},
                {"kind": "direct_video", "url": "https://files.example/two.mp4"},
            ]

        self.complete_baseline()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 0)
        self.assertNotIn("multi-message", self.store.polled_message_ids("task", "chat"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM polled_message_sources").fetchone()[0], 0)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20, collect
            )), 2)
        self.assertIn("multi-message", self.store.polled_message_ids("task", "chat"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM polled_message_sources").fetchone()[0], 2)

    def test_yougile_wav_lower_and_upper_are_normalized_then_source_is_removed(self):
        sources = [
            {"kind": "yougile_file", "url": "/user-data/wav-lower/clip.wav",
             "filename": "clip.wav", "file_id": "wav-lower"},
            {"kind": "yougile_file", "url": "/user-data/wav-upper/CLIP.WAV",
             "filename": "CLIP.WAV", "file_id": "wav-upper"},
        ]
        self.store.enqueue_polled_sources(
            "task", "chat", "wav-message", sources, receiver.PREP_CONFIG_HASH
        )
        incoming = self.root / "incoming"
        audio = self.root / "audio"
        incoming.mkdir()
        audio.mkdir()

        async def download(url, *, authorize_yougile, filename_hint, rate_limiter=None,
                           source_job_id=None):
            path = incoming / filename_hint
            make_wav(path, seconds=2.0, rate=8000)
            return path

        with patch.object(receiver, "INCOMING_DIR", incoming), \
             patch.object(receiver, "AUDIO_DIR", audio), \
             patch.object(receiver, "download_video", new=download), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_audio(self.store)))
            self.assertTrue(asyncio.run(receiver.process_one_audio(self.store)))
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM source_jobs ORDER BY id").fetchall()
        self.assertEqual([row["stage"] for row in rows], ["audio_ready", "audio_ready"])
        self.assertEqual(list(incoming.iterdir()), [])
        for row in rows:
            info = receiver.inspect_completed_wav(Path(row["wav_path"]))
            self.assertEqual(info["sample_rate"], 44100)
            self.assertGreater(info["duration_seconds"], 1.9)

    def test_corrupt_yougile_wav_is_rejected_and_kept_with_safe_value_error_log(self):
        source = {"kind": "yougile_file", "url": "/user-data/broken/broken.WAV",
                  "filename": "broken.WAV", "file_id": "broken"}
        self.store.enqueue_polled_sources(
            "task", "chat", "broken-message", [source], receiver.PREP_CONFIG_HASH
        )
        incoming = self.root / "incoming"
        audio = self.root / "audio"
        incoming.mkdir()
        audio.mkdir()

        async def download(url, *, authorize_yougile, filename_hint, rate_limiter=None,
                           source_job_id=None):
            path = incoming / filename_hint
            path.write_bytes(b"not-a-wav")
            return path

        output = io.StringIO()
        with patch.object(receiver, "INCOMING_DIR", incoming), \
             patch.object(receiver, "AUDIO_DIR", audio), \
             patch.object(receiver, "download_video", new=download), \
             contextlib.redirect_stdout(output):
            self.assertTrue(asyncio.run(receiver.process_one_audio(self.store)))
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM source_jobs").fetchone()
        self.assertEqual(row["stage"], "audio_error")
        self.assertEqual(row["error_type"], "ValueError")
        self.assertTrue(Path(row["local_source_path"]).is_file())
        self.assertFalse(Path(row["wav_path"]).exists())
        log = output.getvalue()
        self.assertIn('detail="Invalid WAV: file is too small"', log)
        self.assertNotIn("https://", log)
        self.assertNotIn(str(incoming), log)

    def test_repeated_wav_message_does_not_create_duplicate_source(self):
        messages = [{
            "id": "same-wav-message",
            "text": "/root/#file:/user-data/same-wav/audio.WAV",
        }]

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        self.complete_baseline()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20
            )), 1)
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "task", "chat", 20
            )), 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM polled_message_sources").fetchone()[0], 1)

    def test_wav_backfill_reads_all_pages_and_queues_only_new_wav_sources(self):
        messages = [
            {"id": f"wav-{index:02d}",
             "text": f"/root/#file:/user-data/wav-{index:02d}/audio-{index:02d}.wav"}
            for index in range(23)
        ]
        messages.extend([
            {"id": "wav-repeat", "text": "/root/#file:/user-data/wav-00/audio-00.wav"},
            {"id": "old-video", "text": "/root/#file:/user-data/video/old.mp4"},
        ])
        calls = []

        async def fetch(chat_id, limit, offset):
            calls.append(offset)
            return messages[offset:offset + limit]

        with contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(receiver.backfill_yougile_wav_history(
                self.store, "task", "chat", 20, fetch
            ))
        self.assertEqual(calls, [0, 20])
        self.assertEqual(result["messages"], 25)
        self.assertEqual(result["wav_found"], 24)
        self.assertEqual(result["source_jobs_created"], 23)
        self.assertEqual(len(result["source_job_ids"]), 23)
        with self.store.connect() as db:
            rows = db.execute("SELECT source_filename FROM source_jobs ORDER BY id").fetchall()
        self.assertEqual(len(rows), 23)
        self.assertTrue(all(Path(row[0]).suffix.lower() == ".wav" for row in rows))

    def test_remove_local_source_never_unlinks_the_output_path(self):
        incoming = self.root / "incoming"
        incoming.mkdir()
        output = incoming / "normalized.wav"
        make_wav(output)
        with patch.object(receiver, "INCOMING_DIR", incoming):
            receiver.remove_local_source(output, output)
        self.assertTrue(output.is_file())

    def test_safe_filename_preserves_wav_extension_when_truncated(self):
        result = receiver.safe_filename("x" * 240 + ".WAV", "fallback.wav")
        self.assertEqual(len(result), 180)
        self.assertTrue(result.endswith(".WAV"))

    def test_safe_value_error_text_redacts_urls_and_credentials(self):
        error = ValueError(
            "failed https://files.example/private?token=url-secret "
            "Authorization: Bearer header-secret token=plain-secret"
        )
        text = receiver.safe_value_error_text(error)
        self.assertNotIn("files.example", text)
        self.assertNotIn("url-secret", text)
        self.assertNotIn("header-secret", text)
        self.assertNotIn("plain-secret", text)
        self.assertIn("[redacted-url]", text)
        self.assertIn("[redacted-credential]", text)

    def test_http_status_log_suffix_exposes_only_status(self):
        request = receiver.httpx.Request("GET", "https://yougile.example/private")
        response = receiver.httpx.Response(403, request=request, text="secret response")
        error = receiver.httpx.HTTPStatusError("forbidden", request=request, response=response)
        self.assertEqual(receiver.http_status_log_suffix(error), " status=403")
        self.assertEqual(receiver.http_status_log_suffix(ValueError("not http")), "")

    def test_yougile_download_uses_auth_and_enforces_content_length(self):
        captured = {}

        class Limiter:
            def __init__(self):
                self.acquired = []
                self.deferred = []

            async def acquire(self, kind):
                self.acquired.append(kind)

            async def defer(self, seconds):
                self.deferred.append(seconds)

        class Response:
            def __init__(self, length):
                self.headers = {"content-length": str(length), "content-type": "video/quicktime"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self, size):
                yield b"abcd"

        class Client:
            def __init__(self, length):
                self.length = length

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def stream(self, method, url, headers):
                captured.update(method=method, url=url, headers=headers)
                return Response(self.length)

        incoming = self.root / "incoming"
        incoming.mkdir()
        url = "https://yougile.example/user-data/file-id/video.MOV"
        limiter = Limiter()
        with patch.object(receiver, "INCOMING_DIR", incoming), \
             patch.object(receiver, "YOUGILE_API_BASE", "https://yougile.example/api-v2"), \
             patch.object(receiver, "YOUGILE_API_KEY", "fixture-key"), \
             patch.object(receiver, "MAX_VIDEO_BYTES", 10), \
             patch.object(receiver.httpx, "AsyncClient", return_value=Client(4)):
            path = asyncio.run(receiver.download_video(
                url, authorize_yougile=True, filename_hint="video.MOV", rate_limiter=limiter
            ))
        self.assertEqual(path.read_bytes(), b"abcd")
        self.assertEqual(captured["headers"], {"Authorization": "Bearer fixture-key"})
        with patch.object(receiver, "YOUGILE_API_BASE", "https://yougile.example/api-v2"), \
             patch.object(receiver, "YOUGILE_API_KEY", "fixture-key"), \
             patch.object(receiver, "MAX_VIDEO_BYTES", 3), \
             patch.object(receiver.httpx, "AsyncClient", return_value=Client(4)):
            with self.assertRaises(ValueError):
                asyncio.run(receiver.download_video(
                    url, authorize_yougile=True, filename_hint="video.MOV", rate_limiter=limiter
                ))
        self.assertEqual(limiter.acquired, ["file_download", "file_download"])

    def test_schema_one_is_migrated_to_poller_schema(self):
        path = self.root / "legacy" / "pipeline.sqlite3"
        path.parent.mkdir()
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE schema_info (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_info VALUES (1, 1)")
        migrated = job_store.PipelineStore(path)
        migrated.initialize()
        with migrated.connect() as db:
            self.assertEqual(
                db.execute("SELECT version FROM schema_info WHERE id=1").fetchone()[0],
                job_store.SCHEMA_VERSION,
            )
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='polled_yougile_files'"
            ).fetchone())

    def test_schema_three_is_migrated_to_task_link_schema(self):
        path = self.root / "legacy-three" / "pipeline.sqlite3"
        legacy = job_store.PipelineStore(path)
        with patch.object(job_store, "SCHEMA_VERSION", 3):
            legacy.initialize()
        migrated = job_store.PipelineStore(path)
        migrated.initialize()
        with migrated.connect() as db:
            self.assertEqual(
                db.execute("SELECT version FROM schema_info WHERE id=1").fetchone()[0],
                job_store.SCHEMA_VERSION,
            )

    def test_schema_six_migrates_baseline_state_without_losing_polled_messages(self):
        path = self.root / "schema-six.sqlite3"
        legacy = job_store.PipelineStore(path)
        legacy.initialize()
        with legacy.connect() as db:
            db.execute(
                "INSERT INTO polled_messages "
                "(task_id,chat_id,message_id,checked_utc,status) "
                "VALUES ('task','chat','message-01','fixture','queued')"
            )
            db.execute("DROP TABLE poller_chat_baselines")
            db.execute("ALTER TABLE polled_messages RENAME TO polled_messages_v7")
            db.execute(
                "CREATE TABLE polled_messages ("
                "task_id TEXT NOT NULL,chat_id TEXT NOT NULL,message_id TEXT NOT NULL,"
                "checked_utc TEXT NOT NULL,PRIMARY KEY(task_id,chat_id,message_id))"
            )
            db.execute(
                "INSERT INTO polled_messages SELECT task_id,chat_id,message_id,checked_utc "
                "FROM polled_messages_v7"
            )
            db.execute("DROP TABLE polled_messages_v7")
            db.execute("UPDATE schema_info SET version=6 WHERE id=1")
            db.commit()
        migrated = job_store.PipelineStore(path)
        migrated.initialize()
        with migrated.connect() as db:
            self.assertEqual(
                db.execute("SELECT version FROM schema_info").fetchone()[0],
                job_store.SCHEMA_VERSION,
            )
            row = db.execute(
                "SELECT message_id,status,reason_code FROM polled_messages"
            ).fetchone()
            self.assertEqual(tuple(row), ("message-01", "processed", None))
            self.assertIsNotNone(db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='poller_chat_baselines'"
            ).fetchone())
            columns = {row[1] for row in db.execute("PRAGMA table_info(message_sources)")}
        self.assertIn("task_id", columns)

    def test_schema_eight_migrates_audio_retry_and_pipeline_status(self):
        path = self.root / "schema-eight.sqlite3"
        legacy = job_store.PipelineStore(path)
        legacy.initialize()
        with legacy.connect() as db:
            db.execute("ALTER TABLE source_jobs DROP COLUMN audio_attempts")
            db.execute("ALTER TABLE source_jobs DROP COLUMN next_attempt_epoch")
            db.execute("ALTER TABLE polled_messages DROP COLUMN pipeline_status")
            db.execute("ALTER TABLE polled_messages DROP COLUMN pipeline_updated_utc")
            db.execute("UPDATE schema_info SET version=8 WHERE id=1")
            db.commit()
        migrated = job_store.PipelineStore(path)
        migrated.initialize()
        with migrated.connect() as db:
            self.assertEqual(
                db.execute("SELECT version FROM schema_info").fetchone()[0],
                job_store.SCHEMA_VERSION,
            )
            source_columns = {row[1] for row in db.execute("PRAGMA table_info(source_jobs)")}
            polled_columns = {row[1] for row in db.execute("PRAGMA table_info(polled_messages)")}
        self.assertTrue({"audio_attempts", "next_attempt_epoch"} <= source_columns)
        self.assertTrue({"pipeline_status", "pipeline_updated_utc"} <= polled_columns)

    def test_yandex_folder_is_one_naturally_ordered_source_and_one_result(self):
        calls = []

        async def fetch(params):
            calls.append(dict(params))
            if "offset" not in params:
                return {"type": "dir", "name": "Test folder"}
            if params["offset"] == 0:
                return {"_embedded": {"total": 4, "items": [
                    {"type": "file", "name": "one.mp4", "path": "disk:/folder/one.mp4", "size": 10},
                    {"type": "file", "name": "notes.txt", "path": "disk:/folder/notes.txt", "size": 5},
                    {"type": "dir", "name": "nested", "path": "disk:/folder/nested"},
                ]}}
            return {"_embedded": {"total": 4, "items": [
                {"type": "file", "name": "TWO.MOV", "path": "disk:/folder/TWO.MOV", "size": 20},
            ]}}

        sources, skipped = asyncio.run(receiver.expand_yandex_public_source(
            "https://disk.yandex.ru/d/folder", fetch
        ))
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["kind"], "yandex_disk_group")
        self.assertEqual(sources[0]["filename"], "Test folder")
        self.assertEqual([item["filename"] for item in sources[0]["items"]], [
            "one.mp4", "TWO.MOV"
        ])
        self.assertEqual([item["item_path"] for item in sources[0]["items"]], [
            "disk:/folder/one.mp4", "disk:/folder/TWO.MOV"
        ])
        self.assertEqual(skipped, 2)
        self.assertEqual([call.get("offset") for call in calls], [None, 0, 3])
        queued = self.store.enqueue_polled_sources(
            "task", "chat", "folder-message", sources, receiver.PREP_CONFIG_HASH
        )
        self.assertEqual(len(queued), 1)

        async def resolve(public_url, *, item_path, filename_hint, size_hint, unlimited):
            return filename_hint, size_hint, "download://" + Path(item_path).name

        def extract(download_url, path):
            seconds = 2 if download_url.endswith("one.mp4") else 3
            make_wav(path, seconds=seconds, rate=44100)
            return path

        audio_dir = self.root / "audio"
        audio_dir.mkdir()
        with patch.object(receiver, "AUDIO_DIR", audio_dir), \
             patch.object(receiver, "resolve_yandex_disk_video", new=resolve), \
             patch.object(receiver, "extract_audio_from_url", new=extract), \
             patch.object(receiver, "ensure_free_space", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_audio(self.store)))
        with self.store.connect() as db:
            source = db.execute("SELECT * FROM source_jobs").fetchone()
        self.assertEqual(source["stage"], "audio_ready")
        self.assertAlmostEqual(source["wav_duration"], 17.0, places=3)
        manifest = json.loads(source["source_manifest_json"])
        self.assertEqual([item["filename"] for item in manifest["items"]], [
            "one.mp4", "TWO.MOV"
        ])
        self.assertEqual(manifest["items"][1]["start_seconds"], 14.0)
        self.assertEqual(self.run_worker(), "complete_no_match")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 1)
            result_dir = Path(db.execute("SELECT result_dir FROM recognitions").fetchone()[0])
        sources_result = json.loads((result_dir / "sources.json").read_text())
        self.assertEqual(len(sources_result["logical_sources"]), 1)
        self.assertEqual(len(sources_result["logical_sources"][0]["items"]), 2)

    def test_migration_does_not_requeue_old_folder_message_as_group(self):
        url = "https://disk.yandex.ru/d/folder"
        legacy = [
            {"kind": "yandex_disk", "url": url, "item_path": "disk:/folder/one.mp4",
             "filename": "one.mp4", "unlimited": True},
            {"kind": "yandex_disk", "url": url, "item_path": "disk:/folder/two.mp4",
             "filename": "two.mp4", "unlimited": True},
        ]
        self.store.enqueue_polled_sources(
            "task", "chat", "old-message", legacy, receiver.PREP_CONFIG_HASH
        )
        grouped = [{
            "kind": "yandex_disk_group", "url": url, "filename": "folder",
            "items": [
                {"item_path": item["item_path"], "filename": item["filename"],
                 "unlimited": True}
                for item in legacy
            ],
        }]
        result = self.store.enqueue_polled_sources(
            "task", "chat", "old-message", grouped, receiver.PREP_CONFIG_HASH
        )
        self.assertEqual(result[0][1], False)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 2)

    def test_webhook_and_poller_share_source_deduplication(self):
        payload = self.payload("same-message", "https://files.example.test/video.mp4")
        self.record(payload)
        self.expand()

        messages = [{"id": "same-message", "text": "https://files.example.test/video.mp4"}]

        async def fetch(chat_id, limit, offset):
            return messages[offset:offset + limit]

        async def collect(message, task_id, message_id):
            return [{"kind": "direct_video", "url": message["text"]}]

        self.complete_baseline("chat-1", "chat-1")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(asyncio.run(receiver.poll_yougile_chat_once(
                self.store, fetch, "chat-1", "chat-1", 20, collect
            )), 0)
        self.assertNotIn("[QUEUE]", output.getvalue())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM polled_message_sources").fetchone()[0], 1)

    def test_round_robin_cursor_survives_restart(self):
        chats = ["chat-e", "chat-a", "chat-d", "chat-b", "chat-c"]
        self.assertEqual(self.store.round_robin_chats(chats, 2), ["chat-a", "chat-b"])
        reopened = job_store.PipelineStore(self.store.path)
        reopened.initialize()
        self.assertEqual(reopened.round_robin_chats(chats, 2), ["chat-c", "chat-d"])
        self.assertEqual(reopened.round_robin_chats(chats, 2), ["chat-e", "chat-a"])

    def test_persistent_yougile_limiter_caps_rolling_minute(self):
        with patch.object(job_store.time, "time", return_value=1000.0):
            for _ in range(40):
                self.assertEqual(self.store.reserve_yougile_request("fixture", 40, 60), 0)
            self.assertGreaterEqual(self.store.reserve_yougile_request("fixture", 40, 60), 59.9)
        reopened = job_store.PipelineStore(self.store.path)
        with patch.object(job_store.time, "time", return_value=1060.1):
            self.assertEqual(reopened.reserve_yougile_request("fixture", 40, 60), 0)

    def test_yougile_429_honors_retry_after(self):
        class Limiter:
            def __init__(self):
                self.acquired = []
                self.deferred = []

            async def acquire(self, kind):
                self.acquired.append(kind)

            async def defer(self, seconds):
                self.deferred.append(seconds)

        class Response:
            status_code = 429
            headers = {"retry-after": "17"}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return Response()

        limiter = Limiter()
        output = io.StringIO()
        with patch.object(receiver, "YOUGILE_LIMITER", limiter), \
             patch.object(receiver, "YOUGILE_API_KEY", "fixture-key"), \
             patch.object(receiver.httpx, "AsyncClient", return_value=Client()), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(receiver.YouGileRateLimited):
                asyncio.run(receiver.yougile_get_json(
                    "/fixture", request_kind="fixture"
                ))
        self.assertEqual(limiter.acquired, ["fixture"])
        self.assertEqual(limiter.deferred, [17.0])
        record = json.loads(output.getvalue().removeprefix(receiver.YOUGILE_HTTP_LOG_PREFIX))
        self.assertEqual(record["direction"], "out")
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["route"], "/<redacted>")
        self.assertEqual(record["status"], 429)

    def test_yougile_http_log_uses_route_template_without_private_values(self):
        output = io.StringIO()
        with patch.object(receiver.time, "monotonic", return_value=10.125), \
             contextlib.redirect_stdout(output):
            receiver.log_yougile_http(
                "out", "POST", "notification_message", 201, 10.0
            )
        line = output.getvalue().strip()
        self.assertTrue(line.startswith(receiver.YOUGILE_HTTP_LOG_PREFIX))
        record = json.loads(line.removeprefix(receiver.YOUGILE_HTTP_LOG_PREFIX))
        self.assertEqual(record, {
            "direction": "out",
            "method": "POST",
            "route": "/chats/{chat_id}/messages",
            "kind": "notification_message",
            "status": 201,
            "duration_ms": 125,
            "outcome": "ok",
        })

    def test_incoming_webhook_http_log_hides_secret_path(self):
        class Url:
            path = "/webhooks/yougile/private-secret"

        class IncomingRequest:
            method = "POST"
            url = Url()

        class Response:
            status_code = 200

        async def call_next(request):
            self.assertIsInstance(request, IncomingRequest)
            return Response()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response = asyncio.run(receiver.safe_yougile_http_audit(
                IncomingRequest(), call_next
            ))
        self.assertEqual(response.status_code, 200)
        line = output.getvalue().strip()
        self.assertNotIn("private-secret", line)
        record = json.loads(line.removeprefix(receiver.YOUGILE_HTTP_LOG_PREFIX))
        self.assertEqual(record["direction"], "in")
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["route"], "/webhooks/yougile/{secret_hidden}")
        self.assertEqual(record["status"], 200)

    def test_allowed_board_column_task_and_chat_scopes_are_combined(self):
        async def fetch_all(path, kind):
            if path == "/columns":
                return [{"id": "column-from-board", "boardId": "board-1"}]
            return [
                {"id": "task-from-board", "columnId": "column-from-board"},
                {"id": "task-from-column", "columnId": "column-1"},
            ]

        cache = {"expires": 0.0, "chat_ids": set(), "task_ids": set()}
        with patch.object(receiver, "YOUGILE_ALLOWED_BOARD_IDS", {"board-1"}), \
             patch.object(receiver, "YOUGILE_ALLOWED_COLUMN_IDS", {"column-1"}), \
             patch.object(receiver, "YOUGILE_ALLOWED_TASK_IDS", {"task-direct"}), \
             patch.object(receiver, "YOUGILE_ALLOWED_CHAT_IDS", {"chat-direct"}), \
             patch.object(receiver, "SCOPE_CACHE", cache), \
             patch.object(receiver, "fetch_all_yougile", new=fetch_all):
            chats, tasks = asyncio.run(receiver.allowed_scope(force=True))
        self.assertEqual(tasks, {"task-direct", "task-from-board", "task-from-column"})
        self.assertEqual(chats, tasks | {"chat-direct"})

    def test_only_one_video_is_claimed_and_local_source_is_removed_after_wav(self):
        sources = [
            {"kind": "yougile_file", "url": "/user-data/file-one/video.MP4",
             "filename": "video.MP4", "file_id": "file-one"},
            {"kind": "yougile_file", "url": "/user-data/file-two/video.MOV",
             "filename": "video.MOV", "file_id": "file-two"},
        ]
        self.store.enqueue_polled_sources(
            "task", "chat", "message", sources, receiver.PREP_CONFIG_HASH
        )
        claimed = self.store.claim_source("first", lease_seconds=60)
        self.assertIsNotNone(claimed)
        second_store = job_store.PipelineStore(self.store.path)
        self.assertIsNone(second_store.claim_source("second", lease_seconds=60))
        self.store.retry_audio(claimed["id"], claimed["claim_token"], "fixture")

        incoming = self.root / "incoming"
        audio = self.root / "audio"
        incoming.mkdir()
        audio.mkdir()

        async def fake_download(*args, **kwargs):
            path = incoming / "downloaded.MP4"
            path.write_bytes(b"video")
            return path

        def fake_extract(video_path, audio_path):
            make_wav(audio_path)
            return audio_path

        with patch.object(receiver, "INCOMING_DIR", incoming), \
             patch.object(receiver, "AUDIO_DIR", audio), \
             patch.object(receiver, "download_video", new=fake_download), \
             patch.object(receiver, "extract_audio", new=fake_extract), \
             patch.object(receiver, "ensure_free_space", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_audio(self.store)))
        with self.store.connect() as db:
            source = db.execute("SELECT * FROM source_jobs ORDER BY id LIMIT 1").fetchone()
        self.assertEqual(source["stage"], "audio_ready")
        self.assertIsNone(source["local_source_path"])
        self.assertEqual(list(incoming.iterdir()), [])

    def test_transient_audio_failure_is_retried_with_backoff(self):
        self.store.enqueue_polled_sources(
            "task", "chat", "message",
            [{"kind": "direct_video", "url": "https://files.example.test/video.mp4"}],
            receiver.PREP_CONFIG_HASH,
        )

        claimed = self.store.claim_source("fixture")
        delay = receiver.audio_retry_delay(receiver.httpx.ConnectError("fixture"), 1)
        self.assertEqual(delay, receiver.AUDIO_RETRY_DELAYS_SECONDS[0])
        self.store.retry_audio(claimed["id"], claimed["claim_token"], "ConnectError", delay)
        with self.store.connect() as db:
            source = db.execute("SELECT * FROM source_jobs").fetchone()
        self.assertEqual(source["stage"], "queued")
        self.assertEqual(source["audio_attempts"], 1)
        self.assertEqual(source["error_type"], "ConnectError")
        self.assertGreater(source["next_attempt_epoch"], time.time())
        self.assertEqual(self.store.counts()["audio_retry_wait"], 1)

    def test_operator_requeues_only_terminal_audio_errors(self):
        self.store.enqueue_polled_sources(
            "task", "chat", "message",
            [{"kind": "direct_video", "url": "https://files.example.test/video.mp4"}],
            receiver.PREP_CONFIG_HASH,
        )
        claimed = self.store.claim_source("fixture")
        self.store.fail_audio(claimed["id"], claimed["claim_token"], "ValueError")

        self.assertEqual(self.store.requeue_audio_errors([claimed["id"]]), [claimed["id"]])
        source = self.store.source(claimed["id"])
        self.assertEqual(source["stage"], "queued")
        self.assertIsNone(source["error_type"])
        self.assertIsNone(source["completed_utc"])
        self.assertEqual(source["audio_attempts"], 1)
        with self.assertRaises(job_store.StoreError):
            self.store.requeue_audio_errors([claimed["id"]])

    def test_operator_full_replay_creates_a_fresh_source_and_keeps_poller_deduplication(self):
        created = self.store.enqueue_polled_sources(
            "task", "chat", "message",
            [{"kind": "direct_video", "url": "https://files.example.test/video.mp4"}],
            receiver.PREP_CONFIG_HASH,
        )
        source_id = created[0][0]
        claimed = self.store.claim_source("fixture")
        self.store.fail_audio(claimed["id"], claimed["claim_token"], "ValueError")

        replay_id = self.store.create_operator_replay(source_id)

        original = self.store.source(source_id)
        replay = self.store.source(replay_id)
        self.assertEqual(original["stage"], "audio_error")
        self.assertEqual(replay["stage"], "queued")
        self.assertEqual(replay["source_url"], original["source_url"])
        self.assertTrue(job_store.is_forced_run_prep_hash(replay["prep_config_hash"]))
        with self.store.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM message_sources WHERE source_job_id=?", (replay_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM polled_message_sources WHERE source_job_id=?", (replay_id,)
                ).fetchone()[0],
                0,
            )

    def test_source_heartbeat_renews_claim_during_preparation(self):
        self.store.enqueue_polled_sources(
            "task", "chat", "message",
            [{"kind": "direct_video", "url": "https://files.example.test/video.mp4"}],
            receiver.PREP_CONFIG_HASH,
        )
        claimed = self.store.claim_source("fixture", lease_seconds=1)
        before = claimed["claim_until"]
        self.store.renew_source_claim(claimed["id"], claimed["claim_token"], lease_seconds=60)
        renewed = self.store.source(claimed["id"])
        self.assertEqual(renewed["stage"], "preparing_audio")
        self.assertEqual(renewed["claim_token"], claimed["claim_token"])
        self.assertGreater(renewed["claim_until"], before)

    def test_polled_message_pipeline_status_tracks_retry_and_recognition_result(self):
        self.store.enqueue_polled_sources(
            "task", "chat", "message",
            [{"kind": "direct_video", "url": "https://files.example.test/video.mp4"}],
            receiver.PREP_CONFIG_HASH,
        )
        claimed = self.store.claim_source("fixture")
        self.store.retry_audio(claimed["id"], claimed["claim_token"], "fixture", 30)
        with self.store.connect() as db:
            status = db.execute("SELECT pipeline_status FROM polled_messages").fetchone()[0]
        self.assertEqual(status, "audio_retry_wait")
        with self.store.connect() as db:
            db.execute("UPDATE source_jobs SET next_attempt_epoch=0")
            db.commit()
        claimed = self.store.claim_source("fixture")
        self.store.set_audio_plan(
            claimed["id"], claimed["claim_token"], "fixture.mp4", 1,
            self.root / "audio" / "fixture.wav",
        )
        info = {"sha256": "a" * 64, "size_bytes": 1, "duration_seconds": 1.0}
        self.store.complete_audio(claimed["id"], claimed["claim_token"], info)
        attaching = self.store.claim_audio_for_recognition("fixture")
        recognition_id = self.store.attach_recognition(
            attaching["id"], attaching["claim_token"], receiver.PREP_CONFIG_HASH,
            self.root / "runs" / "fixture",
        )
        running = self.store.claim_recognition("fixture")
        self.store.complete_recognition(
            recognition_id, running["claim_token"], "complete_no_match", {}
        )
        with self.store.connect() as db:
            status = db.execute("SELECT pipeline_status FROM polled_messages").fetchone()[0]
        self.assertEqual(status, "complete_no_match")
        self.assertEqual(self.store.task_status_counts(), {"complete_no_match": 1})

    def test_same_audio_same_config_reuses_recognition_for_two_messages(self):
        calls = []
        self.seed_ready_audio(self.payload("message-1", "https://disk.yandex.ru/d/one"))
        response = lambda *args: (calls.append(1) or (200, b'{"status":{"code":1001}}'))
        self.assertEqual(self.run_worker(transport=response), "complete_no_match")
        self.seed_ready_audio(self.payload("message-2", "https://disk.yandex.ru/d/two"))
        self.assertEqual(self.run_worker(transport=response), "linked")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_sources WHERE recognition_id IS NOT NULL").fetchone()[0], 2)
            result_dir = Path(db.execute("SELECT result_dir FROM recognitions").fetchone()[0])
        self.assertEqual(len((result_dir / "message_links.jsonl").read_text().splitlines()), 2)
        self.assertEqual(len(calls), 1)

    def test_same_source_new_message_links_to_completed_result(self):
        self.seed_ready_audio(self.payload("message-1", "https://disk.yandex.ru/d/same"))
        calls = []
        response = lambda *args: (calls.append(1) or (200, b'{"status":{"code":1001}}'))
        self.assertEqual(self.run_worker(transport=response), "complete_no_match")
        self.record(self.payload("message-2", "https://disk.yandex.ru/d/same"))
        self.expand()
        self.assertEqual(self.run_worker(transport=response), "links_refreshed")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 1)
            result_dir = Path(db.execute("SELECT result_dir FROM recognitions").fetchone()[0])
        self.assertEqual(len((result_dir / "message_links.jsonl").read_text().splitlines()), 2)
        self.assertEqual(len(calls), 1)

    def test_forced_replay_creates_labeled_wav_and_result_directory(self):
        url = "https://files.example.test/video.mp4"
        forced_prep_hash = job_store.forced_run_prep_hash(
            receiver.PREP_CONFIG_HASH, "task", "chat", "forced-message"
        )
        source_id, inserted = self.store.enqueue_polled_sources(
            "task", "chat", "forced-message", [{"kind": "direct_video", "url": url}],
            forced_prep_hash,
        )[0]
        self.assertTrue(inserted)
        self.assertTrue(receiver.is_forced_replay_message({"text": f"{url} --forced"}))
        claimed = self.store.claim_source("fixture")
        self.assertEqual(claimed["id"], source_id)
        incoming_dir = self.root / "incoming"
        incoming_dir.mkdir()
        video = incoming_dir / "fixture.mp4"
        video.write_bytes(b"fixture")
        audio_dir = self.root / "audio"
        audio_dir.mkdir()

        async def fake_download(source_url):
            self.assertEqual(source_url, url)
            return video

        def fake_extract(source_path, audio_path):
            self.assertEqual(source_path, video)
            make_wav(audio_path, rate=44100)

        with patch.object(receiver, "INCOMING_DIR", incoming_dir), \
             patch.object(receiver, "AUDIO_DIR", audio_dir), \
             patch.object(receiver, "download_video", new=fake_download), \
             patch.object(receiver, "extract_audio", new=fake_extract):
            audio_path, info = asyncio.run(receiver.prepare_claimed_source(claimed, self.store))
        self.assertIn(f"forced-replay-{source_id}", audio_path.name)
        self.store.complete_audio(source_id, claimed["claim_token"], info)
        with patch.object(worker.scan, "load_sdk", return_value=(object(), SDK_INFO)):
            self.assertEqual(worker.attach_one_audio(self.store, self.cfg), "attached")
        recognition = self.store.recognition(1)
        self.assertIn(f"forced-replay-{source_id}", Path(recognition["result_dir"]).name)

    def test_forced_replay_recognition_uses_the_queued_configuration(self):
        forced_prep_hash = job_store.forced_run_prep_hash(
            receiver.PREP_CONFIG_HASH, "task", "chat", "forced-message"
        )
        source_id, inserted = self.store.enqueue_polled_sources(
            "task", "chat", "forced-message",
            [{"kind": "direct_video", "url": "https://files.example.test/video.mp4"}],
            forced_prep_hash,
        )[0]
        self.assertTrue(inserted)
        claimed = self.store.claim_source("fixture")
        path = self.root / "audio" / "forced.wav"
        self.store.set_audio_plan(source_id, claimed["claim_token"], "forced.mp4", 123, path)
        make_wav(path)
        with path.open("rb") as audio:
            self.store.complete_audio(source_id, claimed["claim_token"], scan.inspect_audio(audio))

        calls = []

        def no_match(*args):
            calls.append(1)
            return 200, b'{"status":{"code":1001}}'

        self.assertEqual(self.run_worker(transport=no_match), "complete_no_match")
        self.assertEqual(calls, [1])
        recognition = self.store.recognition(1)
        self.assertTrue((Path(recognition["result_dir"]) / "scan.sqlite3").is_file())

    def test_parallel_claim_allows_only_one_owner(self):
        self.record(self.payload())
        self.expand()
        stores = [job_store.PipelineStore(self.store.path), job_store.PipelineStore(self.store.path)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda pair: pair[0].claim_source(pair[1]), zip(stores, ("one", "two"))))
        self.assertEqual(sum(item is not None for item in claimed), 1)

    def test_expired_claim_is_recovered_after_restart(self):
        self.record(self.payload())
        first = self.store.claim_webhook("first", lease_seconds=0)
        self.assertIsNotNone(first)
        reopened = job_store.PipelineStore(self.store.path)
        second = reopened.claim_webhook("second", lease_seconds=30)
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(second["claim_token"], first["claim_token"])

    def test_receiver_restart_recovers_unexpired_claim(self):
        self.record(self.payload())
        first = self.store.claim_webhook("old-process", lease_seconds=3600)
        self.store.recover_receiver_claims()
        second = self.store.claim_webhook("new-process", lease_seconds=30)
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(second["claim_token"], first["claim_token"])

    def test_worker_restart_recovers_claim_under_global_lock_contract(self):
        self.seed_ready_audio()
        with patch.object(worker.scan, "load_sdk", return_value=(object(), SDK_INFO)):
            self.assertTrue(worker.attach_one_audio(self.store, self.cfg))
        first = self.store.claim_recognition("old-worker", lease_seconds=3600)
        self.assertIsNotNone(first)
        self.store.recover_worker_claims()
        second = self.store.claim_recognition("new-worker", lease_seconds=30)
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(second["claim_token"], first["claim_token"])

    def test_worker_restart_resumes_first_unfinished_window(self):
        self.seed_ready_audio()
        with patch.object(worker.scan, "load_sdk", return_value=(object(), SDK_INFO)):
            self.assertEqual(worker.attach_one_audio(self.store, self.cfg), "attached")
        claimed = self.store.claim_recognition("first-worker", lease_seconds=3600)

        def interrupted(*args):
            raise KeyboardInterrupt

        with patch.object(worker.scan, "load_sdk", return_value=(object(), SDK_INFO)), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            worker.process_recognition(
                self.store, claimed, self.cfg, interrupted, lambda sample: b"fixture-fingerprint"
            )
        self.store.recover_worker_claims()
        self.assertEqual(self.run_worker(), "complete_no_match")
        with self.store.connect() as db:
            result_dir = Path(db.execute("SELECT result_dir FROM recognitions").fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM worker_attempts").fetchone()[0], 2)
        with sqlite3.connect(result_dir / "scan.sqlite3") as scan_db:
            self.assertEqual(
                scan_db.execute("SELECT COUNT(*) FROM attempts WHERE window_index=0").fetchone()[0],
                2,
            )
            self.assertEqual(
                scan_db.execute("SELECT COUNT(*) FROM attempts WHERE state='done'").fetchone()[0],
                1,
            )

    def test_crash_after_wav_is_recovered_without_download(self):
        self.record(self.payload())
        self.expand()
        claimed = self.store.claim_source("crashing", lease_seconds=0)
        wav = self.root / "audio" / "after-crash.wav"
        self.store.set_audio_plan(claimed["id"], claimed["claim_token"], "fixture.mp4", 10, wav)
        make_wav(wav)
        reopened = job_store.PipelineStore(self.store.path)
        recovered = reopened.claim_source("restart", lease_seconds=30)
        audio, info = asyncio.run(receiver.prepare_claimed_source(recovered, reopened))
        reopened.complete_audio(recovered["id"], recovered["claim_token"], info)
        self.assertEqual(audio, wav)
        self.assertEqual(reopened.source(recovered["id"])["stage"], "audio_ready")

    def test_usage_survives_restart_without_stopping_at_legacy_limit(self):
        self.seed_ready_audio(seconds=13.0)
        cfg = worker.WorkerConfig(True, "key", "secret", runs_dir=self.root / "runs")
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO worker_budget VALUES (1,1,0,'fixture','fixture')"
            )
            db.commit()
        self.assertEqual(self.run_worker(cfg=cfg), "complete_no_match")
        reopened = job_store.PipelineStore(self.store.path)
        self.assertEqual(reopened.request_usage(), 2)
        with reopened.connect() as db:
            recognition = db.execute("SELECT * FROM recognitions").fetchone()
            attempts = db.execute("SELECT COUNT(*) FROM worker_attempts").fetchone()[0]
            stored_limit = db.execute("SELECT request_limit FROM worker_budget WHERE id=1").fetchone()[0]
        self.assertEqual(recognition["per_job_limit"], 0)
        self.assertEqual(recognition["attempts_used"], 2)
        self.assertEqual(attempts, 2)
        self.assertEqual(stored_limit, 0)
        self.assertEqual(self.run_worker(cfg=cfg), "idle")

    def test_uncertain_request_retries_are_bounded(self):
        self.seed_ready_audio()
        calls = []
        def uncertain(*args):
            calls.append(1)
            raise TimeoutError("fixture")
        self.assertEqual(self.run_worker(transport=uncertain), "uncertain")
        self.assertEqual(self.run_worker(transport=uncertain), "idle")
        self.assertEqual(len(calls), 3)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM worker_attempts WHERE state='uncertain'").fetchone()[0],
                3,
            )
            result_dir = Path(db.execute("SELECT result_dir FROM recognitions").fetchone()[0])
        scan_db = sqlite3.connect(result_dir / "scan.sqlite3")
        self.addCleanup(scan_db.close)
        self.assertEqual(
            scan_db.execute("SELECT COUNT(*) FROM attempts WHERE state='uncertain'").fetchone()[0],
            3,
        )

    def test_worker_retries_429_and_acr_3015_then_completes(self):
        self.seed_ready_audio()
        responses = [
            (429, b'{"status":{"code":1001}}'),
            (200, b'{"status":{"code":3015}}'),
            (200, b'{"status":{"code":1001}}'),
        ]

        def transient(*args):
            return responses.pop(0)

        self.assertEqual(self.run_worker(transport=transient), "complete_no_match")
        with self.store.connect() as db:
            recognition = db.execute("SELECT * FROM recognitions").fetchone()
            attempts = db.execute("SELECT COUNT(*) FROM worker_attempts").fetchone()[0]
        self.assertEqual(recognition["completed_windows"], 1)
        self.assertEqual(attempts, 3)

    def test_worker_authorization_error_is_not_retried(self):
        self.seed_ready_audio()
        calls = []

        def unauthorized(*args):
            calls.append(1)
            return 200, b'{"status":{"code":3001}}'

        self.assertEqual(self.run_worker(transport=unauthorized), "api_error")
        self.assertEqual(len(calls), 1)

    def test_retry_limited_resumes_same_recognition_without_duplicate_windows(self):
        self.seed_ready_audio()
        exhausted_calls = []

        def exhausted(*args):
            exhausted_calls.append(1)
            return 200, b'{"status":{"code":3003}}'

        self.assertEqual(self.run_worker(transport=exhausted), "api_error")
        self.assertEqual(len(exhausted_calls), 1)
        before = self.store.recognition(1)
        self.assertEqual(self.store.resume_limited_recognitions("chat-1", "chat-1"), [1])
        resumed = self.store.recognition(1)
        self.assertEqual((resumed["state"], resumed["resume_limited"]), ("pending", 1))

        resumed_calls = []

        def no_match(*args):
            resumed_calls.append(1)
            return 200, b'{"status":{"code":1001}}'

        self.assertEqual(self.run_worker(transport=no_match), "complete_no_match")
        after = self.store.recognition(1)
        self.assertEqual(len(resumed_calls), 1)
        self.assertEqual(after["result_dir"], before["result_dir"])
        self.assertEqual(after["state"], "complete_no_match")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 1)

    def test_retry_limited_chat_command_requeues_only_acr_3003_stop(self):
        self.seed_ready_audio()
        self.assertEqual(self.run_worker(
            transport=lambda *args: (200, b'{"status":{"code":3003}}')
        ), "api_error")
        self.record(self.payload("retry-command", "ACR RETRY_LIMITED"))

        async def no_network(payload):
            return payload

        async def allow_scope(force=False):
            del force
            return {"chat-1"}, {"chat-1"}

        async def unexpected_source(*args):
            raise AssertionError("retry command must not be treated as a source")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_webhook(
                self.store, no_network, allow_scope, unexpected_source
            )))
        self.assertEqual(self.store.recognition(1)["state"], "pending")
        self.assertEqual(self.store.recognition(1)["resume_limited"], 1)

    def test_chat_notification_delivery_is_idempotent_and_uses_outbox(self):
        self.assertTrue(self.store.enqueue_chat_notification(
            "chat-1", "duplicate_link", "fixture-duplicate", "Повторная ссылка"
        ))
        self.assertFalse(self.store.enqueue_chat_notification(
            "chat-1", "duplicate_link", "fixture-duplicate", "Повторная ссылка"
        ))
        delivered = []

        async def post(path, payload, *, request_kind):
            delivered.append((path, payload, request_kind))
            return {"id": "message"}

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.flush_one_chat_notification(
                self.store, post_message=post
            )))
            self.assertFalse(asyncio.run(receiver.flush_one_chat_notification(
                self.store, post_message=post
            )))
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], "/chats/chat-1/messages")
        self.assertNotIn("label", delivered[0][1])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM chat_notifications").fetchone()[0], "sent")

    def test_pipeline_notification_is_recognized_without_yougile_label(self):
        self.assertTrue(receiver.is_pipeline_notification_message({
            "text": "Сервер: Готово\nАгрегация завершена."
        }))
        self.assertTrue(receiver.is_pipeline_notification_message({
            "text": "Сервер: Поиск CIS-Net начался"
        }))
        self.assertFalse(receiver.is_pipeline_notification_message({
            "text": "ACR RETRY_LIMITED"
        }))

    def test_chat_notification_uploads_report_once_before_sending(self):
        report = self.root / "summary.md"
        report.write_text("# Aggregation report\n", encoding="utf-8")
        self.store.enqueue_chat_notification(
            "chat-1", "completed", "fixture-completed", "Готово", report
        )
        uploads, delivered = [], []

        async def upload(path):
            uploads.append(path)
            return "/user-data/report-id/summary.md"

        async def post(path, payload, *, request_kind):
            delivered.append(payload["text"])
            return {"id": "message"}

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.flush_one_chat_notification(
                self.store, post_message=post, upload_report=upload
            )))
        self.assertEqual(uploads, [report])
        self.assertEqual(delivered, ["Сервер: Готово\n/root/#file:/user-data/report-id/summary.md"])
        with self.store.connect() as db:
            row = db.execute("SELECT state,attachment_ref FROM chat_notifications").fetchone()
        self.assertEqual((row["state"], row["attachment_ref"]), ("sent", "/user-data/report-id/summary.md"))

    def test_one_shot_request_cap_is_global_across_windows_and_retries(self):
        self.seed_ready_audio(seconds=26.8)
        calls = []

        def always_busy(*args):
            calls.append(1)
            return 503, b'{"status":{"code":3003}}'

        with patch.object(worker.scan, "load_sdk", return_value=(object(), SDK_INFO)), \
             contextlib.redirect_stdout(io.StringIO()):
            state = worker.run_once(
                self.store, self.cfg, always_busy, lambda sample: b"fixture-fingerprint",
                max_requests=3,
            )
        self.assertEqual(state, "api_error")
        self.assertEqual(len(calls), 3)

    def test_scanner_failure_before_request_is_partial(self):
        self.seed_ready_audio()
        def failed_fingerprint(sample):
            raise scan.ScanStop("fixture")
        self.assertEqual(self.run_worker(fingerprinter=failed_fingerprint), "partial")
        with self.store.connect() as db:
            recognition = db.execute("SELECT * FROM recognitions").fetchone()
        self.assertEqual(recognition["state"], "partial")
        self.assertEqual(recognition["attempts_used"], 0)

    def test_ffmpeg_failure_marks_audio_error_and_keeps_no_partial_wav(self):
        self.record(self.payload(text="https://files.example.test/video.mp4"))
        self.expand()
        video = self.root / "video.mp4"
        video.write_bytes(b"fixture")
        async def failing_prepare(job, store):
            audio = self.root / "audio" / "failed.wav"
            store.set_audio_plan(job["id"], job["claim_token"], "video.mp4", 7, audio)
            with patch.object(receiver, "run_ffmpeg_with_progress", side_effect=subprocess.CalledProcessError(1, ["ffmpeg"])):
                receiver.extract_audio(video, audio)
            raise AssertionError("unreachable")
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(receiver.process_one_audio(self.store, failing_prepare))
        with self.store.connect() as db:
            source = db.execute("SELECT * FROM source_jobs").fetchone()
        self.assertEqual(source["stage"], "audio_error")
        self.assertEqual(source["error_type"], "CalledProcessError")
        self.assertFalse((self.root / "audio" / "failed.part.wav").exists())

    def test_existing_audio_file_queues_short_notification(self):
        self.record(self.payload(text="https://files.example.test/video.mp4"))
        self.expand()

        async def existing_audio(job, store):
            raise RuntimeError("UntrackedExistingWav")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(receiver.process_one_audio(self.store, existing_audio)))
        with self.store.connect() as db:
            row = db.execute(
                "SELECT kind,text FROM chat_notifications WHERE kind='audio_exists'"
            ).fetchone()
        self.assertEqual(tuple(row), ("audio_exists", "Сервер: Аудио-файл уже существует"))

    def test_enabled_worker_requires_keys_but_not_internal_limits(self):
        with self.assertRaises(worker.WorkerConfigError):
            worker.WorkerConfig(True)
        cfg = worker.WorkerConfig(True, "key", "secret", runs_dir=self.root / "runs")
        self.assertTrue(cfg.enabled)

    def test_disabled_mode_makes_no_request_and_claims_nothing(self):
        self.seed_ready_audio()
        disabled = worker.WorkerConfig(False, runs_dir=self.root / "runs")
        self.assertEqual(self.run_worker(cfg=disabled, transport=lambda *args: self.fail("network")), "disabled")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT stage FROM source_jobs").fetchone()[0], "audio_ready")

    def test_unqueued_old_wav_is_not_imported(self):
        old = self.root / "audio" / "historical.wav"
        make_wav(old)
        self.assertEqual(self.run_worker(), "idle")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 0)
        self.assertTrue(old.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
