"""Utility for polling job status."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from imednet.models.jobs import (
    TERMINAL_JOB_STATES,
    InvalidJobStateError,
    InvalidJobTransitionError,
    InvalidStateTransitionError,
    Job,
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
    "JobStateTracker",
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


@dataclass
class _TrackedJobStateEntry:
    """Internal per-(study, job) monotonic state and snapshot record."""

    last_state: JobState | None = None
    is_terminal: bool = False
    snapshot: Any = None


def _extract_normalizable_state(job: Any) -> JobState | None:
    """Extract and normalize ``job.state`` while keeping missing states ``None``."""
    raw_state = getattr(job, "state", None)
    if raw_state is None:
        return None
    if isinstance(raw_state, (str, JobState)):
        return normalize_job_state(raw_state)
    if type(raw_state).__module__.startswith("unittest.mock"):
        return None
    return normalize_job_state(raw_state)


def _serialize_job_snapshot(
    job_id: str, snapshot: Any, last_state: JobState | None
) -> dict[str, Any]:
    """Serialize compact JSON-safe metadata for reconstructing a ``JobStatus`` snapshot."""
    payload: dict[str, Any] = {"batchId": getattr(snapshot, "batch_id", None) or job_id}
    if last_state is not None:
        payload["state"] = last_state.value
    progress = getattr(snapshot, "progress", None)
    if isinstance(progress, int) and not isinstance(progress, bool):
        payload["progress"] = progress
    for src_attr, wire_key in (
        ("total_records", "totalRecords"),
        ("successful_records", "successfulRecords"),
        ("failed_records", "failedRecords"),
    ):
        count_val = getattr(snapshot, src_attr, None)
        if isinstance(count_val, int) and not isinstance(count_val, bool):
            payload[wire_key] = count_val
    for str_attr, wire_key in (
        ("result_error", "resultError"),
        ("date_created", "dateCreated"),
        ("date_started", "dateStarted"),
        ("date_finished", "dateFinished"),
    ):
        str_val = getattr(snapshot, str_attr, None)
        if isinstance(str_val, str) and str_val:
            payload[wire_key] = str_val
    return payload


class JobStateTracker:
    """Thread-safe monotonic ``Job`` state and snapshot tracker partitioned by study and job ID."""

    def __init__(self) -> None:
        """Initialize an empty per-study job state tracker."""
        self._entries: dict[str, dict[str, _TrackedJobStateEntry]] = {}
        self._lock = threading.RLock()

    def observe(self, study_key: str, job_id: str, job: Any) -> Any:
        """Validate and record an observed ``job`` payload for ``(study_key, job_id)``.

        Args:
            study_key: Study identifier partitioning the job namespace.
            job_id: Batch or job identifier within ``study_key``.
            job: Observed ``Job``/``JobStatus`` model or status payload.

        Returns:
            Effective job payload (preserving the recorded terminal snapshot when
            a stale non-terminal response arrives after completion).

        Raises:
            InvalidJobStateError: If ``job.state`` is unrecognized.
            InvalidJobTransitionError: If the transition regresses or conflicts with a terminal state.
        """
        curr_state = _extract_normalizable_state(job)
        curr_is_terminal = (
            curr_state in TERMINAL_JOB_STATES
            if curr_state is not None
            else bool(isinstance(getattr(job, "is_terminal", None), bool) and job.is_terminal)
        )

        with self._lock:
            study_bucket = self._entries.setdefault(study_key, {})
            entry = study_bucket.get(job_id)

            if entry is not None and entry.is_terminal:
                if not curr_is_terminal:
                    return entry.snapshot if entry.snapshot is not None else job
                if entry.last_state is not None and curr_state is not None:
                    validate_job_transition(entry.last_state, curr_state)
                if curr_state is not None:
                    entry.last_state = curr_state
                entry.snapshot = job
                return job

            if entry is None:
                entry = _TrackedJobStateEntry()
                study_bucket[job_id] = entry
            elif entry.last_state is not None and curr_state is not None:
                validate_job_transition(entry.last_state, curr_state)

            if curr_state is not None:
                entry.last_state = curr_state
            if curr_is_terminal:
                entry.is_terminal = True
            entry.snapshot = job
            return job

    def get_state(self, study_key: str, job_id: str) -> JobState | None:
        """Return the most recent validated ``JobState`` for ``(study_key, job_id)``."""
        with self._lock:
            entry = self._entries.get(study_key, {}).get(job_id)
            return entry.last_state if entry is not None else None

    def is_terminal(self, study_key: str, job_id: str) -> bool:
        """Return ``True`` if ``(study_key, job_id)`` has reached a terminal state."""
        with self._lock:
            entry = self._entries.get(study_key, {}).get(job_id)
            return entry.is_terminal if entry is not None else False

    def get_snapshot(self, study_key: str, job_id: str) -> Any | None:
        """Return the cached ``Job`` snapshot for ``(study_key, job_id)``, if any."""
        with self._lock:
            entry = self._entries.get(study_key, {}).get(job_id)
            return entry.snapshot if entry is not None else None

    def has_job_in_other_study(self, study_key: str, job_id: str) -> bool:
        """Return ``True`` if ``job_id`` is tracked under a study other than ``study_key``."""
        with self._lock:
            return any(
                other_study != study_key and job_id in bucket
                for other_study, bucket in self._entries.items()
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize tracker state into a JSON-compatible dictionary (e.g., for Airflow XCom)."""
        with self._lock:
            studies_payload: dict[str, dict[str, Any]] = {}
            for study_key, bucket in self._entries.items():
                job_map: dict[str, Any] = {}
                for job_id, entry in bucket.items():
                    serialized_entry: dict[str, Any] = {
                        "last_state": entry.last_state.value
                        if entry.last_state is not None
                        else None,
                        "is_terminal": entry.is_terminal,
                    }
                    if entry.snapshot is not None and (
                        isinstance(entry.snapshot, Job) or entry.last_state is not None
                    ):
                        serialized_entry["snapshot"] = _serialize_job_snapshot(
                            job_id, entry.snapshot, entry.last_state
                        )
                    job_map[job_id] = serialized_entry
                if job_map:
                    studies_payload[study_key] = job_map
            return {"version": 1, "studies": studies_payload}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> JobStateTracker:
        """Restore a ``JobStateTracker`` from a dictionary produced by :meth:`to_dict`."""
        tracker = cls()
        if not isinstance(data, Mapping):
            return tracker
        raw_studies = data.get("studies")
        if not isinstance(raw_studies, Mapping):
            return tracker

        for study_key, raw_bucket in raw_studies.items():
            if not isinstance(study_key, str) or not isinstance(raw_bucket, Mapping):
                continue
            bucket: dict[str, _TrackedJobStateEntry] = {}
            for job_id, raw_entry in raw_bucket.items():
                if not isinstance(job_id, str) or not isinstance(raw_entry, Mapping):
                    continue
                raw_state = raw_entry.get("last_state")
                parsed_state = (
                    normalize_job_state(raw_state) if isinstance(raw_state, str) else None
                )
                is_term = bool(raw_entry.get("is_terminal", False)) or (
                    parsed_state in TERMINAL_JOB_STATES if parsed_state is not None else False
                )
                snapshot: Any = None
                raw_snap = raw_entry.get("snapshot")
                if isinstance(raw_snap, Mapping):
                    snapshot = JobStatus.model_validate(dict(raw_snap))
                elif parsed_state is not None:
                    snapshot = JobStatus.model_validate({"batchId": job_id, "state": parsed_state})
                bucket[job_id] = _TrackedJobStateEntry(
                    last_state=parsed_state,
                    is_terminal=is_term,
                    snapshot=snapshot,
                )
            if bucket:
                tracker._entries[study_key] = bucket
        return tracker


