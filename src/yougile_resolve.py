#!/usr/bin/env python3
"""Resolve YouGile task links without mutating YouGile or pipeline state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import time
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, TextIO
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

import receiver


ENV_PATH = Path("/etc/yougile-video.env")
PIPELINE_DB = receiver.PIPELINE_DB
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile(rf"(?<![0-9a-fA-F-])({UUID_PATTERN})(?![0-9a-fA-F-])")
TASK_CODE_PATTERN = r"[A-Za-zА-Яа-яЁё0-9_]{1,32}-[0-9]{1,12}"
TASK_CODE_RE = re.compile(rf"(?<![\w-])({TASK_CODE_PATTERN})(?![\w-])")
LABELED_REFERENCE_RE = re.compile(
    rf"(?:task(?:Id|_id)?|chat(?:Id|_id)?)[/:=]({UUID_PATTERN}|{TASK_CODE_PATTERN})",
    re.IGNORECASE,
)
TASK_QUERY_NAMES = {"task", "taskid", "task_id", "chat", "chatid", "chat_id"}
ALLOWED_HOST_RE = re.compile(r"(^|\.)yougile\.com$", re.IGNORECASE)


class ResolveError(RuntimeError):
    pass


class SelectionCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedTask:
    task_id: str
    chat_id: str
    column_id: str
    board_id: str
    project_id: str
    task_title: str
    column_title: str
    project_title: str
    task_exists: bool
    column_exists: bool
    board_exists: bool
    project_exists: bool
    chat_accessible: bool


@dataclass(frozen=True)
class ProjectChoice:
    project_id: str
    title: str


@dataclass(frozen=True)
class ColumnChoice:
    project_id: str
    project_title: str
    column_id: str
    title: str
    task_count: int


def _valid_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = UUID_RE.fullmatch(value.strip())
    return match.group(1).lower() if match else None


def _valid_task_reference(value: object) -> str | None:
    uuid_value = _valid_uuid(value)
    if uuid_value:
        return uuid_value
    if isinstance(value, str) and TASK_CODE_RE.fullmatch(value.strip()):
        return value.strip()
    return None


def extract_task_reference(link: str) -> str:
    """Extract a task UUID or API-supported task code from a YouGile link."""
    if not isinstance(link, str) or not link.strip():
        raise ResolveError("пустая ссылка")
    parsed = urlparse(link.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ResolveError("ожидалась полная HTTP(S)-ссылка YouGile")
    if not ALLOWED_HOST_RE.search(parsed.hostname):
        raise ResolveError("ссылка ведёт не на домен YouGile")

    query = parse_qs(parsed.query, keep_blank_values=True)
    fragment_query = parse_qs(parsed.fragment.partition("?")[2], keep_blank_values=True)
    candidates: list[str] = []
    for params in (query, fragment_query):
        for name, values in params.items():
            if name.lower() not in TASK_QUERY_NAMES:
                continue
            for value in values:
                reference = _valid_task_reference(unquote(value))
                if reference:
                    candidates.append(reference)

    decoded = unquote("/".join((parsed.path, parsed.fragment)))
    candidates.extend(
        value.lower() if _valid_uuid(value) else value
        for value in LABELED_REFERENCE_RE.findall(decoded)
    )
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ResolveError("в ссылке найдено несколько ссылочных ID задачи")

    all_uuids = [value.lower() for value in UUID_RE.findall(decoded)]
    all_uuids = list(dict.fromkeys(all_uuids))
    if len(all_uuids) == 1:
        return all_uuids[0]
    if not all_uuids:
        task_codes = list(dict.fromkeys(TASK_CODE_RE.findall(decoded)))
        if len(task_codes) == 1:
            return task_codes[0]
        if not task_codes:
            raise ResolveError("UUID или код задачи в ссылке не найден")
        raise ResolveError("ссылка неоднозначна: найдено несколько кодов задачи")
    raise ResolveError("ссылка неоднозначна: UUID задачи не помечен")


def extract_task_uuid(link: str) -> str:
    reference = extract_task_reference(link)
    uuid_value = _valid_uuid(reference)
    if not uuid_value:
        raise ResolveError("ссылка содержит код задачи, UUID нужно получить через API")
    return uuid_value


def load_env(path: Path | None = None) -> dict[str, str]:
    """Read simple EnvironmentFile assignments without executing shell code."""
    path = ENV_PATH if path is None else path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ResolveError(f"не удалось прочитать EnvironmentFile: {error.__class__.__name__}") from None
    values: dict[str, str] = {}
    for number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ResolveError(f"некорректное имя переменной в EnvironmentFile, строка {number}")
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            raise ResolveError(f"некорректное значение в EnvironmentFile, строка {number}") from None
        values[name] = " ".join(parts)
    return values


def add_allowed_column_ids(column_ids: list[str], path: Path | None = None) -> str:
    """Add selected column UUIDs to the effective EnvironmentFile assignment."""
    path = ENV_PATH if path is None else path
    selected = [_valid_uuid(column_id) for column_id in column_ids]
    if not selected or any(column_id is None for column_id in selected):
        raise ResolveError("выбранные ID колонок некорректны")

    env = load_env(path)
    existing = env.get("YOUGILE_ALLOWED_COLUMN_IDS", "")
    existing_ids = []
    for raw_id in existing.split(","):
        if not raw_id.strip():
            continue
        column_id = _valid_uuid(raw_id)
        if column_id is None:
            raise ResolveError("YOUGILE_ALLOWED_COLUMN_IDS содержит некорректный ID")
        existing_ids.append(column_id)
    merged_ids = list(dict.fromkeys(existing_ids + selected))
    assignment = "YOUGILE_ALLOWED_COLUMN_IDS=" + ",".join(merged_ids)

    try:
        original = path.read_text(encoding="utf-8")
        metadata = path.stat()
    except OSError as error:
        raise ResolveError(
            f"не удалось обновить EnvironmentFile: {error.__class__.__name__}"
        ) from None

    assignment_re = re.compile(
        r"^[ \t]*(?:export[ \t]+)?YOUGILE_ALLOWED_COLUMN_IDS[ \t]*=.*$"
    )
    lines = original.splitlines(keepends=True)
    assignment_lines = [index for index, line in enumerate(lines) if assignment_re.match(line)]
    if assignment_lines:
        index = assignment_lines[-1]
        lines[index] = assignment + ("\n" if lines[index].endswith("\n") else "")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(assignment + "\n")
    updated = "".join(lines)

    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, metadata.st_mode & 0o777)
        os.chown(temporary_name, metadata.st_uid, metadata.st_gid)
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as error:
        raise ResolveError(
            f"не удалось обновить EnvironmentFile: {error.__class__.__name__}"
        ) from None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return assignment


class ReadOnlyYouGileLimiter:
    """Observe the shared SQLite window but never reserve or persist a slot."""

    def __init__(
        self,
        db_path: Path,
        max_requests: int = 40,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.db_path = Path(db_path)
        self.max_requests = min(40, max(1, int(max_requests)))
        self.clock = clock
        self.sleep = sleep
        self.own_requests: deque[float] = deque()
        self.blocked_until = 0.0

    def _shared_state(self, cutoff: float) -> tuple[list[float], float]:
        if not self.db_path.exists():
            raise ResolveError("база общего лимитера не найдена")
        uri = f"file:{self.db_path}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                rows = db.execute(
                    "SELECT requested_epoch FROM yougile_api_requests "
                    "WHERE requested_epoch>? ORDER BY requested_epoch",
                    (cutoff,),
                ).fetchall()
                state = db.execute(
                    "SELECT blocked_until FROM yougile_rate_limit WHERE id=1"
                ).fetchone()
        except (OSError, sqlite3.Error) as error:
            raise ResolveError(
                f"не удалось прочитать состояние общего лимитера: {error.__class__.__name__}"
            ) from None
        return [float(row[0]) for row in rows], float(state[0]) if state else 0.0

    async def acquire(self, request_kind: str) -> None:
        del request_kind
        while True:
            now = self.clock()
            cutoff = now - 60.0
            while self.own_requests and self.own_requests[0] <= cutoff:
                self.own_requests.popleft()
            shared, shared_blocked_until = self._shared_state(cutoff)
            blocked_until = max(self.blocked_until, shared_blocked_until)
            if blocked_until > now:
                await self.sleep(max(blocked_until - now, 0.01))
                continue
            combined = sorted(shared + list(self.own_requests))
            if len(combined) >= self.max_requests:
                wait = combined[len(combined) - self.max_requests] + 60.0 - now
                await self.sleep(max(wait, 0.01))
                continue
            self.own_requests.append(now)
            return

    async def defer(self, retry_after: float) -> None:
        self.blocked_until = max(self.blocked_until, self.clock() + max(float(retry_after), 1.0))


GetJson = Callable[..., Awaitable[dict | list]]


async def _get(
    get_json: GetJson,
    path: str,
    *,
    request_kind: str,
    params: dict | None = None,
    max_429_retries: int = 3,
) -> dict | list:
    for attempt in range(max_429_retries + 1):
        try:
            return await get_json(path, params=params, request_kind=request_kind)
        except receiver.YouGileRateLimited as error:
            if attempt >= max_429_retries:
                raise ResolveError("YouGile продолжает ограничивать частоту запросов") from None
            await asyncio.sleep(max(error.retry_after, 1.0))
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 404:
                raise ResolveError("объект не найден или недоступен") from None
            if status in {401, 403}:
                raise ResolveError(f"доступ к YouGile отклонён (HTTP {status})") from None
            raise ResolveError(f"YouGile вернул HTTP {status}") from None
        except httpx.RequestError:
            raise ResolveError("сетевая ошибка при обращении к YouGile") from None
    raise AssertionError("unreachable")


def _page(body: dict | list, kind: str) -> tuple[list[dict], bool]:
    if isinstance(body, list):
        rows = body
        has_next = False
    elif isinstance(body, dict):
        rows = next(
            (
                body[name]
                for name in ("content", "list", "items", "data")
                if isinstance(body.get(name), list)
            ),
            None,
        )
        if rows is None:
            raise ResolveError(f"YouGile вернул неожиданный список для {kind}")
        paging = body.get("paging")
        has_next = bool(paging.get("next")) if isinstance(paging, dict) else False
    else:
        raise ResolveError(f"YouGile вернул неожиданный список для {kind}")
    if not all(isinstance(row, dict) for row in rows):
        raise ResolveError(f"YouGile вернул некорректные элементы для {kind}")
    if has_next and not rows:
        raise ResolveError(f"YouGile вернул пустую промежуточную страницу для {kind}")
    return rows, has_next


async def fetch_all(
    path: str,
    *,
    request_kind: str,
    kind: str,
    params: dict | None = None,
    get_json: GetJson = receiver.yougile_get_json,
) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page_params = dict(params or {})
        page_params.update({"limit": 1000, "offset": offset})
        body = await _get(
            get_json,
            path,
            params=page_params,
            request_kind=request_kind,
        )
        page, has_next = _page(body, kind)
        rows.extend(page)
        if not has_next:
            return rows
        offset += len(page)


async def count_all(
    path: str,
    *,
    request_kind: str,
    kind: str,
    params: dict | None = None,
    get_json: GetJson = receiver.yougile_get_json,
) -> int:
    count = 0
    offset = 0
    while True:
        page_params = dict(params or {})
        page_params.update({"limit": 1000, "offset": offset})
        body = await _get(
            get_json,
            path,
            params=page_params,
            request_kind=request_kind,
        )
        page, has_next = _page(body, kind)
        count += len(page)
        if not has_next:
            return count
        offset += len(page)


def _require_object(body: dict | list, kind: str) -> dict:
    if not isinstance(body, dict):
        raise ResolveError(f"YouGile вернул неожиданный ответ для {kind}")
    return body


def _require_uuid_field(body: dict, name: str, kind: str) -> str:
    value = _valid_uuid(body.get(name))
    if not value:
        raise ResolveError(f"у {kind} отсутствует корректный {name}")
    return value


def _safe_title(body: dict, kind: str) -> str:
    value = body.get("title")
    if not isinstance(value, str) or not value.strip():
        raise ResolveError(f"у {kind} отсутствует название")
    value = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    value = " ".join(value.split())
    if not value:
        raise ResolveError(f"у {kind} отсутствует безопасное название")
    return value


async def list_projects(get_json: GetJson = receiver.yougile_get_json) -> list[ProjectChoice]:
    rows = await fetch_all(
        "/projects",
        request_kind="resolve_projects",
        kind="проектов",
        get_json=get_json,
    )
    projects: list[ProjectChoice] = []
    seen: set[str] = set()
    for row in rows:
        if row.get("deleted") is True:
            continue
        project_id = _require_uuid_field(row, "id", "проекта")
        if project_id in seen:
            continue
        projects.append(ProjectChoice(project_id, _safe_title(row, "проекта")))
        seen.add(project_id)
    return projects


async def list_project_columns(
    project: ProjectChoice,
    get_json: GetJson = receiver.yougile_get_json,
) -> list[ColumnChoice]:
    board_rows = await fetch_all(
        "/boards",
        params={"projectId": project.project_id},
        request_kind="resolve_project_boards",
        kind="досок",
        get_json=get_json,
    )
    board_ids: list[str] = []
    seen_boards: set[str] = set()
    for row in board_rows:
        if row.get("deleted") is True:
            continue
        row_project_id = _valid_uuid(row.get("projectId"))
        if row_project_id and row_project_id != project.project_id:
            continue
        board_id = _require_uuid_field(row, "id", "доски")
        if board_id not in seen_boards:
            board_ids.append(board_id)
            seen_boards.add(board_id)

    columns: list[ColumnChoice] = []
    seen_columns: set[str] = set()
    for board_id in board_ids:
        column_rows = await fetch_all(
            "/columns",
            params={"boardId": board_id},
            request_kind="resolve_board_columns",
            kind="колонок",
            get_json=get_json,
        )
        for row in column_rows:
            if row.get("deleted") is True:
                continue
            row_board_id = _valid_uuid(row.get("boardId"))
            if row_board_id and row_board_id != board_id:
                continue
            column_id = _require_uuid_field(row, "id", "колонки")
            if column_id in seen_columns:
                continue
            task_count = await count_all(
                "/task-list",
                params={"columnId": column_id},
                request_kind="resolve_column_tasks",
                kind="задач",
                get_json=get_json,
            )
            columns.append(ColumnChoice(
                project_id=project.project_id,
                project_title=project.title,
                column_id=column_id,
                title=_safe_title(row, "колонки"),
                task_count=task_count,
            ))
            seen_columns.add(column_id)
    return columns


def _read_input(input_fn: Callable[[str], str], prompt: str) -> str:
    try:
        return input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise SelectionCancelled from None


def _number(value: str, maximum: int) -> int | None:
    if not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if 1 <= number <= maximum else None


def _show_projects(projects: list[ProjectChoice], output: TextIO) -> None:
    print("\nДоступные проекты:", file=output)
    print("номер | название | project_id", file=output)
    for index, project in enumerate(projects, 1):
        print(f"{index} | {project.title} | {project.project_id}", file=output)


def _show_columns(columns: list[ColumnChoice], output: TextIO) -> None:
    print("\nКолонки проекта:", file=output)
    print("номер | название | column_id | количество задач", file=output)
    for index, column in enumerate(columns, 1):
        print(
            f"{index} | {column.title} | {column.column_id} | {column.task_count}",
            file=output,
        )


def _show_selection(selected: list[ColumnChoice], output: TextIO) -> None:
    projects: list[tuple[str, str]] = []
    seen_projects: set[str] = set()
    for column in selected:
        if column.project_id not in seen_projects:
            projects.append((column.project_title, column.project_id))
            seen_projects.add(column.project_id)

    print("\nВыбранные проекты:", file=output)
    for title, project_id in projects:
        print(f"- {title} | {project_id}", file=output)
    print("Выбранные колонки:", file=output)
    for column in selected:
        print(
            f"- {column.title} | {column.column_id} | задач: {column.task_count}",
            file=output,
        )
    print(
        "YOUGILE_TRIGGER_COLUMN_IDS=" + ",".join(column.column_id for column in selected),
        file=output,
    )


async def interactive_columns(
    *,
    get_json: GetJson = receiver.yougile_get_json,
    input_fn: Callable[[str], str] = input,
    output: TextIO | None = None,
    save_column_ids: Callable[[list[str]], str] = add_allowed_column_ids,
) -> int:
    output = sys.stdout if output is None else output
    projects = await list_projects(get_json)
    if not projects:
        raise ResolveError("доступные проекты не найдены")

    selected: list[ColumnChoice] = []
    selected_ids: set[str] = set()
    cached_columns: dict[str, list[ColumnChoice]] = {}
    current_project: ProjectChoice | None = None
    try:
        while True:
            if current_project is None:
                _show_projects(projects, output)
                value = _read_input(
                    input_fn,
                    "Выберите проект по номеру (x — отмена): ",
                ).casefold()
                if value in {"x", "c", "cancel", "отмена"}:
                    raise SelectionCancelled
                project_number = _number(value, len(projects))
                if project_number is None:
                    print("Некорректный номер проекта.", file=output)
                    continue
                current_project = projects[project_number - 1]

            columns = cached_columns.get(current_project.project_id)
            if columns is None:
                columns = await list_project_columns(current_project, get_json)
                cached_columns[current_project.project_id] = columns
            if not columns:
                print("В выбранном проекте доступные колонки не найдены.", file=output)
                current_project = None
                continue

            _show_columns(columns, output)
            value = _read_input(
                input_fn,
                "Выберите колонку по номеру (b — к проектам, x — отмена): ",
            ).casefold()
            if value in {"x", "c", "cancel", "отмена"}:
                raise SelectionCancelled
            if value in {"b", "back", "назад"}:
                current_project = None
                continue
            column_number = _number(value, len(columns))
            if column_number is None:
                print("Некорректный номер колонки.", file=output)
                continue
            column = columns[column_number - 1]
            if column.column_id in selected_ids:
                print("Эта колонка уже выбрана; повтор не добавлен.", file=output)
            else:
                selected.append(column)
                selected_ids.add(column.column_id)
                print(f"Добавлена колонка: {column.title} | {column.column_id}", file=output)

            while True:
                action = _read_input(
                    input_fn,
                    "Дальше: 1 — ещё колонка здесь, 2 — другой проект, "
                    "3 — завершить, x — отменить: ",
                ).casefold()
                if action == "1":
                    break
                if action == "2":
                    current_project = None
                    break
                if action == "3":
                    if not selected:
                        print("Сначала выберите хотя бы одну колонку.", file=output)
                        continue
                    _show_selection(selected, output)
                    while True:
                        save_action = _read_input(
                            input_fn,
                            "Сохранить выбранные колонки: 1 — добавить в env файл, "
                            "x — отмена: ",
                        ).casefold()
                        if save_action == "1":
                            assignment = save_column_ids(
                                [column.column_id for column in selected]
                            )
                            print(f"Добавлено в env файл: {assignment}", file=output)
                            return 0
                        if save_action in {"x", "c", "cancel", "отмена"}:
                            print("Сохранение отменено. Никаких изменений не выполнено.", file=output)
                            return 0
                        print("Некорректное действие.", file=output)
                if action in {"x", "c", "cancel", "отмена"}:
                    raise SelectionCancelled
                print("Некорректное действие.", file=output)
    except SelectionCancelled:
        print("\nВыбор отменён. Никаких изменений не выполнено.", file=output)
        return 0


async def resolve_task(task_reference: str, get_json: GetJson = receiver.yougile_get_json) -> ResolvedTask:
    safe_reference = quote(task_reference, safe="")
    task = _require_object(
        await _get(get_json, f"/tasks/{safe_reference}", request_kind="resolve_task"), "задачи"
    )
    resolved_task_id = _require_uuid_field(task, "id", "задачи")
    reference_uuid = _valid_uuid(task_reference)
    if reference_uuid and resolved_task_id != reference_uuid:
        raise ResolveError("YouGile вернул другую задачу")
    column_id = _require_uuid_field(task, "columnId", "задачи")
    chat_id = _valid_uuid(task.get("chatId")) or resolved_task_id

    column = _require_object(
        await _get(get_json, f"/columns/{column_id}", request_kind="resolve_column"), "колонки"
    )
    if _require_uuid_field(column, "id", "колонки") != column_id:
        raise ResolveError("YouGile вернул другую колонку")
    board_id = _require_uuid_field(column, "boardId", "колонки")

    board = _require_object(
        await _get(get_json, f"/boards/{board_id}", request_kind="resolve_board"), "доски"
    )
    if _require_uuid_field(board, "id", "доски") != board_id:
        raise ResolveError("YouGile вернул другую доску")
    project_id = _require_uuid_field(board, "projectId", "доски")

    project = _require_object(
        await _get(get_json, f"/projects/{project_id}", request_kind="resolve_project"), "проекта"
    )
    if _require_uuid_field(project, "id", "проекта") != project_id:
        raise ResolveError("YouGile вернул другой проект")

    await _get(
        get_json,
        f"/chats/{chat_id}/messages",
        params={"limit": 1, "offset": 0},
        request_kind="resolve_chat_access",
    )
    return ResolvedTask(
        task_id=resolved_task_id,
        chat_id=chat_id,
        column_id=column_id,
        board_id=board_id,
        project_id=project_id,
        task_title=_safe_title(task, "задачи"),
        column_title=_safe_title(column, "колонки"),
        project_title=_safe_title(project, "проекта"),
        task_exists=True,
        column_exists=True,
        board_exists=True,
        project_exists=True,
        chat_accessible=True,
    )


def format_human(results: list[ResolvedTask]) -> str:
    blocks = []
    for index, result in enumerate(results, 1):
        blocks.append("\n".join([
            f"Ссылка {index}",
            f"  задача: {result.task_title}",
            f"  task_id: {result.task_id}",
            f"  chat_id: {result.chat_id}",
            f"  колонка: {result.column_title}",
            f"  column_id: {result.column_id}",
            f"  board_id: {result.board_id}",
            f"  проект: {result.project_title}",
            f"  project_id: {result.project_id}",
            "  проверки: задача есть; колонка есть; доска есть; проект есть; чат доступен",
        ]))
    return "\n\n".join(blocks)


def format_columns_env(results: list[ResolvedTask]) -> tuple[str, list[str]]:
    column_ids: list[str] = []
    first_link: dict[str, int] = {}
    warnings: list[str] = []
    for index, result in enumerate(results, 1):
        if result.column_id in first_link:
            warnings.append(
                f"предупреждение: ссылки {first_link[result.column_id]} и {index} ведут в одну колонку"
            )
        else:
            first_link[result.column_id] = index
            column_ids.append(result.column_id)
    return "YOUGILE_TRIGGER_COLUMN_IDS=" + ",".join(column_ids), warnings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yougile-resolve",
        description="Read-only разрешение ссылок на задачи YouGile",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--columns-env", action="store_true", help="вывести список уникальных column_id")
    mode.add_argument("--json", action="store_true", help="вывести JSON")
    mode.add_argument("--columns", action="store_true", help="интерактивно выбрать колонки")
    parser.add_argument("links", nargs="*", metavar="ССЫЛКА")
    return parser


def configure_client() -> None:
    env = load_env()
    api_key = env.get("YOUGILE_API_KEY", "")
    if not api_key:
        raise ResolveError("YOUGILE_API_KEY не задан в EnvironmentFile")
    api_base = env.get("YOUGILE_API_BASE", receiver.YOUGILE_API_BASE).rstrip("/")
    parsed_api_base = urlparse(api_base)
    if (
        parsed_api_base.scheme != "https"
        or not parsed_api_base.hostname
        or parsed_api_base.username
        or parsed_api_base.password
        or parsed_api_base.query
        or parsed_api_base.fragment
    ):
        raise ResolveError("YOUGILE_API_BASE должен быть безопасным HTTPS URL")
    try:
        max_requests = int(env.get("YOUGILE_MAX_REQUESTS_PER_MINUTE", "40"))
    except ValueError:
        raise ResolveError("YOUGILE_MAX_REQUESTS_PER_MINUTE задан некорректно") from None
    receiver.YOUGILE_API_KEY = api_key
    receiver.YOUGILE_API_BASE = api_base
    receiver.YOUGILE_LIMITER = ReadOnlyYouGileLimiter(PIPELINE_DB, max_requests)


async def async_main(args: argparse.Namespace) -> int:
    if args.columns:
        configure_client()
        return await interactive_columns()

    task_references = []
    for index, link in enumerate(args.links, 1):
        try:
            task_references.append(extract_task_reference(link))
        except ResolveError as error:
            raise ResolveError(f"ссылка {index}: {error}") from None

    configure_client()

    results = []
    for index, task_reference in enumerate(task_references, 1):
        try:
            results.append(await resolve_task(task_reference))
        except ResolveError as error:
            raise ResolveError(f"ссылка {index}: {error}") from None

    if args.columns_env:
        line, warnings = format_columns_env(results)
        print(line)
        for warning in warnings:
            print(warning, file=sys.stderr)
    elif args.json:
        payload = [asdict(result) for result in results]
        print(json.dumps(payload[0] if len(payload) == 1 else payload, ensure_ascii=False, indent=2))
    else:
        print(format_human(results))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.columns and args.links:
        parser.error("--columns не принимает ссылки")
    if not args.columns and not args.links:
        parser.error("нужно указать хотя бы одну ссылку")
    try:
        return asyncio.run(async_main(args))
    except (ResolveError, ValueError) as error:
        print(f"yougile-resolve: ошибка: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"yougile-resolve: ошибка: неожиданный сбой ({error.__class__.__name__})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
