"""Centralized chunked batch submission and validation coordinator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from imednet.models.jobs import Job, JobStatus
from imednet.utils.job_poller import JobFailedError, JobPollSummary, JobProgressHandler
from imednet.validation.cache import (
    ValidationSchemaProtocol,
    is_schema_record_prevalidated,
    prevalidated_records,
    validate_record_entry,
)

if TYPE_CHECKING:
    pass

DEFAULT_BATCH_CHUNK_SIZE = 100

__all__ = [
    "DEFAULT_BATCH_CHUNK_SIZE",
    "AsyncBatchCoordinator",
    "BatchCoordinator",
    "BatchSubmissionSummary",
    "chunk_records",
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
        """Return True if all submitted chunks succeeded without failed records."""
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


class BatchCoordinator:
    """Coordinate single-pass validation, bounded chunking, and job polling."""

    def __init__(self, chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE) -> None:
        """Initialize the synchronous batch coordinator."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer.")
        self.chunk_size = chunk_size

    def submit(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        submit_chunk: Callable[[list[dict[str, Any]]], Job],
        *,
        schema: ValidationSchemaProtocol | None = None,
        validate_batch_fn: Callable[[str, list[dict[str, Any]]], None] | None = None,
        skip_validation: bool = False,
        skipped_count: int = 0,
        wait_for_completion: bool = False,
        poll_single: Callable[[str], JobStatus] | None = None,
        poll_many: Callable[[list[str]], JobPollSummary] | None = None,
        require_batch_id: bool = False,
        raise_on_poll_failure: bool = True,
    ) -> BatchSubmissionSummary:
        """Validate once, submit in bounded chunks, and optionally poll to completion."""
        if not skip_validation:
            if validate_batch_fn is not None:
                validate_batch_fn(study_key, records_data)
            elif schema is not None:
                _validate_records_with_schema(schema, records_data, study_key=study_key)

        chunks = chunk_records(records_data, self.chunk_size)
        if not chunks:
            return BatchSubmissionSummary(
                study_key=study_key,
                jobs=[],
                chunk_sizes=[],
                total_records=0,
                skipped_count=skipped_count,
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
            missing_batch = any(not j.batch_id for j in jobs)
            if missing_batch and require_batch_id:
                raise ValueError("Submission successful but no batch_id received.")

            batch_ids = [j.batch_id for j in jobs if j.batch_id]
            if batch_ids:
                if len(batch_ids) == 1 and poll_single is not None:
                    single_status = poll_single(batch_ids[0])
                    jobs[-1] = single_status
                    poll_summary = JobPollSummary(
                        study_key=study_key,
                        results={batch_ids[0]: single_status},
                        failures={},
                        elapsed_total=0.0,
                    )
                elif poll_many is not None:
                    poll_summary = poll_many(batch_ids)
                elif poll_single is not None:
                    results: dict[str, JobStatus] = {}
                    failures: dict[str, Exception] = {}
                    for bid in batch_ids:
                        try:
                            results[bid] = poll_single(bid)
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

                if poll_summary is not None:
                    for idx, job in enumerate(jobs):
                        if job.batch_id and job.batch_id in poll_summary.results:
                            jobs[idx] = poll_summary.results[job.batch_id]
                    if raise_on_poll_failure and not poll_summary.all_successful:
                        _raise_from_poll_summary(poll_summary)

        return BatchSubmissionSummary(
            study_key=study_key,
            jobs=jobs,
            chunk_sizes=chunk_sizes,
            total_records=len(records_data),
            skipped_count=skipped_count,
            poll_summary=poll_summary,
        )


class AsyncBatchCoordinator:
    """Asynchronously coordinate single-pass validation, bounded chunking, and job polling."""

    def __init__(self, chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE) -> None:
        """Initialize the asynchronous batch coordinator."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer.")
        self.chunk_size = chunk_size

    async def submit(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        submit_chunk: Callable[[list[dict[str, Any]]], Awaitable[Job]],
        *,
        schema: ValidationSchemaProtocol | None = None,
        validate_batch_fn: Callable[[str, list[dict[str, Any]]], Awaitable[None]] | None = None,
        skip_validation: bool = False,
        skipped_count: int = 0,
        wait_for_completion: bool = False,
        poll_single: Callable[[str], Awaitable[JobStatus]] | None = None,
        poll_many: Callable[[list[str]], Awaitable[JobPollSummary]] | None = None,
        on_progress: JobProgressHandler | None = None,
        require_batch_id: bool = False,
        raise_on_poll_failure: bool = True,
    ) -> BatchSubmissionSummary:
        """Validate once, asynchronously submit bounded chunks, and optionally poll."""
        _ = on_progress
        if not skip_validation:
            if validate_batch_fn is not None:
                await validate_batch_fn(study_key, records_data)
            elif schema is not None:
                _validate_records_with_schema(schema, records_data, study_key=study_key)

        chunks = chunk_records(records_data, self.chunk_size)
        if not chunks:
            return BatchSubmissionSummary(
                study_key=study_key,
                jobs=[],
                chunk_sizes=[],
                total_records=0,
                skipped_count=skipped_count,
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
            missing_batch = any(not j.batch_id for j in jobs)
            if missing_batch and require_batch_id:
                raise ValueError("Submission successful but no batch_id received.")

            batch_ids = [j.batch_id for j in jobs if j.batch_id]
            if batch_ids:
                if len(batch_ids) == 1 and poll_single is not None:
                    single_status = await poll_single(batch_ids[0])
                    jobs[-1] = single_status
                    poll_summary = JobPollSummary(
                        study_key=study_key,
                        results={batch_ids[0]: single_status},
                        failures={},
                        elapsed_total=0.0,
                    )
                elif poll_many is not None:
                    poll_summary = await poll_many(batch_ids)
                elif poll_single is not None:
                    results: dict[str, JobStatus] = {}
                    failures: dict[str, Exception] = {}
                    for bid in batch_ids:
                        try:
                            results[bid] = await poll_single(bid)
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

                if poll_summary is not None:
                    for idx, job in enumerate(jobs):
                        if job.batch_id and job.batch_id in poll_summary.results:
                            jobs[idx] = poll_summary.results[job.batch_id]
                    if raise_on_poll_failure and not poll_summary.all_successful:
                        _raise_from_poll_summary(poll_summary)

        return BatchSubmissionSummary(
            study_key=study_key,
            jobs=jobs,
            chunk_sizes=chunk_sizes,
            total_records=len(records_data),
            skipped_count=skipped_count,
            poll_summary=poll_summary,
        )
