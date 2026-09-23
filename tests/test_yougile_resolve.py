import asyncio
import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import receiver
import yougile_resolve as resolve


TASK = "11111111-1111-4111-8111-111111111111"
COLUMN = "22222222-2222-4222-8222-222222222222"
BOARD = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"
OTHER = "55555555-5555-4555-8555-555555555555"
PROJECT_TWO = "66666666-6666-4666-8666-666666666666"
BOARD_TWO = "77777777-7777-4777-8777-777777777777"
COLUMN_TWO = "88888888-8888-4888-8888-888888888888"
COLUMN_THREE = "99999999-9999-4999-8999-999999999999"


class YouGileResolveTests(unittest.TestCase):
    def test_extracts_common_url_variants(self):
        links = [
            f"https://yougile.com/task/{TASK}",
            f"https://ru.yougile.com/root/#task:{TASK}",
            f"https://team.yougile.com/board/{BOARD}?taskId={TASK}",
            f"https://yougile.com/root/#/board/{BOARD}/task/{TASK}",
            f"https://yougile.com/root/?chat_id={TASK}",
        ]
        self.assertEqual([resolve.extract_task_uuid(link) for link in links], [TASK] * len(links))

    def test_extracts_short_task_code_for_api_resolution(self):
        self.assertEqual(
            resolve.extract_task_reference("https://yougile.com/task/DEV-484"),
            "DEV-484",
        )
        with self.assertRaises(resolve.ResolveError):
            resolve.extract_task_uuid("https://yougile.com/task/DEV-484")

    def test_rejects_non_yougile_missing_and_ambiguous_links(self):
        with self.assertRaises(resolve.ResolveError):
            resolve.extract_task_uuid(f"https://example.com/task/{TASK}")
        with self.assertRaises(resolve.ResolveError):
            resolve.extract_task_uuid("https://yougile.com/task/no-id")
        with self.assertRaises(resolve.ResolveError):
            resolve.extract_task_uuid(f"https://yougile.com/{TASK}/{OTHER}")

    def test_resolves_task_hierarchy_and_checks_chat_without_returning_messages(self):
        calls = []

        async def fake_get(path, *, params=None, request_kind):
            calls.append((path, params, request_kind))
            return {
                f"/tasks/{TASK}": {"id": TASK, "title": " Task  name ", "columnId": COLUMN},
                f"/columns/{COLUMN}": {"id": COLUMN, "title": "Column", "boardId": BOARD},
                f"/boards/{BOARD}": {"id": BOARD, "title": "Board", "projectId": PROJECT},
                f"/projects/{PROJECT}": {"id": PROJECT, "title": "Project"},
                f"/chats/{TASK}/messages": {"content": [{"text": "must stay private"}]},
            }[path]

        result = asyncio.run(resolve.resolve_task(TASK, fake_get))
        self.assertEqual(result.task_title, "Task name")
        self.assertEqual(result.column_id, COLUMN)
        self.assertEqual(result.board_id, BOARD)
        self.assertEqual(result.project_id, PROJECT)
        self.assertTrue(result.chat_accessible)
        self.assertEqual(calls[-1][1], {"limit": 1, "offset": 0})
        self.assertNotIn("must stay private", resolve.format_human([result]))
        self.assertNotIn("must stay private", json.dumps(result.__dict__))

    def test_resolves_short_task_code_to_uuid(self):
        async def fake_get(path, *, params=None, request_kind):
            return {
                "/tasks/DEV-484": {"id": TASK, "title": "Task", "columnId": COLUMN},
                f"/columns/{COLUMN}": {"id": COLUMN, "title": "Column", "boardId": BOARD},
                f"/boards/{BOARD}": {"id": BOARD, "title": "Board", "projectId": PROJECT},
                f"/projects/{PROJECT}": {"id": PROJECT, "title": "Project"},
                f"/chats/{TASK}/messages": {"content": []},
            }[path]

        result = asyncio.run(resolve.resolve_task("DEV-484", fake_get))
        self.assertEqual(result.task_id, TASK)

    def test_columns_env_deduplicates_and_warns_on_duplicate(self):
        base = resolve.ResolvedTask(
            TASK, TASK, COLUMN, BOARD, PROJECT, "Task", "Column", "Project",
            True, True, True, True, True,
        )
        other = resolve.ResolvedTask(
            OTHER, OTHER, OTHER, BOARD, PROJECT, "Task 2", "Column 2", "Project",
            True, True, True, True, True,
        )
        line, warnings = resolve.format_columns_env([base, base, other])
        self.assertEqual(line, f"YOUGILE_ALLOWED_COLUMN_IDS={COLUMN},{OTHER}")
        self.assertEqual(len(warnings), 1)
        self.assertIn("1 и 2", warnings[0])

    def test_429_is_retried_after_delay(self):
        calls = 0
        sleeps = []

        async def fake_get(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise receiver.YouGileRateLimited(7)
            return {"ok": True}

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch.object(resolve.asyncio, "sleep", new=fake_sleep):
            body = asyncio.run(resolve._get(fake_get, "/fixture", request_kind="fixture"))
        self.assertEqual(body, {"ok": True})
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [7])

    def test_read_only_limiter_does_not_change_sqlite(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pipeline.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE yougile_api_requests (
                        requested_epoch REAL NOT NULL,
                        request_kind TEXT NOT NULL
                    );
                    CREATE TABLE yougile_rate_limit (
                        id INTEGER PRIMARY KEY,
                        blocked_until REAL NOT NULL
                    );
                    INSERT INTO yougile_api_requests VALUES (999, 'receiver');
                    INSERT INTO yougile_rate_limit VALUES (1, 0);
                """)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            limiter = resolve.ReadOnlyYouGileLimiter(path, 40, clock=lambda: 1000)
            asyncio.run(limiter.acquire("resolve"))
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM yougile_api_requests").fetchone()[0], 1)

    def test_main_columns_env_keeps_stdout_to_one_line(self):
        env_file = None
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "receiver.env"
            env_file.write_text("YOUGILE_API_KEY='fixture key'\n", encoding="utf-8")
            result = resolve.ResolvedTask(
                TASK, TASK, COLUMN, BOARD, PROJECT, "Task", "Column", "Project",
                True, True, True, True, True,
            )

            async def fake_resolve(task_id):
                return result

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(resolve, "ENV_PATH", env_file), \
                 patch.object(resolve, "resolve_task", new=fake_resolve), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = resolve.main(["--columns-env", f"https://yougile.com/task/{TASK}",
                                     f"https://yougile.com/task/{TASK}"])
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), f"YOUGILE_ALLOWED_COLUMN_IDS={COLUMN}\n")
            self.assertIn("предупреждение", stderr.getvalue())

    def test_interactive_columns_paginates_and_selects_three_unique_columns(self):
        calls = []

        async def fake_get(path, *, params=None, request_kind):
            params = dict(params or {})
            calls.append((path, params, request_kind))
            if path == "/projects":
                if params["offset"] == 0:
                    return {
                        "content": [{"id": PROJECT, "title": "Одинаковое имя"}],
                        "paging": {"next": True},
                    }
                return {
                    "content": [{"id": PROJECT_TWO, "title": "Одинаковое имя"}],
                    "paging": {"next": False},
                }
            if path == "/boards":
                project_id = params["projectId"]
                board_id = BOARD if project_id == PROJECT else BOARD_TWO
                return {
                    "content": [{"id": board_id, "projectId": project_id, "title": "Board"}],
                    "paging": {"next": False},
                }
            if path == "/columns":
                if params["boardId"] == BOARD:
                    rows = [
                        {"id": COLUMN, "boardId": BOARD, "title": "Монтаж"},
                        {"id": COLUMN_TWO, "boardId": BOARD, "title": "Монтаж"},
                    ]
                else:
                    rows = [{"id": COLUMN_THREE, "boardId": BOARD_TWO, "title": "Готово"}]
                return {"content": rows, "paging": {"next": False}}
            if path == "/task-list":
                column_id = params["columnId"]
                if column_id == COLUMN and params["offset"] == 0:
                    return {
                        "content": [{"title": "PRIVATE 1"}, {"title": "PRIVATE 2"}],
                        "paging": {"next": True},
                    }
                counts = {COLUMN: 1, COLUMN_TWO: 0, COLUMN_THREE: 4}
                return {
                    "content": [{"title": "PRIVATE"}] * counts[column_id],
                    "paging": {"next": False},
                }
            raise AssertionError(path)

        output = io.StringIO()
        answers = iter(["1", "1", "1", "2", "2", "2", "1", "3", "1"])

        def fake_input(prompt):
            answer = next(answers)
            output.write(prompt + answer + "\n")
            return answer

        saved = []

        def fake_save(column_ids):
            saved.append(column_ids)
            return "YOUGILE_ALLOWED_COLUMN_IDS=" + ",".join(column_ids)

        code = asyncio.run(resolve.interactive_columns(
            get_json=fake_get,
            input_fn=fake_input,
            output=output,
            save_column_ids=fake_save,
        ))
        transcript = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(f"1 | Одинаковое имя | {PROJECT}", transcript)
        self.assertIn(f"2 | Одинаковое имя | {PROJECT_TWO}", transcript)
        self.assertIn(f"1 | Монтаж | {COLUMN} | 3", transcript)
        self.assertIn(f"2 | Монтаж | {COLUMN_TWO} | 0", transcript)
        self.assertNotIn("PRIVATE", transcript)
        self.assertIn(
            f"YOUGILE_ALLOWED_COLUMN_IDS={COLUMN},{COLUMN_TWO},{COLUMN_THREE}",
            transcript,
        )
        self.assertEqual(saved, [[COLUMN, COLUMN_TWO, COLUMN_THREE]])
        self.assertIn(
            f"Добавлено в env файл: YOUGILE_ALLOWED_COLUMN_IDS={COLUMN},{COLUMN_TWO},{COLUMN_THREE}",
            transcript,
        )
        task_offsets = [
            params["offset"] for path, params, _ in calls
            if path == "/task-list" and params["columnId"] == COLUMN
        ]
        self.assertEqual(task_offsets, [0, 2])

    def test_add_allowed_column_ids_merges_effective_assignment_and_preserves_env_file(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "receiver.env"
            env_file.write_text(
                "YOUGILE_API_KEY='fixture key'\n"
                f"YOUGILE_ALLOWED_COLUMN_IDS={COLUMN}\n"
                "OTHER_SETTING=value\n",
                encoding="utf-8",
            )

            assignment = resolve.add_allowed_column_ids(
                [COLUMN, COLUMN_TWO], path=env_file
            )

            self.assertEqual(
                assignment,
                f"YOUGILE_ALLOWED_COLUMN_IDS={COLUMN},{COLUMN_TWO}",
            )
            self.assertEqual(
                env_file.read_text(encoding="utf-8"),
                "YOUGILE_API_KEY='fixture key'\n"
                f"YOUGILE_ALLOWED_COLUMN_IDS={COLUMN},{COLUMN_TWO}\n"
                "OTHER_SETTING=value\n",
            )

    def test_interactive_columns_can_cancel_without_final_value(self):
        async def fake_get(path, *, params=None, request_kind):
            self.assertEqual(path, "/projects")
            return {
                "content": [{"id": PROJECT, "title": "Project"}],
                "paging": {"next": False},
            }

        output = io.StringIO()
        code = asyncio.run(resolve.interactive_columns(
            get_json=fake_get,
            input_fn=lambda prompt: "x",
            output=output,
        ))
        self.assertEqual(code, 0)
        self.assertIn("Выбор отменён", output.getvalue())
        self.assertNotIn("YOUGILE_ALLOWED_COLUMN_IDS=", output.getvalue())

    def test_columns_mode_accepts_no_links(self):
        args = resolve.build_parser().parse_args(["--columns"])
        self.assertTrue(args.columns)
        self.assertEqual(args.links, [])

    def test_pagination_rejects_empty_intermediate_page(self):
        async def fake_get(path, *, params=None, request_kind):
            return {"content": [], "paging": {"next": True}}

        with self.assertRaises(resolve.ResolveError):
            asyncio.run(resolve.fetch_all(
                "/projects",
                request_kind="fixture",
                kind="проектов",
                get_json=fake_get,
            ))


if __name__ == "__main__":
    unittest.main()
