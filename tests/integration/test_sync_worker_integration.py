"""Integration tests for the background sync worker.

These tests spawn concurrent sync-writer and reader threads against a real
SQLite database to verify that WAL-mode + filelock coordination prevents any
read-write deadlock exceptions.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from imednet.models.records import Record
from imednet.spi.utils import get_sqlite_connection
from imednet_workflows.cached_loader import CachedRecordsLoader
from imednet_workflows.sync_worker import SyncWorker, SyncWorkerConfig


def _make_record(record_id: int, study_key: str = "PROT-01") -> Record:
    """Helper function to  make record."""
    return Record(
        study_key=study_key,
        form_id=1,
        form_key="FORM",
        record_id=record_id,
        subject_key=f"S{record_id:04d}",
        date_modified=datetime(2024, 1, record_id % 28 + 1, tzinfo=timezone.utc),
        record_data={"value": record_id},
    )


def _make_sdk(records: list[Record], study_key: str = "PROT-01") -> Any:
    """Return a mock SDK whose records.list always returns ``records``."""
    sdk = MagicMock()
    # Each call to records.list returns the full list so delta + reconcile work.
    sdk.records.list.return_value = records
    sdk.get_records.return_value = records
    return sdk


class _ReaderLoop(threading.Thread):
    """Continuously reads cached records until ``stop_event`` is set."""

    def __init__(self, loader: CachedRecordsLoader, stop_event: threading.Event) -> None:
        """Initialize the test object."""
        super().__init__(daemon=True)
        self.loader = loader
        self.stop_event = stop_event
        self.errors: list[Exception] = []
        self.read_count = 0

    def run(self) -> None:
        """Helper function to run."""
        while not self.stop_event.is_set():
            try:
                self.loader.get_cached_records("PROT-01")
                self.read_count += 1
            except Exception as exc:  # pragma: no cover - should never fire
                self.errors.append(exc)
                break


def test_concurrent_sync_and_readers_no_deadlock(tmp_path: Path) -> None:
    """Parallel sync cycles and reader threads must not raise any exceptions."""
    records = [_make_record(i) for i in range(1, 11)]
    sdk = _make_sdk(records)

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
    # Seed the cache with an initial sync so readers have something to read.
    loader.sync_records("PROT-01")

    stop_event = threading.Event()

    # Start several concurrent reader threads.
    readers = [_ReaderLoop(loader, stop_event) for _ in range(4)]
    for reader in readers:
        reader.start()

    # Run several sequential sync cycles while readers are active.
    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=0, reconcile=True),
    )
    sync_errors: list[Exception] = []
    for _ in range(5):
        try:
            worker.run_once()
        except Exception as exc:  # pragma: no cover - should never fire
            sync_errors.append(exc)

    stop_event.set()
    for reader in readers:
        reader.join(timeout=5)

    all_reader_errors = [err for reader in readers for err in reader.errors]
    assert sync_errors == [], f"Sync cycle raised exceptions: {sync_errors}"
    assert all_reader_errors == [], f"Reader loop raised exceptions: {all_reader_errors}"
    assert all(reader.read_count > 0 for reader in readers), "Readers never completed a read"


def test_parallel_sync_workers_no_deadlock(tmp_path: Path) -> None:
    """Multiple SyncWorker instances sharing the same DB must not deadlock."""
    records = [_make_record(i) for i in range(1, 6)]

    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def _run_worker(worker_id: int) -> None:
        """Helper function to  run worker."""
        sdk = _make_sdk(records)
        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
        worker = SyncWorker(
            loader,
            config=SyncWorkerConfig(
                study_key="PROT-01",
                interval_seconds=0,
                lock_timeout_seconds=30,
            ),
        )
        barrier.wait()  # synchronise start for maximum contention
        try:
            for _ in range(3):
                worker.run_once()
        except Exception as exc:  # pragma: no cover - should never fire
            errors.append(exc)

    threads = [threading.Thread(target=_run_worker, args=(i,), daemon=True) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"Parallel sync workers raised exceptions: {errors}"

    # Verify the DB contains the expected records after all workers finish.
    loader = CachedRecordsLoader(_make_sdk(records), cache_dir=tmp_path)
    cached = loader.get_cached_records("PROT-01")
    assert {r.record_id for r in cached} == {r.record_id for r in records}


def test_wal_mode_reader_does_not_block_on_writer(tmp_path: Path) -> None:
    """A WAL-mode reader must be able to open a connection while a writer holds a lock."""
    db_path = tmp_path / "records.sqlite3"

    # Initialise the schema via a loader.
    sdk = _make_sdk([_make_record(1)])
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path, database_name="records.sqlite3")
    loader.sync_records("PROT-01")

    writer_conn = get_sqlite_connection(db_path)
    try:
        writer_conn.execute("BEGIN IMMEDIATE")

        # A reader should be able to open a new connection and read even while
        # the writer holds a write-lock, because WAL mode allows concurrent reads.
        reader_conn = get_sqlite_connection(db_path)
        try:
            rows = reader_conn.execute(
                "SELECT COUNT(*) FROM record_cache WHERE study_key = ?", ("PROT-01",)
            ).fetchone()
            assert rows is not None
            assert rows[0] == 1
        finally:
            reader_conn.close()
    finally:
        writer_conn.rollback()
        writer_conn.close()


def test_sync_worker_stop_is_idempotent(tmp_path: Path) -> None:
    """Calling stop() multiple times must not raise."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records.sqlite3"
    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=60),
    )
    worker.stop()
    worker.stop()  # second call must be a no-op


