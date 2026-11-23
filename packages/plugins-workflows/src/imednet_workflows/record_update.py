"""Utilities for submitting and updating records in iMedNet studies."""

from typing import TYPE_CHECKING, Any, Literal, Union, cast

from imednet.spi.errors import ValidationError
from imednet.spi.models import Job
from imednet.spi.utils import (
    DEFAULT_BATCH_CHUNK_SIZE,
    AsyncBatchCoordinator,
    BatchCoordinator,
    BatchSubmissionSummary,
    JobProgressHandler,
    RecordsInput,
    extract_input_form_refs,
    normalize_records_input,
    poll_facade_many_async,
    poll_facade_many_sync,
    poll_facade_single_async,
    poll_facade_single_sync,
)
from imednet.spi.validation import (
    AsyncSchemaValidator,
    BaseSchemaCache,
    SchemaCache,
    SchemaValidator,
    ValidationSchemaProtocol,
)

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

FacadeLike = Union["ImednetFacade", "AsyncImednetFacade"]


def _extract_record_form_ref(record: dict[str, Any]) -> tuple[str | None, Any]:
    """Extract (form_key, form_id) from a record payload without coalescing None to 0."""
    raw_fk = record.get("formKey") or record.get("form_key")
    fk = str(raw_fk) if raw_fk else None
    raw_fid = record.get("formId") if record.get("formId") is not None else record.get("form_id")
    fid = None if raw_fid == "" else raw_fid
    return fk, fid


_poll_single_sync = poll_facade_single_sync
_poll_single_async = poll_facade_single_async
_poll_many_sync = poll_facade_many_sync
_poll_many_async = poll_facade_many_async


