"""MultiStudyOrchestrator: concurrent multi-study pipeline engine.

This module provides the :class:`MultiStudyOrchestrator` class, which drives
parallel execution of a user-supplied pipeline function across all active
iMednet study boundaries discovered via the SDK's studies endpoint.

Three-step usage
----------------

1. **Construct** the orchestrator with a live SDK instance::

       orchestrator = MultiStudyOrchestrator(sdk, max_workers=8)

2. **Define** a pipeline callable that conforms to
   :class:`~imednet.orchestration.StudyWorkerCallable`::

       def my_pipeline(study_key, sdk, study_logger, **kwargs):
           study_logger.info("Running pipeline for %s", study_key)
           subjects = sdk.subjects.list(study_key=study_key)
           return {"count": len(list(subjects))}

3. **Execute** and inspect results::

       results = orchestrator.execute_pipeline(
           my_pipeline,
           whitelist={"PROT-01", "PROT-02"},
       )
       for study_key, r in results.items():
           print(study_key, r["status"], r["duration_seconds"])

Each study runs in its own :func:`~imednet.core.context.study_context`,
so context-sensitive SDK calls resolve the correct study automatically.
Per-study failures are captured in the result dict (``status="FAILED"``)
and never propagate as exceptions, ensuring fault isolation.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any

from imednet.core.context import study_context as _study_context
from imednet.errors.orchestration import FilterConflictError
from imednet.orchestration.logging import StudyContextLogAdapter, make_study_logger
from imednet.orchestration.types import (
    AsyncStudyWorkerCallable,
    OrchestratorResult,
    StudyWorkerCallable,
)

logger = logging.getLogger(__name__)
_DURATION_PRECISION = 4
_GLOBAL_TIMEOUT_SECONDS = 300.0


def _is_async_only_client(sdk: Any) -> bool:
    """Return True when ``sdk`` is an asynchronous-only SDK or facade."""
    from imednet.sdk import AsyncImednetSDK, ImednetSDK

    if isinstance(sdk, AsyncImednetSDK):
        return True
    if isinstance(sdk, ImednetSDK):
        return False
    has_sync = hasattr(sdk, "get_studies") or hasattr(sdk, "create_record")
    has_async = hasattr(sdk, "async_get_studies") or hasattr(sdk, "async_create_record")
    return has_async and not has_sync


def _is_sync_only_client(sdk: Any) -> bool:
    """Return True when ``sdk`` is a synchronous-only SDK or facade."""
    from imednet.sdk import AsyncImednetSDK, ImednetSDK

    if isinstance(sdk, ImednetSDK):
        return True
    if isinstance(sdk, AsyncImednetSDK):
        return False
    has_sync = hasattr(sdk, "get_studies") or hasattr(sdk, "create_record")
    has_async = hasattr(sdk, "async_get_studies") or hasattr(sdk, "async_create_record")
    return has_sync and not has_async


def _ensure_sync_orchestrator_client(sdk: Any, sync_method: str, async_method: str) -> None:
    """Raise ``TypeError`` when a sync orchestrator method is called with an async SDK."""
    if _is_async_only_client(sdk):
        raise TypeError(
            f"MultiStudyOrchestrator.{sync_method}() cannot be used with an asynchronous SDK "
            f"client ({type(sdk).__name__}); await "
            f"MultiStudyOrchestrator.{async_method}() instead."
        )


def _ensure_async_orchestrator_client(sdk: Any, async_method: str, sync_method: str) -> None:
    """Raise ``TypeError`` when an async orchestrator method is called with a sync SDK."""
    if _is_sync_only_client(sdk):
        raise TypeError(
            f"MultiStudyOrchestrator.{async_method}() cannot be used with a synchronous SDK "
            f"client ({type(sdk).__name__}); call "
            f"MultiStudyOrchestrator.{sync_method}() instead."
        )


def _apply_study_filters(
    all_studies: list[str],
    *,
    whitelist: set[str] | None = None,
    blacklist: set[str] | None = None,
) -> list[str]:
    """Filter discovered study keys using mutually exclusive whitelist/blacklist sets."""
    logger.debug("Resolved %d studies from registry.", len(all_studies))

    if whitelist:
        filtered = [study_key for study_key in all_studies if study_key in whitelist]
        logger.info(
            "Whitelist filter applied: %d/%d studies selected.",
            len(filtered),
            len(all_studies),
        )
        return filtered

    if blacklist:
        filtered = [study_key for study_key in all_studies if study_key not in blacklist]
        logger.info(
            "Blacklist filter applied: %d/%d studies selected.",
            len(filtered),
            len(all_studies),
        )
        return filtered

    return all_studies


def _compute_backpressure_step(
    latency: float,
    baseline: float | None,
    current_max: int,
    initial_concurrency: int,
    min_concurrency: int = 1,
) -> tuple[float, int, str | None]:
    """Compute updated latency baseline and max concurrency for adaptive backpressure."""
    if baseline is None:
        return latency, current_max, None

    new_max = current_max
    direction: str | None = None
    if latency > baseline * 2.0:
        candidate = max(min_concurrency, current_max - 1)
        if candidate < current_max:
            logger.warning(
                "Adaptive backpressure: downstream latency increased "
                "(%.2fs vs baseline %.2fs). Reducing max concurrency to %d.",
                latency,
                baseline,
                candidate,
            )
            new_max = candidate
            direction = "down"
    elif latency <= baseline * 1.2:
        candidate = min(initial_concurrency, current_max + 1)
        if candidate > current_max:
            logger.info(
                "Adaptive backpressure: downstream latency recovered. "
                "Increasing max concurrency to %d.",
                candidate,
            )
            new_max = candidate
            direction = "up"

    updated_baseline = 0.8 * baseline + 0.2 * latency
    return updated_baseline, new_max, direction


def _build_study_outcome_result(
    study_key: str,
    duration: float,
    *,
    data: Any = None,
    exc: BaseException | None = None,
) -> OrchestratorResult:
    """Create and log a normalized ``OrchestratorResult`` for a completed study task."""
    rounded = round(duration, _DURATION_PRECISION)
    if exc is None:
        logger.info("[%s] Pipeline completed successfully in %.2fs.", study_key, duration)
        return OrchestratorResult(
            status="SUCCESS",
            data=data,
            error=None,
            duration_seconds=rounded,
        )

    logger.error(
        "[%s] Pipeline execution failed after %.2fs: %s",
        study_key,
        duration,
        repr(exc),
        exc_info=exc,
    )
    return OrchestratorResult(
        status="FAILED",
        data=None,
        error=repr(exc),
        duration_seconds=rounded,
    )


def _build_timeout_result(duration: float) -> OrchestratorResult:
    """Create a normalized ``OrchestratorResult`` for a timed-out study task."""
    return OrchestratorResult(
        status="FAILED",
        data=None,
        error="TimeoutError: Global orchestration timeout reached.",
        duration_seconds=round(duration, _DURATION_PRECISION),
    )


class AdaptiveConcurrencyLimiter:
    """Manages adaptive backpressure by limiting concurrency based on task latency."""

    def __init__(self, initial_concurrency: int):
        """Initialize the adaptive concurrency limiter.

        Args:
            initial_concurrency: The starting maximum number of concurrent tasks.
        """
        self._max_concurrency = initial_concurrency
        self._current_concurrency = 0
        self._cond = threading.Condition()
        self._latency_baseline: float | None = None
        self._lock = threading.Lock()
        self._min_concurrency = 1
        self._initial_concurrency = initial_concurrency

    def acquire(self) -> None:
        """Acquire a concurrency slot, blocking if the limit is reached."""
        with self._cond:
            while self._current_concurrency >= self._max_concurrency:
                self._cond.wait()
            self._current_concurrency += 1

    def release(self) -> None:
        """Release a concurrency slot and notify waiting threads."""
        with self._cond:
            self._current_concurrency -= 1
            self._cond.notify_all()

    def record_latency(self, latency: float) -> None:
        """Record the latency of a completed task and adjust concurrency.

        Args:
            latency: The time taken to complete the task in seconds.
        """
        with self._lock:
            baseline, new_max, direction = _compute_backpressure_step(
                latency,
                self._latency_baseline,
                self._max_concurrency,
                self._initial_concurrency,
                self._min_concurrency,
            )
            if new_max != self._max_concurrency:
                with self._cond:
                    self._max_concurrency = new_max
                    if direction == "up":
                        self._cond.notify_all()
            self._latency_baseline = baseline


class AsyncAdaptiveConcurrencyLimiter:
    """Asynchronous adaptive concurrency limiter using an event-loop condition."""

    def __init__(self, initial_concurrency: int) -> None:
        """Initialize the asynchronous adaptive concurrency limiter.

        Args:
            initial_concurrency: The starting maximum number of concurrent tasks.
        """
        self._max_concurrency = initial_concurrency
        self._current_concurrency = 0
        self._cond = asyncio.Condition()
        self._latency_baseline: float | None = None
        self._min_concurrency = 1
        self._initial_concurrency = initial_concurrency

    async def acquire(self) -> None:
        """Acquire a concurrency slot without blocking the event loop."""
        async with self._cond:
            while self._current_concurrency >= self._max_concurrency:
                await self._cond.wait()
            self._current_concurrency += 1

    async def release(self) -> None:
        """Release a concurrency slot and wake waiting coroutines."""
        async with self._cond:
            self._current_concurrency -= 1
            self._cond.notify_all()

    async def record_latency(self, latency: float) -> None:
        """Record the latency of a completed async task and adjust concurrency."""
        baseline, new_max, direction = _compute_backpressure_step(
            latency,
            self._latency_baseline,
            self._max_concurrency,
            self._initial_concurrency,
            self._min_concurrency,
        )
        self._latency_baseline = baseline
        if new_max != self._max_concurrency:
            async with self._cond:
                self._max_concurrency = new_max
                if direction == "up":
                    self._cond.notify_all()


def _run_with_context(
    study_key: str,
    sdk: Any,
    study_logger: StudyContextLogAdapter,
    func: StudyWorkerCallable[Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    limiter: AdaptiveConcurrencyLimiter | None = None,
) -> Any:
    """Execute *func* inside the ``study_context()`` context manager.

    This ensures that any code inside *func* that calls
    :func:`~imednet.core.context.get_current_study` (or uses SDK endpoints
    without an explicit ``study_key`` argument) will correctly resolve to this
    thread's ``study_key``.
    """
    if limiter:
        limiter.acquire()
    try:
        with _study_context(study_key):
            return func(study_key, sdk, study_logger, *args, **kwargs)
    finally:
        if limiter:
            limiter.release()


async def _async_run_with_context(
    study_key: str,
    sdk: Any,
    study_logger: StudyContextLogAdapter,
    func: AsyncStudyWorkerCallable[Any] | StudyWorkerCallable[Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    limiter: AsyncAdaptiveConcurrencyLimiter | None = None,
) -> Any:
    """Execute an async or sync worker callable inside a task-scoped ``study_context()``."""
    if limiter is not None:
        await limiter.acquire()
    try:
        with _study_context(study_key):
            outcome = func(study_key, sdk, study_logger, *args, **kwargs)
            if inspect.isawaitable(outcome):
                return await outcome
            return outcome
    finally:
        if limiter is not None:
            await limiter.release()


class MultiStudyOrchestrator:
    """Orchestrates pipeline execution across multiple active clinical trial boundaries.

    The SDK instance passed at construction is treated as an **immutable read-only
    resource** — no worker thread or task may mutate its transport, authentication state,
    or connection pool during parallel execution.

    Args:
        sdk: A fully initialized :class:`~imednet.ImednetSDK` or
             :class:`~imednet.AsyncImednetSDK` instance. The orchestrator stores a
             reference (not a copy) and treats it as immutable.
        max_workers: Maximum number of concurrent worker threads or tasks. Defaults to 4.
             Set to 1 to force sequential execution (useful for debugging).

    Example::

        with ImednetSDK(api_key=..., security_key=...) as sdk:
            orchestrator = MultiStudyOrchestrator(sdk, max_workers=8)
            results = orchestrator.execute_pipeline(my_pipeline_func)
    """

    def __init__(self, sdk: Any, max_workers: int = 4) -> None:
        """Initialize the multi-study orchestrator.

        Args:
            sdk: An initialized iMednet SDK instance.
            max_workers: Maximum number of concurrent worker threads or async tasks.
        """
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        self._sdk = sdk
        self._max_workers = max_workers

    @property
    def sdk(self) -> Any:
        """The shared read-only SDK instance."""
        return self._sdk

    @property
    def max_workers(self) -> int:
        """Maximum number of concurrent worker threads or tasks."""
        return self._max_workers

    def resolve_active_studies(
        self,
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
    ) -> list[str]:
        """Query the iMednet registry synchronously and apply filtering rules.

        Calls ``self._sdk.studies.list()`` to fetch the live study inventory,
        then applies the whitelist OR blacklist (mutually exclusive).

        Args:
            whitelist: If provided, only studies whose ``studyKey`` is in this
                       set are included. Mutually exclusive with ``blacklist``.
            blacklist: If provided, studies whose ``studyKey`` is in this set
                       are excluded. Mutually exclusive with ``whitelist``.

        Returns:
            Ordered list of study key strings targeting this execution run.

        Raises:
            TypeError: When bound to an asynchronous SDK client.
            FilterConflictError: When both ``whitelist`` and ``blacklist`` are
                non-empty simultaneously.
        """
        _ensure_sync_orchestrator_client(
            self._sdk,
            "resolve_active_studies",
            "async_resolve_active_studies",
        )
        if whitelist and blacklist:
            raise FilterConflictError(whitelist=whitelist, blacklist=blacklist)

        all_studies = [
            study.study_key for study in self._sdk.studies.list() if study.study_key is not None
        ]
        return _apply_study_filters(all_studies, whitelist=whitelist, blacklist=blacklist)

    async def async_resolve_active_studies(
        self,
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
    ) -> list[str]:
        """Asynchronously query the iMednet registry and apply filtering rules.

        Args:
            whitelist: If provided, only studies whose ``studyKey`` is in this
                       set are included. Mutually exclusive with ``blacklist``.
            blacklist: If provided, studies whose ``studyKey`` is in this set
                       are excluded. Mutually exclusive with ``whitelist``.

        Returns:
            Ordered list of study key strings targeting this execution run.

        Raises:
            TypeError: When bound to a synchronous SDK client.
            FilterConflictError: When both ``whitelist`` and ``blacklist`` are
                non-empty simultaneously.
        """
        _ensure_async_orchestrator_client(
            self._sdk,
            "async_resolve_active_studies",
            "resolve_active_studies",
        )
        if whitelist and blacklist:
            raise FilterConflictError(whitelist=whitelist, blacklist=blacklist)

        studies_res = self._sdk.studies.list()
        if hasattr(studies_res, "__aiter__"):
            studies = [study async for study in studies_res]
        elif inspect.isawaitable(studies_res):
            studies = list(await studies_res)
        else:
            studies = list(studies_res)

        all_studies = [study.study_key for study in studies if study.study_key is not None]
        return _apply_study_filters(all_studies, whitelist=whitelist, blacklist=blacklist)

    def execute_pipeline(
        self,
        pipeline_func: StudyWorkerCallable[Any],
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, OrchestratorResult]:
        """Execute a pipeline function concurrently across resolved study contexts."""
        _ensure_sync_orchestrator_client(
            self._sdk,
            "execute_pipeline",
            "async_execute_pipeline",
        )
        target_studies = self.resolve_active_studies(
            whitelist=whitelist,
            blacklist=blacklist,
        )

        logger.info(
            "Initiating parallel pipeline execution across %d clinical studies "
            "with max_workers=%d.",
            len(target_studies),
            self._max_workers,
        )

        results: dict[str, OrchestratorResult] = {}
        start_times: dict[Future[Any], float] = {}
        limiter = AdaptiveConcurrencyLimiter(self._max_workers)

        executor = ThreadPoolExecutor(max_workers=self._max_workers)

        try:
            future_to_study: dict[Future[Any], str] = {}

            for study_key in target_studies:
                study_logger = make_study_logger(study_key)
                future = executor.submit(
                    _run_with_context,
                    study_key,
                    self._sdk,
                    study_logger,
                    pipeline_func,
                    args,
                    kwargs,
                    limiter,
                )
                future_to_study[future] = study_key
                start_times[future] = time.monotonic()

            try:
                for future in as_completed(future_to_study, timeout=_GLOBAL_TIMEOUT_SECONDS):
                    study_key = future_to_study[future]
                    duration = time.monotonic() - start_times[future]
                    limiter.record_latency(duration)

                    try:
                        data = future.result()
                        results[study_key] = _build_study_outcome_result(
                            study_key, duration, data=data
                        )
                    except Exception as exc:
                        results[study_key] = _build_study_outcome_result(
                            study_key, duration, exc=exc
                        )
            except TimeoutError:
                logger.error(
                    "Global orchestration timeout (%.2fs) reached. Aborting pending studies.",
                    _GLOBAL_TIMEOUT_SECONDS,
                )
                for future, study_key in future_to_study.items():
                    if not future.done():
                        duration = time.monotonic() - start_times[future]
                        results[study_key] = _build_timeout_result(duration)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return results

    async def async_execute_pipeline(
        self,
        pipeline_func: AsyncStudyWorkerCallable[Any] | StudyWorkerCallable[Any],
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, OrchestratorResult]:
        """Asynchronously execute a pipeline function concurrently across resolved studies.

        Each study executes in its own :class:`asyncio.Task` with an isolated
        ``contextvars`` study context and bounded concurrency controlled by
        ``max_workers``. Per-study exceptions are captured in the returned
        :class:`OrchestratorResult` dictionary without cancelling sibling studies.
        """
        _ensure_async_orchestrator_client(
            self._sdk,
            "async_execute_pipeline",
            "execute_pipeline",
        )
        target_studies = await self.async_resolve_active_studies(
            whitelist=whitelist,
            blacklist=blacklist,
        )

        logger.info(
            "Initiating async pipeline execution across %d clinical studies with max_workers=%d.",
            len(target_studies),
            self._max_workers,
        )

        if not target_studies:
            return {}

        results: dict[str, OrchestratorResult] = {}
        limiter = AsyncAdaptiveConcurrencyLimiter(self._max_workers)
        task_to_study: dict[asyncio.Task[Any], str] = {}
        start_times: dict[asyncio.Task[Any], float] = {}

        for study_key in target_studies:
            study_logger = make_study_logger(study_key)
            task: asyncio.Task[Any] = asyncio.create_task(
                _async_run_with_context(
                    study_key,
                    self._sdk,
                    study_logger,
                    pipeline_func,
                    args,
                    kwargs,
                    limiter,
                )
            )
            task_to_study[task] = study_key
            start_times[task] = time.monotonic()

        pending: set[asyncio.Task[Any]] = set(task_to_study.keys())
        deadline = time.monotonic() + _GLOBAL_TIMEOUT_SECONDS

        try:
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError

                done, pending = await asyncio.wait(
                    pending,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done and pending:
                    raise TimeoutError

                for completed_task in done:
                    study_key = task_to_study[completed_task]
                    duration = time.monotonic() - start_times[completed_task]
                    await limiter.record_latency(duration)
                    try:
                        data = completed_task.result()
                        results[study_key] = _build_study_outcome_result(
                            study_key, duration, data=data
                        )
                    except Exception as exc:
                        results[study_key] = _build_study_outcome_result(
                            study_key, duration, exc=exc
                        )
        except TimeoutError:
            logger.error(
                "Global orchestration timeout (%.2fs) reached. Aborting pending studies.",
                _GLOBAL_TIMEOUT_SECONDS,
            )
            for pending_task in pending:
                study_key = task_to_study[pending_task]
                duration = time.monotonic() - start_times[pending_task]
                results[study_key] = _build_timeout_result(duration)
                pending_task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        return results


__all__ = ["MultiStudyOrchestrator"]
