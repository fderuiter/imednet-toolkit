"""Utilities for submitting and updating records in iMedNet studies."""

from typing import TYPE_CHECKING, Any, Literal, Union, cast

from imednet.spi.models import Job
from imednet.spi.validation import AsyncSchemaValidator, SchemaCache, SchemaValidator

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

FacadeLike = Union["ImednetFacade", "AsyncImednetFacade"]


class RecordUpdateWorkflow:
    """Provides workflows for creating or updating records, including batch submission.

    and optional job status monitoring.

    Args:
        sdk: An instance of the ImednetFacade.
    """

    def __init__(self, sdk: "FacadeLike"):
        """Initialize the record update workflow with an SDK instance."""
        self._sdk = sdk
        self._is_async = hasattr(sdk, "async_create_record")
        self._validator: SchemaValidator | AsyncSchemaValidator
        if self._is_async:
            self._validator = AsyncSchemaValidator(cast("AsyncImednetFacade", sdk))
        else:
            self._validator = SchemaValidator(cast("ImednetFacade", sdk))

        self._schema: SchemaCache = cast(SchemaCache, self._validator.schema)

    def _validate_form_key(self, study_key: str, records_data: list[dict[str, Any]]) -> None:
        """Refresh schema if the form key is missing from cache."""
        if not records_data:
            return
        first_ref = records_data[0].get("formKey") or self._schema.form_key_from_id(
            records_data[0].get("formId", 0)
        )
        if first_ref and not self._schema.variables_for_form(first_ref):
            cast(SchemaValidator, self._validator).refresh(study_key)
            if first_ref not in self._schema.forms:
                raise ValueError(f"Form key '{first_ref}' not found")

    async def _async_validate_form_key(
        self, study_key: str, records_data: list[dict[str, Any]]
    ) -> None:
        """Asynchronous variant of :meth:`_validate_form_key`."""
        if not records_data:
            return
        first_ref = records_data[0].get("formKey") or self._schema.form_key_from_id(
            records_data[0].get("formId", 0)
        )
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
        self._validate_form_key(study_key, records_data)

        cast(SchemaValidator, self._validator).validate_batch(study_key, records_data)
        sdk = cast("ImednetFacade", self._sdk)
        job = sdk.create_record(study_key, records_data, schema=self._schema)

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
    ) -> Job:
        """Asynchronous variant of :meth:`create_or_update_records`."""
        await self._async_validate_form_key(study_key, records_data)

        await cast(AsyncSchemaValidator, self._validator).validate_batch(study_key, records_data)
        sdk = cast("AsyncImednetFacade", self._sdk)
        job = await sdk.async_create_record(study_key, records_data, schema=self._schema)

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
        return self.create_or_update_records(
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
        return self.create_or_update_records(
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
        return self.create_or_update_records(
            study_key=study_key,
            records_data=[record],
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
        )
