import asyncio
import hashlib
import json
import math
import os
import re
import select
import shutil
import socket
import subprocess
import time
import wave
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from job_store import (
    PipelineStore, StoreError, forced_run_prep_hash, is_forced_run_prep_hash, stable_hash,
)
from scan import ScanStop, inspect_audio


def env_ids(name: str) -> set[str]:
    return {value.strip() for value in os.getenv(name, "").split(",") if value.strip()}


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def validate_webhook_secret(value: str) -> str:
    secret = value.strip()
    if not secret or secret in {
        "change-me-before-start",
        "replace-with-a-long-random-secret",
    }:
        raise RuntimeError("WEBHOOK_SECRET must be explicitly configured")
    return secret


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
INCOMING_DIR = DATA_DIR / "incoming"
AUDIO_DIR = DATA_DIR / "audio"
EVENTS_DIR = DATA_DIR / "events"
QUEUE_DIR = DATA_DIR / "queue"
PIPELINE_DB = QUEUE_DIR / "pipeline.sqlite3"
STORE = PipelineStore(PIPELINE_DB)

MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_MB", "500")) * 1024 * 1024
YANDEX_DISK_MAX_VIDEO_BYTES = int(os.getenv("YANDEX_DISK_MAX_VIDEO_MB", "10240")) * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "28800"))
DOWNLOAD_PROGRESS_INTERVAL_SECONDS = 5
WAV_PROGRESS_INTERVAL_SECONDS = 5
SOURCE_LEASE_SECONDS = 900
SOURCE_LEASE_HEARTBEAT_SECONDS = 60
AUDIO_RETRY_DELAYS_SECONDS = (30, 120, 300)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
YOUGILE_API_KEY = os.getenv("YOUGILE_API_KEY", "")
YOUGILE_API_BASE = os.getenv("YOUGILE_API_BASE", "https://yougile.com/api-v2").rstrip("/")
YOUGILE_POLL_TASK_ID = os.getenv("YOUGILE_POLL_TASK_ID", "")
YOUGILE_POLL_CHAT_ID = os.getenv("YOUGILE_POLL_CHAT_ID", "")
YOUGILE_POLL_ENABLED = env_flag("YOUGILE_POLL_ENABLED", False)
YOUGILE_ALLOWED_BOARD_IDS = env_ids("YOUGILE_ALLOWED_BOARD_IDS")
YOUGILE_ALLOWED_COLUMN_IDS = env_ids("YOUGILE_ALLOWED_COLUMN_IDS")
YOUGILE_ALLOWED_TASK_IDS = env_ids("YOUGILE_ALLOWED_TASK_IDS")
YOUGILE_ALLOWED_CHAT_IDS = env_ids("YOUGILE_ALLOWED_CHAT_IDS")
if YOUGILE_POLL_TASK_ID:
    YOUGILE_ALLOWED_TASK_IDS.add(YOUGILE_POLL_TASK_ID)
if YOUGILE_POLL_CHAT_ID:
    YOUGILE_ALLOWED_CHAT_IDS.add(YOUGILE_POLL_CHAT_ID)
YOUGILE_POLL_INTERVAL_SECONDS = max(60, int(os.getenv("YOUGILE_POLL_INTERVAL_SECONDS", "60")))
YOUGILE_POLL_INITIAL_LIMIT = int(os.getenv("YOUGILE_POLL_INITIAL_LIMIT", "20"))
YOUGILE_POLL_MAX_CHATS_PER_CYCLE = int(os.getenv("YOUGILE_POLL_MAX_CHATS_PER_CYCLE", "35"))
YOUGILE_MAX_REQUESTS_PER_MINUTE = min(
    40, max(1, int(os.getenv("YOUGILE_MAX_REQUESTS_PER_MINUTE", "40")))
)
YOUGILE_SCOPE_REFRESH_SECONDS = int(os.getenv("YOUGILE_SCOPE_REFRESH_SECONDS", "300"))
MIN_FREE_DISK_BYTES = int(os.getenv("MIN_FREE_DISK_MB", "512")) * 1024 * 1024
YANDEX_API_BASE = "https://cloud-api.yandex.net/v1/disk/public/resources"
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
ALLOWED_YOUGILE_AUDIO_EXTENSIONS = {".wav"}
ALLOWED_YOUGILE_EXTENSIONS = {".mp4", ".mov"} | ALLOWED_YOUGILE_AUDIO_EXTENSIONS
YANDEX_FOLDER_VIDEO_EXTENSIONS = {".mp4", ".mov"}
YANDEX_GROUP_SILENCE_SECONDS = 12
YANDEX_PUBLIC_HOSTS = {
    "disk.yandex.ru", "disk.yandex.com", "disk.yandex.kz", "disk.yandex.by",
    "disk.yandex.uz", "disk.yandex.com.tr", "yadi.sk",
}
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
YOUGILE_FILE_RE = re.compile(r"/root/#file:(/user-data/[^\s\"'<>]+)", re.IGNORECASE)
YOUGILE_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")
PREP_CONFIG_HASH = stable_hash({
    "audio_codec": "pcm_s16le", "channels": 1, "sample_rate": 44100,
    "ffmpeg_atomic_output": True, "version": 1,
})

YOUGILE_HTTP_LOG_PREFIX = "[YOUGILE-HTTP] "
YOUGILE_ROUTE_TEMPLATES = {
    "webhook_delivery": "/webhooks/yougile/{secret_hidden}",
    "scope_columns": "/columns",
    "scope_tasks": "/tasks",
    "scope_task": "/tasks/{task_id}",
    "scope_column": "/columns/{column_id}",
    "message_detail": "/chats/{chat_id}/messages/{message_id}",
    "chat_messages": "/chats/{chat_id}/messages",
    "notification_message": "/chats/{chat_id}/messages",
    "notification_upload": "/upload-file",
    "webhook_list": "/webhooks",
    "webhook_create": "/webhooks",
}


def log_yougile_http(
    direction: str,
    method: str,
    request_kind: str,
    status: int | None,
    started: float,
    error_type: str | None = None,
) -> None:
    record = {
        "direction": direction,
        "method": method,
        "route": YOUGILE_ROUTE_TEMPLATES.get(request_kind, "/<redacted>"),
        "kind": request_kind,
        "status": status,
        "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
        "outcome": "ok" if status is not None and status < 400 else "error",
    }
    if error_type:
        record["error_type"] = error_type
    print(YOUGILE_HTTP_LOG_PREFIX + json.dumps(record, separators=(",", ":")), flush=True)

app = FastAPI(title="YouGile music verifier receiver", version="0.5.0")


@app.middleware("http")
async def safe_yougile_http_audit(request: Request, call_next):
    if not request.url.path.startswith("/webhooks/yougile/"):
        return await call_next(request)
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as error:
        log_yougile_http(
            "in", request.method, "webhook_delivery", 500, started,
            type(error).__name__,
        )
        raise
    log_yougile_http(
        "in", request.method, "webhook_delivery", response.status_code, started
    )
    return response


class YouGileRateLimited(RuntimeError):
    def __init__(self, retry_after: float):
        super().__init__("YouGile request rate limited")
        self.retry_after = retry_after


class RemoteMediaProcessError(RuntimeError):
    """Remote FFmpeg may have failed because its temporary URL or transport expired."""


def audio_retry_delay(error: Exception, attempt: int) -> float | None:
    if attempt < 1:
        raise ValueError("Invalid audio attempt number")
    retryable = isinstance(error, (
        YouGileRateLimited,
        httpx.TransportError,
        ConnectionError,
        TimeoutError,
    ))
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        retryable = status == 429 or 500 <= status <= 599
    max_attempts = 2 if isinstance(error, RemoteMediaProcessError) else (
        len(AUDIO_RETRY_DELAYS_SECONDS) + 1
    )
    if not retryable and not isinstance(error, RemoteMediaProcessError):
        return None
    if attempt >= max_attempts:
        return None
    delay = float(AUDIO_RETRY_DELAYS_SECONDS[attempt - 1])
    if isinstance(error, YouGileRateLimited):
        delay = max(delay, float(error.retry_after))
    return delay


class PollResult(int):
    def __new__(
        cls,
        queued: int,
        *,
        messages_read: int,
        messages_skipped: int,
        messages_new: int,
        baseline_required: bool = False,
    ):
        result = int.__new__(cls, queued)
        result.messages_read = messages_read
        result.messages_skipped = messages_skipped
        result.messages_new = messages_new
        result.baseline_required = baseline_required
        return result


class PersistentYouGileLimiter:
    def __init__(self, store: PipelineStore, max_requests: int):
        self.store = store
        self.max_requests = max_requests

    async def acquire(self, request_kind: str) -> None:
        while True:
            wait_seconds = await asyncio.to_thread(
                self.store.reserve_yougile_request, request_kind, self.max_requests, 60
            )
            if wait_seconds <= 0:
                return
            await asyncio.sleep(wait_seconds)

    async def defer(self, retry_after: float) -> None:
        await asyncio.to_thread(self.store.defer_yougile_requests, retry_after)


YOUGILE_LIMITER = PersistentYouGileLimiter(STORE, YOUGILE_MAX_REQUESTS_PER_MINUTE)
SCOPE_CACHE = {"expires": 0.0, "chat_ids": set(), "task_ids": set()}


def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def extract_urls(payload: dict) -> list[str]:
    urls: list[str] = []
    for text in iter_strings(payload):
        urls.extend(url.rstrip(").,;]}>") for url in URL_RE.findall(text))
        if text.startswith("/user-data/"):
            urls.append("https://yougile.com" + text)
    return list(dict.fromkeys(urls))


def last_yandex_public_url(payload: dict) -> str | None:
    """Return the final Yandex Disk public URL in a chat message.

    Chat messages also contain reference links (for example Shazam URLs).  Only
    the last Yandex Disk link is an intake source; every other URL is metadata.
    """
    urls = [url for url in extract_urls(payload) if is_yandex_public_url(url)]
    return urls[-1] if urls else None


