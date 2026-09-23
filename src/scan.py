#!/usr/bin/env python3
"""Local-first ACRCloud fingerprint scanner for pyacrcloud==1.0.12.

Default mode and --check-all are local-only; HTTP requests require --execute.
Fingerprints use 12-second windows with a 10-second step. SQLite stores
resumable progress and exact responses; completed windows are not sent again.
Retrying a failed or uncertain attempt requires --retry-stopped and may consume
another request. There are no automatic retries or redirects; TLS verification
remains enabled.

Empty fingerprints and tails shorter than one second are recorded locally,
never submitted, and are not API no-match results. Credentials are entered
without echo and are not saved. Results can contain private recording metadata;
returned candidates are not verified songs.
"""

import argparse
import base64
import contextlib
import fcntl
import functools
import getpass
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import ssl
import sys
import time
import uuid
import warnings
import wave

WINDOW_SECONDS = 12
STEP_SECONDS = 10
ENDPOINT = "/v1/identify"
DEFAULT_HOST = "identify-eu-west-1.acrcloud.com"
MAX_RESPONSE = 8 * 1024 * 1024
SDK_VERSION = "1.0.12"
MIN_LOCAL_SECONDS = 1  # Conservative local policy, NOT a claimed SDK/API minimum.
FINGERPRINT_OPTIONS = {"filter_energy_min": 0, "silence_energy_threshold": 100,
                       "silence_rate_threshold": 1}
RETRYABLE_ACR_CODES = {3003}


class ScanStop(Exception):
    pass


def log(message):
    print(message, flush=True)


