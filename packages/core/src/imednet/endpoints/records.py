"""Endpoint for managing records (form instances) in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.operations.record_create import RecordCreateOperation
from imednet.core.endpoint.strategies import MappingParamProcessor
from imednet.endpoints.jobs import AsyncJobsEndpoint, JobsEndpoint
from imednet.models.jobs import Job
from imednet.models.records import Record
from imednet.utils.batch_coordinator import (
    DEFAULT_BATCH_CHUNK_SIZE,
    BatchSubmissionSummary,
    _submit_records_batch_async,
    _submit_records_batch_sync,
)
from imednet.utils.job_poller import JobProgressHandler
from imednet.utils.tabular import normalize_records_input
from imednet.utils.typing import RecordsInput
from imednet.validation.cache import ValidationSchemaProtocol

if TYPE_CHECKING:
    import pandas as pd


class RecordsOperationDef:
    """Definition for Record operations."""

    PATH = "records"
    MODEL = Record
    _id_param = "recordId"
    _lookup_key_hint = "recordOid"
    PARAM_PROCESSOR = MappingParamProcessor(
        {
            "record_data_filter": "recordDataFilter",
            "recordDataFilter": "recordDataFilter",
        }
    )
    FILTER_ATTRIBUTES = frozenset(
        {
            "intervalId",
            "formId",
            "formKey",
            "siteId",
            "recordId",
            "recordOid",
            "recordType",
            "recordStatus",
            "deleted",
            "dateCreated",
            "dateModified",
            "lastUpdated",
            "subjectId",
            "subjectOid",
            "subjectKey",
            "visitId",
            "parentRecordId",
        }
    )

    @execute_operation
    def create(
        self,
        study_key: str,
        records_data: RecordsInput,
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        skip_validation: bool = False,
    ) -> RecordCreateOperation[Job]:
        """Create one or more records in a study.

        Args:
            study_key: The study key.
            records_data: Record dicts, a pandas DataFrame, or a CSV file path.
            email_notify: Whether to send email notifications.
            schema: Optional validation schema (SchemaCache or DataDictionary).
            skip_validation: Whether to skip schema validation if already validated.

        Returns:
            The background job for the creation operation.
        """
        self._validate_study_key(study_key)  # type: ignore
        normalized = normalize_records_input(records_data, schema=schema)
        path = self._get_endpoint_path(study_key)  # type: ignore
        return RecordCreateOperation[Job](
            path=path,
            records_data=normalized,
            email_notify=email_notify,
            schema=schema,
            study_key=study_key,
            skip_validation=skip_validation,
        )


class RecordsEndpoint(RecordsOperationDef, EdcSyncListGetEndpoint[Record]):  # type: ignore[misc]
    """API endpoint for interacting with records (form instances)."""

    def create_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        skip_validation: bool = False,
        wait_for_completion: bool = False,
        timeout: float = 300.0,
        interval: float = 5.0,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once, submit records in bounded chunks, and optionally poll jobs."""
        return _submit_records_batch_sync(
            study_key,
            records_data,
            email_notify,
            validate_study_key_fn=self._validate_study_key,
            create_records_fn=self.create,
            poll_single_fn=lambda sk, bid, **kw: JobsEndpoint(
                client=self._require_sync_client()
            ).poll(sk, bid, **kw),
            poll_many_fn=lambda sk, bids, **kw: JobsEndpoint(
                client=self._require_sync_client()
            ).poll_many(sk, bids, **kw),
            schema=schema,
            chunk_size=chunk_size,
            skip_validation=skip_validation,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            interval=interval,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )

    def to_dataframe(
        self,
        study_key: str | None = None,
        *,
        flatten: bool = False,
        mask_phi: bool = True,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried records directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            flatten: Whether to flatten record_data dictionary into columns.
            mask_phi: Whether to mask sensitive PHI fields by default.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried records.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, records_to_dataframe

        check_pandas("records_to_dataframe")
        records = list(self.list(study_key=study_key, **filters))
        return records_to_dataframe(records, flatten=flatten, mask_phi=mask_phi)


class AsyncRecordsEndpoint(RecordsOperationDef, EdcAsyncListGetEndpoint[Record]):  # type: ignore[misc]
    """Async API endpoint for interacting with records (form instances)."""

    async def create_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        skip_validation: bool = False,
        wait_for_completion: bool = False,
        timeout: float = 300.0,
        interval: float = 5.0,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once, asynchronously submit records in bounded chunks, and optionally poll."""
        return await _submit_records_batch_async(
            study_key,
            records_data,
            email_notify,
            validate_study_key_fn=self._validate_study_key,
            create_records_fn=self.create,
            poll_single_fn=lambda sk, bid, **kw: AsyncJobsEndpoint(
                async_client=self._require_async_client()
            ).poll(sk, bid, **kw),
            poll_many_fn=lambda sk, bids, **kw: AsyncJobsEndpoint(
                async_client=self._require_async_client()
            ).poll_many(sk, bids, **kw),
            schema=schema,
            chunk_size=chunk_size,
            skip_validation=skip_validation,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            interval=interval,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )

    async def to_dataframe(
        self,
        study_key: str | None = None,
        *,
        flatten: bool = False,
        mask_phi: bool = True,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried records asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            flatten: Whether to flatten record_data dictionary into columns.
            mask_phi: Whether to mask sensitive PHI fields by default.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried records.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, records_to_dataframe

        check_pandas("records_to_dataframe")
        records = [record async for record in self.list(study_key=study_key, **filters)]
        return records_to_dataframe(records, flatten=flatten, mask_phi=mask_phi)
