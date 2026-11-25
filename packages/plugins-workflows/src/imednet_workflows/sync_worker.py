"""Background worker for maintaining a local record cache via incremental synchronization."""

from __future__ import annotations

import inspect
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Any

from imednet.spi.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    NotFoundError,
)

from .cached_loader import CachedRecordsLoader, bind_loader_lock_timeout

logger = logging.getLogger(__name__)

_PERMANENT_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403, 404})


def _is_permanent_api_error(exc: ApiError) -> bool:
    """Return True when an API error represents a permanent client failure."""
    if isinstance(
        exc,
        (BadRequestError, AuthenticationError, AuthorizationError, NotFoundError),
    ):
        return True
    return exc.status_code in _PERMANENT_STATUS_CODES


def _validate_study_key_value(study_key: Any) -> None:
    """Validate that ``study_key`` is a non-empty, non-whitespace string."""
    if not isinstance(study_key, str) or not study_key.strip():
        raise ValueError("study_key must be a non-empty string")


def _validate_positive_number(value: Any, *, field_name: str) -> None:
    """Validate that a numeric timeout or interval is finite, non-boolean, and > 0."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be greater than 0")


def _validate_max_consecutive_failures(value: Any) -> None:
    """Validate that ``max_consecutive_failures`` is None or a non-boolean integer >= 1."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_consecutive_failures must be at least 1")


