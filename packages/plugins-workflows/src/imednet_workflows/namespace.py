"""Lazy synchronous and asynchronous namespaces for iMednet workflows."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any, cast, overload

from imednet.spi.sink_base import is_async_sdk

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

    from .data_extraction import DataExtractionWorkflow
    from .duckdb_centralizer import DuckDBIngestionWorkflow
    from .query_management import QueryManagementWorkflow
    from .record_mapper import RecordMapper
    from .record_update import RecordUpdateWorkflow
    from .register_subjects import RegisterSubjectsWorkflow
    from .subject_data import SubjectDataWorkflow
    from .uat import StudySchemaInspector, UATWorkflow


_SYNC_ONLY_WORKFLOWS: frozenset[str] = frozenset(
    {
        "data_extraction",
        "duckdb_centralizer",
        "query_management",
        "record_mapper",
        "register_subjects",
        "subject_data",
        "uat",
    }
)


class Workflows:
    """Lazy namespace for synchronous workflow classes bound to ``ImednetSDK``."""

    def __init__(self, sdk_instance: ImednetFacade) -> None:
        """Initialize the synchronous workflows namespace without eager instantiation."""
        if is_async_sdk(sdk_instance):
            raise TypeError(
                "Workflows requires a synchronous ImednetSDK instance; received an async SDK "
                "client. Use create_workflows(async_sdk) or AsyncWorkflows(async_sdk) instead."
            )
        self._sdk = sdk_instance

    @cached_property
    def data_extraction(self) -> DataExtractionWorkflow:
        """Complex cross-endpoint clinical data extraction workflow."""
        from .data_extraction import DataExtractionWorkflow

        return DataExtractionWorkflow(self._sdk)

    @cached_property
    def duckdb_centralizer(self) -> DuckDBIngestionWorkflow:
        """Incremental record centralization pipeline using DuckDB."""
        from .duckdb_centralizer import DuckDBIngestionWorkflow

        return DuckDBIngestionWorkflow(self._sdk)

    @cached_property
    def query_management(self) -> QueryManagementWorkflow:
        """Study query inspection and state counting workflow."""
        from .query_management import QueryManagementWorkflow

        return QueryManagementWorkflow(self._sdk)

    @cached_property
    def record_mapper(self) -> RecordMapper:
        """Tabular DataFrame and SDTM record mapping workflow."""
        from .record_mapper import RecordMapper

        return RecordMapper(self._sdk)

    @cached_property
    def record_update(self) -> RecordUpdateWorkflow:
        """Record creation, update, and subject registration submission workflow."""
        from .record_update import RecordUpdateWorkflow

        return RecordUpdateWorkflow(self._sdk)

    @cached_property
    def register_subjects(self) -> RegisterSubjectsWorkflow:
        """Batch subject enrollment workflow."""
        from .register_subjects import RegisterSubjectsWorkflow

        return RegisterSubjectsWorkflow(self._sdk)

    @cached_property
    def subject_data(self) -> SubjectDataWorkflow:
        """Comprehensive per-subject clinical data aggregation workflow."""
        from .subject_data import SubjectDataWorkflow

        return SubjectDataWorkflow(self._sdk)

    @cached_property
    def uat(self) -> UATWorkflow:
        """End-to-end UAT execution orchestrator workflow."""
        from .uat import UATWorkflow

        return UATWorkflow(self._sdk)

    @cached_property
    def uat_inspector(self) -> StudySchemaInspector:
        """Study metadata snapshot inspector for UAT workflows."""
        from .uat import StudySchemaInspector

        return StudySchemaInspector(self._sdk)


class AsyncWorkflows:
    """Lazy namespace for async-capable workflows bound to ``AsyncImednetSDK``."""

    def __init__(self, sdk_instance: AsyncImednetFacade) -> None:
        """Initialize the asynchronous workflows namespace."""
        if not is_async_sdk(sdk_instance):
            raise TypeError(
                "AsyncWorkflows requires an AsyncImednetSDK instance; received a synchronous SDK "
                "client. Use create_workflows(sdk) or Workflows(sdk) instead."
            )
        self._sdk = sdk_instance

    @cached_property
    def record_update(self) -> RecordUpdateWorkflow:
        """Async-capable record creation, update, and subject registration workflow."""
        from .record_update import RecordUpdateWorkflow

        return RecordUpdateWorkflow(self._sdk)

    @cached_property
    def uat_inspector(self) -> StudySchemaInspector:
        """Async-capable study metadata snapshot inspector."""
        from .uat import StudySchemaInspector

        return StudySchemaInspector(self._sdk)

    def __getattr__(self, name: str) -> Any:
        """Reject synchronous-only workflows with actionable ``TypeError`` guidance."""
        if name in _SYNC_ONLY_WORKFLOWS:
            raise TypeError(
                f"Workflow '{name}' is synchronous-only and cannot be used with AsyncImednetSDK. "
                "Instantiate synchronous ImednetSDK instead, or use async-compatible workflows "
                "('record_update' async_* methods or 'uat_inspector.async_inspect')."
            )
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


@overload
def create_workflows(sdk: ImednetFacade) -> Workflows: ...


@overload
def create_workflows(sdk: AsyncImednetFacade) -> AsyncWorkflows: ...


def create_workflows(sdk: ImednetFacade | AsyncImednetFacade) -> Workflows | AsyncWorkflows:
    """Factory registered under ``imednet.plugins`` (name=``workflows``)."""
    if is_async_sdk(sdk):
        return AsyncWorkflows(cast("AsyncImednetFacade", sdk))
    return Workflows(cast("ImednetFacade", sdk))


__all__ = [
    "AsyncWorkflows",
    "Workflows",
    "create_workflows",
]
