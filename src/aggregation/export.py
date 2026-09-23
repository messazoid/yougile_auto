"""Atomic, read-only filesystem exports for completed aggregation runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import TYPE_CHECKING, Any
import uuid

from job_store import json_sha256

if TYPE_CHECKING:
    from job_store import PipelineStore


EXPORT_CONTRACT_VERSION = "aggregation-files/v3"
_REQUIRED_FILES = ("result.json", "summary.json", "summary.md", "manifest.json")


class AggregationExportError(RuntimeError):
    """A completed aggregation run could not be exported safely."""


def aggregation_export_directory(store: "PipelineStore", recognition_id: int, aggregation_run_id: int) -> Path:
    """Return the stable artifact directory adjacent to the pipeline data root."""
    # Production uses ``data/queue/pipeline.sqlite3`` and therefore publishes
    # at ``data/aggregation``.  Unit fixtures and isolated deployments can use
    # a DB outside a queue directory without accidentally sharing an ancestor.
    data_root = store.path.parent.parent if store.path.parent.name == "queue" else store.path.parent
    return data_root / "aggregation" / f"{recognition_id}_{aggregation_run_id}"


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_text(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _family_by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["family_id"]: item
        for item in result.get("track_families", [])
        if isinstance(item, dict) and isinstance(item.get("family_id"), str)
    }


def _iswc_values(candidate: dict[str, Any]) -> list[str]:
    """Return all ISWCs attached to one original ACRCloud music candidate."""
    values: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in values:
            values.append(value)
        elif isinstance(value, list):
            for item in value:
                add(item)

    external_ids = candidate.get("external_ids")
    if isinstance(external_ids, dict):
        add(external_ids.get("iswc"))
    works = candidate.get("works")
    if isinstance(works, list):
        for work in works:
            if isinstance(work, dict):
                add(work.get("iswc"))
    return values


def _input_candidates(canonical_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index immutable S0 candidates by the observation IDs used by S8."""
    candidates: dict[str, dict[str, Any]] = {}
    for window in canonical_input.get("windows", []):
        if not isinstance(window, dict):
            continue
        for candidate in window.get("candidates", []):
            if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str):
                candidates[candidate["candidate_id"]] = candidate
    return candidates


