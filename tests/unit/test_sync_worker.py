"""Unit tests for sync worker."""

from __future__ import annotations

from threading import Event
from unittest.mock import MagicMock

from imednet_workflows.sync_worker import SyncWorker, SyncWorkerConfig


def test_sync_worker_run_once_syncs_with_lock(tmp_path) -> None:
    """Test that sync worker run once syncs with lock."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    loader.count_cached_records.return_value = 2

    worker = SyncWorker(loader, config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=1))

    synced_count = worker.run_once()

    assert synced_count == 2
    loader.sync_records.assert_called_once_with("PROT-01", reconcile=True)
    loader.count_cached_records.assert_called_once_with("PROT-01")


def test_sync_worker_run_forever_stops_gracefully(tmp_path) -> None:
    """Test that sync worker run forever stops gracefully."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    stop_event = Event()
    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=60),
        stop_event=stop_event,
    )

    def _stop_after_first_cycle(*args, **kwargs) -> None:
        """Helper function to  stop after first cycle."""
        stop_event.set()

    loader.sync_records.side_effect = _stop_after_first_cycle
    loader.count_cached_records.return_value = 0
    worker.run_forever()

    loader.sync_records.assert_called_once_with("PROT-01", reconcile=True)
    loader.count_cached_records.assert_called_once_with("PROT-01")


def test_sync_worker_run_once_does_not_hold_lock_during_sync_records(tmp_path) -> None:
    """Test that SyncWorker does not hold the file lock while sync_records runs."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    loader.count_cached_records.return_value = 1

    worker = SyncWorker(loader, config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=1))

    lock_held_during_sync = None

    def _check_lock(*args, **kwargs) -> None:
        nonlocal lock_held_during_sync
        lock_held_during_sync = worker._lock.is_locked

    loader.sync_records.side_effect = _check_lock

    worker.run_once()

    assert lock_held_during_sync is False
