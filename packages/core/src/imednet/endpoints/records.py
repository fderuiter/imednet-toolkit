"""Endpoint for managing records (form instances) in a study."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import NonCallableMock

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.operations.record_create import RecordCreateOperation
from imednet.core.endpoint.strategies import MappingParamProcessor
from imednet.endpoints.forms import AsyncFormsEndpoint, FormsEndpoint
from imednet.endpoints.jobs import AsyncJobsEndpoint, JobsEndpoint
from imednet.endpoints.variables import AsyncVariablesEndpoint, VariablesEndpoint
from imednet.models.jobs import Job
from imednet.models.records import Record
from imednet.utils.batch_coordinator import (
    DEFAULT_BATCH_CHUNK_SIZE,
    BatchSubmissionSummary,
    _submit_records_batch_async,
    _submit_records_batch_sync,
)
from imednet.utils.batch_ledger import BatchExecutionLedger
from imednet.utils.job_poller import JobProgressHandler
from imednet.utils.record_filter_eval import RECORDS_FILTER_ATTRIBUTES
from imednet.utils.typing import RecordsInput
from imednet.utils.write_pipeline import WriteIngestionPipeline
from imednet.validation.cache import ValidationSchemaProtocol, is_schema_record_prevalidated

if TYPE_CHECKING:
    import pandas as pd


@dataclass(frozen=True)
class _EndpointSchemaAdapter:
    """Lightweight forms/variables adapter for schema-aware dry-run in create_batch."""

    forms: Any
    variables: Any


def _build_sync_schema_adapter(client: Any) -> _EndpointSchemaAdapter | None:
    if client is None or isinstance(client, NonCallableMock):
        return None
    return _EndpointSchemaAdapter(
        forms=FormsEndpoint(client=client),
        variables=VariablesEndpoint(client=client),
    )


def _build_async_schema_adapter(async_client: Any) -> _EndpointSchemaAdapter | None:
    if async_client is None or isinstance(async_client, NonCallableMock):
        return None
    return _EndpointSchemaAdapter(
        forms=AsyncFormsEndpoint(async_client=async_client),
        variables=AsyncVariablesEndpoint(async_client=async_client),
    )


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
    FILTER_ATTRIBUTES = RECORDS_FILTER_ATTRIBUTES

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
        path = self._get_endpoint_path(study_key)  # type: ignore
        if (
            schema is not None
            and isinstance(records_data, Sequence)
            and not isinstance(records_data, (str, bytes))
            and bool(records_data)
            and all(
                isinstance(r, dict) and is_schema_record_prevalidated(schema, r)
                for r in records_data
            )
        ):
            return RecordCreateOperation[Job](
                path=path,
                records_data=list(records_data),  # type: ignore[arg-type]
                email_notify=email_notify,
                schema=schema,
                study_key=study_key,
                skip_validation=skip_validation,
            )
        envelopes = WriteIngestionPipeline.read_input(records_data)
        if not skip_validation and WriteIngestionPipeline.needs_schema_refresh(
            schema, envelopes, study_key=study_key
        ):
            return RecordCreateOperation[Job](
                path=path,
                records_data=[],
                email_notify=email_notify,
                schema=schema,
                study_key=study_key,
                skip_validation=skip_validation,
                raw_envelopes=envelopes,
            )
        normalized = WriteIngestionPipeline.bind(envelopes, schema=schema, study_key=study_key)
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
        ledger: BatchExecutionLedger | None = None,
        ledger_path: str | os.PathLike[str] | None = None,
        resume: bool = False,
        on_error: Literal["raise", "collect"] = "raise",
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
            sdk=_build_sync_schema_adapter(getattr(self, "_client", None)),
            schema=schema,
            chunk_size=chunk_size,
            skip_validation=skip_validation,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            interval=interval,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            ledger=ledger,
            ledger_path=ledger_path,
            resume=resume,
            on_error=on_error,
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
        ledger: BatchExecutionLedger | None = None,
        ledger_path: str | os.PathLike[str] | None = None,
        resume: bool = False,
        on_error: Literal["raise", "collect"] = "raise",
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
            sdk=_build_async_schema_adapter(getattr(self, "_async_client", None)),
            schema=schema,
            chunk_size=chunk_size,
            skip_validation=skip_validation,
            wait_for_completion=wait_for_completion,
            timeout=timeout,
            interval=interval,
            on_progress=on_progress,
            dry_run=dry_run,
            collect_errors=collect_errors,
            ledger=ledger,
            ledger_path=ledger_path,
            resume=resume,
            on_error=on_error,
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
