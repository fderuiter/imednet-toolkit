"""Supported core capabilities for external export sink implementations."""

from collections.abc import Callable
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
    redact_batch_records,
    run_streaming_export,
)

__all__ = [
    "STATE_PROVIDER_ENTRY_POINT_GROUP",
    "CentralizedMapper",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "apply_quality_gate",
    "build_sink_mapper",
    "extract_record_id",
    "extract_record_timestamp",
    "get_state_provider",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "redact_batch_records",
    "redact_uri",
    "require_optional_dep",
    "run_streaming_export",
]


def build_sink_mapper(
    config: SinkConfig,
    *,
    mode: str = "document",
    post_processor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> CentralizedMapper:
    """Create a CentralizedMapper configured with SinkConfig PHI and enrichment rules."""
    extra = getattr(config, "extra", None)
    extra_dict = extra if isinstance(extra, dict) else {}
    mapper = CentralizedMapper(
        mode=mode,
        post_processor=post_processor,
        mask_phi=getattr(config, "mask_phi", True),
        study_config=extra_dict.get("study_config"),
    )
    custom_pipeline = extra_dict.get("enrichment_pipeline")
    if custom_pipeline is not None and hasattr(custom_pipeline, "process"):
        mapper.register_pipeline(config.study_key, custom_pipeline)
    return mapper


def redact_uri(uri: str) -> str:
    """Return a destination URI with credentials masked."""
    return _redact_uri(uri)


def require_optional_dep(module_name: str, extra: str) -> Any:
    """Load a destination driver or raise the existing actionable install error."""
    return _require_optional_dep(module_name, extra)
