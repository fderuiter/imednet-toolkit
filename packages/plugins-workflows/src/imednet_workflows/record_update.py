"""Utilities for submitting and updating records in iMedNet studies."""

from typing import TYPE_CHECKING, Any, Literal, Union, cast

from imednet.spi.models import Job
from imednet.spi.utils import (
    DEFAULT_BATCH_CHUNK_SIZE,
    AsyncBatchCoordinator,
    BatchCoordinator,
    BatchSubmissionSummary,
    JobPollSummary,
)
from imednet.spi.validation import AsyncSchemaValidator, SchemaCache, SchemaValidator

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

FacadeLike = Union["ImednetFacade", "AsyncImednetFacade"]


def _poll_many_sync(
    sdk: "ImednetFacade",
    study_key: str,
    batch_ids: list[str],
    *,
    timeout: int,
    poll_interval: int,
) -> JobPollSummary:
    """Poll multiple batch IDs via facade poll_jobs with fallback to poll_job."""
    poll_jobs_fn = getattr(sdk, "poll_jobs", None)
    if callable(poll_jobs_fn):
        candidate = poll_jobs_fn(
            study_key,
            batch_ids,
            timeout=float(timeout),
            interval=float(poll_interval),
        )
        if isinstance(candidate, JobPollSummary):
            return candidate

    results = {}
    failures = {}
    for bid in batch_ids:
        try:
            results[bid] = sdk.poll_job(study_key, bid, timeout=timeout, interval=poll_interval)
        except Exception as exc:
            failures[bid] = exc
            raise
    return JobPollSummary(
        study_key=study_key,
        results=results,
        failures=failures,
        elapsed_total=0.0,
    )


