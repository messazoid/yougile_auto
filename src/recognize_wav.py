#!/usr/bin/env python3
"""Inject a WAV into the durable recognition and aggregation pipeline."""

import argparse
import os
from pathlib import Path
import shutil
import uuid

from job_store import PipelineStore, stable_hash
from scan import inspect_audio


BASE_DIR = Path(__file__).resolve().parent.parent
PIPELINE_DB = BASE_DIR / "data" / "queue" / "pipeline.sqlite3"
AUDIO_DIR = BASE_DIR / "data" / "audio"
MANUAL_PREP_CONFIG_HASH = stable_hash({"source": "manual_wav", "version": 1})


def copy_into_pipeline(audio: Path, info: dict) -> Path:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    target = AUDIO_DIR / f"manual_{info['sha256']}.wav"
    if target.exists():
        with target.open("rb") as handle:
            if inspect_audio(handle)["sha256"] != info["sha256"]:
                raise RuntimeError("Existing manual WAV has an unexpected checksum")
        return target
    temporary = target.with_name(target.name + f".{os.getpid()}.part")
    try:
        shutil.copyfile(audio, temporary)
        os.chmod(temporary, 0o644)
        with temporary.open("rb") as handle:
            if inspect_audio(handle)["sha256"] != info["sha256"]:
                raise RuntimeError("Copied WAV checksum mismatch")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def resolve_input(value: Path) -> Path:
    """Use the pipeline audio directory for short input names."""
    candidate = value if value.is_absolute() else AUDIO_DIR / value
    return candidate.resolve(strict=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input", type=Path, required=True, help="Input mono PCM WAV file")
    parser.add_argument("--force", action="store_true",
                        help="Run this WAV again, bypassing content deduplication")
    args = parser.parse_args(argv)
    try:
        audio = resolve_input(args.input)
    except FileNotFoundError:
        parser.error(f"WAV file was not found: {args.input}")
    with audio.open("rb") as handle:
        info = inspect_audio(handle)
    target = copy_into_pipeline(audio, info)
    store = PipelineStore(PIPELINE_DB)
    store.initialize()
    force_token = uuid.uuid4().hex if args.force else None
    source_hash = stable_hash("manual_wav", info["sha256"], force_token)
    prep_hash = (f"{MANUAL_PREP_CONFIG_HASH}:scope-run:{force_token}"
                 if force_token else MANUAL_PREP_CONFIG_HASH)
    source_id, created = store.enqueue_manual_wav(
        source_hash, prep_hash, audio.name, target, info,
    )
    state = "queued" if created else "already_registered"
    force_suffix = f" force_run={force_token}" if force_token else ""
    print(f"[MANUAL WAV] source_job={source_id} stage=audio_ready status={state}{force_suffix}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
