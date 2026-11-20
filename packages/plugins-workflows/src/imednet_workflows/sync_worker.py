"""Background worker for maintaining a local record cache via incremental synchronization."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from filelock import FileLock

from imednet.spi.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
)

from .cached_loader import CachedRecordsLoader

logger = logging.getLogger(__name__)

_PERMANENT_STATUS_CODES: frozenset[int] = frozenset({401, 403, 404})


def _is_permanent_api_error(exc: ApiError) -> bool:
    """Return True when an API error represents a permanent client failure."""
    if isinstance(exc, (AuthenticationError, AuthorizationError, NotFoundError)):
        return True
    return exc.status_code in _PERMANENT_STATUS_CODES


@dataclass(slots=True)
class SyncWorkerConfig:
    """Configuration for a synchronization worker."""

    study_key: str
    interval_seconds: int = 900
    reconcile: bool = True
    lock_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        """Validate worker configuration bounds."""
        if isinstance(self.interval_seconds, bool) or self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than 0")


class SyncWorker:
    """Background cache refresher that runs incremental record sync loops."""

    def __init__(
        self,
        loader: CachedRecordsLoader,
        *,
        config: SyncWorkerConfig,
        stop_event: Event | None = None,
    ) -> None:
        """Initialize the sync worker.

        Args:
            loader: The cached records loader to use for synchronization.
            config: Worker configuration including study key and interval.
            stop_event: Optional threading event to control worker termination.
        """
        self._loader = loader
        self._config = config
        self._stop_event = stop_event or Event()
        lock_path = Path(f"{loader.db_path}.lock")
        if hasattr(loader, "lock") and isinstance(loader.lock, FileLock):
            loader.lock.timeout = config.lock_timeout_seconds
            self._lock = loader.lock
        else:
            self._lock = FileLock(str(lock_path), timeout=config.lock_timeout_seconds)

    def run_once(self) -> int:
        """Run one idempotent cache sync cycle."""
        self._loader.sync_records(
            self._config.study_key,
            reconcile=self._config.reconcile,
        )
        record_count = self._loader.count_cached_records(self._config.study_key)
        logger.info(
            "sync cycle complete",
            extra={
                "study_key": self._config.study_key,
                "record_count": record_count,
            },
        )
        return record_count

    def run_forever(
        self,
        on_cycle: Callable[[int, int], None] | None = None,
        on_sleep_tick: Callable[[int, int], None] | None = None,
    ) -> None:
        """Run sync cycles until stopped."""
        logger.info(
            "sync worker started",
            extra={
                "study_key": self._config.study_key,
                "interval_seconds": self._config.interval_seconds,
            },
        )
        cycle_count = 0
        while not self._stop_event.is_set():
            cycle_count += 1
            try:
                record_count = self.run_once()
                if on_cycle:
                    on_cycle(cycle_count, record_count)
            except ApiError as exc:
                if _is_permanent_api_error(exc):
                    raise
                logger.exception("sync worker cycle failed")
            except Exception:  # pragma: no cover - defensive logging path
                logger.exception("sync worker cycle failed")
            if on_sleep_tick:
                remaining = self._config.interval_seconds
                while remaining > 0 and not self._stop_event.is_set():
                    on_sleep_tick(cycle_count, remaining)
                    sleep_chunk = min(1.0, float(remaining))
                    if self._stop_event.wait(sleep_chunk):
                        break
                    remaining -= int(sleep_chunk) if sleep_chunk >= 1 else 1
            elif self._stop_event.wait(self._config.interval_seconds):
                break
        logger.info("sync worker stopped", extra={"study_key": self._config.study_key})

    def stop(self) -> None:
        """Request graceful termination."""
        self._stop_event.set()