async def _poll_many_async(
    sdk: "AsyncImednetFacade",
    study_key: str,
    batch_ids: list[str],
    *,
    timeout: int,
    poll_interval: int,
) -> JobPollSummary:
    """Asynchronously poll multiple batch IDs via async_poll_jobs with fallback."""
    poll_jobs_fn = getattr(sdk, "async_poll_jobs", None)
    if callable(poll_jobs_fn):
        maybe_coro = poll_jobs_fn(
            study_key,
            batch_ids,
            timeout=float(timeout),
            interval=float(poll_interval),
        )
        if hasattr(maybe_coro, "__await__"):
            candidate = await maybe_coro
            if isinstance(candidate, JobPollSummary):
                return candidate

    results = {}
    failures = {}
    for bid in batch_ids:
        try:
            results[bid] = await sdk.async_poll_job(
                study_key, bid, timeout=timeout, interval=poll_interval
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


class RecordUpdateWorkflow:
    """Provides workflows for creating or updating records, including batch submission.

    and optional job status monitoring.

    Args:
        sdk: An instance of the ImednetFacade.
        batch_size: Maximum number of records per API POST chunk (default: 100).
    """

    def __init__(self, sdk: "FacadeLike", *, batch_size: int = DEFAULT_BATCH_CHUNK_SIZE):
        """Initialize the record update workflow with an SDK instance."""
        self._sdk = sdk
        self._batch_size = batch_size
        self._is_async = hasattr(sdk, "async_create_record")
        self._validator: SchemaValidator | AsyncSchemaValidator
        if self._is_async:
            self._validator = AsyncSchemaValidator(cast("AsyncImednetFacade", sdk))
        else:
            self._validator = SchemaValidator(cast("ImednetFacade", sdk))

        self._schema: SchemaCache = cast(SchemaCache, self._validator.schema)

    def _collect_distinct_form_refs(
        self, records_data: list[dict[str, Any]]
    ) -> tuple[list[str], list[int]]:
        """Collect distinct form keys and form IDs across all records in order."""
        seen_keys: set[str] = set()
        form_keys: list[str] = []
        seen_ids: set[int] = set()
        unresolved_form_ids: list[int] = []

        for rec in records_data:
            fk = rec.get("formKey") or rec.get("form_key")
            if fk:
                if fk not in seen_keys:
                    seen_keys.add(fk)
                    form_keys.append(fk)
                continue

            fid = rec.get("formId") or rec.get("form_id") or 0
            if fid:
                resolved = self._schema.form_key_from_id(fid)
                if resolved:
                    if resolved not in seen_keys:
                        seen_keys.add(resolved)
                        form_keys.append(resolved)
                elif fid not in seen_ids:
                    seen_ids.add(fid)
                    unresolved_form_ids.append(fid)

        return form_keys, unresolved_form_ids

    def _validate_form_key(self, study_key: str, records_data: list[dict[str, Any]]) -> None:
        """Refresh schema once if any distinct form key in the batch is uncached."""
        if not records_data:
            return
        form_keys, unresolved_ids = self._collect_distinct_form_refs(records_data)
        missing_before_refresh = [fk for fk in form_keys if not self._schema.variables_for_form(fk)]
        if missing_before_refresh or unresolved_ids:
            cast(SchemaValidator, self._validator).refresh(study_key)
            form_keys, _ = self._collect_distinct_form_refs(records_data)

        for ref in form_keys:
            if ref not in self._schema.forms:
                raise ValueError(f"Form key '{ref}' not found")

    async def _async_validate_form_key(
        self, study_key: str, records_data: list[dict[str, Any]]
    ) -> None:
        """Asynchronous variant of :meth:`_validate_form_key`."""
        if not records_data:
            return
        form_keys, unresolved_ids = self._collect_distinct_form_refs(records_data)
        missing_before_refresh = [fk for fk in form_keys if not self._schema.variables_for_form(fk)]
        if missing_before_refresh or unresolved_ids:
            await cast(AsyncSchemaValidator, self._validator).refresh(study_key)
            form_keys, _ = self._collect_distinct_form_refs(records_data)

        for ref in form_keys:
            if ref not in self._schema.forms:
                raise ValueError(f"Form key '{ref}' not found")

    def create_or_update_records_batch(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
    ) -> BatchSubmissionSummary:
        """Validate once, submit records in bounded chunks, and return a batch summary."""
        self._validate_form_key(study_key, records_data)
        sdk = cast("ImednetFacade", self._sdk)
        coordinator = BatchCoordinator(chunk_size=chunk_size or self._batch_size)
        return coordinator.submit(
            study_key,
            records_data,
            lambda chunk: sdk.create_record(study_key, chunk, schema=self._schema),
            schema=self._schema,
            validate_batch_fn=cast(SchemaValidator, self._validator).validate_batch,
            wait_for_completion=wait_for_completion,
            poll_single=lambda bid: sdk.poll_job(
                study_key, bid, timeout=timeout, interval=poll_interval
            ),
            poll_many=lambda bids: _poll_many_sync(
                sdk, study_key, bids, timeout=timeout, poll_interval=poll_interval
            ),
            require_batch_id=True,
        )

    def create_or_update_records(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> Job:
        """Submit records for creation or update and optionally wait for completion."""
        summary = self.create_or_update_records_batch(
            study_key=study_key,
            records_data=records_data,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        if summary.last_job is not None:
            return summary.last_job
        return Job(state="COMPLETED")

    async def async_create_or_update_records_batch(
        self,
        study_key: str,
        records_data: list[dict[str, Any]],
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
    ) -> BatchSubmissionSummary:
        """Asynchronously validate once, submit chunks, and return a batch summary."""
        await self._async_validate_form_key(study_key, records_data)
        sdk = cast("AsyncImednetFacade", self._sdk)
        coordinator = AsyncBatchCoordinator(chunk_size=chunk_size or self._batch_size)
        return await coordinator.submit(
            study_key,
            records_data,
            lambda chunk: sdk.async_create_record(study_key, chunk, schema=self._schema),
            schema=self._schema,
            validate_batch_fn=cast(AsyncSchemaValidator, self._validator).validate_batch,
            wait_for_completion=wait_for_completion,
            poll_single=lambda bid: sdk.async_poll_job(
                study_key, bid, timeout=timeout, interval=poll_interval
            ),
            poll_many=lambda bids: _poll_many_async(
                sdk, study_key, bids, timeout=timeout, poll_interval=poll_interval
            ),
            require_batch_id=True,
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
        summary = await self.async_create_or_update_records_batch(
            study_key=study_key,
            records_data=records_data,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        if summary.last_job is not None:
            return summary.last_job
        return Job(state="COMPLETED")

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
