"""Regression tests for origin-partitioned cache, atomic sync, filter parity, and SyncWorker hardening."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pyarrow.parquet as pq
import pytest

import imednet_workflows.cached_loader as cached_loader_mod
from imednet import ImednetSDK
from imednet.core.retry import RetryConfig
from imednet.errors import (
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ServerError,
)
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.models.records import Record, RecordStatus
from imednet.simulation import SimulationState, SimulationTransport
from imednet.utils.url import normalize_cache_origin, resolve_sdk_base_url
from imednet_workflows.cached_loader import (
    CachedRecordsLoader,
    StudySyncStatus,
    get_sqlite_connection,
)
from imednet_workflows.record_mapper import RecordMapper
from imednet_workflows.schema_profiler import SchemaProfiler
from imednet_workflows.sync_worker import SyncWorker, SyncWorkerConfig, SyncWorkerState


def _build_env_transport(
    *,
    study_key: str,
    records_ref: list[dict[str, Any]],
    forms: list[dict[str, Any]] | None = None,
    variables: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    """Create a deterministic MockTransport supporting filter, recordDataFilter, and sort."""
    state = SimulationState()
    state.studies = [{"studyKey": study_key, "studyName": study_key}]
    state.forms = forms or [
        {"studyKey": study_key, "formId": 10, "formKey": "DEMOG", "formName": "Demographics"},
        {"studyKey": study_key, "formId": 20, "formKey": "AE", "formName": "Adverse Events"},
    ]
    state.variables = variables or [
        {
            "studyKey": study_key,
            "variableId": 101,
            "variableName": "AGE",
            "variableKey": "AGE",
            "label": "Age",
            "variableType": "integer",
            "formId": 10,
            "formKey": "DEMOG",
        },
        {
            "studyKey": study_key,
            "variableId": 102,
            "variableName": "COHORT",
            "variableKey": "COHORT",
            "label": "Cohort",
            "variableType": "text",
            "formId": 10,
            "formKey": "DEMOG",
        },
        {
            "studyKey": study_key,
            "variableId": 201,
            "variableName": "AETERM",
            "variableKey": "AETERM",
            "label": "AE Term",
            "variableType": "text",
            "formId": 20,
            "formKey": "AE",
        },
    ]
    state.intervals = []
    state.visits = []
    state.subjects = []
    state.sites = []
    sim_transport = SimulationTransport(state=state)

    def _handler(request: httpx.Request) -> httpx.Response:
        state.records = [dict(item) for item in records_ref]
        return sim_transport.handle_request(request)

    return httpx.MockTransport(_handler)


# ---------------------------------------------------------------------------
# Requirement 1: Origin Partitioning & In-Place Legacy Migration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_url", "expected_origin"),
    [
        (None, "https://edc.prod.imednetapi.com"),
        ("", "https://edc.prod.imednetapi.com"),
        ("https://edc.trn.imednetapi.com", "https://edc.trn.imednetapi.com"),
        ("https://EDC.TRN.IMEDNETAPI.COM:443/api/v1/edc/", "https://edc.trn.imednetapi.com"),
        ("https://edc.trn.imednetapi.com/api/v1", "https://edc.trn.imednetapi.com"),
        ("https://edc.trn.imednetapi.com/api/", "https://edc.trn.imednetapi.com"),
        ("http://LOCAL.TEST:80/api/v1/edc", "http://local.test"),
        ("https://edc.trn.imednetapi.com:8443/api/v1/edc/", "https://edc.trn.imednetapi.com:8443"),
        ("https://proxy.example.org/tenant/api/v1/edc/", "https://proxy.example.org/tenant"),
    ],
)
def test_normalize_cache_origin_canonicalizes_urls(
    raw_url: str | None, expected_origin: str
) -> None:
    """Equivalent origin URLs normalize to a single canonical partition key."""
    assert normalize_cache_origin(raw_url) == expected_origin


def test_in_place_migration_of_unpartitioned_epoch_and_sync_state_tables(tmp_path: Path) -> None:
    """Pre-existing unpartitioned record_cache + study_sync_state migrate without data loss."""
    db_path = tmp_path / "records_cache.sqlite3"
    r1 = Record(
        study_key="PROT-01",
        record_id=1,
        form_id=10,
        form_key="DEMOG",
        subject_key="SUBJ-01",
        date_modified="2025-05-01T10:00:00Z",
        record_data={"AGE": 51},
    )
    r2_draft = Record(
        study_key="PROT-01",
        record_id=2,
        form_id=10,
        form_key="DEMOG",
        subject_key="SUBJ-02",
        date_modified=None,
        record_data={"AGE": 34},
    )

    conn = get_sqlite_connection(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE record_cache (
                study_key TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                form_key TEXT NOT NULL,
                date_modified TEXT,
                payload TEXT NOT NULL,
                upserted_epoch INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (study_key, record_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE study_sync_state (
                study_key TEXT PRIMARY KEY,
                sync_epoch INTEGER NOT NULL DEFAULT 0,
                high_water_mark TEXT,
                initial_sync_complete INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'idle',
                last_synced_at TEXT,
                error_message TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO record_cache (study_key, record_id, form_key, date_modified, payload, upserted_epoch)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "PROT-01",
                    1,
                    "DEMOG",
                    "2025-05-01T10:00:00Z",
                    json.dumps(r1.model_dump(mode="json", by_alias=True), sort_keys=True),
                    4,
                ),
                (
                    "PROT-01",
                    2,
                    "DEMOG",
                    None,
                    json.dumps(r2_draft.model_dump(mode="json", by_alias=True), sort_keys=True),
                    4,
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO study_sync_state (
                study_key, sync_epoch, high_water_mark, initial_sync_complete, status, last_synced_at
            )
            VALUES (?, ?, ?, 1, 'synced', '2025-05-01T10:05:00+00:00')
            """,
            ("PROT-01", 4, "2025-05-01T10:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    prod_sdk = MagicMock()
    prod_sdk.base_url = "https://edc.prod.imednetapi.com/api/v1/edc/"
    prod_loader = CachedRecordsLoader(prod_sdk, cache_dir=tmp_path)

    assert prod_loader.db_path == db_path
    assert prod_loader.base_url == "https://edc.prod.imednetapi.com"
    assert [r.record_id for r in prod_loader.get_cached_records("PROT-01")] == [1, 2]
    migrated_state = prod_loader.get_study_sync_state("PROT-01")
    assert migrated_state.sync_epoch == 4
    assert migrated_state.high_water_mark == "2025-05-01T10:00:00Z"
    assert migrated_state.initial_sync_complete is True
    assert migrated_state.status == StudySyncStatus.SYNCED
    assert migrated_state.last_synced_at == "2025-05-01T10:05:00+00:00"

    # Opening the same migrated DB from training sees an empty, isolated partition
    trn_sdk = MagicMock()
    trn_sdk.base_url = "https://edc.trn.imednetapi.com"
    trn_loader = CachedRecordsLoader(trn_sdk, cache_dir=tmp_path)
    assert trn_loader.get_cached_records("PROT-01") == []
    assert trn_loader.get_study_sync_state("PROT-01").initial_sync_complete is False
    # And prod data remains completely intact
    assert [r.record_id for r in prod_loader.get_cached_records("PROT-01")] == [1, 2]


def test_multi_origin_isolation_across_loader_worker_profiler_and_hive_parquet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Training and production SDKs sharing DEFAULT_CACHE_DIR never cross-contaminate PROT-01."""
    shared_cache_dir = tmp_path / "default-cache"
    monkeypatch.setattr(cached_loader_mod, "DEFAULT_CACHE_DIR", shared_cache_dir)

    trn_records = [
        {
            "studyKey": "PROT-01",
            "recordId": 1,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectId": 11,
            "subjectKey": "TRN-SUBJ-01",
            "recordStatus": "Record Complete",
            "deleted": False,
            "dateModified": "2026-01-10T10:00:00Z",
            "recordData": {"AGE": 29, "COHORT": "TRAINING_ONLY"},
        }
    ]
    prod_records = [
        {
            "studyKey": "PROT-01",
            "recordId": 1,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectId": 99,
            "subjectKey": "PROD-SUBJ-01",
            "recordStatus": "Record Complete",
            "deleted": False,
            "dateModified": "2026-01-05T08:00:00Z",
            "recordData": {"AGE": 64, "COHORT": "PRODUCTION"},
        }
    ]

    trn_transport = _build_env_transport(study_key="PROT-01", records_ref=trn_records)
    prod_transport = _build_env_transport(study_key="PROT-01", records_ref=prod_records)

    with (
        ImednetSDK(
            api_key="trn-k",  # pragma: allowlist secret
            security_key="trn-s",  # pragma: allowlist secret
            base_url="https://EDC.TRN.IMEDNETAPI.COM:443/api/v1/edc/",
            transport=trn_transport,
        ) as trn_sdk,
        ImednetSDK(
            api_key="prod-k",  # pragma: allowlist secret
            security_key="prod-s",  # pragma: allowlist secret
            base_url="https://edc.prod.imednetapi.com",
            transport=prod_transport,
        ) as prod_sdk,
    ):
        assert resolve_sdk_base_url(trn_sdk) == "https://edc.trn.imednetapi.com"
        assert resolve_sdk_base_url(prod_sdk) == "https://edc.prod.imednetapi.com"

        # 1. Sync training via SyncWorker on the shared default cache file
        trn_loader = CachedRecordsLoader(trn_sdk)
        trn_worker = SyncWorker(
            trn_loader,
            config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=10, reconcile=True),
        )
        assert trn_worker.run_once() == 1
        assert [r.subject_key for r in trn_loader.load_records("PROT-01")] == ["TRN-SUBJ-01"]

        # 2. Export production to Hive Parquet (internally instantiating default CachedRecordsLoader(prod_sdk))
        hive_out = tmp_path / "hive-prod"
        export_to_hive_parquet(prod_sdk, "PROT-01", str(hive_out), mask_phi=False)
        parquet_files = sorted(hive_out.rglob("*.parquet"))
        assert len(parquet_files) == 1
        exported_df = pq.read_table(parquet_files[0]).to_pandas()
        assert list(exported_df["subjectKey"]) == ["PROD-SUBJ-01"]
        assert list(exported_df["COHORT"]) == ["PRODUCTION"]
        assert "TRN-SUBJ-01" not in set(exported_df["subjectKey"])

        # 3. Profile both origins via SchemaProfiler using default cache directory
        trn_profile = SchemaProfiler(trn_sdk).profile_records("PROT-01")
        prod_profile = SchemaProfiler(prod_sdk).profile_records("PROT-01")
        assert trn_profile["DEMOG"].record_count == 1
        assert prod_profile["DEMOG"].record_count == 1
        assert trn_profile["DEMOG"].fields["COHORT"].unique_values == ["TRAINING_ONLY"]
        assert prod_profile["DEMOG"].fields["COHORT"].unique_values == ["PRODUCTION"]

        # 4. Verify training cache was not overwritten or pruned by production reconciliation
        assert [r.subject_key for r in trn_loader.get_cached_records("PROT-01")] == ["TRN-SUBJ-01"]
        assert trn_loader.db_path == shared_cache_dir / "records_cache.sqlite3"


# ---------------------------------------------------------------------------
# Requirement 2 & 3: Null-Watermark Draft Completion & Atomic Voided Pruning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reconcile", [True, False])
def test_completing_draft_record_on_null_watermark_updates_payload_and_watermark(
    tmp_path: Path,
    reconcile: bool,
) -> None:
    """Completing a dateModified=None draft refreshes status, dateModified, and variables."""
    records_state: list[dict[str, Any]] = [
        {
            "studyKey": "STUDY-DRAFT",
            "recordId": 1,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectKey": "SUBJ-01",
            "recordStatus": "Record Incomplete",
            "deleted": False,
            "dateModified": None,
            "recordData": {"AGE": 40, "COHORT": "DRAFT_V1"},
        },
        {
            "studyKey": "STUDY-DRAFT",
            "recordId": 2,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectKey": "SUBJ-02",
            "recordStatus": "Record Incomplete",
            "deleted": False,
            "dateModified": None,
            "recordData": {"AGE": 35, "COHORT": "STILL_DRAFT"},
        },
    ]
    transport = _build_env_transport(study_key="STUDY-DRAFT", records_ref=records_state)

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.prod.imednetapi.com",
        transport=transport,
    ) as sdk:
        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / f"draft-{reconcile}")

        initial = loader.load_records("STUDY-DRAFT", reconcile=reconcile)
        assert [r.record_id for r in initial] == [1, 2]
        assert loader.get_study_sync_state("STUDY-DRAFT").high_water_mark is None
        assert loader.get_study_sync_state("STUDY-DRAFT").initial_sync_complete is True

        # Complete draft record 1 upstream; leave record 2 as an untimestamped draft
        records_state[0] = {
            **records_state[0],
            "recordStatus": "Record Complete",
            "dateModified": "2026-03-15T14:20:00Z",
            "recordData": {"AGE": 41, "COHORT": "COMPLETED_FINAL"},
        }

        refreshed = {
            r.record_id: r for r in loader.load_records("STUDY-DRAFT", reconcile=reconcile)
        }
        assert sorted(k for k in refreshed if k is not None) == [1, 2]
        assert refreshed[1].record_status == RecordStatus.RECORD_COMPLETE
        assert refreshed[1].date_modified is not None
        assert refreshed[1].record_data == {"AGE": 41, "COHORT": "COMPLETED_FINAL"}
        assert refreshed[2].record_data == {"AGE": 35, "COHORT": "STILL_DRAFT"}
        assert loader.get_study_sync_state("STUDY-DRAFT").high_water_mark is not None


@pytest.mark.parametrize("reconcile", [True, False])
def test_initial_load_excludes_voided_and_subsequent_void_prunes_without_leaks(
    tmp_path: Path,
    reconcile: bool,
) -> None:
    """Initial loads exclude deleted=True and voiding a cached Record prunes it atomically."""
    records_state: list[dict[str, Any]] = [
        {
            "studyKey": "STUDY-VOID",
            "recordId": 1,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectKey": "SUBJ-01",
            "recordStatus": "Record Incomplete",
            "deleted": False,
            "dateModified": None,
            "recordData": {"AGE": 50},
        },
        {
            "studyKey": "STUDY-VOID",
            "recordId": 2,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectKey": "SUBJ-02",
            "recordStatus": "Record Complete",
            "deleted": False,
            "dateModified": "2026-02-01T10:00:00Z",
            "recordData": {"AGE": 60},
        },
        {
            "studyKey": "STUDY-VOID",
            "recordId": 99,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectKey": "SUBJ-99",
            "recordStatus": "DELETED",
            "deleted": True,
            "dateModified": "2026-01-15T00:00:00Z",
            "recordData": {"AGE": 99},
        },
    ]
    transport = _build_env_transport(study_key="STUDY-VOID", records_ref=records_state)

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.prod.imednetapi.com",
        transport=transport,
    ) as sdk:
        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / f"void-{reconcile}")

        # Cycle 1: pre-voided record 99 is never cached
        initial = loader.load_records("STUDY-VOID", reconcile=reconcile)
        assert [r.record_id for r in initial] == [1, 2]

        # Complete draft 1 and void record 2
        records_state[0] = {
            **records_state[0],
            "recordStatus": "Record Complete",
            "dateModified": "2026-02-10T11:00:00Z",
            "recordData": {"AGE": 52},
        }
        records_state[1] = {
            **records_state[1],
            "recordStatus": "DELETED",
            "deleted": True,
            "dateModified": "2026-02-10T11:05:00Z",
        }

        after_void = loader.load_records("STUDY-VOID", reconcile=reconcile)
        assert [r.record_id for r in after_void] == [1]
        assert after_void[0].record_data == {"AGE": 52}
        assert all(not r.is_deleted for r in after_void)

        # Raw SQLite inspection confirms zero deleted=True rows exist on disk
        with get_sqlite_connection(loader.db_path) as conn:
            rows = conn.execute("SELECT record_id, payload FROM record_cache").fetchall()
        assert [int(r["record_id"]) for r in rows] == [1]
        assert json.loads(rows[0]["payload"]).get("deleted") is not True


def test_mid_sync_failures_rollback_atomically_without_deleted_leaks_and_keep_lock_under_50ms(
    tmp_path: Path,
) -> None:
    """Mid-sync exceptions roll back delta upserts/deletions, expose zero deleted=True, and hold lock <50ms."""
    records_state: list[dict[str, Any]] = [
        {
            "studyKey": "STUDY-ATOMIC",
            "recordId": 1,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectKey": "SUBJ-01",
            "recordStatus": "Record Complete",
            "deleted": False,
            "dateModified": "2026-01-01T00:00:00Z",
            "recordData": {"AGE": 30},
        },
        {
            "studyKey": "STUDY-ATOMIC",
            "recordId": 2,
            "formId": 10,
            "formKey": "DEMOG",
            "subjectKey": "SUBJ-02",
            "recordStatus": "Record Incomplete",
            "deleted": False,
            "dateModified": None,
            "recordData": {"AGE": 40},
        },
    ]
    transport = _build_env_transport(study_key="STUDY-ATOMIC", records_ref=records_state)

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.prod.imednetapi.com",
        transport=transport,
    ) as sdk:
        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
        loader.sync_records("STUDY-ATOMIC", reconcile=True)
        initial_hwm = loader.get_study_sync_state("STUDY-ATOMIC").high_water_mark
        assert initial_hwm == "2026-01-01T00:00:00Z"

        # Upstream voids record 1, completes draft 2, and adds active record 3
        records_state[0] = {
            **records_state[0],
            "recordStatus": "DELETED",
            "deleted": True,
            "dateModified": "2026-02-01T00:00:00Z",
        }
        records_state[1] = {
            **records_state[1],
            "recordStatus": "Record Complete",
            "dateModified": "2026-02-02T00:00:00Z",
            "recordData": {"AGE": 41},
        }
        records_state.append(
            {
                "studyKey": "STUDY-ATOMIC",
                "recordId": 3,
                "formId": 10,
                "formKey": "DEMOG",
                "subjectKey": "SUBJ-03",
                "recordStatus": "Record Complete",
                "deleted": False,
                "dateModified": "2026-02-03T00:00:00Z",
                "recordData": {"AGE": 55},
            }
        )

        # Simulate mid-commit failure inside reconcile_cache after delta returned voided record 1 + new record 3
        with (
            patch.object(
                loader, "reconcile_cache", side_effect=RuntimeError("simulated commit abort")
            ),
            pytest.raises(RuntimeError, match="simulated commit abort"),
        ):
            loader.sync_records("STUDY-ATOMIC", reconcile=True)

        # Atomic rollback: record 1 (pre-void active copy) and record 2 (draft copy) remain intact,
        # record 3 was NOT partially committed, zero deleted=True records exist, and HWM is unchanged!
        rolled_back = {r.record_id: r for r in loader.get_cached_records("STUDY-ATOMIC")}
        assert sorted(k for k in rolled_back if k is not None) == [1, 2]
        assert rolled_back[1].is_deleted is False
        assert rolled_back[2].record_data == {"AGE": 40}
        failed_state = loader.get_study_sync_state("STUDY-ATOMIC")
        assert failed_state.high_water_mark == initial_hwm
        assert failed_state.status == StudySyncStatus.FAILED

        # Now run a normal sync while concurrent reader threads inspect get_cached_records and measure lock hold
        stop_readers = threading.Event()
        deleted_leaks: list[int] = []
        reader_errors: list[Exception] = []

        def _reader() -> None:
            while not stop_readers.is_set():
                try:
                    snap = loader.get_cached_records("STUDY-ATOMIC")
                    for rec in snap:
                        if rec.is_deleted and rec.record_id is not None:
                            deleted_leaks.append(rec.record_id)
                except Exception as exc:  # pragma: no cover
                    reader_errors.append(exc)
                    break

        hold_times_ms: list[float] = []
        acquire_during_network = False
        in_network_call = False
        orig_list_records = loader._list_records

        def _tracked_list_records(**kw: Any) -> list[Record]:
            nonlocal acquire_during_network, in_network_call
            in_network_call = True
            try:
                if loader.lock.is_locked:
                    acquire_during_network = True
                return orig_list_records(**kw)
            finally:
                in_network_call = False

        orig_acquire = loader.lock.acquire
        orig_release = loader.lock.release
        lock_start: dict[int, float] = {}

        def _on_acquire(*a: Any, **kw: Any) -> Any:
            nonlocal acquire_during_network
            if in_network_call:
                acquire_during_network = True
            res = orig_acquire(*a, **kw)
            lock_start[threading.get_ident()] = time.perf_counter()
            return res

        def _on_release(*a: Any, **kw: Any) -> Any:
            started = lock_start.pop(threading.get_ident(), None)
            if started is not None:
                hold_times_ms.append((time.perf_counter() - started) * 1000.0)
            return orig_release(*a, **kw)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()
        try:
            with (
                patch.object(loader, "_list_records", side_effect=_tracked_list_records),
                patch.object(loader.lock, "acquire", side_effect=_on_acquire),
                patch.object(loader.lock, "release", side_effect=_on_release),
            ):
                loader.sync_records("STUDY-ATOMIC", reconcile=True)
        finally:
            stop_readers.set()
            reader_thread.join(timeout=5)

        assert reader_errors == []
        assert deleted_leaks == []
        assert acquire_during_network is False
        assert len(hold_times_ms) == 1
        assert hold_times_ms[0] < 50.0

        committed = {r.record_id: r for r in loader.get_cached_records("STUDY-ATOMIC")}
        assert sorted(k for k in committed if k is not None) == [2, 3]
        assert committed[2].record_data == {"AGE": 41}
        assert loader.get_study_sync_state("STUDY-ATOMIC").high_water_mark == "2026-02-03T00:00:00Z"


# ---------------------------------------------------------------------------
# Requirement 4: RecordMapper._iter_records Filter & Streaming Parity
# ---------------------------------------------------------------------------


def test_record_mapper_cached_matches_uncached_across_extra_filters_record_data_filter_deleted_and_sort(
    tmp_path: Path,
) -> None:
    """Cached RecordMapper matches uncached _stream_sdk_records on filters, deleted, and custom sort."""
    records_state: list[dict[str, Any]] = [
        {
            "studyKey": "STUDY-MAP",
            "recordId": 1,
            "formId": 10,
            "formKey": "DEMOG",
            "visitId": 100,
            "siteId": 1,
            "subjectKey": "SUBJ-01",
            "recordStatus": "Record Complete",
            "deleted": False,
            "dateModified": "2026-01-10T10:00:00Z",
            "recordData": {"AGE": 45, "COHORT": "Arm-Alpha", "AETERM": "Mild Headache"},
        },
        {
            "studyKey": "STUDY-MAP",
            "recordId": 2,
            "formId": 10,
            "formKey": "DEMOG",
            "visitId": 200,
            "siteId": 2,
            "subjectKey": "SUBJ-02",
            "recordStatus": "Record Incomplete",
            "deleted": False,
            "dateModified": "2026-01-12T10:00:00Z",
            "recordData": {"AGE": 28, "COHORT": "Arm-Beta", "AETERM": "Fatigue"},
        },
        {
            "studyKey": "STUDY-MAP",
            "recordId": 3,
            "formId": 20,
            "formKey": "AE",
            "visitId": 100,
            "siteId": 1,
            "subjectKey": "SUBJ-03",
            "recordStatus": "Record Complete",
            "deleted": False,
            "dateModified": "2026-01-15T10:00:00Z",
            "recordData": {"AGE": 62, "COHORT": "Arm-Alpha", "AETERM": "Severe Nausea"},
        },
        {
            "studyKey": "STUDY-MAP",
            "recordId": 4,
            "formId": 20,
            "formKey": "AE",
            "visitId": 100,
            "siteId": 1,
            "subjectKey": "SUBJ-01",
            "recordStatus": "DELETED",
            "deleted": True,
            "dateModified": "2026-01-20T10:00:00Z",
            "recordData": {"AGE": 45, "COHORT": "Arm-Alpha", "AETERM": "Voided Event"},
        },
    ]
    transport = _build_env_transport(study_key="STUDY-MAP", records_ref=records_state)

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.prod.imednetapi.com",
        transport=transport,
    ) as sdk:
        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
        uncached = RecordMapper(sdk)
        cached = RecordMapper(sdk, loader=loader)

        query_matrix: list[dict[str, Any]] = [
            {"extra_filters": {"subject_key": "SUBJ-01"}},
            {"extra_filters": {"subjectKey": ["SUBJ-01", "SUBJ-03"]}},
            {"extra_filters": {"formId": [10, 20], "site_id": 1}},
            {"extra_filters": {"record_status": "Record Complete", "site_id": 1}},
            {"extra_filters": {"date_modified": (">=", "2026-01-12T10:00:00Z")}},
            {"extra_filters": {"filter": "subjectKey==SUBJ-02,subjectKey==SUBJ-03"}},
            {"record_data_filter": "AGE>=40;COHORT==Arm-Alpha"},
            {"record_data_filter": "age>=40;cohort==Arm-Alpha"},
            {"record_data_filter": "AETERM=~headache,AETERM=~nausea"},
            {"record_data_filter": "none", "extra_filters": {"form_key": "DEMOG"}},
            {
                "visit_key": "100",
                "extra_filters": {"formId": [10, 20], "recordId": ("!=", 1)},
                "record_data_filter": "AGE>50",
            },
        ]

        for kwargs in query_matrix:
            uncached_items = list(uncached._iter_records("STUDY-MAP", **kwargs))
            cached_items = list(cached._iter_records("STUDY-MAP", **kwargs))
            assert [r.record_id for r in cached_items] == [r.record_id for r in uncached_items], (
                f"Mismatch for {kwargs}"
            )
            assert [r.record_data for r in cached_items] == [
                r.record_data for r in uncached_items
            ], f"record_data mismatch for {kwargs}"

        # Invalid filter syntax or unsupported attributes raise ValueError/ConfigurationError identically
        for bad_kwargs in (
            {"record_data_filter": "AGE invalid syntax"},
            {"extra_filters": {"subject_key": ("=~", "SUBJ")}},
            {"extra_filters": {"subjectKey": ["SUBJ-01", "SUBJ-03"], "formId": [10, 20]}},
            {"extra_filters": {"unknownAttribute": "value"}},
            {"extra_filters": {"filter": "unknownAttr==1"}},
        ):
            with pytest.raises(ValueError):
                list(uncached._iter_records("STUDY-MAP", **bad_kwargs))
            with pytest.raises(ValueError):
                list(cached._iter_records("STUDY-MAP", **bad_kwargs))

        # include_deleted=True streams directly from SDK without invoking loader.sync_records
        with patch.object(loader, "sync_records", wraps=loader.sync_records) as sync_spy:
            cached_with_deleted = list(
                cached._iter_records(
                    "STUDY-MAP",
                    extra_filters={"subject_key": "SUBJ-01"},
                    include_deleted=True,
                )
            )
            uncached_with_deleted = list(
                uncached._iter_records(
                    "STUDY-MAP",
                    extra_filters={"subject_key": "SUBJ-01"},
                    include_deleted=True,
                )
            )
            assert sync_spy.call_count == 0
            assert (
                [r.record_id for r in cached_with_deleted]
                == [r.record_id for r in uncached_with_deleted]
                == [1, 4]
            )

            # Custom sort also streams directly from SDK without invoking loader.sync_records
            cached_desc = list(cached._iter_records("STUDY-MAP", sort="dateModified,DESC"))
            uncached_desc = list(uncached._iter_records("STUDY-MAP", sort="dateModified,DESC"))
            assert sync_spy.call_count == 0
            assert (
                [r.record_id for r in cached_desc]
                == [r.record_id for r in uncached_desc]
                == [3, 2, 1]
            )


# ---------------------------------------------------------------------------
# Requirement 5 & 6: Retry Ownership, Permanent 4xx, and SyncWorker Hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409])
def test_permanent_4xx_executes_once_without_retry_and_400_401_keep_sync_worker_failed(
    tmp_path: Path,
    status_code: int,
) -> None:
    """Permanent 4xx errors execute strictly once; 400 and 401 keep SyncWorker permanently FAILED."""
    http_calls = 0

    def _error_handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(status_code, json={"message": f"permanent {status_code}"})

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.prod.imednetapi.com",
        transport=httpx.MockTransport(_error_handler),
        retry_config=RetryConfig(retries=3, backoff_factor=0.0),
    ) as sdk:
        loader = CachedRecordsLoader(
            sdk,
            cache_dir=tmp_path / f"status-{status_code}",
            retry_attempts=4,
        )
        worker = SyncWorker(
            loader,
            config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=5),
        )

        expected_exc = {
            400: BadRequestError,
            401: AuthenticationError,
            403: AuthorizationError,
            404: NotFoundError,
            409: ConflictError,
        }[status_code]

        with pytest.raises(expected_exc):
            worker.run_once()

        assert http_calls == 1

        if status_code in (400, 401, 403, 404):
            assert worker.state == SyncWorkerState.FAILED
            # Later run_once and run_forever calls preserve terminal FAILED state without extra HTTP calls
            with pytest.raises(expected_exc):
                worker.run_once()
            assert worker.state == SyncWorkerState.FAILED
            assert http_calls == 1

            with pytest.raises(expected_exc):
                worker.run_forever()
            assert worker.state == SyncWorkerState.FAILED
            assert http_calls == 1


def test_http_503_delegates_to_sdk_default_retry_policy_without_outer_multiplication(
    tmp_path: Path,
) -> None:
    """HTTP 503 under DefaultRetryPolicy executes 1 + retries attempts without loader multiplication."""
    http_calls = 0

    def _503_handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(503, json={"message": "service unavailable"})

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.prod.imednetapi.com",
        transport=httpx.MockTransport(_503_handler),
        retry_config=RetryConfig(retries=2, backoff_factor=0.0),
    ) as sdk:
        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path, retry_attempts=4)
        with pytest.raises(ServerError):
            loader.sync_records("PROT-01", reconcile=True)

    # RetryConfig(retries=2) => 1 initial + 2 retries = 3 total (not 4 * 3 = 12)
    assert http_calls == 3


def test_facade_without_sdk_retry_policy_skips_4xx_and_validation_errors(tmp_path: Path) -> None:
    """Lightweight facade callers never retry permanent 4xx or client validation errors."""
    for non_retryable in (
        BadRequestError("bad filter", status_code=400),
        AuthenticationError("bad key", status_code=401),
        ConfigurationError("invalid config"),
        ValueError("invalid parameter"),
    ):
        sdk = MagicMock()
        sdk.get_records.side_effect = non_retryable
        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path, retry_attempts=3)
        with pytest.raises(type(non_retryable)):
            loader.sync_records("PROT-01", reconcile=False)
        assert sdk.get_records.call_count == 1


def test_sync_worker_preserves_terminal_failed_state_after_max_consecutive_failures(
    tmp_path: Path,
) -> None:
    """Once max_consecutive_failures sets FAILED, later run_once calls keep FAILED and do not re-sync."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    original_timeout = 30
    loader.lock.timeout = original_timeout
    loader.sync_records.side_effect = [
        RuntimeError("transient 1"),
        RuntimeError("transient 2"),
        None,
    ]
    loader.count_cached_records.return_value = 5

    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(
            study_key="PROT-01",
            interval_seconds=1,
            lock_timeout_seconds=12.5,
            max_consecutive_failures=2,
        ),
    )
    assert loader.lock.timeout == original_timeout

    with pytest.raises(RuntimeError, match="transient 1"):
        worker.run_once()
    assert worker.state == SyncWorkerState.DEGRADED

    with pytest.raises(RuntimeError, match="transient 2"):
        worker.run_once()
    assert worker.state == SyncWorkerState.FAILED

    # Subsequent run_once must keep FAILED and never invoke sync_records a 3rd time
    with pytest.raises(RuntimeError, match="transient 2"):
        worker.run_once()
    assert worker.state == SyncWorkerState.FAILED
    assert loader.sync_records.call_count == 2
    assert loader.lock.timeout == original_timeout