class BaseJobPoller:
    """Base class for polling a job until it reaches a terminal state."""

    def __init__(self) -> None:
        """Initialize transition tracking ledger."""
        self._tracker = JobStateTracker()
        self._terminal_states: dict[str, JobStatus] = {}
        self._lock = threading.Lock()

    def _resolve_terminal_status(self, study_key: str, batch_id: str) -> JobStatus | None:
        """Resolve any previously recorded terminal ``JobStatus`` for ``(study_key, batch_id)``."""
        if self._tracker.is_terminal(study_key, batch_id):
            snap = self._tracker.get_snapshot(study_key, batch_id)
            if isinstance(snap, JobStatus):
                return snap
        with self._lock:
            legacy = self._terminal_states.get(batch_id)
        if legacy is not None and not self._tracker.has_job_in_other_study(study_key, batch_id):
            return legacy
        return None

    def _guard_transition(
        self,
        batch_id: str,
        status: JobStatus,
        last_status: JobStatus | None,
        terminal_status: JobStatus | None,
        *,
        study_key: str = "",
    ) -> tuple[JobStatus, JobStatus | None]:
        """Guard against state regressions and validate monotonic transitions.

        If a terminal state was already reached, stale non-terminal payloads are ignored
        and the terminal status is preserved, while conflicting terminal states raise
        :class:`InvalidJobTransitionError`.

        Args:
            batch_id: The job batch identifier.
            status: The newly polled JobStatus payload.
            last_status: The most recent status observed in this polling run.
            terminal_status: The terminal status if one has already been reached.
            study_key: Optional study identifier partitioning the job namespace.

        Returns:
            A tuple of (effective_status, effective_terminal_status).
        """
        if self._tracker.get_state(study_key, batch_id) is None and not self._tracker.is_terminal(
            study_key, batch_id
        ):
            if terminal_status is not None and getattr(terminal_status, "is_terminal", False):
                self._tracker.observe(study_key, batch_id, terminal_status)
            elif last_status is not None:
                self._tracker.observe(study_key, batch_id, last_status)

        was_terminal = self._tracker.is_terminal(study_key, batch_id)
        prior_terminal = self._tracker.get_snapshot(study_key, batch_id) if was_terminal else None

        effective = cast(JobStatus, self._tracker.observe(study_key, batch_id, status))

        if was_terminal and not getattr(status, "is_terminal", False):
            preserved_state = getattr(prior_terminal, "state", effective.state)
            logger.warning(
                "[JobPoller] Ignoring stale response for batch_id=%s in state %s; terminal state %s preserved",
                batch_id,
                getattr(status, "state", None),
                preserved_state,
            )
            return effective, effective

        if self._tracker.is_terminal(study_key, batch_id):
            with self._lock:
                self._terminal_states[batch_id] = effective
            return effective, effective

        return effective, terminal_status

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
        terminal_status = self._resolve_terminal_status(study_key, batch_id)

        while True:
            poll_number += 1
            result = self._get_job(study_key, batch_id)
            status = self._check_complete(result, batch_id)
            status, terminal_status = self._guard_transition(
                batch_id,
                status,
                last_status,
                terminal_status,
                study_key=study_key,
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
        terminal_status = self._resolve_terminal_status(study_key, batch_id)

        while True:
            poll_number += 1
            result = await self._get_job(study_key, batch_id)
            status = self._check_complete(result, batch_id)
            status, terminal_status = self._guard_transition(
                batch_id,
                status,
                last_status,
                terminal_status,
                study_key=study_key,
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
