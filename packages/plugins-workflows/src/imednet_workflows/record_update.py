"""Utilities for submitting and updating records in iMedNet studies."""

from typing import TYPE_CHECKING, Any, Literal, Union, cast

from imednet.spi.models import Job
from imednet.spi.validation import AsyncSchemaValidator, SchemaCache, SchemaValidator

from ._guards import ensure_async_sdk, ensure_sync_sdk, is_async_sdk

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

FacadeLike = Union["ImednetFacade", "AsyncImednetFacade"]
_OWNER = "RecordUpdateWorkflow"


class RecordUpdateWorkflow:
    """Provides workflows for creating or updating records, including batch submission.

    and optional job status monitoring.

    Args:
        sdk: An instance of the ImednetFacade or AsyncImednetFacade.
    """

    def __init__(self, sdk: "FacadeLike"):
        """Initialize the record update workflow with an SDK instance."""
        self._sdk = sdk
        self._is_async = is_async_sdk(sdk)
        self._validator: SchemaValidator | AsyncSchemaValidator
        if self._is_async:
            self._validator = AsyncSchemaValidator(cast("AsyncImednetFacade", sdk))
        else:
            self._validator = SchemaValidator(cast("ImednetFacade", sdk))

        self._schema: SchemaCache = cast(SchemaCache, self._validator.schema)

    def _first_form_ref(self, records_data: list[dict[str, Any]]) -> str | None:
        """Resolve the form key reference for the first record in a batch."""
        if not records_data:
            return None
        return records_data[0].get("formKey") or self._schema.form_key_from_id(
            records_data[0].get("formId", 0)
        )

    def _validate_form_key(self, study_key: str, records_data: list[dict[str, Any]]) -> None:
        """Refresh schema if the form key is missing from cache."""
        first_ref = self._first_form_ref(records_data)
        if first_ref and not self._schema.variables_for_form(first_ref):
            cast(SchemaValidator, self._validator).refresh(study_key)
            if first_ref not in self._schema.forms:
                raise ValueError(f"Form key '{first_ref}' not found")

    async def _async_validate_form_key(
        self, study_key: str, records_data: list[dict[str, Any]]
    ) -> None:
        """Asynchronous variant of :meth:`_validate_form_key`."""
        first_ref = self._first_form_ref(records_data)
        if first_ref and not self._schema.variables_for_form(first_ref):
            await cast(AsyncSchemaValidator, self._validator).refresh(study_key)
            if first_ref not in self._schema.forms:
                raise ValueError(f"Form key '{first_ref}' not found")

    def create_or_update_records(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Job:
        """Submit records for creation or update and optionally wait for completion."""
        ensure_sync_sdk(
            self._sdk,
            _OWNER,
            "create_or_update_records",
            "async_create_or_update_records",
        )
        self._validate_form_key(study_key, records_data)

        cast(SchemaValidator, self._validator).validate_batch(study_key, records_data)
        sdk = cast("ImednetFacade", self._sdk)
        job = sdk.create_record(study_key, records_data, schema=self._schema)

        if not wait_for_completion:
            return job
        if not job.batch_id:
            raise ValueError("Submission successful but no batch_id received.")

        return cast(
            "Job", sdk.poll_job(study_key, job.batch_id, timeout=timeout, interval=poll_interval)
        )

    async def async_create_or_update_records(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Job:
        """Asynchronous variant of :meth:`create_or_update_records`."""
        ensure_async_sdk(
            self._sdk,
            _OWNER,
            "async_create_or_update_records",
            "create_or_update_records",
        )
        await self._async_validate_form_key(study_key, records_data)

        await cast(AsyncSchemaValidator, self._validator).validate_batch(study_key, records_data)
        sdk = cast("AsyncImednetFacade", self._sdk)
        job = await sdk.async_create_record(study_key, records_data, schema=self._schema)

        if not wait_for_completion:
            return job
        if not job.batch_id:
            raise ValueError("Submission successful but no batch_id received.")

        return cast(
            "Job",
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
    ) -> Job:
        """Registers a new subject by submitting a single record."""
        ensure_sync_sdk(self._sdk, _OWNER, "register_subject", "async_register_subject")
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            site_identifier=site_identifier,
            site_identifier_type=site_identifier_type,
            data=data,
        )
        return self.create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
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
    ) -> Job:
        """Asynchronous variant of :meth:`register_subject`."""
        ensure_async_sdk(self._sdk, _OWNER, "async_register_subject", "register_subject")
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            site_identifier=site_identifier,
            site_identifier_type=site_identifier_type,
            data=data,
        )
        return await self.async_create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
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
    ) -> Job:
        """Updates an existing scheduled record for a subject."""
        ensure_sync_sdk(
            self._sdk,
            _OWNER,
            "update_scheduled_record",
            "async_update_scheduled_record",
        )
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            interval_identifier=interval_identifier,
            interval_identifier_type=interval_identifier_type,
            data=data,
        )
        return self.create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
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
    ) -> Job:
        """Asynchronous variant of :meth:`update_scheduled_record`."""
        ensure_async_sdk(
            self._sdk,
            _OWNER,
            "async_update_scheduled_record",
            "update_scheduled_record",
        )
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            interval_identifier=interval_identifier,
            interval_identifier_type=interval_identifier_type,
            data=data,
        )
        return await self.async_create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
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
    ) -> Job:
        """Creates a new (unscheduled) record for an existing subject."""
        ensure_sync_sdk(self._sdk, _OWNER, "create_new_record", "async_create_new_record")
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            data=data,
        )
        return self.create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
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
    ) -> Job:
        """Asynchronous variant of :meth:`create_new_record`."""
        ensure_async_sdk(self._sdk, _OWNER, "async_create_new_record", "create_new_record")
        record = self._build_record_payload(
            form_identifier=form_identifier,
            form_identifier_type=form_identifier_type,
            subject_identifier=subject_identifier,
            subject_identifier_type=subject_identifier_type,
            data=data,
        )
        return await self.async_create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
        )
