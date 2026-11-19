# pylint: disable=duplicate-code
"""Public Service Provider Interface for export sinks.

Provides base classes and utilities for external export sink implementations,
decoupling them from internal integration components.
"""

from imednet.integrations.sink_base import (
    STATE_PROVIDER_ENTRY_POINT_GROUP,
    ExportSink,
    SinkConfig,
    StateProviderProtocol,
    _redact_uri,
    _require_optional_dep,
    apply_quality_gate,
    build_record_query_branches,
    extract_record_id,
    extract_record_timestamp,
    get_state_provider,
    is_record_deleted,
    iter_batches,
    iter_filtered_sdk_records,
    normalize_watermark_datetime,
    redact_batch_records,
    run_streaming_export,
)

__all__ = [
    "STATE_PROVIDER_ENTRY_POINT_GROUP",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "_redact_uri",
    "_require_optional_dep",
    "apply_quality_gate",
    "build_record_query_branches",
    "extract_record_id",
    "extract_record_timestamp",
    "get_state_provider",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "normalize_watermark_datetime",
    "redact_batch_records",
    "run_streaming_export",
]