def inspect_audio(source):
    """Hash the same open file descriptor that will subsequently supply samples."""
    before = os.fstat(source.fileno())
    digest = hashlib.sha256()
    source.seek(0)
    for block in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(block)
    after = os.fstat(source.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ScanStop("Audio changed during preflight; wait for extraction to finish.")
    source.seek(0)
    with wave.open(source, "rb") as reader:
        rate, frames = reader.getframerate(), reader.getnframes()
        if (reader.getnchannels(), reader.getsampwidth(), reader.getcomptype()) != (1, 2, "NONE"):
            raise ScanStop("Expected uncompressed mono, 16-bit PCM WAV.")
        if not 8000 <= rate <= 96000 or frames < 1:
            raise ScanStop("Unsupported sample rate or empty WAV.")
        reader.setpos(frames - 1)
        if len(reader.readframes(1)) != 2:
            raise ScanStop("WAV is truncated: its last declared sample is missing.")
    return {
        "sha256": digest.hexdigest(), "size_bytes": before.st_size,
        "sample_rate": rate, "frames": frames,
        "duration_seconds": frames / rate,
    }


def scanner_config(audio_info, host, sdk_info=None):
    """Build the same persisted configuration for manual and worker runs."""
    return {
        "version": 2,
        "audio": audio_info,
        "host": host,
        "window_seconds": WINDOW_SECONDS,
        "step_seconds": STEP_SECONDS,
        "data_type": "fingerprint",
        "sdk": sdk_info if sdk_info is not None else load_sdk()[1],
        "min_local_seconds": MIN_LOCAL_SECONDS,
    }


def windows(frames, rate):
    for index, start in enumerate(range(0, frames, STEP_SECONDS * rate)):
        yield index, start, min(WINDOW_SECONDS * rate, frames - start)


def wav_sample(reader, start, frames):
    reader.setpos(start)
    pcm = reader.readframes(frames)
    if len(pcm) != frames * 2:
        raise ScanStop("WAV changed or is truncated; request was not sent.")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(reader.getframerate())
        writer.writeframes(pcm)
    sample = buffer.getvalue()
    if len(sample) >= 5_000_000:
        raise ScanStop("Sample exceeds the Identification API size limit.")
    return sample


@functools.lru_cache(maxsize=1)
def load_sdk():
    try:
        from importlib.metadata import version
        from acrcloud import acrcloud_extr_tool as sdk
        installed = version("pyacrcloud")
        if installed != SDK_VERSION:
            raise ScanStop(f"Expected pyacrcloud=={SDK_VERSION}; use venvs/acr-sdk.")
        native = sdk.version()
        binary_sha = hashlib.sha256(Path(sdk.__file__).read_bytes()).hexdigest()
    except ScanStop:
        raise
    except Exception as exc:
        raise ScanStop(f"Cannot load pyacrcloud SDK ({type(exc).__name__}); use venvs/acr-sdk.") from None
    return sdk, {"package": "pyacrcloud", "version": installed,
                 "native_version": native, "binary_sha256": binary_sha,
                 "function": "create_fingerprint_by_filebuffer", "options": dict(FINGERPRINT_OPTIONS)}


def fingerprint_sample(sample):
    sdk, _ = load_sdk()
    try:
        # Only this already-extracted WAV window is given to the SDK. Even for
        # a partial last window it cannot decode data beyond the actual EOF.
        fp = sdk.create_fingerprint_by_filebuffer(sample, 0, WINDOW_SECONDS, False,
                                                 dict(FINGERPRINT_OPTIONS))
    except Exception as exc:
        raise ScanStop(f"SDK fingerprint creation failed ({type(exc).__name__}); no request sent.") from None
    if fp is None:
        raise ScanStop("SDK could not decode the window; no request sent. Review source audio.")
    if not isinstance(fp, bytes) or len(fp) >= 5_000_000:
        raise ScanStop("SDK returned invalid/oversized fingerprint; no request sent.")
    return fp


def prepare_window(reader, start, count, fingerprinter=fingerprint_sample):
    sample = wav_sample(reader, start, count)
    if count < MIN_LOCAL_SECONDS * reader.getframerate():
        return sample, b"", "tail_under_1_second"
    fp = fingerprinter(sample)
    if fp is None or not isinstance(fp, bytes) or len(fp) >= 5_000_000:
        raise ScanStop("Invalid fingerprint result; no request sent.")
    return sample, fp, "empty_sdk_fingerprint" if not fp else None


def request_body(sample, key, secret, timestamp=None):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    signing = "\n".join(("POST", ENDPOINT, key, "fingerprint", "1", timestamp))
    signature = base64.b64encode(hmac.new(
        secret.encode("ascii"), signing.encode("ascii"), hashlib.sha1
    ).digest()).decode("ascii")
    fields = {
        "access_key": key, "sample_bytes": str(len(sample)),
        "timestamp": timestamp, "signature": signature,
        "data_type": "fingerprint", "signature_version": "1",
    }
    boundary = "acrscan" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; '
                      f'name="{name}"\r\n\r\n{value}\r\n').encode("ascii"))
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; '
                  'name="sample"; filename="sample.fp"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n').encode("ascii"))
    parts.extend((sample, f"\r\n--{boundary}--\r\n".encode("ascii")))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def send_sample(host, sample, key, secret):
    body, content_type = request_body(sample, key, secret)
    conn = http.client.HTTPSConnection(host, timeout=60, context=ssl.create_default_context())
    try:
        conn.request("POST", ENDPOINT, body=body, headers={
            "Content-Type": content_type, "Content-Length": str(len(body)),
            "Accept": "application/json", "User-Agent": "yougile-acr-sdk/2.0",
        })
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ScanStop("Unexpectedly large API response; request outcome is uncertain.")
        return response.status, raw
    finally:
        conn.close()


def classify(http_status, raw):
    try:
        payload = json.loads(raw)
        status = payload.get("status", {}) if isinstance(payload, dict) else {}
        code = status.get("code") if isinstance(status, dict) else None
    except (ValueError, UnicodeError):
        code = None
    if type(code) is not int:
        code = None
    accepted = http_status == 200 and code in (0, 1001)
    return accepted, code


def retryable_response(http_status, acr_code):
    return http_status == 429 or 500 <= http_status <= 599 or acr_code in RETRYABLE_ACR_CODES


def music_candidates(raw):
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, UnicodeError):
        return []
    metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    music = metadata.get("music", []) if isinstance(metadata, dict) else []
    return music if isinstance(music, list) else []


def normalized_candidate(candidate):
    if not isinstance(candidate, dict):
        candidate = {}
    artists = candidate.get("artists", [])
    artist_names = [
        artist.get("name") for artist in artists
        if isinstance(artist, dict) and isinstance(artist.get("name"), str)
    ] if isinstance(artists, list) else []
    album = candidate.get("album")
    album_name = album.get("name") if isinstance(album, dict) else album
    external_ids = candidate.get("external_ids")
    return {
        "title": candidate.get("title"),
        "artists": artist_names,
        "album": album_name,
        "label": candidate.get("label"),
        "isrc": (
            candidate.get("isrc") or
            (external_ids.get("isrc") if isinstance(external_ids, dict) else None)
        ),
        "score": candidate.get("score"),
        "external_ids": external_ids if isinstance(external_ids, dict) else {},
        "external_metadata": (
            candidate.get("external_metadata")
            if isinstance(candidate.get("external_metadata"), dict) else {}
        ),
    }


