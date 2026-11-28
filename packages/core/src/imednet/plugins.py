"""Plugin contracts for the iMednet SDK.

This module defines the :class:`PluginProtocol` that namespace-level plugin
factories satisfy when discovered via the ``imednet.plugins`` entry-point
group (such as ``sinks = "imednet_sinks.plugin:create_sinks"``), together with
mode-separated :class:`SyncWorkflowsNamespaceProtocol`,
:class:`AsyncWorkflowsNamespaceProtocol`, :class:`WorkflowsNamespaceProtocol`,
:class:`SinksNamespaceProtocol`, and :class:`AsyncSinksNamespaceProtocol`.

Individual workflows exposed on ``sdk.workflows`` are discovered lazily by
:class:`~imednet.sdk.WorkflowRegistry` via the ``imednet.workflows``
entry-point group, where each entry point maps a workflow attribute name to a
callable accepting the SDK instance.

Minimal plugin skeleton
-----------------------

To register an individual workflow on ``sdk.workflows``:

1. Expose a class or factory callable under ``[project.entry-points."imednet.workflows"]``.
2. The callable must accept a single argument — the :class:`~imednet.sdk.ImednetSDK`
   (or :class:`~imednet.sdk.AsyncImednetSDK`) instance — and return the workflow helper.

Example ``pyproject.toml`` snippet::

    [project.entry-points."imednet.workflows"]
    my_workflow = "myplugin.workflows:MyWorkflow"

    [project.entry-points."imednet.plugins"]
    sinks = "imednet_sinks.plugin:create_sinks"

Example workflow class::

    # myplugin/workflows.py
    from imednet.sdk import ImednetSDK

    class MyWorkflow:
        def __init__(self, sdk: ImednetSDK) -> None:
            self._sdk = sdk
"""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, overload, runtime_checkable

from typing_extensions import deprecated

if TYPE_CHECKING:
    import pandas as pd

    from .datadict import (
        DataDictionary,
        DatasetJsonBundle,
        DatasetJsonEnvelope,
        OdmAnnotation,
        OdmAuditRecord,
    )
    from .integrations.sink_base import AsyncExportSink, ExportSink
    from .models.forms import Form
    from .models.intervals import Interval
    from .models.jobs import Job
    from .models.queries import Query
    from .models.record_revisions import RecordRevision
    from .models.records import Record, RecordStatus, RegisterSubjectRequest
    from .models.sites import Site
    from .models.subjects import Subject
    from .models.triage import TriageItem, TriageStatus
    from .models.variables import Variable
    from .models.visits import Visit
    from .spi.facade import AsyncImednetFacade, ImednetFacade
    from .utils.batch_coordinator import BatchSubmissionSummary
    from .utils.job_poller import JobProgressHandler
    from .utils.typing import RecordsInput
    from .validation.cache import ValidationSchemaProtocol

if not TYPE_CHECKING:
    from typing import _ProtocolMeta

    class _WorkflowsRuntimeProtocolMeta(_ProtocolMeta):
        """Protocol metaclass supporting lazy descriptors and mode-aware WorkflowRegistry checks."""

        def __instancecheck__(self, instance: Any) -> bool:
            """Check WorkflowRegistry mode/entry-points or static descriptors without eager imports."""
            entry_points_map = getattr(instance, "_entry_points", None)
            is_async_fn = getattr(instance, "_is_async_sdk", None)
            if isinstance(entry_points_map, dict) and callable(is_async_fn):
                is_async = bool(is_async_fn())
                if self is SyncWorkflowsNamespaceProtocol:
                    required = {
                        "data_extraction",
                        "duckdb_centralizer",
                        "query_management",
                        "record_mapper",
                        "record_update",
                        "register_subjects",
                        "subject_data",
                        "uat",
                        "uat_inspector",
                    }
                    return (not is_async) and required.issubset(entry_points_map)
                if self is AsyncWorkflowsNamespaceProtocol:
                    required = {
                        "data_extraction",
                        "duckdb_centralizer",
                        "query_management",
                        "record_mapper",
                        "record_update",
                        "register_subjects",
                        "subject_data",
                        "uat",
                        "uat_inspector",
                    }
                    async_eps = getattr(instance, "_async_entry_points", None)
                    available = (
                        set(async_eps) | set(entry_points_map)
                        if isinstance(async_eps, dict)
                        else set(entry_points_map)
                    )
                    return is_async and required.issubset(available)
            if sys.version_info < (3, 12) and getattr(
                self, "_is_runtime_protocol", False
            ):  # pragma: no cover
                from typing import _get_protocol_attrs

                for attr in _get_protocol_attrs(self):
                    try:
                        inspect.getattr_static(instance, attr)
                    except AttributeError:
                        return False
                return True
            ann = getattr(self, "__annotations__", None)
            if isinstance(ann, dict) and ann:
                for attr in ann:
                    try:
                        inspect.getattr_static(instance, attr)
                    except AttributeError:
                        return False
                de = (
                    instance.__dict__.get("data_extraction")
                    if hasattr(instance, "__dict__")
                    else None
                )
                if de is not None and callable(getattr(de, "extract_records_by_criteria", None)):
                    is_coro = inspect.iscoroutinefunction(de.extract_records_by_criteria)
                    if self is SyncWorkflowsNamespaceProtocol and is_coro:
                        return False
                    if self is AsyncWorkflowsNamespaceProtocol and not is_coro:
                        return False
                return True
            return super().__instancecheck__(instance)


class DataExtractionWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.data_extraction``."""

    def extract_records_by_criteria(
        self,
        study_key: str,
        record_filter: dict[str, Any] | None = None,
        subject_filter: dict[str, Any] | None = None,
        visit_filter: dict[str, Any] | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        include_deleted: bool = False,
        record_data_filter: str | None = None,
        **other_filters: Any,
    ) -> list[Record]:
        """Extract study records matching combined subject, visit, and record filters."""
        ...

    def extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any] | None = None,
        **filters: Any,
    ) -> list[RecordRevision]:
        """Extract record revisions bounded by optional UTC dates and filters."""
        ...

    def extract_odm_audit_records(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any] | None = None,
        *,
        default_location_oid: str | None = None,
        mask_phi: bool = True,
        phi_keys: Collection[str] | None = None,
        **filters: Any,
    ) -> list[OdmAuditRecord]:
        """Extract record revisions mapped to CDISC ODM 1.3.2 AuditRecord models."""
        ...


class AsyncDataExtractionWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.data_extraction``."""

    async def extract_records_by_criteria(
        self,
        study_key: str,
        record_filter: dict[str, Any] | None = None,
        subject_filter: dict[str, Any] | None = None,
        visit_filter: dict[str, Any] | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        include_deleted: bool = False,
        record_data_filter: str | None = None,
        **other_filters: Any,
    ) -> list[Record]:
        """Extract study records matching combined subject, visit, and record filters."""
        ...

    async def extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any] | None = None,
        **filters: Any,
    ) -> list[RecordRevision]:
        """Extract record revisions bounded by optional UTC dates and filters."""
        ...

    async def extract_odm_audit_records(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any] | None = None,
        *,
        default_location_oid: str | None = None,
        mask_phi: bool = True,
        phi_keys: Collection[str] | None = None,
        **filters: Any,
    ) -> list[OdmAuditRecord]:
        """Asynchronously extract record revisions mapped to CDISC ODM 1.3.2 AuditRecord models."""
        ...


class DuckDBIngestionWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.duckdb_centralizer``."""

    def ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
        mask_phi: bool | None = None,
    ) -> int:
        """Ingest study record revisions into the DuckDB bronze table."""
        ...

    def build_silver_view(self, study_key: str, *, include_deleted: bool = False) -> None:
        """Create or replace the deduplicated DuckDB silver view for a study."""
        ...

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        ...

    def __enter__(self) -> Self:
        """Enter the context manager and return the workflow instance."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager and close the DuckDB connection."""
        ...


class AsyncDuckDBIngestionWorkflowProtocol(Protocol):
    """Asynchronous contract for ``sdk.workflows.duckdb_centralizer`` on ``AsyncImednetSDK``."""

    async def ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
        mask_phi: bool | None = None,
    ) -> int:
        """Asynchronously ingest study record revisions into the DuckDB bronze table."""
        ...

    async def build_silver_view(self, study_key: str, *, include_deleted: bool = False) -> None:
        """Asynchronously create or replace the deduplicated DuckDB silver view for a study."""
        ...

    async def aclose(self) -> None:
        """Asynchronously close the underlying DuckDB connection."""
        ...

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        ...

    async def __aenter__(self) -> Self:
        """Enter the async context manager and return the workflow instance."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the async context manager and close the DuckDB connection."""
        ...

    def __enter__(self) -> Self:
        """Enter the context manager and return the workflow instance."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager and close the DuckDB connection."""
        ...


class TriageStoreProtocol(Protocol):
    """Structural contract for triage stores used by ``sdk.workflows.query_management``."""

    def upsert_item(self, item: TriageItem) -> None:
        """Insert or update a triage item while preserving annotations and history."""
        ...

    def get_triage_item(self, item_id: str) -> TriageItem | None:
        """Fetch a single triage item by ID."""
        ...

    def get_queue(self, study_key: str, status: TriageStatus | None = None) -> list[TriageItem]:
        """Fetch the triage queue for a study, optionally filtered by status."""
        ...


class QueryManagementWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.query_management``."""

    def get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any] | None = None,
        include_answered: bool = False,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieve open queries for a study, optionally including answered queries."""
        ...

    def get_unresolved_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieve all unresolved (OPEN and ANSWERED) queries for a study."""
        ...

    def get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieve queries associated with a specific subject."""
        ...

    def get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieve queries associated with a specific site."""
        ...

    def get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Count queries grouped by query status."""
        ...

    def sync_queries_to_triage(
        self,
        study_key: str,
        store: TriageStoreProtocol | str | Path | None = None,
        *,
        additional_filter: dict[str, Any] | None = None,
        unresolved_only: bool = False,
        default_severity: str = "medium",
        **kwargs: Any,
    ) -> list[TriageItem]:
        """Synchronize clinical queries for a study into a local triage queue."""
        ...

    def extract_odm_annotations(
        self,
        study_key: str,
        additional_filter: dict[str, Any] | None = None,
        *,
        open_only: bool = False,
        include_answered: bool = False,
        mask_phi: bool = True,
        phi_keys: Collection[str] | None = None,
        **kwargs: Any,
    ) -> list[OdmAnnotation]:
        """Extract study queries mapped to CDISC ODM 1.3.2 Annotation models."""
        ...


