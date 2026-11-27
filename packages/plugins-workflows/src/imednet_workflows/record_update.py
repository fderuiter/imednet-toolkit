"""Utilities for submitting and updating records in iMedNet studies."""

from __future__ import annotations

import functools
import inspect
import os
from typing import TYPE_CHECKING, Any, Literal, cast

from typing_extensions import deprecated

from imednet.spi.errors import ValidationError
from imednet.spi.models import Job
from imednet.spi.utils import (
    DEFAULT_BATCH_CHUNK_SIZE,
    BatchSubmissionSummary,
    JobProgressHandler,
    RecordsInput,
    SyncToAsyncFacadeAdapter,
    call_async_facade_method,
    is_async_facade,
    normalize_records_input,
    run_sync_over_async,
)
from imednet.spi.validation import (
    AsyncSchemaValidator,
    BaseSchemaCache,
    SchemaCache,
    SchemaValidator,
    ValidationSchemaProtocol,
)

from ._workflow_helpers import (
    FacadeLike,
    WorkflowBatchSpec,
    build_positional_record_chunk_submitter,
    build_record_payload,
    build_single_record_call_kwargs,
    enforce_single_job_records,
    ensure_sync_workflow_mode,
    prepare_batch_preflight,
    submit_workflow_batch_async,
    unwrap_single_submitted_job,
)

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade


def _extract_record_form_ref(record: dict[str, Any]) -> tuple[str | None, Any]:
    """Extract (form_key, form_id) from a record payload without coalescing None to 0."""
    raw_fk = record.get("formKey") or record.get("form_key")
    fk = str(raw_fk) if raw_fk else None
    raw_fid = record.get("formId") if record.get("formId") is not None else record.get("form_id")
    fid = None if raw_fid == "" else raw_fid
    return fk, fid


