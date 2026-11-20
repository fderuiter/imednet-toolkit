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


def test_sync_worker_config_rejects_non_positive_interval() -> None:
    """SyncWorkerConfig rejects zero, negative, and boolean interval_seconds values."""
    import pytest

    for invalid in (0, -1, -60, False):
        with pytest.raises(ValueError, match="interval_seconds must be greater than 0"):
            SyncWorkerConfig(study_key="PROT-01", interval_seconds=invalid)  # type: ignore[arg-type]


def test_sync_worker_run_forever_reraises_permanent_api_errors(tmp_path) -> None:
    """SyncWorker.run_forever fails fast on 401, 403, and 404 ApiError exceptions."""
    import pytest

    from imednet.spi.errors import (
        ApiError,
        AuthenticationError,
        AuthorizationError,
        ForbiddenError,
        NotFoundError,
        UnauthorizedError,
    )

    permanent_errors = [
        AuthenticationError("bad key"),
        UnauthorizedError("unauthorized"),
        AuthorizationError("denied"),
        ForbiddenError("forbidden"),
        NotFoundError("unknown study"),
        ApiError("raw 401", status_code=401),
        ApiError("raw 403", status_code=403),
        ApiError("raw 404", status_code=404),
    ]

    for err in permanent_errors:
        loader = MagicMock()
        loader.db_path = tmp_path / "records_cache.sqlite3"
        loader.sync_records.side_effect = err
        worker = SyncWorker(
            loader,
            config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=60),
        )
        with pytest.raises(type(err)):
            worker.run_forever()
        loader.sync_records.assert_called_once()


def test_sync_worker_run_forever_retries_transient_errors(tmp_path, caplog) -> None:
    """SyncWorker.run_forever logs and retries 429, 5xx, and network errors on subsequent cycles."""
    from imednet.spi.errors import RateLimitError, RequestError, ServerError

    for transient_err in (
        RateLimitError("rate limited"),
        ServerError("gateway timeout", status_code=503),
        RequestError("connection reset"),
        RuntimeError("transient glitch"),
    ):
        caplog.clear()
        loader = MagicMock()
        loader.db_path = tmp_path / "records_cache.sqlite3"
        stop_event = Event()
        calls = 0

        def _fail_then_succeed(*_args, _err=transient_err, **_kwargs) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _err
            stop_event.set()

        loader.sync_records.side_effect = _fail_then_succeed
        loader.count_cached_records.return_value = 7
        worker = SyncWorker(
            loader,
            config=SyncWorkerConfig(study_key="PROT-01", interval_seconds=1),
            stop_event=stop_event,
        )
        cycles: list[tuple[int, int]] = []
        worker.run_forever(on_cycle=lambda c, n: cycles.append((c, n)))

        assert calls == 2
        assert cycles == [(2, 7)]
        assert "sync worker cycle failed" in caplog.text
