#!/usr/bin/env python3
"""Atomically add selected YouGile columns to the protected Compose env file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile


UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?YOUGILE_ALLOWED_COLUMN_IDS[ \t]*=(.*)$")


def parse_ids(value: str) -> list[str]:
    parts = value.split(",")
    if not parts or any(UUID.fullmatch(part.strip()) is None for part in parts):
        raise ValueError("invalid YouGile column IDs")
    return list(dict.fromkeys(part.strip().lower() for part in parts))


def update(path: Path, selected: str) -> str:
    selected_ids = parse_ids(selected)
    if path.is_symlink():
        raise ValueError("refusing a symbolic-link environment file")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("environment file is not a regular file")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    positions = [index for index, line in enumerate(lines) if ASSIGNMENT.match(line)]
    existing_ids: list[str] = []
    if positions:
        raw = ASSIGNMENT.match(lines[positions[-1]]).group(1)
        values = shlex.split(raw, comments=True)
        existing = " ".join(values)
        if existing:
            existing_ids = parse_ids(existing)
    merged = list(dict.fromkeys(existing_ids + selected_ids))
    assignment = "YOUGILE_ALLOWED_COLUMN_IDS=" + ",".join(merged)
    if positions:
        position = positions[-1]
        lines[position] = assignment + ("\n" if lines[position].endswith("\n") else "")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(assignment + "\n")

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), stat.S_IMODE(metadata.st_mode))
            current = os.fstat(handle.fileno())
            if (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
                os.fchown(handle.fileno(), metadata.st_uid, metadata.st_gid)
            handle.write("".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return assignment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file", type=Path)
    parser.add_argument("column_ids")
    args = parser.parse_args()
    print(update(args.env_file, args.column_ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
