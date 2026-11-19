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
        second_batch,
        [first_batch[1], second_batch[0]],
    ]

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "custom-cache")

    first_load = loader.load_records("STUDY")
    second_load = loader.load_records("STUDY")

    assert loader.db_path.parent == tmp_path / "custom-cache"
    assert [record.record_id for record in first_load] == [1, 2]
    assert [record.record_id for record in second_load] == [2, 3]

    first_delta_call = sdk.get_records.call_args_list[0]
    second_delta_call = sdk.get_records.call_args_list[2]
    assert first_delta_call.kwargs == {"study_key": "STUDY", "record_data_filter": None}
    assert second_delta_call.kwargs == {
        "study_key": "STUDY",
        "filter": 'dateModified>="2024-01-02 00:00:00+00:00"',
        "record_data_filter": None,
    }
    assert sdk.get_records.call_args_list[3].kwargs == {
        "study_key": "STUDY",
        "record_data_filter": "none",
        "deleted": False,
    }
    sdk.records.list.assert_not_called()


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
        [],
    ]
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)

    loader.sync_records("STUDY", reconcile=False)
    loader.sync_records("STUDY", reconcile=False)

    assert sdk.get_records.call_count == 2
    sdk.records.list.assert_not_called()
    assert sdk.get_records.call_args_list[1].kwargs == {
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
