"""Utility for polling job status."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol, cast, runtime_checkable

from imednet.models.jobs import (
    InvalidJobStateError,
    InvalidJobTransitionError,
    InvalidStateTransitionError,
    JobState,
    JobStatus,
    normalize_job_state,
    validate_job_transition,
)
from imednet.utils.security import (
    sanitize_diagnostic_payload,
    sanitize_diagnostic_text,
)
from imednet.utils.url import validate_path_segment

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
_ORIGINAL_TIME_SLEEP = time.sleep


class _PollAbortedError(Exception):
    """Internal cooperative stop signal for sibling synchronous pollers."""


@dataclass
class _PollState:
    """Mutable per-run state for single-job polling loops."""

    batch_id: str
    start: float
    poll_number: int = 0
    last_status: JobStatus | None = None
    terminal_status: JobStatus | None = None


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
    "fetch_terminal_job_result",
    "resolve_sdk_result_fetcher",
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
            and (getattr(s, "failed_records", None) or 0) == 0
            and not getattr(s, "result_error", None)
            for s in self.results.values()
        )

    @property
    def any_failed(self) -> bool:
        """Return True if any job failed, timed out, or reported failed records."""
        return bool(self.failures) or any(
            cast(Any, s).is_failed
            or (getattr(s, "failed_records", None) or 0) > 0
            or bool(getattr(s, "result_error", None))
            for s in self.results.values()
        )

    @property
    def terminal_statuses(self) -> dict[str, JobStatus]:
        """Return all terminal JobStatus objects across completed results and failed batches."""
        combined: dict[str, JobStatus] = dict(self.results)
        for batch_id, err in self.failures.items():
            status = getattr(err, "status", None)
            if isinstance(status, JobStatus) and batch_id not in combined:
                combined[batch_id] = status
        return combined


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
                extracted.append(cast(dict[str, Any], sanitize_diagnostic_payload(item)))
    elif isinstance(results, dict):
        nested_errors = results.get("errors")
        if isinstance(nested_errors, list):
            for err_item in nested_errors:
                if isinstance(err_item, dict):
                    extracted.append(cast(dict[str, Any], sanitize_diagnostic_payload(err_item)))
                elif err_item is not None:
                    extracted.append({"error": sanitize_diagnostic_text(str(err_item))})
        elif results.get("error"):
            extracted.append(cast(dict[str, Any], sanitize_diagnostic_payload(results)))
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
        poll_summary: JobPollSummary | None = None,
        partial_summary: Any | None = None,
    ) -> None:
        """Initialize the JobFailedError with attached record and batch diagnostics."""
        resolved_errors = (
            cast(list[dict[str, Any]], sanitize_diagnostic_payload(record_errors))
            if record_errors is not None
            else _extract_record_errors(status)
        )
        raw_res_err = (
            result_error if result_error is not None else getattr(status, "result_error", None)
        )
        resolved_result_error = (
            sanitize_diagnostic_text(raw_res_err)
            if isinstance(raw_res_err, str) and raw_res_err
            else None
        )
        sanitized_base = sanitize_diagnostic_text(message)
        formatted_message = _format_job_failure_details(
            sanitized_base,
            status,
            resolved_errors,
            resolved_result_error,
        )
        super().__init__(formatted_message)
        self.status = status
        self.record_errors = resolved_errors
        self.result_error = resolved_result_error
        self.poll_summary = poll_summary
        self.partial_summary = partial_summary


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


def _attach_fetched_result(status: JobStatus | Any, res: Any) -> JobStatus | Any:
    """Attach a fetched ``resultUrl`` JSON payload to ``status`` or raise on non-JSON bodies."""
    parsed: Any
    if isinstance(res, (dict, list)):
        status.results = res
        return status

    try:
        if hasattr(res, "json"):
            parsed = res.json()
        elif isinstance(res, (str, bytes)):
            parsed = json.loads(res)
        else:
            raise ValueError(f"Unsupported resultUrl response type: {type(res).__name__}")
        if not isinstance(parsed, (dict, list)):
            raise ValueError(
                f"Expected JSON object or array from resultUrl, got {type(parsed).__name__}"
            )
    except Exception as exc:
        redacted_err = sanitize_diagnostic_text(str(exc)) or "Invalid JSON in resultUrl body"
        status.results = None
        status.result_error = redacted_err
        raise ValueError(f"Non-JSON resultUrl response: {redacted_err}") from exc

    status.results = parsed
    return status


def resolve_sdk_result_fetcher(sdk: Any) -> Callable[[str], Any] | None:
    """Resolve a synchronous ``resultUrl`` fetcher callable from an SDK or endpoint double."""
    from unittest.mock import NonCallableMock

    from imednet.endpoints.jobs import JobsEndpoint

    jobs = (
        getattr(sdk, "jobs", None)
        if not isinstance(sdk, NonCallableMock)
        or "jobs" in vars(sdk)
        or "jobs" in getattr(sdk, "_mock_children", {})
        else None
    )
    if isinstance(jobs, JobsEndpoint):
        return jobs.fetch_result
    if jobs is not None and (
        not isinstance(jobs, NonCallableMock)
        or "fetch_result" in vars(jobs)
        or "fetch_result" in getattr(jobs, "_mock_children", {})
    ):
        fetch_fn = getattr(jobs, "fetch_result", None)
        if callable(fetch_fn):
            return cast(Callable[[str], Any], fetch_fn)
    return None


def _raise_fetch_result_error(
    batch_id: str,
    status: JobStatus | Any,
    exc: Exception,
) -> NoReturn:
    """Redact credentials and PHI, record ``result_error``, log, and raise :class:`JobFailedError`."""
    redacted_err = sanitize_diagnostic_text(str(exc))
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


def fetch_terminal_job_result(
    status: JobStatus | Any,
    fetch_result: Callable[[str], Any] | None,
    *,
    batch_id: str | None = None,
) -> JobStatus | Any:
    """Fetch and attach ``resultUrl`` output for a terminal ``Job`` status when available."""
    if not getattr(status, "is_terminal", False) or not callable(fetch_result):
        return status
    result_url = getattr(status, "result_url", None)
    if not _is_fetchable_result_url(result_url):
        return status

    resolved_batch_id = batch_id or str(
        getattr(status, "batch_id", getattr(status, "batchId", "unknown"))
    )
    try:
        res = fetch_result(cast(str, result_url))
        return _attach_fetched_result(status, res)
    except Exception as exc:
        _raise_fetch_result_error(resolved_batch_id, status, exc)


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
        return cast(JobStatus, _attach_fetched_result(status, res))

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

    def _prepare_poll(self, study_key: str, batch_id: str) -> _PollState:
        """Validate polling path segments, start timer, and inspect terminal ledger."""
        validate_path_segment(study_key, name="study_key")
        validate_path_segment(batch_id, name="batch_id")
        start = time.monotonic()
        with self._lock:
            terminal_status = self._terminal_states.get(batch_id)
        return _PollState(batch_id=batch_id, start=start, terminal_status=terminal_status)

    def _record_poll_step(
        self,
        poll: _PollState,
        result: JobStatus,
        timeout: float,
        on_progress: JobProgressHandler | None,
    ) -> JobStatusEvent:
        """Validate transition, evaluate timeout/completion, log progress, and notify callbacks."""
        poll.poll_number += 1
        status = self._check_complete(result, poll.batch_id)
        status, poll.terminal_status = self._guard_transition(
            poll.batch_id, status, poll.last_status, poll.terminal_status
        )
        poll.last_status = status
        elapsed = time.monotonic() - poll.start
        is_terminal = self._evaluate(poll.start, timeout, poll.batch_id, status)
        event = JobStatusEvent(
            batch_id=poll.batch_id,
            status=status,
            poll_number=poll.poll_number,
            elapsed=elapsed,
            is_terminal=is_terminal,
        )
        logger.debug(
            "[JobPoller] batch_id=%s state=%s progress=%s%% elapsed=%ss",
            poll.batch_id,
            status.state,
            getattr(status, "progress", 0),
            round(elapsed, 1),
        )
        self._trigger_callback(on_progress, event)
        return event

    def _finalize_poll(self, event: JobStatusEvent) -> JobStatus:
        """Log terminal polling status, evaluate final job state, and return status."""
        status = event.status
        if cast(Any, status).is_successful:
            logger.info(
                "[JobPoller] batch_id=%s COMPLETED after %s polls (%ss)",
                event.batch_id,
                event.poll_number,
                round(event.elapsed, 1),
            )
        else:
            logger.error(
                "[JobPoller] batch_id=%s FAILED: state=%s failed_records=%s",
                event.batch_id,
                status.state,
                getattr(status, "failed_records", 0),
            )
        evaluate_job_state(status)
        return status

    def _prepare_batch_poll(
        self, study_key: str, batch_ids: list[str]
    ) -> tuple[float, dict[str, JobStatus], dict[str, Exception], JobPollSummary | None]:
        """Validate batch path segments and initialize summary state."""
        validate_path_segment(study_key, name="study_key")
        for batch_id in batch_ids:
            validate_path_segment(batch_id, name="batch_id")
        start = time.monotonic()
        results: dict[str, JobStatus] = {}
        failures: dict[str, Exception] = {}
        empty = (
            self._build_poll_summary(study_key, results, failures, start) if not batch_ids else None
        )
        return start, results, failures, empty

    def _record_batch_result(
        self, results: dict[str, JobStatus], batch_id: str, status: JobStatus
    ) -> None:
        """Record a batch status while preserving previously recorded terminal states."""
        existing = results.get(batch_id)
        if (
            existing is not None
            and getattr(existing, "is_terminal", False)
            and not getattr(status, "is_terminal", False)
        ):
            return
        results[batch_id] = status

    def _build_poll_summary(
        self,
        study_key: str,
        results: dict[str, JobStatus],
        failures: dict[str, Exception],
        start: float,
    ) -> JobPollSummary:
        """Assemble a :class:`JobPollSummary` with total elapsed polling duration."""
        return JobPollSummary(
            study_key=study_key,
            results=results,
            failures=failures,
            elapsed_total=time.monotonic() - start,
        )


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

    @staticmethod
    def _wait_or_abort(interval: float, stop_event: threading.Event | None) -> None:
        """Sleep for interval seconds or wake immediately when stop_event is set."""
        if stop_event is not None:
            if stop_event.is_set():
                raise _PollAbortedError
            if time.sleep is not _ORIGINAL_TIME_SLEEP:
                time.sleep(interval)
                if stop_event.is_set():
                    raise _PollAbortedError
                return
            if stop_event.wait(timeout=max(interval, 0.0)):
                raise _PollAbortedError
            return
        time.sleep(interval)

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
        return self._run_with_stop(
            study_key,
            batch_id,
            interval=interval,
            timeout=timeout,
            on_progress=on_progress,
            stop_event=None,
        )

    def _run_with_stop(
        self,
        study_key: str,
        batch_id: str,
        interval: float = 5.0,
        timeout: float = 300.0,
        *,
        on_progress: JobProgressHandler | None = None,
        stop_event: threading.Event | None = None,
    ) -> JobStatus:
        """Synchronously poll a job until completion or cooperative stop_event signal."""
        poll = self._prepare_poll(study_key, batch_id)
        while True:
            if stop_event is not None and stop_event.is_set():
                raise _PollAbortedError
            result = self._get_job(study_key, batch_id)
            event = self._record_poll_step(poll, result, timeout, on_progress)
            if event.is_terminal:
                if stop_event is not None and stop_event.is_set():
                    raise _PollAbortedError
                fetch_terminal_job_result(event.status, self._fetch_result, batch_id=batch_id)
                return self._finalize_poll(event)
            self._wait_or_abort(interval, stop_event)

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
            If True, wake sibling worker threads cooperatively and raise the
            triggering exception (such as JobFailedError or JobTimeoutError)
            as soon as any single job fails or times out.
            If False (default), continue polling remaining jobs and collect all
            failures into JobPollSummary.failures.
        on_progress : Optional[JobProgressCallback]
            Called for each job on each poll cycle. Thread-safe: the callback
            must be safe to call from multiple threads simultaneously.
        """
        start, results, failures, empty_summary = self._prepare_batch_poll(study_key, batch_ids)
        if empty_summary is not None:
            return empty_summary
        lock = threading.Lock()
        stop_event = threading.Event()
        first_failure: Exception | None = None

        def _poll_one(batch_id: str) -> None:
            nonlocal first_failure
            if fail_fast and stop_event.is_set():
                return
            try:
                status = self._run_with_stop(
                    study_key,
                    batch_id,
                    interval=interval,
                    timeout=timeout,
                    on_progress=on_progress,
                    stop_event=stop_event if fail_fast else None,
                )
                with lock:
                    self._record_batch_result(results, batch_id, status)
            except _PollAbortedError:
                return
            except Exception as exc:
                with lock:
                    failures[batch_id] = exc
                    if fail_fast and first_failure is None:
                        first_failure = exc
                        stop_event.set()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [
                executor.submit(contextvars.copy_context().run, _poll_one, batch_id)
                for batch_id in batch_ids
            ]
            for future in futures:
                future.result()

        if fail_fast and first_failure is not None:
            raise first_failure

        return self._build_poll_summary(study_key, results, failures, start)


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

    async def _fetch_terminal_result(self, batch_id: str, status: JobStatus) -> JobStatus:
        """Fetch and attach ``resultUrl`` output for a terminal job status when available."""
        result_url = getattr(status, "result_url", None)
        if not self._fetch_result or not _is_fetchable_result_url(result_url):
            return status
        try:
            res = await self._fetch_result(cast(str, result_url))
            self._process_fetch_result(status, res)
            return status
        except Exception as exc:
            _raise_fetch_result_error(batch_id, status, exc)

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
        poll = self._prepare_poll(study_key, batch_id)
        while True:
            result = await self._get_job(study_key, batch_id)
            event = self._record_poll_step(poll, result, timeout, on_progress)
            if event.is_terminal:
                await self._fetch_terminal_result(batch_id, event.status)
                return self._finalize_poll(event)
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
        """Asynchronously poll multiple jobs concurrently with cooperative cancellation.

        All jobs are polled in parallel. Results are collected regardless of
        individual failures unless fail_fast=True, which cancels and awaits
        pending sibling tasks before raising the triggering exception.
        """
        start, results, failures, empty_summary = self._prepare_batch_poll(study_key, batch_ids)
        if empty_summary is not None:
            return empty_summary

        async def _poll_one(batch_id: str) -> None:
            try:
                status = await self.run(
                    study_key,
                    batch_id,
                    interval=interval,
                    timeout=timeout,
                    on_progress=on_progress,
                )
                self._record_batch_result(results, batch_id, status)
            except Exception as exc:
                failures[batch_id] = exc
                if fail_fast:
                    raise

        pending: set[asyncio.Task[None]] = {
            asyncio.create_task(_poll_one(bid)) for bid in batch_ids
        }
        triggering_exc: Exception | None = None

        try:
            if fail_fast:
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_EXCEPTION)
                    for finished in done:
                        if finished.cancelled():
                            continue
                        exc = finished.exception()
                        if isinstance(exc, Exception) and triggering_exc is None:
                            triggering_exc = exc
                    if triggering_exc is not None:
                        for sibling in pending:
                            sibling.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        pending.clear()
                        raise triggering_exc
            else:
                await asyncio.gather(*pending, return_exceptions=True)
                pending.clear()
        finally:
            if pending:
                for sibling in pending:
                    sibling.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        return self._build_poll_summary(study_key, results, failures, start)