def _validate_reconcile_every_n_cycles(value: Any) -> None:
    """Validate that ``reconcile_every_n_cycles`` is a non-boolean integer >= 1."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("reconcile_every_n_cycles must be at least 1")


class SyncWorkerState(StrEnum):
    """Lifecycle states for a background cache synchronization worker."""

    IDLE = "idle"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    FAILED = "failed"


SyncErrorCallback = Callable[[Exception, int], None] | Callable[[Exception], None]


def _invoke_error_callback(
    callback: SyncErrorCallback, exc: Exception, consecutive_failures: int
) -> None:
    """Invoke an error callback accepting either ``(exc, failures)`` or ``(exc,)``."""
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        callback(exc, consecutive_failures)  # type: ignore[call-arg]
        return

    positional_count = 0
    has_varargs = False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            has_varargs = True
            break
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional_count += 1

    if has_varargs or positional_count >= 2:
        callback(exc, consecutive_failures)  # type: ignore[call-arg]
    else:
        callback(exc)  # type: ignore[call-arg]


@dataclass(slots=True)
class SyncWorkerConfig:
    """Configuration for a synchronization worker."""

    study_key: str
    interval_seconds: int = 900
    reconcile: bool = True
    reconcile_every_n_cycles: int = 1
    lock_timeout_seconds: float = 30
    max_consecutive_failures: int | None = None

    def __post_init__(self) -> None:
        """Validate worker configuration bounds."""
        _validate_study_key_value(self.study_key)
        _validate_positive_number(self.interval_seconds, field_name="interval_seconds")
        _validate_reconcile_every_n_cycles(self.reconcile_every_n_cycles)
        _validate_positive_number(self.lock_timeout_seconds, field_name="lock_timeout_seconds")
        _validate_max_consecutive_failures(self.max_consecutive_failures)


class SyncWorker:
    """Background cache refresher that runs incremental record sync loops."""

    def __init__(
        self,
        loader: CachedRecordsLoader,
        *,
        config: SyncWorkerConfig,
        stop_event: Event | None = None,
        on_error: SyncErrorCallback | None = None,
        max_consecutive_failures: int | None = None,
    ) -> None:
        """Initialize the sync worker.

        Args:
            loader: The cached records loader to use for synchronization.
            config: Worker configuration including study key and interval.
            stop_event: Optional threading event to control worker termination.
            on_error: Optional callback invoked when a sync cycle raises an exception.
            max_consecutive_failures: Optional limit overriding ``config.max_consecutive_failures``.
        """
        _validate_study_key_value(config.study_key)
        _validate_positive_number(config.interval_seconds, field_name="interval_seconds")
        _validate_reconcile_every_n_cycles(config.reconcile_every_n_cycles)
        _validate_positive_number(config.lock_timeout_seconds, field_name="lock_timeout_seconds")
        _validate_max_consecutive_failures(config.max_consecutive_failures)
        if max_consecutive_failures is not None:
            _validate_max_consecutive_failures(max_consecutive_failures)

        effective_max = (
            max_consecutive_failures
            if max_consecutive_failures is not None
            else config.max_consecutive_failures
        )

        self._loader = loader
        self._config = config
        self._stop_event = stop_event or Event()
        self._on_error = on_error
        self._max_consecutive_failures = effective_max
        self._state = SyncWorkerState.IDLE
        self._consecutive_failures = 0
        self._cycle_index = 0
        self._last_error: Exception | None = None

    @property
    def state(self) -> SyncWorkerState:
        """Return the current lifecycle state of the worker."""
        return self._state

    @property
    def consecutive_failures(self) -> int:
        """Return the number of consecutive failed sync cycles."""
        return self._consecutive_failures

    @property
    def last_error(self) -> Exception | None:
        """Return the exception raised by the most recent failed sync cycle, if any."""
        return self._last_error

    @property
    def max_consecutive_failures(self) -> int | None:
        """Return the configured maximum consecutive failures before stopping as FAILED."""
        return self._max_consecutive_failures

    def run_once(self) -> int:
        """Run one idempotent cache sync cycle, preserving terminal FAILED state."""
        if self._state == SyncWorkerState.FAILED:
            if self._last_error is not None:
                raise self._last_error
            raise RuntimeError("SyncWorker cannot execute run_once from terminal FAILED state")

        if self._state in (
            SyncWorkerState.IDLE,
            SyncWorkerState.STOPPED,
        ):
            self._state = SyncWorkerState.RUNNING

        self._cycle_index += 1
        should_reconcile = bool(
            self._config.reconcile
            and self._cycle_index % self._config.reconcile_every_n_cycles == 0
        )

        try:
            with bind_loader_lock_timeout(self._config.lock_timeout_seconds):
                self._loader.sync_records(
                    self._config.study_key,
                    reconcile=should_reconcile,
                )
            record_count = self._loader.count_cached_records(self._config.study_key)
        except Exception as exc:
            self._consecutive_failures += 1
            self._last_error = exc
            is_permanent = isinstance(exc, ApiError) and _is_permanent_api_error(exc)
            if is_permanent or (
                self._max_consecutive_failures is not None
                and self._consecutive_failures >= self._max_consecutive_failures
            ):
                self._state = SyncWorkerState.FAILED
                self._stop_event.set()
            else:
                self._state = SyncWorkerState.DEGRADED

            if self._on_error is not None:
                _invoke_error_callback(self._on_error, exc, self._consecutive_failures)
            raise

        self._consecutive_failures = 0
        self._last_error = None
        if self._stop_event.is_set():
            if self._state != SyncWorkerState.FAILED:
                self._state = SyncWorkerState.STOPPED
        else:
            self._state = SyncWorkerState.RUNNING

        logger.info(
            "sync cycle complete",
            extra={
                "study_key": self._config.study_key,
                "record_count": record_count,
                "state": self._state.value,
            },
        )
        return record_count

    def run_forever(
        self,
        on_cycle: Callable[[int, int], None] | None = None,
        on_sleep_tick: Callable[[int, int], None] | None = None,
        on_error: SyncErrorCallback | None = None,
    ) -> None:
        """Run sync cycles until stopped or failed."""
        if self._state == SyncWorkerState.FAILED:
            if isinstance(self._last_error, ApiError) and _is_permanent_api_error(self._last_error):
                raise self._last_error
            return

        if self._stop_event.is_set():
            self._state = SyncWorkerState.STOPPED
            return

        self._state = SyncWorkerState.RUNNING
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
            except Exception as exc:
                if isinstance(exc, ApiError) and _is_permanent_api_error(exc):
                    if on_error is not None and on_error is not self._on_error:
                        _invoke_error_callback(on_error, exc, self._consecutive_failures)
                    raise
                logger.exception(
                    "sync worker cycle failed",
                    extra={
                        "study_key": self._config.study_key,
                        "consecutive_failures": self._consecutive_failures,
                        "state": self._state.value,
                    },
                )
                if on_error is not None and on_error is not self._on_error:
                    _invoke_error_callback(on_error, exc, self._consecutive_failures)
                if self._state == SyncWorkerState.FAILED:
                    break
            if self._stop_event.is_set():
                break
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

        if self._state != SyncWorkerState.FAILED:
            self._state = SyncWorkerState.STOPPED
        logger.info(
            "sync worker stopped",
            extra={
                "study_key": self._config.study_key,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
            },
        )

    def stop(self) -> None:
        """Request graceful termination."""
        self._stop_event.set()
        if self._state != SyncWorkerState.FAILED:
            self._state = SyncWorkerState.STOPPED
