"""Utility for polling job status."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from imednet.models.jobs import (
    InvalidJobStateError,
    InvalidJobTransitionError,
    InvalidStateTransitionError,
    JobState,
    JobStatus,
    normalize_job_state,
    validate_job_transition,
)
from imednet.utils.secrets import redact_sensitive_payload

logger = logging.getLogger(__name__)

__all__ = [
    "AsyncJobPoller",
    "BaseJobPoller",
    "InvalidJobStateError",
    "InvalidJobTransitionError",
    "InvalidStateTransitionError",
    "JobFailedError",
    "JobPollSummary",
    "JobPoller",
    "JobProgressCallback",
    "JobProgressHandler",
    "JobStatusEvent",
    "JobTimeoutError",
    "evaluate_job_state",
]


@runtime_checkable
class JobProgressCallback(Protocol):
    """Callable invoked on each poll cycle with the current job status.

    Parameters
    ----------
    batch_id : str
        The batch ID being polled.
    status : JobStatus
        The current status object from the jobs endpoint.
    elapsed : float
        Seconds elapsed since polling started for this job.
    """

    def __call__(
        self,
        batch_id: str,
        status: JobStatus,
        elapsed: float,
    ) -> None:
        """Invoke the progress callback."""
        ...


@dataclass(frozen=True)
class JobStatusEvent:
    """Structured event for job status updates."""

    batch_id: str
    status: JobStatus
    poll_number: int  # 1-based poll attempt count
    elapsed: float  # seconds since polling started
    is_terminal: bool  # shortcut: status.is_terminal


JobProgressHandler = JobProgressCallback | Callable[[JobStatusEvent], None]


@dataclass
class JobPollSummary:
    """Aggregate result of polling multiple jobs."""

    study_key: str
    results: dict[str, JobStatus]  # batch_id → final JobStatus
    failures: dict[str, Exception]  # batch_id → exception (timeout or API error)
    elapsed_total: float

    @property
    def all_successful(self) -> bool:
        """Return True if all jobs were successful with zero failed records."""
        return not self.failures and all(
            cast(Any, s).is_successful
            and getattr(s, "failed_records", 0) == 0
            and not getattr(s, "result_error", None)
            for s in self.results.values()
        )

    @property
    def any_failed(self) -> bool:
        """Return True if any job failed, timed out, or reported failed records."""
        return bool(self.failures) or any(
            cast(Any, s).is_failed
            or getattr(s, "failed_records", 0) > 0
            or bool(getattr(s, "result_error", None))
            for s in self.results.values()
        )


class JobTimeoutError(TimeoutError):
    """Raised when a job does not finish before the timeout."""


def _extract_record_errors(status: JobStatus | Any) -> list[dict[str, Any]]:
    """Extract sanitized per-record error entries from a JobStatus results payload."""
    results = getattr(status, "results", None)
    extracted: list[dict[str, Any]] = []
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            item_status = str(item.get("status") or "").upper()
            has_err_key = bool(item.get("error") or item.get("errors"))
            if item_status in {"FAIL", "FAILED", "ERROR"} or (not item_status and has_err_key):
                extracted.append(cast(dict[str, Any], redact_sensitive_payload(item)))
    elif isinstance(results, dict):
        nested_errors = results.get("errors")
        if isinstance(nested_errors, list):
            for err_item in nested_errors:
                if isinstance(err_item, dict):
                    extracted.append(cast(dict[str, Any], redact_sensitive_payload(err_item)))
                elif err_item is not None:
                    extracted.append({"error": str(redact_sensitive_payload(err_item))})
        elif results.get("error"):
            extracted.append(cast(dict[str, Any], redact_sensitive_payload(results)))
    return extracted


def _is_fetchable_result_url(url: Any) -> bool:
    """Return True when ``resultUrl`` is a non-empty, non-placeholder string."""
    if not isinstance(url, str):
        return False
    stripped = url.strip()
    if not stripped:
        return False
    return not (stripped.startswith("<") and stripped.endswith(">"))


def _format_job_failure_details(
    base_message: str,
    status: JobStatus | Any,
    record_errors: list[dict[str, Any]],
    result_error: str | None,
) -> str:
    """Append record failure and resultUrl error diagnostics to a base message."""
    extras: list[str] = []
    failed_count = getattr(status, "failed_records", 0)
    if isinstance(failed_count, int) and failed_count > 0:
        extras.append(f"failed_records={failed_count}")
    if result_error and result_error not in base_message:
        extras.append(f"result_error={result_error}")
    if record_errors:
        summaries: list[str] = []
        for entry in record_errors[:5]:
            detail = (
                entry.get("error")
                or entry.get("message")
                or entry.get("description")
                or entry.get("errors")
                or entry
            )
            summaries.append(str(detail))
        extras.append(f"record_errors={summaries}")
    if extras:
        return f"{base_message} ({'; '.join(extras)})"
    return base_message


class JobFailedError(Exception):
    """Raised when a job fails, is cancelled, has failed records, or fails fetching resultUrl."""

    def __init__(
        self,
        message: str,
        status: JobStatus,
        *,
        record_errors: list[dict[str, Any]] | None = None,
        result_error: str | None = None,
    ) -> None:
        """Initialize the JobFailedError with attached record diagnostics."""
        resolved_errors = (
            record_errors if record_errors is not None else _extract_record_errors(status)
        )
        raw_res_err = (
            result_error if result_error is not None else getattr(status, "result_error", None)
        )
        resolved_result_error = (
            str(redact_sensitive_payload(raw_res_err))
            if isinstance(raw_res_err, str) and raw_res_err
            else None
        )
        formatted_message = _format_job_failure_details(
            message,
            status,
            resolved_errors,
            resolved_result_error,
        )
        super().__init__(formatted_message)
        self.status = status
        self.record_errors = resolved_errors
        self.result_error = resolved_result_error


def evaluate_job_state(
    job: JobStatus | Any,
    previous_state: JobState | str | None = None,
) -> bool:
    """Evaluate a job's status.

    Verifies monotonic state progression before raising errors.

    Args:
        job: The job or job status object to evaluate.
        previous_state: The optional previous state to verify monotonic progression from.

    Returns:
        True if the job completed successfully.
        False if the job is still in progress.

    Raises:
        InvalidJobStateError: If the job state string is unrecognized.
        InvalidJobTransitionError: If the transition from previous_state to current state is invalid.
        JobFailedError: If the job ended in a failed or cancelled state.
    """
    current_state_val = getattr(job, "state", None)
    if isinstance(current_state_val, (str, JobState)):
        normalize_job_state(current_state_val)

    # Determine previous state (explicit parameter takes precedence over attribute)
    prev = previous_state
    if prev is None:
        candidate = getattr(job, "previous_state", None)
        if isinstance(candidate, (str, JobState)):
            prev = candidate

    if prev is not None and isinstance(current_state_val, (str, JobState)):
        validate_job_transition(prev, current_state_val)

    failed_count = getattr(job, "failed_records", 0)
    has_failed_records = (
        getattr(job, "is_terminal", False) and isinstance(failed_count, int) and failed_count > 0
    )
    res_err = getattr(job, "result_error", None)
    has_result_error = (
        getattr(job, "is_terminal", False) and isinstance(res_err, str) and bool(res_err)
    )
    if getattr(job, "is_failed", False) or has_failed_records or has_result_error:
        batch_id = getattr(job, "batch_id", getattr(job, "batchId", "unknown"))
        raise JobFailedError(f"Job {batch_id} ended in state {job.state}", job)
    return getattr(job, "is_terminal", False)


class BaseJobPoller:
    """Base class for polling a job until it reaches a terminal state."""

    def __init__(self) -> None:
        """Initialize transition tracking ledger."""
        self._terminal_states: dict[str, JobStatus] = {}
        self._lock = threading.Lock()

    def _guard_transition(
        self,
        batch_id: str,
        status: JobStatus,
        last_status: JobStatus | None,
        terminal_status: JobStatus | None,
    ) -> tuple[JobStatus, JobStatus | None]:
        """Guard against state regressions and validate monotonic transitions.

        If a terminal state was already reached, stale non-terminal payloads are ignored
        and the terminal status is preserved.

        Args:
            batch_id: The job batch identifier.
            status: The newly polled JobStatus payload.
            last_status: The most recent status observed in this polling run.
            terminal_status: The terminal status if one has already been reached.

        Returns:
            A tuple of (effective_status, effective_terminal_status).
        """
        # If terminal state was already recorded, ignore stale non-terminal responses
        if terminal_status is not None and getattr(terminal_status, "is_terminal", False):
            if not getattr(status, "is_terminal", False):
                logger.warning(
                    "[JobPoller] Ignoring stale response for batch_id=%s in state %s; terminal state %s preserved",
                    batch_id,
                    getattr(status, "state", None),
                    terminal_status.state,
                )
                return terminal_status, terminal_status
            terminal_status = status
            with self._lock:
                self._terminal_states[batch_id] = status
            return status, terminal_status

        # If the newly polled status is terminal, record it
        if getattr(status, "is_terminal", False):
            terminal_status = status
            with self._lock:
                self._terminal_states[batch_id] = status
            return status, terminal_status

        # If both previous and new status are active, validate monotonic transition
        last_state = getattr(last_status, "state", None) if last_status else None
        curr_state = getattr(status, "state", None)
        if last_state is not None and curr_state is not None:
            validate_job_transition(last_state, curr_state)

        return status, terminal_status

    def _check_complete(self, status: JobStatus, batch_id: str) -> JobStatus:
        """Internal hook to check if the status is complete."""
        return status

    def _evaluate(
        self, start_time: float, timeout: float, batch_id: str, status: JobStatus
    ) -> bool:
        """Evaluate if the job is complete, returning True if terminal."""
        if getattr(status, "is_terminal", False):
            return True
        if time.monotonic() - start_time >= timeout:
            raise JobTimeoutError(f"Timeout ({timeout}s) waiting for job {batch_id}")
        return False

    def _process_fetch_result(self, status: JobStatus, res: Any) -> JobStatus:
        """Process the fetched result data and attach to the status object."""
        if hasattr(res, "json"):
            try:
                status.results = res.json()
            except Exception:
                status.results = getattr(res, "text", str(res))
        else:
            status.results = res
        return status

    def _trigger_callback(
        self,
        on_progress: JobProgressHandler | None,
        event: JobStatusEvent,
    ) -> None:
        """Invoke the progress callback with signature detection."""
        if on_progress is None:
            return
        legacy_args = (event.batch_id, event.status, event.elapsed)
        try:
            signature = inspect.signature(on_progress)
        except (TypeError, ValueError):
            # Opaque builtins use the original three-argument contract, exactly once.
            cast(JobProgressCallback, on_progress)(*legacy_args)
            return
        try:
            signature.bind(*legacy_args)
        except TypeError:
            signature.bind(event)
            cast(Callable[[JobStatusEvent], None], on_progress)(event)
        else:
            cast(JobProgressCallback, on_progress)(*legacy_args)


class JobPoller(BaseJobPoller):
    """Synchronously poll a job until completion."""

    def __init__(
        self,
        get_job: Callable[[str, str], JobStatus],
        fetch_result: Callable[[str], Any] | None = None,
    ) -> None:
        """Initialize the synchronous job poller.

        Args:
            get_job: Callable that takes study_key and batch_id and returns JobStatus.
            fetch_result: Optional callable to fetch the result data from the result URL.
        """
        super().__init__()
        self._get_job = get_job
        self._fetch_result = fetch_result

    def run(
        self,
        study_key: str,
        batch_id: str,
        interval: float = 5.0,
        timeout: float = 300.0,
        *,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Synchronously poll a job until completion."""
        start = time.monotonic()
        poll_number = 0
        last_status: JobStatus | None = None
        with self._lock:
            terminal_status: JobStatus | None = self._terminal_states.get(batch_id)

        while True:
            poll_number += 1
            result = self._get_job(study_key, batch_id)
            status = self._check_complete(result, batch_id)
            status, terminal_status = self._guard_transition(
                batch_id, status, last_status, terminal_status
            )
            last_status = status
            elapsed = time.monotonic() - start
            is_terminal = self._evaluate(start, timeout, batch_id, status)

            event = JobStatusEvent(
                batch_id=batch_id,
                status=status,
                poll_number=poll_number,
                elapsed=elapsed,
                is_terminal=is_terminal,
            )

            # Structured logging
            logger.debug(
                "[JobPoller] batch_id=%s state=%s progress=%s%% elapsed=%ss",
                batch_id,
                status.state,
                getattr(status, "progress", 0),
                round(elapsed, 1),
            )

            self._trigger_callback(on_progress, event)

            if is_terminal:
                if self._fetch_result and _is_fetchable_result_url(
                    getattr(status, "result_url", None)
                ):
                    try:
                        res = self._fetch_result(status.result_url)  # type: ignore
                        self._process_fetch_result(status, res)
                    except Exception as exc:
                        redacted_err = str(redact_sensitive_payload(str(exc)))
                        status.result_error = redacted_err
                        logger.error(
                            "[JobPoller] batch_id=%s FAILED fetching resultUrl: %s",
                            batch_id,
                            redacted_err,
                        )
                        raise JobFailedError(
                            f"Job {batch_id} failed to fetch resultUrl: {redacted_err}",
                            status,
                            result_error=redacted_err,
                        ) from exc

                if cast(Any, status).is_successful:
                    logger.info(
                        "[JobPoller] batch_id=%s COMPLETED after %s polls (%ss)",
                        batch_id,
                        poll_number,
                        round(elapsed, 1),
                    )
                else:
                    logger.error(
                        "[JobPoller] batch_id=%s FAILED: state=%s failed_records=%s",
                        batch_id,
                        status.state,
                        getattr(status, "failed_records", 0),
                    )

                evaluate_job_state(status)
                return status

            time.sleep(interval)

    def poll_many(
        self,
        study_key: str,
        batch_ids: list[str],
        interval: float = 5.0,
        timeout: float = 300.0,
        *,
        on_progress: JobProgressHandler | None = None,
        fail_fast: bool = False,
    ) -> JobPollSummary:
        """Poll multiple jobs concurrently using threading.

        Parameters
        ----------
        study_key : str
            The study key.
        batch_ids : List[str]
            The batch IDs to monitor.
        interval : float
            Seconds between poll cycles.
        timeout : float
            Seconds before a job is considered timed out.
        fail_fast : bool
            If True, raise JobTimeoutError as soon as any single job times out.
            If False (default), continue polling remaining jobs and collect all
            failures into JobPollSummary.failures.
        on_progress : Optional[JobProgressCallback]
            Called for each job on each poll cycle. Thread-safe: the callback
            must be safe to call from multiple threads simultaneously.
        """
        results: dict[str, JobStatus] = {}
        failures: dict[str, Exception] = {}
        lock = threading.Lock()
        start = time.monotonic()

        def _poll_one(batch_id: str) -> None:
            try:
                status = self.run(
                    study_key,
                    batch_id,
                    interval=interval,
                    timeout=timeout,
                    on_progress=on_progress,
                )
                with lock:
                    if (
                        batch_id in results
                        and getattr(results[batch_id], "is_terminal", False)
                        and not getattr(status, "is_terminal", False)
                    ):
                        pass
                    else:
                        results[batch_id] = status
            except Exception as exc:
                with lock:
                    failures[batch_id] = exc
                # If fail_fast is true, we still let the thread exit gracefully.
                # The exception is raised in the main thread after joining.

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            list(executor.map(_poll_one, batch_ids))

        if fail_fast and failures:
            raise next(iter(failures.values()))

        return JobPollSummary(
            study_key=study_key,
            results=results,
            failures=failures,
            elapsed_total=time.monotonic() - start,
        )


