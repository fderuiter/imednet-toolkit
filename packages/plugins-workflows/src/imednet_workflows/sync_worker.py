"""Background worker for maintaining a local record cache via incremental synchronization."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from filelock import FileLock

from .cached_loader import CachedRecordsLoader

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncWorkerConfig:
    """Configuration for a synchronization worker."""

    study_key: str
    interval_seconds: int = 900
    reconcile: bool = True
    lock_timeout_seconds: int = 30


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
        self._lock = FileLock(str(lock_path), timeout=config.lock_timeout_seconds)

    def run_once(self) -> int:
        """Run one idempotent cache sync cycle."""
        with self._lock:
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

    def run_forever(self) -> None:
        """Run sync cycles until stopped."""
        logger.info(
            "sync worker started",
            extra={
                "study_key": self._config.study_key,
                "interval_seconds": self._config.interval_seconds,
            },
        )
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # pragma: no cover - defensive logging path
                logger.exception("sync worker cycle failed")
            if self._stop_event.wait(self._config.interval_seconds):
                break
        logger.info("sync worker stopped", extra={"study_key": self._config.study_key})

    def stop(self) -> None:
        """Request graceful termination."""
        self._stop_event.set()