def parse_yougile_file_path(file_path: str) -> dict:
    parsed = urlparse(file_path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Invalid YouGile file path")
    raw_parts = parsed.path.split("/")
    if len(raw_parts) != 4 or raw_parts[:2] != ["", "user-data"]:
        raise ValueError("Invalid YouGile file path")
    file_id = unquote(raw_parts[2])
    filename = unquote(raw_parts[3])
    if not YOUGILE_FILE_ID_RE.fullmatch(file_id) or filename in {"", ".", ".."}:
        raise ValueError("Invalid YouGile file identifier")
    if "/" in filename or "\\" in filename:
        raise ValueError("Invalid YouGile filename")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_YOUGILE_EXTENSIONS:
        raise ValueError(f"Not a supported YouGile file: {suffix or 'unknown type'}")
    return {
        "file_id": file_id,
        "file_path": parsed.path,
        "filename": safe_filename(filename, "yougile-video.mp4"),
    }


def extract_yougile_file_refs(message: dict) -> list[dict]:
    text = message.get("text")
    if not isinstance(text, str):
        return []
    refs = []
    seen = set()
    for match in YOUGILE_FILE_RE.finditer(text):
        file_path = match.group(1).rstrip(").,;]}")
        ref = parse_yougile_file_path(file_path)
        key = (ref["file_id"], ref["file_path"])
        if key not in seen:
            refs.append(ref)
            seen.add(key)
    return refs


def extract_yougile_wav_refs(message: dict) -> list[dict]:
    text = message.get("text")
    if not isinstance(text, str):
        return []
    refs = []
    seen = set()
    for match in YOUGILE_FILE_RE.finditer(text):
        file_path = match.group(1).rstrip(").,;]}")
        filename = unquote(Path(urlparse(file_path).path).name)
        if Path(filename).suffix.lower() not in ALLOWED_YOUGILE_AUDIO_EXTENSIONS:
            continue
        ref = parse_yougile_file_path(file_path)
        key = (ref["file_id"], ref["file_path"])
        if key not in seen:
            refs.append(ref)
            seen.add(key)
    return refs


def safe_value_error_text(error: Exception) -> str:
    text = str(error).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"https?://\S+", "[redacted-url]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\bauthorization\s*:\s*bearer\s+\S+",
        "[redacted-credential]",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|secret)\b\s*[:=]\s*\S+|\bbearer\s+\S+",
        "[redacted-credential]",
        text,
    )
    text = " ".join(text.split())
    return text[:240] or "ValueError"


def value_error_log_suffix(error: Exception) -> str:
    if not isinstance(error, ValueError):
        return ""
    return " detail=" + json.dumps(safe_value_error_text(error), ensure_ascii=False)


def http_status_log_suffix(error: Exception) -> str:
    """Expose only an HTTP status; never include request or response content."""
    if isinstance(error, httpx.HTTPStatusError) and error.response is not None:
        return f" status={error.response.status_code}"
    return ""


def yougile_file_url(file_path: str) -> str:
    parse_yougile_file_path(file_path)
    api = urlparse(YOUGILE_API_BASE)
    if api.scheme != "https" or not api.netloc:
        raise ValueError("YOUGILE_API_BASE must use HTTPS for file downloads")
    return urlunsplit((api.scheme, api.netloc, file_path, "", ""))


def retry_after_seconds(headers) -> float:
    value = headers.get("retry-after") if headers else None
    if not value:
        return 60.0
    try:
        return max(float(value), 1.0)
    except (TypeError, ValueError):
        try:
            return max(parsedate_to_datetime(value).timestamp() - time.time(), 1.0)
        except (TypeError, ValueError, OverflowError):
            return 60.0


async def yougile_get_json(path: str, *, params=None, request_kind: str) -> dict | list:
    if not YOUGILE_API_KEY:
        raise RuntimeError("YOUGILE_API_KEY is not configured")
    await YOUGILE_LIMITER.acquire(request_kind)
    headers = {"Authorization": f"Bearer {YOUGILE_API_KEY}"}
    url = f"{YOUGILE_API_BASE}{path}"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, params=params)
    except Exception as error:
        log_yougile_http("out", "GET", request_kind, None, started, type(error).__name__)
        raise
    log_yougile_http("out", "GET", request_kind, response.status_code, started)
    if response.status_code == 429:
        retry_after = retry_after_seconds(response.headers)
        await YOUGILE_LIMITER.defer(retry_after)
        raise YouGileRateLimited(retry_after)
    response.raise_for_status()
    return response.json()


async def yougile_post_json(path: str, payload: dict, *, request_kind: str) -> dict | list:
    if not YOUGILE_API_KEY:
        raise RuntimeError("YOUGILE_API_KEY is not configured")
    await YOUGILE_LIMITER.acquire(request_kind)
    headers = {"Authorization": f"Bearer {YOUGILE_API_KEY}"}
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(f"{YOUGILE_API_BASE}{path}", headers=headers, json=payload)
    except Exception as error:
        log_yougile_http("out", "POST", request_kind, None, started, type(error).__name__)
        raise
    log_yougile_http("out", "POST", request_kind, response.status_code, started)
    if response.status_code == 429:
        retry_after = retry_after_seconds(response.headers)
        await YOUGILE_LIMITER.defer(retry_after)
        raise YouGileRateLimited(retry_after)
    response.raise_for_status()
    return response.json()


def uploaded_yougile_file_path(payload) -> str:
    """Extract only a safe /user-data reference from an upload response."""
    if isinstance(payload, str):
        value = payload
        if value.startswith("/root/#file:"):
            value = value.removeprefix("/root/#file:")
        return value if value.startswith("/user-data/") else ""
    if isinstance(payload, dict):
        for value in payload.values():
            found = uploaded_yougile_file_path(value)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = uploaded_yougile_file_path(value)
            if found:
                return found
    return ""


async def upload_yougile_report(path: Path) -> str:
    if not YOUGILE_API_KEY or not path.is_file() or path.suffix.lower() != ".md":
        raise ValueError("Invalid aggregation report attachment")
    await YOUGILE_LIMITER.acquire("notification_upload")
    headers = {"Authorization": f"Bearer {YOUGILE_API_KEY}"}
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            with path.open("rb") as handle:
                response = await client.post(
                    f"{YOUGILE_API_BASE}/upload-file",
                    headers=headers,
                    files={"file": (path.name, handle, "text/markdown")},
                )
    except Exception as error:
        log_yougile_http(
            "out", "POST", "notification_upload", None, started, type(error).__name__
        )
        raise
    log_yougile_http(
        "out", "POST", "notification_upload", response.status_code, started
    )
    if response.status_code == 429:
        retry_after = retry_after_seconds(response.headers)
        await YOUGILE_LIMITER.defer(retry_after)
        raise YouGileRateLimited(retry_after)
    response.raise_for_status()
    try:
        attachment_path = uploaded_yougile_file_path(response.json())
    except ValueError:
        attachment_path = ""
    if not attachment_path:
        raise ValueError("YouGile upload response has no safe file reference")
    return attachment_path


async def fetch_all_yougile(path: str, request_kind: str) -> list[dict]:
    rows = []
    offset = 0
    while True:
        body = await yougile_get_json(
            path, params={"limit": 1000, "offset": offset}, request_kind=request_kind
        )
        page = body if isinstance(body, list) else next(
            (body[key] for key in ("content", "list", "items", "data")
             if isinstance(body.get(key), list)),
            [],
        )
        if not all(isinstance(item, dict) for item in page):
            raise ValueError("Unexpected YouGile list")
        rows.extend(page)
        paging = body.get("paging", {}) if isinstance(body, dict) else {}
        if not paging.get("next") or not page:
            return rows
        offset += len(page)


async def allowed_scope(force: bool = False) -> tuple[set[str], set[str]]:
    now = time.monotonic()
    if not force and SCOPE_CACHE["expires"] > now:
        return set(SCOPE_CACHE["chat_ids"]), set(SCOPE_CACHE["task_ids"])
    task_ids = set(YOUGILE_ALLOWED_TASK_IDS)
    column_ids = set(YOUGILE_ALLOWED_COLUMN_IDS)
    if YOUGILE_ALLOWED_BOARD_IDS:
        columns = await fetch_all_yougile("/columns", "scope_columns")
        column_ids.update(
            str(column["id"])
            for column in columns
            if str(column.get("boardId") or "") in YOUGILE_ALLOWED_BOARD_IDS and column.get("id")
        )
    if column_ids:
        tasks = await fetch_all_yougile("/tasks", "scope_tasks")
        task_ids.update(
            str(task["id"])
            for task in tasks
            if str(task.get("columnId") or "") in column_ids and task.get("id")
        )
    chat_ids = set(YOUGILE_ALLOWED_CHAT_IDS) | task_ids
    SCOPE_CACHE.update(
        expires=now + max(YOUGILE_SCOPE_REFRESH_SECONDS, 60),
        chat_ids=set(chat_ids),
        task_ids=set(task_ids),
    )
    return chat_ids, task_ids


def is_yandex_public_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in YANDEX_PUBLIC_HOSTS


def find_value(payload, names: set[str]):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in names and isinstance(value, (str, int)):
                return value
            found = find_value(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_value(value, names)
            if found is not None:
                return found
    return None


def event_company_id(payload: dict) -> str | None:
    value = find_value(payload, {"companyid", "company_id", "organizationid", "organization_id"})
    return str(value) if value is not None else None


def event_task_id(payload: dict) -> str | None:
    event_payload = payload.get("payload")
    if not isinstance(event_payload, dict):
        return None
    properties = event_payload.get("properties")
    candidates = [
        event_payload.get("taskId"),
        event_payload.get("task_id"),
        properties.get("taskId") if isinstance(properties, dict) else None,
        properties.get("task_id") if isinstance(properties, dict) else None,
    ]
    if str(payload.get("event") or "").startswith("task-"):
        candidates.append(event_payload.get("id"))
    for value in candidates:
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def event_chat_id(payload: dict) -> str | None:
    event_payload = payload.get("payload")
    if isinstance(event_payload, dict):
        value = event_payload.get("chatId") or event_payload.get("chat_id")
        if isinstance(value, str) and value:
            return value
    if str(payload.get("event") or "").startswith("task-"):
        task_id = event_task_id(payload)
        if task_id:
            return task_id
    value = find_value(payload, {"chatid", "chat_id"})
    return str(value) if value is not None else None


def event_message_id(payload: dict) -> str | None:
    event_payload = payload.get("payload")
    if isinstance(event_payload, dict):
        value = event_payload.get("id")
        if isinstance(value, (str, int)):
            return str(value)
    value = find_value(payload, {"messageid", "message_id"})
    return str(value) if value is not None else None


def _object_id(value) -> str:
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, dict):
        candidate = value.get("id") or value.get("columnId") or value.get("column_id")
        return str(candidate) if isinstance(candidate, (str, int)) else ""
    return ""


