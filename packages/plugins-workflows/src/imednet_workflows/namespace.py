"""Namespace for accessing workflow classes."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

from .data_extraction import DataExtractionWorkflow
from .duckdb_centralizer import DuckDBIngestionWorkflow
from .query_management import QueryManagementWorkflow
from .record_mapper import RecordMapper
from .record_update import RecordUpdateWorkflow
from .subject_data import SubjectDataWorkflow
from .uat import StudySchemaInspector, UATWorkflow

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


class Workflows:
    """Namespace for accessing workflow classes."""

    def __init__(self, sdk_instance: ImednetFacade):
        """Initialize the workflows namespace.

        Args:
            sdk_instance: An instance of the iMednet SDK facade.
        """
        self._sdk = sdk_instance
        self.data_extraction = DataExtractionWorkflow(sdk_instance)
        self.query_management = QueryManagementWorkflow(sdk_instance)
        self.record_mapper = RecordMapper(sdk_instance)
        self.record_update = RecordUpdateWorkflow(sdk_instance)
        self.subject_data = SubjectDataWorkflow(sdk_instance)
        self.uat_inspector = StudySchemaInspector(sdk_instance)
        self.uat = UATWorkflow(sdk_instance)

    @cached_property
    def duckdb_centralizer(self) -> DuckDBIngestionWorkflow:
        """Incremental record centralization pipeline using DuckDB."""
        return DuckDBIngestionWorkflow(self._sdk)
