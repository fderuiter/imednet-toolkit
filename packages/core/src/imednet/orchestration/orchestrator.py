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

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any

from imednet.core.context import sdk_governance_context as _sdk_governance_context
from imednet.core.context import study_context as _study_context
from imednet.errors.orchestration import FilterConflictError
from imednet.orchestration.logging import StudyContextLogAdapter, make_study_logger
from imednet.orchestration.types import OrchestratorResult, StudyWorkerCallable

logger = logging.getLogger(__name__)
_DURATION_PRECISION = 4


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
            if self._latency_baseline is None:
                self._latency_baseline = latency
            else:
                # If latency is significantly higher than baseline (e.g., 2x), reduce concurrency
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
                # If latency is good, slowly recover concurrency
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

                # Update baseline with exponential moving average
                self._latency_baseline = 0.8 * self._latency_baseline + 0.2 * latency


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
        with _sdk_governance_context(sdk), _study_context(study_key):
            return func(study_key, sdk, study_logger, *args, **kwargs)
    finally:
        if limiter:
            limiter.release()


class MultiStudyOrchestrator:
    """Orchestrates pipeline execution across multiple active clinical trial boundaries.

    The SDK instance passed at construction is treated as an **immutable read-only
    resource** — no worker thread may mutate its transport, authentication state,
    or connection pool during parallel execution.

    Args:
        sdk: A fully initialized :class:`~imednet.ImednetSDK` instance. The
             orchestrator stores a reference (not a copy) and treats it as immutable.
        max_workers: Maximum number of concurrent worker threads. Defaults to 4.
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
            max_workers: Maximum number of concurrent worker threads.
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
        """Maximum number of concurrent worker threads."""
        return self._max_workers

    def resolve_active_studies(
        self,
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
    ) -> list[str]:
        """Query the iMednet registry and apply filtering rules.

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
            FilterConflictError: When both ``whitelist`` and ``blacklist`` are
                non-empty simultaneously.
        """
        if whitelist and blacklist:
            raise FilterConflictError(whitelist=whitelist, blacklist=blacklist)

        all_studies = [study.study_key for study in self._sdk.studies.list()]
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

    def execute_pipeline(
        self,
        pipeline_func: StudyWorkerCallable[Any],
        whitelist: set[str] | None = None,
        blacklist: set[str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, OrchestratorResult]:
        """Execute a pipeline function concurrently across resolved study contexts."""
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

        timeout_seconds = 300.0  # Mandatory maximum timeout for concurrent groups
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
                for future in as_completed(future_to_study, timeout=timeout_seconds):
                    study_key = future_to_study[future]
                    duration = time.monotonic() - start_times[future]

                    # Record latency for backpressure
                    limiter.record_latency(duration)

                    try:
                        data = future.result()
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
                            exc_info=True,
                        )
            except TimeoutError:
                logger.error(
                    "Global orchestration timeout (%.2fs) reached. Aborting pending studies.",
                    timeout_seconds,
                )
                for future, study_key in future_to_study.items():
                    if not future.done():
                        duration = time.monotonic() - start_times[future]
                        results[study_key] = OrchestratorResult(
                            status="FAILED",
                            data=None,
                            error="TimeoutError: Global orchestration timeout reached.",
                            duration_seconds=round(duration, _DURATION_PRECISION),
                        )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return results


__all__ = ["MultiStudyOrchestrator"]
