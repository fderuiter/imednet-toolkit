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
        date_modified=datetime.fromisoformat(modified_at.replace("Z", "+00:00")),
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
        "record_data_filter": None,
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
