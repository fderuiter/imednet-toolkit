"""Supported core capabilities for external export sink implementations."""

from collections.abc import Callable
from typing import Any

from imednet.integrations.enrichment import (
    CentralizedMapper,
    EnrichmentPipeline,
    resolve_enrichment_pipeline,
    resolve_study_configuration,
)
from imednet.integrations.sink_base import (
    DEFAULT_MAX_BUFFERED_RECORDS,
    STATE_PROVIDER_ENTRY_POINT_GROUP,
    BoundedFormBufferRouter,
    ExportSink,
    SinkConfig,
    StateProviderProtocol,
    _redact_uri,
    _require_optional_dep,
    apply_quality_gate,
    bind_sdtm_stream_metadata,
    evaluate_standards_readiness,
    extract_record_form_ref,
    extract_record_id,
    extract_record_timestamp,
    get_state_provider,
    is_incremental_sink_config,
    is_record_deleted,
    iter_batches,
    iter_filtered_sdk_records,
    normalize_sink_value,
    redact_batch_records,
    resolve_max_buffered_records,
    run_streaming_export,
    validate_max_buffered_records,
)
from imednet.models.sdtm import (
    RECORD_ENVELOPE_KEYS,
    SDTM_DOMAIN_SPECS,
    format_variable_name,
    get_sdtm_domain_variables,
    normalize_naming_convention,
    resolve_sdtm_domain,
    transform_records_to_sdtm,
    transform_records_to_sdtm_rows,
)

__all__ = [
    "DEFAULT_MAX_BUFFERED_RECORDS",
    "RECORD_ENVELOPE_KEYS",
    "SDTM_DOMAIN_SPECS",
    "STATE_PROVIDER_ENTRY_POINT_GROUP",
    "BoundedFormBufferRouter",
    "CentralizedMapper",
    "EnrichmentPipeline",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "apply_quality_gate",
    "bind_sdtm_stream_metadata",
    "build_sink_mapper",
    "evaluate_standards_readiness",
    "extract_record_form_ref",
    "extract_record_id",
    "extract_record_timestamp",
    "format_variable_name",
    "get_sdtm_domain_variables",
    "get_state_provider",
    "is_incremental_sink_config",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "normalize_naming_convention",
    "normalize_sink_value",
    "redact_batch_records",
    "redact_uri",
    "require_optional_dep",
    "resolve_enrichment_pipeline",
    "resolve_max_buffered_records",
    "resolve_sdtm_domain",
    "resolve_study_configuration",
    "run_streaming_export",
    "transform_records_to_sdtm",
    "transform_records_to_sdtm_rows",
    "validate_max_buffered_records",
]


def build_sink_mapper(
    config: SinkConfig,
    *,
    mode: str = "document",
    post_processor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    seq_counters: dict[str, int] | None = None,
    record_seqs: dict[str, int] | None = None,
    existing_record_seqs: dict[str, int] | None = None,
    preserve_record_id: bool | None = None,
    include_record_id: bool | None = None,
) -> CentralizedMapper:
    """Create a CentralizedMapper configured with SinkConfig PHI, SDTM, and naming rules."""
    extra = getattr(config, "extra", None)
    extra_dict = extra if isinstance(extra, dict) else {}
    as_sdtm = bool(getattr(config, "as_sdtm", False))
    sdtm_domain = getattr(config, "sdtm_domain", None)
    if include_record_id is None and preserve_record_id is None:
        effective_include_record_id = bool(as_sdtm or sdtm_domain is not None)
    else:
        effective_include_record_id = bool(include_record_id) or bool(preserve_record_id)
    mapper = CentralizedMapper(
        mode=mode,
        post_processor=post_processor,
        mask_phi=getattr(config, "mask_phi", True),
        study_config=extra_dict.get("study_config"),
        naming_convention=getattr(config, "naming_convention", None),
        as_sdtm=as_sdtm,
        sdtm_domain=sdtm_domain,
        seq_counters=seq_counters,
        record_seqs=record_seqs,
        existing_record_seqs=existing_record_seqs,
        preserve_record_id=effective_include_record_id,
        include_record_id=effective_include_record_id,
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
