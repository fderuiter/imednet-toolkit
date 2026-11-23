"""Centralized chunked batch submission and validation coordinator."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, cast

from imednet.errors import ValidationError
from imednet.models.jobs import Job, JobStatus
from imednet.utils.job_poller import (
    BaseJobPoller,
    JobFailedError,
    JobPollSummary,
    JobProgressHandler,
    JobStatusEvent,
)
from imednet.utils.security import sanitize_diagnostic_payload, sanitize_diagnostic_text
from imednet.utils.tabular import ensure_no_masked_sentinel
from imednet.validation.cache import (
    BatchValidationReport,
    RecordValidationIssue,
    ValidationSchemaProtocol,
    is_schema_record_prevalidated,
    prevalidated_records,
    validate_record_entry,
    validate_records_batch,
)

DEFAULT_BATCH_CHUNK_SIZE = 100

__all__ = [
    "DEFAULT_BATCH_CHUNK_SIZE",
    "AsyncBatchCoordinator",
    "BatchCoordinator",
    "BatchSubmissionSummary",
    "chunk_records",
    "extract_single_submitted_job",
    "poll_facade_many_async",
    "poll_facade_many_sync",
    "poll_facade_single_async",
    "poll_facade_single_sync",
    "sanitize_job_diagnostics",
    "validate_single_job_preflight",
]


def chunk_records(
    records_data: list[dict[str, Any]],
    chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
) -> list[list[dict[str, Any]]]:
    """Split ``records_data`` into bounded chunks of at most ``chunk_size`` items."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer.")
    if not records_data:
        return []
    return [records_data[i : i + chunk_size] for i in range(0, len(records_data), chunk_size)]


def validate_single_job_preflight(
    records_data: list[dict[str, Any]],
    *,
    chunk_size: int,
    dry_run: bool,
    method_name: str,
    batch_method_name: str,
) -> None:
    """Guard single-Job workflow methods against dry-run, empty, and multi-chunk inputs."""
    if dry_run:
        raise ValidationError(
            f"{method_name}() does not support dry_run=True because no Job is created; "
            f"use {batch_method_name}() instead."
        )
    if not records_data:
        raise ValidationError(
            f"{method_name}() requires at least one record to submit a Job; "
            f"use {batch_method_name}() when zero-Job outcomes are expected."
        )
    if len(records_data) > chunk_size:
        raise ValidationError(
            f"{method_name}() accepts at most {chunk_size} record(s) (1 chunk), "
            f"got {len(records_data)}; use {batch_method_name}() for multi-chunk submissions."
        )


def extract_single_submitted_job(
    summary: BatchSubmissionSummary,
    *,
    method_name: str,
    batch_method_name: str,
) -> Job:
    """Return the single submitted Job from ``summary`` or raise :class:`ValidationError`."""
    if len(summary.jobs) != 1 or summary.last_job is None:
        raise ValidationError(
            f"{method_name}() expected exactly 1 submitted Job, got {len(summary.jobs)}; "
            f"use {batch_method_name}() instead."
        )
    return summary.last_job


def _job_is_settled_success(job: Job) -> bool:
    return (
        bool(getattr(job, "is_successful", False))
        and getattr(job, "failed_records", 0) == 0
        and not getattr(job, "result_error", None)
    )


def _job_has_no_failure(job: Job) -> bool:
    return (
        not getattr(job, "is_failed", False)
        and getattr(job, "failed_records", 0) == 0
        and not getattr(job, "result_error", None)
    )


