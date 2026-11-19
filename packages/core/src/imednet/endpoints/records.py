"""Endpoint for managing records (form instances) in a study."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.operations.record_create import RecordCreateOperation
from imednet.core.endpoint.strategies import MappingParamProcessor
from imednet.models.jobs import Job
from imednet.models.records import Record
from imednet.utils.batch_coordinator import (
    DEFAULT_BATCH_CHUNK_SIZE,
    AsyncBatchCoordinator,
    BatchCoordinator,
    BatchSubmissionSummary,
)
from imednet.utils.typing import JsonDict
from imednet.validation.cache import ValidationSchemaProtocol

if TYPE_CHECKING:
    import pandas as pd


class RecordsOperationDef:
    """Definition for Record operations."""

    PATH = "records"
    MODEL = Record
    _id_param = "recordId"
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
        records_data: list[JsonDict],
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        skip_validation: bool = False,
    ) -> RecordCreateOperation[Job]:
        """Create one or more records in a study.

        Args:
            study_key: The study key.
            records_data: The data for the records to create.
            email_notify: Whether to send email notifications.
            schema: Optional validation schema (SchemaCache or DataDictionary).
            skip_validation: Whether to skip schema validation if already validated.

        Returns:
            The background job for the creation operation.
        """
        path = self._get_endpoint_path(study_key)  # type: ignore
        return RecordCreateOperation[Job](
            path=path,
            records_data=records_data,
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
        records_data: list[JsonDict],
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        skip_validation: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once and submit records in bounded chunks (default 100 per POST)."""
        coordinator = BatchCoordinator(chunk_size=chunk_size)
        return coordinator.submit(
            study_key,
            records_data,
            lambda chunk: self.create(
                study_key,
                chunk,
                email_notify=email_notify,
                schema=schema,
                skip_validation=True,
            ),
            schema=schema,
            skip_validation=skip_validation,
        )

    def to_dataframe(
        self,
        study_key: str | None = None,
        *,
        flatten: bool = False,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried records directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            flatten: Whether to flatten record_data dictionary into columns.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried records.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, records_to_dataframe

        check_pandas("records_to_dataframe")
        records = list(self.list(study_key=study_key, **filters))
        return records_to_dataframe(records, flatten=flatten)


class AsyncRecordsEndpoint(RecordsOperationDef, EdcAsyncListGetEndpoint[Record]):  # type: ignore[misc]
    """Async API endpoint for interacting with records (form instances)."""

    async def create_batch(
        self,
        study_key: str,
        records_data: list[JsonDict],
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        skip_validation: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once and asynchronously submit records in bounded chunks."""
        coordinator = AsyncBatchCoordinator(chunk_size=chunk_size)
        return await coordinator.submit(
            study_key,
            records_data,
            lambda chunk: self.create(
                study_key,
                chunk,
                email_notify=email_notify,
                schema=schema,
                skip_validation=True,
            ),
            schema=schema,
            skip_validation=skip_validation,
        )

    async def to_dataframe(
        self,
        study_key: str | None = None,
        *,
        flatten: bool = False,
        **filters: Any,
    ) -> pd.DataFrame:
        """Export queried records asynchronously directly to a pandas DataFrame.

        Args:
            study_key: Optional study key override.
            flatten: Whether to flatten record_data dictionary into columns.
            **filters: Resource filters passed to :meth:`list`.

        Returns:
            A pandas DataFrame containing the queried records.

        Raises:
            ImportError: If pandas is not installed.
        """
        from imednet.utils.pandas import check_pandas, records_to_dataframe

        check_pandas("records_to_dataframe")
        records = [record async for record in self.list(study_key=study_key, **filters)]
        return records_to_dataframe(records, flatten=flatten)
