"""Centralized chunked batch submission and validation coordinator."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass
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
from imednet.utils.tabular import ensure_finite_numbers, ensure_no_masked_sentinel
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
    "poll_facade_many_async",
    "poll_facade_many_sync",
    "poll_facade_single_async",
    "poll_facade_single_sync",
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

    @property
    def batch_ids(self) -> list[str]:
        """Return all non-empty batch IDs created across submitted chunks."""
        return [j.batch_id for j in self.jobs if j.batch_id]

    @property
    def chunk_count(self) -> int:
        """Return the number of submitted chunks."""
        return len(self.jobs)

    @property
    def last_job(self) -> Job | None:
        """Return the final Job in the batch (or terminal JobStatus if polled)."""
        if self.poll_summary is not None and self.jobs:
            for job in reversed(self.jobs):
                if job.batch_id and job.batch_id in self.poll_summary.results:
                    return self.poll_summary.results[job.batch_id]
        return self.jobs[-1] if self.jobs else None

    @property
    def all_successful(self) -> bool:
        """Return True if validation and all submitted chunks succeeded without failed records."""
        if self.validation_report is not None and not self.validation_report.is_valid:
            return False
        if self.poll_summary is not None:
            return self.poll_summary.all_successful
        return all(not getattr(j, "is_failed", False) for j in self.jobs)

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
    ensure_finite_numbers(records_data)
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
            ensure_finite_numbers([rec])
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


def _finalize_polled_jobs(
    jobs: list[Job],
    poll_summary: JobPollSummary | None,
    *,
    raise_on_poll_failure: bool,
) -> None:
    """Attach terminal JobStatus results onto ``jobs`` and optionally raise on failures."""
    if poll_summary is None:
        return
    for idx, job in enumerate(jobs):
        if job.batch_id and job.batch_id in poll_summary.results:
            jobs[idx] = poll_summary.results[job.batch_id]
    if raise_on_poll_failure and not poll_summary.all_successful:
        _raise_from_poll_summary(poll_summary)


def _extract_batch_ids(jobs: list[Job], *, require_batch_id: bool) -> list[str]:
    """Validate and extract non-empty ``batch_id`` values from submitted chunk jobs."""
    if any(not j.batch_id for j in jobs) and require_batch_id:
        raise ValueError("Submission successful but no batch_id received.")
    return [j.batch_id for j in jobs if j.batch_id]


def _raise_from_poll_summary(poll_summary: JobPollSummary) -> None:
    """Raise the first recorded polling failure or partial-failure JobFailedError."""
    if poll_summary.failures:
        raise next(iter(poll_summary.failures.values()))
    for batch_id, status in poll_summary.results.items():
        if getattr(status, "is_failed", False) or getattr(status, "failed_records", 0) > 0:
            raise JobFailedError(
                f"Job {batch_id} ended in state {status.state}",
                status,
            )


def _handle_validation_report(
    report: BatchValidationReport,
    *,
    dry_run: bool,
    collect_errors: bool,
) -> None:
    """Raise :class:`ValidationError` unless ``dry_run`` and ``collect_errors`` are both True."""
    if not report.is_valid and not (dry_run and collect_errors):
        report.raise_for_errors()


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
            if dry_run or collect_errors:
                return BatchValidationReport(
                    total_records=len(records_data),
                    valid_records=len(records_data),
                )
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

        _handle_validation_report(report, dry_run=dry_run, collect_errors=collect_errors)
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
            return BatchSubmissionSummary(
                study_key=study_key,
                jobs=[],
                chunk_sizes=[],
                total_records=len(records_data),
                skipped_count=skipped_count,
                dry_run=True,
                validation_report=report,
            )

        chunks = chunk_records(records_data, self.chunk_size)
        if not chunks:
            return BatchSubmissionSummary(
                study_key=study_key,
                jobs=[],
                chunk_sizes=[],
                total_records=0,
                skipped_count=skipped_count,
                validation_report=report,
            )

        jobs: list[Job] = []
        chunk_sizes: list[int] = []
        for chunk in chunks:
            ctx = prevalidated_records(schema, chunk) if schema is not None else nullcontext()
            with ctx:
                job = submit_chunk(chunk)
            jobs.append(job)
            chunk_sizes.append(len(chunk))

        poll_summary: JobPollSummary | None = None
        if wait_for_completion:
            batch_ids = _extract_batch_ids(jobs, require_batch_id=require_batch_id)
            if batch_ids:
                if len(batch_ids) == 1 and poll_single is not None:
                    single_status = _invoke_poll_single_sync(poll_single, batch_ids[0], on_progress)
                    poll_summary = JobPollSummary(
                        study_key=study_key,
                        results={batch_ids[0]: single_status},
                        failures={},
                        elapsed_total=0.0,
                    )
                elif poll_many is not None:
                    poll_summary = _invoke_poll_many_sync(poll_many, batch_ids, on_progress)
                elif poll_single is not None:
                    results: dict[str, JobStatus] = {}
                    failures: dict[str, Exception] = {}
                    for bid in batch_ids:
                        try:
                            results[bid] = _invoke_poll_single_sync(poll_single, bid, on_progress)
                        except Exception as exc:
                            failures[bid] = exc
                            if raise_on_poll_failure:
                                raise
                    poll_summary = JobPollSummary(
                        study_key=study_key,
                        results=results,
                        failures=failures,
                        elapsed_total=0.0,
                    )
                _finalize_polled_jobs(
                    jobs, poll_summary, raise_on_poll_failure=raise_on_poll_failure
                )

        return BatchSubmissionSummary(
            study_key=study_key,
            jobs=jobs,
            chunk_sizes=chunk_sizes,
            total_records=len(records_data),
            skipped_count=skipped_count,
            poll_summary=poll_summary,
            validation_report=report,
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
            if dry_run or collect_errors:
                return BatchValidationReport(
                    total_records=len(records_data),
                    valid_records=len(records_data),
                )
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

        _handle_validation_report(report, dry_run=dry_run, collect_errors=collect_errors)
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
            return BatchSubmissionSummary(
                study_key=study_key,
                jobs=[],
                chunk_sizes=[],
                total_records=len(records_data),
                skipped_count=skipped_count,
                dry_run=True,
                validation_report=report,
            )

        chunks = chunk_records(records_data, self.chunk_size)
        if not chunks:
            return BatchSubmissionSummary(
                study_key=study_key,
                jobs=[],
                chunk_sizes=[],
                total_records=0,
                skipped_count=skipped_count,
                validation_report=report,
            )

        jobs: list[Job] = []
        chunk_sizes: list[int] = []
        for chunk in chunks:
            ctx = prevalidated_records(schema, chunk) if schema is not None else nullcontext()
            with ctx:
                job = await submit_chunk(chunk)
            jobs.append(job)
            chunk_sizes.append(len(chunk))

        poll_summary: JobPollSummary | None = None
        if wait_for_completion:
            batch_ids = _extract_batch_ids(jobs, require_batch_id=require_batch_id)
            if batch_ids:
                if len(batch_ids) == 1 and poll_single is not None:
                    single_status = await _invoke_poll_single_async(
                        poll_single, batch_ids[0], on_progress
                    )
                    poll_summary = JobPollSummary(
                        study_key=study_key,
                        results={batch_ids[0]: single_status},
                        failures={},
                        elapsed_total=0.0,
                    )
                elif poll_many is not None:
                    poll_summary = await _invoke_poll_many_async(poll_many, batch_ids, on_progress)
                elif poll_single is not None:
                    results: dict[str, JobStatus] = {}
                    failures: dict[str, Exception] = {}
                    for bid in batch_ids:
                        try:
                            results[bid] = await _invoke_poll_single_async(
                                poll_single, bid, on_progress
                            )
                        except Exception as exc:
                            failures[bid] = exc
                            if raise_on_poll_failure:
                                raise
                    poll_summary = JobPollSummary(
                        study_key=study_key,
                        results=results,
                        failures=failures,
                        elapsed_total=0.0,
                    )
                _finalize_polled_jobs(
                    jobs, poll_summary, raise_on_poll_failure=raise_on_poll_failure
                )

        return BatchSubmissionSummary(
            study_key=study_key,
            jobs=jobs,
            chunk_sizes=chunk_sizes,
            total_records=len(records_data),
            skipped_count=skipped_count,
            poll_summary=poll_summary,
            validation_report=report,
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
        poll_kwargs: dict[str, Any] = {
            "timeout": float(timeout),
            "interval": float(poll_interval),
        }
        if on_progress is not None:
            poll_kwargs["on_progress"] = on_progress
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
            raise
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
        poll_kwargs: dict[str, Any] = {
            "timeout": float(timeout),
            "interval": float(poll_interval),
        }
        if on_progress is not None:
            poll_kwargs["on_progress"] = on_progress
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
            raise
    return JobPollSummary(
        study_key=study_key,
        results=results,
        failures=failures,
        elapsed_total=0.0,
    )
