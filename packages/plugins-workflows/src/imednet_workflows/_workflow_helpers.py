"""Internal helpers shared across synchronous and asynchronous write workflows."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar, Union

from imednet.spi.models import Job
from imednet.spi.utils import (
    AsyncBatchCoordinator,
    BatchCoordinator,
    BatchSubmissionSummary,
    JobProgressHandler,
    extract_single_submitted_job,
    poll_facade_many_async,
    poll_facade_many_sync,
    poll_facade_single_async,
    poll_facade_single_sync,
    validate_single_job_preflight,
)
from imednet.spi.validation import ValidationSchemaProtocol

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

FacadeLike = Union["ImednetFacade", "AsyncImednetFacade"]
_ResultT = TypeVar("_ResultT")

_SUBJECT_ID_FIELDS = {"key": "subjectKey", "id": "subjectId", "oid": "subjectOid"}
_SITE_ID_FIELDS = {"name": "siteName", "id": "siteId"}
_INTERVAL_ID_FIELDS = {"name": "intervalName", "id": "intervalId"}


@dataclass(frozen=True, slots=True)
class WorkflowBatchSpec:
    """Shared parameters for synchronous and asynchronous batch coordinator submission."""

    study_key: str
    records_data: list[dict[str, Any]]
    chunk_size: int
    wait_for_completion: bool
    timeout: int
    poll_interval: int
    require_batch_id: bool
    schema: ValidationSchemaProtocol | None = None
    skipped_count: int = 0
    on_progress: JobProgressHandler | None = None
    dry_run: bool = False
    collect_errors: bool = False


def ensure_sync_workflow_mode(
    is_async: bool, workflow_name: str, method_name: str, async_counterpart: str
) -> None:
    """Raise ``TypeError`` when a synchronous workflow method is called on an async SDK."""
    if is_async:
        raise TypeError(
            f"{workflow_name}.{method_name}() requires a synchronous ImednetSDK "
            f"(ImednetFacade). With AsyncImednetSDK, await "
            f"workflow.{async_counterpart}(...) instead."
        )


def enforce_single_job_records(
    records_data: list[dict[str, Any]],
    chunk_size: int,
    dry_run: bool,
    pair: tuple[str, str] | None,
) -> None:
    """Validate record counts and ``dry_run`` mode for single-Job workflow methods."""
    if pair is not None:
        validate_single_job_preflight(
            records_data,
            chunk_size=chunk_size,
            dry_run=dry_run,
            method_name=pair[0],
            batch_method_name=pair[1],
        )


def prepare_batch_preflight(
    chunk_size: int | None,
    default_batch_size: int,
    dry_run: bool,
    pair: tuple[str, str] | None,
) -> int:
    """Resolve effective chunk size and reject single-Job ``dry_run=True`` calls before I/O."""
    effective = chunk_size or default_batch_size
    if dry_run:
        enforce_single_job_records([], effective, True, pair)
    return effective


def unwrap_single_submitted_job(summary: BatchSubmissionSummary, pair: tuple[str, str]) -> Job:
    """Extract the terminal or submitted ``Job`` from a single-chunk batch summary."""
    return extract_single_submitted_job(summary, method_name=pair[0], batch_method_name=pair[1])


def bind_facade_pollers(
    sdk: Any, study_key: str, is_async: bool, timeout: int, poll_interval: int
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Bind synchronous or asynchronous SPI facade pollers for a study."""
    single_fn: Callable[..., Any] = (
        poll_facade_single_async if is_async else poll_facade_single_sync
    )
    many_fn: Callable[..., Any] = poll_facade_many_async if is_async else poll_facade_many_sync
    return (
        functools.partial(single_fn, sdk, study_key, timeout=timeout, poll_interval=poll_interval),
        functools.partial(many_fn, sdk, study_key, timeout=timeout, poll_interval=poll_interval),
    )


def build_positional_record_chunk_submitter(
    create_record_fn: Callable[..., _ResultT],
    study_key: str,
    *,
    schema: ValidationSchemaProtocol,
    email_notify: bool | str | None,
) -> Callable[[list[dict[str, Any]]], _ResultT]:
    """Bind positional ``create_record`` / ``async_create_record`` calls for record updates."""
    extra_kwargs: dict[str, Any] = {"schema": schema}
    if email_notify is not None:
        extra_kwargs["email_notify"] = email_notify
    return lambda chunk: create_record_fn(study_key, chunk, **extra_kwargs)


