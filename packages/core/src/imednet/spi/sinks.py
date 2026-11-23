"""Supported core capabilities for external export sink implementations."""

from collections.abc import Callable, Mapping
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
    evaluate_standards_readiness,
    extract_record_form_ref,
    extract_record_id,
    extract_record_timestamp,
    extract_sdtm_natural_key,
    get_state_provider,
    hydrate_sdtm_checkpoint_state,
    is_record_deleted,
    iter_batches,
    iter_filtered_sdk_records,
    normalize_sink_value,
    persist_sdtm_checkpoint_state,
    pop_deleted_sdtm_provenance_keys,
    pop_sdtm_record_id_provenance,
    reconcile_sdtm_provenance_rows,
    redact_batch_records,
    resolve_max_buffered_records,
    run_streaming_export,
    sdtm_natural_key_tuple,
    strip_sdtm_provenance,
    validate_max_buffered_records,
)
from imednet.models.sdtm import (
    SDTM_DOMAIN_SPECS,
    SDTM_PROVENANCE_RECORD_ID_KEY,
    form_matches_sdtm_domain,
    format_variable_name,
    get_sdtm_domain_variables,
    infer_record_sdtm_domain,
    infer_sdtm_domain_from_form,
    normalize_naming_convention,
    record_matches_sdtm_domain,
    resolve_sdtm_domain,
    transform_records_to_sdtm,
    transform_records_to_sdtm_rows,
)

__all__ = [
    "DEFAULT_MAX_BUFFERED_RECORDS",
    "SDTM_DOMAIN_SPECS",
    "SDTM_PROVENANCE_RECORD_ID_KEY",
    "STATE_PROVIDER_ENTRY_POINT_GROUP",
    "BoundedFormBufferRouter",
    "CentralizedMapper",
    "EnrichmentPipeline",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "apply_quality_gate",
    "build_sink_mapper",
    "evaluate_standards_readiness",
    "extract_record_form_ref",
    "extract_record_id",
    "extract_record_timestamp",
    "extract_sdtm_natural_key",
    "form_matches_sdtm_domain",
    "format_variable_name",
    "get_sdtm_domain_variables",
    "get_state_provider",
    "hydrate_sdtm_checkpoint_state",
    "infer_record_sdtm_domain",
    "infer_sdtm_domain_from_form",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "normalize_naming_convention",
    "normalize_sink_value",
    "persist_sdtm_checkpoint_state",
    "pop_deleted_sdtm_provenance_keys",
    "pop_sdtm_record_id_provenance",
    "reconcile_sdtm_provenance_rows",
    "record_matches_sdtm_domain",
    "redact_batch_records",
    "redact_uri",
    "require_optional_dep",
    "resolve_enrichment_pipeline",
    "resolve_max_buffered_records",
    "resolve_sdtm_domain",
    "resolve_study_configuration",
    "run_streaming_export",
    "sdtm_natural_key_tuple",
    "strip_sdtm_provenance",
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
    seq_counters_by_domain: dict[str, dict[str, int]] | None = None,
    include_sdtm_provenance: bool = False,
    sdtm_provenance: Mapping[str, Mapping[str, Any]] | None = None,
) -> CentralizedMapper:
    """Create a CentralizedMapper configured with SinkConfig PHI, SDTM, and naming rules."""
    extra = getattr(config, "extra", None)
    extra_dict = extra if isinstance(extra, dict) else {}
    as_sdtm_flag = bool(getattr(config, "as_sdtm", False))
    mapper = CentralizedMapper(
        mode=mode,
        post_processor=post_processor,
        mask_phi=getattr(config, "mask_phi", True),
        study_config=extra_dict.get("study_config"),
        naming_convention=getattr(config, "naming_convention", None),
        as_sdtm=as_sdtm_flag,
        sdtm_domain=getattr(config, "sdtm_domain", None),
        seq_counters=seq_counters,
        seq_counters_by_domain=seq_counters_by_domain,
        include_sdtm_provenance=bool(include_sdtm_provenance and as_sdtm_flag),
        sdtm_provenance=sdtm_provenance,
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
