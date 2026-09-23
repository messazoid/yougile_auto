#!/usr/bin/env python3
"""Safe operator console for inspecting durable music-verifier runs.

The console deliberately moves disposable runtime artifacts to ``data/.trash``.
It never deletes durable queue history or calls YouGile/ACRCloud.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import select
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import textwrap
import termios
from urllib.parse import urlsplit
import uuid

from job_store import (
    PipelineStore,
    StoreError,
    TERMINAL_RECOGNITION_STATES,
    TERMINAL_SOURCE_STATES,
    utcnow,
)


class RunConsoleError(RuntimeError):
    pass


class TerminalScreen:
    """Redraw interactive pages in an alternate terminal screen when possible."""

    _ENTER = "\x1b[?1049h\x1b[2J\x1b[H"
    _CLEAR = "\x1b[2J\x1b[H"
    _EXIT = "\x1b[?1049l"

    def __init__(self, stream, enabled: bool):
        self.stream = stream
        self.enabled = enabled
        self.renderer = None
        self.footer = ""
        self._previous_resize_handler = None
        self.resize_pending = False

    def __enter__(self):
        if self.enabled:
            self._previous_resize_handler = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, self._on_resize)
            self.stream.write(self._ENTER)
            self.stream.flush()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.enabled:
            signal.signal(signal.SIGWINCH, self._previous_resize_handler)
            self.stream.write(self._EXIT)
            self.stream.flush()
        return False

    def clear(self) -> None:
        if self.enabled:
            self.stream.write(self._CLEAR)
            self.stream.flush()

    def _on_resize(self, signum, frame) -> None:
        del signum, frame
        # A signal handler must remain side-effect free: multiple SIGWINCH
        # signals can arrive while the user is entering text.  Redrawing is
        # deferred to the normal interactive loop instead of raising here.
        self.resize_pending = True

    def set_renderer(self, renderer) -> None:
        self.renderer = renderer

    def set_footer(self, message: str) -> None:
        self.footer = message

    def redraw(self) -> None:
        if not self.renderer:
            return
        self.resize_pending = False
        if not self.enabled:
            self.renderer()
            return
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.renderer()
        self.clear()
        size = shutil.get_terminal_size(fallback=(80, 24))
        width = max(1, size.columns - 1)
        body_lines = []
        for line in output.getvalue().splitlines():
            wrapped = textwrap.wrap(
                line, width=width, replace_whitespace=False, drop_whitespace=False,
                break_long_words=True, break_on_hyphens=False,
            ) or [""]
            body_lines.extend(wrapped)
        footer_lines = []
        if self.footer:
            # This is a fixed help area, like nano: it can use explicit lines,
            # but it never scrolls with page output or wraps menu text into
            # unpredictable rows.
            footer_lines = [line[:width] for line in self.footer.splitlines()]
            if self.footer.endswith("\n"):
                footer_lines.append("")
            footer_lines = footer_lines[-size.lines:]
            visible = max(0, size.lines - len(footer_lines))
            if len(body_lines) > visible:
                body_lines = body_lines[:visible]
                if body_lines:
                    body_lines[-1] = "…"
        for segment in body_lines:
            self.stream.write(segment + "\n")
        if footer_lines:
            first_footer_row = size.lines - len(footer_lines) + 1
            for offset, footer in enumerate(footer_lines):
                self.stream.write(f"\x1b[{first_footer_row + offset};1H\x1b[2K{footer}")
        self.stream.flush()

    @contextlib.contextmanager
    def pause_resize(self):
        if not self.enabled:
            yield
            return
        handler = signal.signal(signal.SIGWINCH, signal.SIG_IGN)
        try:
            yield
        finally:
            signal.signal(signal.SIGWINCH, handler)


def _as_selector(value: str) -> int:
    value = value.strip().upper()
    if value.startswith("S"):
        value = value[1:]
    if not value.isdecimal() or int(value) < 1:
        raise argparse.ArgumentTypeError("selector must be a source job ID, for example S14")
    return int(value)


def _public_yandex_link(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme == "https"
        and parsed.hostname in {"disk.yandex.ru", "yadi.sk", "yandex.ru"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    ):
        return value
    return None


def _display_size(value: int | None) -> str:
    """Format a persisted media size without inspecting or opening the file."""
    if value is None:
        return "unknown"
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if size < 0:
        return "unknown"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "unknown"


def _name_with_size(name: str | None, size: int | None) -> str:
    if not name:
        return "-"
    return f"{name} ({_display_size(size)})"


def _display_datetime(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "-"
    return parsed.strftime("%d.%m.%Y %H:%M")


def _display_error_type(value: str | None) -> str:
    labels = {
        "RemoteMediaProcessError": "remote_ffmpeg",
    }
    return labels.get(value or "", value or "")


def _display_period(period: dict) -> str:
    """Render an aggregation interval together with its duration when numeric."""
    start = period.get("start")
    end = period.get("end")
    try:
        start_number = float(start)
        end_number = float(end)
    except (TypeError, ValueError):
        return f"{start}–{end}"
    return f"{start_number:.1f}–{end_number:.1f} ({end_number - start_number:.1f} s)"


class RunConsole:
    def __init__(self, database: Path, company_id: str | None = None):
        self.database = Path(database).resolve()
        self.company_id = company_id or os.getenv("YOUGILE_COMPANY_ID")
        self.data_root = self.database.parent.parent if self.database.parent.name == "queue" else self.database.parent
        self.audio_root = self.data_root / "audio"
        self.runs_root = self.data_root / "recognition-runs"
        self.aggregation_root = self.data_root / "aggregation"
        self.trash_root = self.data_root / ".trash"
        self.store = PipelineStore(self.database)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def runs(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT s.id AS source_job_id,s.source_filename,s.source_size,s.source_kind,s.stage,s.error_type,
                       s.created_utc,s.completed_utc AS source_completed_utc,s.wav_path,s.wav_deleted_utc,
                       s.result_dir AS source_result_dir,
                       s.recognition_id,r.state AS recognition_state,r.result_dir AS recognition_result_dir,
                       r.audio_path,r.audio_deleted_utc,r.results_deleted_utc,
                       r.completed_utc AS recognition_completed_utc
                FROM source_jobs s
                LEFT JOIN recognitions r ON r.id=s.recognition_id
                ORDER BY s.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def detail(self, source_id: int) -> dict:
        with self.connect() as db:
            source = db.execute("SELECT * FROM source_jobs WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise RunConsoleError(f"Source job S{source_id} does not exist")
            result: dict = {"source": dict(source), "recognition": None, "links": [], "aggregation": []}
            if source["recognition_id"] is not None:
                recognition = db.execute(
                    "SELECT * FROM recognitions WHERE id=?", (source["recognition_id"],)
                ).fetchone()
                result["recognition"] = dict(recognition) if recognition else None
                result["aggregation"] = [dict(row) for row in db.execute(
                    "SELECT id,state,result_digest FROM aggregation_runs WHERE recognition_id=? ORDER BY id",
                    (source["recognition_id"],),
                ).fetchall()]
            result["links"] = [dict(row) for row in db.execute(
                """
                SELECT company_id,task_id,chat_id,message_id FROM message_sources
                WHERE source_job_id=? ORDER BY task_id,chat_id,message_id
                """, (source_id,),
            ).fetchall()]
        return result

    @staticmethod
    def _safe_path(value: str | None, root: Path) -> Path | None:
        if not value:
            return None
        candidate = Path(value)
        if not candidate.exists():
            return None
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise RunConsoleError(f"Refusing a path outside {root}: {candidate}") from error
        return candidate

    def _deletion_plan(self, detail: dict, mode: str) -> list[tuple[Path, Path]]:
        source = detail["source"]
        recognition = detail["recognition"]
        source_id = source["id"]
        operation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-S{source_id}-{uuid.uuid4().hex[:8]}"
        base = self.trash_root / operation
        planned: list[tuple[Path, Path]] = []
        if mode in {"audio", "all"}:
            audio = self._safe_path(source["wav_path"], self.audio_root)
            if audio:
                planned.append((audio, base / "audio" / audio.name))
        if mode in {"results", "all"} and recognition:
            run_dir = self._safe_path(recognition["result_dir"], self.runs_root)
            if run_dir:
                planned.append((run_dir, base / "recognition-runs" / run_dir.name))
            for aggregation in detail["aggregation"]:
                export = self.aggregation_root / f"{recognition['id']}_{aggregation['id']}"
                export = self._safe_path(str(export), self.aggregation_root)
                if export:
                    planned.append((export, base / "aggregation" / export.name))
        return planned

    def _assert_deletion_safe(self, db: sqlite3.Connection, detail: dict, mode: str) -> None:
        source_id = detail["source"]["id"]
        source_row = db.execute("SELECT * FROM source_jobs WHERE id=?", (source_id,)).fetchone()
        if not source_row:
            raise RunConsoleError("Source job disappeared before archive")
        source = dict(source_row)
        recognition = None
        if source["recognition_id"] is not None:
            recognition_row = db.execute(
                "SELECT * FROM recognitions WHERE id=?", (source["recognition_id"],)
            ).fetchone()
            recognition = dict(recognition_row) if recognition_row else None
        if source["stage"] not in TERMINAL_SOURCE_STATES or source["claim_token"]:
            raise RunConsoleError("Only an unclaimed terminal source job can be archived")
        if not recognition:
            return
        if recognition["state"] not in TERMINAL_RECOGNITION_STATES or recognition["claim_token"]:
            raise RunConsoleError("Recognition is active and cannot be archived")
        shared = db.execute(
            "SELECT id FROM source_jobs WHERE recognition_id=? AND id<>? LIMIT 1",
            (recognition["id"], source["id"]),
        ).fetchone()
        if shared and mode in {"results", "all"}:
            raise RunConsoleError("Recognition is shared by another source job; refusing to archive shared results")
        if mode in {"audio", "all"} and source["wav_path"]:
            same_audio = db.execute(
                "SELECT id FROM source_jobs WHERE wav_path=? AND id<>? AND wav_deleted_utc IS NULL LIMIT 1",
                (source["wav_path"], source["id"]),
            ).fetchone()
            if same_audio:
                raise RunConsoleError("WAV is referenced by another source job; refusing to archive shared audio")

    def archive(self, source_id: int, mode: str) -> list[Path]:
        if mode not in {"audio", "results", "all"}:
            raise ValueError("Invalid archive mode")
        detail = self.detail(source_id)
        plan = self._deletion_plan(detail, mode)
        if not plan:
            raise RunConsoleError("No matching on-disk artifacts remain to archive")
        moved: list[tuple[Path, Path]] = []
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._assert_deletion_safe(db, detail, mode)
                for original, destination in plan:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        raise RunConsoleError(f"Trash destination already exists: {destination}")
                    os.replace(original, destination)
                    moved.append((original, destination))
                now = utcnow()
                if mode in {"audio", "all"}:
                    db.execute("UPDATE source_jobs SET wav_deleted_utc=?,updated_utc=? WHERE id=?", (now, now, source_id))
                    if detail["recognition"]:
                        db.execute("UPDATE recognitions SET audio_deleted_utc=?,updated_utc=? WHERE id=?", (now, now, detail["recognition"]["id"]))
                if mode in {"results", "all"} and detail["recognition"]:
                    db.execute("UPDATE recognitions SET results_deleted_utc=?,updated_utc=? WHERE id=?", (now, now, detail["recognition"]["id"]))
                db.commit()
        except BaseException:
            for original, destination in reversed(moved):
                if destination.exists() and not original.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, original)
            raise
        return [destination for _, destination in moved]

    def continue_run(self, source_id: int) -> str:
        """Resume the one safe incomplete stage represented by this run."""
        detail = self.detail(source_id)
        source = detail["source"]
        recognition = detail["recognition"]
        if source["stage"] == "audio_error":
            self.store.requeue_audio_errors([source_id])
            return f"S{source_id} queued from audio preparation."
        if recognition and recognition["state"] == "api_error":
            if self.store.resume_limited_recognition(int(recognition["id"])):
                return (
                    f"R{recognition['id']} queued from its saved ACRCloud windows "
                    f"(S{source_id})."
                )
            raise RunConsoleError(
                "Only an ACRCloud 3003 quota stop can be continued from saved windows"
            )
        raise RunConsoleError(
            "This run has no supported continuation; use full replay to start a fresh run"
        )

    def can_continue(self, source_id: int) -> bool:
        """Whether the interactive menu may offer continuation for this run."""
        detail = self.detail(source_id)
        if detail["source"]["stage"] == "audio_error":
            return True
        recognition = detail["recognition"]
        if not recognition or recognition["state"] != "api_error":
            return False
        scan_path = self._safe_path(
            str(Path(recognition["result_dir"]) / "scan.sqlite3"), self.runs_root
        )
        if not scan_path:
            return False
        try:
            with sqlite3.connect(f"file:{scan_path}?mode=ro", uri=True) as scan_db:
                return scan_db.execute(
                    "SELECT 1 FROM attempts WHERE state='error' AND acr_code=3003 LIMIT 1"
                ).fetchone() is not None
        except sqlite3.Error:
            return False

    def full_replay(self, source_id: int) -> int:
        """Create a fresh source job without changing the selected historic run."""
        return self.store.create_operator_replay(source_id)

    def chat_link(self, link: dict) -> str | None:
        company_id = link.get("company_id") or self.company_id
        chat_id = link.get("chat_id")
        if not company_id or not chat_id:
            return None
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
        if not set(str(company_id)) <= allowed or not set(str(chat_id)) <= allowed:
            return None
        return f"https://yougile.com/team/{str(company_id)[-12:]}/#chat:{str(chat_id)[-12:]}"

    def view(self, source_id: int, target: str, limit: int | None = 20, raw: bool = False) -> None:
        if limit is not None and (limit < 1 or limit > 100):
            raise RunConsoleError("limit must be between 1 and 100")
        detail = self.detail(source_id)
        recognition = detail["recognition"]
        if not recognition:
            raise RunConsoleError("Recognition has not been created")
        result_dir = Path(recognition["result_dir"])
        if target == "scan":
            path = self._safe_path(str(result_dir / "scan.sqlite3"), self.runs_root)
            if not path:
                raise RunConsoleError("scan.sqlite3 is unavailable")
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as scan:
                quick_check = scan.execute("PRAGMA quick_check").fetchone()[0]
                states = scan.execute("SELECT state,COUNT(*) FROM attempts GROUP BY state ORDER BY state").fetchall()
                responses = scan.execute(
                    "SELECT http_status,acr_code,COUNT(*) FROM attempts GROUP BY http_status,acr_code "
                    "ORDER BY http_status,acr_code"
                ).fetchall()
                latest_query = (
                    "SELECT window_index,start_seconds,duration_seconds,state,http_status,acr_code,error_type "
                    "FROM attempts ORDER BY id DESC"
                )
                if limit is not None:
                    latest_query += " LIMIT ?"
                    latest = scan.execute(latest_query, (limit,)).fetchall()
                else:
                    latest = scan.execute(latest_query).fetchall()
            print(f"Scan database: {path}")
            print(f"Integrity: {quick_check}")
            print("States:", ", ".join(f"{state}={count}" for state, count in states))
            print("Responses:", ", ".join(f"HTTP {http_status}/ACR {acr_code}={count}" for http_status, acr_code, count in responses))
            print("Latest windows:")
            for row in latest:
                print("  window={0} start={1:g}s duration={2:g}s state={3} http={4} acr={5} error={6}".format(*row))
            return
        if target == "responses":
            path = self._safe_path(str(result_dir / "responses.jsonl"), self.runs_root)
            if not path:
                raise RunConsoleError("responses.jsonl is unavailable")
            print(f"Responses log: {path}")
            with path.open(encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if limit is not None and number > limit:
                        break
                    record = json.loads(line)
                    if raw:
                        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
                        continue
                    raw_response = record.get("response_text")
                    music = []
                    try:
                        payload = json.loads(raw_response) if isinstance(raw_response, str) else {}
                        music = payload.get("metadata", {}).get("music", [])
                    except (TypeError, ValueError):
                        pass
                    titles = [str(item.get("title") or "untitled") for item in music[:3] if isinstance(item, dict)]
                    print(
                        f"  window={record.get('window_index')} start={record.get('start_seconds')} "
                        f"http={record.get('http_status')} acr={record.get('acr_code')} "
                        f"candidates={'; '.join(titles) or '-'}"
                    )
            return
        if target == "aggregation":
            completed = next((row for row in detail["aggregation"] if row["state"] == "complete"), None)
            if not completed:
                raise RunConsoleError("No completed aggregation result is available")
            path = self._safe_path(
                str(self.aggregation_root / f"{recognition['id']}_{completed['id']}" / "result.json"),
                self.aggregation_root,
            )
            if not path:
                raise RunConsoleError("Aggregation result.json is unavailable")
            result = json.loads(path.read_text(encoding="utf-8"))
            print(f"Aggregation result: {path}")
            if raw:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
                return
            if isinstance(result, list):
                print(f"Appearances: {len(result)}")
                for number, item in enumerate(result if limit is None else result[:limit], 1):
                    if not isinstance(item, dict):
                        continue
                    period = item.get("period") if isinstance(item.get("period"), dict) else {}
                    artists = item.get("artist", [])
                    if isinstance(artists, str):
                        artists = [artists]
                    artists_text = ", ".join(str(value) for value in artists if value) if isinstance(artists, list) else ""
                    iswcs = item.get("iswc", [])
                    if isinstance(iswcs, str):
                        iswcs = [iswcs]
                    iswc_text = ", ".join(str(value) for value in iswcs if value) if isinstance(iswcs, list) else ""
                    details = (f" — {artists_text}" if artists_text else "") + (f"; ISWC: {iswc_text}" if iswc_text else "")
                    print(
                        f"  {number}: {_display_period(period)} "
                        f"{item.get('title') or 'untitled'}{details}"
                    )
                return
            if not isinstance(result, dict):
                raise RunConsoleError("Aggregation result.json has an unsupported format")
            families = {
                row.get("family_id"): row for row in result.get("track_families", [])
                if isinstance(row, dict) and row.get("family_id")
            }
            appearances = [row for row in result.get("appearances", []) if isinstance(row, dict)]
            print(f"Families: {len(families)}; appearances: {len(appearances)}")
            for item in appearances if limit is None else appearances[:limit]:
                family = families.get(item.get("family_id"), {})
                observed = item.get("observed_range") if isinstance(item.get("observed_range"), dict) else {}
                artists = ", ".join(str(value) for value in family.get("artists", []) if value)
                print(
                    f"  {item.get('appearance_id')}: {_display_period(observed)} "
                    f"{family.get('preferred_core_title') or 'untitled'}" + (f" — {artists}" if artists else "")
                )
            return
        raise RunConsoleError("view target must be scan, responses, or aggregation")


def _display_list(console: RunConsole) -> list[dict]:
    rows = console.runs()
    if not rows:
        print("No source jobs.")
        return rows
    display_rows = []
    for number, row in enumerate(rows, 1):
        status = row["recognition_state"] or row["stage"]
        error_type = _display_error_type(row["error_type"])
        suffix = f" ({error_type})" if error_type else ""
        name = _name_with_size(row["source_filename"] or "unnamed source", row["source_size"])
        recognition = f"R{row['recognition_id']}" if row["recognition_id"] else "-"
        completed = row["recognition_completed_utc"] or row["source_completed_utc"] or row["created_utc"]
        display_rows.append((
            number, _display_datetime(completed), f"S{row['source_job_id']}/{recognition}", status + suffix, name,
        ))
    number_width = max(len("#"), len(str(display_rows[-1][0])))
    date_width = max(len("date"), *(len(date) for _, date, _, _, _ in display_rows))
    selector_width = max(len("selector"), *(len(selector) for _, _, selector, _, _ in display_rows))
    status_width = max(len("status"), *(len(status) for _, _, _, status, _ in display_rows))
    print(
        f"{'#':<{number_width}}  {'date':<{date_width}}  {'selector':<{selector_width}}  "
        f"{'status':<{status_width}}  name"
    )
    for number, date, selector, status, name in display_rows:
        print(
            f"{number:<{number_width}}  {date:<{date_width}}  {selector:<{selector_width}}  "
            f"{status:<{status_width}}  {name}"
        )
    return rows


def _display_detail(console: RunConsole, source_id: int) -> None:
    detail = console.detail(source_id)
    source = detail["source"]
    recognition = detail["recognition"]
    print(f"Source job: S{source['id']} ({source['stage']})")
    print(f"Source file: {_name_with_size(source['source_filename'], source['source_size'])}")
    print(f"WAV: {_name_with_size(source['wav_path'], source['wav_size'])}")
    if source["wav_deleted_utc"]:
        print(f"WAV archived: {source['wav_deleted_utc']}")
    print(f"Yandex Disk: {_public_yandex_link(source['source_url']) or '[not a safe public Yandex link]'}")
    if recognition:
        print(f"Recognition: R{recognition['id']} ({recognition['state']})")
        print(f"Raw database: {Path(recognition['result_dir']) / 'scan.sqlite3'}")
        print(f"Responses log: {Path(recognition['result_dir']) / 'responses.jsonl'}")
        for aggregation in detail["aggregation"]:
            export = console.aggregation_root / f"{recognition['id']}_{aggregation['id']}"
            print(f"Aggregation A{aggregation['id']} ({aggregation['state']}): {export / 'result.json'}")
        if recognition["results_deleted_utc"]:
            print(f"Results archived: {recognition['results_deleted_utc']}")
    else:
        print("Recognition: not created")
    if detail["links"]:
        for link in detail["links"]:
            print(f"YouGile IDs: task={link['task_id']} chat={link['chat_id']} message={link['message_id']}")
            chat_link = console.chat_link(link)
            if chat_link:
                print(f"YouGile chat: {chat_link}")
            else:
                print("YouGile chat: unavailable (pass --company-id to build a direct chat link)")
    else:
        print("YouGile IDs: unavailable")


def _archive_command(console: RunConsole, selector: int, mode: str, confirmation: str) -> int:
    expected = f"TRASH S{selector}"
    if confirmation != expected:
        raise RunConsoleError(f"Confirmation must be exactly: {expected}")
    destinations = console.archive(selector, mode)
    print("Archived:")
    for destination in destinations:
        print(destination)
    return 0


def _continue_command(console: RunConsole, selector: int, confirmation: str) -> int:
    expected = f"CONTINUE S{selector}"
    if confirmation != expected:
        raise RunConsoleError(f"Confirmation must be exactly: {expected}")
    print(console.continue_run(selector))
    return 0


def _replay_command(console: RunConsole, selector: int, confirmation: str) -> int:
    expected = f"REPLAY S{selector}"
    if confirmation != expected:
        raise RunConsoleError(f"Confirmation must be exactly: {expected}")
    replay_id = console.full_replay(selector)
    print(f"Full replay queued as S{replay_id}; S{selector} was retained unchanged.")
    return 0


def _draw(screen: TerminalScreen, renderer) -> None:
    screen.set_renderer(renderer)
    screen.redraw()


@contextlib.contextmanager
def _noecho_cbreak_input():
    """Yield a TTY fd whose input is byte-oriented and not terminal-echoed."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            yield None
            return
        previous = termios.tcgetattr(fd)
    except (AttributeError, OSError, termios.error):
        yield None
        return
    updated = termios.tcgetattr(fd)
    updated[3] &= ~(termios.ICANON | termios.ECHO)
    updated[6][termios.VMIN] = 0
    updated[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, updated)
    except (OSError, termios.error):
        yield None
        return
    try:
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _consume_terminal_input(buffer: bytearray, data: bytes) -> bool:
    """Apply one raw terminal input chunk; return true after Enter."""
    for byte in data:
        if byte in {10, 13}:
            return True
        if byte in {8, 127}:
            if buffer:
                buffer.pop()
            continue
        if byte == 32 or 48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122:
            buffer.append(byte)
    return False


