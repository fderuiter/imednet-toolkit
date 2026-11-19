"""Supported core capabilities for external export sink implementations."""

from typing import Any

from imednet.integrations.enrichment import CentralizedMapper
from imednet.integrations.sink_base import (
    STATE_PROVIDER_ENTRY_POINT_GROUP,
    ExportSink,
    SinkConfig,
    StateProviderProtocol,
    _redact_uri,
    _require_optional_dep,
    apply_quality_gate,
    extract_record_id,
    extract_record_timestamp,
    get_state_provider,
    is_record_deleted,
    iter_batches,
    iter_filtered_sdk_records,
    run_streaming_export,
)

__all__ = [
    "STATE_PROVIDER_ENTRY_POINT_GROUP",
    "CentralizedMapper",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "apply_quality_gate",
    "extract_record_id",
    "extract_record_timestamp",
    "get_state_provider",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "redact_uri",
    "require_optional_dep",
    "run_streaming_export",
]


def redact_uri(uri: str) -> str:
    """Return a destination URI with credentials masked."""
    return _redact_uri(uri)


def require_optional_dep(module_name: str, extra: str) -> Any:
    """Load a destination driver or raise the existing actionable install error."""
    return _require_optional_dep(module_name, extra)