@dataclass
class BatchSubmissionSummary:
    """Summary of a chunked batch submission and optional job polling."""

    study_key: str
    jobs: list[Job]
    chunk_sizes: list[int]
    total_records: int
    skipped_count: int = 0
    poll_summary: JobPollSummary | None = None
    dry_run: bool = False
    validation_report: BatchValidationReport | None = None
    unsubmitted_chunks: list[list[dict[str, Any]]] = field(default_factory=list)
    wait_for_completion: bool = False

    @property
    def batch_ids(self) -> list[str]:
        """Return all non-empty batch IDs created across submitted chunks."""
        return [j.batch_id for j in self.jobs if j.batch_id]

    @property
    def chunk_count(self) -> int:
        """Return the number of submitted chunks."""
        return len(self.jobs)

    @property
    def unsubmitted_records(self) -> list[dict[str, Any]]:
        """Return flattened unsubmitted record slices across all unsubmitted chunks."""
        return [rec for chunk in self.unsubmitted_chunks for rec in chunk]

    @property
    def last_job(self) -> Job | None:
        """Return the final Job in the batch (or terminal JobStatus if polled)."""
        if self.poll_summary is not None and self.jobs:
            statuses = self.poll_summary.terminal_statuses
            for job in reversed(self.jobs):
                if job.batch_id and job.batch_id in statuses:
                    return statuses[job.batch_id]
        return self.jobs[-1] if self.jobs else None

    @property
    def all_successful(self) -> bool:
        """Return True if validation and all chunks succeeded without failed or unpolled jobs."""
        if self.unsubmitted_chunks:
            return False
        if self.validation_report is not None and not self.validation_report.is_valid:
            return False
        if self.poll_summary is not None:
            if not self.poll_summary.all_successful:
                return False
            if self.wait_for_completion:
                return all(_job_is_settled_success(j) for j in self.jobs)
            return True
        if self.wait_for_completion and self.jobs:
            return all(_job_is_settled_success(j) for j in self.jobs)
        return all(_job_has_no_failure(j) for j in self.jobs)

    def __len__(self) -> int:
        """Return the number of submitted chunk jobs."""
        return len(self.jobs)

    def __iter__(self) -> Iterator[Job]:
        """Iterate over submitted chunk jobs."""
        return iter(self.jobs)

    def __getitem__(self, index: int) -> Job:
        """Index into submitted chunk jobs."""
        return self.jobs[index]


def _validate_records_with_schema(
    schema: ValidationSchemaProtocol,
    records_data: list[dict[str, Any]],
    *,
    study_key: str | None = None,
) -> None:
    """Validate records not already marked prevalidated in the active task scope."""
    ensure_no_masked_sentinel(records_data)
    is_rec_prevalidated = getattr(schema, "is_record_prevalidated", None)
    if callable(is_rec_prevalidated):
        for rec in records_data:
            if is_rec_prevalidated(rec) is not True:
                validate_record_entry(schema, rec, study_key=study_key)
        return
    if getattr(schema, "is_prevalidated", False) is True:
        return
    for rec in records_data:
        if not is_schema_record_prevalidated(schema, rec):
            validate_record_entry(schema, rec, study_key=study_key)