def event_target_column_id(payload: dict) -> str | None:
    event_payload = payload.get("payload")
    if not isinstance(event_payload, dict):
        return None
    properties = event_payload.get("properties")
    values = [
        event_payload.get("to"),
        event_payload.get("columnId"),
        event_payload.get("column_id"),
        event_payload.get("targetColumnId"),
        properties.get("to") if isinstance(properties, dict) else None,
        properties.get("columnId") if isinstance(properties, dict) else None,
        properties.get("targetColumnId") if isinstance(properties, dict) else None,
    ]
    task = event_payload.get("task")
    if isinstance(task, dict):
        values.extend((task.get("columnId"), task.get("column_id")))
    for value in values:
        candidate = _object_id(value)
        if candidate:
            return candidate
    return None


def task_move_scope_decision(payload: dict, task_id: str) -> bool | None:
    """Decide from the event when possible; None requires one task lookup."""
    target_column = event_target_column_id(payload)
    if YOUGILE_ALLOWED_COLUMN_IDS and target_column:
        return target_column in YOUGILE_ALLOWED_COLUMN_IDS
    if task_id in YOUGILE_ALLOWED_TASK_IDS or task_id in YOUGILE_ALLOWED_CHAT_IDS:
        return True
    return None


def move_into_configured_scope(payload: dict, chat_id: str) -> bool:
    event_payload = payload.get("payload")
    if not isinstance(event_payload, dict):
        return False
    properties = event_payload.get("properties")
    if not isinstance(properties, dict) or properties.get("move") is not True:
        return False
    if properties.get("fromSystem") is not True:
        return False
    task_id = str(properties.get("taskId") or "")
    target_column = str(properties.get("to") or "")
    if task_id != chat_id or not target_column:
        return False
    if YOUGILE_ALLOWED_COLUMN_IDS:
        return target_column in YOUGILE_ALLOWED_COLUMN_IDS
    return bool(
        chat_id in YOUGILE_ALLOWED_TASK_IDS
        or chat_id in YOUGILE_ALLOWED_CHAT_IDS
        or YOUGILE_ALLOWED_BOARD_IDS
    )


async def webhook_task_in_scope(task_id: str, get_json=yougile_get_json) -> bool:
    """Validate one webhook task without listing or polling all YouGile tasks."""
    if not task_id:
        return False
    if task_id in YOUGILE_ALLOWED_TASK_IDS or task_id in YOUGILE_ALLOWED_CHAT_IDS:
        return True
    if not YOUGILE_ALLOWED_COLUMN_IDS and not YOUGILE_ALLOWED_BOARD_IDS:
        return False
    task = await get_json(f"/tasks/{task_id}", request_kind="scope_task")
    if not isinstance(task, dict):
        raise ValueError("Unexpected YouGile task")
    column_id = str(task.get("columnId") or task.get("column_id") or "")
    if column_id in YOUGILE_ALLOWED_COLUMN_IDS:
        return True
    board_id = str(task.get("boardId") or task.get("board_id") or "")
    if board_id in YOUGILE_ALLOWED_BOARD_IDS:
        return True
    if YOUGILE_ALLOWED_BOARD_IDS and column_id:
        column = await get_json(f"/columns/{column_id}", request_kind="scope_column")
        if not isinstance(column, dict):
            raise ValueError("Unexpected YouGile column")
        return str(column.get("boardId") or column.get("board_id") or "") in YOUGILE_ALLOWED_BOARD_IDS
    return False


async def enrich_from_message_api(payload: dict) -> dict:
    if not YOUGILE_API_KEY:
        return payload
    chat_id = event_chat_id(payload)
    message_id = event_message_id(payload)
    if not chat_id or not message_id:
        return payload
    message = await yougile_get_json(
        f"/chats/{chat_id}/messages/{message_id}", request_kind="message_detail"
    )
    return {"webhook": payload, "yougile_message": message}


async def fetch_chat_messages(chat_id: str, limit: int, offset: int = 0) -> list[dict]:
    if limit < 1 or offset < 0:
        raise ValueError("Invalid YouGile message page")
    body = await yougile_get_json(
        f"/chats/{chat_id}/messages",
        params={"limit": limit, "offset": offset},
        request_kind="chat_messages",
    )
    messages = body.get("content") if isinstance(body, dict) else body
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("Unexpected YouGile message list")
    return messages


def message_id(message: dict) -> str | None:
    value = message.get("id")
    return str(value) if isinstance(value, (str, int)) else None


def message_order_key(value: str):
    if value.isdigit():
        return (0, int(value))
    match = re.search(r"(\d+)$", value)
    if match:
        return (1, value[:match.start()], int(match.group(1)))
    return (2, value)


def verify_oldest_first(messages: list[dict]) -> str:
    ids = [message_id(message) for message in messages]
    if any(value is None for value in ids):
        raise ValueError("YouGile message without a durable ID")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate YouGile message ID in API snapshot")
    keys = [message_order_key(value) for value in ids]
    if any(left >= right for left, right in zip(keys, keys[1:])):
        raise ValueError("YouGile message order is not oldest-first")
    return "oldest_first"


async def fetch_history_once(fetch_messages, chat_id: str, limit: int) -> tuple[list[dict], int]:
    messages = []
    offset = 0
    pages = 0
    while True:
        page = await fetch_messages(chat_id, limit, offset)
        pages += 1
        messages.extend(page)
        offset += len(page)
        if len(page) < limit:
            return messages, pages


async def fetch_stable_history(
    fetch_messages,
    chat_id: str,
    limit: int,
    max_passes: int = 5,
) -> tuple[list[dict], int, int]:
    previous_ids = None
    total_pages = 0
    for pass_number in range(1, max_passes + 1):
        messages, pages = await fetch_history_once(fetch_messages, chat_id, limit)
        total_pages += pages
        verify_oldest_first(messages)
        ids = tuple(message_id(message) for message in messages)
        if pages == 1 or ids == previous_ids:
            return messages, total_pages, pass_number
        previous_ids = ids
    raise RuntimeError("YouGile history changed during pagination; baseline not committed")


