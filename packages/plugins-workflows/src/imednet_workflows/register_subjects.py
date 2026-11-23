"""Workflow for registering subjects in iMednet via the Records API.

This workflow is self-contained and does not borrow from record_update.py.
It provides a simple, robust interface for registering one or more subjects
synchronously or asynchronously across both ImednetFacade and AsyncImednetFacade.
"""

import asyncio
import functools
import inspect
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Union, cast

from imednet.spi.models import Job, RegisterSubjectRequest
from imednet.spi.utils import (
    DEFAULT_BATCH_CHUNK_SIZE,
    AsyncBatchCoordinator,
    BatchCoordinator,
    BatchSubmissionSummary,
    JobPollSummary,
    JobProgressHandler,
    RecordsInput,
    build_site_validation_hook,
    filter_existing_subject_registrations,
    normalize_subject_registration_inputs,
    poll_facade_many_async,
    poll_facade_many_sync,
    poll_facade_single_async,
    poll_facade_single_sync,
)
from imednet.spi.validation import RecordValidationIssue, ValidationSchemaProtocol

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

FacadeLike = Union["ImednetFacade", "AsyncImednetFacade"]
SubjectInputs = Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput


def _is_async_facade(sdk: object) -> bool:
    """Return True if *sdk* exposes native asynchronous facade methods."""
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