def test_sync_worker_respects_reconcile_false(tmp_path: Path) -> None:
    """When reconcile=False the loader is called with reconcile=False."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records.sqlite3"
    loader.count_cached_records.return_value = 0

    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=0, reconcile=False),
    )
    worker.run_once()

    loader.sync_records.assert_called_once_with("PROT-01", reconcile=False)
    loader.count_cached_records.assert_called_once_with("PROT-01")


def test_sync_worker_config_defaults() -> None:
    """SyncWorkerConfig must expose sensible defaults."""
    cfg = SyncWorkerConfig(study_key="MY-STUDY")
    assert cfg.interval_seconds == 900
    assert cfg.reconcile is True
    assert cfg.lock_timeout_seconds == 30


@pytest.mark.parametrize(("n_readers", "n_cycles"), [(2, 10), (6, 5)])
def test_high_concurrency_no_exceptions(tmp_path: Path, n_readers: int, n_cycles: int) -> None:
    """High reader/writer concurrency must not produce any exceptions."""
    records = [_make_record(i) for i in range(1, 21)]
    sdk = _make_sdk(records)
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
    loader.sync_records("PROT-01")

    stop_event = threading.Event()
    readers = [_ReaderLoop(loader, stop_event) for _ in range(n_readers)]
    for r in readers:
        r.start()

    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=0),
    )
    sync_errors: list[Exception] = []
    for _ in range(n_cycles):
        try:
            worker.run_once()
        except Exception as exc:  # pragma: no cover
            sync_errors.append(exc)

    stop_event.set()
    for r in readers:
        r.join(timeout=10)

    assert sync_errors == []
    assert [e for r in readers for e in r.errors] == []


def test_parallel_sync_workers_no_timeout_under_network_delay(tmp_path: Path) -> None:
    """Parallel workers must not timeout when network requests experience latency."""
    import time

    records = [_make_record(i) for i in range(1, 6)]

    def _slow_fetch(*args, **kwargs):
        # 0.15s latency simulates slow remote EDC API response
        time.sleep(0.15)
        return records

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _run_worker(worker_id: int) -> None:
        sdk = MagicMock()
        sdk.records.list.side_effect = _slow_fetch
        sdk.get_records.side_effect = _slow_fetch
        loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
        # Lock timeout is 50ms, strictly LESS than the 150ms network delay.
        # If the lock were held across the network call, the second worker would time out.
        worker = SyncWorker(
            loader,
            config=SyncWorkerConfig(
                study_key="PROT-01",
                interval_seconds=0,
                lock_timeout_seconds=0.05,
            ),
        )
        barrier.wait()
        try:
            for _ in range(2):
                worker.run_once()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=_run_worker, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == [], f"Workers raised timeout or other exceptions under network delay: {errors}"


def test_concurrent_readers_during_slow_remote_sync(tmp_path: Path) -> None:
    """Readers access local cache without blocking while remote data retrieval is in progress."""
    import time

    records = [_make_record(i) for i in range(1, 6)]
    initial_sdk = _make_sdk(records)
    initial_loader = CachedRecordsLoader(initial_sdk, cache_dir=tmp_path)
    initial_loader.sync_records("PROT-01")

    # Mock SDK with 0.3s network delay
    def _slow_fetch(*args, **kwargs):
        time.sleep(0.3)
        return records

    sdk = MagicMock()
    sdk.records.list.side_effect = _slow_fetch
    sdk.get_records.side_effect = _slow_fetch
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path)
    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=0),
    )

    stop_event = threading.Event()
    reader = _ReaderLoop(loader, stop_event)
    reader.start()

    worker_thread = threading.Thread(target=worker.run_once)
    worker_thread.start()

    # While worker is sleeping in network call, reader should perform multiple reads
    time.sleep(0.15)
    reads_mid_sync = reader.read_count

    worker_thread.join(timeout=5)
    stop_event.set()
    reader.join(timeout=5)

    assert reader.errors == []
    assert reads_mid_sync > 0, "Reader was blocked while remote sync was retrieving data"