class RecordUpdateWorkflow:
    """Provides workflows for creating or updating records, including batch submission.

    and optional job status monitoring.

    Args:
        sdk: An instance of the ImednetFacade.
        batch_size: Maximum number of records per API POST chunk (default: 100).
        schema: Optional offline or pre-populated ValidationSchemaProtocol
            (e.g. DataDictionary or SchemaCache).
    """

    supports_async = True

    def __init__(
        self,
        sdk: "FacadeLike",
        *,
        batch_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        schema: ValidationSchemaProtocol | None = None,
    ) -> None:
        """Initialize the record update workflow with an SDK instance."""
        self._sdk = sdk
        self._batch_size = batch_size
        self._is_async = hasattr(sdk, "async_create_record")
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
        if self._is_async:
            raise TypeError(
                f"RecordUpdateWorkflow.{method_name}() requires a synchronous ImednetSDK "
                f"(ImednetFacade). With AsyncImednetSDK, await "
                f"workflow.{async_counterpart}(...) instead."
            )

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
        self, records_data: list[dict[str, Any]], cache: BaseSchemaCache[Any]
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

        _ = cache
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

    def _validate_form_key(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        *,
        already_refreshed: bool = False,
    ) -> None:
        """Refresh schema once if any distinct form key or form ID in the batch is uncached."""
        self._ensure_sync_mode("_validate_form_key", "_async_validate_form_key")
        if not records_data or not isinstance(self._schema, BaseSchemaCache):
            return
        cache = self._schema
        explicit_form_keys, form_ids = self._collect_distinct_form_refs(records_data, cache)
        if not already_refreshed and self._check_form_refs_need_refresh(
            cache, study_key, explicit_form_keys, form_ids
        ):
            cast(SchemaValidator, self._validator).refresh(study_key)
        self._verify_cached_form_refs(cache, study_key, explicit_form_keys, form_ids)

    async def _async_validate_form_key(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        *,
        already_refreshed: bool = False,
    ) -> None:
        """Asynchronous variant of :meth:`_validate_form_key`."""
        if not records_data or not isinstance(self._schema, BaseSchemaCache):
            return
        cache = self._schema
        explicit_form_keys, form_ids = self._collect_distinct_form_refs(records_data, cache)
        if not already_refreshed and self._check_form_refs_need_refresh(
            cache, study_key, explicit_form_keys, form_ids
        ):
            await cast(AsyncSchemaValidator, self._validator).refresh(study_key)
        self._verify_cached_form_refs(cache, study_key, explicit_form_keys, form_ids)

    def create_or_update_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once, submit records in bounded chunks, and return a batch summary."""
        self._ensure_sync_mode(
            "create_or_update_records_batch", "async_create_or_update_records_batch"
        )
        active_schema, is_offline = self._resolve_active_schema(schema)
        pre_refreshed = False
        if not is_offline and isinstance(self._schema, BaseSchemaCache):
            raw_fks, raw_fids = extract_input_form_refs(records_data)
            if self._check_form_refs_need_refresh(self._schema, study_key, raw_fks, raw_fids):
                cast(SchemaValidator, self._validator).refresh(study_key)
                pre_refreshed = True
        normalized = normalize_records_input(
            records_data,
            schema=active_schema,
            study_key=study_key,
        )
        if not is_offline:
            self._validate_form_key(study_key, normalized, already_refreshed=pre_refreshed)
        sdk = cast("ImednetFacade", self._sdk)
        coordinator = BatchCoordinator(chunk_size=chunk_size or self._batch_size)
        return coordinator.submit(
            study_key,
            normalized,
            lambda chunk: sdk.create_record(study_key, chunk, schema=active_schema),
            schema=active_schema,
            validate_batch_fn=(
                None if is_offline else cast(SchemaValidator, self._validator).validate_batch
            ),
            wait_for_completion=wait_for_completion,
            poll_single=lambda bid, *, on_progress=None: _poll_single_sync(
                sdk,
                study_key,
                bid,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            ),
            poll_many=lambda bids, *, on_progress=None: _poll_many_sync(
                sdk,
                study_key,
                bids,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            ),
            on_progress=on_progress,
            require_batch_id=True,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )

    def create_or_update_records(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Submit records for creation or update and optionally wait for completion."""
        self._ensure_sync_mode("create_or_update_records", "async_create_or_update_records")
        summary = self.create_or_update_records_batch(
            study_key=study_key,
            records_data=records_data,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )
        if summary.last_job is not None:
            return summary.last_job
        return Job.model_validate({"state": "COMPLETED"})

    async def async_create_or_update_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Asynchronously validate once, submit chunks, and return a batch summary."""
        active_schema, is_offline = self._resolve_active_schema(schema)
        pre_refreshed = False
        if not is_offline and isinstance(self._schema, BaseSchemaCache):
            raw_fks, raw_fids = extract_input_form_refs(records_data)
            if self._check_form_refs_need_refresh(self._schema, study_key, raw_fks, raw_fids):
                await cast(AsyncSchemaValidator, self._validator).refresh(study_key)
                pre_refreshed = True
        normalized = normalize_records_input(
            records_data,
            schema=active_schema,
            study_key=study_key,
        )
        if not is_offline:
            await self._async_validate_form_key(
                study_key,
                normalized,
                already_refreshed=pre_refreshed,
            )
        sdk = cast("AsyncImednetFacade", self._sdk)
        coordinator = AsyncBatchCoordinator(chunk_size=chunk_size or self._batch_size)
        return await coordinator.submit(
            study_key,
            normalized,
            lambda chunk: sdk.async_create_record(study_key, chunk, schema=active_schema),
            schema=active_schema,
            validate_batch_fn=(
                None if is_offline else cast(AsyncSchemaValidator, self._validator).validate_batch
            ),
            wait_for_completion=wait_for_completion,
            poll_single=lambda bid, *, on_progress=None: _poll_single_async(
                sdk,
                study_key,
                bid,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            ),
            poll_many=lambda bids, *, on_progress=None: _poll_many_async(
                sdk,
                study_key,
                bids,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            ),
            on_progress=on_progress,
            require_batch_id=True,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )

    async def async_create_or_update_records(
        self,
        study_key: str,
        records_data: RecordsInput,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> Job:
        """Asynchronous variant of :meth:`create_or_update_records`."""
        summary = await self.async_create_or_update_records_batch(
            study_key=study_key,
            records_data=records_data,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            schema=schema,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )
        if summary.last_job is not None:
            return summary.last_job
        return Job.model_validate({"state": "COMPLETED"})

    def _build_record_payload(
        self,
        *,
        form_identifier: str | int,
        form_identifier_type: Literal["key", "id"] = "key",
        data: dict[str, Any],
        subject_identifier: str | int | None = None,
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        site_identifier: str | int | None = None,
        site_identifier_type: Literal["name", "id"] = "name",
        interval_identifier: str | int | None = None,
        interval_identifier_type: Literal["name", "id"] = "name",
    ) -> dict[str, Any]:
        """Return a record payload for ``create_or_update_records``."""
        record: dict[str, Any] = {
            "formKey" if form_identifier_type == "key" else "formId": form_identifier,
            "data": data,
        }

        if subject_identifier is not None:
            subject_id_field_map = {
                "key": "subjectKey",
                "id": "subjectId",
                "oid": "subjectOid",
            }
            record[subject_id_field_map[subject_identifier_type]] = subject_identifier

        if site_identifier is not None:
            site_id_field_map = {
                "name": "siteName",
                "id": "siteId",
            }
            record[site_id_field_map[site_identifier_type]] = site_identifier

        if interval_identifier is not None:
            interval_id_field_map = {
                "name": "intervalName",
                "id": "intervalId",
            }
            record[interval_id_field_map[interval_identifier_type]] = interval_identifier

        return record

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
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Registers a new subject by submitting a single record.

        Args:
            study_key: The study key.
            form_identifier: The form key or ID.
            site_identifier: The site name or ID.
            data: The dictionary of record data (variable names and values).
            form_identifier_type: Whether `form_identifier` is a 'key' or 'id'.
            site_identifier_type: Whether `site_identifier` is a 'name' or 'id'.
            wait_for_completion: If True, wait for the job to complete.
            timeout: Timeout in seconds for waiting.
            poll_interval: Polling interval in seconds.
            schema: Optional validation schema override.

        Returns:
            The Job status object.
        """
        self._ensure_sync_mode("register_subject", "async_register_subject")
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            site_identifier=site_identifier,
            site_identifier_type=site_identifier_type,
            data=data,
        )
        return self._submit_single_record(
            study_key,
            record,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            schema=schema,
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
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Asynchronously register a new subject by submitting a single record."""
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            site_identifier=site_identifier,
            site_identifier_type=site_identifier_type,
            data=data,
        )
        return await self._async_submit_single_record(
            study_key,
            record,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            schema=schema,
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
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Updates an existing scheduled record for a subject.

        Args:
            study_key: The study key.
            form_identifier: The form key or ID.
            subject_identifier: The subject key, ID, or OID.
            interval_identifier: The interval name or ID.
            data: The dictionary of record data (variable names and values).
            form_identifier_type: Whether `form_identifier` is a 'key' or 'id'.
            subject_identifier_type: Whether `subject_identifier` is a 'key', 'id', or 'oid'.
            interval_identifier_type: Whether `interval_identifier` is a 'name' or 'id'.
            wait_for_completion: If True, wait for the job to complete.
            timeout: Timeout in seconds for waiting.
            poll_interval: Polling interval in seconds.
            schema: Optional validation schema override.

        Returns:
            The Job status object.
        """
        self._ensure_sync_mode("update_scheduled_record", "async_update_scheduled_record")
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            interval_identifier=interval_identifier,
            interval_identifier_type=interval_identifier_type,
            data=data,
        )
        return self._submit_single_record(
            study_key,
            record,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            schema=schema,
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
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Asynchronously update an existing scheduled record for a subject."""
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            interval_identifier=interval_identifier,
            interval_identifier_type=interval_identifier_type,
            data=data,
        )
        return await self._async_submit_single_record(
            study_key,
            record,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            schema=schema,
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
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Creates a new (unscheduled) record for an existing subject.

        Args:
            study_key: The study key.
            form_identifier: The form key or ID.
            subject_identifier: The subject key, ID, or OID.
            data: The dictionary of record data (variable names and values).
            form_identifier_type: Whether `form_identifier` is a 'key' or 'id'.
            subject_identifier_type: Whether `subject_identifier` is a 'key', 'id', or 'oid'.
            wait_for_completion: If True, wait for the job to complete.
            timeout: Timeout in seconds for waiting.
            poll_interval: Polling interval in seconds.
            schema: Optional validation schema override.

        Returns:
            The Job status object.
        """
        self._ensure_sync_mode("create_new_record", "async_create_new_record")
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            data=data,
        )
        return self._submit_single_record(
            study_key,
            record,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            schema=schema,
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
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Asynchronously create a new (unscheduled) record for an existing subject."""
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            data=data,
        )
        return await self._async_submit_single_record(
            study_key,
            record,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            schema=schema,
        )

    def _submit_single_record(
        self,
        study_key: str,
        record: dict[str, Any],
        *,
        wait_for_completion: bool,
        timeout: int,
        poll_interval: int,
        schema: ValidationSchemaProtocol | None,
    ) -> Job:
        """Submit a single constructed record payload synchronously."""
        extra_kwargs: dict[str, Any] = {"schema": schema} if schema is not None else {}
        return self.create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            **extra_kwargs,
        )

    async def _async_submit_single_record(
        self,
        study_key: str,
        record: dict[str, Any],
        *,
        wait_for_completion: bool,
        timeout: int,
        poll_interval: int,
        schema: ValidationSchemaProtocol | None,
    ) -> Job:
        """Submit a single constructed record payload asynchronously."""
        extra_kwargs: dict[str, Any] = {"schema": schema} if schema is not None else {}
        return await self.async_create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            **extra_kwargs,
        )