class AsyncQueryManagementWorkflowProtocol(Protocol):
    """Asynchronous contract for ``sdk.workflows.query_management`` on ``AsyncImednetSDK``."""

    async def get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any] | None = None,
        include_answered: bool = False,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronously retrieve open queries for a study, optionally including answered."""
        ...

    async def get_unresolved_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronously retrieve all unresolved (OPEN and ANSWERED) queries for a study."""
        ...

    async def get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronously retrieve queries associated with a specific subject."""
        ...

    async def get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Asynchronously retrieve queries associated with a specific site."""
        ...

    async def get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Asynchronously count queries grouped by query status."""
        ...

    async def sync_queries_to_triage(
        self,
        study_key: str,
        store: TriageStoreProtocol | str | Path | None = None,
        *,
        additional_filter: dict[str, Any] | None = None,
        unresolved_only: bool = False,
        default_severity: str = "medium",
        **kwargs: Any,
    ) -> list[TriageItem]:
        """Asynchronously synchronize clinical queries into a local triage queue."""
        ...

    async def extract_odm_annotations(
        self,
        study_key: str,
        additional_filter: dict[str, Any] | None = None,
        *,
        open_only: bool = False,
        include_answered: bool = False,
        mask_phi: bool = True,
        phi_keys: Collection[str] | None = None,
        **kwargs: Any,
    ) -> list[OdmAnnotation]:
        """Asynchronously extract study queries mapped to CDISC ODM 1.3.2 Annotation models."""
        ...


class RecordMapperProtocol(Protocol):
    """Structural contract for ``sdk.workflows.record_mapper``."""

    def dataframe(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Return mapped study records as a pandas DataFrame."""
        ...

    def iter_dataframes(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> Iterator[pd.DataFrame]:
        """Yield mapped study record DataFrames in bounded chunks."""
        ...

    def build_hierarchy(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_keys: bool = False,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        records: Iterable[Record] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate a nested JSON hierarchy for study records."""
        ...

    def transform_to_sdtm(
        self,
        records: Iterable[Any],
        domain: str,
        study_key: str,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        seq_counters: dict[str, int] | None = None,
        record_seqs: dict[str, int] | None = None,
        preserve_record_id: bool = False,
    ) -> pd.DataFrame:
        """Transform records into a normalized SDTM domain DataFrame."""
        ...

    def sdtm_dataframe(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Return study records mapped to a CDISC SDTM domain DataFrame."""
        ...

    def iter_sdtm_dataframes(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> Iterator[pd.DataFrame]:
        """Yield chunked DataFrames mapped to a CDISC SDTM domain."""
        ...

    def to_dataset_json(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = False,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        naming_convention: str | None = None,
        preserve_record_id: bool = False,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        metadata_version_oid: str = "MDV.001",
        file_oid: str | None = None,
        creation_datetime: str | None = None,
        records: Iterable[Any] | None = None,
    ) -> DatasetJsonEnvelope:
        """Emit a CDISC Dataset-JSON v1.1 envelope for CDASH Forms or SDTM domains."""
        ...

    def to_dataset_json_bundle(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = False,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        naming_convention: str | None = None,
        preserve_record_id: bool = False,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        metadata_version_oid: str = "MDV.001",
        file_oid: str | None = None,
        creation_datetime: str | None = None,
        records: Iterable[Any] | None = None,
    ) -> DatasetJsonBundle:
        """Emit a multi-dataset CDISC Dataset-JSON v1.1 bundle for CDASH Forms or SDTM domains."""
        ...

    def sdtm_dataset_json(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        preserve_record_id: bool = False,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        metadata_version_oid: str = "MDV.001",
        file_oid: str | None = None,
        creation_datetime: str | None = None,
    ) -> DatasetJsonEnvelope:
        """Emit a CDISC Dataset-JSON v1.1 envelope for a specific SDTM domain."""
        ...


class AsyncRecordMapperProtocol(Protocol):
    """Structural contract for ``sdk.workflows.record_mapper``."""

    async def dataframe(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Return mapped study records as a pandas DataFrame."""
        ...

    def iter_dataframes(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = True,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> AsyncIterator[pd.DataFrame]:
        """Yield mapped study record DataFrames in bounded chunks."""
        ...

    async def build_hierarchy(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_keys: bool = False,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        records: Iterable[Record] | AsyncIterable[Record] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate a nested JSON hierarchy for study records."""
        ...

    def transform_to_sdtm(
        self,
        records: Iterable[Any],
        domain: str,
        study_key: str,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        seq_counters: dict[str, int] | None = None,
        record_seqs: dict[str, int] | None = None,
        preserve_record_id: bool = False,
    ) -> pd.DataFrame:
        """Transform records into a normalized SDTM domain DataFrame."""
        ...

    async def sdtm_dataframe(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> pd.DataFrame:
        """Return study records mapped to a CDISC SDTM domain DataFrame."""
        ...

    def iter_sdtm_dataframes(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
    ) -> AsyncIterator[pd.DataFrame]:
        """Yield chunked DataFrames mapped to a CDISC SDTM domain."""
        ...

    async def to_dataset_json(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = False,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        naming_convention: str | None = None,
        preserve_record_id: bool = False,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        metadata_version_oid: str = "MDV.001",
        file_oid: str | None = None,
        creation_datetime: str | None = None,
        records: Iterable[Any] | AsyncIterable[Any] | None = None,
    ) -> DatasetJsonEnvelope:
        """Asynchronously emit a CDISC Dataset-JSON v1.1 envelope for CDASH Forms or SDTM domains."""
        ...

    async def to_dataset_json_bundle(
        self,
        study_key: str,
        visit_key: str | None = None,
        use_labels_as_columns: bool = False,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        as_sdtm: bool = False,
        domain: str | None = None,
        naming_convention: str | None = None,
        preserve_record_id: bool = False,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        metadata_version_oid: str = "MDV.001",
        file_oid: str | None = None,
        creation_datetime: str | None = None,
        records: Iterable[Any] | AsyncIterable[Any] | None = None,
    ) -> DatasetJsonBundle:
        """Asynchronously emit a multi-dataset CDISC Dataset-JSON v1.1 bundle."""
        ...

    async def sdtm_dataset_json(
        self,
        study_key: str,
        domain: str,
        visit_key: str | None = None,
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
        *,
        preserve_record_id: bool = False,
        mask_phi: bool | None = None,
        include_deleted: bool | None = None,
        metadata_version_oid: str = "MDV.001",
        file_oid: str | None = None,
        creation_datetime: str | None = None,
    ) -> DatasetJsonEnvelope:
        """Asynchronously emit a CDISC Dataset-JSON v1.1 envelope for a specific SDTM domain."""
        ...


class SyncRecordUpdateWorkflowProtocol(Protocol):
    """Synchronous contract for ``sdk.workflows.record_update`` on ``ImednetSDK``."""

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
        resolve_results: bool = True,
    ) -> BatchSubmissionSummary:
        """Validate once, submit records in bounded chunks, and return a batch summary."""
        ...

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
        resolve_results: bool = True,
    ) -> Job:
        """Submit records for creation or update and return the resulting Job."""
        ...

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
        subject_key: str | None = None,
        subject_oid: str | None = None,
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Register a new subject by submitting a single enrollment record."""
        ...

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
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Update an existing scheduled record for a subject."""
        ...

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
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Create a new unscheduled record for an existing subject."""
        ...

    def update_record(
        self,
        study_key: str,
        data: dict[str, Any] | None = None,
        *,
        record_id: int | None = None,
        record_oid: str | None = None,
        visit_id: int | None = None,
        form_identifier: str | int | None = None,
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier: str | int | None = None,
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier: str | int | None = None,
        interval_identifier_type: Literal["name", "id"] = "name",
        site_identifier: str | int | None = None,
        site_identifier_type: Literal["name", "id"] = "name",
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Update an existing record by recordId, recordOid, visitId, or interval."""
        ...


class AsyncRecordUpdateWorkflowProtocol(Protocol):
    """Asynchronous contract for ``sdk.workflows.record_update`` on ``AsyncImednetSDK``."""

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
        resolve_results: bool = True,
    ) -> BatchSubmissionSummary:
        """Validate once, submit records in bounded chunks, and return a batch summary."""
        ...

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
        resolve_results: bool = True,
    ) -> Job:
        """Submit records for creation or update and return the resulting Job."""
        ...

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
        subject_key: str | None = None,
        subject_oid: str | None = None,
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Register a new subject by submitting a single enrollment record."""
        ...

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
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Update an existing scheduled record for a subject."""
        ...

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
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Create a new unscheduled record for an existing subject."""
        ...

    async def update_record(
        self,
        study_key: str,
        data: dict[str, Any] | None = None,
        *,
        record_id: int | None = None,
        record_oid: str | None = None,
        visit_id: int | None = None,
        form_identifier: str | int | None = None,
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier: str | int | None = None,
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier: str | int | None = None,
        interval_identifier_type: Literal["name", "id"] = "name",
        site_identifier: str | int | None = None,
        site_identifier_type: Literal["name", "id"] = "name",
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Asynchronously update an existing record by recordId, recordOid, visitId, or interval."""
        ...

    @deprecated(
        "async_create_or_update_records_batch() is deprecated; "
        "use await create_or_update_records_batch(...) instead."
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
        resolve_results: bool = True,
    ) -> BatchSubmissionSummary:
        """Asynchronously validate once, submit chunks, and return a batch summary."""
        ...

    @deprecated(
        "async_create_or_update_records() is deprecated; use await create_or_update_records(...) instead."
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
        resolve_results: bool = True,
    ) -> Job:
        """Asynchronously submit records for creation or update and return the Job."""
        ...

    @deprecated("async_register_subject() is deprecated; use await register_subject(...) instead.")
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
        subject_key: str | None = None,
        subject_oid: str | None = None,
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Asynchronously register a new subject via the Records API."""
        ...

    @deprecated(
        "async_update_scheduled_record() is deprecated; use await update_scheduled_record(...) instead."
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
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Asynchronously update an existing scheduled record for a subject."""
        ...

    @deprecated(
        "async_create_new_record() is deprecated; use await create_new_record(...) instead."
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
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Asynchronously create a new unscheduled record for an existing subject."""
        ...

    @deprecated("async_update_record() is deprecated; use await update_record(...) instead.")
    async def async_update_record(
        self,
        study_key: str,
        data: dict[str, Any] | None = None,
        *,
        record_id: int | None = None,
        record_oid: str | None = None,
        visit_id: int | None = None,
        form_identifier: str | int | None = None,
        form_identifier_type: Literal["key", "id"] = "key",
        subject_identifier: str | int | None = None,
        subject_identifier_type: Literal["key", "id", "oid"] = "key",
        interval_identifier: str | int | None = None,
        interval_identifier_type: Literal["name", "id"] = "name",
        site_identifier: str | int | None = None,
        site_identifier_type: Literal["name", "id"] = "name",
        record_status: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        email_notify: bool | str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        resolve_results: bool = True,
    ) -> Job:
        """Asynchronously update an existing record by recordId, recordOid, visitId, or interval."""
        ...


class SyncRegisterSubjectsWorkflowProtocol(Protocol):
    """Synchronous contract for ``sdk.workflows.register_subjects`` on ``ImednetSDK``."""

    def register_subjects_batch(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        resolve_results: bool = True,
    ) -> BatchSubmissionSummary:
        """Validate sites and submit subject registrations in bounded chunks."""
        ...

    def register_subjects(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
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
        resolve_results: bool = True,
    ) -> Job:
        """Register one or more subjects in a study."""
        ...

    async def async_register_subjects_batch(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        resolve_results: bool = True,
    ) -> BatchSubmissionSummary:
        """Asynchronously register subjects in bounded chunks."""
        ...

    async def async_register_subjects(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
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
        resolve_results: bool = True,
    ) -> Job:
        """Asynchronously register one or more subjects in a study."""
        ...


RegisterSubjectsWorkflowProtocol = SyncRegisterSubjectsWorkflowProtocol


class AsyncRegisterSubjectsWorkflowProtocol(Protocol):
    """Asynchronous contract for ``sdk.workflows.register_subjects`` on ``AsyncImednetSDK``."""

    async def register_subjects_batch(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        resolve_results: bool = True,
    ) -> BatchSubmissionSummary:
        """Asynchronously validate sites and submit subject registrations in bounded chunks."""
        ...

    async def register_subjects(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
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
        resolve_results: bool = True,
    ) -> Job:
        """Asynchronously register one or more subjects in a study."""
        ...

    @deprecated(
        "async_register_subjects_batch() is deprecated; "
        "use await register_subjects_batch(...) instead."
    )
    async def async_register_subjects_batch(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
        email_notify: str | None = None,
        wait_for_completion: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        *,
        chunk_size: int | None = None,
        filter_enrolled: bool | None = None,
        skip_existing: bool = False,
        subject_key_field: str | None = None,
        schema: ValidationSchemaProtocol | None = None,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
        resolve_results: bool = True,
    ) -> BatchSubmissionSummary:
        """Deprecated alias for :meth:`register_subjects_batch`."""
        ...

    @deprecated(
        "async_register_subjects() is deprecated; use await register_subjects(...) instead."
    )
    async def async_register_subjects(
        self,
        study_key: str,
        subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | RecordsInput,
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
        resolve_results: bool = True,
    ) -> Job:
        """Deprecated alias for :meth:`register_subjects`."""
        ...


class SubjectComprehensiveDataProtocol(Protocol):
    """Structural contract for aggregated subject data returned by ``subject_data``."""

    @property
    def subject_details(self) -> Subject | None:
        """Core details of the subject."""
        ...

    @property
    def visits(self) -> list[Visit]:
        """Visits associated with the subject."""
        ...

    @property
    def records(self) -> list[Record]:
        """Records associated with the subject."""
        ...

    @property
    def queries(self) -> list[Query]:
        """Queries associated with the subject."""
        ...


class SubjectDataWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.subject_data``."""

    def get_all_subject_data(
        self,
        study_key: str,
        subject_key: str,
        *,
        include_deleted: bool = False,
    ) -> SubjectComprehensiveDataProtocol:
        """Retrieve subject details, visits, records, and queries for a subject."""
        ...


class AsyncSubjectDataWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.subject_data``."""

    async def get_all_subject_data(
        self,
        study_key: str,
        subject_key: str,
        *,
        include_deleted: bool = False,
    ) -> SubjectComprehensiveDataProtocol:
        """Retrieve subject details, visits, records, and queries for a subject."""
        ...


class StudySnapshotProtocol(Protocol):
    """Structural contract for a study metadata snapshot."""

    @property
    def study_key(self) -> str:
        """Study key identifier."""
        ...

    @property
    def captured_at(self) -> datetime:
        """UTC timestamp when the snapshot was captured."""
        ...

    @property
    def forms(self) -> list[Form]:
        """Study form definitions."""
        ...

    @property
    def variables(self) -> list[Variable]:
        """Study variable definitions."""
        ...

    @property
    def intervals(self) -> list[Interval]:
        """Study interval definitions."""
        ...

    @property
    def sites(self) -> list[Site]:
        """Study site definitions."""
        ...

    @property
    def forms_by_key(self) -> dict[str, Form]:
        """Lookup dictionary mapping form keys to Form models."""
        ...

    @property
    def variables_by_form(self) -> dict[str, list[Variable]]:
        """Lookup dictionary mapping form keys to Variable lists."""
        ...

    @property
    def intervals_by_name(self) -> dict[str, Interval]:
        """Lookup dictionary mapping interval names to Interval models."""
        ...

    def enrollment_forms(self) -> list[Form]:
        """Return forms configured for subject enrollment/registration."""
        ...

    def scheduled_forms(self) -> list[Form]:
        """Return scheduled non-enrollment forms."""
        ...

    def unscheduled_forms(self) -> list[Form]:
        """Return unscheduled forms."""
        ...

    def active_sites(self) -> list[Site]:
        """Return actively enrolling sites."""
        ...


class SyncStudySchemaInspectorProtocol(Protocol):
    """Synchronous contract for ``sdk.workflows.uat_inspector`` on ``ImednetSDK``."""

    def inspect(self, study_key: str, *, force_refresh: bool = False) -> StudySnapshotProtocol:
        """Synchronously fetch and index structural study metadata."""
        ...

    def clear_cache(self, study_key: str | None = None) -> None:
        """Clear cached study snapshots."""
        ...


class AsyncStudySchemaInspectorProtocol(Protocol):
    """Asynchronous contract for ``sdk.workflows.uat_inspector`` on ``AsyncImednetSDK``."""

    async def inspect(
        self, study_key: str, *, force_refresh: bool = False
    ) -> StudySnapshotProtocol:
        """Asynchronously fetch and index structural study metadata concurrently."""
        ...

    def clear_cache(self, study_key: str | None = None) -> None:
        """Clear cached study snapshots."""
        ...

    @deprecated("async_inspect() is deprecated; use await inspect(...) instead.")
    async def async_inspect(
        self, study_key: str, *, force_refresh: bool = False
    ) -> StudySnapshotProtocol:
        """Deprecated alias for :meth:`inspect`."""
        ...


class UATRunResultProtocol(Protocol):
    """Structural contract for ``UATWorkflow.run`` execution results."""

    @property
    def study_key(self) -> str:
        """Study key tested."""
        ...

    @property
    def run_id(self) -> str:
        """Unique execution identifier."""
        ...

    @property
    def started_at(self) -> datetime:
        """UTC timestamp when the UAT run started."""
        ...

    @property
    def completed_at(self) -> datetime | None:
        """UTC timestamp when the UAT run finished, if completed."""
        ...

    @property
    def state(self) -> str:
        """Current or final lifecycle state value."""
        ...

    @property
    def state_history(self) -> Sequence[str]:
        """Ordered sequence of visited lifecycle states."""
        ...

    @property
    def failure_phase(self) -> str | None:
        """Phase in which execution failed, if any."""
        ...

    @property
    def failed_phase(self) -> str | None:
        """Alias for ``failure_phase``."""
        ...

    @property
    def error_message(self) -> str | None:
        """Failure message captured when a phase errors."""
        ...

    @property
    def is_terminal(self) -> bool:
        """Whether the run reached a terminal lifecycle state."""
        ...

    @property
    def overall_success(self) -> bool:
        """Whether all UAT batches completed without failure."""
        ...

    @property
    def spec(self) -> Any:
        """Generated or supplied UAT specification."""
        ...

    @property
    def record_sets(self) -> Sequence[Any]:
        """Generated synthetic record sets submitted during the run."""
        ...

    @property
    def submission_result(self) -> Any:
        """Bulk submission result produced by the submit phase."""
        ...

    @property
    def poll_summary(self) -> Any:
        """Job polling summary produced by the monitor phase."""
        ...

    @property
    def dry_run(self) -> bool:
        """Whether the run executed in local dry-run validation mode."""
        ...

    @property
    def validation_report(self) -> Any:
        """Local batch validation report captured during dry-run execution, if any."""
        ...

    @property
    def phase_durations(self) -> Mapping[str, float]:
        """Elapsed seconds recorded per execution phase."""
        ...

    def summary(self) -> str:
        """Format a human-readable execution summary."""
        ...


class UATWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.uat``."""

    @property
    def state(self) -> str:
        """Current lifecycle state of the UAT workflow."""
        ...

    @property
    def state_history(self) -> Sequence[str]:
        """Recorded lifecycle state transitions for the current or latest run."""
        ...

    @property
    def last_result(self) -> UATRunResultProtocol | None:
        """Result from the most recent ``run()`` invocation, if any."""
        ...

    def transition_to(self, target: str, *, dry_run: bool = False) -> str:
        """Transition workflow state along a valid lifecycle edge."""
        ...

    def run(
        self,
        study_key: str,
        *,
        spec: Any = None,
        subject_count: int = 2,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        email_notify: bool | str | None = None,
        data_dictionary: DataDictionary | None = None,
        schema: ValidationSchemaProtocol | None = None,
        seed: int | None = None,
        dry_run: bool = False,
    ) -> UATRunResultProtocol:
        """Execute the end-to-end UAT pipeline and return its run result."""
        ...

    def inspect(self, study_key: str) -> StudySnapshotProtocol:
        """Inspect study metadata via StudySchemaInspector."""
        ...

    def build_spec(self, snapshot: Any, **kwargs: Any) -> Any:
        """Auto-generate a UAT specification from a study snapshot."""
        ...

    def generate(
        self,
        spec: Any,
        snapshot: Any,
        *,
        data_dictionary: DataDictionary | None = None,
        seed: int | None = None,
    ) -> list[Any]:
        """Generate synthetic record sets from a UAT specification and snapshot."""
        ...

    def submit(
        self,
        study_key: str,
        record_sets: list[Any],
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Any:
        """Submit generated UAT record sets in two phases."""
        ...

    def monitor(self, study_key: str, submission_result: Any) -> Any:
        """Poll submitted UAT job batches to completion."""
        ...

    def reset(self) -> None:
        """Reset the UAT workflow state machine."""
        ...


class AsyncUATWorkflowProtocol(Protocol):
    """Structural contract for ``sdk.workflows.uat``."""

    @property
    def state(self) -> str:
        """Current lifecycle state of the UAT workflow."""
        ...

    @property
    def state_history(self) -> Sequence[str]:
        """Recorded lifecycle state transitions for the current or latest run."""
        ...

    @property
    def last_result(self) -> UATRunResultProtocol | None:
        """Result from the most recent ``run()`` invocation, if any."""
        ...

    def transition_to(self, target: str, *, dry_run: bool = False) -> str:
        """Transition workflow state along a valid lifecycle edge."""
        ...

    async def run(
        self,
        study_key: str,
        *,
        spec: Any = None,
        subject_count: int = 2,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        email_notify: bool | str | None = None,
        data_dictionary: DataDictionary | None = None,
        schema: ValidationSchemaProtocol | None = None,
        seed: int | None = None,
        dry_run: bool = False,
    ) -> UATRunResultProtocol:
        """Execute the end-to-end UAT pipeline and return its run result."""
        ...

    async def inspect(self, study_key: str) -> StudySnapshotProtocol:
        """Inspect study metadata via StudySchemaInspector."""
        ...

    def build_spec(self, snapshot: Any, **kwargs: Any) -> Any:
        """Auto-generate a UAT specification from a study snapshot."""
        ...

    def generate(
        self,
        spec: Any,
        snapshot: Any,
        *,
        data_dictionary: DataDictionary | None = None,
        seed: int | None = None,
    ) -> list[Any]:
        """Generate synthetic record sets from a UAT specification and snapshot."""
        ...

    async def submit(
        self,
        study_key: str,
        record_sets: list[Any],
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Any:
        """Submit generated UAT record sets in two phases."""
        ...

    async def monitor(self, study_key: str, submission_result: Any) -> Any:
        """Poll submitted UAT job batches to completion."""
        ...

    def reset(self) -> None:
        """Reset the UAT workflow state machine."""
        ...


@runtime_checkable
class SyncWorkflowsNamespaceProtocol(Protocol):
    """Synchronous workflows namespace contract exposed on ``ImednetSDK.workflows``."""

    @property
    def data_extraction(self) -> DataExtractionWorkflowProtocol:
        """Access the synchronous data extraction workflow."""
        ...

    @property
    def duckdb_centralizer(self) -> DuckDBIngestionWorkflowProtocol:
        """Access the lazy DuckDB bronze/silver ingestion workflow."""
        ...

    @property
    def query_management(self) -> QueryManagementWorkflowProtocol:
        """Access the query management workflow."""
        ...

    @property
    def record_mapper(self) -> RecordMapperProtocol:
        """Access the tabular and SDTM record mapper workflow."""
        ...

    @property
    def record_update(self) -> SyncRecordUpdateWorkflowProtocol:
        """Access the synchronous record submission and update workflow."""
        ...

    @property
    def register_subjects(self) -> SyncRegisterSubjectsWorkflowProtocol:
        """Access the synchronous bulk subject registration workflow."""
        ...

    @property
    def subject_data(self) -> SubjectDataWorkflowProtocol:
        """Access the comprehensive subject data retrieval workflow."""
        ...

    @property
    def uat(self) -> UATWorkflowProtocol:
        """Access the end-to-end UAT orchestration workflow."""
        ...

    @property
    def uat_inspector(self) -> SyncStudySchemaInspectorProtocol:
        """Access the synchronous study schema inspector."""
        ...


@runtime_checkable
class AsyncWorkflowsNamespaceProtocol(Protocol):
    """Asynchronous workflows namespace contract exposed on ``AsyncImednetSDK.workflows``."""

    @property
    def data_extraction(self) -> AsyncDataExtractionWorkflowProtocol:
        """Access the asynchronous data extraction workflow."""
        ...

    @property
    def duckdb_centralizer(self) -> AsyncDuckDBIngestionWorkflowProtocol:
        """Access the asynchronous DuckDB bronze/silver ingestion workflow."""
        ...

    @property
    def query_management(self) -> AsyncQueryManagementWorkflowProtocol:
        """Access the asynchronous query management workflow."""
        ...

    @property
    def record_mapper(self) -> AsyncRecordMapperProtocol:
        """Access the asynchronous tabular and SDTM record mapper workflow."""
        ...

    @property
    def record_update(self) -> AsyncRecordUpdateWorkflowProtocol:
        """Access the asynchronous record submission and update workflow."""
        ...

    @property
    def register_subjects(self) -> AsyncRegisterSubjectsWorkflowProtocol:
        """Access the asynchronous bulk subject registration workflow."""
        ...

    @property
    def subject_data(self) -> AsyncSubjectDataWorkflowProtocol:
        """Access the asynchronous comprehensive subject data retrieval workflow."""
        ...

    @property
    def uat(self) -> AsyncUATWorkflowProtocol:
        """Access the asynchronous end-to-end UAT orchestration workflow."""
        ...

    @property
    def uat_inspector(self) -> AsyncStudySchemaInspectorProtocol:
        """Access the asynchronous study schema inspector."""
        ...


WorkflowsNamespaceProtocol = SyncWorkflowsNamespaceProtocol

if not TYPE_CHECKING:
    SyncWorkflowsNamespaceProtocol.__annotations__ = {
        "data_extraction": DataExtractionWorkflowProtocol,
        "duckdb_centralizer": DuckDBIngestionWorkflowProtocol,
        "query_management": QueryManagementWorkflowProtocol,
        "record_mapper": RecordMapperProtocol,
        "record_update": SyncRecordUpdateWorkflowProtocol,
        "register_subjects": SyncRegisterSubjectsWorkflowProtocol,
        "subject_data": SubjectDataWorkflowProtocol,
        "uat": UATWorkflowProtocol,
        "uat_inspector": SyncStudySchemaInspectorProtocol,
    }
    AsyncWorkflowsNamespaceProtocol.__annotations__ = {
        "data_extraction": AsyncDataExtractionWorkflowProtocol,
        "duckdb_centralizer": AsyncDuckDBIngestionWorkflowProtocol,
        "query_management": AsyncQueryManagementWorkflowProtocol,
        "record_mapper": AsyncRecordMapperProtocol,
        "record_update": AsyncRecordUpdateWorkflowProtocol,
        "register_subjects": AsyncRegisterSubjectsWorkflowProtocol,
        "subject_data": AsyncSubjectDataWorkflowProtocol,
        "uat": AsyncUATWorkflowProtocol,
        "uat_inspector": AsyncStudySchemaInspectorProtocol,
    }
    SyncWorkflowsNamespaceProtocol.__class__ = _WorkflowsRuntimeProtocolMeta
    AsyncWorkflowsNamespaceProtocol.__class__ = _WorkflowsRuntimeProtocolMeta


class MongoDbSinkConfigProtocol(Protocol):
    """Structural contract for ``MongoDbSinkConfig`` instances."""

    study_key: str
    uri: str
    database: str
    collection: str
    batch_size: int
    max_buffered_records: int | None
    as_sdtm: bool
    sdtm_domain: str | None
    standards_profile: str | Any | None
    min_standards_score: float | None
    naming_convention: str | None
    preserve_record_id: bool | None
    idempotent: bool
    mask_phi: bool
    include_deleted: bool
    incremental: bool
    modified_since: datetime | str | None
    record_filter: dict[str, Any] | str | None


class MongoDbSinkConfigFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.MongoDbSinkConfig``."""

    def __call__(
        self,
        study_key: str,
        batch_size: int = 500,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        idempotent: bool = True,
        mask_phi: bool = True,
        extra: dict[str, Any] = ...,
        quality_gate_enabled: bool = False,
        min_schema_readiness_score: float = 100.0,
        tracer: Any | None = None,
        modified_since: datetime | str | None = None,
        record_filter: dict[str, Any] | str | None = None,
        record_data_filter: str | None = None,
        include_deleted: bool = False,
        incremental: bool = False,
        stream_name: str | None = None,
        ledger_path: str | Path | None = None,
        state_provider: Any | None = None,
        max_buffered_records: int | None = None,
        as_sdtm: bool = False,
        sdtm_domain: str | None = None,
        standards_profile: str | Any | None = None,
        min_standards_score: float | None = None,
        naming_convention: str | None = None,
        preserve_record_id: bool | None = None,
        uri: str = "",
        database: str = "",
        collection: str = "",
    ) -> MongoDbSinkConfigProtocol:
        """Instantiate a MongoDB sink configuration."""
        ...


class MongoDbExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.MongoDbExportSink``."""

    def __call__(self, config: Any) -> ExportSink:
        """Instantiate a MongoDB export sink."""
        ...


class AsyncMongoDbExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.AsyncMongoDbExportSink``."""

    def __call__(self, config: Any) -> AsyncExportSink:
        """Instantiate an asynchronous MongoDB export sink."""
        ...


class BoundExportToMongoDbProtocol(Protocol):
    """Bound callable contract for ``sdk.sinks.export_to_mongodb``."""

    @overload
    def __call__(
        self,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_mongodb() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def __call__(
        self,
        sdk: ImednetFacade,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to MongoDB using the bound SDK client."""
        ...


class BoundAsyncExportToMongoDbProtocol(Protocol):
    """Bound coroutine contract for ``async_sdk.sinks.export_to_mongodb``."""

    @overload
    async def __call__(
        self,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_mongodb() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    async def __call__(
        self,
        sdk: AsyncImednetFacade,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    async def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Asynchronously export study records to MongoDB using the bound ``AsyncImednetSDK``."""
        ...


class Neo4jSinkConfigProtocol(Protocol):
    """Structural contract for ``Neo4jSinkConfig`` instances."""

    study_key: str
    uri: str
    auth: tuple[str, str]
    database: str
    batch_size: int
    max_buffered_records: int | None
    as_sdtm: bool
    sdtm_domain: str | None
    standards_profile: str | Any | None
    min_standards_score: float | None
    naming_convention: str | None
    preserve_record_id: bool | None
    idempotent: bool
    mask_phi: bool
    include_deleted: bool
    incremental: bool
    modified_since: datetime | str | None
    record_filter: dict[str, Any] | str | None


class Neo4jSinkConfigFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.Neo4jSinkConfig``."""

    def __call__(
        self,
        study_key: str,
        batch_size: int = 500,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        idempotent: bool = True,
        mask_phi: bool = True,
        extra: dict[str, Any] = ...,
        quality_gate_enabled: bool = False,
        min_schema_readiness_score: float = 100.0,
        tracer: Any | None = None,
        modified_since: datetime | str | None = None,
        record_filter: dict[str, Any] | str | None = None,
        record_data_filter: str | None = None,
        include_deleted: bool = False,
        incremental: bool = False,
        stream_name: str | None = None,
        ledger_path: str | Path | None = None,
        state_provider: Any | None = None,
        max_buffered_records: int | None = None,
        as_sdtm: bool = False,
        sdtm_domain: str | None = None,
        standards_profile: str | Any | None = None,
        min_standards_score: float | None = None,
        naming_convention: str | None = None,
        preserve_record_id: bool | None = None,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        database: str = "neo4j",
    ) -> Neo4jSinkConfigProtocol:
        """Instantiate a Neo4j sink configuration."""
        ...


class Neo4jExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.Neo4jExportSink``."""

    def __call__(self, config: Any) -> ExportSink:
        """Instantiate a Neo4j export sink."""
        ...


class AsyncNeo4jExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.AsyncNeo4jExportSink``."""

    def __call__(self, config: Any) -> AsyncExportSink:
        """Instantiate an asynchronous Neo4j export sink."""
        ...


class BoundExportToNeo4jProtocol(Protocol):
    """Bound callable contract for ``sdk.sinks.export_to_neo4j``."""

    @overload
    def __call__(
        self,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_neo4j() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def __call__(
        self,
        sdk: ImednetFacade,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to Neo4j using the bound SDK client."""
        ...


class BoundAsyncExportToNeo4jProtocol(Protocol):
    """Bound coroutine contract for ``async_sdk.sinks.export_to_neo4j``."""

    @overload
    async def __call__(
        self,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_neo4j() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    async def __call__(
        self,
        sdk: AsyncImednetFacade,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Any = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    async def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Asynchronously export study records to Neo4j using the bound ``AsyncImednetSDK``."""
        ...


class SnowflakeSinkConfigProtocol(Protocol):
    """Structural contract for ``SnowflakeSinkConfig`` instances."""

    study_key: str
    account: str
    user: str
    database: str
    schema: str
    warehouse: str
    stage: str
    table: str
    stage_prefix: str
    batch_size: int
    max_buffered_records: int | None
    as_sdtm: bool
    sdtm_domain: str | None
    standards_profile: str | Any | None
    min_standards_score: float | None
    naming_convention: str | None
    preserve_record_id: bool | None
    idempotent: bool
    mask_phi: bool
    include_deleted: bool
    incremental: bool
    modified_since: datetime | str | None
    record_filter: dict[str, Any] | str | None


class SnowflakeSinkConfigFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.SnowflakeSinkConfig``."""

    def __call__(
        self,
        study_key: str,
        batch_size: int = 500,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        idempotent: bool = True,
        mask_phi: bool = True,
        extra: dict[str, Any] = ...,
        quality_gate_enabled: bool = False,
        min_schema_readiness_score: float = 100.0,
        tracer: Any | None = None,
        modified_since: datetime | str | None = None,
        record_filter: dict[str, Any] | str | None = None,
        record_data_filter: str | None = None,
        include_deleted: bool = False,
        incremental: bool = False,
        stream_name: str | None = None,
        ledger_path: str | Path | None = None,
        state_provider: Any | None = None,
        max_buffered_records: int | None = None,
        as_sdtm: bool = False,
        sdtm_domain: str | None = None,
        standards_profile: str | Any | None = None,
        min_standards_score: float | None = None,
        naming_convention: str | None = None,
        preserve_record_id: bool | None = None,
        account: str = "",
        user: str = "",
        password: str = "",
        database: str = "",
        schema: str = "PUBLIC",
        warehouse: str = "",
        stage: str = "",
        table: str = "",
        stage_prefix: str = "imednet",
        prefix: str | None = None,
        local_staging_dir: str | os.PathLike[str] | None = None,
        manifest_path: str | os.PathLike[str] | None = None,
        hard_delete: bool = False,
    ) -> SnowflakeSinkConfigProtocol:
        """Instantiate a Snowflake sink configuration."""
        ...


class SnowflakeExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.SnowflakeExportSink``."""

    def __call__(self, config: Any) -> ExportSink:
        """Instantiate a Snowflake export sink."""
        ...


class AsyncSnowflakeExportSinkFactoryProtocol(Protocol):
    """Callable factory contract for ``sdk.sinks.AsyncSnowflakeExportSink``."""

    def __call__(self, config: Any) -> AsyncExportSink:
        """Instantiate an asynchronous Snowflake export sink."""
        ...


class BoundExportToSnowflakeProtocol(Protocol):
    """Bound callable contract for ``sdk.sinks.export_to_snowflake``."""

    @overload
    def __call__(
        self,
        study_key: str,
        *,
        config: Any,
        mask_phi: bool | None = None,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_snowflake() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def __call__(
        self,
        sdk: ImednetFacade,
        study_key: str,
        *,
        config: Any,
        mask_phi: bool | None = None,
    ) -> int: ...

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to Snowflake using the bound SDK client."""
        ...


class BoundAsyncExportToSnowflakeProtocol(Protocol):
    """Bound coroutine contract for ``async_sdk.sinks.export_to_snowflake``."""

    @overload
    async def __call__(
        self,
        study_key: str,
        *,
        config: Any,
        mask_phi: bool | None = None,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_snowflake() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    async def __call__(
        self,
        sdk: AsyncImednetFacade,
        study_key: str,
        *,
        config: Any,
        mask_phi: bool | None = None,
    ) -> int: ...

    async def __call__(self, *args: Any, **kwargs: Any) -> int:
        """Asynchronously export study records to Snowflake using the bound ``AsyncImednetSDK``."""
        ...


@runtime_checkable
class SinksNamespaceProtocol(Protocol):
    """Synchronous sinks namespace contract exposed on ``ImednetSDK.sinks``."""

    @property
    def export_to_mongodb(self) -> BoundExportToMongoDbProtocol:
        """Bound helper to export study records to MongoDB."""
        ...

    @property
    def MongoDbSinkConfig(self) -> MongoDbSinkConfigFactoryProtocol:
        """Configuration class for MongoDB export sinks."""
        ...

    @property
    def MongoDbExportSink(self) -> MongoDbExportSinkFactoryProtocol:
        """Sink class for MongoDB record exports."""
        ...

    @property
    def export_to_neo4j(self) -> BoundExportToNeo4jProtocol:
        """Bound helper to export study records to Neo4j."""
        ...

    @property
    def Neo4jSinkConfig(self) -> Neo4jSinkConfigFactoryProtocol:
        """Configuration class for Neo4j export sinks."""
        ...

    @property
    def Neo4jExportSink(self) -> Neo4jExportSinkFactoryProtocol:
        """Sink class for Neo4j record exports."""
        ...

    @property
    def export_to_snowflake(self) -> BoundExportToSnowflakeProtocol:
        """Bound helper to export study records to Snowflake."""
        ...

    @property
    def SnowflakeSinkConfig(self) -> SnowflakeSinkConfigFactoryProtocol:
        """Configuration class for Snowflake export sinks."""
        ...

    @property
    def SnowflakeExportSink(self) -> SnowflakeExportSinkFactoryProtocol:
        """Sink class for Snowflake record exports."""
        ...


SyncSinksNamespaceProtocol = SinksNamespaceProtocol


@runtime_checkable
class AsyncSinksNamespaceProtocol(Protocol):
    """Asynchronous sinks namespace contract exposed on ``AsyncImednetSDK.sinks``."""

    @property
    def export_to_mongodb(self) -> BoundAsyncExportToMongoDbProtocol:
        """Bound coroutine helper to export study records to MongoDB."""
        ...

    @property
    def MongoDbSinkConfig(self) -> MongoDbSinkConfigFactoryProtocol:
        """Configuration class for MongoDB export sinks."""
        ...

    @property
    def MongoDbExportSink(self) -> MongoDbExportSinkFactoryProtocol:
        """Synchronous sink class for MongoDB record exports."""
        ...

    @property
    def AsyncMongoDbExportSink(self) -> AsyncMongoDbExportSinkFactoryProtocol:
        """Asynchronous sink class for MongoDB record exports."""
        ...

    @property
    def export_to_neo4j(self) -> BoundAsyncExportToNeo4jProtocol:
        """Bound coroutine helper to export study records to Neo4j."""
        ...

    @property
    def Neo4jSinkConfig(self) -> Neo4jSinkConfigFactoryProtocol:
        """Configuration class for Neo4j export sinks."""
        ...

    @property
    def Neo4jExportSink(self) -> Neo4jExportSinkFactoryProtocol:
        """Synchronous sink class for Neo4j record exports."""
        ...

    @property
    def AsyncNeo4jExportSink(self) -> AsyncNeo4jExportSinkFactoryProtocol:
        """Asynchronous sink class for Neo4j record exports."""
        ...

    @property
    def export_to_snowflake(self) -> BoundAsyncExportToSnowflakeProtocol:
        """Bound coroutine helper to export study records to Snowflake."""
        ...

    @property
    def SnowflakeSinkConfig(self) -> SnowflakeSinkConfigFactoryProtocol:
        """Configuration class for Snowflake export sinks."""
        ...

    @property
    def SnowflakeExportSink(self) -> SnowflakeExportSinkFactoryProtocol:
        """Synchronous sink class for Snowflake record exports."""
        ...

    @property
    def AsyncSnowflakeExportSink(self) -> AsyncSnowflakeExportSinkFactoryProtocol:
        """Asynchronous sink class for Snowflake record exports."""
        ...


@runtime_checkable
class PluginProtocol(Protocol):
    """Protocol that every iMednet plugin factory must satisfy.

    A conforming factory is a callable that accepts an SDK instance and returns
    an object implementing :class:`WorkflowsNamespaceProtocol`.

    Example::

        from imednet.plugins import PluginProtocol
        from imednet.sdk import ImednetSDK

        class MyWorkflows:
            def __init__(self, sdk: ImednetSDK) -> None:
                self.data_extraction = ...
                self.duckdb_centralizer = ...
                self.query_management = ...
                self.record_mapper = ...
                self.record_update = ...
                self.register_subjects = ...
                self.subject_data = ...
                self.uat = ...
                self.uat_inspector = ...

        def create_workflows(sdk: ImednetSDK) -> MyWorkflows:
            return MyWorkflows(sdk)

        # Verify conformance at import time (optional, for development use):
        assert isinstance(create_workflows, PluginProtocol)
    """

    def __call__(self, sdk_instance: ImednetFacade, /) -> WorkflowsNamespaceProtocol:
        """Initialize the workflows plugin with an SDK instance."""
        ...


@runtime_checkable
class SinksPluginProtocol(Protocol):
    """Protocol for the sinks plugin factory."""

    @overload
    def __call__(self, sdk_instance: AsyncImednetFacade, /) -> AsyncSinksNamespaceProtocol: ...

    @overload
    def __call__(self, sdk_instance: ImednetFacade, /) -> SinksNamespaceProtocol: ...

    @overload
    def __call__(
        self, sdk_instance: ImednetFacade | AsyncImednetFacade, /
    ) -> SinksNamespaceProtocol | AsyncSinksNamespaceProtocol: ...

    def __call__(
        self, sdk_instance: ImednetFacade | AsyncImednetFacade, /
    ) -> SinksNamespaceProtocol | AsyncSinksNamespaceProtocol:
        """Initialize the sinks plugin with an SDK instance."""
        ...


__all__ = [
    "AsyncDataExtractionWorkflowProtocol",
    "AsyncDuckDBIngestionWorkflowProtocol",
    "AsyncMongoDbExportSinkFactoryProtocol",
    "AsyncNeo4jExportSinkFactoryProtocol",
    "AsyncQueryManagementWorkflowProtocol",
    "AsyncRecordMapperProtocol",
    "AsyncRecordUpdateWorkflowProtocol",
    "AsyncRegisterSubjectsWorkflowProtocol",
    "AsyncSinksNamespaceProtocol",
    "AsyncSnowflakeExportSinkFactoryProtocol",
    "AsyncStudySchemaInspectorProtocol",
    "AsyncSubjectDataWorkflowProtocol",
    "AsyncUATWorkflowProtocol",
    "AsyncWorkflowsNamespaceProtocol",
    "BoundAsyncExportToMongoDbProtocol",
    "BoundAsyncExportToNeo4jProtocol",
    "BoundAsyncExportToSnowflakeProtocol",
    "BoundExportToMongoDbProtocol",
    "BoundExportToNeo4jProtocol",
    "BoundExportToSnowflakeProtocol",
    "DataExtractionWorkflowProtocol",
    "DuckDBIngestionWorkflowProtocol",
    "MongoDbExportSinkFactoryProtocol",
    "MongoDbSinkConfigFactoryProtocol",
    "MongoDbSinkConfigProtocol",
    "Neo4jExportSinkFactoryProtocol",
    "Neo4jSinkConfigFactoryProtocol",
    "Neo4jSinkConfigProtocol",
    "PluginProtocol",
    "QueryManagementWorkflowProtocol",
    "RecordMapperProtocol",
    "RegisterSubjectsWorkflowProtocol",
    "SinksNamespaceProtocol",
    "SinksPluginProtocol",
    "SnowflakeExportSinkFactoryProtocol",
    "SnowflakeSinkConfigFactoryProtocol",
    "SnowflakeSinkConfigProtocol",
    "StudySnapshotProtocol",
    "SubjectComprehensiveDataProtocol",
    "SubjectDataWorkflowProtocol",
    "SyncRecordUpdateWorkflowProtocol",
    "SyncRegisterSubjectsWorkflowProtocol",
    "SyncSinksNamespaceProtocol",
    "SyncStudySchemaInspectorProtocol",
    "SyncWorkflowsNamespaceProtocol",
    "TriageStoreProtocol",
    "UATRunResultProtocol",
    "UATWorkflowProtocol",
    "WorkflowsNamespaceProtocol",
]
