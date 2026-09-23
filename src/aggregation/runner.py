"""Bounded production execution of the existing S2--S8 aggregation core."""

from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3
import time
from typing import TYPE_CHECKING

from job_store import StoreError, json_sha256

from . import (
    AGGREGATED_RESULT_CONTRACT_VERSION,
    AGGREGATION_ENGINE_VERSION,
    CANONICAL_RECOGNITION_CONTRACT_VERSION,
    CALIBRATED_AGGREGATION_V2,
    CALIBRATED_FAMILY_V2,
    CALIBRATED_TEMPORAL_V2,
    FEATURE_EXTRACTION_CONTRACT_VERSION,
    CanonicalRecognitionRun,
    aggregate_families,
    aggregate_secondary_evidence,
    aggregate_temporally,
    aggregate_transitions,
    build_aggregated_result,
    extract_features,
    normalize_run,
    serialize_aggregated_result,
)
from .production_adapter import (
    ADAPTER_VERSION,
    ProductionAdapterError,
    canonical_run_hash,
    load_production_run,
    serialize_canonical_run,
)
from .export import export_completed_aggregation_run

if TYPE_CHECKING:
    from job_store import PipelineStore


# This is the sole production persistence contract for the current v2 replay
# profile.  Changing any profile material below deliberately produces a new run.
ENGINE_VERSION = AGGREGATION_ENGINE_VERSION
RESULT_SCHEMA_VERSION = AGGREGATED_RESULT_CONTRACT_VERSION
PROFILE_NAME = "aggregation-v2-calibrated"
PROFILE_HASH = json_sha256({
    "profile_name": PROFILE_NAME,
    "temporal": asdict(CALIBRATED_TEMPORAL_V2),
    "family": asdict(CALIBRATED_FAMILY_V2),
    "secondary_and_transitions": asdict(CALIBRATED_AGGREGATION_V2),
    "feature_contract": FEATURE_EXTRACTION_CONTRACT_VERSION,
})
SUCCESSFUL_RECOGNITION_STATES = frozenset({
    "complete_candidates", "complete_no_match", "complete_local_only",
})
_UNSCHEDULABLE_UNTIL: dict[int, float] = {}


def aggregate_canonical_run(run: CanonicalRecognitionRun):
    """Run the existing S2--S8 core without validation/reference matching."""
    normalized = normalize_run(run)
    temporal = aggregate_temporally(normalized, CALIBRATED_TEMPORAL_V2)
    families = aggregate_families(temporal, CALIBRATED_FAMILY_V2)
    secondary = aggregate_secondary_evidence(families, CALIBRATED_AGGREGATION_V2)
    transitions = aggregate_transitions(secondary, CALIBRATED_AGGREGATION_V2)
    return build_aggregated_result(extract_features(transitions))


def ensure_aggregation_for_recognition(store: "PipelineStore", recognition_id: int) -> dict:
    """Build/find the immutable input and its idempotent current-profile run."""
    try:
        recognition = store.recognition(recognition_id)
        if not recognition or recognition["state"] not in SUCCESSFUL_RECOGNITION_STATES:
            raise ProductionAdapterError("recognition is not a successful completed run")
        canonical = load_production_run(store, recognition_id)
        if canonical.contract_version != CANONICAL_RECOGNITION_CONTRACT_VERSION:
            raise ProductionAdapterError("unsupported canonical recognition contract")
        canonical_json = serialize_canonical_run(canonical)
        input_row = store.ensure_aggregation_input(
            recognition_id,
            canonical.contract_version,
            ADAPTER_VERSION,
            canonical_json,
            canonical_run_hash(canonical),
        )
        run = store.ensure_aggregation_run(
            input_row["id"], ENGINE_VERSION, RESULT_SCHEMA_VERSION, PROFILE_NAME, PROFILE_HASH
        )
    except Exception as error:
        _UNSCHEDULABLE_UNTIL[recognition_id] = time.monotonic() + _retry_delay(error)
        raise
    _UNSCHEDULABLE_UNTIL.pop(recognition_id, None)
    return run