async def baseline_yougile_chat(
    store: PipelineStore,
    task_id: str,
    chat_id: str,
    page_size: int = 20,
    fetch_messages=fetch_chat_messages,
) -> dict:
    if not task_id or not chat_id or page_size < 1:
        raise ValueError("Invalid YouGile baseline parameters")
    current = await asyncio.to_thread(store.baseline_state, task_id, chat_id)
    if current["status"] == "complete":
        result = {
            "status": "already_complete",
            "messages_seen": int(current["history_count"]),
            "messages_marked": 0,
            "source_jobs_created": 0,
            "pages": 0,
            "passes": 0,
            "message_order": current["message_order"],
        }
        print("[BASELINE] " + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return result
    await asyncio.to_thread(store.begin_baseline, task_id, chat_id)
    messages, pages, passes = await fetch_stable_history(fetch_messages, chat_id, page_size)
    ids = [message_id(message) for message in messages]
    completed = await asyncio.to_thread(
        store.complete_baseline, task_id, chat_id, ids, "oldest_first"
    )
    result = {
        "status": "complete",
        "messages_seen": len(ids),
        "messages_marked": int(completed["inserted"]),
        "source_jobs_created": 0,
        "pages": pages,
        "passes": passes,
        "message_order": "oldest_first",
    }
    print("[BASELINE] " + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return result


def safe_filename(filename: str, fallback: str) -> str:
    clean = Path(filename).name
    clean = re.sub(r"[^A-Za-zА-Яа-яЁё0-9._ -]+", "_", clean).strip(" .")
    clean = clean or fallback
    if len(clean) <= 180:
        return clean
    suffix = Path(clean).suffix
    if 1 < len(suffix) <= 16:
        stem_limit = 180 - len(suffix)
        return clean[:stem_limit].rstrip(" .") + suffix
    return clean[:180]


def validate_video_metadata(filename: str, mime_type: str) -> None:
    suffix = Path(filename).suffix.lower()
    mime_type = mime_type.lower()
    if suffix not in ALLOWED_VIDEO_EXTENSIONS and not mime_type.startswith("video/"):
        raise ValueError(f"Not a supported video: {mime_type or suffix or 'unknown type'}")


def natural_filename_key(value: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def ensure_free_space(required_bytes: int = 0) -> None:
    free = shutil.disk_usage(AUDIO_DIR).free
    if free < MIN_FREE_DISK_BYTES + max(int(required_bytes), 0):
        raise OSError("Insufficient free disk space")


async def yandex_get_json(params: dict) -> dict:
    timeout = httpx.Timeout(connect=30, read=300, write=30, pool=30)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(YANDEX_API_BASE, params=params)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Unexpected Yandex Disk response")
    return body


async def expand_yandex_public_source(public_url: str, fetch_json=None) -> tuple[list[dict], int]:
    if not is_yandex_public_url(public_url):
        raise ValueError("Not a supported Yandex Disk public link")
    fetch_json = fetch_json or yandex_get_json
    metadata = await fetch_json({
        "public_key": public_url,
        "fields": "name,size,mime_type,type,path",
    })
    if metadata.get("type") == "file":
        filename = safe_filename(
            str(metadata.get("name") or "yandex-video.mp4"), "yandex-video.mp4"
        )
        validate_video_metadata(filename, str(metadata.get("mime_type") or ""))
        return [{
            "kind": "yandex_disk",
            "url": public_url,
            "filename": filename,
            "size": int(metadata.get("size") or 0),
        }], 0
    if metadata.get("type") != "dir":
        raise ValueError("Unsupported Yandex Disk resource type")
    folder_name = safe_filename(str(metadata.get("name") or "yandex-folder"), "yandex-folder")
    items_found = []
    skipped = 0
    offset = 0
    page_size = 100
    while True:
        page = await fetch_json({
            "public_key": public_url,
            "limit": page_size,
            "offset": offset,
            "fields": "_embedded.items.name,_embedded.items.size,_embedded.items.mime_type,"
                      "_embedded.items.type,_embedded.items.path,_embedded.limit,"
                      "_embedded.offset,_embedded.total",
        })
        embedded = page.get("_embedded") or {}
        items = embedded.get("items") or []
        if not isinstance(items, list):
            raise ValueError("Unexpected Yandex Disk folder listing")
        for item in items:
            try:
                name = str(item.get("name") or "")
                item_path = item.get("path")
                if item.get("type") != "file" or not isinstance(item_path, str):
                    skipped += 1
                    continue
                if Path(name).suffix.lower() not in YANDEX_FOLDER_VIDEO_EXTENSIONS:
                    skipped += 1
                    continue
                items_found.append({
                    "item_path": item_path,
                    "filename": safe_filename(name, "yandex-video.mp4"),
                    "size": int(item.get("size") or 0),
                    "unlimited": True,
                })
            except (TypeError, ValueError):
                skipped += 1
        offset += len(items)
        total = int(embedded.get("total") or offset)
        if not items or offset >= total:
            if not items_found:
                return [], skipped
            items_found.sort(key=lambda item: (
                natural_filename_key(item["filename"]), item["item_path"]
            ))
            return [{
                "kind": "yandex_disk_group",
                "url": public_url,
                "filename": folder_name,
                "size": sum(item["size"] for item in items_found),
                "unlimited": True,
                "items": items_found,
            }], skipped


async def resolve_yandex_disk_video(
    public_url: str,
    *,
    item_path: str | None = None,
    filename_hint: str | None = None,
    size_hint: int = 0,
    unlimited: bool = False,
) -> tuple[str, int, str]:
    if not is_yandex_public_url(public_url):
        raise ValueError("Not a supported Yandex Disk public link")
    filename = filename_hint
    size = int(size_hint or 0)
    if item_path is None:
        metadata = await yandex_get_json({
            "public_key": public_url,
            "fields": "name,size,mime_type,type",
        })
        if metadata.get("type") != "file":
            raise ValueError("The Yandex Disk link must point to one file")
        filename = safe_filename(
            str(metadata.get("name") or "yandex-video.mp4"), "yandex-video.mp4"
        )
        validate_video_metadata(filename, str(metadata.get("mime_type") or ""))
        size = int(metadata.get("size") or 0)
    elif not filename:
        raise ValueError("Yandex Disk folder item filename is missing")
    if not unlimited and size and size > YANDEX_DISK_MAX_VIDEO_BYTES:
        raise ValueError("Yandex Disk video exceeds YANDEX_DISK_MAX_VIDEO_MB")
    params = {"public_key": public_url}
    if item_path is not None:
        params["path"] = item_path
    timeout = httpx.Timeout(connect=30, read=300, write=30, pool=30)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(f"{YANDEX_API_BASE}/download", params=params)
    response.raise_for_status()
    download_url = response.json().get("href")
    if not isinstance(download_url, str) or urlparse(download_url).scheme != "https":
        raise RuntimeError("Yandex Disk did not return a secure download URL")
    return filename, size, download_url


async def collect_message_sources(
    message: dict,
    task_id: str,
    message_id: str,
    expand_yandex=None,
) -> list[dict]:
    expand_yandex = expand_yandex or expand_yandex_public_source
    sources = [
        {
            "kind": "yougile_file",
            "url": ref["file_path"],
            "filename": ref["filename"],
            "file_id": ref["file_id"],
        }
        for ref in extract_yougile_file_refs(message)
    ]
    url = last_yandex_public_url(message)
    if url:
        expanded, _ = await expand_yandex(url)
        sources.extend(expanded)
    for candidate in extract_urls(message):
        if is_yandex_public_url(candidate):
            continue
        suffix = Path(urlparse(candidate).path).suffix.lower()
        if suffix in ALLOWED_VIDEO_EXTENSIONS:
            sources.append({"kind": "direct_video", "url": candidate})
    unique = []
    seen = set()
    for source in sources:
        identity = (source["kind"], source["url"], source.get("item_path"))
        if identity not in seen:
            unique.append(source)
            seen.add(identity)
    return unique


async def download_video(
    url: str,
    *,
    authorize_yougile: bool = False,
    filename_hint: str | None = None,
    rate_limiter=None,
    source_job_id: int | None = None,
) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only HTTPS download URLs are accepted")
    headers = {}
    api = urlparse(YOUGILE_API_BASE)
    same_yougile_origin = (parsed.scheme, parsed.netloc) == (api.scheme, api.netloc)
    if authorize_yougile:
        if not same_yougile_origin:
            raise ValueError("Refusing to send YouGile authorization to another origin")
        if not parsed.path.startswith("/user-data/"):
            raise ValueError("Refusing an unexpected YouGile download path")
        if not YOUGILE_API_KEY:
            raise RuntimeError("YOUGILE_API_KEY is not configured")
        headers["Authorization"] = f"Bearer {YOUGILE_API_KEY}"
    elif YOUGILE_API_KEY and same_yougile_origin:
        headers["Authorization"] = f"Bearer {YOUGILE_API_KEY}"
    limiter = rate_limiter or YOUGILE_LIMITER
    if same_yougile_origin:
        await limiter.acquire("file_download")
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as response:
            if getattr(response, "status_code", 200) == 429:
                retry_after = retry_after_seconds(response.headers)
                await limiter.defer(retry_after)
                raise YouGileRateLimited(retry_after)
            response.raise_for_status()
            content_length = int(response.headers.get("content-length", "0") or 0)
            if content_length > MAX_VIDEO_BYTES:
                raise ValueError("Media file exceeds MAX_VIDEO_MB")
            ensure_free_space(content_length)
            filename = safe_filename(filename_hint or unquote(Path(parsed.path).name), "video.mp4")
            suffix = Path(filename).suffix.lower()
            content_type = response.headers.get("content-type", "").lower()
            if (
                suffix not in ALLOWED_VIDEO_EXTENSIONS
                and suffix not in ALLOWED_YOUGILE_AUDIO_EXTENSIONS
                and not content_type.startswith("video/")
            ):
                raise ValueError(f"Not a supported media file: {content_type or suffix}")
            digest = hashlib.sha256(url.encode()).hexdigest()[:12]
            target = INCOMING_DIR / f"{digest}_{Path(filename).name}"
            temporary = target.with_suffix(target.suffix + ".part")
            received = 0
            started = time.monotonic()
            last_logged = started

            def log_progress(event: str) -> None:
                elapsed = max(time.monotonic() - started, 0.001)
                speed = received / elapsed
                fields = [
                    f"[DOWNLOAD] source_job={source_job_id if source_job_id is not None else '-'}",
                    f"event={event}",
                    f"received_bytes={received}",
                    f"speed_bytes_s={speed:.0f}",
                ]
                if content_length:
                    percent = min(100.0, received * 100 / content_length)
                    fields.extend((
                        f"total_bytes={content_length}",
                        f"progress_pct={percent:.1f}",
                    ))
                    if speed:
                        fields.append(f"eta_seconds={max(0.0, (content_length - received) / speed):.1f}")
                    else:
                        fields.append("eta_seconds=unknown")
                else:
                    fields.append("total_bytes=unknown")
                print(" ".join(fields), flush=True)

            log_progress("started")
            try:
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        received += len(chunk)
                        if received > MAX_VIDEO_BYTES:
                            raise ValueError("Media file exceeds MAX_VIDEO_MB")
                        output.write(chunk)
                        now = time.monotonic()
                        if now - last_logged >= DOWNLOAD_PROGRESS_INTERVAL_SECONDS:
                            log_progress("progress")
                            last_logged = now
                temporary.replace(target)
                log_progress("complete")
                return target
            except Exception:
                temporary.unlink(missing_ok=True)
                raise


def run_ffmpeg_with_progress(command: list[str], temporary: Path) -> None:
    """Run FFmpeg and expose its machine-readable conversion progress in the journal."""
    started = time.monotonic()
    print(f"[WAV] output={temporary.name} event=started", flush=True)
    progress_command = [
        command[0], "-stats_period", str(WAV_PROGRESS_INTERVAL_SECONDS), "-progress", "pipe:1", *command[1:]
    ]
    process = subprocess.Popen(
        progress_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    out_seconds = 0.0
    deadline = started + FFMPEG_TIMEOUT_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, FFMPEG_TIMEOUT_SECONDS)
            ready, _, _ = select.select([process.stdout], [], [], min(1.0, remaining))
            if ready:
                line = process.stdout.readline()
                if line:
                    name, _, value = line.strip().partition("=")
                    if name == "out_time_ms":
                        out_seconds = int(value or 0) / 1_000_000
                    elif name == "progress" and value == "continue":
                        print(
                            f"[WAV] output={temporary.name} event=progress "
                            f"out_seconds={out_seconds:.1f}",
                            flush=True,
                        )
            if process.poll() is not None:
                break
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        print(
            f"[WAV] output={temporary.name} event=complete out_seconds={out_seconds:.1f}",
            flush=True,
        )
    finally:
        if process.stdout:
            process.stdout.close()


def extract_audio(video_path: Path, audio_path: Path | None = None) -> Path:
    audio_path = audio_path or AUDIO_DIR / f"{video_path.stem}.wav"
    temporary = audio_path.with_suffix(".part.wav")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(video_path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "44100",
        "-c:a", "pcm_s16le", str(temporary),
    ]
    try:
        run_ffmpeg_with_progress(command, temporary)
        temporary.replace(audio_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return audio_path


def inspect_input_wav(path: Path) -> dict:
    size = path.stat().st_size
    if size < 44:
        raise ValueError("Invalid WAV: file is too small")
    if size > MAX_VIDEO_BYTES:
        raise ValueError("WAV exceeds MAX_VIDEO_MB")
    command = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "format=format_name,duration,size:stream=codec_type,duration",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True,
            timeout=min(FFMPEG_TIMEOUT_SECONDS, 300),
        )
        payload = json.loads(completed.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise ValueError("Invalid WAV: ffprobe rejected the file") from None
    format_info = payload.get("format") if isinstance(payload, dict) else None
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(format_info, dict) or "wav" not in str(format_info.get("format_name", "")).split(","):
        raise ValueError("Invalid WAV: unsupported container")
    if not isinstance(streams, list) or not any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    ):
        raise ValueError("Invalid WAV: audio stream is missing")
    duration_value = format_info.get("duration")
    if duration_value in (None, "N/A"):
        duration_value = next(
            (stream.get("duration") for stream in streams
             if isinstance(stream, dict) and stream.get("duration") not in (None, "N/A")),
            None,
        )
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        raise ValueError("Invalid WAV: duration is unavailable") from None
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid WAV: duration must be positive")
    return {"size_bytes": size, "duration_seconds": duration, "format_name": "wav"}


def extract_audio_from_url(download_url: str, audio_path: Path) -> Path:
    temporary = audio_path.with_suffix(".part.wav")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-i", download_url, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "44100",
        "-c:a", "pcm_s16le", str(temporary),
    ]
    try:
        run_ffmpeg_with_progress(command, temporary)
        temporary.replace(audio_path)
    except subprocess.CalledProcessError:
        temporary.unlink(missing_ok=True)
        raise RemoteMediaProcessError("Remote media processing failed") from None
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return audio_path


async def prepare_yandex_group(job: dict, store: PipelineStore) -> tuple[Path, dict]:
    try:
        items = json.loads(job.get("source_items_json") or "")
    except (TypeError, ValueError):
        raise ValueError("Invalid grouped Yandex Disk source") from None
    if not isinstance(items, list) or not items:
        raise ValueError("Grouped Yandex Disk source is empty")
    digest = job["source_hash"][:12]
    group_name = safe_filename(job.get("source_filename") or "yandex-folder", "yandex-folder")
    replay_suffix = f"_forced-replay-{job['id']}" if is_forced_run_prep_hash(
        job["prep_config_hash"]
    ) else ""
    audio_path = AUDIO_DIR / safe_filename(
        f"yandex_{digest}_{Path(group_name).stem}_combined{replay_suffix}.wav",
        f"yandex_{digest}_combined.wav",
    )
    if audio_path.exists():
        raise RuntimeError("UntrackedExistingWav")
    store.set_audio_plan(
        job["id"], job["claim_token"], group_name,
        sum(int(item.get("size") or 0) for item in items), audio_path,
    )
    ensure_free_space()
    combined_temporary = audio_path.with_suffix(".part.wav")
    item_audio = audio_path.with_name(f".{audio_path.stem}.source.wav")
    item_part = item_audio.with_suffix(".part.wav")
    manifest_items = []
    total_frames = 0
    rate = 44100
    try:
        with wave.open(str(combined_temporary), "wb") as combined:
            combined.setnchannels(1)
            combined.setsampwidth(2)
            combined.setframerate(rate)
            for index, item in enumerate(items):
                filename, source_size, download_url = await resolve_yandex_disk_video(
                    job["source_url"],
                    item_path=item["item_path"],
                    filename_hint=item["filename"],
                    size_hint=int(item.get("size") or 0),
                    unlimited=bool(item.get("unlimited", True)),
                )
                ensure_free_space()
                await asyncio.to_thread(extract_audio_from_url, download_url, item_audio)
                with wave.open(str(item_audio), "rb") as source:
                    if (
                        source.getnchannels() != 1
                        or source.getsampwidth() != 2
                        or source.getframerate() != rate
                        or source.getcomptype() != "NONE"
                    ):
                        raise ValueError("Unexpected intermediate WAV format")
                    if index:
                        silence_frames = YANDEX_GROUP_SILENCE_SECONDS * rate
                        combined.writeframesraw(b"\x00\x00" * silence_frames)
                        total_frames += silence_frames
                    start_frames = total_frames
                    remaining = source.getnframes()
                    while remaining:
                        chunk_frames = min(remaining, rate * 60)
                        chunk = source.readframes(chunk_frames)
                        if not chunk:
                            raise ValueError("Unexpected end of intermediate WAV")
                        combined.writeframesraw(chunk)
                        written = len(chunk) // 2
                        total_frames += written
                        remaining -= written
                    duration = source.getnframes() / rate
                item_audio.unlink(missing_ok=True)
                manifest_items.append({
                    "order": index + 1,
                    "filename": filename,
                    "item_path": item["item_path"],
                    "source_size": source_size,
                    "start_seconds": start_frames / rate,
                    "duration_seconds": duration,
                    "end_seconds": start_frames / rate + duration,
                })
        manifest = {
            "schema_version": 1,
            "ordering": "natural_filename",
            "inter_source_silence_seconds": YANDEX_GROUP_SILENCE_SECONDS,
            "items": manifest_items,
        }
        store.set_source_manifest(job["id"], job["claim_token"], manifest)
        combined_temporary.replace(audio_path)
        return audio_path, inspect_completed_wav(audio_path)
    except Exception:
        combined_temporary.unlink(missing_ok=True)
        item_audio.unlink(missing_ok=True)
        item_part.unlink(missing_ok=True)
        raise


def inspect_completed_wav(path: Path) -> dict:
    if not path.is_file() or path.name.endswith(".part.wav"):
        raise ScanStop("Completed WAV is missing")
    with path.open("rb") as source:
        return inspect_audio(source)


def remove_local_source(path_value: str | Path, output_path: str | Path | None = None) -> None:
    path = Path(path_value)
    incoming = INCOMING_DIR.resolve()
    resolved = path.resolve()
    if output_path is not None and resolved == Path(output_path).resolve():
        return
    if path.is_symlink() or resolved.parent != incoming:
        raise ValueError("Refusing to remove an unexpected local source path")
    path.unlink(missing_ok=True)


async def prepare_claimed_source(job: dict, store: PipelineStore) -> tuple[Path, dict]:
    source_url = job["source_url"]
    recovering = bool(job.get("wav_path"))
    replay_suffix = f"_forced-replay-{job['id']}" if is_forced_run_prep_hash(
        job["prep_config_hash"]
    ) else ""
    if recovering:
        audio_path = Path(job["wav_path"])
        if audio_path.exists():
            info = await asyncio.to_thread(inspect_completed_wav, audio_path)
            if job["source_kind"] == "yandex_disk" and not job.get("source_manifest_json"):
                store.set_source_manifest(job["id"], job["claim_token"], {
                    "schema_version": 1,
                    "ordering": "single_file",
                    "inter_source_silence_seconds": 0,
                    "items": [{
                        "order": 1,
                        "filename": job.get("source_filename") or audio_path.name,
                        "item_path": job.get("source_item_path"),
                        "source_size": int(job.get("source_size") or 0),
                        "start_seconds": 0.0,
                        "duration_seconds": info["duration_seconds"],
                        "end_seconds": info["duration_seconds"],
                    }],
                })
            if job.get("local_source_path"):
                await asyncio.to_thread(
                    remove_local_source, job["local_source_path"], audio_path
                )
                store.clear_local_source(job["id"], job["claim_token"])
            return audio_path, info
    if job["source_kind"] == "yandex_disk_group":
        return await prepare_yandex_group(job, store)
    if job["source_kind"] == "yandex_disk":
        filename, source_size, download_url = await resolve_yandex_disk_video(
            source_url,
            item_path=job.get("source_item_path"),
            filename_hint=job.get("source_filename"),
            size_hint=job.get("source_size") or 0,
            unlimited=bool(job.get("source_unlimited")),
        )
        digest = job["source_hash"][:12]
        audio_name = f"yandex_{digest}_{Path(filename).stem}{replay_suffix}.wav"
        audio_path = AUDIO_DIR / safe_filename(audio_name, f"yandex_{digest}.wav")
        if not recovering and audio_path.exists():
            raise RuntimeError("UntrackedExistingWav")
        store.set_audio_plan(job["id"], job["claim_token"], filename, source_size, audio_path)
        ensure_free_space()
        await asyncio.to_thread(extract_audio_from_url, download_url, audio_path)
        info = await asyncio.to_thread(inspect_completed_wav, audio_path)
        store.set_source_manifest(job["id"], job["claim_token"], {
            "schema_version": 1,
            "ordering": "single_file",
            "inter_source_silence_seconds": 0,
            "items": [{
                "order": 1,
                "filename": filename,
                "item_path": job.get("source_item_path"),
                "source_size": source_size,
                "start_seconds": 0.0,
                "duration_seconds": info["duration_seconds"],
                "end_seconds": info["duration_seconds"],
            }],
        })
    elif job["source_kind"] == "yougile_file":
        ref = parse_yougile_file_path(source_url)
        digest = job["source_hash"][:12]
        audio_path = AUDIO_DIR / safe_filename(
            f"yougile_{digest}_{Path(ref['filename']).stem}{replay_suffix}.wav",
            f"yougile_{digest}.wav",
        )
        if not recovering and audio_path.exists():
            raise RuntimeError("UntrackedExistingWav")
        context = store.polled_yougile_context(job["id"]) or {}
        print(
            f"[YOUGILE FILE] task_id={context.get('task_id', '-')} "
            f"message_id={context.get('message_id', '-')} file_id={ref['file_id']} stage=download",
            flush=True,
        )
        source_path = await download_video(
            yougile_file_url(source_url), authorize_yougile=True, filename_hint=ref["filename"],
            source_job_id=job["id"],
        )
        store.set_audio_plan(
            job["id"], job["claim_token"], ref["filename"], source_path.stat().st_size,
            audio_path, source_path,
        )
        ensure_free_space(source_path.stat().st_size)
        if Path(ref["filename"]).suffix.lower() in ALLOWED_YOUGILE_AUDIO_EXTENSIONS:
            input_info = await asyncio.to_thread(inspect_input_wav, source_path)
            print(
                f"[WAV INPUT] task_id={context.get('task_id', '-')} "
                f"message_id={context.get('message_id', '-')} file_id={ref['file_id']} "
                f"size={input_info['size_bytes']} duration={input_info['duration_seconds']:.3f}",
                flush=True,
            )
        await asyncio.to_thread(extract_audio, source_path, audio_path)
    else:
        parsed = urlparse(source_url)
        filename = safe_filename(Path(parsed.path).name or "video.mp4", "video.mp4")
        digest = job["source_hash"][:12]
        audio_path = AUDIO_DIR / safe_filename(
            f"{digest}_{Path(filename).stem}{replay_suffix}.wav", f"{digest}.wav"
        )
        if not recovering and audio_path.exists():
            raise RuntimeError("UntrackedExistingWav")
        video_path = await download_video(source_url)
        store.set_audio_plan(
            job["id"], job["claim_token"], filename, video_path.stat().st_size,
            audio_path, video_path,
        )
        ensure_free_space()
        await asyncio.to_thread(extract_audio, video_path, audio_path)
    info = await asyncio.to_thread(inspect_completed_wav, audio_path)
    current = store.source(job["id"])
    if current.get("local_source_path"):
        await asyncio.to_thread(
            remove_local_source, current["local_source_path"], audio_path
        )
        store.clear_local_source(job["id"], job["claim_token"])
    return audio_path, info


async def process_one_webhook(
    store: PipelineStore = STORE,
    enrich=enrich_from_message_api,
    scope=None,
    source_collector=collect_message_sources,
    move_poll=None,
    task_allowed=None,
) -> bool:
    job = await asyncio.to_thread(store.claim_webhook, socket.gethostname())
    if not job:
        return False
    try:
        payload = json.loads(job["payload_json"])
        event_name = job["event_name"]
        if event_name == "task-moved":
            task_id = event_task_id(payload) or job.get("chat_id") or ""
            decision = task_move_scope_decision(payload, task_id)
            if decision is None:
                if scope is not None:
                    allowed_chats, _ = await scope(force=True)
                    decision = task_id in allowed_chats
                else:
                    checker = task_allowed or webhook_task_in_scope
                    decision = await checker(task_id)
            if not decision:
                await asyncio.to_thread(
                    store.complete_webhook, job["id"], job["claim_token"], [], PREP_CONFIG_HASH
                )
                print(f"[QUEUE] task_id={task_id or '-'} event=task-moved sources=0 reason=scope", flush=True)
                return True
            trigger_id = f"webhook:{job['delivery_key']}"
            rearmed = await asyncio.to_thread(
                store.rearm_scope_run, task_id, task_id, trigger_id
            )
            print(
                f"[QUEUE] task_id={task_id} event=task-moved "
                f"state={'rearmed' if rearmed else 'already_rearmed'}",
                flush=True,
            )
            if rearmed:
                poll = move_poll or poll_yougile_chat_once
                await poll(store, fetch_chat_messages, task_id, task_id)
            await asyncio.to_thread(
                store.complete_webhook, job["id"], job["claim_token"], [], PREP_CONFIG_HASH
            )
            return True
        if event_name != "chat_message-created":
            await asyncio.to_thread(store.complete_webhook, job["id"], job["claim_token"], [], PREP_CONFIG_HASH)
            return True
        chat_id = job.get("chat_id") or ""
        message_id = job.get("message_id") or "-"
        if scope is not None:
            allowed_chats, _ = await scope()
            allowed = chat_id in allowed_chats
            if not allowed:
                allowed_chats, _ = await scope(force=True)
                allowed = chat_id in allowed_chats
        else:
            checker = task_allowed or webhook_task_in_scope
            allowed = await checker(chat_id)
        if not allowed:
            await asyncio.to_thread(
                store.complete_webhook, job["id"], job["claim_token"], [], PREP_CONFIG_HASH
            )
            print(
                f"[QUEUE] task_id=- message_id={message_id} sources=0 reason=scope",
                flush=True,
            )
            return True
        enriched = await enrich(payload)
        message_content = enriched.get("yougile_message") if isinstance(enriched, dict) else None
        message = message_content if isinstance(message_content, dict) else payload.get("payload", payload)
        task_id = chat_id
        if is_pipeline_notification_message(message):
            await asyncio.to_thread(
                store.complete_webhook, job["id"], job["claim_token"], [], PREP_CONFIG_HASH
            )
            return True
        moved_into_scope = move_into_configured_scope(payload, chat_id)
        if moved_into_scope:
            # YouGile emits an authoritative task-moved webhook and a second
            # system chat message for the same move. Processing both resets the
            # poller and makes the already accepted source look duplicated.
            # The task-moved branch above exclusively owns scope activation.
            await asyncio.to_thread(
                store.complete_webhook, job["id"], job["claim_token"], [], PREP_CONFIG_HASH
            )
            print(
                f"[QUEUE] task_id={chat_id} message_id={message_id} "
                "state=ignored_duplicate_move_event",
                flush=True,
            )
            return True
        if is_retry_limited_command(message):
            resumed = await asyncio.to_thread(store.resume_limited_recognitions, task_id, chat_id)
            await asyncio.to_thread(
                store.complete_webhook, job["id"], job["claim_token"], [], PREP_CONFIG_HASH
            )
            print(
                f"[ACR RETRY_LIMITED] task_id={task_id} message_id={message_id} resumed={len(resumed)}",
                flush=True,
            )
            return True
        forced_replay = is_forced_replay_message(message)
        prep_hash = (
            forced_run_prep_hash(PREP_CONFIG_HASH, task_id, chat_id, message_id)
            if forced_replay else PREP_CONFIG_HASH
        )
        sources = await source_collector(message, task_id, message_id)
        duplicates = [await asyncio.to_thread(
            store.task_has_linked_source, task_id, chat_id, [source]
        ) for source in sources]
        source_ids = await asyncio.to_thread(
            store.complete_webhook, job["id"], job["claim_token"], sources, prep_hash
        )
        for source_id, duplicate in zip(source_ids, duplicates):
            if forced_replay or not duplicate:
                await queue_chat_notification(
                    store,
                    chat_id,
                    "link_received",
                    f"link-received:{chat_id}:{message_id}:{source_id}",
                    "Ссылка получена",
                )
            else:
                await queue_chat_notification(
                    store,
                    chat_id,
                    "duplicate_link",
                    f"duplicate:{chat_id}:{message_id}:{source_id}",
                    "Повторная ссылка\nЭта ссылка уже была принята ранее. Новый скан не запускался.",
                )
        print(
            f"[QUEUE] task_id={task_id} message_id={message_id} "
            f"webhook={job['id']} sources={len(sources)}"
            f"{' forced_replay=1' if forced_replay else ''}",
            flush=True,
        )
    except YouGileRateLimited as error:
        await asyncio.to_thread(
            store.retry_webhook, job["id"], job["claim_token"], type(error).__name__
        )
        print(
            f"[ERROR] webhook={job['id']} stage=expand_rate_limit "
            f"type={type(error).__name__} retry_after={round(error.retry_after)}",
            flush=True,
        )
    except Exception as error:
        await asyncio.to_thread(store.fail_webhook, job["id"], job["claim_token"], type(error).__name__)
        print(
            f"[ERROR] webhook={job['id']} stage=expand type={type(error).__name__}"
            f"{value_error_log_suffix(error)}",
            flush=True,
        )
    return True


async def prepare_with_source_heartbeat(job: dict, store: PipelineStore, prepare):
    preparation = asyncio.create_task(prepare(job, store))
    try:
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(preparation), timeout=SOURCE_LEASE_HEARTBEAT_SECONDS
                )
            except TimeoutError:
                await asyncio.to_thread(
                    store.renew_source_claim,
                    job["id"],
                    job["claim_token"],
                    SOURCE_LEASE_SECONDS,
                )
    finally:
        if not preparation.done():
            preparation.cancel()
        await asyncio.gather(preparation, return_exceptions=True)


async def process_one_audio(store: PipelineStore = STORE, prepare=prepare_claimed_source) -> bool:
    job = await asyncio.to_thread(
        store.claim_source, socket.gethostname(), SOURCE_LEASE_SECONDS
    )
    if not job:
        return False
    context = await asyncio.to_thread(store.polled_yougile_context, job["id"])
    context_log = (
        f" task_id={context['task_id']} message_id={context['message_id']}" if context else ""
    )
    try:
        audio_path, info = await prepare_with_source_heartbeat(job, store, prepare)
        planned = store.source(job["id"])["wav_path"]
        if planned and str(audio_path) != planned:
            raise RuntimeError("PreparedWavPathMismatch")
        await asyncio.to_thread(store.complete_audio, job["id"], job["claim_token"], info)
        print(
            f"[AUDIO READY] source_job={job['id']}{context_log} sha256={info['sha256'][:12]}",
            flush=True,
        )
    except StoreError as error:
        print(
            f"[ERROR] source_job={job['id']}{context_log} stage=audio_claim "
            f"type={type(error).__name__}",
            flush=True,
        )
    except Exception as error:
        attempt = int(job.get("audio_attempts") or 1)
        delay = audio_retry_delay(error, attempt)
        if delay is not None:
            await asyncio.to_thread(
                store.retry_audio,
                job["id"],
                job["claim_token"],
                type(error).__name__,
                delay,
            )
            print(
                f"[RETRY] source_job={job['id']}{context_log} stage=audio "
                f"type={type(error).__name__} attempt={attempt} "
                f"next_in={round(delay)}",
                flush=True,
            )
        else:
            await asyncio.to_thread(
                store.fail_audio, job["id"], job["claim_token"], type(error).__name__
            )
            if isinstance(error, RuntimeError) and str(error) == "UntrackedExistingWav":
                for link in await asyncio.to_thread(store.chat_links_for_source, job["id"]):
                    await queue_chat_notification(
                        store,
                        link["chat_id"],
                        "audio_exists",
                        f"audio-exists:{link['chat_id']}:{job['id']}",
                        "Аудио-файл уже существует",
                    )
            print(
                f"[ERROR] source_job={job['id']}{context_log} stage=audio "
                f"type={type(error).__name__} attempts={attempt}"
                f"{value_error_log_suffix(error)}",
                flush=True,
            )
    return True


async def flush_one_chat_notification(
    store: PipelineStore = STORE,
    post_message=yougile_post_json,
    upload_report=upload_yougile_report,
) -> bool:
    """Deliver one durable notification without coupling it to pipeline success."""
    notification = await asyncio.to_thread(
        store.claim_chat_notification, socket.gethostname(), 300
    )
    if not notification:
        return False
    try:
        attachment_ref = notification.get("attachment_ref")
        attachment_path = notification.get("attachment_path")
        if attachment_path and not attachment_ref:
            attachment_ref = await upload_report(Path(attachment_path))
            await asyncio.to_thread(
                store.set_chat_notification_attachment,
                notification["id"], notification["claim_token"], attachment_ref,
            )
        text = notification["text"]
        if attachment_ref:
            text += f"\n/root/#file:{attachment_ref}"
        await post_message(
            f"/chats/{notification['chat_id']}/messages",
            {"text": text, "textHtml": text.replace("\n", "<br>")},
            request_kind="notification_message",
        )
        await asyncio.to_thread(
            store.complete_chat_notification, notification["id"], notification["claim_token"]
        )
        print(
            f"[NOTIFY] notification_id={notification['id']} kind={notification['kind']} stage=sent",
            flush=True,
        )
    except YouGileRateLimited as error:
        await asyncio.to_thread(
            store.retry_chat_notification, notification["id"], notification["claim_token"],
            type(error).__name__, error.retry_after,
        )
        print(
            f"[NOTIFY] notification_id={notification['id']} kind={notification['kind']} "
            f"stage=rate_limited retry_after={round(error.retry_after)}",
            flush=True,
        )
    except Exception as error:
        delay = min(3600.0, 30.0 * (2 ** min(int(notification["attempts"]), 6)))
        try:
            await asyncio.to_thread(
                store.retry_chat_notification, notification["id"], notification["claim_token"],
                type(error).__name__, delay,
            )
        except StoreError:
            pass
        print(
            f"[NOTIFY] notification_id={notification['id']} kind={notification['kind']} "
            f"stage=error type={type(error).__name__}",
            flush=True,
        )
    return True


def is_retry_limited_command(message: dict) -> bool:
    text = message.get("text")
    return isinstance(text, str) and " ".join(text.split()).upper() == "ACR RETRY_LIMITED"


def is_forced_replay_message(message: dict) -> bool:
    text = message.get("text")
    return isinstance(text, str) and any(part.casefold() == "--forced" for part in text.split())


def is_pipeline_notification_message(message: dict) -> bool:
    text = message.get("text")
    return isinstance(text, str) and text.startswith((
        "Сервер: Ссылка получена",
        "Сервер: Принято",  # Compatibility with previously sent notifications.
        "Сервер: Сканирование",
        "Сервер: Лимит ACRCloud",
        "Сервер: Готово",
        "Сервер: Повторная ссылка",
        "Сервер: Аудио-файл уже существует",
        "Сервер: Поиск CIS-Net начался",
        "Сервер: Результаты CIS-Net:",
    ))


async def queue_chat_notification(
    store: PipelineStore, chat_id: str, kind: str, dedupe_key: str, text: str
) -> bool:
    try:
        return await asyncio.to_thread(
            store.enqueue_chat_notification, chat_id, kind, dedupe_key, text
        )
    except Exception as error:
        print(
            f"[NOTIFY] kind={kind} stage=enqueue_error type={type(error).__name__}",
            flush=True,
        )
        return False


def terminal_message_reason(error: ValueError) -> str:
    text = str(error).casefold()
    if "not a supported" in text:
        return "unsupported_source"
    if "file" in text and ("invalid" in text or "missing" in text):
        return "invalid_file_reference"
    return "invalid_message"


async def fetch_suffix_once(
    fetch_messages,
    chat_id: str,
    limit: int,
    next_offset: int,
    known_ids: set[str],
) -> dict:
    probe_offset = max(0, next_offset - limit)
    calls = 0
    while True:
        page = await fetch_messages(chat_id, limit, probe_offset)
        calls += 1
        verify_oldest_first(page)
        known_positions = [
            index for index, message in enumerate(page)
            if message_id(message) in known_ids
        ]
        if known_positions:
            anchor_index = known_positions[-1]
            anchor_id = message_id(page[anchor_index])
            anchor_offset = probe_offset + anchor_index
            suffix = list(page[anchor_index + 1:])
            offset = probe_offset + len(page)
            last_page_size = len(page)
            break
        if probe_offset == 0:
            anchor_id = None
            anchor_offset = -1
            suffix = list(page)
            offset = len(page)
            last_page_size = len(page)
            break
        probe_offset = max(0, probe_offset - limit)
    while last_page_size == limit:
        page = await fetch_messages(chat_id, limit, offset)
        calls += 1
        verify_oldest_first(page)
        suffix.extend(page)
        offset += len(page)
        last_page_size = len(page)
    verify_oldest_first(suffix)
    return {
        "anchor_id": anchor_id,
        "anchor_offset": anchor_offset,
        "messages": suffix,
        "end_offset": offset,
        "calls": calls,
    }


async def fetch_stable_suffix(
    fetch_messages,
    chat_id: str,
    limit: int,
    next_offset: int,
    known_ids: set[str],
    max_passes: int = 5,
) -> tuple[dict, int]:
    previous = None
    total_calls = 0
    for pass_number in range(1, max_passes + 1):
        snapshot = await fetch_suffix_once(
            fetch_messages, chat_id, limit, next_offset, known_ids
        )
        total_calls += snapshot["calls"]
        signature = (
            snapshot["anchor_id"],
            snapshot["anchor_offset"],
            tuple(message_id(message) for message in snapshot["messages"]),
            snapshot["end_offset"],
        )
        if snapshot["calls"] == 1 or signature == previous:
            snapshot["calls"] = total_calls
            return snapshot, pass_number
        previous = signature
    raise RuntimeError("YouGile messages changed during pagination; poll deferred")


async def poll_latest_source_history(
    store: PipelineStore,
    fetch_messages,
    task_id: str,
    chat_id: str,
    limit: int,
    source_collector,
    run_prep_hash: str,
) -> PollResult:
    """On scope entry, queue at most the newest supported chat-history source."""
    snapshot, passes = await fetch_stable_suffix(fetch_messages, chat_id, limit, 0, set())
    messages = snapshot["messages"]
    queued = 0
    selected_id = None
    for message in reversed(messages):
        current_id = message_id(message)
        if not current_id:
            continue
        sources = await source_collector(message, task_id, current_id)
        if not sources:
            continue
        selected_id = current_id
        forced_replay = is_forced_replay_message(message)
        message_prep_hash = (
            forced_run_prep_hash(run_prep_hash, task_id, chat_id, current_id)
            if forced_replay else run_prep_hash
        )
        already_linked = False if forced_replay else await asyncio.to_thread(
            store.task_has_linked_source, task_id, chat_id, sources
        )
        if already_linked:
            await asyncio.to_thread(
                store.mark_polled_message, task_id, chat_id, current_id,
                "deduplicated", "PreviousSource", "latest_source_previously_linked",
            )
            await queue_chat_notification(
                store,
                chat_id,
                "duplicate_link",
                f"duplicate:{chat_id}:{current_id}:latest",
                "Повторная ссылка",
            )
        else:
            results = await asyncio.to_thread(
                store.enqueue_polled_sources,
                task_id, chat_id, current_id, sources, message_prep_hash,
            )
            queued = sum(1 for _, inserted in results if inserted)
            for source, (source_id, inserted) in zip(sources, results):
                if not inserted:
                    continue
                await queue_chat_notification(
                    store,
                    chat_id,
                    "link_received",
                    f"link-received:{chat_id}:{current_id}:{source_id}",
                    "Ссылка получена",
                )
                if source["kind"] == "yougile_file":
                    print(
                        f"[YOUGILE FILE] task_id={task_id} message_id={current_id} "
                        f"file_id={source['file_id']} stage=discovered",
                        flush=True,
                    )
                print(
                    f"[QUEUE] task_id={task_id} message_id={current_id} "
                    f"source_job={source_id} kind={source['kind']}",
                    flush=True,
                )
        break
    last_message_id = message_id(messages[-1]) if messages else None
    await asyncio.to_thread(
        store.set_poller_chat_position, task_id, chat_id, snapshot["end_offset"], last_message_id
    )
    print(
        f"[POLLER] task_id={task_id} chat_id={chat_id} pages={snapshot['calls']} "
        f"passes={passes} messages_read={len(messages)} selected_message={selected_id or '-'} "
        f"queued={queued} next_offset={snapshot['end_offset']}",
        flush=True,
    )
    return PollResult(
        queued, messages_read=len(messages), messages_skipped=0,
        messages_new=1 if selected_id else 0,
    )


async def poll_yougile_chat_once(
    store: PipelineStore = STORE,
    fetch_messages=fetch_chat_messages,
    task_id: str = YOUGILE_POLL_TASK_ID,
    chat_id: str = YOUGILE_POLL_CHAT_ID,
    limit: int = YOUGILE_POLL_INITIAL_LIMIT,
    source_collector=collect_message_sources,
) -> int:
    if not task_id or not chat_id:
        raise RuntimeError("YouGile poll target is not configured")
    run_prep_hash = await asyncio.to_thread(
        store.scope_run_prep_hash, task_id, chat_id, PREP_CONFIG_HASH
    )
    baseline = await asyncio.to_thread(store.require_baseline, task_id, chat_id)
    if baseline["status"] == "required":
        baseline = await asyncio.to_thread(store.activate_chat_from_start, task_id, chat_id)
        if baseline.get("activated"):
            print(
                f"[POLLER] task_id={task_id} chat_id={chat_id} "
                "state=activated_from_start",
                flush=True,
            )
    if baseline["status"] != "complete":
        print(
            f"[POLLER] task_id={task_id} chat_id={chat_id} "
            f"state=baseline_{baseline['status']} queued=0",
            flush=True,
        )
        return PollResult(
            0, messages_read=0, messages_skipped=0, messages_new=0,
            baseline_required=True,
        )
    if baseline.get("activated"):
        return await poll_latest_source_history(
            store, fetch_messages, task_id, chat_id, limit, source_collector, run_prep_hash
        )
    position = await asyncio.to_thread(store.poller_chat_position, task_id, chat_id)
    records = await asyncio.to_thread(store.polled_message_records, task_id, chat_id)
    checked = set(records)
    snapshot, passes = await fetch_stable_suffix(
        fetch_messages, chat_id, limit, int(position["next_offset"]), checked
    )
    messages = snapshot["messages"]
    offset = snapshot["anchor_offset"] + 1
    last_message_id = snapshot["anchor_id"]
    queued = 0
    pages = snapshot["calls"]
    messages_read = 0
    messages_skipped = 0
    messages_new = 0
    for message in messages:
        messages_read += 1
        current_id = message_id(message)
        if current_id in checked:
            messages_skipped += 1
            print(
                f"[MESSAGE] task_id={task_id} chat_id={chat_id} message_id={current_id} "
                f"stage=already_processed status={records[current_id]['status']}",
                flush=True,
            )
            offset += 1
            last_message_id = current_id
            continue
        messages_new += 1
        try:
            if is_pipeline_notification_message(message):
                await asyncio.to_thread(
                    store.mark_polled_message, task_id, chat_id, current_id,
                    "terminal_skipped", None, "pipeline_notification",
                )
                checked.add(current_id)
                offset += 1
                last_message_id = current_id
                continue
            if is_retry_limited_command(message):
                resumed = await asyncio.to_thread(
                    store.resume_limited_recognitions, task_id, chat_id
                )
                await asyncio.to_thread(
                    store.mark_polled_message, task_id, chat_id, current_id,
                    "processed", None, "acr_retry_limited",
                )
                print(
                    f"[ACR RETRY_LIMITED] task_id={task_id} message_id={current_id} "
                    f"resumed={len(resumed)}",
                    flush=True,
                )
                checked.add(current_id)
                offset += 1
                last_message_id = current_id
                continue
            sources = await source_collector(message, task_id, current_id)
            if not sources:
                await asyncio.to_thread(
                    store.mark_polled_message, task_id, chat_id, current_id,
                    "terminal_skipped", "NoSupportedSource", "no_supported_source",
                )
                print(
                    f"[MESSAGE] task_id={task_id} chat_id={chat_id} message_id={current_id} "
                    "stage=terminal_skipped reason=no_supported_source",
                    flush=True,
                )
            else:
                forced_replay = is_forced_replay_message(message)
                message_prep_hash = (
                    forced_run_prep_hash(run_prep_hash, task_id, chat_id, current_id)
                    if forced_replay else run_prep_hash
                )
                results = await asyncio.to_thread(
                    store.enqueue_polled_sources,
                    task_id, chat_id, current_id, sources, message_prep_hash,
                )
                created = 0
                for source, (source_id, inserted) in zip(sources, results):
                    if not inserted:
                        await queue_chat_notification(
                            store,
                            chat_id,
                            "duplicate_link",
                            f"duplicate:{chat_id}:{current_id}:{source_id}",
                            "Повторная ссылка\nЭта ссылка уже была принята ранее. Новый скан не запускался.",
                        )
                        continue
                    created += 1
                    queued += 1
                    await queue_chat_notification(
                        store,
                        chat_id,
                        "link_received",
                        f"link-received:{chat_id}:{current_id}:{source_id}",
                        "Ссылка получена",
                    )
                    if source["kind"] == "yougile_file":
                        print(
                            f"[YOUGILE FILE] task_id={task_id} message_id={current_id} "
                            f"file_id={source['file_id']} stage=discovered",
                            flush=True,
                        )
                    print(
                        f"[QUEUE] task_id={task_id} message_id={current_id} "
                        f"source_job={source_id} kind={source['kind']}",
                        flush=True,
                    )
                print(
                    f"[MESSAGE] task_id={task_id} chat_id={chat_id} message_id={current_id} "
                    f"stage=processed status={'queued' if created else 'deduplicated'}",
                    flush=True,
                )
            checked.add(current_id)
        except ValueError as error:
            reason = terminal_message_reason(error)
            await asyncio.to_thread(
                store.mark_polled_message, task_id, chat_id, current_id,
                "terminal_skipped", "ValueError", reason,
            )
            print(
                f"[MESSAGE] task_id={task_id} chat_id={chat_id} message_id={current_id} "
                f"stage=terminal_skipped reason={reason}",
                flush=True,
            )
            checked.add(current_id)
        except Exception as error:
            print(
                f"[ERROR] task_id={task_id} message_id={current_id} "
                f"stage=poller_message type={type(error).__name__}",
                flush=True,
            )
            await asyncio.to_thread(
                store.set_poller_chat_position, task_id, chat_id, offset, last_message_id
            )
            print(
                f"[POLLER] task_id={task_id} chat_id={chat_id} pages={pages} "
                f"passes={passes} messages_read={messages_read} "
                f"messages_skipped={messages_skipped} messages_new={messages_new} "
                f"queued={queued} stopped=transient_message_error",
                flush=True,
            )
            return PollResult(
                queued, messages_read=messages_read,
                messages_skipped=messages_skipped, messages_new=messages_new,
            )
        offset += 1
        last_message_id = current_id
    await asyncio.to_thread(
        store.set_poller_chat_position, task_id, chat_id, offset, last_message_id
    )
    print(
        f"[POLLER] task_id={task_id} chat_id={chat_id} pages={pages} passes={passes} "
        f"messages_read={messages_read} messages_skipped={messages_skipped} "
        f"messages_new={messages_new} queued={queued} next_offset={offset}",
        flush=True,
    )
    return PollResult(
        queued,
        messages_read=messages_read,
        messages_skipped=messages_skipped,
        messages_new=messages_new,
    )


async def backfill_yougile_wav_history(
    store: PipelineStore,
    task_id: str,
    chat_id: str,
    page_size: int = 20,
    fetch_messages=fetch_chat_messages,
) -> dict:
    if not task_id or not chat_id or page_size < 1:
        raise ValueError("Invalid WAV backfill parameters")
    with store.connect() as db:
        known_source_ids = {int(row[0]) for row in db.execute("SELECT id FROM source_jobs")}
    found_refs = set()
    created_ids = []
    offset = 0
    pages = 0
    last_message_id = None
    while True:
        messages = await fetch_messages(chat_id, page_size, offset)
        pages += 1
        if not messages:
            break
        for message in messages:
            message_id_value = message.get("id")
            if not isinstance(message_id_value, (str, int)):
                continue
            message_id = str(message_id_value)
            try:
                refs = extract_yougile_wav_refs(message)
                sources = [{
                    "kind": "yougile_file",
                    "url": ref["file_path"],
                    "filename": ref["filename"],
                    "file_id": ref["file_id"],
                } for ref in refs]
                for ref in refs:
                    found_refs.add((message_id, ref["file_id"], ref["file_path"]))
                results = await asyncio.to_thread(
                    store.enqueue_polled_sources,
                    task_id, chat_id, message_id, sources, PREP_CONFIG_HASH,
                ) if sources else []
                for source_id, _ in results:
                    if source_id not in known_source_ids:
                        known_source_ids.add(source_id)
                        created_ids.append(source_id)
                await asyncio.to_thread(
                    store.mark_polled_message, task_id, chat_id, message_id
                )
            except Exception as error:
                print(
                    f"[ERROR] task_id={task_id} message_id={message_id} "
                    f"stage=wav_backfill_message type={type(error).__name__}"
                    f"{value_error_log_suffix(error)}",
                    flush=True,
                )
            last_message_id = message_id
        offset += len(messages)
        await asyncio.to_thread(
            store.set_poller_chat_position, task_id, chat_id, offset, last_message_id
        )
        if len(messages) < page_size:
            break
    result = {
        "pages": pages,
        "messages": offset,
        "wav_found": len(found_refs),
        "source_jobs_created": len(created_ids),
        "source_job_ids": created_ids,
        "next_offset": offset,
    }
    print(
        "[WAV BACKFILL] " + json.dumps(result, ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    return result


async def poll_yougile_cycle(
    store: PipelineStore = STORE,
    scope=allowed_scope,
    poll_chat=poll_yougile_chat_once,
) -> int:
    chat_ids, task_ids = await scope()
    selected = await asyncio.to_thread(
        store.round_robin_chats, list(chat_ids), YOUGILE_POLL_MAX_CHATS_PER_CYCLE
    )
    queued = 0
    messages_read = 0
    messages_skipped = 0
    messages_new = 0
    for chat_id in selected:
        task_id = chat_id if chat_id in task_ids else chat_id
        try:
            result = await poll_chat(store, fetch_chat_messages, task_id, chat_id)
            queued += int(result)
            messages_read += int(getattr(result, "messages_read", 0))
            messages_skipped += int(getattr(result, "messages_skipped", 0))
            messages_new += int(getattr(result, "messages_new", 0))
        except YouGileRateLimited as error:
            print(
                f"[ERROR] task_id={task_id} message_id=- stage=poller_rate_limit "
                f"type={type(error).__name__} retry_after={round(error.retry_after)}",
                flush=True,
            )
            break
        except Exception as error:
            print(
                f"[ERROR] task_id={task_id} message_id=- "
                f"stage=poller_chat type={type(error).__name__}",
                flush=True,
            )
    stats = await asyncio.to_thread(store.yougile_request_stats, 60)
    print(
        f"[POLLER] chats_total={len(chat_ids)} chats_polled={len(selected)} "
        f"messages_read={messages_read} messages_skipped={messages_skipped} "
        f"messages_new={messages_new} queued={queued} "
        f"requests_60s={stats['total']} limit={YOUGILE_MAX_REQUESTS_PER_MINUTE}",
        flush=True,
    )
    return queued


async def persistent_yougile_poller(store: PipelineStore = STORE):
    print(
        f"[POLLER] interval={YOUGILE_POLL_INTERVAL_SECONDS} "
        f"initial_limit={YOUGILE_POLL_INITIAL_LIMIT} "
        f"max_requests={YOUGILE_MAX_REQUESTS_PER_MINUTE} stage=started",
        flush=True,
    )
    await asyncio.sleep(YOUGILE_POLL_INTERVAL_SECONDS)
    while True:
        try:
            await poll_yougile_cycle(store)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(
                f"[ERROR] task_id=- message_id=- stage=poller "
                f"type={type(error).__name__}{http_status_log_suffix(error)}",
                flush=True,
            )
        await asyncio.sleep(YOUGILE_POLL_INTERVAL_SECONDS)


async def persistent_worker(store: PipelineStore = STORE):
    while True:
        worked = await flush_one_chat_notification(store)
        worked = await process_one_webhook(store) or worked
        worked = await process_one_audio(store) or worked
        if not worked:
            await asyncio.sleep(1.0)


def write_latest_event(payload: dict) -> None:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    target = EVENTS_DIR / "latest.json"
    temporary = EVENTS_DIR / "latest.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


@app.on_event("startup")
async def startup():
    validate_webhook_secret(WEBHOOK_SECRET)
    for directory in (INCOMING_DIR, AUDIO_DIR, EVENTS_DIR, QUEUE_DIR):
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    await asyncio.to_thread(STORE.initialize)
    await asyncio.to_thread(STORE.recover_receiver_claims)
    asyncio.create_task(persistent_worker(STORE))
    if YOUGILE_POLL_ENABLED and any((
        YOUGILE_ALLOWED_BOARD_IDS,
        YOUGILE_ALLOWED_COLUMN_IDS,
        YOUGILE_ALLOWED_TASK_IDS,
        YOUGILE_ALLOWED_CHAT_IDS,
    )):
        asyncio.create_task(persistent_yougile_poller(STORE))


@app.get("/health")
async def health():
    counts = await asyncio.to_thread(STORE.counts)
    task_statuses = await asyncio.to_thread(STORE.task_status_counts)
    requests = await asyncio.to_thread(STORE.yougile_request_stats, 60)
    return {
        "status": "ok",
        "queued": sum(counts.values()),
        **counts,
        "yougile_requests_60s": requests["total"],
        "yougile_request_limit_60s": YOUGILE_MAX_REQUESTS_PER_MINUTE,
        "yougile_rate_limited": requests["blocked_seconds"] > 0,
        "yougile_polling_enabled": YOUGILE_POLL_ENABLED,
        "tasks_by_status": task_statuses,
    }


@app.post("/webhooks/yougile/{secret}")
async def yougile_webhook(secret: str, request: Request, content_type: str = Header(default="")):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="Not found")
    if "application/json" not in content_type.lower():
        raise HTTPException(status_code=415, detail="JSON required")
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        raise HTTPException(status_code=413, detail="Webhook too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="Invalid JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    event_id, inserted = await asyncio.to_thread(
        STORE.record_webhook, payload, str(payload.get("event") or ""),
        event_company_id(payload), event_chat_id(payload), event_message_id(payload),
    )
    try:
        await asyncio.to_thread(write_latest_event, payload)
    except Exception as error:
        print(f"[WARN] webhook={event_id} event_copy={type(error).__name__}", flush=True)
    return {"accepted": True, "duplicate": not inserted, "event_id": event_id}
