"""Unit tests for cached records loader."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from imednet.models.records import Record
from imednet.spi.utils import get_sqlite_connection
from imednet_workflows.cached_loader import CachedRecordsLoader


def _record(record_id: int, modified_at: str) -> Record:
    """Helper function to  record."""
    return Record(
        study_key="STUDY",
        form_id=10,
        form_key="FORM",
        record_id=record_id,
        subject_key=f"S{record_id}",
        date_modified=datetime.fromisoformat(modified_at),
        record_data={"value": record_id},
    )


def test_get_sqlite_connection_enables_wal_mode(tmp_path: Path) -> None:
    """Test that get cache connection enables wal mode."""
    conn = get_sqlite_connection(tmp_path / "cache" / "records.sqlite3")
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "wal"
    finally:
        conn.close()


def test_cached_loader_applies_delta_sync_and_reconciliation(tmp_path: Path) -> None:
    """Test that cached loader applies delta sync and reconciliation."""
    sdk = MagicMock()
    first_batch = [
        _record(1, "2024-01-01 00:00:00+00:00"),
        _record(2, "2024-01-02 00:00:00+00:00"),
    ]
    second_batch = [_record(3, "2024-01-03 00:00:00+00:00")]

    sdk.get_records.side_effect = [
        first_batch,
        first_batch,
        [first_batch[1], second_batch[0]],
    ]
    sdk.records.list.side_effect = [second_batch]

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "custom-cache")

    first_load = loader.load_records("STUDY")
    second_load = loader.load_records("STUDY")

    assert loader.db_path.parent == tmp_path / "custom-cache"
    assert [record.record_id for record in first_load] == [1, 2]
    assert [record.record_id for record in second_load] == [2, 3]

    first_delta_call = sdk.get_records.call_args_list[0]
    second_delta_call = sdk.records.list.call_args_list[0]
    assert first_delta_call.kwargs == {"study_key": "STUDY", "record_data_filter": None}
    assert second_delta_call.kwargs == {
        "study_key": "STUDY",
        "filter": 'dateModified>="2024-01-02 00:00:00+00:00"',
        "record_data_filter": None,
    }
    assert sdk.get_records.call_args_list[2].kwargs == {
        "study_key": "STUDY",
        "record_data_filter": "none",
        "deleted": False,
    }


def test_cached_loader_retries_record_fetches(tmp_path: Path) -> None:
    """Test that cached loader retries record fetches."""
    sdk = MagicMock()
    record = _record(1, "2024-01-01 00:00:00+00:00")
    sdk.get_records.side_effect = [RuntimeError("temporary"), [record], [record]]

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path, retry_attempts=2)

    records = loader.load_records("STUDY")

    assert [item.record_id for item in records] == [1]
    assert sdk.get_records.call_count == 3


def test_iter_cached_records_yields_chunked_rows(tmp_path: Path) -> None:
    """Test that iter cached records yields chunked rows."""
    sdk = MagicMock()
    sdk.get_records.side_effect = [
        [_record(1, "2024-01-01 00:00:00+00:00"), _record(2, "2024-01-02 00:00:00+00:00")],
        [_record(1, "2024-01-01 00:00:00+00:00"), _record(2, "2024-01-02 00:00:00+00:00")],
    ]
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    loader.load_records("STUDY")
    records = list(loader.iter_cached_records("STUDY", chunk_size=1))

    assert [record.record_id for record in records] == [1, 2]


def test_iter_cached_records_rejects_non_positive_chunk_size(tmp_path: Path) -> None:
    """Test that iter cached records rejects non positive chunk size."""
    loader = CachedRecordsLoader(MagicMock(), cache_dir=tmp_path)

    with pytest.raises(ValueError, match="chunk_size must be greater than zero"):
        next(loader.iter_cached_records("STUDY", chunk_size=0))


def test_sync_records_updates_cache_without_loading_rows(tmp_path: Path) -> None:
    """Test that sync records updates cache without loading rows."""
    sdk = MagicMock()
    sdk.get_records.side_effect = [
        [_record(1, "2024-01-01 00:00:00+00:00")],
        [_record(1, "2024-01-01 00:00:00+00:00")],
    ]
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    loader.sync_records("STUDY")

    with get_sqlite_connection(loader.db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM record_cache WHERE study_key = ?",
            ("STUDY",),
        ).fetchone()

    assert row is not None
    assert row[0] == 1


def test_sync_records_handles_empty_delta_without_reconciliation(tmp_path: Path) -> None:
    """Test that sync records handles empty delta without reconciliation."""
    sdk = MagicMock()
    sdk.get_records.side_effect = [
        [_record(1, "2024-01-01 00:00:00+00:00")],
    ]
    sdk.records.list.side_effect = [[]]
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    loader.sync_records("STUDY", reconcile=False)
    loader.sync_records("STUDY", reconcile=False)

    assert sdk.get_records.call_count == 1
    assert sdk.records.list.call_count == 1
    assert sdk.records.list.call_args_list[0].kwargs == {
        "study_key": "STUDY",
        "filter": 'dateModified>="2024-01-01 00:00:00+00:00"',
        "record_data_filter": None,
    }


def test_count_cached_records_empty_cache(tmp_path: Path) -> None:
    """Test that count_cached_records returns 0 when cache is empty."""
    sdk = MagicMock()
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    assert loader.count_cached_records("STUDY") == 0


def test_count_cached_records_counts_matching_study(tmp_path: Path) -> None:
    """Test that count_cached_records returns accurate count for the requested study."""
    sdk = MagicMock()
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    record_study_a = _record(1, "2024-01-01 00:00:00+00:00")
    record_study_a_2 = _record(2, "2024-01-02 00:00:00+00:00")
    record_study_b = Record(
        study_key="OTHER",
        form_id=10,
        form_key="FORM",
        record_id=1,
        subject_key="S1",
        date_modified=datetime.fromisoformat("2024-01-01 00:00:00+00:00"),
        record_data={"value": 1},
    )

    with get_sqlite_connection(loader.db_path) as conn:
        loader._upsert_records(conn, [record_study_a, record_study_a_2, record_study_b])

    assert loader.count_cached_records("STUDY") == 2
    assert loader.count_cached_records("OTHER") == 1
    assert loader.count_cached_records("NONEXISTENT") == 0

    with get_sqlite_connection(loader.db_path) as conn:
        assert loader.count_cached_records("STUDY", conn=conn) == 2


def test_fetch_active_record_ids_uses_lightweight_projection_without_record_data(
    tmp_path: Path,
) -> None:
    """Verify that _fetch_active_record_ids requests record_data_filter="none" and extracts IDs."""
    sdk = MagicMock()
    # Projected record models returned by API omit clinical question data (record_data is None)
    projected_records = [
        Record(study_key="STUDY", record_id=101, form_key="DEMOG", record_data=None),
        Record(study_key="STUDY", record_id=102, form_key="VITALS", record_data=None),
        Record(study_key="STUDY", record_id=None, form_key="EMPTY", record_data=None),
    ]
    sdk.get_records.return_value = projected_records

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
    active_ids = loader._fetch_active_record_ids("STUDY")

    assert active_ids == {101, 102}
    sdk.get_records.assert_called_once_with(
        study_key="STUDY",
        record_data_filter="none",
        deleted=False,
    )


def test_reconcile_cache_prunes_deleted_records_with_projected_id_set(tmp_path: Path) -> None:
    """Verify reconcile_cache accurately prunes orphaned records when supplied projected IDs."""
    sdk = MagicMock()
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    r1 = _record(1, "2024-01-01 00:00:00+00:00")
    r2 = _record(2, "2024-01-02 00:00:00+00:00")
    r3 = _record(3, "2024-01-03 00:00:00+00:00")

    with get_sqlite_connection(loader.db_path) as conn:
        loader._upsert_records(conn, [r1, r2, r3])
        assert loader.count_cached_records("STUDY", conn=conn) == 3

        # Projected active IDs set indicates record 2 was deleted upstream in EDC
        projected_active_ids = {1, 3}
        loader.reconcile_cache(conn, "STUDY", projected_active_ids)

        remaining = [
            row["record_id"]
            for row in conn.execute(
                "SELECT record_id FROM record_cache WHERE study_key = ? ORDER BY record_id",
                ("STUDY",),
            ).fetchall()
        ]
        assert remaining == [1, 3]


def test_cache_sync_minimal_network_overhead_with_lightweight_projection(tmp_path: Path) -> None:
    """Verify mock network transfer overhead is reduced by >90% when omitting clinical form data."""
    import json

    sdk = MagicMock()
    # Create large clinical form payloads with multiple questions
    clinical_form_data = {f"QUESTION_{i}": f"clinical_response_value_{i}" * 5 for i in range(100)}
    full_payloads = [
        Record(
            study_key="STUDY",
            record_id=i,
            form_key="FORM",
            record_data=clinical_form_data,
        ).model_dump(mode="json", by_alias=True)
        for i in range(50)
    ]
    projected_payloads = [
        Record(
            study_key="STUDY",
            record_id=i,
            form_key="FORM",
            record_data=None,
        ).model_dump(mode="json", by_alias=True)
        for i in range(50)
    ]

    full_bytes = len(json.dumps(full_payloads).encode("utf-8"))
    projected_bytes = len(json.dumps(projected_payloads).encode("utf-8"))

    # Verify lightweight projection achieves >90% reduction in network payload volume
    payload_reduction = (full_bytes - projected_bytes) / full_bytes
    assert payload_reduction > 0.90, f"Payload reduction was only {payload_reduction:.2%}"

    # Verify loader uses projected records during sync reconciliation
    sdk.get_records.side_effect = [
        [Record(**p) for p in full_payloads],  # Initial delta sync
        [Record(**p) for p in projected_payloads],  # Reconciliation with record_data_filter="none"
    ]

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
    loader.sync_records("STUDY", reconcile=True)

    reconcile_call = sdk.get_records.call_args_list[1]
    assert reconcile_call.kwargs["record_data_filter"] == "none"
    assert loader.count_cached_records("STUDY") == 50


def test_remote_fetches_execute_outside_file_lock(tmp_path: Path) -> None:
    """Verify that remote delta and active record ID fetches run outside the file lock."""
    from filelock import FileLock

    sdk = MagicMock()
    r1 = _record(1, "2024-01-01 00:00:00+00:00")
    projected = [Record(study_key="STUDY", record_id=1, form_key="FORM", record_data=None)]

    lock_held_during_delta = None
    external_can_acquire_during_delta = None
    lock_held_during_reconcile = None
    external_can_acquire_during_reconcile = None
    call_count = 0

    def _mock_fetch(*args, **kwargs):
        nonlocal call_count, lock_held_during_delta, external_can_acquire_during_delta
        nonlocal lock_held_during_reconcile, external_can_acquire_during_reconcile
        call_count += 1
        if call_count == 1:
            lock_held_during_delta = loader.lock.is_locked
            external_lock = FileLock(str(loader.db_path) + ".lock", timeout=0)
            try:
                with external_lock:
                    external_can_acquire_during_delta = True
            except Exception:
                external_can_acquire_during_delta = False
            return [r1]
        lock_held_during_reconcile = loader.lock.is_locked
        external_lock = FileLock(str(loader.db_path) + ".lock", timeout=0)
        try:
            with external_lock:
                external_can_acquire_during_reconcile = True
        except Exception:
            external_can_acquire_during_reconcile = False
        return projected

    sdk.get_records.side_effect = _mock_fetch

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
    loader.sync_records("STUDY", reconcile=True)

    assert lock_held_during_delta is False
    assert external_can_acquire_during_delta is True
    assert lock_held_during_reconcile is False
    assert external_can_acquire_during_reconcile is True


def test_write_transactions_acquire_file_lock(tmp_path: Path) -> None:
    """Verify that _upsert_records and reconcile_cache acquire file lock during execution."""
    from unittest.mock import patch

    sdk = MagicMock()
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    lock_states_in_upsert = []
    original_acquire = loader.lock.acquire

    def _spy_acquire(*args, **kwargs):
        res = original_acquire(*args, **kwargs)
        lock_states_in_upsert.append(loader.lock.is_locked)
        return res

    with patch.object(loader.lock, "acquire", side_effect=_spy_acquire):
        with get_sqlite_connection(loader.db_path) as conn:
            loader._upsert_records(conn, [_record(1, "2024-01-01 00:00:00+00:00")])

    assert lock_states_in_upsert == [True]
    assert loader.lock.is_locked is False

    lock_states_in_reconcile = []

    def _spy_acquire_reconcile(*args, **kwargs):
        res = original_acquire(*args, **kwargs)
        lock_states_in_reconcile.append(loader.lock.is_locked)
        return res

    with patch.object(loader.lock, "acquire", side_effect=_spy_acquire_reconcile):
        with get_sqlite_connection(loader.db_path) as conn:
            loader.reconcile_cache(conn, "STUDY", set())  # record 1 is orphaned

    assert lock_states_in_reconcile == [True]
    assert loader.lock.is_locked is False


def test_lock_hold_duration_below_threshold(tmp_path: Path) -> None:
    """Verify that file lock hold duration is well below 50ms for typical batch upsert operations."""
    import time
    from unittest.mock import patch

    sdk = MagicMock()
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    records = [_record(i, f"2024-01-{(i % 28) + 1:02d} 00:00:00+00:00") for i in range(1, 101)]

    hold_durations = []
    original_acquire = loader.lock.acquire
    original_release = loader.lock.release

    start_time = 0.0

    def _tracking_acquire(*args, **kwargs):
        nonlocal start_time
        res = original_acquire(*args, **kwargs)
        start_time = time.perf_counter()
        return res

    def _tracking_release(*args, **kwargs):
        nonlocal start_time
        duration = time.perf_counter() - start_time
        hold_durations.append(duration)
        return original_release(*args, **kwargs)

    with (
        patch.object(loader.lock, "acquire", side_effect=_tracking_acquire),
        patch.object(loader.lock, "release", side_effect=_tracking_release),
    ):
        with get_sqlite_connection(loader.db_path) as conn:
            loader._upsert_records(conn, records)

    assert len(hold_durations) == 1
    # 50 milliseconds = 0.05 seconds
    assert hold_durations[0] < 0.05, f"Lock held for {hold_durations[0]:.4f}s which exceeds 50ms"


def test_cached_loader_http_contract_iso_timestamp_and_lowercase_bool(tmp_path: Path) -> None:
    """Verify CachedRecordsLoader emits top-level unquoted UTC filter and deleted==false."""
    import httpx
    import respx

    from imednet.sdk import ImednetSDK

    with respx.mock:
        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        calls: list[dict[str, str]] = []

        def responder(request: httpx.Request) -> httpx.Response:
            calls.append(dict(request.url.params))
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "studyKey": "STUDY",
                                "recordId": 1,
                                "formId": 10,
                                "formKey": "DEMOG",
                                "subjectKey": "SUB1",
                                "dateModified": "2024-01-02T00:00:00Z",
                                "recordData": {"AGE": 42},
                            }
                        ]
                    },
                )
            if len(calls) == 2:
                return httpx.Response(
                    200,
                    json={"data": [{"studyKey": "STUDY", "recordId": 1, "formKey": "DEMOG"}]},
                )
            return httpx.Response(200, json={"data": []})

        respx.get("https://api.test/api/v1/edc/studies/STUDY/records").mock(side_effect=responder)

        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "http-cache")
        loader.sync_records("STUDY", reconcile=True)
        loader.sync_records("STUDY", reconcile=False)

        assert "filter" not in calls[0]
        assert calls[1]["filter"] == "deleted==false"
        assert calls[1]["recordDataFilter"] == "none"
        assert calls[2]["filter"] == "dateModified>=2024-01-02T00:00:00Z"


def test_cached_loader_incremental_sync_with_simulation_transport(tmp_path: Path) -> None:
    """Verify CachedRecordsLoader fetches and caches modified and created records via SimulationTransport."""
    import httpx

    from imednet.sdk import ImednetSDK
    from imednet.simulation import SimulationState, SimulationTransport

    state = SimulationState()
    transport = SimulationTransport(state=state)
    recorded_get_params: list[dict[str, str]] = []
    original_handle = transport.handle_request

    def spy_handle_request(request: httpx.Request) -> httpx.Response:
        if request.method.upper() == "GET":
            recorded_get_params.append(dict(request.url.params))
        return original_handle(request)

    transport.handle_request = spy_handle_request  # type: ignore[method-assign]
    sdk = ImednetSDK(simulation_mode=True, transport=transport)
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "sim-cache")

    initial_records = loader.load_records("STUDY")
    assert [r.record_id for r in initial_records] == [601, 602]
    assert initial_records[1].record_data == {"V_WEIGHT": 68.5}

    # Modify existing record 602 and create a new record after the initial high-water mark
    sdk.records.create(
        "STUDY",
        [
            {
                "recordId": 602,
                "subjectKey": "SUB1",
                "formKey": "F_VS",
                "data": {"V_WEIGHT": 71.0},
                "dateModified": "2026-11-15T12:00:00Z",
            },
            {
                "subjectKey": "SUB1",
                "formKey": "F_VS",
                "data": {"V_WEIGHT": 72.5},
                "dateModified": "2026-11-16T08:00:00Z",
            },
        ],
    )

    recorded_get_params.clear()
    refreshed_records = loader.load_records("STUDY")

    # Delta request must send top-level timestamp filter without studyKey
    assert recorded_get_params[0]["filter"] == "dateModified>=2026-01-10T09:35:00Z"
    assert "studyKey" not in recorded_get_params[0]["filter"]
    assert recorded_get_params[1]["filter"] == "deleted==false"
    assert recorded_get_params[1]["recordDataFilter"] == "none"

    by_id = {r.record_id: r for r in refreshed_records}
    assert sorted(k for k in by_id if k is not None) == [601, 602, 1001]
    assert by_id[601].record_data == {"V_AGE": 45, "V_GENDER": "Female"}
    assert by_id[602].record_data == {"V_WEIGHT": 71.0}
    assert by_id[1001].record_data == {"V_WEIGHT": 72.5}


def _null_timestamp_record(record_id: int, study_key: str = "STUDY") -> Record:
    """Create a draft Record instance whose date_modified is None."""
    return Record(
        study_key=study_key,
        form_id=10,
        form_key="DRAFT_FORM",
        record_id=record_id,
        subject_key=f"S{record_id}",
        date_modified=None,
        record_data={"draft_value": record_id},
    )


def test_date_modified_none_stored_as_sql_null_and_sync_state_tracked(tmp_path: Path) -> None:
    """Verify date_modified=None is stored as SQL NULL and study_sync_state tracks epoch/watermark."""
    from imednet_workflows.cached_loader import StudySyncStatus

    sdk = MagicMock()
    r1 = _null_timestamp_record(1)
    r2 = _record(2, "2024-02-01 00:00:00+00:00")
    projected = [
        Record(study_key="STUDY", record_id=1, form_key="DRAFT_FORM", record_data=None),
        Record(study_key="STUDY", record_id=2, form_key="FORM", record_data=None),
    ]
    sdk.get_records.side_effect = [[r1, r2], projected]

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
    loader.sync_records("STUDY", reconcile=True)

    with get_sqlite_connection(loader.db_path) as conn:
        rows = conn.execute(
            """
            SELECT record_id, date_modified, upserted_epoch
            FROM record_cache
            WHERE study_key = ?
            ORDER BY record_id
            """,
            ("STUDY",),
        ).fetchall()

    assert rows[0]["record_id"] == 1
    assert rows[0]["date_modified"] is None
    assert rows[0]["upserted_epoch"] == 1
    assert rows[1]["record_id"] == 2
    assert rows[1]["date_modified"] == "2024-02-01 00:00:00+00:00"
    assert rows[1]["upserted_epoch"] == 1

    state = loader.get_study_sync_state("STUDY")
    assert state.sync_epoch == 1
    assert state.high_water_mark == "2024-02-01 00:00:00+00:00"
    assert state.initial_sync_complete is True
    assert state.status == StudySyncStatus.SYNCED
    assert state.last_synced_at is not None
    assert state.error_message is None


def test_study_with_only_null_timestamps_uses_lightweight_reconcile_after_initial_load(
    tmp_path: Path,
) -> None:
    """Study with only date_modified=None runs one full load, then lightweight reconcile on later cycles."""
    sdk = MagicMock()
    r1 = _null_timestamp_record(1)
    r2 = _null_timestamp_record(2)
    r3 = _null_timestamp_record(3)

    proj_cycle_1 = [
        Record(study_key="STUDY", record_id=1, form_key="DRAFT_FORM", record_data=None),
        Record(study_key="STUDY", record_id=2, form_key="DRAFT_FORM", record_data=None),
    ]
    # Cycle 2: record 1 deleted upstream, record 3 created with date_modified=None
    proj_cycle_2 = [
        Record(study_key="STUDY", record_id=2, form_key="DRAFT_FORM", record_data=None),
        Record(study_key="STUDY", record_id=3, form_key="DRAFT_FORM", record_data=None),
    ]
    proj_cycle_3 = proj_cycle_2

    sdk.get_records.side_effect = [
        [r1, r2],  # Cycle 1: initial full load (record_data_filter=None)
        proj_cycle_1,  # Cycle 1: active ID projection (record_data_filter="none")
        proj_cycle_2,  # Cycle 2: lightweight active ID projection only (NO unfiltered full load!)
        [r3],  # Cycle 2: targeted backfill for missing record_id=[3]
        proj_cycle_3,  # Cycle 3: lightweight active ID projection only, zero missing IDs
    ]

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    loader.sync_records("STUDY", reconcile=True)
    assert [r.record_id for r in loader.get_cached_records("STUDY")] == [1, 2]
    assert loader.get_study_sync_state("STUDY").high_water_mark is None

    loader.sync_records("STUDY", reconcile=True)
    assert [r.record_id for r in loader.get_cached_records("STUDY")] == [2, 3]

    loader.sync_records("STUDY", reconcile=True)
    assert [r.record_id for r in loader.get_cached_records("STUDY")] == [2, 3]

    assert sdk.get_records.call_count == 5
    assert sdk.records.list.call_count == 0
    # Unfiltered full study load happened strictly once (call 0)
    assert sdk.get_records.call_args_list[0].kwargs == {
        "study_key": "STUDY",
        "record_data_filter": None,
    }
    assert sdk.get_records.call_args_list[2].kwargs == {
        "study_key": "STUDY",
        "record_data_filter": "none",
        "deleted": False,
    }
    assert sdk.get_records.call_args_list[3].kwargs == {
        "study_key": "STUDY",
        "record_id": [3],
        "record_data_filter": None,
    }
    assert sdk.get_records.call_args_list[4].kwargs == {
        "study_key": "STUDY",
        "record_data_filter": "none",
        "deleted": False,
    }


def test_timestamped_study_backfills_new_null_timestamp_record_during_reconciliation(
    tmp_path: Path,
) -> None:
    """Timestamped study ingests newly created date_modified=None records found during reconciliation."""
    sdk = MagicMock()
    r1 = _record(1, "2024-01-01 00:00:00+00:00")
    r2 = _record(2, "2024-01-02 00:00:00+00:00")
    r3_draft = _null_timestamp_record(3)
    r4_stamped = _record(4, "2024-01-05 00:00:00+00:00")

    sdk.get_records.side_effect = [
        [r1, r2],  # Cycle 1 full load
        [
            Record(study_key="STUDY", record_id=1, form_key="FORM", record_data=None),
            Record(study_key="STUDY", record_id=2, form_key="FORM", record_data=None),
        ],  # Cycle 1 active IDs
        [
            Record(study_key="STUDY", record_id=1, form_key="FORM", record_data=None),
            Record(study_key="STUDY", record_id=2, form_key="FORM", record_data=None),
            Record(study_key="STUDY", record_id=3, form_key="DRAFT_FORM", record_data=None),
            Record(study_key="STUDY", record_id=4, form_key="FORM", record_data=None),
        ],  # Cycle 2 active IDs include untimestamped record 3
        [r3_draft],  # Cycle 2 backfill for missing record 3
    ]
    # Delta query with dateModified >= 2024-01-02 returns only timestamped r4_stamped, omitting r3_draft
    sdk.records.list.side_effect = [[r4_stamped]]

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
    loader.sync_records("STUDY", reconcile=True)
    loader.sync_records("STUDY", reconcile=True)

    cached = {r.record_id: r for r in loader.get_cached_records("STUDY")}
    assert sorted(k for k in cached if k is not None) == [1, 2, 3, 4]
    assert cached[3].record_data == {"draft_value": 3}
    assert cached[3].date_modified is None

    state = loader.get_study_sync_state("STUDY")
    assert state.high_water_mark == "2024-01-05 00:00:00+00:00"
    assert state.sync_epoch == 3


def test_sync_records_preserves_high_water_mark_when_any_step_fails(tmp_path: Path) -> None:
    """Watermark remains at previous value when delta, active-ID, backfill, or reconcile fails."""
    from imednet_workflows.cached_loader import StudySyncStatus

    sdk = MagicMock()
    r1 = _record(1, "2024-01-02 00:00:00+00:00")
    r2_newer = _record(2, "2024-01-10 00:00:00+00:00")

    sdk.get_records.side_effect = [
        [r1],
        [Record(study_key="STUDY", record_id=1, form_key="FORM", record_data=None)],
    ]
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path, retry_attempts=1)
    loader.sync_records("STUDY", reconcile=True)
    assert loader.get_study_sync_state("STUDY").high_water_mark == "2024-01-02 00:00:00+00:00"

    # 1. Delta fetch failure preserves high_water_mark
    sdk.records.list.side_effect = RuntimeError("delta network error")
    with pytest.raises(RuntimeError, match="delta network error"):
        loader.sync_records("STUDY", reconcile=True)
    state = loader.get_study_sync_state("STUDY")
    assert state.high_water_mark == "2024-01-02 00:00:00+00:00"
    assert state.status == StudySyncStatus.FAILED
    assert state.error_message == "delta network error"

    # 2. Backfill failure after delta upsert of newer r2_newer still preserves previous high_water_mark
    sdk.records.list.side_effect = [[r2_newer]]
    sdk.get_records.side_effect = [
        [
            Record(study_key="STUDY", record_id=1, form_key="FORM", record_data=None),
            Record(study_key="STUDY", record_id=2, form_key="FORM", record_data=None),
            Record(study_key="STUDY", record_id=3, form_key="DRAFT_FORM", record_data=None),
        ],
        RuntimeError("backfill timeout"),
    ]
    with pytest.raises(RuntimeError, match="backfill timeout"):
        loader.sync_records("STUDY", reconcile=True)
    state = loader.get_study_sync_state("STUDY")
    assert state.high_water_mark == "2024-01-02 00:00:00+00:00"
    assert state.status == StudySyncStatus.FAILED
    assert state.error_message == "backfill timeout"

    # 3. Reconciliation failure preserves previous high_water_mark
    sdk.records.list.side_effect = [[r2_newer]]
    sdk.get_records.side_effect = [
        [
            Record(study_key="STUDY", record_id=1, form_key="FORM", record_data=None),
            Record(study_key="STUDY", record_id=2, form_key="FORM", record_data=None),
        ]
    ]
    from unittest.mock import patch

    with (
        patch.object(loader, "reconcile_cache", side_effect=RuntimeError("disk full")),
        pytest.raises(RuntimeError, match="disk full"),
    ):
        loader.sync_records("STUDY", reconcile=True)
    state = loader.get_study_sync_state("STUDY")
    assert state.high_water_mark == "2024-01-02 00:00:00+00:00"
    assert state.status == StudySyncStatus.FAILED
    assert state.error_message == "disk full"


def test_initialise_cache_migrates_legacy_sqlite_schema_in_place(tmp_path: Path) -> None:
    """Legacy records_cache.sqlite3 is migrated in place without deleting cached Study data."""
    import json

    from imednet_workflows.cached_loader import StudySyncStatus

    db_path = tmp_path / "records_cache.sqlite3"
    r1 = _record(1, "2024-03-01 00:00:00+00:00")
    r2_null = _null_timestamp_record(2)
    r3_null_only_study = _null_timestamp_record(10, study_key="NULL_STUDY")

    # Build a legacy SQLite database matching the pre-epoch NOT NULL schema
    conn = get_sqlite_connection(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE record_cache (
                study_key TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                form_key TEXT NOT NULL,
                date_modified TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (study_key, record_id)
            )
            """
        )
        conn.executemany(
            "INSERT INTO record_cache (study_key, record_id, form_key, date_modified, payload) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "STUDY",
                    1,
                    "FORM",
                    "2024-03-01 00:00:00+00:00",
                    json.dumps(r1.model_dump(mode="json", by_alias=True), sort_keys=True),
                ),
                (
                    "STUDY",
                    2,
                    "DRAFT_FORM",
                    "",  # Legacy coerced empty string for date_modified=None
                    json.dumps(r2_null.model_dump(mode="json", by_alias=True), sort_keys=True),
                ),
                (
                    "NULL_STUDY",
                    10,
                    "DRAFT_FORM",
                    "",
                    json.dumps(
                        r3_null_only_study.model_dump(mode="json", by_alias=True), sort_keys=True
                    ),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    sdk = MagicMock()
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    cached_study = loader.get_cached_records("STUDY")
    assert [r.record_id for r in cached_study] == [1, 2]
    assert [r.record_id for r in loader.get_cached_records("NULL_STUDY")] == [10]

    with get_sqlite_connection(db_path) as migrated_conn:
        row_2 = migrated_conn.execute(
            "SELECT date_modified, upserted_epoch FROM record_cache WHERE study_key = ? AND record_id = 2",
            ("STUDY",),
        ).fetchone()
        assert row_2["date_modified"] is None
        assert row_2["upserted_epoch"] == 1

    study_state = loader.get_study_sync_state("STUDY")
    assert study_state.sync_epoch == 1
    assert study_state.high_water_mark == "2024-03-01 00:00:00+00:00"
    assert study_state.initial_sync_complete is True
    assert study_state.status == StudySyncStatus.SYNCED

    null_study_state = loader.get_study_sync_state("NULL_STUDY")
    assert null_study_state.sync_epoch == 1
    assert null_study_state.high_water_mark is None
    assert null_study_state.initial_sync_complete is True
    assert null_study_state.status == StudySyncStatus.SYNCED


def test_reconcile_cache_epoch_fencing_retains_newer_upserts(tmp_path: Path) -> None:
    """reconcile_cache prunes orphans up to max_epoch while retaining newer concurrent upserts."""
    sdk = MagicMock()
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    r1 = _record(1, "2024-01-01 00:00:00+00:00")
    r2_deleted = _record(2, "2024-01-02 00:00:00+00:00")
    r3_concurrent_insert = _record(3, "2024-01-03 00:00:00+00:00")

    with get_sqlite_connection(loader.db_path) as conn:
        # Epoch 1: r1 and r2_deleted are cached
        loader._upsert_records(conn, [r1, r2_deleted])
        pre_reconcile_epoch = loader.get_study_sync_state("STUDY", conn=conn).sync_epoch
        assert pre_reconcile_epoch == 1

        # Simulate a stale upstream active-ID snapshot {1} captured at epoch 1,
        # while a concurrent sync cycle upserts r3_concurrent_insert at epoch 2.
        loader._upsert_records(conn, [r3_concurrent_insert])
        assert loader.get_study_sync_state("STUDY", conn=conn).sync_epoch == 2

        loader.reconcile_cache(conn, "STUDY", {1}, max_epoch=pre_reconcile_epoch)

    remaining_ids = [r.record_id for r in loader.get_cached_records("STUDY")]
    assert remaining_ids == [1, 3]


def test_iter_cached_records_order_by_subject_groups_subjects_cleanly(tmp_path: Path) -> None:
    """Verify order_by_subject=True groups cached records by USUBJID/subjectKey then record_id."""
    sdk = MagicMock()
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "subj-order-cache")

    interleaved = [
        Record(
            study_key="STUDY",
            record_id=10,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-02",
            record_data={"aeTerm": "Later subject first id"},
        ),
        Record(
            study_key="STUDY",
            record_id=20,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-01",
            record_data={"aeTerm": "Subj 1 first"},
        ),
        Record(
            study_key="STUDY",
            record_id=30,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-02",
            record_data={"aeTerm": "Later subject second id"},
        ),
        Record(
            study_key="STUDY",
            record_id=40,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-99",
            record_data={"USUBJID": "SUBJ-00", "aeTerm": "Explicit USUBJID override"},
        ),
        Record(
            study_key="STUDY",
            record_id=50,
            form_id=20,
            form_key="AE",
            subject_key="SUBJ-01",
            record_data={"aeTerm": "Subj 1 second"},
        ),
    ]
    with get_sqlite_connection(loader.db_path) as conn:
        loader._upsert_records(conn, interleaved)

    default_order = list(loader.iter_cached_records("STUDY", chunk_size=2))
    assert [r.record_id for r in default_order] == [10, 20, 30, 40, 50]

    subject_grouped = list(loader.iter_cached_records("STUDY", chunk_size=2, order_by_subject=True))
    assert [r.record_id for r in subject_grouped] == [40, 20, 50, 10, 30]
