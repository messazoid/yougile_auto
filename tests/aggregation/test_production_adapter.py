import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import job_store
from offline.aggregation.archive_adapter import load_run
from src.aggregation import RecognitionWindowStatus
from src.aggregation.production_adapter import (
    ADAPTER_VERSION,
    canonical_run_hash,
    load_production_run,
    serialize_canonical_run,
)
from aggregation import runner as production_runner
import aggregation.export as aggregation_export
from src.aggregation import (
    CALIBRATED_AGGREGATION_V2,
    CALIBRATED_FAMILY_V2,
    CALIBRATED_TEMPORAL_V2,
    aggregate_families,
    aggregate_secondary_evidence,
    aggregate_temporally,
    aggregate_transitions,
    build_aggregated_result,
    extract_features,
    normalize_run,
)


ROOT = Path(__file__).parents[2]
RAW_ROOT = ROOT / "offline" / "fixtures" / "raw"


def semantic_projection(run):
    """Compare canonical observations while allowing adapter-specific locators."""
    return {
        "run_id": run.run_id,
        "source_id": run.source_id,
        "source_duration": run.source_duration,
        "window_size": run.window_size,
        "step": run.step,
        "source_raw_values": run.source_raw_values,
        "windows": [
            {
                "index": item.index,
                "source_start": item.source_start,
                "source_end": item.source_end,
                "actual_duration": item.actual_duration,
                "status": item.status.value,
                "raw_values": dict(item.raw_values),
                "candidates": [
                    {
                        "rank": candidate.rank,
                        "score": candidate.score,
                        "acrid": candidate.acrid,
                        "isrc": candidate.isrc,
                        "title": candidate.title,
                        "artists": candidate.artists,
                        "album": candidate.album,
                        "label": candidate.label,
                        "version": candidate.version,
                        "play_offset": candidate.play_offset,
                        "raw_values": dict(candidate.raw_values),
                    }
                    for candidate in item.candidates
                ],
            }
            for item in run.windows
        ],
    }


def aggregate_result(run):
    normalized = normalize_run(run)
    temporal = aggregate_temporally(normalized, CALIBRATED_TEMPORAL_V2)
    families = aggregate_families(temporal, CALIBRATED_FAMILY_V2)
    secondary = aggregate_secondary_evidence(families, CALIBRATED_AGGREGATION_V2)
    transitions = aggregate_transitions(secondary, CALIBRATED_AGGREGATION_V2)
    return build_aggregated_result(extract_features(transitions))


def semantic_conflict_order(result):
    acrid_by_identity = {item.identity_id: item.acrid for item in result.recording_identities}
    observations = {
        item.observation_id: (item.window_index, item.rank, acrid_by_identity.get(item.recording_identity_id))
        for item in result.observations
    }
    return [
        (item.kind.value, item.resolution_state.value, item.source_start, item.source_end,
         item.window_indices, tuple(observations[observation_id] for observation_id in item.observation_ids))
        for item in result.conflicts
    ]
class ProductionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture_store(self, run_path: Path, *, recognition_id=None):
        job = json.loads((run_path / "job.json").read_text(encoding="utf-8"))
        sources = json.loads((run_path / "sources.json").read_text(encoding="utf-8"))
        recognition_id = recognition_id or job["recognition_id"]
        store = job_store.PipelineStore(self.root / f"{run_path.name}.sqlite3")
        store.initialize()
        with store.connect() as db:
            db.execute(
                """
                INSERT INTO recognitions
                    (id,audio_sha256,config_hash,audio_path,result_dir,state,per_job_limit,created_utc,updated_utc)
                VALUES (?,?,?,?,?,'complete_candidates',0,'fixture','fixture')
                """,
                (recognition_id, job["audio_sha256"], "fixture-config", "fixture.wav", str(run_path)),
            )
            for number, source in enumerate(sources["logical_sources"], start=1):
                source = dict(source)
                source_id = source.pop("source_job_id")
                source_kind = source.pop("source_kind")
                source_filename = source.pop("source_filename")
                db.execute(
                    """
                    INSERT INTO source_jobs
                        (id,source_hash,prep_config_hash,source_kind,source_url,stage,created_utc,
                         updated_utc,source_filename,source_manifest_json,recognition_id)
                    VALUES (?,?,?,?,?,'complete_candidates','fixture','fixture',?,?,?)
                    """,
                    (source_id, f"fixture-source-{number}", "fixture-prep", source_kind,
                     "fixture://source", source_filename,
                     json.dumps(source, ensure_ascii=False, sort_keys=True), recognition_id),
                )
            db.commit()
        return store, recognition_id

    def test_ten_real_runs_semantically_match_offline_adapter(self):
        runs = sorted(path for path in RAW_ROOT.iterdir() if path.is_dir())
        self.assertEqual(len(runs), 10)
        for run_path in runs:
            store, recognition_id = self.fixture_store(run_path)
            production = load_production_run(store, recognition_id)
            offline = load_run(run_path)
            self.assertEqual(semantic_projection(production), semantic_projection(offline), run_path.name)
            self.assertEqual(len(production.windows), len(offline.windows), run_path.name)
            self.assertEqual(
                sum(len(item.candidates) for item in production.windows),
                sum(len(item.candidates) for item in offline.windows), run_path.name,
            )

    def test_real_provenance_1001_local_and_candidate_metadata_are_conserved(self):
        run_path = RAW_ROOT / "cce58d848c724a753a7c1314-dc50dd1106209666"
        store, recognition_id = self.fixture_store(run_path)
        run = load_production_run(store, recognition_id)
        statuses = [item.status for item in run.windows]
        self.assertIn(RecognitionWindowStatus.NO_RESULT_1001, statuses)
        self.assertIn(RecognitionWindowStatus.NOT_SUBMITTED, statuses)
        candidate_window = next(item for item in run.windows if item.candidates)
        candidate = candidate_window.candidates[0]
        self.assertEqual(candidate.play_offset, candidate.raw_values.get("play_offset_ms"))
        self.assertEqual(candidate.raw_provenance.adapter, ADAPTER_VERSION)
        self.assertEqual(candidate_window.raw_provenance.locator["table"], "attempts")
        local = next(item for item in run.windows if item.status is RecognitionWindowStatus.NOT_SUBMITTED)
        self.assertEqual(local.raw_provenance.locator["table"], "local_windows")

    def test_ten_real_runs_have_adapter_neutral_final_conflict_order(self):
        for run_path in sorted(path for path in RAW_ROOT.iterdir() if path.is_dir()):
            store, recognition_id = self.fixture_store(run_path)
            production = aggregate_result(load_production_run(store, recognition_id))
            offline = aggregate_result(load_run(run_path))
            self.assertEqual(semantic_conflict_order(production), semantic_conflict_order(offline), run_path.name)
            self.assertEqual(
                (len(production.appearances), len(production.transitions), len(production.appearance_splits),
                 len(production.conflicts), len(production.observations)),
                (len(offline.appearances), len(offline.transitions), len(offline.appearance_splits),
                 len(offline.conflicts), len(offline.observations)), run_path.name,
            )

    def test_adapter_represents_processing_error_and_missing_raw_without_exports(self):
        run_path = self.root / "synthetic"
        run_path.mkdir()
        scan_path = run_path / "scan.sqlite3"
        config = {
            "audio": {"frames": 30, "sample_rate": 1, "duration_seconds": 30},
            "window_seconds": 10, "step_seconds": 10,
        }
        with sqlite3.connect(scan_path) as db:
            db.executescript(
                "CREATE TABLE metadata (id INTEGER PRIMARY KEY, config TEXT NOT NULL);"
                "CREATE TABLE attempts (id INTEGER PRIMARY KEY,window_index INTEGER,start_seconds REAL,"
                "duration_seconds REAL,started_utc TEXT,state TEXT,http_status INTEGER,acr_code INTEGER,"
                "response_raw BLOB,error_type TEXT,fingerprint_bytes INTEGER,fingerprint_sha256 TEXT,"
                "retry_number INTEGER,retryable INTEGER);"
                "CREATE TABLE local_windows (window_index INTEGER PRIMARY KEY,start_seconds REAL,"
                "duration_seconds REAL,reason TEXT,sample_sha256 TEXT,created_utc TEXT);"
            )
            db.execute("INSERT INTO metadata VALUES (1,?)", (json.dumps(config),))
            raw = json.dumps({"metadata": {"music": [{"acrid": "raw-id", "score": 88,
                      "play_offset_ms": 42, "artists": [{"name": "Artist"}]}]}}).encode()
            db.execute(
                "INSERT INTO attempts VALUES (1,0,0,10,'fixture','done',200,0,?,NULL,1,'hash',0,0)",
                (raw,),
            )
            db.execute(
                "INSERT INTO attempts VALUES (2,1,10,10,'fixture','error',503,NULL,NULL,'HttpError',1,'hash',0,0)"
            )
        store = job_store.PipelineStore(self.root / "synthetic.sqlite3")
        store.initialize()
        with store.connect() as db:
            db.execute(
                "INSERT INTO recognitions (id,audio_sha256,config_hash,audio_path,result_dir,state,per_job_limit,created_utc,updated_utc) "
                "VALUES (1,'audio','config','audio.wav',?,'partial',0,'fixture','fixture')", (str(run_path),)
            )
            db.execute(
                "INSERT INTO source_jobs (source_hash,prep_config_hash,source_kind,source_url,stage,created_utc,updated_utc,source_manifest_json,recognition_id) "
                "VALUES ('source','prep','fixture','fixture://source','partial','fixture','fixture','{}',1)"
            )
            db.commit()
        run = load_production_run(store, 1)
        self.assertEqual([item.status for item in run.windows], [
            RecognitionWindowStatus.CANDIDATES_RETURNED,
            RecognitionWindowStatus.PROCESSING_ERROR,
            RecognitionWindowStatus.MISSING_RAW,
        ])
        self.assertEqual(run.windows[0].candidates[0].raw_values["acrid"], "raw-id")

    def test_input_hash_and_store_idempotency_leases_and_retry(self):
        run_path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        store, recognition_id = self.fixture_store(run_path)
        run = load_production_run(store, recognition_id)
        first_json, second_json = serialize_canonical_run(run), serialize_canonical_run(load_production_run(store, recognition_id))
        self.assertEqual(first_json, second_json)
        self.assertEqual(canonical_run_hash(run), canonical_run_hash(run))
        first = store.ensure_aggregation_input(
            recognition_id, run.contract_version, ADAPTER_VERSION, first_json, canonical_run_hash(run)
        )
        second = store.ensure_aggregation_input(
            recognition_id, run.contract_version, ADAPTER_VERSION, second_json, canonical_run_hash(run)
        )
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(sqlite3.DatabaseError):
            with store.connect() as db:
                db.execute("UPDATE aggregation_inputs SET adapter_version='changed' WHERE id=?", (first["id"],))
        initial = store.ensure_aggregation_run(
            first["id"], "aggregation-engine/v1", "aggregated-result/v1", "profile-v1", "profile-hash-v1"
        )
        self.assertEqual(initial["state"], "pending")
        self.assertEqual(initial["id"], store.ensure_aggregation_run(
            first["id"], "aggregation-engine/v1", "aggregated-result/v1", "profile-v1", "profile-hash-v1"
        )["id"])
        now = time.time()
        claimed = store.claim_aggregation_run("fixture", lease_seconds=5, now_epoch=now)
        self.assertEqual(claimed["id"], initial["id"])
        self.assertIsNone(store.claim_aggregation_run("second", lease_seconds=5, now_epoch=now + 1))
        before_state = store.recognition(recognition_id)["state"]
        store.fail_aggregation_run(claimed["id"], claimed["lease_token"], "FixtureError", "retry later", 60)
        self.assertEqual(store.recognition(recognition_id)["state"], before_state)
        with store.connect() as db:
            db.execute("UPDATE aggregation_runs SET state='running',lease_token='expired',lease_expires_at=? WHERE id=?", (now - 1, initial["id"]))
            db.commit()
        self.assertEqual(store.recover_expired_aggregation_leases(now), 1)
        recovered = store.aggregation_runs_for_recognition(recognition_id)[0]
        self.assertEqual((recovered["state"], recovered["last_error_class"]), ("error", "LeaseExpired"))
        retry_claim = store.claim_aggregation_run("retry", lease_seconds=5, now_epoch=now + 1)
        self.assertEqual(retry_claim["id"], initial["id"])
        digest = store.complete_aggregation_run(
            retry_claim["id"], retry_claim["lease_token"], {"contract_version": "aggregated-result/v1", "run": {"id": "fixture"}}
        )
        completed = store.aggregation_runs_for_recognition(recognition_id)[0]
        self.assertEqual((completed["state"], completed["result_digest"]), ("complete", digest))
        self.assertEqual(digest, job_store.json_sha256(completed["result_json"]))
        self.assertEqual(
            digest,
            job_store.json_sha256('{"run":{"id":"fixture"},"contract_version":"aggregated-result/v1"}'),
        )
        changed = store.ensure_aggregation_run(
            first["id"], "aggregation-engine/v2", "aggregated-result/v1", "profile-v2", "profile-hash-v2"
        )
        self.assertNotEqual(initial["id"], changed["id"])

    def test_runner_reconciles_executes_and_preserves_ten_run_semantics(self):
        for run_path in sorted(path for path in RAW_ROOT.iterdir() if path.is_dir()):
            store, recognition_id = self.fixture_store(run_path)
            self.assertEqual(production_runner.reconcile_aggregation(store), 1)
            self.assertEqual(production_runner.reconcile_aggregation(store), 0)
            scheduled = store.aggregation_runs_for_recognition(recognition_id)
            self.assertEqual(len(scheduled), 1)
            self.assertEqual(scheduled[0]["state"], "pending")
            self.assertEqual(production_runner.run_one_aggregation(store, "fixture"), "complete")
            completed = store.aggregation_runs_for_recognition(recognition_id)[0]
            self.assertEqual(completed["state"], "complete")
            self.assertEqual(completed["result_digest"], job_store.json_sha256(completed["result_json"]))
            expected = aggregate_result(load_production_run(store, recognition_id))
            self.assertEqual(
                completed["result_json"],
                job_store.canonical_json_text(production_runner.serialize_aggregated_result(expected)),
            )
            offline = aggregate_result(load_run(run_path))
            self.assertEqual(semantic_conflict_order(expected), semantic_conflict_order(offline), run_path.name)
            self.assertEqual(
                (len(expected.appearances), len(expected.transitions), len(expected.appearance_splits),
                 len(expected.conflicts), len(expected.observations)),
                (len(offline.appearances), len(offline.transitions), len(offline.appearance_splits),
                 len(offline.conflicts), len(offline.observations)), run_path.name,
            )

    def test_v2_reconciliation_does_not_backfill_recognitions_with_prior_aggregation(self):
        run_path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        store, recognition_id = self.fixture_store(run_path)
        canonical = load_production_run(store, recognition_id)
        canonical_json = serialize_canonical_run(canonical)
        input_row = store.ensure_aggregation_input(
            recognition_id,
            canonical.contract_version,
            "fixture-adapter",
            canonical_json,
            canonical_run_hash(canonical),
        )
        store.ensure_aggregation_run(
            input_row["id"],
            "aggregation-engine/v1",
            production_runner.RESULT_SCHEMA_VERSION,
            "aggregation-v1.1-experimental",
            "prior-profile-hash",
        )

        self.assertEqual(production_runner.reconcile_aggregation(store), 0)
        runs = store.aggregation_runs_for_recognition(recognition_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["engine_version"], "aggregation-engine/v1")

    def test_completed_run_exports_atomic_deterministic_artifacts(self):
        run_path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        store, recognition_id = self.fixture_store(run_path)
        production_runner.ensure_aggregation_for_recognition(store, recognition_id)

        self.assertEqual(production_runner.run_one_aggregation(store, "fixture"), "complete")
        completed = store.aggregation_runs_for_recognition(recognition_id)[0]
        input_row = store.aggregation_input(completed["aggregation_input_id"])
        destination = aggregation_export.aggregation_export_directory(store, recognition_id, completed["id"])

        self.assertEqual(
            {path.name for path in destination.iterdir()},
            {"result.json", "summary.json", "summary.md", "manifest.json"},
        )
        result = json.loads((destination / "result.json").read_text(encoding="utf-8"))
        stored_result = json.loads(completed["result_json"])
        summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            result,
            aggregation_export.build_public_result(stored_result, json.loads(input_row["canonical_json"])),
        )
        self.assertTrue(all(set(item) <= {"period", "title", "artist", "iswc"} for item in result))
        self.assertTrue(all(set(item["period"]) == {"start", "end"} for item in result))
        self.assertEqual(
            (summary["recognition_id"], summary["aggregation_run_id"]),
            (recognition_id, completed["id"]),
        )
        self.assertEqual(
            (summary["families_count"], summary["appearances_count"], summary["transitions_count"],
             summary["conflicts_count"], summary["observations_count"]),
            (len(stored_result["track_families"]), len(stored_result["appearances"]), len(stored_result["transitions"]),
             len(stored_result["conflicts"]), len(stored_result["evidence_catalog"]["observations"])),
        )
        self.assertEqual(
            [item["start"] for item in summary["appearances"]],
            [item["observed_range"]["start"] for item in stored_result["appearances"]],
        )
        self.assertEqual(summary["summary_contract_version"], "aggregation-files/v3")
        self.assertEqual(summary["candidate_tracks_count"], len(stored_result["track_families"]))
        self.assertEqual(
            {item["family_id"] for item in summary["candidate_tracks"]},
            {item["family_id"] for item in stored_result["track_families"]},
        )
        appearance_families = {item["family_id"] for item in stored_result["appearances"]}
        self.assertTrue(all(
            (item["status"] == "primary_appearance") == (item["family_id"] in appearance_families)
            for item in summary["candidate_tracks"]
        ))
        self.assertEqual(
            {
                "recognition_id": manifest["recognition_id"],
                "aggregation_run_id": manifest["aggregation_run_id"],
                "aggregation_input_id": manifest["aggregation_input_id"],
                "engine_version": manifest["engine_version"],
                "result_schema_version": manifest["result_schema_version"],
                "profile_name": manifest["profile_name"],
                "profile_hash": manifest["profile_hash"],
                "input_hash": manifest["input_hash"],
                "result_digest": manifest["result_digest"],
                "created_at": manifest["created_at"],
                "completed_at": manifest["completed_at"],
            },
            {
                "recognition_id": completed["recognition_id"],
                "aggregation_run_id": completed["id"],
                "aggregation_input_id": completed["aggregation_input_id"],
                "engine_version": completed["engine_version"],
                "result_schema_version": completed["result_schema_version"],
                "profile_name": completed["profile_name"],
                "profile_hash": completed["profile_hash"],
                "input_hash": input_row["input_hash"],
                "result_digest": completed["result_digest"],
                "created_at": completed["created_at"],
                "completed_at": completed["completed_at"],
            },
        )
        before = {path.name: path.read_bytes() for path in destination.iterdir()}
        self.assertEqual(aggregation_export.export_completed_aggregation_run(store, completed["id"]), destination)
        self.assertEqual(before, {path.name: path.read_bytes() for path in destination.iterdir()})
        markdown = (destination / "summary.md").read_text(encoding="utf-8")
        self.assertIn("# Aggregation report", markdown)
        self.assertIn("## All ACRCloud candidate families (single recognition pass)", markdown)
        self.assertIn("possible_secondary", markdown)
        self.assertNotIn('"raw_values"', markdown)

    def test_public_result_uses_supporting_acrcloud_metadata_and_optional_iswc(self):
        run = load_run(RAW_ROOT / "b170dcff0641c05ec9137670-0e58820e96b73825")
        public = aggregation_export.build_public_result(
            aggregate_result(run).to_dict(), json.loads(serialize_canonical_run(run)),
        )
        love_and_happiness = next(item for item in public if item["title"] == "Love and Happiness")
        self.assertEqual(love_and_happiness, {
            "period": {"start": 240.0, "end": 292.0},
            "title": "Love and Happiness",
            "artist": ["Al Green"],
            "iswc": ["T0702418798"],
        })
        self.assertTrue(any("iswc" not in item for item in public))

    def test_completed_run_can_explicitly_replace_a_legacy_export(self):
        run_path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        store, recognition_id = self.fixture_store(run_path)
        production_runner.ensure_aggregation_for_recognition(store, recognition_id)
        self.assertEqual(production_runner.run_one_aggregation(store, "fixture"), "complete")
        completed = store.aggregation_runs_for_recognition(recognition_id)[0]
        destination = aggregation_export.aggregation_export_directory(store, recognition_id, completed["id"])
        (destination / "result.json").write_text('{"legacy":true}\n', encoding="utf-8")

        self.assertEqual(
            aggregation_export.export_completed_aggregation_run(
                store, completed["id"], replace_existing=True,
            ),
            destination,
        )
        result = json.loads((destination / "result.json").read_text(encoding="utf-8"))
        self.assertIsInstance(result, list)
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["export_contract_version"], "aggregation-files/v3")
        self.assertEqual(
            list(destination.parent.glob(f".{destination.name}.previous-*")),
            [],
        )

    def test_export_write_failure_preserves_completed_run_and_publishes_no_partial_directory(self):
        run_path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        store, recognition_id = self.fixture_store(run_path)
        production_runner.ensure_aggregation_for_recognition(store, recognition_id)
        original_write_json = aggregation_export._write_json

        def fail_after_result(path, value):
            if path.name == "summary.json":
                raise OSError("fixture export failure")
            return original_write_json(path, value)

        with patch.object(aggregation_export, "_write_json", side_effect=fail_after_result):
            self.assertEqual(production_runner.run_one_aggregation(store, "fixture"), "complete")

        completed = store.aggregation_runs_for_recognition(recognition_id)[0]
        destination = aggregation_export.aggregation_export_directory(store, recognition_id, completed["id"])
        self.assertEqual(completed["state"], "complete")
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.iterdir()), [])

    def test_runner_failure_retry_and_expired_lease_leave_recognition_complete(self):
        run_path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        store, recognition_id = self.fixture_store(run_path)
        run = production_runner.ensure_aggregation_for_recognition(store, recognition_id)
        with patch.object(production_runner, "aggregate_canonical_run", side_effect=ValueError("fixture malformed")):
            self.assertEqual(production_runner.run_one_aggregation(store, "fixture"), "error")
        failed = store.aggregation_runs_for_recognition(recognition_id)[0]
        self.assertEqual((failed["state"], failed["last_error_class"]), ("error", "ValueError"))
        self.assertGreater(failed["next_attempt_at"], time.time() + 3000)
        self.assertEqual(store.recognition(recognition_id)["state"], "complete_candidates")
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT stage FROM source_jobs").fetchone()[0], "complete_candidates")
        with store.connect() as db:
            db.execute(
                "UPDATE aggregation_runs SET state='running',lease_token='expired',lease_expires_at=0 WHERE id=?",
                (run["id"],),
            )
            db.commit()
        self.assertEqual(production_runner.run_one_aggregation(store, "fixture"), "complete")
        completed = store.aggregation_runs_for_recognition(recognition_id)[0]
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(store.recognition(recognition_id)["state"], "complete_candidates")

    def test_new_engine_or_profile_reuses_input_without_rebuilding_recognition(self):
        run_path = RAW_ROOT / "12eae5cb26bc68df9257a54e-309fa49d99885465"
        store, recognition_id = self.fixture_store(run_path)
        current = production_runner.ensure_aggregation_for_recognition(store, recognition_id)
        input_row = store.aggregation_input(current["aggregation_input_id"])
        changed = store.ensure_aggregation_run(
            input_row["id"], "aggregation-engine/v2", production_runner.RESULT_SCHEMA_VERSION,
            "aggregation-v2", "new-profile-hash",
        )
        self.assertNotEqual(current["id"], changed["id"])
        self.assertEqual(current["aggregation_input_id"], changed["aggregation_input_id"])
        self.assertEqual(store.recognition(recognition_id)["state"], "complete_candidates")

    def test_v9_to_current_migration_preserves_rows_and_foreign_keys(self):
        path = self.root / "v9.sqlite3"
        store = job_store.PipelineStore(path)
        store.initialize()
        with store.connect() as db:
            db.execute(
                "INSERT INTO recognitions (id,audio_sha256,config_hash,audio_path,result_dir,state,per_job_limit,created_utc,updated_utc) "
                "VALUES (1,'audio','config','audio.wav','result','complete_no_match',0,'fixture','fixture')"
            )
            db.execute(
                "INSERT INTO source_jobs (source_hash,prep_config_hash,source_kind,source_url,stage,created_utc,updated_utc,recognition_id) "
                "VALUES ('source','prep','fixture','fixture://source','complete_no_match','fixture','fixture',1)"
            )
            db.executescript(
                "DROP TRIGGER aggregation_inputs_no_update; DROP TRIGGER aggregation_inputs_no_delete;"
                "DROP TABLE aggregation_runs; DROP TABLE aggregation_inputs;"
                "UPDATE schema_info SET version=9 WHERE id=1;"
            )
            db.commit()
        migrated = job_store.PipelineStore(path)
        migrated.initialize()
        with migrated.connect() as db:
            self.assertEqual(
                db.execute("SELECT version FROM schema_info").fetchone()[0],
                job_store.SCHEMA_VERSION,
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_jobs").fetchone()[0], 1)
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='aggregation_inputs'").fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_notifications'").fetchone())
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