def _read_terminal_prompt(screen: TerminalScreen, message: str, fd: int) -> str:
    buffer = bytearray()
    while True:
        screen.set_footer(message + buffer.decode("ascii"))
        screen.redraw()
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except InterruptedError:
                ready = []
            if screen.resize_pending:
                break
            if not ready:
                continue
            data = os.read(fd, 64)
            if _consume_terminal_input(buffer, data):
                return buffer.decode("ascii").strip()
            break


def _consume_viewer_keys(pending: bytearray, data: bytes) -> list[str]:
    """Decode ASCII navigation keys and common terminal arrow sequences."""
    pending.extend(data)
    keys: list[str] = []
    while pending:
        if pending[0] != 27:
            byte = pending.pop(0)
            if byte in {ord("p"), ord("q")}:
                keys.append(chr(byte))
            continue
        if len(pending) == 1:
            break
        if pending[1] != ord("["):
            del pending[:2]
            continue
        final = next((index for index, byte in enumerate(pending[2:], 2) if 64 <= byte <= 126), None)
        if final is None:
            break
        sequence = bytes(pending[:final + 1])
        del pending[:final + 1]
        if sequence[-1:] == b"A":
            keys.append("fast_up" if b"5" in sequence else "up")
        elif sequence[-1:] == b"B":
            keys.append("fast_down" if b"5" in sequence else "down")
    return keys


