"""Workflow for registering subjects in iMedNet via the Records API."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from typing_extensions import deprecated

from imednet.spi.models import Job, RegisterSubjectRequest
from imednet.spi.utils import (
    DEFAULT_BATCH_CHUNK_SIZE,
    BatchSubmissionSummary,
    JobProgressHandler,
    RecordsInput,
    SyncToAsyncFacadeAdapter,
    call_async_facade_method,
    filter_existing_subject_registrations,
    is_async_facade,
    normalize_subject_registration_inputs,
    run_sync_over_async,
    validate_subject_registration_sites,
)
from imednet.spi.validation import BaseSchemaCache, ValidationSchemaProtocol

from ._workflow_helpers import (
    FacadeLike,
    WorkflowBatchSpec,
    build_keyword_record_chunk_submitter,
    enforce_single_job_records,
    ensure_sync_workflow_mode,
    prepare_batch_preflight,
    submit_workflow_batch_async,
    unwrap_single_submitted_job,
)

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

SubjectInputs = Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput


def _is_offline_schema_dry_run(dry_run: bool, schema: ValidationSchemaProtocol | None) -> bool:
    """Return True when dry_run uses an offline DataDictionary / non-cache schema."""
    return bool(dry_run and schema is not None and not isinstance(schema, BaseSchemaCache))


def _is_async_facade(sdk: object) -> bool:
    """Return True if *sdk* exposes native asynchronous facade methods."""
    if is_async_facade(sdk):
        return True
    async_create = getattr(sdk, "async_create_record", None)
    if async_create is None:
        return False
    if inspect.iscoroutinefunction(async_create):
        return True
    if callable(async_create) and inspect.iscoroutinefunction(async_create.__call__):
        return True
    if type(async_create).__name__ == "AsyncMock":
        return True
    return not hasattr(sdk, "create_record")


class _RegisterSubjectsBase:
    """Shared validation, filtering, and canonical async execution for subject registration."""

    _batch_size: int
    _filter_enrolled: bool

    def _prepare_registration_batch(
        self,
        chunk_size: int | None,
        filter_enrolled: bool | None,
        skip_existing: bool,
        schema: ValidationSchemaProtocol | None,
        dry_run: bool,
        single_job_method: tuple[str, str] | None,
    ) -> tuple[int, bool, bool]:
        """Resolve chunk size, offline dry-run flag, and active enrolled-subject filter flag."""
        eff_chunk = prepare_batch_preflight(
            chunk_size, self._batch_size, dry_run, single_job_method
        )
        offline_dry_run = _is_offline_schema_dry_run(dry_run, schema)
        should_filter = (
            (self._filter_enrolled if filter_enrolled is None else filter_enrolled) or skip_existing
        ) and not offline_dry_run
        return eff_chunk, offline_dry_run, should_filter

    @staticmethod
    def _build_registration_spec(
        study_key: str,
        normalized_pairs: Sequence[
            tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]
        ],
        sites: Sequence[Any] | None,
        existing_subjects: Sequence[Any] | None,
        *,
        should_filter: bool,
        subject_key_field: str | None,
        chunk_size: int,
        schema: ValidationSchemaProtocol | None,
        options: tuple[bool, int, int, JobProgressHandler | None, bool, bool],
        single_job_method: tuple[str, str] | None,
    ) -> WorkflowBatchSpec:
        """Validate sites, filter enrolled subjects, and assemble a batch submission spec."""
        wait_for_completion, timeout, poll_interval, on_progress, dry_run, collect_errors = options
        validate_subject_registration_sites(normalized_pairs, sites)
        subjects_data, skipped_count = filter_existing_subject_registrations(
            normalized_pairs,
            existing_subjects,
            should_filter=should_filter,
            subject_key_field=subject_key_field,
        )
        enforce_single_job_records(subjects_data, chunk_size, dry_run, single_job_method)
        return WorkflowBatchSpec(
            study_key,
            subjects_data,
            chunk_size,
            wait_for_completion,
            timeout,
            poll_interval,
            wait_for_completion,
            schema=schema,
            skipped_count=skipped_count,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )

    async def _execute_registration_batch_async(
        self,
        async_sdk: AsyncImednetFacade,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Canonical asynchronous subject registration pipeline."""
        eff_chunk, offline_dry_run, should_filter = self._prepare_registration_batch(
            chunk_size, filter_enrolled, skip_existing, schema, dry_run, _single_job_method
        )
        sites = (
            None
            if offline_dry_run
            else await call_async_facade_method(async_sdk, "get_sites", study_key=study_key)
        )
        pairs = normalize_subject_registration_inputs(subjects, schema=schema, study_key=study_key)
        existing = (
            await call_async_facade_method(async_sdk, "get_subjects", study_key=study_key)
            if should_filter
            else None
        )
        opts = (wait_for_completion, timeout, poll_interval, on_progress, dry_run, collect_errors)
        spec = self._build_registration_spec(
            study_key,
            pairs,
            sites,
            existing,
            should_filter=should_filter,
            subject_key_field=subject_key_field,
            chunk_size=eff_chunk,
            schema=schema,
            options=opts,
            single_job_method=_single_job_method,
        )
        create_fn = functools.partial(call_async_facade_method, async_sdk, "create_record")
        submitter = build_keyword_record_chunk_submitter(
            create_fn, study_key, email_notify=email_notify, schema=schema
        )
        return await submit_workflow_batch_async(async_sdk, spec, submitter)