def build_keyword_record_chunk_submitter(
    create_record_fn: Callable[..., _ResultT],
    study_key: str,
    *,
    email_notify: str | None,
    schema: ValidationSchemaProtocol | None,
) -> Callable[[list[dict[str, Any]]], _ResultT]:
    """Bind keyword ``create_record`` / ``async_create_record`` calls for subject enrollment."""
    extra: dict[str, Any] = {"schema": schema} if schema is not None else {}
    return lambda chunk: create_record_fn(
        study_key=study_key, records_data=chunk, email_notify=email_notify, **extra
    )


def build_record_payload(
    form_id: str | int,
    form_type: Literal["key", "id"],
    data: dict[str, Any],
    *,
    subject_id: str | int | None = None,
    subject_type: Literal["key", "id", "oid"] = "key",
    site_id: str | int | None = None,
    site_type: Literal["name", "id"] = "name",
    interval_id: str | int | None = None,
    interval_type: Literal["name", "id"] = "name",
) -> dict[str, Any]:
    """Build a single record payload dictionary from form, subject, site, and interval refs."""
    record: dict[str, Any] = {"formKey" if form_type == "key" else "formId": form_id, "data": data}
    if subject_id is not None:
        record[_SUBJECT_ID_FIELDS[subject_type]] = subject_id
    if site_id is not None:
        record[_SITE_ID_FIELDS[site_type]] = site_id
    if interval_id is not None:
        record[_INTERVAL_ID_FIELDS[interval_type]] = interval_id
    return record


def build_single_record_call_kwargs(
    study_key: str,
    record: dict[str, Any],
    wait_for_completion: bool,
    timeout: int,
    poll_interval: int,
    email_notify: bool | str | None,
    schema: ValidationSchemaProtocol | None,
) -> dict[str, Any]:
    """Assemble keyword arguments for delegating one record to ``create_or_update_records``."""
    call_kwargs: dict[str, Any] = {
        "study_key": study_key,
        "records_data": [record],
        "wait_for_completion": wait_for_completion,
        "timeout": timeout,
        "poll_interval": poll_interval,
    }
    if schema is not None:
        call_kwargs["schema"] = schema
    if email_notify is not None:
        call_kwargs["email_notify"] = email_notify
    return call_kwargs


def _coordinator_submit_kwargs(
    sdk: Any,
    spec: WorkflowBatchSpec,
    *,
    is_async: bool,
    validate_batch_fn: Callable[..., Any] | None,
) -> dict[str, Any]:
    """Assemble common ``BatchCoordinator.submit`` / ``AsyncBatchCoordinator.submit`` kwargs."""
    poll_single, poll_many = bind_facade_pollers(
        sdk, spec.study_key, is_async, spec.timeout, spec.poll_interval
    )
    return {
        "schema": spec.schema,
        "validate_batch_fn": validate_batch_fn,
        "skipped_count": spec.skipped_count,
        "wait_for_completion": spec.wait_for_completion,
        "poll_single": poll_single,
        "poll_many": poll_many,
        "on_progress": spec.on_progress,
        "require_batch_id": spec.require_batch_id,
        "dry_run": spec.dry_run,
        "collect_errors": spec.collect_errors,
    }


def submit_workflow_batch_sync(
    sdk: ImednetFacade,
    spec: WorkflowBatchSpec,
    submit_chunk: Callable[[list[dict[str, Any]]], Job],
    *,
    validate_batch_fn: Callable[..., Any] | None = None,
) -> BatchSubmissionSummary:
    """Submit a prepared record batch synchronously through :class:`BatchCoordinator`."""
    kwargs = _coordinator_submit_kwargs(
        sdk, spec, is_async=False, validate_batch_fn=validate_batch_fn
    )
    return BatchCoordinator(chunk_size=spec.chunk_size, sdk=sdk).submit(
        spec.study_key, spec.records_data, submit_chunk, **kwargs
    )


async def submit_workflow_batch_async(
    sdk: AsyncImednetFacade,
    spec: WorkflowBatchSpec,
    submit_chunk: Callable[[list[dict[str, Any]]], Awaitable[Job]],
    *,
    validate_batch_fn: Callable[..., Awaitable[Any]] | None = None,
) -> BatchSubmissionSummary:
    """Submit a prepared record batch asynchronously through :class:`AsyncBatchCoordinator`."""
    kwargs = _coordinator_submit_kwargs(
        sdk, spec, is_async=True, validate_batch_fn=validate_batch_fn
    )
    return await AsyncBatchCoordinator(chunk_size=spec.chunk_size, sdk=sdk).submit(
        spec.study_key, spec.records_data, submit_chunk, **kwargs
    )
