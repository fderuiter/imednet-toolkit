"""Unit tests for sync worker."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest
from filelock import FileLock

from imednet_workflows.sync_worker import SyncWorker, SyncWorkerConfig, SyncWorkerState


def test_sync_worker_run_once_syncs_and_transitions_to_running(tmp_path: Path) -> None:
    """Test that sync worker run_once syncs records and transitions to RUNNING."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    loader.count_cached_records.return_value = 2

    worker = SyncWorker(loader, config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=1))
    assert worker.state == SyncWorkerState.IDLE

    synced_count = worker.run_once()

    assert synced_count == 2
    assert worker.state == SyncWorkerState.RUNNING
    assert worker.consecutive_failures == 0
    assert worker.last_error is None
    loader.sync_records.assert_called_once_with("PROT-01", reconcile=True)
    loader.count_cached_records.assert_called_once_with("PROT-01")


def test_sync_worker_run_forever_stops_gracefully(tmp_path: Path) -> None:
    """Test that sync worker run_forever stops gracefully in STOPPED state."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    stop_event = Event()
    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=60),
        stop_event=stop_event,
    )

    def _stop_after_first_cycle(*args, **kwargs) -> None:
        stop_event.set()

    loader.sync_records.side_effect = _stop_after_first_cycle
    loader.count_cached_records.return_value = 0
    worker.run_forever()

    assert worker.state == SyncWorkerState.STOPPED
    loader.sync_records.assert_called_once_with("PROT-01", reconcile=True)
    loader.count_cached_records.assert_called_once_with("PROT-01")


def test_sync_worker_init_leaves_loader_lock_unchanged_and_has_no_private_lock(
    tmp_path: Path,
) -> None:
    """SyncWorker must not mutate loader.lock.timeout, create _lock, or hold FileLock during sync."""
    lock = FileLock(str(tmp_path / "records_cache.sqlite3.lock"), timeout=12.5)
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    loader.lock = lock
    loader.count_cached_records.return_value = 1

    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(
            study_key="PROT-01",
            interval_seconds=1,
            lock_timeout_seconds=99,
        ),
    )

    assert loader.lock.timeout == 12.5
    assert not hasattr(worker, "_lock")

    lock_held_during_sync = None

    def _check_lock(*args, **kwargs) -> None:
        nonlocal lock_held_during_sync
        lock_held_during_sync = loader.lock.is_locked

    loader.sync_records.side_effect = _check_lock
    worker.run_once()

    assert lock_held_during_sync is False


def test_sync_worker_degraded_recovery_and_error_callbacks(tmp_path: Path) -> None:
    """SyncWorker reports DEGRADED on first failure, invokes callbacks, and recovers to RUNNING."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    loader.count_cached_records.return_value = 5

    err1 = RuntimeError("transient 503")
    err2 = RuntimeError("transient timeout")
    stop_event = Event()
    states_observed: list[SyncWorkerState] = []
    callback_events: list[tuple[str, int]] = []

    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=0),
        stop_event=stop_event,
        on_error=lambda exc, count: callback_events.append((str(exc), count)),
    )

    call_num = 0

    def _sync_side_effect(*args, **kwargs) -> None:
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            raise err1
        if call_num == 2:
            states_observed.append(worker.state)
            raise err2
        # Cycle 3 succeeds
        states_observed.append(worker.state)

    loader.sync_records.side_effect = _sync_side_effect

    def _on_cycle(cycle: int, count: int) -> None:
        assert cycle == 3
        assert count == 5
        states_observed.append(worker.state)
        worker.stop()

    single_arg_errors: list[Exception] = []
    worker.run_forever(on_cycle=_on_cycle, on_error=lambda exc: single_arg_errors.append(exc))

    assert callback_events == [("transient 503", 1), ("transient timeout", 2)]
    assert single_arg_errors == [err1, err2]
    # Cycle 2 & 3 started while DEGRADED, then on_cycle observed recovery to RUNNING, then stop() -> STOPPED
    assert states_observed == [
        SyncWorkerState.DEGRADED,
        SyncWorkerState.DEGRADED,
        SyncWorkerState.RUNNING,
    ]
    assert worker.consecutive_failures == 0
    assert worker.last_error is None
    assert worker.state == SyncWorkerState.STOPPED


def test_sync_worker_transitions_to_failed_at_max_consecutive_failures(tmp_path: Path) -> None:
    """SyncWorker stops automatically in FAILED state when consecutive_failures reaches limit."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    loader.sync_records.side_effect = RuntimeError("401 invalid credentials")

    observed_states: list[tuple[int, SyncWorkerState]] = []
    sleep_ticks: list[tuple[int, int]] = []

    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(
            study_key="PROT-01",
            interval_seconds=0,
            max_consecutive_failures=3,
        ),
        on_error=lambda _exc, failures: observed_states.append((failures, worker.state)),
    )

    worker.run_forever(on_sleep_tick=lambda c, r: sleep_ticks.append((c, r)))

    assert observed_states == [
        (1, SyncWorkerState.DEGRADED),
        (2, SyncWorkerState.DEGRADED),
        (3, SyncWorkerState.FAILED),
    ]
    assert worker.state == SyncWorkerState.FAILED
    assert worker.consecutive_failures == 3
    assert isinstance(worker.last_error, RuntimeError)
    assert str(worker.last_error) == "401 invalid credentials"
    assert sleep_ticks == []

    # Calling stop() in a cleanup block preserves terminal FAILED state
    worker.stop()
    assert worker.state == SyncWorkerState.FAILED


def test_sync_worker_rejects_non_positive_max_consecutive_failures(tmp_path: Path) -> None:
    """Both SyncWorkerConfig and SyncWorker validate max_consecutive_failures >= 1."""
    with pytest.raises(ValueError, match="max_consecutive_failures must be at least 1"):
        SyncWorkerConfig(study_key="PROT-01", max_consecutive_failures=0)

    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    with pytest.raises(ValueError, match="max_consecutive_failures must be at least 1"):
        SyncWorker(
            loader,
            config=SyncWorkerConfig(study_key="PROT-01"),
            max_consecutive_failures=0,
        )


def test_sync_worker_sleep_tick_and_pre_stopped_run_forever(tmp_path: Path) -> None:
    """Verify on_sleep_tick stops mid-countdown and pre-stopped run_forever exits in STOPPED."""
    loader = MagicMock()
    loader.db_path = tmp_path / "records_cache.sqlite3"
    loader.count_cached_records.return_value = 3

    worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=2),
        max_consecutive_failures=4,
    )
    assert worker.max_consecutive_failures == 4

    ticks: list[tuple[int, int]] = []

    def _on_tick(cycle: int, remaining: int) -> None:
        ticks.append((cycle, remaining))
        worker.stop()

    worker.run_forever(on_sleep_tick=_on_tick)
    assert ticks == [(1, 2)]
    assert worker.state == SyncWorkerState.STOPPED

    # Calling run_forever again when stop_event is already set returns immediately in STOPPED
    pre_stopped = Event()
    pre_stopped.set()
    idle_worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=5),
        stop_event=pre_stopped,
    )
    idle_worker.run_forever()
    assert idle_worker.state == SyncWorkerState.STOPPED