def initialize_db(db, config):
    # Check BEFORE any DDL, so incompatible databases are not migrated silently.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
        old = db.execute("SELECT config FROM metadata WHERE id=1").fetchone()
        if not old or json.loads(old[0]) != config:
            raise ScanStop("This database belongs to different audio/settings. Use a NEW SDK output directory.")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_index INTEGER NOT NULL, start_seconds REAL NOT NULL,
            duration_seconds REAL NOT NULL, started_utc TEXT NOT NULL,
            state TEXT NOT NULL, http_status INTEGER, acr_code INTEGER,
            response_raw BLOB, error_type TEXT,
            fingerprint_bytes INTEGER NOT NULL, fingerprint_sha256 TEXT NOT NULL,
            retry_number INTEGER NOT NULL DEFAULT 0,
            retryable INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS local_windows (
            window_index INTEGER PRIMARY KEY, start_seconds REAL NOT NULL,
            duration_seconds REAL NOT NULL, reason TEXT NOT NULL,
            sample_sha256 TEXT NOT NULL, created_utc TEXT NOT NULL
        );
    """)
    attempt_columns = {row[1] for row in db.execute("PRAGMA table_info(attempts)")}
    if "retry_number" not in attempt_columns:
        db.execute("ALTER TABLE attempts ADD COLUMN retry_number INTEGER NOT NULL DEFAULT 0")
    if "retryable" not in attempt_columns:
        db.execute("ALTER TABLE attempts ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0")
    row = db.execute("SELECT config FROM metadata WHERE id=1").fetchone()
    if row and json.loads(row[0]) != config:
        raise ScanStop("This output directory belongs to different audio/settings. Do not overwrite it.")
    if not row:
        db.execute("INSERT INTO metadata VALUES (1, ?)", (json.dumps(config, sort_keys=True),))
        db.commit()


def check_output(output, config):
    if not output.exists():
        return
    if not output.is_dir():
        raise ScanStop("Output is not a directory.")
    dbpath = output / "scan.sqlite3"
    if not dbpath.exists():
        if any(output.iterdir()):
            raise ScanStop("Nonempty output without a scanner database. Use a NEW SDK output directory.")
        return
    with contextlib.closing(sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT config FROM metadata WHERE id=1").fetchone()
        if not row or json.loads(row[0]) != config:
            raise ScanStop("Output belongs to different audio/settings. Old audio scans are NOT reused or changed.")


def atomic_text(path, lines):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def export_results(db, output, total):
    rows = db.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    responses, matches = [], []
    done, matched, no_match = set(), set(), set()
    for row in rows:
        record = dict(row)
        raw = record.pop("response_raw")
        record["response_text"] = raw.decode("utf-8", errors="replace") if raw is not None else None
        # Exact response bytes remain in SQLite; JSONL is a convenient export.
        responses.append(json.dumps(record, ensure_ascii=False) + "\n")
        if record["state"] != "done":
            continue
        index = record["window_index"]
        done.add(index)
        if record["acr_code"] == 1001:
            no_match.add(index)
            continue
        music = music_candidates(raw)
        if music:
            matched.add(index)
        for rank, candidate in enumerate(music, start=1):
            matches.append(json.dumps({
                "window_index": index, "start_seconds": record["start_seconds"],
                "duration_seconds": record["duration_seconds"], "candidate_rank": rank,
                "verification": "not_reviewed", **normalized_candidate(candidate),
                "music": candidate,
            }, ensure_ascii=False) + "\n")
    local = [dict(r) for r in db.execute("SELECT * FROM local_windows ORDER BY window_index")]
    local_indices = {r["window_index"] for r in local}
    processed = done | local_indices
    summary = {
        "schema_version": 2, "data_type": "fingerprint",
        "total_windows": total, "completed_windows": len(done),
        "processed_windows": len(processed), "locally_skipped_windows": len(local),
        "remaining_windows": total - len(processed), "attempts": len(rows),
        "windows_with_music_candidates": len(matched),
        "no_match_windows": len(no_match), "candidate_rows": len(matches),
        "complete": len(processed) == total,
        "all_windows_submitted": len(done) == total,
        "note": "complete means the queue was processed, including locally skipped windows. "
                "Empty fingerprints/short tails are NOT API no-match results. "
                "Candidates are not verified songs. Window bounds are not precise song boundaries.",
    }
    atomic_text(output / "responses.jsonl", responses)
    atomic_text(output / "matches.jsonl", matches)
    atomic_text(output / "local_windows.jsonl", [json.dumps(r, ensure_ascii=False) + "\n" for r in local])
    atomic_text(output / "summary.json", [json.dumps(summary, indent=2, ensure_ascii=False) + "\n"])
    return summary


def scan(db, reader, config, key, secret, max_requests, retry_stopped,
         transport=send_sample, clock=time.monotonic, sleeper=time.sleep,
         fingerprinter=fingerprint_sample, max_retries=0,
         retry_base_seconds=2.0, retry_max_seconds=30.0, log_context=""):
    if max_retries < 0 or retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
        raise ValueError("Invalid retry policy")
    rate = config["audio"]["sample_rate"]
    schedule = list(windows(config["audio"]["frames"], rate))
    completed = {r[0] for r in db.execute("SELECT window_index FROM attempts WHERE state='done'")}
    completed.update(r[0] for r in db.execute("SELECT window_index FROM local_windows"))
    last_start = None
    for index, start, count in schedule:
        if index in completed:
            continue
        previous_rows = db.execute(
            "SELECT state,retryable FROM attempts WHERE window_index=? ORDER BY id", (index,)
        ).fetchall()
        if previous_rows and not retry_stopped:
            last = previous_rows[-1]
            can_resume = last["retryable"] or last["state"] in {"sending", "uncertain"}
            if not can_resume or len(previous_rows) >= 1 + max_retries:
                raise ScanStop(
                    f"Window {index + 1} has a stopped or exhausted attempt. "
                    "Inspect responses.jsonl before any manual retry."
                )
        sample, fp, local_reason = prepare_window(reader, start, count, fingerprinter)
        if local_reason:
            db.execute("INSERT INTO local_windows VALUES (?,?,?,?,?,?)",
                       (index, start / rate, count / rate, local_reason,
                        hashlib.sha256(sample).hexdigest(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            db.commit()
            completed.add(index)
            log(f"[LOCAL SKIP] {index + 1}/{len(schedule)}: {local_reason}; NOT an API no-match.")
            continue
        log(
            f"[FINGERPRINT]{log_context} window={index + 1}/{len(schedule)} "
            f"offset={start / rate:.2f}s duration={count / rate:.2f}s bytes={len(fp)}"
        )
        while True:
            previous_count = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE window_index=?", (index,)
            ).fetchone()[0]
            used = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            if max_requests is not None and used >= max_requests:
                raise ScanStop(f"Total request budget reached ({max_requests}); progress retained.")
            if previous_count >= 1 + max_retries and not retry_stopped:
                raise ScanStop(f"Window {index + 1} exhausted its retry limit.")
            if last_start is not None:
                sleeper(max(0.0, 1.05 - (clock() - last_start)))
            cursor = db.execute(
                "INSERT INTO attempts "
                "(window_index,start_seconds,duration_seconds,started_utc,state,"
                "fingerprint_bytes,fingerprint_sha256,retry_number,retryable) "
                "VALUES (?,?,?,?, 'sending',?,?,?,0)",
                (index, start / rate, count / rate,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 len(fp), hashlib.sha256(fp).hexdigest(), previous_count),
            )
            attempt_id = cursor.lastrowid
            db.commit()  # Durable BEFORE networking: a crash must remain visible.
            last_start = clock()
            log(
                f"[ACR REQUEST]{log_context} window={index + 1}/{len(schedule)} "
                f"attempt={previous_count + 1}/{1 + max_retries}"
            )
            try:
                http_status, raw = transport(config["host"], fp, key, secret)
            except BaseException as exc:
                db.execute(
                    "UPDATE attempts SET state='uncertain',error_type=?,retryable=1 WHERE id=?",
                    (type(exc).__name__, attempt_id),
                )
                db.commit()
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                can_retry = previous_count < max_retries
                if can_retry:
                    delay = min(retry_max_seconds, retry_base_seconds * (2 ** previous_count))
                    log(
                        f"[ACR ERROR]{log_context} window={index + 1}/{len(schedule)} "
                        f"type={type(exc).__name__} retry=1 delay={delay:.1f}s"
                    )
                    sleeper(delay)
                    continue
                log(
                    f"[ACR ERROR]{log_context} window={index + 1}/{len(schedule)} "
                    f"type={type(exc).__name__} retry=0 exhausted=1"
                )
                raise ScanStop(f"Request retries exhausted ({type(exc).__name__}).") from None
            accepted, code = classify(http_status, raw)
            retryable = retryable_response(http_status, code)
            db.execute(
                "UPDATE attempts SET state=?,http_status=?,acr_code=?,response_raw=?,retryable=? "
                "WHERE id=?",
                ("done" if accepted else "error", http_status, code, raw,
                 int(retryable), attempt_id),
            )
            db.commit()
            if accepted:
                completed.add(index)
                if code == 1001:
                    log(
                        f"[ACR NO MATCH]{log_context} window={index + 1}/{len(schedule)} "
                        f"http={http_status} code={code}"
                    )
                else:
                    log(
                        f"[ACR MATCH]{log_context} window={index + 1}/{len(schedule)} "
                        f"http={http_status} code={code} candidates={len(music_candidates(raw))}"
                    )
                break
            if retryable and previous_count < max_retries:
                delay = min(retry_max_seconds, retry_base_seconds * (2 ** previous_count))
                log(
                    f"[ACR ERROR]{log_context} window={index + 1}/{len(schedule)} "
                    f"http={http_status} code={code} retry=1 delay={delay:.1f}s"
                )
                sleeper(delay)
                continue
            log(
                f"[ACR ERROR]{log_context} window={index + 1}/{len(schedule)} "
                f"http={http_status} code={code} retry=0"
            )
            raise ScanStop(f"API stopped: HTTP {http_status}, ACRCloud code {code}. Response saved.")
    return len(completed)


def validate_credential(value):
    value = value.strip()
    if not value or not value.isascii() or any(c.isspace() for c in value):
        raise ScanStop("Credential must be nonempty ASCII without whitespace.")
    return value


def credentials_from_environment():
    """Return configured ACR credentials, or None to use the interactive prompt."""
    key = os.getenv("ACR_ACCESS_KEY")
    secret = os.getenv("ACR_SECRET_KEY")
    if key is None and secret is None:
        return None
    if key is None or secret is None:
        raise ScanStop("Both ACR_ACCESS_KEY and ACR_SECRET_KEY must be set together.")
    return validate_credential(key), validate_credential(secret)


def credentials_from_file(path):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScanStop(f"Cannot read credential file ({type(exc).__name__}).") from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if separator and name in {"ACR_ACCESS_KEY", "ACR_SECRET_KEY"}:
            values[name] = value
    key = values.get("ACR_ACCESS_KEY")
    secret = values.get("ACR_SECRET_KEY")
    if key is None or secret is None:
        raise ScanStop("Credential file must contain both ACR_ACCESS_KEY and ACR_SECRET_KEY.")
    return validate_credential(key), validate_credential(secret)


def hidden_credential(label):
    # Refuse fallback to echoed input (e.g. piping this script to python).
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass(label).strip()
        except getpass.GetPassWarning:
            raise ScanStop("An interactive terminal is required for hidden credential entry.") from None
    return validate_credential(value)


def stop_signal(signum, frame):
    raise KeyboardInterrupt


def local_check(reader, info, check_all=False):
    schedule = list(windows(info["frames"], info["sample_rate"]))
    selected = range(len(schedule)) if check_all else sorted({0, min(12, len(schedule) - 1), len(schedule) - 1})
    available, skipped, payload_bytes = 0, 0, 0
    began = time.perf_counter()
    for i in selected:
        index, start, count = schedule[i]
        _, fp, reason = prepare_window(reader, start, count)
        available += int(reason is None)
        skipped += int(reason is not None)
        payload_bytes += len(fp)
        if not check_all or (i + 1) % 50 == 0 or i == len(schedule) - 1:
            log(f"[LOCAL] {index + 1}/{len(schedule)} at {start / info['sample_rate']:.2f}s, "
                f"length={count / info['sample_rate']:.6f}s; "
                + (f"{reason}; NOT an API no-match" if reason else
                   f"fingerprint={len(fp)} bytes; sha256={hashlib.sha256(fp).hexdigest()}"))
    log(f"[CHECK] windows={available + skipped}; fingerprints={available}; locally_skipped={skipped}; "
        f"fingerprint_bytes={payload_bytes}; elapsed={time.perf_counter() - began:.3f}s")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Defaults to a sibling <audio-stem>.acr-sdk-scan directory")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--execute", action="store_true", help="Send real requests; otherwise preflight only")
    parser.add_argument("--check-all", action="store_true", help="Generate ALL fingerprints locally; no HTTP, credentials or output files")
    parser.add_argument("--retry-stopped", action="store_true", help="Explicitly permit retrying failed/uncertain windows")
    parser.add_argument("--max-requests", type=int, default=1, help="Total persisted HTTP attempt cap, including previous runs; 0 is unlimited; default 1")
    parser.add_argument("--env-file", type=Path, help="Read ACR_ACCESS_KEY and ACR_SECRET_KEY from this protected file")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"identify-[a-z0-9-]+\.acrcloud\.com", args.host):
        parser.error("Host must be an identify-*.acrcloud.com hostname, without https:// or a path")
    if args.max_requests < 0 or (args.retry_stopped and not args.execute):
        parser.error("Invalid budget or retry flag without --execute")
    if args.check_all and args.execute:
        parser.error("--check-all is LOCAL ONLY; cannot combine it with --execute")
    audio = args.audio.resolve(strict=True)
    output = args.output.resolve() if args.output else audio.with_name(audio.stem + ".acr-sdk-scan")
    os.umask(0o077)
    with audio.open("rb") as source:
        info = inspect_audio(source)
        _, sdk_info = load_sdk()
        config = scanner_config(info, args.host, sdk_info)
        check_output(output, config)
        total = len(list(windows(info["frames"], info["sample_rate"])))
        log(f"[PLAN] Duration: {info['duration_seconds']:.6f}s; windows: {total}; "
            f"window=12s; step=10s; last={(info['frames'] - (total - 1) * 10 * info['sample_rate']) / info['sample_rate']:.6f}s")
        request_budget = None if args.max_requests == 0 else args.max_requests
        limit_text = "unlimited" if request_budget is None else str(request_budget)
        log(f"[PLAN] Host: {args.host}; data_type=fingerprint; max total HTTP attempts={limit_text}")
        log(f"[PLAN] SDK: {sdk_info['package']}=={sdk_info['version']}; local window decoding")
        log(f"[PLAN] Output: {output}")
        if not args.execute:
            source.seek(0)
            with wave.open(source, "rb") as reader:
                local_check(reader, info, args.check_all)
            log("[DRY RUN] No network requests, no credentials, no output files created.")
            return 0
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (output / "scan.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ScanStop("A scanner is already using this output directory.") from None
            with contextlib.closing(sqlite3.connect(output / "scan.sqlite3")) as db:
                db.row_factory = sqlite3.Row
                initialize_db(db, config)
                already = db.execute("SELECT COUNT(*) FROM (SELECT window_index FROM attempts WHERE state='done' "
                                     "UNION SELECT window_index FROM local_windows)").fetchone()[0]
                if already == total:
                    export_results(db, output, total)
                    log(f"[DONE] All {total} windows already processed (including local skips); no requests sent.")
                    return 0
                log(f"[RESUME] Completed {already}/{total}; remaining {total - already}.")
                credentials = (credentials_from_file(args.env_file)
                               if args.env_file else credentials_from_environment())
                if credentials is None:
                    log("Enter project credentials locally; they will not appear on screen or be saved.")
                    key = hidden_credential("Access Key (hidden): ")
                    secret = hidden_credential("Secret Key (hidden): ")
                else:
                    key, secret = credentials
                    log("[CONFIG] Using ACRCloud credentials from environment; values are not logged.")
                signal.signal(signal.SIGTERM, stop_signal)
                signal.signal(signal.SIGHUP, stop_signal)
                source.seek(0)
                try:
                    with wave.open(source, "rb") as reader:
                        scan(db, reader, config, key, secret, request_budget, args.retry_stopped)
                finally:
                    summary = export_results(db, output, total)
                    log(f"[SAVED] processed={summary['processed_windows']}/{total}; "
                        f"API-completed={summary['completed_windows']}; local-skipped={summary['locally_skipped_windows']}; "
                        f"HTTP attempts={summary['attempts']}. Results: {output}")
                log(f"[DONE] Queue processed: {total} windows. Check local_windows.jsonl; matches still require review.")
                return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("[STOP] Interrupted. Resume using the same output directory; uncertain attempts need review.")
        sys.exit(130)
    except ScanStop as exc:
        log(f"[STOP] {exc}")
        sys.exit(2)
    except Exception as exc:
        log(f"[STOP] {type(exc).__name__}; no automatic retry. Check environment, paths and saved progress.")
        sys.exit(2)