def _accepts_kwarg(fn: Callable[..., Any], kwarg_name: str) -> bool:
    """Return True if *fn* accepts *kwarg_name* explicitly or via ``**kwargs``."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == kwarg_name and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _masked_only_report(records_data: list[dict[str, Any]]) -> BatchValidationReport:
    """Build a :class:`BatchValidationReport` checking masked PHI sentinels when schema is absent."""
    report = BatchValidationReport(total_records=len(records_data))
    for idx, rec in enumerate(records_data):
        try:
            ensure_no_masked_sentinel([rec])
            report.valid_records += 1
        except ValidationError as exc:
            report.invalid_records += 1
            report.errors.append(
                RecordValidationIssue(
                    record_index=idx,
                    form_key=str(rec.get("formKey") or rec.get("form_key") or "") or None,
                    variable_name=None,
                    message=str(exc),
                )
            )
    return report


def _emit_fallback_progress(
    on_progress: JobProgressHandler | None,
    batch_id: str,
    status: JobStatus,
) -> None:
    """Emit a progress event for custom 1-argument poll callables that lack ``on_progress``."""
    if on_progress is None:
        return
    event = JobStatusEvent(
        batch_id=batch_id,
        status=status,
        poll_number=1,
        elapsed=0.0,
        is_terminal=bool(getattr(status, "is_terminal", True)),
    )
    BaseJobPoller()._trigger_callback(on_progress, event)


def _invoke_poll_single_sync(
    poll_single: Callable[..., JobStatus],
    batch_id: str,
    on_progress: JobProgressHandler | None,
) -> JobStatus:
    if on_progress is not None and _accepts_kwarg(poll_single, "on_progress"):
        return poll_single(batch_id, on_progress=on_progress)
    status = poll_single(batch_id)
    _emit_fallback_progress(on_progress, batch_id, status)
    return status


def _invoke_poll_many_sync(
    poll_many: Callable[..., JobPollSummary],
    batch_ids: list[str],
    on_progress: JobProgressHandler | None,
) -> JobPollSummary:
    if on_progress is not None and _accepts_kwarg(poll_many, "on_progress"):
        return poll_many(batch_ids, on_progress=on_progress)
    summary = poll_many(batch_ids)
    if on_progress is not None:
        for bid, status in summary.results.items():
            _emit_fallback_progress(on_progress, bid, status)
    return summary


async def _invoke_poll_single_async(
    poll_single: Callable[..., Awaitable[JobStatus]],
    batch_id: str,
    on_progress: JobProgressHandler | None,
) -> JobStatus:
    if on_progress is not None and _accepts_kwarg(poll_single, "on_progress"):
        return await poll_single(batch_id, on_progress=on_progress)
    status = await poll_single(batch_id)
    _emit_fallback_progress(on_progress, batch_id, status)
    return status


async def _invoke_poll_many_async(
    poll_many: Callable[..., Awaitable[JobPollSummary]],
    batch_ids: list[str],
    on_progress: JobProgressHandler | None,
) -> JobPollSummary:
    if on_progress is not None and _accepts_kwarg(poll_many, "on_progress"):
        return await poll_many(batch_ids, on_progress=on_progress)
    summary = await poll_many(batch_ids)
    if on_progress is not None:
        for bid, status in summary.results.items():
            _emit_fallback_progress(on_progress, bid, status)
    return summary


def sanitize_job_diagnostics(job: Job) -> Job:
    """Return a copy of ``job`` with diagnostic results redacted for secrets and PHI."""
    if isinstance(job, JobStatus):
        updates: dict[str, Any] = {}
        if job.results is not None:
            updates["results"] = sanitize_diagnostic_payload(job.results)
        if isinstance(job.result_error, str) and job.result_error:
            updates["result_error"] = sanitize_diagnostic_text(job.result_error)
        if updates:
            return job.model_copy(update=updates)
    return job


_sanitize_job_diagnostics = sanitize_job_diagnostics


def _sanitize_poll_summary(poll_summary: JobPollSummary | None) -> JobPollSummary | None:
    """Return a sanitized copy of ``poll_summary`` suitable for error attachment."""
    if poll_summary is None:
        return None
    sanitized_results = {
        bid: cast(JobStatus, _sanitize_job_diagnostics(status))
        for bid, status in poll_summary.results.items()
    }
    sanitized_failures: dict[str, Exception] = {}
    for bid, err in poll_summary.failures.items():
        if isinstance(err, JobFailedError) and isinstance(err.status, JobStatus):
            err.status = cast(JobStatus, _sanitize_job_diagnostics(err.status))
        sanitized_failures[bid] = err
    return JobPollSummary(
        study_key=poll_summary.study_key,
        results=sanitized_results,
        failures=sanitized_failures,
        elapsed_total=poll_summary.elapsed_total,
    )


def _apply_poll_summary_to_jobs(jobs: list[Job], poll_summary: JobPollSummary | None) -> None:
    """Attach terminal JobStatus objects from ``poll_summary`` onto ``jobs`` in place."""
    if poll_summary is None:
        return
    terminal_map = poll_summary.terminal_statuses
    for idx, job in enumerate(jobs):
        if job.batch_id and job.batch_id in terminal_map:
            jobs[idx] = terminal_map[job.batch_id]


def _attach_mid_batch_error(
    exc: Exception,
    *,
    study_key: str,
    jobs: list[Job],
    chunk_sizes: list[int],
    total_records: int,
    skipped_count: int,
    poll_summary: JobPollSummary | None,
    report: BatchValidationReport | None,
    remaining_chunks: list[list[dict[str, Any]]],
    wait_for_completion: bool,
) -> None:
    """Attach sanitized partial summary and unsubmitted slices onto ``exc``."""
    sanitized_poll = _sanitize_poll_summary(poll_summary)
    sanitized_jobs = [_sanitize_job_diagnostics(j) for j in jobs]
    sanitized_chunks = cast(
        list[list[dict[str, Any]]],
        sanitize_diagnostic_payload(remaining_chunks),
    )
    partial_summary = BatchSubmissionSummary(
        study_key=study_key,
        jobs=sanitized_jobs,
        chunk_sizes=list(chunk_sizes),
        total_records=total_records,
        skipped_count=skipped_count,
        poll_summary=sanitized_poll,
        validation_report=report,
        unsubmitted_chunks=sanitized_chunks,
        wait_for_completion=wait_for_completion,
    )
    exc.partial_summary = partial_summary  # type: ignore[attr-defined]
    exc.unsubmitted_chunks = sanitized_chunks  # type: ignore[attr-defined]
    exc.unsubmitted_records = partial_summary.unsubmitted_records  # type: ignore[attr-defined]
    exc.submitted_jobs = list(sanitized_jobs)  # type: ignore[attr-defined]
    exc.poll_summary = sanitized_poll  # type: ignore[attr-defined]


def _extract_batch_ids(jobs: list[Job], *, require_batch_id: bool) -> list[str]:
    """Validate and extract non-empty ``batch_id`` values from submitted chunk jobs."""
    if any(not j.batch_id for j in jobs) and require_batch_id:
        raise ValueError("Submission successful but no batch_id received.")
    return [j.batch_id for j in jobs if j.batch_id]


def _raise_from_poll_summary(
    poll_summary: JobPollSummary,
    *,
    partial_summary: BatchSubmissionSummary,
) -> None:
    """Attach settled batch summaries and raise the recorded polling failure."""
    sanitized_poll = _sanitize_poll_summary(poll_summary)
    assert sanitized_poll is not None
    sanitized_jobs = [_sanitize_job_diagnostics(j) for j in partial_summary.jobs]
    attached_summary = BatchSubmissionSummary(
        study_key=partial_summary.study_key,
        jobs=sanitized_jobs,
        chunk_sizes=list(partial_summary.chunk_sizes),
        total_records=partial_summary.total_records,
        skipped_count=partial_summary.skipped_count,
        poll_summary=sanitized_poll,
        dry_run=partial_summary.dry_run,
        validation_report=partial_summary.validation_report,
        unsubmitted_chunks=list(partial_summary.unsubmitted_chunks),
        wait_for_completion=partial_summary.wait_for_completion,
    )

    target_exc: Exception | None = None
    if sanitized_poll.failures:
        target_exc = next(iter(sanitized_poll.failures.values()))
    else:
        for batch_id, status in sanitized_poll.results.items():
            if (
                getattr(status, "is_failed", False)
                or getattr(status, "failed_records", 0) > 0
                or bool(getattr(status, "result_error", None))
            ):
                target_exc = JobFailedError(
                    f"Job {batch_id} ended in state {status.state}",
                    status,
                    poll_summary=sanitized_poll,
                    partial_summary=attached_summary,
                )
                break

    if target_exc is not None:
        target_exc.poll_summary = sanitized_poll  # type: ignore[attr-defined]
        target_exc.partial_summary = attached_summary  # type: ignore[attr-defined]
        target_exc.submitted_jobs = list(sanitized_jobs)  # type: ignore[attr-defined]
        raise target_exc


def _finalize_submission_summary(
    *,
    study_key: str,
    jobs: list[Job],
    chunk_sizes: list[int],
    total_records: int,
    skipped_count: int,
    poll_summary: JobPollSummary | None,
    report: BatchValidationReport | None,
    wait_for_completion: bool,
    raise_on_poll_failure: bool,
) -> BatchSubmissionSummary:
    """Update ``jobs`` from ``poll_summary``, raise on failure if requested, or return summary."""
    _apply_poll_summary_to_jobs(jobs, poll_summary)
    summary = BatchSubmissionSummary(
        study_key=study_key,
        jobs=jobs,
        chunk_sizes=chunk_sizes,
        total_records=total_records,
        skipped_count=skipped_count,
        poll_summary=poll_summary,
        validation_report=report,
        wait_for_completion=wait_for_completion,
    )
    if poll_summary is not None and raise_on_poll_failure and not poll_summary.all_successful:
        _raise_from_poll_summary(poll_summary, partial_summary=summary)
    return summary


def _poll_many_failure_summary(
    study_key: str,
    batch_ids: list[str],
    exc: Exception,
) -> JobPollSummary:
    return JobPollSummary(
        study_key=study_key,
        results={},
        failures={batch_ids[0]: exc},
        elapsed_total=0.0,
    )


def _settle_batches_sync(
    study_key: str,
    batch_ids: list[str],
    *,
    poll_single: Callable[..., JobStatus] | None,
    poll_many: Callable[..., JobPollSummary] | None,
    on_progress: JobProgressHandler | None,
) -> JobPollSummary | None:
    """Settle all ``batch_ids`` synchronously without aborting sibling batches on failure."""
    if not batch_ids:
        return None
    if len(batch_ids) > 1 and poll_many is not None:
        try:
            return _invoke_poll_many_sync(poll_many, batch_ids, on_progress)
        except Exception as exc:
            return _poll_many_failure_summary(study_key, batch_ids, exc)
    if poll_single is not None:
        results: dict[str, JobStatus] = {}
        failures: dict[str, Exception] = {}
        for bid in batch_ids:
            try:
                results[bid] = _invoke_poll_single_sync(poll_single, bid, on_progress)
            except Exception as exc:
                failures[bid] = exc
        return JobPollSummary(
            study_key=study_key,
            results=results,
            failures=failures,
            elapsed_total=0.0,
        )
    if poll_many is not None:
        try:
            return _invoke_poll_many_sync(poll_many, batch_ids, on_progress)
        except Exception as exc:
            return _poll_many_failure_summary(study_key, batch_ids, exc)
    return None


async def _settle_batches_async(
    study_key: str,
    batch_ids: list[str],
    *,
    poll_single: Callable[..., Awaitable[JobStatus]] | None,
    poll_many: Callable[..., Awaitable[JobPollSummary]] | None,
    on_progress: JobProgressHandler | None,
) -> JobPollSummary | None:
    """Settle all ``batch_ids`` asynchronously without aborting sibling batches on failure."""
    if not batch_ids:
        return None
    if len(batch_ids) > 1 and poll_many is not None:
        try:
            return await _invoke_poll_many_async(poll_many, batch_ids, on_progress)
        except Exception as exc:
            return _poll_many_failure_summary(study_key, batch_ids, exc)
    if poll_single is not None:
        results: dict[str, JobStatus] = {}
        failures: dict[str, Exception] = {}
        for bid in batch_ids:
            try:
                results[bid] = await _invoke_poll_single_async(poll_single, bid, on_progress)
            except Exception as exc:
                failures[bid] = exc
        return JobPollSummary(
            study_key=study_key,
            results=results,
            failures=failures,
            elapsed_total=0.0,
        )
    if poll_many is not None:
        try:
            return await _invoke_poll_many_async(poll_many, batch_ids, on_progress)
        except Exception as exc:
            return _poll_many_failure_summary(study_key, batch_ids, exc)
    return None


def _handle_validation_report(
    report: BatchValidationReport,
    *,
    dry_run: bool,
) -> None:
    """Raise :class:`ValidationError` on invalid records unless ``dry_run`` is True."""
    if not report.is_valid and not dry_run:
        report.raise_for_errors()


def _build_early_summary(
    study_key: str,
    records_data: list[dict[str, Any]],
    *,
    skipped_count: int,
    dry_run: bool,
    report: BatchValidationReport | None,
) -> BatchSubmissionSummary:
    """Build an empty-jobs summary for dry-run or zero-chunk outcomes."""
    return BatchSubmissionSummary(
        study_key=study_key,
        jobs=[],
        chunk_sizes=[],
        total_records=len(records_data) if dry_run else 0,
        skipped_count=skipped_count,
        dry_run=dry_run,
        validation_report=report,
    )


class BatchCoordinator:
    """Coordinate single-pass validation, bounded chunking, and job polling."""

    def __init__(self, chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE) -> None:
        """Initialize the synchronous batch coordinator."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer.")
        self.chunk_size = chunk_size

    @staticmethod
    def _call_sync_validate_batch(
        validate_batch_fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = validate_batch_fn(*args, **kwargs)
        if inspect.iscoroutine(result):
            result.close()
            raise TypeError(
                "BatchCoordinator received a coroutine from 'validate_batch_fn'; "
                "use AsyncBatchCoordinator for async validation."
            )
        return result

    def _run_sync_validation(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        *,
        schema: ValidationSchemaProtocol | None,
        validate_batch_fn: Callable[..., Any] | None,
        skip_validation: bool,
        dry_run: bool,
        collect_errors: bool,
    ) -> BatchValidationReport | None:
        if validate_batch_fn is not None and inspect.iscoroutinefunction(validate_batch_fn):
            raise TypeError(
                "BatchCoordinator received an asynchronous 'validate_batch_fn'; "
                "use AsyncBatchCoordinator for async validation."
            )

        if skip_validation:
            ensure_no_masked_sentinel(records_data)
            return None

        if not dry_run and not collect_errors:
            ensure_no_masked_sentinel(records_data)
            if validate_batch_fn is not None:
                self._call_sync_validate_batch(validate_batch_fn, study_key, records_data)
            elif schema is not None:
                _validate_records_with_schema(schema, records_data, study_key=study_key)
            return None

        report: BatchValidationReport
        if validate_batch_fn is not None and _accepts_kwarg(validate_batch_fn, "collect_errors"):
            report = cast(
                BatchValidationReport,
                self._call_sync_validate_batch(
                    validate_batch_fn, study_key, records_data, collect_errors=True
                ),
            )
        elif validate_batch_fn is not None:
            ensure_no_masked_sentinel(records_data)
            self._call_sync_validate_batch(validate_batch_fn, study_key, records_data)
            report = BatchValidationReport(
                total_records=len(records_data),
                valid_records=len(records_data),
            )
        elif schema is not None:
            report = validate_records_batch(
                schema, records_data, study_key=study_key, collect_errors=True
            )
        else:
            report = _masked_only_report(records_data)

        _handle_validation_report(report, dry_run=dry_run)
        return report

    def submit(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        submit_chunk: Callable[[list[dict[str, Any]]], Job],
        *,
        schema: ValidationSchemaProtocol | None = None,
        validate_batch_fn: Callable[..., Any] | None = None,
        skip_validation: bool = False,
        skipped_count: int = 0,
        wait_for_completion: bool = False,
        poll_single: Callable[..., JobStatus] | None = None,
        poll_many: Callable[..., JobPollSummary] | None = None,
        on_progress: JobProgressHandler | None = None,
        require_batch_id: bool = False,
        raise_on_poll_failure: bool = True,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once, submit in bounded chunks, and optionally poll to completion."""
        report = self._run_sync_validation(
            study_key,
            records_data,
            schema=schema,
            validate_batch_fn=validate_batch_fn,
            skip_validation=skip_validation,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )
        if dry_run:
            return _build_early_summary(
                study_key,
                records_data,
                skipped_count=skipped_count,
                dry_run=True,
                report=report,
            )

        chunks = chunk_records(records_data, self.chunk_size)
        if not chunks:
            return _build_early_summary(
                study_key,
                records_data,
                skipped_count=skipped_count,
                dry_run=False,
                report=report,
            )

        jobs: list[Job] = []
        chunk_sizes: list[int] = []
        for idx, chunk in enumerate(chunks):
            try:
                ctx = prevalidated_records(schema, chunk) if schema is not None else nullcontext()
                with ctx:
                    job = submit_chunk(chunk)
            except Exception as exc:
                settled_poll: JobPollSummary | None = None
                if wait_for_completion and jobs:
                    accepted_bids = _extract_batch_ids(jobs, require_batch_id=False)
                    settled_poll = _settle_batches_sync(
                        study_key,
                        accepted_bids,
                        poll_single=poll_single,
                        poll_many=poll_many,
                        on_progress=on_progress,
                    )
                    _apply_poll_summary_to_jobs(jobs, settled_poll)
                _attach_mid_batch_error(
                    exc,
                    study_key=study_key,
                    jobs=jobs,
                    chunk_sizes=chunk_sizes,
                    total_records=len(records_data),
                    skipped_count=skipped_count,
                    poll_summary=settled_poll,
                    report=report,
                    remaining_chunks=chunks[idx:],
                    wait_for_completion=wait_for_completion,
                )
                raise
            jobs.append(job)
            chunk_sizes.append(len(chunk))

        poll_summary: JobPollSummary | None = None
        if wait_for_completion:
            batch_ids = _extract_batch_ids(jobs, require_batch_id=require_batch_id)
            poll_summary = _settle_batches_sync(
                study_key,
                batch_ids,
                poll_single=poll_single,
                poll_many=poll_many,
                on_progress=on_progress,
            )

        return _finalize_submission_summary(
            study_key=study_key,
            jobs=jobs,
            chunk_sizes=chunk_sizes,
            total_records=len(records_data),
            skipped_count=skipped_count,
            poll_summary=poll_summary,
            report=report,
            wait_for_completion=wait_for_completion,
            raise_on_poll_failure=raise_on_poll_failure,
        )


class AsyncBatchCoordinator:
    """Asynchronously coordinate single-pass validation, bounded chunking, and job polling."""

    def __init__(self, chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE) -> None:
        """Initialize the asynchronous batch coordinator."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer.")
        self.chunk_size = chunk_size

    async def _run_async_validation(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        *,
        schema: ValidationSchemaProtocol | None,
        validate_batch_fn: Callable[..., Awaitable[Any]] | None,
        skip_validation: bool,
        dry_run: bool,
        collect_errors: bool,
    ) -> BatchValidationReport | None:
        if skip_validation:
            ensure_no_masked_sentinel(records_data)
            return None

        if not dry_run and not collect_errors:
            ensure_no_masked_sentinel(records_data)
            if validate_batch_fn is not None:
                await validate_batch_fn(study_key, records_data)
            elif schema is not None:
                _validate_records_with_schema(schema, records_data, study_key=study_key)
            return None

        report: BatchValidationReport
        if validate_batch_fn is not None and _accepts_kwarg(validate_batch_fn, "collect_errors"):
            report = cast(
                BatchValidationReport,
                await validate_batch_fn(study_key, records_data, collect_errors=True),
            )
        elif validate_batch_fn is not None:
            ensure_no_masked_sentinel(records_data)
            await validate_batch_fn(study_key, records_data)
            report = BatchValidationReport(
                total_records=len(records_data),
                valid_records=len(records_data),
            )
        elif schema is not None:
            report = validate_records_batch(
                schema, records_data, study_key=study_key, collect_errors=True
            )
        else:
            report = _masked_only_report(records_data)

        _handle_validation_report(report, dry_run=dry_run)
        return report

    async def submit(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        submit_chunk: Callable[[list[dict[str, Any]]], Awaitable[Job]],
        *,
        schema: ValidationSchemaProtocol | None = None,
        validate_batch_fn: Callable[..., Awaitable[Any]] | None = None,
        skip_validation: bool = False,
        skipped_count: int = 0,
        wait_for_completion: bool = False,
        poll_single: Callable[..., Awaitable[JobStatus]] | None = None,
        poll_many: Callable[..., Awaitable[JobPollSummary]] | None = None,
        on_progress: JobProgressHandler | None = None,
        require_batch_id: bool = False,
        raise_on_poll_failure: bool = True,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once, asynchronously submit bounded chunks, and optionally poll."""
        report = await self._run_async_validation(
            study_key,
            records_data,
            schema=schema,
            validate_batch_fn=validate_batch_fn,
            skip_validation=skip_validation,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )
        if dry_run:
            return _build_early_summary(
                study_key,
                records_data,
                skipped_count=skipped_count,
                dry_run=True,
                report=report,
            )

        chunks = chunk_records(records_data, self.chunk_size)
        if not chunks:
            return _build_early_summary(
                study_key,
                records_data,
                skipped_count=skipped_count,
                dry_run=False,
                report=report,
            )

        jobs: list[Job] = []
        chunk_sizes: list[int] = []
        for idx, chunk in enumerate(chunks):
            try:
                ctx = prevalidated_records(schema, chunk) if schema is not None else nullcontext()
                with ctx:
                    job = await submit_chunk(chunk)
            except Exception as exc:
                settled_poll: JobPollSummary | None = None
                if wait_for_completion and jobs:
                    accepted_bids = _extract_batch_ids(jobs, require_batch_id=False)
                    settled_poll = await _settle_batches_async(
                        study_key,
                        accepted_bids,
                        poll_single=poll_single,
                        poll_many=poll_many,
                        on_progress=on_progress,
                    )
                    _apply_poll_summary_to_jobs(jobs, settled_poll)
                _attach_mid_batch_error(
                    exc,
                    study_key=study_key,
                    jobs=jobs,
                    chunk_sizes=chunk_sizes,
                    total_records=len(records_data),
                    skipped_count=skipped_count,
                    poll_summary=settled_poll,
                    report=report,
                    remaining_chunks=chunks[idx:],
                    wait_for_completion=wait_for_completion,
                )
                raise
            jobs.append(job)
            chunk_sizes.append(len(chunk))

        poll_summary: JobPollSummary | None = None
        if wait_for_completion:
            batch_ids = _extract_batch_ids(jobs, require_batch_id=require_batch_id)
            poll_summary = await _settle_batches_async(
                study_key,
                batch_ids,
                poll_single=poll_single,
                poll_many=poll_many,
                on_progress=on_progress,
            )

        return _finalize_submission_summary(
            study_key=study_key,
            jobs=jobs,
            chunk_sizes=chunk_sizes,
            total_records=len(records_data),
            skipped_count=skipped_count,
            poll_summary=poll_summary,
            report=report,
            wait_for_completion=wait_for_completion,
            raise_on_poll_failure=raise_on_poll_failure,
        )


def poll_facade_single_sync(
    sdk: Any,
    study_key: str,
    batch_id: str,
    *,
    timeout: float,
    poll_interval: float,
    on_progress: JobProgressHandler | None = None,
) -> Any:
    """Poll a single batch ID via ``sdk.poll_job``, omitting ``on_progress`` when ``None``."""
    if on_progress is None:
        return sdk.poll_job(study_key, batch_id, timeout=timeout, interval=poll_interval)
    return sdk.poll_job(
        study_key,
        batch_id,
        timeout=timeout,
        interval=poll_interval,
        on_progress=on_progress,
    )


async def poll_facade_single_async(
    sdk: Any,
    study_key: str,
    batch_id: str,
    *,
    timeout: float,
    poll_interval: float,
    on_progress: JobProgressHandler | None = None,
) -> Any:
    """Asynchronously poll a single batch ID via ``sdk.async_poll_job``."""
    if on_progress is None:
        return await sdk.async_poll_job(
            study_key, batch_id, timeout=timeout, interval=poll_interval
        )
    return await sdk.async_poll_job(
        study_key,
        batch_id,
        timeout=timeout,
        interval=poll_interval,
        on_progress=on_progress,
    )


def _build_facade_poll_kwargs(
    timeout: float,
    poll_interval: float,
    on_progress: JobProgressHandler | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": float(timeout),
        "interval": float(poll_interval),
    }
    if on_progress is not None:
        kwargs["on_progress"] = on_progress
    return kwargs


def poll_facade_many_sync(
    sdk: Any,
    study_key: str,
    batch_ids: list[str],
    *,
    timeout: float,
    poll_interval: float,
    on_progress: JobProgressHandler | None = None,
) -> JobPollSummary:
    """Poll multiple batch IDs via ``sdk.poll_jobs`` with fallback to ``sdk.poll_job``."""
    poll_jobs_fn = getattr(sdk, "poll_jobs", None)
    if callable(poll_jobs_fn):
        poll_kwargs = _build_facade_poll_kwargs(timeout, poll_interval, on_progress)
        candidate = poll_jobs_fn(study_key, batch_ids, **poll_kwargs)
        if isinstance(candidate, JobPollSummary):
            return candidate

    results: dict[str, JobStatus] = {}
    failures: dict[str, Exception] = {}
    for bid in batch_ids:
        try:
            results[bid] = poll_facade_single_sync(
                sdk,
                study_key,
                bid,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            )
        except Exception as exc:
            failures[bid] = exc
    return JobPollSummary(
        study_key=study_key,
        results=results,
        failures=failures,
        elapsed_total=0.0,
    )


async def poll_facade_many_async(
    sdk: Any,
    study_key: str,
    batch_ids: list[str],
    *,
    timeout: float,
    poll_interval: float,
    on_progress: JobProgressHandler | None = None,
) -> JobPollSummary:
    """Asynchronously poll multiple batch IDs via ``sdk.async_poll_jobs`` with fallback."""
    poll_jobs_fn = getattr(sdk, "async_poll_jobs", None)
    if callable(poll_jobs_fn):
        poll_kwargs = _build_facade_poll_kwargs(timeout, poll_interval, on_progress)
        maybe_coro = poll_jobs_fn(study_key, batch_ids, **poll_kwargs)
        if hasattr(maybe_coro, "__await__"):
            candidate = await maybe_coro
            if isinstance(candidate, JobPollSummary):
                return candidate

    results: dict[str, JobStatus] = {}
    failures: dict[str, Exception] = {}
    for bid in batch_ids:
        try:
            results[bid] = await poll_facade_single_async(
                sdk,
                study_key,
                bid,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            )
        except Exception as exc:
            failures[bid] = exc
    return JobPollSummary(
        study_key=study_key,
        results=results,
        failures=failures,
        elapsed_total=0.0,
    )