class AsyncRegisterSubjectsWorkflow(_RegisterSubjectsBase):
    """Asynchronous workflow for bulk subject registration on ``AsyncImednetSDK``."""

    supports_async = True

    def __init__(
        self,
        sdk: AsyncImednetFacade,
        *,
        batch_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        filter_enrolled: bool = False,
    ) -> None:
        """Initialize the asynchronous subject registration workflow."""
        self._sdk = sdk
        self._batch_size = batch_size
        self._filter_enrolled = filter_enrolled

    async def register_subjects_batch(
        self,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Asynchronously validate sites, filter enrolled subjects, and submit in bounded chunks."""
        return await self._execute_registration_batch_async(
            self._sdk,
            study_key,
            subjects,
            email_notify,
            wait_for_completion,
            timeout,
            poll_interval,
            chunk_size=chunk_size,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=_single_job_method,
        )

    async def register_subjects(
        self,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Asynchronously register a single-chunk batch of subjects and return its Job."""
        pair = ("register_subjects", "register_subjects_batch")
        summary = await self.register_subjects_batch(
            study_key,
            subjects,
            email_notify,
            wait_for_completion,
            timeout,
            poll_interval,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=pair,
        )
        return unwrap_single_submitted_job(summary, pair)

    @deprecated(
        "async_register_subjects_batch() is deprecated; "
        "use await register_subjects_batch(...) instead.",
        category=DeprecationWarning,
    )
    async def async_register_subjects_batch(
        self,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Deprecated alias for :meth:`register_subjects_batch`."""
        return await self.register_subjects_batch(
            study_key,
            subjects,
            email_notify,
            wait_for_completion,
            timeout,
            poll_interval,
            chunk_size=chunk_size,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=_single_job_method,
        )

    @deprecated(
        "async_register_subjects() is deprecated; use await register_subjects(...) instead.",
        category=DeprecationWarning,
    )
    async def async_register_subjects(
        self,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Deprecated alias for :meth:`register_subjects`."""
        pair = ("async_register_subjects", "async_register_subjects_batch")
        if "async_register_subjects_batch" in vars(self):
            summary = await self.async_register_subjects_batch(
                study_key,
                subjects,
                email_notify,
                wait_for_completion,
                timeout,
                poll_interval,
                filter_enrolled=filter_enrolled,
                skip_existing=skip_existing,
                subject_key_field=subject_key_field,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
                _single_job_method=pair,
            )
        else:
            summary = await self.register_subjects_batch(
                study_key,
                subjects,
                email_notify,
                wait_for_completion,
                timeout,
                poll_interval,
                filter_enrolled=filter_enrolled,
                skip_existing=skip_existing,
                subject_key_field=subject_key_field,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
                _single_job_method=pair,
            )
        return unwrap_single_submitted_job(summary, pair)


class RegisterSubjectsWorkflow(_RegisterSubjectsBase):
    """Manages synchronous registration of subjects over the shared async execution engine."""

    supports_async = True

    def __init__(
        self,
        sdk: FacadeLike,
        *,
        batch_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        filter_enrolled: bool = False,
    ) -> None:
        """Initialize the RegisterSubjectsWorkflow.

        Args:
            sdk: An instance of ``ImednetFacade`` or ``AsyncImednetFacade``.
            batch_size: Maximum number of records per chunked POST request (default: 100).
            filter_enrolled: If True, filter out subjects already enrolled in the study.
        """
        self._sdk = sdk
        self._batch_size = batch_size
        self._filter_enrolled = filter_enrolled

    def _ensure_sync_mode(self, method_name: str, async_counterpart: str) -> None:
        """Raise TypeError when synchronous registration methods are called on an async SDK."""
        ensure_sync_workflow_mode(
            _is_async_facade(self._sdk), "RegisterSubjectsWorkflow", method_name, async_counterpart
        )

    def _resolve_async_facade(self) -> AsyncImednetFacade:
        """Return an async facade (native or sync-adapted) for canonical async execution."""
        if _is_async_facade(self._sdk):
            return cast("AsyncImednetFacade", self._sdk)
        return cast(
            "AsyncImednetFacade",
            SyncToAsyncFacadeAdapter(cast("ImednetFacade", self._sdk)),
        )

    def register_subjects_batch(
        self,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Validate sites once, optionally filter enrolled subjects, and submit in bounded chunks."""
        self._ensure_sync_mode("register_subjects_batch", "async_register_subjects_batch")
        adapter = self._resolve_async_facade()
        return run_sync_over_async(
            lambda: self._execute_registration_batch_async(
                adapter,
                study_key,
                subjects,
                email_notify,
                wait_for_completion,
                timeout,
                poll_interval,
                chunk_size=chunk_size,
                filter_enrolled=filter_enrolled,
                skip_existing=skip_existing,
                subject_key_field=subject_key_field,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
                _single_job_method=_single_job_method,
            )
        )

    def register_subjects(
        self,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Register a single-chunk batch of subjects synchronously and return its Job."""
        self._ensure_sync_mode("register_subjects", "async_register_subjects")
        pair = ("register_subjects", "register_subjects_batch")
        summary = self.register_subjects_batch(
            study_key,
            subjects,
            email_notify,
            wait_for_completion,
            timeout,
            poll_interval,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=pair,
        )
        return unwrap_single_submitted_job(summary, pair)

    @deprecated(
        "RegisterSubjectsWorkflow.async_register_subjects_batch() is deprecated; "
        "use AsyncRegisterSubjectsWorkflow.register_subjects_batch(...) via "
        "async_sdk.workflows.register_subjects instead.",
        category=DeprecationWarning,
    )
    async def async_register_subjects_batch(
        self,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Deprecated asynchronous alias for subject batch registration."""
        return await self._execute_registration_batch_async(
            self._resolve_async_facade(),
            study_key,
            subjects,
            email_notify,
            wait_for_completion,
            timeout,
            poll_interval,
            chunk_size=chunk_size,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=_single_job_method,
        )

    @deprecated(
        "RegisterSubjectsWorkflow.async_register_subjects() is deprecated; "
        "use AsyncRegisterSubjectsWorkflow.register_subjects(...) via "
        "async_sdk.workflows.register_subjects instead.",
        category=DeprecationWarning,
    )
    async def async_register_subjects(
        self,
        study_key: str,
        subjects: SubjectInputs,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Deprecated asynchronous alias for single-chunk subject registration."""
        pair = ("async_register_subjects", "async_register_subjects_batch")
        summary = await self.async_register_subjects_batch(
            study_key,
            subjects,
            email_notify,
            wait_for_completion,
            timeout,
            poll_interval,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=pair,
        )
        return unwrap_single_submitted_job(summary, pair)


__all__ = ["AsyncRegisterSubjectsWorkflow", "RegisterSubjectsWorkflow", "SubjectInputs"]
