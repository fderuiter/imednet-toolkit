"""Utilities for submitting and updating records in iMedNet studies."""

from typing import TYPE_CHECKING, Any, Literal, Union, cast

from imednet.spi.errors import ValidationError
from imednet.spi.models import Job
from imednet.spi.validation import (
    AsyncSchemaValidator,
    BaseSchemaCache,
    SchemaCache,
    SchemaValidator,
    ValidationSchemaProtocol,
    validate_record_entry,
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


class RecordUpdateWorkflow:
    """Provides workflows for creating or updating records, including batch submission.

    and optional job status monitoring.

    Args:
        sdk: An instance of the ImednetFacade.
        schema: Optional offline or pre-populated ValidationSchemaProtocol
            (e.g. DataDictionary or SchemaCache).
    """

    def __init__(
        self,
        sdk: "FacadeLike",
        *,
        schema: ValidationSchemaProtocol | None = None,
    ) -> None:
        """Initialize the record update workflow with an SDK instance."""
        self._sdk = sdk
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

    def _resolve_active_schema(
        self, schema: ValidationSchemaProtocol | None
    ) -> tuple[ValidationSchemaProtocol, bool]:
        """Return ``(active_schema, is_explicit_or_offline)``."""
        if schema is not None:
            return schema, True
        if self._custom_schema is not None:
            return self._custom_schema, True
        return self._schema, not isinstance(self._schema, BaseSchemaCache)

    def _validate_form_key(self, study_key: str, records_data: list[dict[str, Any]]) -> None:
        """Refresh schema if the form key or form ID is missing from cache."""
        if not records_data or not isinstance(self._schema, BaseSchemaCache):
            return
        cache = self._schema
        fk, fid = _extract_record_form_ref(records_data[0])
        if fk:
            if not cache.variables_for_form(fk):
                cast(SchemaValidator, self._validator).refresh(study_key)
                if fk not in cache.forms:
                    raise ValueError(f"Form key '{fk}' not found")
        elif fid is not None:
            if isinstance(fid, bool):
                raise ValidationError(f"Unknown form ID {fid}")
            resolved = cache.form_key_from_id(fid)
            if not resolved or not cache.variables_for_form(resolved):
                cast(SchemaValidator, self._validator).refresh(study_key)
                resolved = cache.form_key_from_id(fid)
                if not resolved or resolved not in cache.forms:
                    raise ValidationError(f"Unknown form ID {fid}")

    async def _async_validate_form_key(
        self, study_key: str, records_data: list[dict[str, Any]]
    ) -> None:
        """Asynchronous variant of :meth:`_validate_form_key`."""
        if not records_data or not isinstance(self._schema, BaseSchemaCache):
            return
        cache = self._schema
        fk, fid = _extract_record_form_ref(records_data[0])
        if fk:
            if not cache.variables_for_form(fk):
                await cast(AsyncSchemaValidator, self._validator).refresh(study_key)
                if fk not in cache.forms:
                    raise ValueError(f"Form key '{fk}' not found")
        elif fid is not None:
            if isinstance(fid, bool):
                raise ValidationError(f"Unknown form ID {fid}")
            resolved = cache.form_key_from_id(fid)
            if not resolved or not cache.variables_for_form(resolved):
                await cast(AsyncSchemaValidator, self._validator).refresh(study_key)
                resolved = cache.form_key_from_id(fid)
                if not resolved or resolved not in cache.forms:
                    raise ValidationError(f"Unknown form ID {fid}")

    def create_or_update_records(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Submit records for creation or update and optionally wait for completion."""
        active_schema, is_offline = self._resolve_active_schema(schema)
        if is_offline:
            for rec in records_data:
                validate_record_entry(active_schema, rec)
        else:
            self._validate_form_key(study_key, records_data)
            cast(SchemaValidator, self._validator).validate_batch(study_key, records_data)

        sdk = cast("ImednetFacade", self._sdk)
        job = sdk.create_record(study_key, records_data, schema=active_schema)

        if not wait_for_completion:
            return job
        if not job.batch_id:
            raise ValueError("Submission successful but no batch_id received.")

        return cast(
            'Job', sdk.poll_job(study_key, job.batch_id, timeout=timeout, interval=poll_interval)
        )

    async def async_create_or_update_records(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Asynchronous variant of :meth:`create_or_update_records`."""
        active_schema, is_offline = self._resolve_active_schema(schema)
        if is_offline:
            for rec in records_data:
                validate_record_entry(active_schema, rec)
        else:
            await self._async_validate_form_key(study_key, records_data)
            await cast(AsyncSchemaValidator, self._validator).validate_batch(
                study_key, records_data
            )

        sdk = cast("AsyncImednetFacade", self._sdk)
        job = await sdk.async_create_record(study_key, records_data, schema=active_schema)

        if not wait_for_completion:
            return job
        if not job.batch_id:
            raise ValueError("Submission successful but no batch_id received.")

        return cast(
            'Job',
            await sdk.async_poll_job(
                study_key, job.batch_id, timeout=timeout, interval=poll_interval
            ),
        )

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
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            site_identifier=site_identifier,
            site_identifier_type=site_identifier_type,
            data=data,
        )
        extra_kwargs: dict[str, Any] = {}
        if schema is not None:
            extra_kwargs["schema"] = schema
        return self.create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            **extra_kwargs,
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
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            interval_identifier=interval_identifier,
            interval_identifier_type=interval_identifier_type,
            data=data,
        )
        extra_kwargs: dict[str, Any] = {}
        if schema is not None:
            extra_kwargs["schema"] = schema
        return self.create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            **extra_kwargs,
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
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            data=data,
        )
        extra_kwargs: dict[str, Any] = {}
        if schema is not None:
            extra_kwargs["schema"] = schema
        return self.create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
            **extra_kwargs,
        )
