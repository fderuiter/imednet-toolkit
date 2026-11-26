"""Namespace for accessing workflow classes."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

from .data_extraction import AsyncDataExtractionWorkflow, DataExtractionWorkflow
from .duckdb_centralizer import AsyncDuckDBIngestionWorkflow, DuckDBIngestionWorkflow
from .query_management import AsyncQueryManagementWorkflow, QueryManagementWorkflow
from .record_mapper import AsyncRecordMapper, RecordMapper
from .record_update import AsyncRecordUpdateWorkflow, RecordUpdateWorkflow
from .register_subjects import AsyncRegisterSubjectsWorkflow, RegisterSubjectsWorkflow
from .subject_data import AsyncSubjectDataWorkflow, SubjectDataWorkflow
from .uat import AsyncStudySchemaInspector, AsyncUATWorkflow, StudySchemaInspector, UATWorkflow

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade


class Workflows:
    """Namespace for accessing workflow classes."""

    def __init__(self, sdk_instance: ImednetFacade):
        """Initialize the workflows namespace."""
        self._sdk = sdk_instance
        self.data_extraction = DataExtractionWorkflow(sdk_instance)
        self.query_management = QueryManagementWorkflow(sdk_instance)
        self.record_mapper = RecordMapper(sdk_instance)
        self.record_update = RecordUpdateWorkflow(sdk_instance)
        self.register_subjects = RegisterSubjectsWorkflow(sdk_instance)
        self.subject_data = SubjectDataWorkflow(sdk_instance)
        self.uat_inspector = StudySchemaInspector(sdk_instance)
        self.uat = UATWorkflow(sdk_instance)

    @cached_property
    def duckdb_centralizer(self) -> DuckDBIngestionWorkflow:
        """Incremental record centralization pipeline using DuckDB."""
        return DuckDBIngestionWorkflow(self._sdk)


class AsyncWorkflows:
    """Namespace for accessing asynchronous workflow classes."""

    def __init__(self, sdk_instance: AsyncImednetFacade):
        """Initialize the async workflows namespace."""
        self._sdk = sdk_instance
        self.data_extraction = AsyncDataExtractionWorkflow(sdk_instance)
        self.query_management = AsyncQueryManagementWorkflow(sdk_instance)
        self.record_mapper = AsyncRecordMapper(sdk_instance)
        self.record_update = AsyncRecordUpdateWorkflow(sdk_instance)
        self.register_subjects = AsyncRegisterSubjectsWorkflow(sdk_instance)
        self.subject_data = AsyncSubjectDataWorkflow(sdk_instance)
        self.uat_inspector = AsyncStudySchemaInspector(sdk_instance)
        self.uat = AsyncUATWorkflow(sdk_instance)

    @cached_property
    def duckdb_centralizer(self) -> AsyncDuckDBIngestionWorkflow:
        """Asynchronous incremental record centralization pipeline using DuckDB."""
        return AsyncDuckDBIngestionWorkflow(self._sdk)


__all__ = ["AsyncWorkflows", "Workflows"]
