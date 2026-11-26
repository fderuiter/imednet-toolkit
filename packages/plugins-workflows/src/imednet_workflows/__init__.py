# pylint: disable=duplicate-code
"""Workflow helpers built on top of the iMednet SDK."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imednet.spi.utils import (
        AsyncBatchCoordinator,
        AsyncJobPoller,
        BatchCoordinator,
        BatchSubmissionSummary,
        JobPoller,
        JobPollSummary,
        JobProgressCallback,
        JobProgressHandler,
        JobStatusEvent,
        JobTimeoutError,
    )

    from .cached_loader import CachedRecordsLoader, StudySyncState, StudySyncStatus
    from .chunked_pipeline import ChunkedRecordPipeline, iter_chunks
    from .config_version_control import ConfigVersionStore
    from .data_extraction import AsyncDataExtractionWorkflow, DataExtractionWorkflow
    from .duckdb_centralizer import AsyncDuckDBIngestionWorkflow, DuckDBIngestionWorkflow
    from .extraction_engine import ExtractionResult, extract_canonical_records
    from .namespace import AsyncWorkflows, Workflows
    from .query_management import AsyncQueryManagementWorkflow, QueryManagementWorkflow
    from .record_mapper import (
        AsyncRecordMapper,
        RecordMapper,
        format_iso8601_dtc,
        transform_records_to_sdtm,
    )
    from .record_update import AsyncRecordUpdateWorkflow, RecordUpdateWorkflow
    from .register_subjects import AsyncRegisterSubjectsWorkflow, RegisterSubjectsWorkflow
    from .schema_profiler import FieldProfile, FormProfile, SchemaProfiler
    from .standards_validation import (
        CategoricalNormalizer,
        NormalizationResult,
        StandardsReadinessReport,
        StandardsReadinessValidator,
    )
    from .state_ledger import (
        DEFAULT_LEDGER_PATH,
        ExtractionStateLedger,
        InvalidStateTransitionError,
        LedgerState,
        StreamExecutionStatus,
        StreamState,
    )
    from .study_structure import async_get_study_structure, get_study_structure
    from .subject_data import AsyncSubjectDataWorkflow, SubjectDataWorkflow
    from .sync_worker import SyncErrorCallback, SyncWorker, SyncWorkerConfig, SyncWorkerState
    from .triage_store import TriageStore
    from .uat import (
        AsyncStudySchemaInspector,
        AsyncUATWorkflow,
        StudySchemaInspector,
        UATSpecification,
        UATWorkflow,
    )

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "AsyncBatchCoordinator": ("imednet.spi.utils", "AsyncBatchCoordinator"),
    "AsyncDataExtractionWorkflow": (
        "imednet_workflows.data_extraction",
        "AsyncDataExtractionWorkflow",
    ),
    "AsyncDuckDBIngestionWorkflow": (
        "imednet_workflows.duckdb_centralizer",
        "AsyncDuckDBIngestionWorkflow",
    ),
    "AsyncQueryManagementWorkflow": (
        "imednet_workflows.query_management",
        "AsyncQueryManagementWorkflow",
    ),
    "AsyncRecordMapper": ("imednet_workflows.record_mapper", "AsyncRecordMapper"),
    "AsyncRecordUpdateWorkflow": ("imednet_workflows.record_update", "AsyncRecordUpdateWorkflow"),
    "AsyncRegisterSubjectsWorkflow": (
        "imednet_workflows.register_subjects",
        "AsyncRegisterSubjectsWorkflow",
    ),
    "AsyncStudySchemaInspector": ("imednet_workflows.uat", "AsyncStudySchemaInspector"),
    "AsyncSubjectDataWorkflow": ("imednet_workflows.subject_data", "AsyncSubjectDataWorkflow"),
    "AsyncUATWorkflow": ("imednet_workflows.uat", "AsyncUATWorkflow"),
    "AsyncWorkflows": ("imednet_workflows.namespace", "AsyncWorkflows"),
    "AsyncJobPoller": ("imednet.spi.utils", "AsyncJobPoller"),
    "BatchCoordinator": ("imednet.spi.utils", "BatchCoordinator"),
    "BatchSubmissionSummary": ("imednet.spi.utils", "BatchSubmissionSummary"),
    "CachedRecordsLoader": ("imednet_workflows.cached_loader", "CachedRecordsLoader"),
    "CategoricalNormalizer": ("imednet_workflows.standards_validation", "CategoricalNormalizer"),
    "ChunkedRecordPipeline": ("imednet_workflows.chunked_pipeline", "ChunkedRecordPipeline"),
    "ConfigVersionStore": ("imednet_workflows.config_version_control", "ConfigVersionStore"),
    "DEFAULT_LEDGER_PATH": ("imednet_workflows.state_ledger", "DEFAULT_LEDGER_PATH"),
    "DataExtractionWorkflow": ("imednet_workflows.data_extraction", "DataExtractionWorkflow"),
    "DuckDBIngestionWorkflow": (
        "imednet_workflows.duckdb_centralizer",
        "DuckDBIngestionWorkflow",
    ),
    "ExtractionResult": ("imednet_workflows.extraction_engine", "ExtractionResult"),
    "ExtractionStateLedger": ("imednet_workflows.state_ledger", "ExtractionStateLedger"),
    "FieldProfile": ("imednet_workflows.schema_profiler", "FieldProfile"),
    "FormProfile": ("imednet_workflows.schema_profiler", "FormProfile"),
    "InvalidStateTransitionError": (
        "imednet_workflows.state_ledger",
        "InvalidStateTransitionError",
    ),
    "JobPollSummary": ("imednet.spi.utils", "JobPollSummary"),
    "JobPoller": ("imednet.spi.utils", "JobPoller"),
    "JobProgressCallback": ("imednet.spi.utils", "JobProgressCallback"),
    "JobProgressHandler": ("imednet.spi.utils", "JobProgressHandler"),
    "JobStatusEvent": ("imednet.spi.utils", "JobStatusEvent"),
    "JobTimeoutError": ("imednet.spi.utils", "JobTimeoutError"),
    "LedgerState": ("imednet_workflows.state_ledger", "LedgerState"),
    "NormalizationResult": ("imednet_workflows.standards_validation", "NormalizationResult"),
    "QueryManagementWorkflow": ("imednet_workflows.query_management", "QueryManagementWorkflow"),
    "RecordMapper": ("imednet_workflows.record_mapper", "RecordMapper"),
    "RecordUpdateWorkflow": ("imednet_workflows.record_update", "RecordUpdateWorkflow"),
    "RegisterSubjectsWorkflow": ("imednet_workflows.register_subjects", "RegisterSubjectsWorkflow"),
    "SchemaProfiler": ("imednet_workflows.schema_profiler", "SchemaProfiler"),
    "StandardsReadinessReport": (
        "imednet_workflows.standards_validation",
        "StandardsReadinessReport",
    ),
    "StandardsReadinessValidator": (
        "imednet_workflows.standards_validation",
        "StandardsReadinessValidator",
    ),
    "StreamExecutionStatus": ("imednet_workflows.state_ledger", "StreamExecutionStatus"),
    "StreamState": ("imednet_workflows.state_ledger", "StreamState"),
    "StudySchemaInspector": ("imednet_workflows.uat", "StudySchemaInspector"),
    "StudySyncState": ("imednet_workflows.cached_loader", "StudySyncState"),
    "StudySyncStatus": ("imednet_workflows.cached_loader", "StudySyncStatus"),
    "SubjectDataWorkflow": ("imednet_workflows.subject_data", "SubjectDataWorkflow"),
    "SyncErrorCallback": ("imednet_workflows.sync_worker", "SyncErrorCallback"),
    "SyncWorker": ("imednet_workflows.sync_worker", "SyncWorker"),
    "SyncWorkerConfig": ("imednet_workflows.sync_worker", "SyncWorkerConfig"),
    "SyncWorkerState": ("imednet_workflows.sync_worker", "SyncWorkerState"),
    "TriageStore": ("imednet_workflows.triage_store", "TriageStore"),
    "UATSpecification": ("imednet_workflows.uat", "UATSpecification"),
    "UATWorkflow": ("imednet_workflows.uat", "UATWorkflow"),
    "Workflows": ("imednet_workflows.namespace", "Workflows"),
    "async_get_study_structure": (
        "imednet_workflows.study_structure",
        "async_get_study_structure",
    ),
    "extract_canonical_records": (
        "imednet_workflows.extraction_engine",
        "extract_canonical_records",
    ),
    "format_iso8601_dtc": ("imednet_workflows.record_mapper", "format_iso8601_dtc"),
    "get_study_structure": ("imednet_workflows.study_structure", "get_study_structure"),
    "iter_chunks": ("imednet_workflows.chunked_pipeline", "iter_chunks"),
    "transform_records_to_sdtm": (
        "imednet_workflows.record_mapper",
        "transform_records_to_sdtm",
    ),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily load workflow classes and utilities on demand."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy exports."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "DEFAULT_LEDGER_PATH",
    "AsyncBatchCoordinator",
    "AsyncDataExtractionWorkflow",
    "AsyncDuckDBIngestionWorkflow",
    "AsyncJobPoller",
    "AsyncQueryManagementWorkflow",
    "AsyncRecordMapper",
    "AsyncRecordUpdateWorkflow",
    "AsyncRegisterSubjectsWorkflow",
    "AsyncStudySchemaInspector",
    "AsyncSubjectDataWorkflow",
    "AsyncUATWorkflow",
    "AsyncWorkflows",
    "BatchCoordinator",
    "BatchSubmissionSummary",
    "CachedRecordsLoader",
    "CategoricalNormalizer",
    "ChunkedRecordPipeline",
    "ConfigVersionStore",
    "DataExtractionWorkflow",
    "DuckDBIngestionWorkflow",
    "ExtractionResult",
    "ExtractionStateLedger",
    "FieldProfile",
    "FormProfile",
    "InvalidStateTransitionError",
    "JobPollSummary",
    "JobPoller",
    "JobProgressCallback",
    "JobProgressHandler",
    "JobStatusEvent",
    "JobTimeoutError",
    "LedgerState",
    "NormalizationResult",
    "QueryManagementWorkflow",
    "RecordMapper",
    "RecordUpdateWorkflow",
    "RegisterSubjectsWorkflow",
    "SchemaProfiler",
    "StandardsReadinessReport",
    "StandardsReadinessValidator",
    "StreamExecutionStatus",
    "StreamState",
    "StudySchemaInspector",
    "StudySyncState",
    "StudySyncStatus",
    "SubjectDataWorkflow",
    "SyncErrorCallback",
    "SyncWorker",
    "SyncWorkerConfig",
    "SyncWorkerState",
    "TriageStore",
    "UATSpecification",
    "UATWorkflow",
    "Workflows",
    "async_get_study_structure",
    "extract_canonical_records",
    "format_iso8601_dtc",
    "get_study_structure",
    "iter_chunks",
    "transform_records_to_sdtm",
]