def _interactive_viewer(console: RunConsole, screen: TerminalScreen, source_id: int, target: str) -> str:
    """Display every compact result row in a scrollable, read-only page."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        console.view(source_id, target, limit=None)
    source_lines = captured.getvalue().splitlines() or ["(empty)"]
    offset = 0
    pending = bytearray()
    lines: list[str] = []

    def refresh_lines() -> None:
        nonlocal lines, offset
        width = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
        lines = []
        for line in source_lines:
            lines.extend(textwrap.wrap(
                line, width=width, replace_whitespace=False, drop_whitespace=False,
                break_long_words=True, break_on_hyphens=False,
            ) or [""])
        offset = min(offset, len(lines) - 1)

    def render() -> None:
        for line in lines[offset:]:
            print(line)

    refresh_lines()
    with _noecho_cbreak_input() as fd:
        if fd is None:
            _draw(screen, render)
            return _prompt(screen, "Navigation: [p]revious [q]uit\n").lower()
        while True:
            refresh_lines()
            screen.set_renderer(render)
            screen.set_footer(
                f"View: {target}  Line {offset + 1}/{len(lines)}\n"
                "Navigation: [↑/↓] scroll [Ctrl+↑/↓] fast [p]revious [q]uit"
            )
            screen.redraw()
            while True:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                except InterruptedError:
                    ready = []
                if screen.resize_pending:
                    break
                if not ready:
                    continue
                keys = _consume_viewer_keys(pending, os.read(fd, 64))
                if not keys:
                    continue
                exit_key = next((key for key in keys if key in {"p", "q"}), None)
                if exit_key:
                    return exit_key
                for key in keys:
                    if key == "up":
                        offset = max(0, offset - 1)
                    elif key == "down":
                        offset = min(len(lines) - 1, offset + 1)
                    elif key == "fast_up":
                        offset = max(0, offset - 10)
                    elif key == "fast_down":
                        offset = min(len(lines) - 1, offset + 10)
                break


def _prompt(screen: TerminalScreen, message: str) -> str:
    if screen.enabled:
        with _noecho_cbreak_input() as fd:
            if fd is not None:
                return _read_terminal_prompt(screen, message, fd)
    while True:
        if screen.enabled:
            # Fallback for a non-TTY test stream: real terminals use the
            # no-echo cbreak reader above.
            screen.set_footer(message)
            screen.redraw()
            while True:
                try:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                except InterruptedError:
                    ready = []
                if screen.resize_pending:
                    screen.redraw()
                    continue
                if ready:
                    break
        try:
            if screen.enabled and hasattr(sys.stdin, "buffer"):
                # Terminal erase is byte-oriented.  After typing and erasing a
                # UTF-8 Cyrillic character, one invalid byte can remain in the
                # canonical input line.  Decode the bytes permissively so a
                # following valid ASCII command is still retained.
                raw = sys.stdin.buffer.readline().decode("utf-8", errors="ignore").rstrip("\r\n")
            else:
                raw = input() if screen.enabled else input(message)
        except UnicodeDecodeError:
            # The console accepts command keys and numeric selectors only.  A
            # terminal using a non-UTF-8 keyboard encoding must not terminate
            # the operator console before it can discard that input.
            continue
        value = "".join(
            character for character in raw
            if character.isascii() and (character.isalnum() or character == " ")
        ).strip()
        # Preserve a genuine Enter press, but silently ignore a line composed
        # only of unsupported characters (for example, Russian keyboard input).
        if raw and not value:
            continue
        if screen.resize_pending:
            screen.redraw()
            continue
        return value


def _interactive(console: RunConsole, screen: TerminalScreen) -> int:
    notice = ""
    while True:
        rows: list[dict] = []

        def draw_list() -> None:
            if notice:
                print(f"Input: {notice}")
            rows[:] = _display_list(console)

        _draw(screen, draw_list)
        if not rows:
            return 0
        selection = _prompt(screen, "Select a list number or S<ID> (q to quit)\nSelection: ")
        if selection.lower() == "q":
            return 0
        if not selection:
            notice = "enter a list number or source selector, for example S14"
            continue
        if selection.isdecimal() and 1 <= int(selection) <= len(rows):
            source_id = int(rows[int(selection) - 1]["source_job_id"])
        else:
            try:
                source_id = _as_selector(selection)
            except argparse.ArgumentTypeError as error:
                notice = str(error)
                continue
        notice = ""
        while True:
            _draw(screen, lambda: _display_detail(console, source_id))
            can_continue = console.can_continue(source_id)
            continuation = " [c]ontinue" if can_continue else ""
            action = _prompt(
                screen,
                f"File: [v]iew{continuation} [f]ull replay\n"
                "Delete: [a]udio [r]esults [b]oth\n"
                "Navigation: [p]revious [q]uit\n"
                "Action: ",
            ).lower()
            if not action:
                # Enter is deliberately inert in menus.  In particular it
                # must not fall through to the old "unknown action" exit.
                continue
            if action == "p":
                break
            if action == "q":
                return 0
            if action == "v":
                while True:
                    target = _prompt(
                        screen,
                        "View: [s]can, [r]esponses, or [a]ggregation\n"
                        "Navigation: [p]revious [q]uit\n",
                    ).lower()
                    if target == "p":
                        break
                    if target == "q":
                        return 0
                    targets = {"s": "scan", "r": "responses", "a": "aggregation"}
                    if target not in targets:
                        continue
                    if screen.enabled:
                        navigation = _interactive_viewer(console, screen, source_id, targets[target])
                    else:
                        _draw(screen, lambda: console.view(source_id, targets[target]))
                        navigation = _prompt(screen, "Navigation: [p]revious [q]uit\n").lower()
                    if navigation == "q":
                        return 0
                    if navigation in {"", "p"}:
                        break
                continue
            if action == "c" and not can_continue:
                continue
            if action in {"c", "f"}:
                verb = "CONTINUE" if action == "c" else "REPLAY"
                confirmation = _prompt(screen, f"Type {verb} S{source_id} to confirm: ")
                expected = f"{verb} S{source_id}"
                if confirmation != expected:
                    raise RunConsoleError(f"Confirmation must be exactly: {expected}")
                with screen.pause_resize():
                    if action == "c":
                        outcome = console.continue_run(source_id)
                    else:
                        replay_id = console.full_replay(source_id)
                        outcome = f"Full replay queued as S{replay_id}; S{source_id} was retained unchanged."
                _draw(screen, lambda: print(outcome))
                _prompt(screen, "Press Enter to return to the run: ")
                continue
            modes = {"a": "audio", "r": "results", "b": "all"}
            if action not in modes:
                return 0
            confirmation = _prompt(screen, f"Type TRASH S{source_id} to confirm: ")
            expected = f"TRASH S{source_id}"
            if confirmation != expected:
                raise RunConsoleError(f"Confirmation must be exactly: {expected}")
            with screen.pause_resize():
                archived = console.archive(source_id, modes[action])

            def show_archived() -> None:
                print("Archived:")
                for destination in archived:
                    print(destination)

            _draw(screen, show_archived)
            _prompt(screen, "Archived. Press Enter to exit: ")
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and safely archive music-verifier run artifacts")
    parser.add_argument("--db", type=Path, default=Path("data/queue/pipeline.sqlite3"), help="pipeline SQLite path")
    parser.add_argument("--company-id", help="YouGile company UUID used to build direct task-chat links")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="list source jobs")
    show = commands.add_parser("show", help="show one source job")
    show.add_argument("source", type=_as_selector)
    view = commands.add_parser("view", help="view compact recognition or aggregation results")
    view.add_argument("source", type=_as_selector)
    view.add_argument("target", choices=("scan", "responses", "aggregation"))
    view.add_argument("--limit", type=int, default=20, help="rows or appearances to show (1-100)")
    view.add_argument("--raw", action="store_true", help="show raw JSON records for responses or aggregation")
    continue_run = commands.add_parser("continue", help="resume a supported incomplete stage")
    continue_run.add_argument("source", type=_as_selector)
    continue_run.add_argument("--confirm", required=True, help="must be exactly CONTINUE S<ID>")
    replay = commands.add_parser("replay", help="queue a fresh full replay and retain the selected run")
    replay.add_argument("source", type=_as_selector)
    replay.add_argument("--confirm", required=True, help="must be exactly REPLAY S<ID>")
    for command, mode in (("trash-audio", "audio"), ("trash-results", "results"), ("trash-all", "all")):
        archive = commands.add_parser(command, help=f"move {mode} artifacts to data/.trash")
        archive.add_argument("source", type=_as_selector)
        archive.add_argument("--confirm", required=True, help="must be exactly TRASH S<ID>")
        archive.set_defaults(mode=mode)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = RunConsole(args.db, args.company_id)
    try:
        if args.command is None:
            if not sys.stdin.isatty():
                parser.error("choose a command when stdin is not interactive")
            with TerminalScreen(sys.stdout, sys.stdout.isatty()) as screen:
                return _interactive(console, screen)
        if args.command == "list":
            _display_list(console)
            return 0
        if args.command == "show":
            _display_detail(console, args.source)
            return 0
        if args.command == "view":
            console.view(args.source, args.target, args.limit, args.raw)
            return 0
        if args.command == "continue":
            return _continue_command(console, args.source, args.confirm)
        if args.command == "replay":
            return _replay_command(console, args.source, args.confirm)
        return _archive_command(console, args.source, args.mode, args.confirm)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except (RunConsoleError, StoreError, sqlite3.Error, OSError) as error:
        print(f"yougile-runs: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