class _RecordUpdateBase:
    """Shared validation, normalization, and canonical async execution for record updates."""

    _batch_size: int
    _validator: SchemaValidator | AsyncSchemaValidator
    _custom_schema: ValidationSchemaProtocol | None
    _schema: ValidationSchemaProtocol

    @staticmethod
    def _is_tabular_input(records_data: RecordsInput) -> bool:
        """Return True when *records_data* is a CSV path or pandas DataFrame."""
        if isinstance(records_data, (str, os.PathLike)):
            return True
        cls = type(records_data)
        return cls.__name__ == "DataFrame" and cls.__module__.startswith("pandas")

    def _resolve_active_schema(
        self, schema: ValidationSchemaProtocol | None
    ) -> tuple[ValidationSchemaProtocol, bool]:
        """Return ``(active_schema, is_explicit_or_offline)``."""
        if schema is not None:
            return schema, True
        if self._custom_schema is not None:
            return self._custom_schema, True
        return self._schema, not isinstance(self._schema, BaseSchemaCache)

    def _collect_distinct_form_refs(
        self, records_data: list[dict[str, Any]]
    ) -> tuple[list[str], list[Any]]:
        """Collect distinct explicit form keys and form IDs across all records in order."""
        seen_keys: set[str] = set()
        explicit_form_keys: list[str] = []
        seen_ids: set[Any] = set()
        form_ids: list[Any] = []

        for rec in records_data:
            fk, fid = _extract_record_form_ref(rec)
            if fk:
                if fk not in seen_keys:
                    seen_keys.add(fk)
                    explicit_form_keys.append(fk)
                continue

            if fid is not None:
                if isinstance(fid, bool):
                    raise ValidationError(f"Unknown form ID {fid}")
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    form_ids.append(fid)

        return explicit_form_keys, form_ids

    def _check_form_refs_need_refresh(
        self,
        cache: BaseSchemaCache[Any],
        study_key: str,
        explicit_form_keys: list[str],
        form_ids: list[Any],
    ) -> bool:
        """Return True if any explicit form key or form ID for ``study_key`` is missing from ``cache``."""
        if any(not cache.variables_for_form(fk, study_key=study_key) for fk in explicit_form_keys):
            return True
        for fid in form_ids:
            resolved = cache.form_key_from_id(fid, study_key=study_key)
            if not resolved or not cache.variables_for_form(resolved, study_key=study_key):
                return True
        return False

    def _verify_cached_form_refs(
        self,
        cache: BaseSchemaCache[Any],
        study_key: str,
        explicit_form_keys: list[str],
        form_ids: list[Any],
    ) -> None:
        """Verify all distinct form keys and form IDs exist in ``cache`` for ``study_key`` after refresh."""
        study_forms = cache.forms_for_study(study_key)
        for fk in explicit_form_keys:
            if fk not in study_forms:
                raise ValueError(f"Form key '{fk}' not found")
        for fid in form_ids:
            resolved = cache.form_key_from_id(fid, study_key=study_key)
            if not resolved or resolved not in study_forms:
                raise ValidationError(f"Unknown form ID {fid}")

    def _prepare_form_ref_validation(
        self, study_key: str, records_data: list[dict[str, Any]]
    ) -> tuple[BaseSchemaCache[Any], list[str], list[Any], bool] | None:
        """Collect distinct form references and determine whether cache refresh is needed."""
        if not records_data or not isinstance(self._schema, BaseSchemaCache):
            return None
        cache = self._schema
        explicit_form_keys, form_ids = self._collect_distinct_form_refs(records_data)
        needs_refresh = self._check_form_refs_need_refresh(
            cache, study_key, explicit_form_keys, form_ids
        )
        return cache, explicit_form_keys, form_ids, needs_refresh

    async def _await_validator_refresh(self, study_key: str) -> None:
        """Invoke ``self._validator.refresh`` regardless of sync or async validator mode."""
        res = self._validator.refresh(study_key)
        if inspect.isawaitable(res):
            await res

    async def _async_validate_form_key(
        self, study_key: str, records_data: list[dict[str, Any]]
    ) -> None:
        """Asynchronous form-key and form-ID validation with single-pass cache refresh."""
        prepared = self._prepare_form_ref_validation(study_key, records_data)
        if prepared is None:
            return
        cache, explicit_form_keys, form_ids, needs_refresh = prepared
        if needs_refresh:
            await self._await_validator_refresh(study_key)
        self._verify_cached_form_refs(cache, study_key, explicit_form_keys, form_ids)

    def _prepare_batch_context(
        self,
        study_key: str,
        records_data: RecordsInput,
        chunk_size: int | None,
        schema: ValidationSchemaProtocol | None,
        dry_run: bool,
        single_job_method: tuple[str, str] | None,
    ) -> tuple[int, ValidationSchemaProtocol, bool, bool]:
        """Resolve chunk size, active schema, offline flag, and cold tabular refresh flag."""
        effective_chunk_size = prepare_batch_preflight(
            chunk_size, self._batch_size, dry_run, single_job_method
        )
        active_schema, is_offline = self._resolve_active_schema(schema)
        needs_tabular_refresh = (
            not is_offline
            and self._is_tabular_input(records_data)
            and isinstance(active_schema, BaseSchemaCache)
            and not active_schema.forms_for_study(study_key)
        )
        return effective_chunk_size, active_schema, is_offline, needs_tabular_refresh

    @staticmethod
    def _build_batch_spec(
        study_key: str,
        records_data: RecordsInput,
        active_schema: ValidationSchemaProtocol,
        chunk_size: int,
        options: tuple[bool, int, int, JobProgressHandler | None, bool, bool],
        single_job_method: tuple[str, str] | None,
    ) -> WorkflowBatchSpec:
        """Normalize tabular or sequence record inputs and assemble a batch submission spec."""
        wait_for_completion, timeout, poll_interval, on_progress, dry_run, collect_errors = options
        normalized = normalize_records_input(
            records_data, schema=active_schema, study_key=study_key
        )
        enforce_single_job_records(normalized, chunk_size, dry_run, single_job_method)
        return WorkflowBatchSpec(
            study_key,
            normalized,
            chunk_size,
            wait_for_completion,
            timeout,
            poll_interval,
            True,
            schema=active_schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )

    async def _execute_batch_async(
        self,
        async_sdk: AsyncImednetFacade,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        chunk_size: int | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Canonical asynchronous batch validation, chunked submission, and optional job polling."""
        eff_chunk, active_schema, is_offline, needs_refresh = self._prepare_batch_context(
            study_key, records_data, chunk_size, schema, dry_run, _single_job_method
        )
        if needs_refresh:
            await self._await_validator_refresh(study_key)
        opts = (wait_for_completion, timeout, poll_interval, on_progress, dry_run, collect_errors)
        spec = self._build_batch_spec(
            study_key, records_data, active_schema, eff_chunk, opts, _single_job_method
        )
        if not is_offline:
            await self._async_validate_form_key(study_key, spec.records_data)

        create_fn = functools.partial(call_async_facade_method, async_sdk, "create_record")
        submitter = build_positional_record_chunk_submitter(
            create_fn, study_key, schema=active_schema, email_notify=email_notify
        )

        validate_fn: Any = None
        if not is_offline:
            raw_validate = self._validator.validate_batch
            if inspect.iscoroutinefunction(raw_validate):
                validate_fn = raw_validate
            else:

                async def _sync_validate_bridge(*v_args: Any, **v_kwargs: Any) -> Any:
                    res = raw_validate(*v_args, **v_kwargs)
                    if inspect.isawaitable(res):
                        return await res
                    return res

                validate_fn = _sync_validate_bridge

        return await submit_workflow_batch_async(
            async_sdk,
            spec,
            submitter,
            validate_batch_fn=validate_fn,
        )


class AsyncRecordUpdateWorkflow(_RecordUpdateBase):
    """Asynchronous workflow for creating or updating records on ``AsyncImednetSDK``."""

    supports_async = True

    def __init__(
        self,
        sdk: AsyncImednetFacade,
        *,
        batch_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        schema: ValidationSchemaProtocol | None = None,
    ) -> None:
        """Initialize the asynchronous record update workflow."""
        self._sdk = sdk
        self._batch_size = batch_size
        self._validator: AsyncSchemaValidator = AsyncSchemaValidator(sdk)
        self._custom_schema: ValidationSchemaProtocol | None = schema
        self._schema: ValidationSchemaProtocol = (
            schema if schema is not None else self._validator.schema
        )

    async def create_or_update_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        chunk_size: int | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Asynchronously validate once, submit chunks, and return a batch summary."""
        return await self._execute_batch_async(
            self._sdk,
            study_key,
            records_data,
            wait_for_completion,
            timeout,
            poll_interval,
            email_notify=email_notify,
            chunk_size=chunk_size,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=_single_job_method,
        )

    async def create_or_update_records(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Asynchronously submit a single-chunk record payload and return its Job."""
        pair = ("create_or_update_records", "create_or_update_records_batch")
        summary = await self.create_or_update_records_batch(
            study_key,
            records_data,
            wait_for_completion,
            timeout,
            poll_interval,
            email_notify=email_notify,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=pair,
        )
        return unwrap_single_submitted_job(summary, pair)

    async def register_subject(
        self,
        study_key: str,
        form_identifier: str | int,
        site_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        site_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Asynchronously register a new subject by submitting a single record."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            site_id=site_identifier,
            site_type=site_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return await self.create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )

    async def update_scheduled_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        interval_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Asynchronously update an existing scheduled record for a subject."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            subject_id=subject_identifier,
            subject_type=subject_identifier_type,
            interval_id=interval_identifier,
            interval_type=interval_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return await self.create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )

    async def create_new_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Asynchronously create a new (unscheduled) record for an existing subject."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            subject_id=subject_identifier,
            subject_type=subject_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return await self.create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )

    @deprecated(
        "async_create_or_update_records_batch() is deprecated; "
        "use await create_or_update_records_batch(...) instead.",
        category=DeprecationWarning,
    )
    async def async_create_or_update_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        chunk_size: int | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Deprecated alias for :meth:`create_or_update_records_batch`."""
        return await self.create_or_update_records_batch(
            study_key,
            records_data,
            wait_for_completion,
            timeout,
            poll_interval,
            email_notify=email_notify,
            chunk_size=chunk_size,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=_single_job_method,
        )

    @deprecated(
        "async_create_or_update_records() is deprecated; "
        "use await create_or_update_records(...) instead.",
        category=DeprecationWarning,
    )
    async def async_create_or_update_records(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Deprecated alias for :meth:`create_or_update_records`."""
        pair = ("async_create_or_update_records", "async_create_or_update_records_batch")
        if "async_create_or_update_records_batch" in vars(self):
            summary = await self.async_create_or_update_records_batch(
                study_key,
                records_data,
                wait_for_completion,
                timeout,
                poll_interval,
                email_notify=email_notify,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
                _single_job_method=pair,
            )
        else:
            summary = await self.create_or_update_records_batch(
                study_key,
                records_data,
                wait_for_completion,
                timeout,
                poll_interval,
                email_notify=email_notify,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
                _single_job_method=pair,
            )
        return unwrap_single_submitted_job(summary, pair)

    @deprecated(
        "async_register_subject() is deprecated; use await register_subject(...) instead.",
        category=DeprecationWarning,
    )
    async def async_register_subject(
        self,
        study_key: str,
        form_identifier: str | int,
        site_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        site_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Deprecated alias for :meth:`register_subject`."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            site_id=site_identifier,
            site_type=site_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        call_kwargs = build_single_record_call_kwargs(study_key, rec, *opts)
        if "async_create_or_update_records" in vars(self):
            return await self.async_create_or_update_records(**call_kwargs)
        return await self.create_or_update_records(**call_kwargs)

    @deprecated(
        "async_update_scheduled_record() is deprecated; "
        "use await update_scheduled_record(...) instead.",
        category=DeprecationWarning,
    )
    async def async_update_scheduled_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        interval_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Deprecated alias for :meth:`update_scheduled_record`."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            subject_id=subject_identifier,
            subject_type=subject_identifier_type,
            interval_id=interval_identifier,
            interval_type=interval_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        call_kwargs = build_single_record_call_kwargs(study_key, rec, *opts)
        if "async_create_or_update_records" in vars(self):
            return await self.async_create_or_update_records(**call_kwargs)
        return await self.create_or_update_records(**call_kwargs)

    @deprecated(
        "async_create_new_record() is deprecated; use await create_new_record(...) instead.",
        category=DeprecationWarning,
    )
    async def async_create_new_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Deprecated alias for :meth:`create_new_record`."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            subject_id=subject_identifier,
            subject_type=subject_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        call_kwargs = build_single_record_call_kwargs(study_key, rec, *opts)
        if "async_create_or_update_records" in vars(self):
            return await self.async_create_or_update_records(**call_kwargs)
        return await self.create_or_update_records(**call_kwargs)


class RecordUpdateWorkflow(_RecordUpdateBase):
    """Provides synchronous workflows for creating or updating records over the shared async core.

    Args:
        sdk: An instance of the ImednetFacade (or AsyncImednetFacade for legacy direct usage).
        batch_size: Maximum number of records per API POST chunk (default: 100).
        schema: Optional offline or pre-populated ValidationSchemaProtocol
            (e.g. DataDictionary or SchemaCache).
    """

    supports_async = True

    def __init__(
        self,
        sdk: FacadeLike,
        *,
        batch_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        schema: ValidationSchemaProtocol | None = None,
    ) -> None:
        """Initialize the record update workflow with an SDK instance."""
        self._sdk = sdk
        self._batch_size = batch_size
        self._is_async = is_async_facade(sdk) or hasattr(sdk, "async_create_record")
        self._validator: SchemaValidator | AsyncSchemaValidator
        if self._is_async:
            self._validator = AsyncSchemaValidator(cast("AsyncImednetFacade", sdk))
        else:
            self._validator = SchemaValidator(cast("ImednetFacade", sdk))

        self._custom_schema: ValidationSchemaProtocol | None = schema
        self._schema: ValidationSchemaProtocol = (
            schema if schema is not None else cast(SchemaCache, self._validator.schema)
        )

    def _ensure_sync_mode(self, method_name: str, async_counterpart: str) -> None:
        """Raise TypeError when synchronous record methods are called on an async SDK."""
        ensure_sync_workflow_mode(
            self._is_async, "RecordUpdateWorkflow", method_name, async_counterpart
        )

    def _resolve_async_facade(self) -> AsyncImednetFacade:
        """Return an async facade (native or sync-adapted) for canonical async execution."""
        if self._is_async:
            return cast("AsyncImednetFacade", self._sdk)
        return cast(
            "AsyncImednetFacade",
            SyncToAsyncFacadeAdapter(cast("ImednetFacade", self._sdk)),
        )

    def _validate_form_key(self, study_key: str, records_data: list[dict[str, Any]]) -> None:
        """Refresh schema once if any distinct form key or form ID in the batch is uncached."""
        self._ensure_sync_mode("_validate_form_key", "_async_validate_form_key")
        run_sync_over_async(lambda: self._async_validate_form_key(study_key, records_data))

    def create_or_update_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        chunk_size: int | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Validate once, submit records in bounded chunks, and return a batch summary."""
        self._ensure_sync_mode(
            "create_or_update_records_batch", "async_create_or_update_records_batch"
        )
        adapter = self._resolve_async_facade()
        return run_sync_over_async(
            lambda: self._execute_batch_async(
                adapter,
                study_key,
                records_data,
                wait_for_completion,
                timeout,
                poll_interval,
                email_notify=email_notify,
                chunk_size=chunk_size,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
                _single_job_method=_single_job_method,
            )
        )

    def create_or_update_records(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Submit a single-chunk record payload and return its Job."""
        self._ensure_sync_mode("create_or_update_records", "async_create_or_update_records")
        pair = ("create_or_update_records", "create_or_update_records_batch")
        summary = self.create_or_update_records_batch(
            study_key,
            records_data,
            wait_for_completion,
            timeout,
            poll_interval,
            email_notify=email_notify,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=pair,
        )
        return unwrap_single_submitted_job(summary, pair)

    @deprecated(
        "RecordUpdateWorkflow.async_create_or_update_records_batch() is deprecated; "
        "use AsyncRecordUpdateWorkflow.create_or_update_records_batch(...) via "
        "async_sdk.workflows.record_update instead.",
        category=DeprecationWarning,
    )
    async def async_create_or_update_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        chunk_size: int | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        _single_job_method: tuple[str, str] | None = None,
    ) -> BatchSubmissionSummary:
        """Deprecated asynchronous alias for batch record submission."""
        return await self._execute_batch_async(
            self._resolve_async_facade(),
            study_key,
            records_data,
            wait_for_completion,
            timeout,
            poll_interval,
            email_notify=email_notify,
            chunk_size=chunk_size,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            _single_job_method=_single_job_method,
        )

    @deprecated(
        "RecordUpdateWorkflow.async_create_or_update_records() is deprecated; "
        "use AsyncRecordUpdateWorkflow.create_or_update_records(...) via "
        "async_sdk.workflows.record_update instead.",
        category=DeprecationWarning,
    )
    async def async_create_or_update_records(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Deprecated asynchronous variant of :meth:`create_or_update_records`."""
        pair = ("async_create_or_update_records", "async_create_or_update_records_batch")
        if "async_create_or_update_records_batch" in vars(self):
            summary = await self.async_create_or_update_records_batch(
                study_key,
                records_data,
                wait_for_completion,
                timeout,
                poll_interval,
                email_notify=email_notify,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
                _single_job_method=pair,
            )
        else:
            summary = await self._execute_batch_async(
                self._resolve_async_facade(),
                study_key,
                records_data,
                wait_for_completion,
                timeout,
                poll_interval,
                email_notify=email_notify,
                schema=schema,
                on_progress=on_progress,
                dry_run=dry_run,
                collect_errors=collect_errors,
                _single_job_method=pair,
            )
        return unwrap_single_submitted_job(summary, pair)

    def register_subject(
        self,
        study_key: str,
        form_identifier: str | int,
        site_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        site_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Register a new subject by submitting a single enrollment record."""
        self._ensure_sync_mode("register_subject", "async_register_subject")
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            site_id=site_identifier,
            site_type=site_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return self.create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )

    @deprecated(
        "RecordUpdateWorkflow.async_register_subject() is deprecated; "
        "use AsyncRecordUpdateWorkflow.register_subject(...) via "
        "async_sdk.workflows.record_update instead.",
        category=DeprecationWarning,
    )
    async def async_register_subject(
        self,
        study_key: str,
        form_identifier: str | int,
        site_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        site_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Deprecated asynchronous alias for :meth:`register_subject`."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            site_id=site_identifier,
            site_type=site_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return await self.async_create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )

    def update_scheduled_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        interval_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Update an existing scheduled visit record for a subject."""
        self._ensure_sync_mode("update_scheduled_record", "async_update_scheduled_record")
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            subject_id=subject_identifier,
            subject_type=subject_identifier_type,
            interval_id=interval_identifier,
            interval_type=interval_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return self.create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )

    @deprecated(
        "RecordUpdateWorkflow.async_update_scheduled_record() is deprecated; "
        "use AsyncRecordUpdateWorkflow.update_scheduled_record(...) via "
        "async_sdk.workflows.record_update instead.",
        category=DeprecationWarning,
    )
    async def async_update_scheduled_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        interval_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier_type: Literal["name", "id"] = "name",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Deprecated asynchronous alias for :meth:`update_scheduled_record`."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            subject_id=subject_identifier,
            subject_type=subject_identifier_type,
            interval_id=interval_identifier,
            interval_type=interval_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return await self.async_create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )

    def create_new_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Create a new unscheduled record for an existing subject."""
        self._ensure_sync_mode("create_new_record", "async_create_new_record")
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            subject_id=subject_identifier,
            subject_type=subject_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return self.create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )

    @deprecated(
        "RecordUpdateWorkflow.async_create_new_record() is deprecated; "
        "use AsyncRecordUpdateWorkflow.create_new_record(...) via "
        "async_sdk.workflows.record_update instead.",
        category=DeprecationWarning,
    )
    async def async_create_new_record(
        self,
        study_key: str,
        form_identifier: str | int,
        subject_identifier: str | int,
        data: dict[str, Any],
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Deprecated asynchronous alias for :meth:`create_new_record`."""
        rec = build_record_payload(
            form_identifier,
            form_identifier_type,
            data,
            subject_id=subject_identifier,
            subject_type=subject_identifier_type,
        )
        opts = (wait_for_completion, timeout, poll_interval, email_notify, schema)
        return await self.async_create_or_update_records(
            **build_single_record_call_kwargs(study_key, rec, *opts)
        )


__all__ = ["AsyncRecordUpdateWorkflow", "RecordUpdateWorkflow"]