class AsyncJobPoller(BaseJobPoller):
    """Asynchronously poll a job until completion."""

    def __init__(
        self,
        get_job: Callable[[str, str], Awaitable[JobStatus]],
        fetch_result: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        """Initialize the asynchronous job poller.

        Args:
            get_job: Awaitable callable that takes study_key and batch_id and returns JobStatus.
            fetch_result: Optional awaitable callable to fetch the result data.
        """
        super().__init__()
        self._get_job = get_job
        self._fetch_result = fetch_result

    async def run(
        self,
        study_key: str,
        batch_id: str,
        interval: float = 5.0,
        timeout: float = 300.0,
        *,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Asynchronously poll a job until completion."""
        start = time.monotonic()
        poll_number = 0
        last_status: JobStatus | None = None
        with self._lock:
            terminal_status: JobStatus | None = self._terminal_states.get(batch_id)

        while True:
            poll_number += 1
            result = await self._get_job(study_key, batch_id)
            status = self._check_complete(result, batch_id)
            status, terminal_status = self._guard_transition(
                batch_id, status, last_status, terminal_status
            )
            last_status = status
            elapsed = time.monotonic() - start
            is_terminal = self._evaluate(start, timeout, batch_id, status)

            event = JobStatusEvent(
                batch_id=batch_id,
                status=status,
                poll_number=poll_number,
                elapsed=elapsed,
                is_terminal=is_terminal,
            )

            # Structured logging
            logger.debug(
                "[JobPoller] batch_id=%s state=%s progress=%s%% elapsed=%ss",
                batch_id,
                status.state,
                getattr(status, "progress", 0),
                round(elapsed, 1),
            )

            self._trigger_callback(on_progress, event)

            if is_terminal:
                if self._fetch_result and _is_fetchable_result_url(
                    getattr(status, "result_url", None)
                ):
                    try:
                        res = await self._fetch_result(status.result_url)  # type: ignore
                        self._process_fetch_result(status, res)
                    except Exception as exc:
                        redacted_err = str(redact_sensitive_payload(str(exc)))
                        status.result_error = redacted_err
                        logger.error(
                            "[JobPoller] batch_id=%s FAILED fetching resultUrl: %s",
                            batch_id,
                            redacted_err,
                        )
                        raise JobFailedError(
                            f"Job {batch_id} failed to fetch resultUrl: {redacted_err}",
                            status,
                            result_error=redacted_err,
                        ) from exc

                if cast(Any, status).is_successful:
                    logger.info(
                        "[JobPoller] batch_id=%s COMPLETED after %s polls (%ss)",
                        batch_id,
                        poll_number,
                        round(elapsed, 1),
                    )
                else:
                    logger.error(
                        "[JobPoller] batch_id=%s FAILED: state=%s failed_records=%s",
                        batch_id,
                        status.state,
                        getattr(status, "failed_records", 0),
                    )

                evaluate_job_state(status)
                return status

            await asyncio.sleep(interval)

    async def async_poll_many(
        self,
        study_key: str,
        batch_ids: list[str],
        interval: float = 5.0,
        timeout: float = 300.0,
        *,
        on_progress: JobProgressHandler | None = None,
        fail_fast: bool = False,
    ) -> JobPollSummary:
        """Asynchronously poll multiple jobs concurrently via asyncio.gather.

        All jobs are polled in parallel. Results are collected regardless of
        individual failures unless fail_fast=True.
        """
        start = time.monotonic()
        results: dict[str, JobStatus] = {}
        failures: dict[str, Exception] = {}

        async def _poll_one(batch_id: str) -> None:
            try:
                status = await self.run(
                    study_key,
                    batch_id,
                    interval=interval,
                    timeout=timeout,
                    on_progress=on_progress,
                )
                if (
                    batch_id in results
                    and getattr(results[batch_id], "is_terminal", False)
                    and not getattr(status, "is_terminal", False)
                ):
                    pass
                else:
                    results[batch_id] = status
            except Exception as exc:
                failures[batch_id] = exc
                if fail_fast:
                    raise

        # We use return_exceptions=not fail_fast so we can collect failures manually
        await asyncio.gather(
            *[_poll_one(bid) for bid in batch_ids], return_exceptions=not fail_fast
        )

        return JobPollSummary(
            study_key=study_key,
            results=results,
            failures=failures,
            elapsed_total=time.monotonic() - start,
        )