@pytest.mark.parametrize("bad_study_key", ["", "   ", "\t\n", None, 123])
def test_sync_worker_config_and_worker_reject_blank_study_key(
    tmp_path: Path,
    bad_study_key: Any,
) -> None:
    """SyncWorkerConfig and SyncWorker reject blank or non-string study_key."""
    with pytest.raises(ValueError, match="study_key"):
        SyncWorkerConfig(study_key=bad_study_key)

    valid_cfg = SyncWorkerConfig(study_key="PROT-01")
    object.__setattr__(valid_cfg, "study_key", bad_study_key)
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    with pytest.raises(ValueError, match="study_key"):
        SyncWorker(loader, config=valid_cfg)


@pytest.mark.parametrize("bad_max_failures", [True, False, 0, -2, 1.5])
def test_sync_worker_config_and_worker_reject_boolean_and_invalid_max_consecutive_failures(
    tmp_path: Path,
    bad_max_failures: Any,
) -> None:
    """SyncWorkerConfig and SyncWorker reject boolean and <1 max_consecutive_failures."""
    with pytest.raises(ValueError, match="max_consecutive_failures must be at least 1"):
        SyncWorkerConfig(study_key="PROT-01", max_consecutive_failures=bad_max_failures)

    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    with pytest.raises(ValueError, match="max_consecutive_failures must be at least 1"):
        SyncWorker(
            loader,
            config=SyncWorkerConfig(study_key="PROT-01"),
            max_consecutive_failures=bad_max_failures,
        )


@pytest.mark.parametrize(
    "bad_lock_timeout",
    [0, -1, -0.5, True, False, float("nan"), float("inf"), float("-inf"), "30"],
)
def test_sync_worker_config_and_worker_reject_invalid_lock_timeout_seconds(
    tmp_path: Path,
    bad_lock_timeout: Any,
) -> None:
    """SyncWorkerConfig and SyncWorker reject non-positive, boolean, or non-finite lock_timeout_seconds."""
    with pytest.raises(ValueError, match="lock_timeout_seconds must be greater than 0"):
        SyncWorkerConfig(study_key="PROT-01", lock_timeout_seconds=bad_lock_timeout)

    valid_cfg = SyncWorkerConfig(study_key="PROT-01")
    object.__setattr__(valid_cfg, "lock_timeout_seconds", bad_lock_timeout)
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    with pytest.raises(ValueError, match="lock_timeout_seconds must be greater than 0"):
        SyncWorker(loader, config=valid_cfg)