def build_public_result(result: dict[str, Any], canonical_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the intentionally minimal result.json projection.

    The full aggregation result remains in SQLite.  Names and ISWCs come from
    the immutable original ACRCloud candidates supporting each appearance, so
    the export preserves ACRCloud spelling and does not infer a work identity.
    """
    candidates = _input_candidates(canonical_input)
    public: list[dict[str, Any]] = []
    for appearance in result.get("appearances", []):
        if not isinstance(appearance, dict):
            continue
        observed = appearance.get("observed_range")
        if not isinstance(observed, dict):
            observed = {}
        supporting = [
            candidates[observation_id]
            for observation_id in appearance.get("supporting_observation_ids", [])
            if isinstance(observation_id, str) and observation_id in candidates
        ]
        title = next(
            (item["title"] for item in supporting if isinstance(item.get("title"), str)),
            None,
        )
        artist = next(
            (item["artists"] for item in supporting if isinstance(item.get("artists"), list)),
            [],
        )
        iswcs: list[str] = []
        for candidate in supporting:
            raw = candidate.get("raw_values")
            values = raw if isinstance(raw, dict) else candidate
            for iswc in _iswc_values(values):
                if iswc not in iswcs:
                    iswcs.append(iswc)
        item: dict[str, Any] = {
            "period": {"start": observed.get("start"), "end": observed.get("end")},
            "title": title,
            "artist": artist,
        }
        if iswcs:
            item["iswc"] = iswcs
        public.append(item)
    return public


def _candidate_tracks(
    result: dict[str, Any],
    families: dict[str, dict[str, Any]],
    appearances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project every family returned by the single recognition pass.

    Final appearances remain the conservative timeline.  Families supported
    only by lower-ranked observations are retained as possible tracks instead
    of disappearing from the human-readable export.
    """
    appearance_ids: dict[str, list[str]] = {}
    for item in appearances:
        family_id = item.get("family_id")
        appearance_id = item.get("appearance_id")
        if isinstance(family_id, str) and isinstance(appearance_id, str):
            appearance_ids.setdefault(family_id, []).append(appearance_id)

    evidence_by_family: dict[str, list[dict[str, Any]]] = {}
    for item in result.get("evidence", []):
        if not isinstance(item, dict) or not isinstance(item.get("family_id"), str):
            continue
        evidence_by_family.setdefault(item["family_id"], []).append(item)

    tracks = []
    for family_id, family in sorted(families.items()):
        evidence = evidence_by_family.get(family_id, [])
        ranks = [item["rank"] for item in evidence if isinstance(item.get("rank"), int)]
        scores = [item["score"] for item in evidence if isinstance(item.get("score"), (int, float))]
        starts = [item["source_start"] for item in evidence if isinstance(item.get("source_start"), (int, float))]
        ends = [item["source_end"] for item in evidence if isinstance(item.get("source_end"), (int, float))]
        windows = {item["window_index"] for item in evidence if isinstance(item.get("window_index"), int)}
        kinds = sorted({str(item["kind"]) for item in evidence if item.get("kind")})
        linked_appearances = sorted(appearance_ids.get(family_id, []))
        if linked_appearances:
            status = "primary_appearance"
        elif "persistent_competitor" in kinds:
            status = "possible_persistent_secondary"
        else:
            status = "possible_secondary"
        tracks.append({
            "family_id": family_id,
            "status": status,
            "title": family.get("preferred_core_title"),
            "artists": family.get("artists", []),
            "version_alternatives": family.get("version_alternatives", []),
            "version_signatures": family.get("version_signatures", []),
            "first_observed": min(starts) if starts else None,
            "last_observed": max(ends) if ends else None,
            "best_rank": min(ranks) if ranks else None,
            "best_score": max(scores) if scores else None,
            "observation_count": len(evidence),
            "window_count": len(windows),
            "evidence_kinds": kinds,
            "appearance_ids": linked_appearances,
            "ambiguity_flags": family.get("ambiguity_flags", []),
        })
    return sorted(
        tracks,
        key=lambda item: (
            item["first_observed"] is None,
            item["first_observed"] if item["first_observed"] is not None else 0,
            item["best_rank"] if item["best_rank"] is not None else 10**9,
            item["family_id"],
        ),
    )


def build_summary(result: dict[str, Any], recognition_id: int, aggregation_run_id: int) -> dict[str, Any]:
    """Build a compact, non-decisional projection of an AggregatedResult."""
    families = _family_by_id(result)
    appearances = []
    for appearance in result.get("appearances", []):
        if not isinstance(appearance, dict):
            continue
        family = families.get(appearance.get("family_id"), {})
        observed = appearance.get("observed_range") if isinstance(appearance.get("observed_range"), dict) else {}
        gaps = appearance.get("gaps") if isinstance(appearance.get("gaps"), list) else []
        conflicts = appearance.get("conflict_ids") if isinstance(appearance.get("conflict_ids"), list) else []
        appearances.append({
            "appearance_id": appearance.get("appearance_id"),
            "start": observed.get("start"),
            "end": observed.get("end"),
            "title": family.get("preferred_core_title"),
            "artists": family.get("artists", []),
            "supports": len(appearance.get("supporting_observation_ids", [])),
            "family_id": appearance.get("family_id"),
            "recording_identity_ids": appearance.get("member_recording_identity_ids", []),
            "preferred_recording_identity_id": appearance.get("preferred_recording_identity_id"),
            "gaps": [
                {key: gap.get(key) for key in ("kind", "source_start", "source_end", "bridged")}
                for gap in gaps if isinstance(gap, dict)
            ],
            "conflict_ids": conflicts,
            "ambiguity_flags": appearance.get("family_ambiguity_flags", []),
        })
    candidate_tracks = _candidate_tracks(result, families, appearances)
    catalog = result.get("evidence_catalog") if isinstance(result.get("evidence_catalog"), dict) else {}
    return {
        "summary_contract_version": EXPORT_CONTRACT_VERSION,
        "recognition_id": recognition_id,
        "aggregation_run_id": aggregation_run_id,
        "families_count": len(result.get("track_families", [])),
        "appearances_count": len(appearances),
        "transitions_count": len(result.get("transitions", [])),
        "conflicts_count": len(result.get("conflicts", [])),
        "observations_count": len(catalog.get("observations", [])),
        "candidate_tracks_count": len(candidate_tracks),
        "possible_tracks_count": sum(item["status"] != "primary_appearance" for item in candidate_tracks),
        "appearances": appearances,
        "candidate_tracks": candidate_tracks,
    }


def _markdown_cell(value: Any) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", " ")


def build_summary_markdown(summary: dict[str, Any]) -> str:
    """Render a stable human-readable report without raw candidate payloads."""
    lines = [
        "# Aggregation report",
        "",
        f"- Recognition: `{summary['recognition_id']}`",
        f"- Aggregation run: `{summary['aggregation_run_id']}`",
        f"- Families: {summary['families_count']}; appearances: {summary['appearances_count']}; "
        f"transitions: {summary['transitions_count']}; conflicts: {summary['conflicts_count']}; "
        f"observations: {summary['observations_count']}",
        "",
        "## Appearances",
        "",
        "| Time, s | Title | Artist | Supports |",
        "| --- | --- | --- | ---: |",
    ]
    notes = []
    for item in summary["appearances"]:
        artists = ", ".join(str(value) for value in item["artists"]) or "—"
        lines.append(
            f"| {_markdown_cell(item['start'])}–{_markdown_cell(item['end'])} | "
            f"{_markdown_cell(item['title'])} | {_markdown_cell(artists)} | {item['supports']} |"
        )
        details = []
        if item["gaps"]:
            details.append("gaps: " + ", ".join(
                f"{gap.get('kind')} ({gap.get('source_start')}–{gap.get('source_end')})"
                for gap in item["gaps"]
            ))
        if item["conflict_ids"]:
            details.append(f"conflicts: {len(item['conflict_ids'])}")
        if item["ambiguity_flags"]:
            details.append("ambiguity: " + ", ".join(str(value) for value in item["ambiguity_flags"]))
        if details:
            notes.append(f"- `{item['appearance_id']}` — " + "; ".join(details))
    if notes:
        lines.extend(["", "## Notes", "", *notes])
    lines.extend([
        "",
        "## All ACRCloud candidate families (single recognition pass)",
        "",
        "This section preserves every candidate family returned by the same ACRCloud pass. "
        "Possible tracks are evidence, not verified final results.",
        "",
        "| Time, s | Status | Title | Artist | Versions | Best rank | Windows |",
        "| --- | --- | --- | --- | --- | ---: | ---: |",
    ])
    for item in summary.get("candidate_tracks", []):
        artists = ", ".join(str(value) for value in item.get("artists", [])) or "—"
        versions = ", ".join(str(value) for value in item.get("version_signatures", []))
        if not versions:
            versions = ", ".join(str(value) for value in item.get("version_alternatives", [])) or "—"
        start = _markdown_cell(item.get("first_observed"))
        end = _markdown_cell(item.get("last_observed"))
        lines.append(
            f"| {start}–{end} | {_markdown_cell(item.get('status'))} | "
            f"{_markdown_cell(item.get('title'))} | {_markdown_cell(artists)} | "
            f"{_markdown_cell(versions)} | {_markdown_cell(item.get('best_rank'))} | "
            f"{item.get('window_count', 0)} |"
        )
    return "\n".join(lines) + "\n"


def _manifest(run: dict[str, Any], input_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "recognition_id": run["recognition_id"],
        "aggregation_run_id": run["id"],
        "aggregation_input_id": run["aggregation_input_id"],
        "engine_version": run["engine_version"],
        "result_schema_version": run["result_schema_version"],
        "profile_name": run["profile_name"],
        "profile_hash": run["profile_hash"],
        "input_hash": input_row["input_hash"],
        "result_digest": run["result_digest"],
        "created_at": run["created_at"],
        "completed_at": run["completed_at"],
    }


def _existing_export_matches(destination: Path, public_result: list[dict[str, Any]], manifest: dict[str, Any]) -> bool:
    if not destination.is_dir() or any(not (destination / name).is_file() for name in _REQUIRED_FILES):
        return False
    try:
        existing_result = json.loads((destination / "result.json").read_text(encoding="utf-8"))
        existing_manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return json_sha256(existing_result) == json_sha256(public_result) and existing_manifest == manifest


def export_completed_aggregation_run(
    store: "PipelineStore", aggregation_run_id: int, *, replace_existing: bool = False,
) -> Path:
    """Atomically publish four files for one durable completed aggregation run.

    The database remains authoritative.  Existing targets are rejected unless a
    caller explicitly requests a controlled replacement from that same durable
    result.
    """
    run = store.aggregation_run(aggregation_run_id)
    if not run or run["state"] != "complete" or not run["result_json"] or not run["result_digest"]:
        raise AggregationExportError("Aggregation run is not a completed result")
    if json_sha256(run["result_json"]) != run["result_digest"]:
        raise AggregationExportError("Completed aggregation result digest does not match")
    input_row = store.aggregation_input(run["aggregation_input_id"])
    if not input_row:
        raise AggregationExportError("Aggregation input is unavailable")
    result = json.loads(run["result_json"])
    canonical_input = json.loads(input_row["canonical_json"])
    public_result = build_public_result(result, canonical_input)
    summary = build_summary(result, run["recognition_id"], run["id"])
    manifest = _manifest(run, input_row)
    destination = aggregation_export_directory(store, run["recognition_id"], run["id"])
    if destination.exists():
        if _existing_export_matches(destination, public_result, manifest):
            return destination
        if not replace_existing or not destination.is_dir():
            raise AggregationExportError("Aggregation export destination exists but is incomplete or mismatched")

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    previous: Path | None = None
    published = False
    try:
        _write_json(temporary / "result.json", public_result)
        _write_json(temporary / "summary.json", summary)
        _write_text(temporary / "summary.md", build_summary_markdown(summary))
        _write_json(temporary / "manifest.json", manifest)
        if destination.exists():
            previous = destination.with_name(f".{destination.name}.previous-{uuid.uuid4().hex}")
            os.replace(destination, previous)
        os.replace(temporary, destination)
        published = True
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if previous and previous.exists() and not destination.exists():
            os.replace(previous, destination)
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        if published and previous:
            shutil.rmtree(previous, ignore_errors=True)
    return destination
