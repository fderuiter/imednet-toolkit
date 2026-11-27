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
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any
from unittest.mock import NonCallableMock

from imednet.core.context import sdk_governance_context as _sdk_governance_context
from imednet.core.context import study_context as _study_context
from imednet.errors.orchestration import FilterConflictError
from imednet.orchestration.logging import StudyContextLogAdapter, make_study_logger
from imednet.orchestration.types import (
    AsyncStudyWorkerCallable,
    OrchestratorResult,
    StudyWorkerCallable,
    T_Output,
)
from imednet.utils.async_bridge import (
    SyncToAsyncFacadeAdapter,
    call_async_facade_method,
    is_async_facade,
    maybe_to_thread,
    run_sync_over_async,
)

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

logger = logging.getLogger(__name__)
_DURATION_PRECISION = 4


class AdaptiveConcurrencyLimiter:
    """Manages adaptive backpressure by limiting concurrency based on task latency."""

    def __init__(self, initial_concurrency: int) -> None:
        """Initialize the adaptive concurrency limiter.

        Args:
            initial_concurrency: The starting maximum number of concurrent tasks.
        """
        self._max_concurrency = initial_concurrency
        self._current_concurrency = 0
        self._cond = threading.Condition()
        self._async_cond: asyncio.Condition | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._latency_baseline: float | None = None
        self._lock = threading.Lock()
        self._min_concurrency = 1
        self._initial_concurrency = initial_concurrency

    def _get_async_cond(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._async_cond is None or self._async_loop is not loop:
            self._async_cond = asyncio.Condition()
            self._async_loop = loop
        return self._async_cond

    def acquire(self) -> None:
        """Acquire a concurrency slot synchronously, blocking if the limit is reached."""
        with self._cond:
            while self._current_concurrency >= self._max_concurrency:
                self._cond.wait()
            self._current_concurrency += 1

    def release(self) -> None:
        """Release a concurrency slot synchronously and notify waiting threads."""
        with self._cond:
            self._current_concurrency -= 1
            self._cond.notify_all()

    async def acquire_async(self) -> None:
        """Asynchronously acquire a concurrency slot on the active event loop."""
        cond = self._get_async_cond()
        async with cond:
            while self._current_concurrency >= self._max_concurrency:
                await cond.wait()
            self._current_concurrency += 1

    async def release_async(self) -> None:
        """Asynchronously release a concurrency slot and wake waiting tasks."""
        cond = self._get_async_cond()
        async with cond:
            self._current_concurrency -= 1
            cond.notify_all()

    def record_latency(self, latency: float) -> None:
        """Record the latency of a completed task and adjust concurrency.

        Args:
            latency: The time taken to complete the task in seconds.
        """
        increased = False
        with self._lock:
            if self._latency_baseline is None:
                self._latency_baseline = latency
            else:
                if latency > self._latency_baseline * 2.0:
                    with self._cond:
                        new_max = max(self._min_concurrency, self._max_concurrency - 1)
                        if new_max < self._max_concurrency:
                            logger.warning(
                                "Adaptive backpressure: downstream latency increased "
                                "(%.2fs vs baseline %.2fs). Reducing max concurrency to %d.",
                                latency,
                                self._latency_baseline,
                                new_max,
                            )
                            self._max_concurrency = new_max
                elif latency <= self._latency_baseline * 1.2:
                    with self._cond:
                        new_max = min(self._initial_concurrency, self._max_concurrency + 1)
                        if new_max > self._max_concurrency:
                            logger.info(
                                "Adaptive backpressure: downstream latency recovered. "
                                "Increasing max concurrency to %d.",
                                new_max,
                            )
                            self._max_concurrency = new_max
                            self._cond.notify_all()
                            increased = True

                self._latency_baseline = 0.8 * self._latency_baseline + 0.2 * latency

        if increased and self._async_cond is not None and self._async_loop is not None:
            async_cond = self._async_cond

            async def _wake() -> None:
                async with async_cond:
                    async_cond.notify_all()

            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is self._async_loop:
                running.create_task(_wake())


async def _fetch_study_keys_async(sdk: AsyncImednetFacade) -> list[str]:
    """Fetch all study keys through the async studies endpoint or facade method."""
    studies_ep: Any = None
    if isinstance(sdk, NonCallableMock):
        mock_children = getattr(sdk, "_mock_children", {})
        obj_vars = vars(sdk)
        if "studies" in mock_children or "studies" in obj_vars:
            studies_ep = sdk.studies
    else:
        studies_ep = getattr(sdk, "studies", None)

    if studies_ep is not None and callable(getattr(studies_ep, "list", None)):
        raw = studies_ep.list()
        if hasattr(raw, "__aiter__"):
            studies_items = [item async for item in raw]
        elif inspect.isawaitable(raw):
            resolved = await raw
            studies_items = list(resolved) if isinstance(resolved, Iterable) else []
        elif isinstance(raw, Iterable):
            studies_items = list(raw)
        else:
            studies_items = []
    else:
        studies_items = list(await call_async_facade_method(sdk, "get_studies"))

    return [str(study.study_key) for study in studies_items]


class AsyncMultiStudyOrchestrator:
    """Canonical asynchronous multi-study pipeline engine.

    Executes asynchronous pipeline coroutines concurrently across active clinical
    studies resolved via :class:`~imednet.spi.facade.AsyncImednetFacade`, enforcing
    per-task ``study_context`` isolation, adaptive latency-based backpressure, and
    fault isolation.
    """

    def __init__(self, sdk: AsyncImednetFacade, max_workers: int = 4) -> None:
        """Initialize the asynchronous multi-study orchestrator.

        Args:
            sdk: An initialized :class:`AsyncImednetSDK` or :class:`AsyncImednetFacade`.
            max_workers: Maximum number of concurrent study tasks. Defaults to 4.
        """
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        if type(sdk).__name__ == "ImednetSDK" or (
            not isinstance(sdk, (SyncToAsyncFacadeAdapter, NonCallableMock))
            and not is_async_facade(sdk)
        ):
            raise TypeError(
                "AsyncMultiStudyOrchestrator requires an AsyncImednetSDK or AsyncImednetFacade "
                "instance; for synchronous ImednetSDK, use MultiStudyOrchestrator(sdk)."
            )
        self._sdk: AsyncImednetFacade = sdk
        self._max_workers = max_workers

    @property
    def sdk(self) -> AsyncImednetFacade:
        """The shared read-only asynchronous SDK facade."""
        return self._sdk

    @property
    def max_workers(self) -> int:
        """Maximum number of concurrent study tasks."""
        return self._max_workers

    async def resolve_active_studies(
        self,
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
    ) -> list[str]:
        """Asynchronously query the iMednet study registry and apply filtering rules."""
        if whitelist and blacklist:
            raise FilterConflictError(whitelist=whitelist, blacklist=blacklist)

        all_studies = await _fetch_study_keys_async(self._sdk)
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

    async def execute_pipeline(
        self,
        pipeline_func: (AsyncStudyWorkerCallable[T_Output] | Callable[..., Awaitable[T_Output]]),
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, OrchestratorResult]:
        """Asynchronously execute a pipeline coroutine concurrently across resolved studies."""
        target_studies = await self.resolve_active_studies(
            whitelist=whitelist,
            blacklist=blacklist,
        )

        logger.info(
            "Initiating parallel pipeline execution across %d clinical studies "
            "with max_workers=%d.",
            len(target_studies),
            self._max_workers,
        )

        if not target_studies:
            return {}

        results: dict[str, OrchestratorResult] = {}
        start_times: dict[asyncio.Task[Any], float] = {}
        task_to_study: dict[asyncio.Task[Any], str] = {}
        limiter = AdaptiveConcurrencyLimiter(self._max_workers)
        timeout_seconds = 300.0
        gov_target: Any = (
            self._sdk.sync_sdk if isinstance(self._sdk, SyncToAsyncFacadeAdapter) else self._sdk
        )

        async def _run_study_task(
            study_key: str,
            study_logger: StudyContextLogAdapter,
        ) -> T_Output:
            await limiter.acquire_async()
            try:
                with _sdk_governance_context(gov_target), _study_context(study_key):
                    return await pipeline_func(
                        study_key,
                        self._sdk,
                        study_logger,
                        *args,
                        **kwargs,
                    )
            finally:
                await limiter.release_async()

        for study_key in target_studies:
            study_logger = make_study_logger(study_key)
            task: asyncio.Task[Any] = asyncio.create_task(_run_study_task(study_key, study_logger))
            task_to_study[task] = study_key
            start_times[task] = time.monotonic()

        done, pending = await asyncio.wait(
            task_to_study.keys(),
            timeout=timeout_seconds,
        )

        for task in done:
            study_key = task_to_study[task]
            duration = time.monotonic() - start_times[task]
            limiter.record_latency(duration)
            try:
                data = task.result()
                results[study_key] = OrchestratorResult(
                    status="SUCCESS",
                    data=data,
                    error=None,
                    duration_seconds=round(duration, _DURATION_PRECISION),
                )
                logger.info(
                    "[%s] Pipeline completed successfully in %.2fs.",
                    study_key,
                    duration,
                )
            except Exception as exc:
                results[study_key] = OrchestratorResult(
                    status="FAILED",
                    data=None,
                    error=repr(exc),
                    duration_seconds=round(duration, _DURATION_PRECISION),
                )
                logger.error(
                    "[%s] Pipeline execution failed after %.2fs: %s",
                    study_key,
                    duration,
                    repr(exc),
                    exc_info=exc,
                )

        if pending:
            logger.error(
                "Global orchestration timeout (%.2fs) reached. Aborting pending studies.",
                timeout_seconds,
            )
            for task in pending:
                task.cancel()
                study_key = task_to_study[task]
                duration = time.monotonic() - start_times[task]
                results[study_key] = OrchestratorResult(
                    status="FAILED",
                    data=None,
                    error="TimeoutError: Global orchestration timeout reached.",
                    duration_seconds=round(duration, _DURATION_PRECISION),
                )
            await asyncio.gather(*pending, return_exceptions=True)

        return results


class MultiStudyOrchestrator:
    """Synchronous multi-study orchestrator wrapping :class:`AsyncMultiStudyOrchestrator`.

    Strictly typed against :class:`~imednet.spi.facade.ImednetFacade` while sharing
    the canonical async execution, study filtering, context isolation, and adaptive
    backpressure engine. Safely executes even when called from an active event loop
    in Jupyter or Pyodide.
    """

    def __init__(self, sdk: ImednetFacade, max_workers: int = 4) -> None:
        """Initialize the multi-study orchestrator.

        Args:
            sdk: An initialized synchronous :class:`ImednetSDK` or :class:`ImednetFacade`.
            max_workers: Maximum number of concurrent worker threads. Defaults to 4.

        Raises:
            ValueError: If ``max_workers < 1``.
            TypeError: If an :class:`AsyncImednetSDK` or async facade is provided.
        """
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        if is_async_facade(sdk):
            raise TypeError(
                "MultiStudyOrchestrator requires a synchronous ImednetSDK or ImednetFacade "
                "instance; for AsyncImednetSDK, use AsyncMultiStudyOrchestrator(sdk)."
            )
        self._sdk: ImednetFacade = sdk
        self._max_workers = max_workers
        self._adapter = SyncToAsyncFacadeAdapter(sdk, offload_threads=False)
        self._async_orchestrator = AsyncMultiStudyOrchestrator(
            self._adapter,
            max_workers=max_workers,
        )

    @property
    def sdk(self) -> ImednetFacade:
        """The shared read-only synchronous SDK facade."""
        return self._sdk

    @property
    def max_workers(self) -> int:
        """Maximum number of concurrent worker threads."""
        return self._max_workers

    def resolve_active_studies(
        self,
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
    ) -> list[str]:
        """Query the iMednet registry and apply whitelist/blacklist filtering rules."""
        return run_sync_over_async(
            lambda: self._async_orchestrator.resolve_active_studies(
                whitelist=whitelist,
                blacklist=blacklist,
            )
        )

    def execute_pipeline(
        self,
        pipeline_func: (StudyWorkerCallable[T_Output] | Callable[..., T_Output]),
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, OrchestratorResult]:
        """Execute a synchronous pipeline function concurrently across resolved studies."""
        sync_sdk = self._sdk

        async def _async_worker(
            study_key: str,
            _adapted_sdk: AsyncImednetFacade,
            study_logger: StudyContextLogAdapter,
            *worker_args: Any,
            **worker_kwargs: Any,
        ) -> T_Output:
            return await maybe_to_thread(
                pipeline_func,
                study_key,
                sync_sdk,
                study_logger,
                *worker_args,
                **worker_kwargs,
            )

        return run_sync_over_async(
            lambda: self._async_orchestrator.execute_pipeline(
                _async_worker,
                whitelist,
                blacklist,
                *args,
                **kwargs,
            )
        )


__all__ = ["AsyncMultiStudyOrchestrator", "MultiStudyOrchestrator"]