def reconcile_aggregation(store: "PipelineStore", limit: int = 1) -> int:
    """Close the post-recognition crash gap without mutating recognition state."""
    recognition_ids = store.recognition_ids_missing_aggregation_run(
        SUCCESSFUL_RECOGNITION_STATES,
        ENGINE_VERSION,
        RESULT_SCHEMA_VERSION,
        PROFILE_HASH,
        limit=max(limit, min(100, limit + len(_UNSCHEDULABLE_UNTIL))),
        require_no_prior_aggregation=True,
    )
    scheduled = 0
    for recognition_id in recognition_ids:
        if _UNSCHEDULABLE_UNTIL.get(recognition_id, 0) > time.monotonic():
            continue
        ensure_aggregation_for_recognition(store, recognition_id)
        scheduled += 1
        if scheduled >= limit:
            break
    return scheduled


def _retry_delay(error: Exception) -> float:
    """Keep malformed immutable artifacts from causing immediate retry churn."""
    if isinstance(error, (ProductionAdapterError, ValueError, KeyError, TypeError, json.JSONDecodeError)):
        return 3600
    if isinstance(error, (sqlite3.Error, OSError)):
        return 30
    return 300


def execute_claimed_aggregation_run(store: "PipelineStore", claimed: dict) -> str:
    """Execute and durably complete one already-claimed current-contract run."""
    if claimed["state"] != "running" or not claimed.get("lease_token"):
        raise StoreError("Aggregation run is not claimed")
    if (
        claimed["engine_version"], claimed["result_schema_version"], claimed["profile_hash"]
    ) != (ENGINE_VERSION, RESULT_SCHEMA_VERSION, PROFILE_HASH):
        raise StoreError("Aggregation run does not match the current runner contract")
    input_row = store.aggregation_input(claimed["aggregation_input_id"])
    if not input_row:
        raise ProductionAdapterError("aggregation input is unavailable")
    if input_row["recognition_id"] != claimed["recognition_id"]:
        raise ProductionAdapterError("aggregation run/input recognition mismatch")
    if input_row["adapter_version"] != ADAPTER_VERSION:
        raise ProductionAdapterError("aggregation input was not produced by the production adapter")
    if input_row["canonical_schema_version"] != CANONICAL_RECOGNITION_CONTRACT_VERSION:
        raise ProductionAdapterError("unsupported immutable canonical input contract")
    if json_sha256(input_row["canonical_json"]) != input_row["input_hash"]:
        raise ProductionAdapterError("immutable canonical input hash mismatch")
    canonical = CanonicalRecognitionRun.from_dict(json.loads(input_row["canonical_json"]))
    result_json = serialize_aggregated_result(aggregate_canonical_run(canonical))
    digest = store.complete_aggregation_run(claimed["id"], claimed["lease_token"], result_json)
    try:
        export_directory = export_completed_aggregation_run(store, claimed["id"])
        for link in store.links_for_recognition(claimed["recognition_id"]):
            chat_id = link.get("chat_id")
            if not chat_id:
                continue
            try:
                store.enqueue_chat_notification(
                    str(chat_id),
                    "completed",
                    f"completed:{chat_id}:{claimed['recognition_id']}:{claimed['id']}",
                    "Готово. Прикреплён отчёт",
                    export_directory / "summary.md",
                )
            except Exception as error:
                print(
                    f"[NOTIFY] recognition_id={claimed['recognition_id']} aggregation_run_id={claimed['id']} "
                    f"kind=completed stage=enqueue_error type={type(error).__name__}",
                    flush=True,
                )
    except Exception as error:
        # Export artifacts are useful projections, not part of recognition or
        # aggregation completion semantics.  The durable DB result stays complete.
        print(
            f"[AGGREGATION] recognition_id={claimed['recognition_id']} aggregation_run_id={claimed['id']} "
            f"stage=export_error error_type={type(error).__name__}",
            flush=True,
        )
    return digest


def run_one_aggregation(store: "PipelineStore", owner: str = "acr-worker") -> str:
    """Recover leases then execute at most one current-profile aggregation run."""
    store.recover_expired_aggregation_leases()
    claimed = store.claim_aggregation_run(
        owner,
        engine_version=ENGINE_VERSION,
        result_schema_version=RESULT_SCHEMA_VERSION,
        profile_hash=PROFILE_HASH,
    )
    if not claimed:
        return "idle"
    try:
        execute_claimed_aggregation_run(store, claimed)
    except StoreError:
        # A concurrent recovery/worker can only make this claim stale; never
        # overwrite the other owner with an error record.
        return "lost_claim"
    except Exception as error:
        try:
            store.fail_aggregation_run(
                claimed["id"], claimed["lease_token"], type(error).__name__, str(error),
                retry_delay_seconds=_retry_delay(error),
            )
        except StoreError:
            return "lost_claim"
        return "error"
    return "complete"
