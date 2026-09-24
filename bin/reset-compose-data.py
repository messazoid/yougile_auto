#!/usr/bin/env python3
"""Validate the Compose data source and clear only this checkout's data directory."""

import json
import os
from pathlib import Path
import shutil
import stat
import sys


TARGET = "/opt/music-verifier/data"
SERVICES = ("init-data", "receiver", "worker", "cisnet-runner")


def fail(message: str) -> None:
    raise SystemExit(f"yougile-reset-data: {message}")


def checked_data_path(repository: Path) -> Path:
    path = repository / "data"
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        fail("data directory is missing")
    if not stat.S_ISDIR(mode) or path.is_mount():
        fail("data path must be a real directory, not a symlink or mount point")
    for name in (".container-initialized", "queue/pipeline.sqlite3"):
        item = path / name
        try:
            item_mode = item.lstat().st_mode
        except FileNotFoundError:
            fail(f"data marker is missing: {name}")
        if not stat.S_ISREG(item_mode):
            fail(f"data marker is not a regular file: {name}")
    return path


def check_mounts(repository: Path, project_name: str) -> str:
    config = json.load(sys.stdin)
    mounts = []
    for service in SERVICES:
        service_config = config.get("services", {}).get(service, {})
        selected = [mount for mount in service_config.get("volumes", [])
                    if mount.get("target") == TARGET]
        if len(selected) != 1:
            fail(f"expected exactly one data mount for {service}")
        mounts.append((selected[0].get("type"), selected[0].get("source")))
    if len(set(mounts)) != 1:
        fail("application services use different data sources")
    kind, source = mounts[0]
    if kind == "volume" and source in ("music-data", f"{project_name}_music-data"):
        return "volume"
    expected = repository / "data"
    if kind == "bind" and source == str(expected):
        checked_data_path(repository)
        return "bind"
    fail("active data source is outside this checkout")


def clear_bind(repository: Path) -> None:
    path = checked_data_path(repository)
    device = path.stat().st_dev
    for current, directories, _ in os.walk(path, followlinks=False):
        for directory in directories:
            child = Path(current) / directory
            if child.is_symlink():
                continue
            if child.is_mount() or child.stat().st_dev != device:
                fail(f"nested mount in data directory: {child}")
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    print(f"Cleared application data in {path}; directory retained.")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4) or sys.argv[1] not in ("check", "clear-bind"):
        fail("internal usage: check REPOSITORY PROJECT or clear-bind REPOSITORY")
    repository = Path(sys.argv[2]).resolve(strict=True)
    if sys.argv[1] == "check" and len(sys.argv) == 4:
        print(check_mounts(repository, sys.argv[3]))
    elif sys.argv[1] == "clear-bind" and len(sys.argv) == 3:
        clear_bind(repository)
    else:
        fail("invalid internal arguments")