class RegisterSubjectsWorkflow:
    """Manages the registration of subjects using the iMedNet SDK.

    Supports both synchronous (:class:`ImednetFacade`) and asynchronous
    (:class:`AsyncImednetFacade`) clients.
    """

    supports_async = True

    def __init__(
        self,
        sdk: "FacadeLike",
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
        if _is_async_facade(self._sdk):
            raise TypeError(
                f"RegisterSubjectsWorkflow.{method_name}() requires a synchronous ImednetSDK "
                f"(ImednetFacade). With AsyncImednetSDK, await "
                f"workflow.{async_counterpart}(...) instead."
            )

    def _poll_single(
        self,
        study_key: str,
        batch_id: str,
        *,
        timeout: int,
        poll_interval: int,
        on_progress: JobProgressHandler | None = None,
    ) -> Any:
        return poll_facade_single_sync(
            self._sdk,
            study_key,
            batch_id,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        )

    def _poll_many(
        self,
        study_key: str,
        batch_ids: list[str],
        *,
        timeout: int,
        poll_interval: int,
        on_progress: JobProgressHandler | None = None,
    ) -> JobPollSummary:
        """Poll multiple registration chunk jobs via poll_jobs with fallback to poll_job."""
        return poll_facade_many_sync(
            self._sdk,
            study_key,
            batch_ids,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        )

    async def _async_poll_single(
        self,
        study_key: str,
        batch_id: str,
        *,
        timeout: int,
        poll_interval: int,
        on_progress: JobProgressHandler | None = None,
    ) -> Any:
        return await poll_facade_single_async(
            self._sdk,
            study_key,
            batch_id,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        )

    async def _async_poll_many(
        self,
        study_key: str,
        batch_ids: list[str],
        *,
        timeout: int,
        poll_interval: int,
        on_progress: JobProgressHandler | None = None,
    ) -> JobPollSummary:
        """Asynchronously poll multiple registration chunk jobs with fallback."""
        return await poll_facade_many_async(
            self._sdk,
            study_key,
            batch_ids,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        )

    @staticmethod
    def _prepare_registration_payload(
        study_key: str,
        subjects: SubjectInputs,
        sites: Sequence[Any],
        existing_subjects: Sequence[Any] | None,
        *,
        should_filter: bool,
        subject_key_field: str | None,
        default_form_key: str | None,
        default_site_name: str | None,
        schema: ValidationSchemaProtocol | None,
        collect_errors: bool,
    ) -> tuple[
        list[dict[str, Any]],
        int,
        list[RecordValidationIssue],
        Callable[..., Any],
    ]:
        """Normalize subject inputs, filter enrolled subjects, and build the site hook."""
        norm_issues: list[RecordValidationIssue] = []
        normalized_pairs = normalize_subject_registration_inputs(
            subjects,
            default_form_key=default_form_key,
            default_site_name=default_site_name,
            schema=schema,
            study_key=study_key,
            collect_errors=collect_errors,
            issues=norm_issues,
        )
        subjects_data, skipped_count = filter_existing_subject_registrations(
            normalized_pairs,
            existing_subjects,
            should_filter=should_filter,
            subject_key_field=subject_key_field,
            issues=norm_issues,
        )
        invalid_site_indices = {
            iss.record_index
            for iss in norm_issues
            if iss.rule_id == "invalid_identifier" and iss.variable_name in {"siteId", "site_id"}
        }
        site_hook = build_site_validation_hook(sites, skip_indices=invalid_site_indices)
        return subjects_data, skipped_count, norm_issues, site_hook

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
        default_form_key: str | None = None,
        default_site_name: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate sites once, optionally filter enrolled subjects, and submit in bounded chunks."""
        self._ensure_sync_mode("register_subjects_batch", "async_register_subjects_batch")
        sdk = cast("ImednetFacade", self._sdk)
        sites = list(sdk.get_sites(study_key=study_key))
        should_filter = (
            self._filter_enrolled if filter_enrolled is None else filter_enrolled
        ) or skip_existing
        existing_subjects = list(sdk.get_subjects(study_key=study_key)) if should_filter else None
        subjects_data, skipped_count, norm_issues, site_hook = self._prepare_registration_payload(
            study_key,
            subjects,
            sites,
            existing_subjects,
            should_filter=should_filter,
            subject_key_field=subject_key_field,
            default_form_key=default_form_key,
            default_site_name=default_site_name,
            schema=schema,
            collect_errors=collect_errors,
        )

        def _submit_chunk(chunk: list[dict[str, Any]]) -> Job:
            if schema is not None:
                return sdk.create_record(
                    study_key=study_key,
                    records_data=chunk,
                    email_notify=email_notify,
                    schema=schema,
                )
            return sdk.create_record(
                study_key=study_key,
                records_data=chunk,
                email_notify=email_notify,
            )

        coordinator = BatchCoordinator(chunk_size=chunk_size or self._batch_size)
        return coordinator.submit(
            study_key,
            subjects_data,
            _submit_chunk,
            schema=schema,
            validate_batch_fn=site_hook,
            precollected_issues=norm_issues,
            skipped_count=skipped_count,
            wait_for_completion=wait_for_completion,
            poll_single=lambda bid, *, on_progress=None: self._poll_single(
                study_key,
                bid,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            ),
            poll_many=lambda bids, *, on_progress=None: self._poll_many(
                study_key,
                bids,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            ),
            on_progress=on_progress,
            require_batch_id=False,
            dry_run=dry_run,
            collect_errors=collect_errors,
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
        """Registers multiple subjects in the specified study synchronously."""
        self._ensure_sync_mode("register_subjects", "async_register_subjects")
        summary = self.register_subjects_batch(
            study_key=study_key,
            subjects=subjects,
            email_notify=email_notify,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )
        if summary.last_job is not None:
            return summary.last_job
        return Job.model_validate({"state": "COMPLETED"})

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
        default_form_key: str | None = None,
        default_site_name: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Asynchronously register subjects using either ``AsyncImednetFacade`` or ``ImednetFacade``."""
        if not _is_async_facade(self._sdk):
            sync_call = functools.partial(
                self.register_subjects_batch,
                study_key,
                subjects,
                email_notify=email_notify,
                wait_for_completion=wait_for_completion,
                timeout=timeout,
                poll_interval=poll_interval,
                chunk_size=chunk_size,
                filter_enrolled=filter_enrolled,
                skip_existing=skip_existing,
                subject_key_field=subject_key_field,
                default_form_key=default_form_key,
                default_site_name=default_site_name,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
            )
            return await asyncio.to_thread(sync_call)

        sdk = cast("AsyncImednetFacade", self._sdk)
        sites = list(await sdk.async_get_sites(study_key=study_key))
        should_filter = (
            self._filter_enrolled if filter_enrolled is None else filter_enrolled
        ) or skip_existing
        existing_subjects = (
            list(await sdk.async_get_subjects(study_key=study_key)) if should_filter else None
        )
        subjects_data, skipped_count, norm_issues, site_hook = self._prepare_registration_payload(
            study_key,
            subjects,
            sites,
            existing_subjects,
            should_filter=should_filter,
            subject_key_field=subject_key_field,
            default_form_key=default_form_key,
            default_site_name=default_site_name,
            schema=schema,
            collect_errors=collect_errors,
        )

        async def _submit_chunk_async(chunk: list[dict[str, Any]]) -> Job:
            if schema is not None:
                return await sdk.async_create_record(
                    study_key=study_key,
                    records_data=chunk,
                    email_notify=email_notify,
                    schema=schema,
                )
            return await sdk.async_create_record(
                study_key=study_key,
                records_data=chunk,
                email_notify=email_notify,
            )

        coordinator = AsyncBatchCoordinator(chunk_size=chunk_size or self._batch_size)
        return await coordinator.submit(
            study_key,
            subjects_data,
            _submit_chunk_async,
            schema=schema,
            validate_batch_fn=site_hook,
            precollected_issues=norm_issues,
            skipped_count=skipped_count,
            wait_for_completion=wait_for_completion,
            poll_single=lambda bid, *, on_progress=None: self._async_poll_single(
                study_key,
                bid,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            ),
            poll_many=lambda bids, *, on_progress=None: self._async_poll_many(
                study_key,
                bids,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            ),
            on_progress=on_progress,
            require_batch_id=False,
            dry_run=dry_run,
            collect_errors=collect_errors,
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
        """Asynchronously register multiple subjects using either sync or async SDK facades."""
        summary = await self.async_register_subjects_batch(
            study_key=study_key,
            subjects=subjects,
            email_notify=email_notify,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            filter_enrolled=filter_enrolled,
            skip_existing=skip_existing,
            subject_key_field=subject_key_field,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )
        if summary.last_job is not None:
            return summary.last_job
        return Job.model_validate({"state": "COMPLETED"})
