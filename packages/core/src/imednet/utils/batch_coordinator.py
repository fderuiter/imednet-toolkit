"""Centralized chunked batch submission and validation coordinator."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, TypedDict, Unpack, cast

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
from imednet.utils.tabular import ensure_no_masked_sentinel, normalize_records_input
from imednet.utils.typing import RecordsInput
from imednet.validation._base import _extract_form_ref
from imednet.validation.cache import (
    BatchValidationReport,
    RecordValidationIssue,
    ValidationSchemaProtocol,
    _has_existing_record_target,
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


def _validate_chunk_size(chunk_size: int) -> int:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer.")
    return chunk_size


def chunk_records(
    records_data: list[dict[str, Any]],
    chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
) -> list[list[dict[str, Any]]]:
    """Split ``records_data`` into bounded chunks of at most ``chunk_size`` items."""
    _validate_chunk_size(chunk_size)
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
        and (getattr(job, "failed_records", None) or 0) == 0
        and not getattr(job, "result_error", None)
    )


def _job_has_no_failure(job: Job) -> bool:
    return (
        not getattr(job, "is_failed", False)
        and (getattr(job, "failed_records", None) or 0) == 0
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
    has_error: bool = False

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
        if self.has_error or self.unsubmitted_chunks:
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
    """Build a :class:`BatchValidationReport` checking form identifiers and masked PHI sentinels."""
    report = BatchValidationReport(total_records=len(records_data))
    for idx, rec in enumerate(records_data):
        row_errors: list[RecordValidationIssue] = []
        fk, fid = _extract_form_ref(rec)
        has_valid_fid = fid is not None and not isinstance(fid, bool)
        if not fk and not has_valid_fid and not _has_existing_record_target(rec):
            row_errors.append(
                RecordValidationIssue(
                    record_index=idx,
                    form_key=None,
                    variable_name=None,
                    message="Record payload must specify either 'formKey' or 'formId'.",
                    rule_id="missing_form_identifier",
                )
            )
        try:
            ensure_no_masked_sentinel([rec])
        except ValidationError as exc:
            row_errors.append(
                RecordValidationIssue(
                    record_index=idx,
                    form_key=fk,
                    variable_name=None,
                    message=str(exc),
                )
            )
        if row_errors:
            report.invalid_records += 1
            report.errors.extend(row_errors)
        else:
            report.valid_records += 1
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


def _prepare_poll_call(
    poll_fn: Callable[..., Any],
    on_progress: JobProgressHandler | None,
) -> tuple[dict[str, Any], bool]:
    if on_progress is not None and _accepts_kwarg(poll_fn, "on_progress"):
        return {"on_progress": on_progress}, False
    return {}, on_progress is not None


def _finalize_poll_many_result(
    summary: JobPollSummary,
    on_progress: JobProgressHandler | None,
) -> JobPollSummary:
    if on_progress is not None:
        for bid, status in summary.results.items():
            _emit_fallback_progress(on_progress, bid, status)
    return summary


def _invoke_poll_single_sync(
    poll_single: Callable[..., JobStatus],
    batch_id: str,
    on_progress: JobProgressHandler | None,
) -> JobStatus:
    kw, emit_after = _prepare_poll_call(poll_single, on_progress)
    status = poll_single(batch_id, **kw)
    if emit_after:
        _emit_fallback_progress(on_progress, batch_id, status)
    return status


async def _invoke_poll_single_async(
    poll_single: Callable[..., Awaitable[JobStatus]],
    batch_id: str,
    on_progress: JobProgressHandler | None,
) -> JobStatus:
    kw, emit_after = _prepare_poll_call(poll_single, on_progress)
    status = await poll_single(batch_id, **kw)
    if emit_after:
        _emit_fallback_progress(on_progress, batch_id, status)
    return status


def _invoke_poll_many_sync(
    poll_many: Callable[..., JobPollSummary],
    batch_ids: list[str],
    on_progress: JobProgressHandler | None,
) -> JobPollSummary:
    kw, emit_after = _prepare_poll_call(poll_many, on_progress)
    return _finalize_poll_many_result(
        poll_many(batch_ids, **kw), on_progress if emit_after else None
    )


async def _invoke_poll_many_async(
    poll_many: Callable[..., Awaitable[JobPollSummary]],
    batch_ids: list[str],
    on_progress: JobProgressHandler | None,
) -> JobPollSummary:
    kw, emit_after = _prepare_poll_call(poll_many, on_progress)
    return _finalize_poll_many_result(
        await poll_many(batch_ids, **kw), on_progress if emit_after else None
    )


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


def _sanitize_poll_summary(poll_summary: JobPollSummary | None) -> JobPollSummary | None:
    """Return a sanitized copy of ``poll_summary`` suitable for error attachment."""
    if poll_summary is None:
        return None
    sanitized_results = {
        bid: cast(JobStatus, sanitize_job_diagnostics(status))
        for bid, status in poll_summary.results.items()
    }
    sanitized_failures: dict[str, Exception] = {}
    for bid, err in poll_summary.failures.items():
        if isinstance(err, JobFailedError) and isinstance(err.status, JobStatus):
            err.status = cast(JobStatus, sanitize_job_diagnostics(err.status))
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


def _attach_committed_jobs_context(
    exc: BaseException,
    jobs: Sequence[Job],
    chunk_sizes: Sequence[int] | None = None,
) -> None:
    """Attach committed chunk ``Job`` objects and ``batch_id`` values onto *exc*."""
    if not jobs:
        return
    committed = list(jobs)
    batch_ids = [j.batch_id for j in committed if j.batch_id]
    try:
        exc.jobs = committed  # type: ignore[attr-defined]
        exc.committed_jobs = committed  # type: ignore[attr-defined]
        exc.batch_ids = batch_ids  # type: ignore[attr-defined]
        exc.committed_batch_ids = batch_ids  # type: ignore[attr-defined]
        if batch_ids and not getattr(exc, "batch_id", None):
            exc.batch_id = batch_ids[0]  # type: ignore[attr-defined]
        if chunk_sizes is not None:
            exc.chunk_sizes = list(chunk_sizes)  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


def _attach_validation_error_summary(
    exc: ValidationError,
    *,
    study_key: str,
    total_records: int,
    skipped_count: int,
    dry_run: bool,
    report: BatchValidationReport | None = None,
) -> None:
    """Attach an errored :class:`BatchSubmissionSummary` onto validation exceptions."""
    resolved_report = report or getattr(exc, "validation_report", None)
    summary = BatchSubmissionSummary(
        study_key=study_key,
        jobs=[],
        chunk_sizes=[],
        total_records=total_records,
        skipped_count=skipped_count,
        dry_run=dry_run,
        validation_report=resolved_report,
        has_error=True,
    )
    exc.partial_summary = summary  # type: ignore[attr-defined]
    exc.batch_summary = summary  # type: ignore[attr-defined]


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
    sanitized_jobs = [sanitize_job_diagnostics(j) for j in jobs]
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
        has_error=True,
    )
    exc.partial_summary = partial_summary  # type: ignore[attr-defined]
    exc.batch_summary = partial_summary  # type: ignore[attr-defined]
    exc.unsubmitted_chunks = sanitized_chunks  # type: ignore[attr-defined]
    exc.unsubmitted_records = partial_summary.unsubmitted_records  # type: ignore[attr-defined]
    exc.submitted_jobs = list(sanitized_jobs)  # type: ignore[attr-defined]
    exc.poll_summary = sanitized_poll  # type: ignore[attr-defined]
    _attach_committed_jobs_context(exc, sanitized_jobs, chunk_sizes)


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
    sanitized_jobs = [sanitize_job_diagnostics(j) for j in partial_summary.jobs]
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
        has_error=True,
    )

    target_exc: Exception | None = None
    if sanitized_poll.failures:
        target_exc = next(iter(sanitized_poll.failures.values()))
    else:
        for batch_id, status in sanitized_poll.results.items():
            if (
                getattr(status, "is_failed", False)
                or (getattr(status, "failed_records", None) or 0) > 0
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
        target_exc.batch_summary = attached_summary  # type: ignore[attr-defined]
        target_exc.submitted_jobs = list(sanitized_jobs)  # type: ignore[attr-defined]
        _attach_committed_jobs_context(target_exc, sanitized_jobs, partial_summary.chunk_sizes)
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


def _resolve_failed_batch_id(batch_ids: list[str], exc: Exception) -> str:
    status_bid = getattr(getattr(exc, "status", None), "batch_id", None)
    if isinstance(status_bid, str) and status_bid:
        return status_bid
    exc_bid = getattr(exc, "batch_id", None)
    if isinstance(exc_bid, str) and exc_bid:
        return exc_bid
    return batch_ids[0]


def _poll_many_failure_summary(
    study_key: str,
    batch_ids: list[str],
    exc: Exception,
) -> JobPollSummary:
    attached = getattr(exc, "poll_summary", None)
    fail_bid = _resolve_failed_batch_id(batch_ids, exc)
    if isinstance(attached, JobPollSummary):
        results = dict(attached.results)
        failures = dict(attached.failures)
        if not failures:
            failures[fail_bid] = exc
        return JobPollSummary(
            study_key=attached.study_key or study_key,
            results=results,
            failures=failures,
            elapsed_total=attached.elapsed_total,
        )
    return JobPollSummary(
        study_key=study_key,
        results={},
        failures={fail_bid: exc},
        elapsed_total=0.0,
    )


def _build_sequential_poll_summary(
    study_key: str,
    results: dict[str, JobStatus],
    failures: dict[str, Exception] | None = None,
) -> JobPollSummary:
    return JobPollSummary(
        study_key=study_key,
        results=results,
        failures=failures if failures is not None else {},
        elapsed_total=0.0,
    )


def _poll_sequential_sync(
    study_key: str,
    batch_ids: list[str],
    poll_one: Callable[[str], JobStatus],
    *,
    raise_on_failure: bool,
) -> JobPollSummary:
    results: dict[str, JobStatus] = {}
    failures: dict[str, Exception] = {}
    for bid in batch_ids:
        try:
            results[bid] = poll_one(bid)
        except Exception as exc:
            failures[bid] = exc
            if raise_on_failure:
                raise
    return _build_sequential_poll_summary(study_key, results, failures)


async def _poll_sequential_async(
    study_key: str,
    batch_ids: list[str],
    poll_one: Callable[[str], Awaitable[JobStatus]],
    *,
    raise_on_failure: bool,
) -> JobPollSummary:
    results: dict[str, JobStatus] = {}
    failures: dict[str, Exception] = {}
    for bid in batch_ids:
        try:
            results[bid] = await poll_one(bid)
        except Exception as exc:
            failures[bid] = exc
            if raise_on_failure:
                raise
    return _build_sequential_poll_summary(study_key, results, failures)


def _settle_unpolled_siblings_sync(
    batch_ids: list[str],
    summary: JobPollSummary,
    poll_single: Callable[..., JobStatus] | None,
    on_progress: JobProgressHandler | None,
) -> JobPollSummary:
    if poll_single is None:
        return summary
    for bid in batch_ids:
        if bid in summary.results or bid in summary.failures:
            continue
        try:
            summary.results[bid] = _invoke_poll_single_sync(poll_single, bid, on_progress)
        except Exception as sibling_exc:
            summary.failures[bid] = sibling_exc
    return summary


async def _settle_unpolled_siblings_async(
    batch_ids: list[str],
    summary: JobPollSummary,
    poll_single: Callable[..., Awaitable[JobStatus]] | None,
    on_progress: JobProgressHandler | None,
) -> JobPollSummary:
    if poll_single is None:
        return summary
    for bid in batch_ids:
        if bid in summary.results or bid in summary.failures:
            continue
        try:
            summary.results[bid] = await _invoke_poll_single_async(poll_single, bid, on_progress)
        except Exception as sibling_exc:
            summary.failures[bid] = sibling_exc
    return summary


def _settle_batches_sync(
    study_key: str,
    batch_ids: list[str],
    *,
    poll_single: Callable[..., JobStatus] | None,
    poll_many: Callable[..., JobPollSummary] | None,
    on_progress: JobProgressHandler | None,
    prefer_poll_many: bool = False,
) -> JobPollSummary | None:
    """Settle all ``batch_ids`` synchronously without aborting sibling batches on failure."""
    if not batch_ids:
        return None
    if (len(batch_ids) > 1 or prefer_poll_many) and poll_many is not None:
        try:
            return _invoke_poll_many_sync(poll_many, batch_ids, on_progress)
        except Exception as exc:
            summary = _poll_many_failure_summary(study_key, batch_ids, exc)
            return _settle_unpolled_siblings_sync(batch_ids, summary, poll_single, on_progress)
    if poll_single is not None:
        return _poll_sequential_sync(
            study_key,
            batch_ids,
            lambda bid: _invoke_poll_single_sync(poll_single, bid, on_progress),
            raise_on_failure=False,
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
    prefer_poll_many: bool = False,
) -> JobPollSummary | None:
    """Settle all ``batch_ids`` asynchronously without aborting sibling batches on failure."""
    if not batch_ids:
        return None
    if (len(batch_ids) > 1 or prefer_poll_many) and poll_many is not None:
        try:
            return await _invoke_poll_many_async(poll_many, batch_ids, on_progress)
        except Exception as exc:
            summary = _poll_many_failure_summary(study_key, batch_ids, exc)
            return await _settle_unpolled_siblings_async(
                batch_ids, summary, poll_single, on_progress
            )
    if poll_single is not None:
        return await _poll_sequential_async(
            study_key,
            batch_ids,
            lambda bid: _invoke_poll_single_async(poll_single, bid, on_progress),
            raise_on_failure=False,
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


def _plan_validation(
    study_key: str,
    records_data: list[dict[str, Any]],
    *,
    schema: ValidationSchemaProtocol | None,
    validate_batch_fn: Callable[..., Any] | None,
    skip_validation: bool,
    dry_run: bool,
    collect_errors: bool,
    sdk: Any = None,
    reject_async: bool = False,
) -> tuple[BatchValidationReport | None, dict[str, Any] | None, bool]:
    if (
        reject_async
        and validate_batch_fn is not None
        and inspect.iscoroutinefunction(validate_batch_fn)
    ):
        raise TypeError(
            "BatchCoordinator received an asynchronous 'validate_batch_fn'; "
            "use AsyncBatchCoordinator for async validation."
        )
    if skip_validation:
        ensure_no_masked_sentinel(records_data)
        return None, None, False
    if validate_batch_fn is not None:
        if (dry_run or collect_errors) and _accepts_kwarg(validate_batch_fn, "collect_errors"):
            return None, {"collect_errors": True}, False
        ensure_no_masked_sentinel(records_data)
        return None, {}, False
    if not dry_run and not collect_errors:
        ensure_no_masked_sentinel(records_data)
        if schema is not None:
            _validate_records_with_schema(schema, records_data, study_key=study_key)
        return None, None, False

    report = _masked_only_report(records_data)
    if schema is not None:
        if report.is_valid:
            report = validate_records_batch(
                schema, records_data, study_key=study_key, collect_errors=True
            )
    elif report.is_valid and dry_run and sdk is not None:
        from imednet.datadict.loaders.api import can_load_from_api

        if can_load_from_api(sdk):
            return report, None, True
    _handle_validation_report(report, dry_run=dry_run)
    return report, None, False


def _validate_loaded_api_schema(
    loaded_schema: ValidationSchemaProtocol,
    records_data: list[dict[str, Any]],
    *,
    study_key: str,
    dry_run: bool,
) -> BatchValidationReport:
    report = validate_records_batch(
        loaded_schema,
        records_data,
        study_key=study_key,
        collect_errors=True,
    )
    _handle_validation_report(report, dry_run=dry_run)
    return report


def _complete_custom_validation(
    raw_result: Any,
    records_data: list[dict[str, Any]],
    *,
    used_collect_errors: bool,
    dry_run: bool,
    collect_errors: bool,
) -> BatchValidationReport | None:
    if not dry_run and not collect_errors:
        return None
    report = (
        cast(BatchValidationReport, raw_result)
        if used_collect_errors
        else BatchValidationReport(
            total_records=len(records_data),
            valid_records=len(records_data),
        )
    )
    _handle_validation_report(report, dry_run=dry_run)
    return report


def _prepare_submission_chunks(
    study_key: str,
    records_data: list[dict[str, Any]],
    *,
    chunk_size: int,
    skipped_count: int,
    dry_run: bool,
    report: BatchValidationReport | None,
) -> tuple[list[list[dict[str, Any]]], BatchSubmissionSummary | None]:
    if dry_run:
        return [], BatchSubmissionSummary(
            study_key=study_key,
            jobs=[],
            chunk_sizes=[],
            total_records=len(records_data),
            skipped_count=skipped_count,
            dry_run=True,
            validation_report=report,
        )
    chunks = chunk_records(records_data, chunk_size)
    if not chunks:
        return [], BatchSubmissionSummary(
            study_key=study_key,
            jobs=[],
            chunk_sizes=[],
            total_records=0,
            skipped_count=skipped_count,
            validation_report=report,
        )
    return chunks, None


class BatchCoordinator:
    """Coordinate single-pass validation, bounded chunking, and job polling."""

    def __init__(
        self,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        *,
        sdk: Any = None,
    ) -> None:
        """Initialize the synchronous batch coordinator."""
        self.chunk_size = _validate_chunk_size(chunk_size)
        self._sdk = sdk

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
        sdk: Any = None,
        prefer_poll_many: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once, submit in bounded chunks, and optionally poll to completion."""
        effective_sdk = sdk if sdk is not None else self._sdk
        report: BatchValidationReport | None = None
        try:
            report, call_kwargs, needs_api_schema = _plan_validation(
                study_key,
                records_data,
                schema=schema,
                validate_batch_fn=validate_batch_fn,
                skip_validation=skip_validation,
                dry_run=dry_run,
                collect_errors=collect_errors,
                sdk=effective_sdk,
                reject_async=True,
            )
            if call_kwargs is not None and validate_batch_fn is not None:
                raw_result = self._call_sync_validate_batch(
                    validate_batch_fn, study_key, records_data, **call_kwargs
                )
                report = _complete_custom_validation(
                    raw_result,
                    records_data,
                    used_collect_errors=bool(call_kwargs),
                    dry_run=dry_run,
                    collect_errors=collect_errors,
                )
            elif needs_api_schema:
                from imednet.datadict.loaders.api import load_from_api

                report = _validate_loaded_api_schema(
                    load_from_api(effective_sdk, study_key),
                    records_data,
                    study_key=study_key,
                    dry_run=dry_run,
                )
        except ValidationError as exc:
            _attach_validation_error_summary(
                exc,
                study_key=study_key,
                total_records=len(records_data),
                skipped_count=skipped_count,
                dry_run=dry_run,
                report=report,
            )
            raise

        chunks, early_summary = _prepare_submission_chunks(
            study_key,
            records_data,
            chunk_size=self.chunk_size,
            skipped_count=skipped_count,
            dry_run=dry_run,
            report=report,
        )
        if early_summary is not None:
            return early_summary

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
                        prefer_poll_many=prefer_poll_many,
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
            try:
                batch_ids = _extract_batch_ids(jobs, require_batch_id=require_batch_id)
            except Exception as exc:
                _attach_mid_batch_error(
                    exc,
                    study_key=study_key,
                    jobs=jobs,
                    chunk_sizes=chunk_sizes,
                    total_records=len(records_data),
                    skipped_count=skipped_count,
                    poll_summary=None,
                    report=report,
                    remaining_chunks=[],
                    wait_for_completion=wait_for_completion,
                )
                raise
            poll_summary = _settle_batches_sync(
                study_key,
                batch_ids,
                poll_single=poll_single,
                poll_many=poll_many,
                on_progress=on_progress,
                prefer_poll_many=prefer_poll_many,
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

    def __init__(
        self,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        *,
        sdk: Any = None,
    ) -> None:
        """Initialize the asynchronous batch coordinator."""
        self.chunk_size = _validate_chunk_size(chunk_size)
        self._sdk = sdk

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
        sdk: Any = None,
        prefer_poll_many: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once, asynchronously submit bounded chunks, and optionally poll."""
        effective_sdk = sdk if sdk is not None else self._sdk
        report: BatchValidationReport | None = None
        try:
            report, call_kwargs, needs_api_schema = _plan_validation(
                study_key,
                records_data,
                schema=schema,
                validate_batch_fn=validate_batch_fn,
                skip_validation=skip_validation,
                dry_run=dry_run,
                collect_errors=collect_errors,
                sdk=effective_sdk,
            )
            if call_kwargs is not None and validate_batch_fn is not None:
                raw_result = await validate_batch_fn(study_key, records_data, **call_kwargs)
                report = _complete_custom_validation(
                    raw_result,
                    records_data,
                    used_collect_errors=bool(call_kwargs),
                    dry_run=dry_run,
                    collect_errors=collect_errors,
                )
            elif needs_api_schema:
                from imednet.datadict.loaders.api import async_load_from_api

                report = _validate_loaded_api_schema(
                    await async_load_from_api(effective_sdk, study_key),
                    records_data,
                    study_key=study_key,
                    dry_run=dry_run,
                )
        except ValidationError as exc:
            _attach_validation_error_summary(
                exc,
                study_key=study_key,
                total_records=len(records_data),
                skipped_count=skipped_count,
                dry_run=dry_run,
                report=report,
            )
            raise

        chunks, early_summary = _prepare_submission_chunks(
            study_key,
            records_data,
            chunk_size=self.chunk_size,
            skipped_count=skipped_count,
            dry_run=dry_run,
            report=report,
        )
        if early_summary is not None:
            return early_summary

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
                        prefer_poll_many=prefer_poll_many,
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
            try:
                batch_ids = _extract_batch_ids(jobs, require_batch_id=require_batch_id)
            except Exception as exc:
                _attach_mid_batch_error(
                    exc,
                    study_key=study_key,
                    jobs=jobs,
                    chunk_sizes=chunk_sizes,
                    total_records=len(records_data),
                    skipped_count=skipped_count,
                    poll_summary=None,
                    report=report,
                    remaining_chunks=[],
                    wait_for_completion=wait_for_completion,
                )
                raise
            poll_summary = await _settle_batches_async(
                study_key,
                batch_ids,
                poll_single=poll_single,
                poll_many=poll_many,
                on_progress=on_progress,
                prefer_poll_many=prefer_poll_many,
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


class _RecordsBatchOptions(TypedDict, total=False):
    schema: ValidationSchemaProtocol | None
    chunk_size: int
    skip_validation: bool
    wait_for_completion: bool
    timeout: float
    interval: float
    on_progress: JobProgressHandler | None
    dry_run: bool
    collect_errors: bool


def _bind_study_poller(
    study_key: str,
    poll_fn: Callable[..., Any] | None,
    enabled: bool,
    interval: float,
    timeout: float,
) -> Callable[..., Any] | None:
    if not enabled or poll_fn is None:
        return None

    def _poller(target: Any, *, on_progress: JobProgressHandler | None = None) -> Any:
        return poll_fn(
            study_key, target, interval=interval, timeout=timeout, on_progress=on_progress
        )

    return _poller


def _prepare_records_batch_execution(
    study_key: str,
    records_data: RecordsInput,
    email_notify: bool | str | None,
    validate_study_key_fn: Callable[[str], None] | None,
    create_records_fn: Callable[..., Any],
    poll_single_fn: Callable[..., Any] | None,
    poll_many_fn: Callable[..., Any] | None,
    options: _RecordsBatchOptions,
    *,
    prefer_poll_many: bool = False,
) -> tuple[int, list[dict[str, Any]], Callable[[list[dict[str, Any]]], Any], dict[str, Any]]:
    if validate_study_key_fn is not None:
        validate_study_key_fn(study_key)
    schema = options.get("schema")
    wait = options.get("wait_for_completion", False)
    timeout = options.get("timeout", 300.0)
    interval = options.get("interval", 5.0)
    normalized = normalize_records_input(records_data, schema=schema, study_key=study_key)

    def _submit_chunk(chunk: list[dict[str, Any]]) -> Any:
        return create_records_fn(
            study_key, chunk, email_notify=email_notify, schema=schema, skip_validation=True
        )

    submit_kwargs: dict[str, Any] = {
        "schema": schema,
        "skip_validation": options.get("skip_validation", False),
        "wait_for_completion": wait,
        "poll_single": _bind_study_poller(study_key, poll_single_fn, wait, interval, timeout),
        "poll_many": _bind_study_poller(study_key, poll_many_fn, wait, interval, timeout),
        "on_progress": options.get("on_progress"),
        "require_batch_id": wait,
        "dry_run": options.get("dry_run", False),
        "collect_errors": options.get("collect_errors", False),
        "prefer_poll_many": prefer_poll_many,
    }
    return (
        options.get("chunk_size", DEFAULT_BATCH_CHUNK_SIZE),
        normalized,
        _submit_chunk,
        submit_kwargs,
    )


def _submit_records_batch_sync(
    study_key: str,
    records_data: RecordsInput,
    email_notify: bool | str | None = None,
    *,
    create_records_fn: Callable[..., Job],
    validate_study_key_fn: Callable[[str], None] | None = None,
    poll_single_fn: Callable[..., JobStatus] | None = None,
    poll_many_fn: Callable[..., JobPollSummary] | None = None,
    sdk: Any = None,
    prefer_poll_many: bool = False,
    **options: Unpack[_RecordsBatchOptions],
) -> BatchSubmissionSummary:
    chunk_size, normalized, submit_chunk, submit_kwargs = _prepare_records_batch_execution(
        study_key,
        records_data,
        email_notify,
        validate_study_key_fn,
        create_records_fn,
        poll_single_fn,
        poll_many_fn,
        options,
        prefer_poll_many=prefer_poll_many,
    )
    return BatchCoordinator(chunk_size=chunk_size, sdk=sdk).submit(
        study_key, normalized, submit_chunk, **submit_kwargs
    )


async def _submit_records_batch_async(
    study_key: str,
    records_data: RecordsInput,
    email_notify: bool | str | None = None,
    *,
    create_records_fn: Callable[..., Awaitable[Job]],
    validate_study_key_fn: Callable[[str], None] | None = None,
    poll_single_fn: Callable[..., Awaitable[JobStatus]] | None = None,
    poll_many_fn: Callable[..., Awaitable[JobPollSummary]] | None = None,
    sdk: Any = None,
    prefer_poll_many: bool = False,
    **options: Unpack[_RecordsBatchOptions],
) -> BatchSubmissionSummary:
    chunk_size, normalized, submit_chunk, submit_kwargs = _prepare_records_batch_execution(
        study_key,
        records_data,
        email_notify,
        validate_study_key_fn,
        create_records_fn,
        poll_single_fn,
        poll_many_fn,
        options,
        prefer_poll_many=prefer_poll_many,
    )
    return await AsyncBatchCoordinator(chunk_size=chunk_size, sdk=sdk).submit(
        study_key, normalized, submit_chunk, **submit_kwargs
    )


def _facade_poll_kwargs(
    timeout: float,
    poll_interval: float,
    on_progress: JobProgressHandler | None,
    *,
    coerce_float: bool = False,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": float(timeout) if coerce_float else timeout,
        "interval": float(poll_interval) if coerce_float else poll_interval,
    }
    if on_progress is not None:
        kwargs["on_progress"] = on_progress
    return kwargs


def _call_facade_poll_many(
    sdk: Any,
    attr_name: str,
    study_key: str,
    batch_ids: list[str],
    timeout: float,
    poll_interval: float,
    on_progress: JobProgressHandler | None,
) -> Any:
    poll_jobs_fn = getattr(sdk, attr_name, None)
    if not callable(poll_jobs_fn):
        return None
    return poll_jobs_fn(
        study_key,
        batch_ids,
        **_facade_poll_kwargs(timeout, poll_interval, on_progress, coerce_float=True),
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
    return sdk.poll_job(
        study_key, batch_id, **_facade_poll_kwargs(timeout, poll_interval, on_progress)
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
    """Asynchronously poll a single batch ID via ``sdk.poll_job``."""
    from imednet.utils.async_bridge import call_async_facade_method

    return await call_async_facade_method(
        sdk,
        "poll_job",
        study_key,
        batch_id,
        **_facade_poll_kwargs(timeout, poll_interval, on_progress),
    )


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
    candidate = _call_facade_poll_many(
        sdk, "poll_jobs", study_key, batch_ids, timeout, poll_interval, on_progress
    )
    if isinstance(candidate, JobPollSummary):
        return candidate
    return _poll_sequential_sync(
        study_key,
        batch_ids,
        lambda bid: poll_facade_single_sync(
            sdk,
            study_key,
            bid,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        ),
        raise_on_failure=False,
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
    """Asynchronously poll multiple batch IDs via ``sdk.poll_jobs`` with fallback."""
    try:
        inst_dict = object.__getattribute__(sdk, "__dict__")
    except AttributeError:
        inst_dict = {}
    prefer_legacy = "async_poll_jobs" in inst_dict and "poll_jobs" not in inst_dict
    method_order = (
        ("async_poll_jobs", "poll_jobs") if prefer_legacy else ("poll_jobs", "async_poll_jobs")
    )
    for attr_name in method_order:
        maybe_coro = _call_facade_poll_many(
            sdk, attr_name, study_key, batch_ids, timeout, poll_interval, on_progress
        )
        if hasattr(maybe_coro, "__await__"):
            candidate = await maybe_coro
            if isinstance(candidate, JobPollSummary):
                return candidate
    return await _poll_sequential_async(
        study_key,
        batch_ids,
        lambda bid: poll_facade_single_async(
            sdk,
            study_key,
            bid,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        ),
        raise_on_failure=False,
    )
