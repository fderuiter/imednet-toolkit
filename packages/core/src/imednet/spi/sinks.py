"""Supported core capabilities for external export sink implementations."""

from collections.abc import Callable, Mapping
from typing import Any

from imednet.integrations.enrichment import (
    CentralizedMapper,
    EnrichmentPipeline,
    resolve_enrichment_pipeline,
    resolve_sink_enrichment_pipeline,
    resolve_sink_phi_keys,
    resolve_study_configuration,
)
from imednet.integrations.sink_base import (
    _SDTM_RECORDS_SORT,
    DEFAULT_MAX_BUFFERED_RECORDS,
    STATE_PROVIDER_ENTRY_POINT_GROUP,
    AsyncBoundedFormBufferRouter,
    AsyncExportSink,
    BoundedFormBufferRouter,
    ExportSink,
    SinkConfig,
    StateProviderProtocol,
    _redact_uri,
    _require_optional_dep,
    apply_quality_gate,
    async_apply_quality_gate,
    async_iter_batches,
    async_iter_filtered_sdk_records,
    async_iter_subject_ordered_sdtm_records,
    async_run_streaming_export,
    bind_sdtm_stream_metadata,
    build_form_sdtm_domain_map,
    commit_sdtm_lineage,
    deduplicate_sdtm_rows,
    evaluate_standards_readiness,
    extract_record_form_ref,
    extract_record_id,
    extract_record_timestamp,
    extract_sdtm_composite_key,
    filter_forms_by_sdtm_domain,
    get_state_provider,
    is_incremental_sink_config,
    is_record_deleted,
    iter_batches,
    iter_filtered_sdk_records,
    iter_subject_ordered_sdtm_records,
    merge_form_ids_into_record_filter,
    normalize_sink_value,
    record_matches_sdtm_domain,
    redact_batch_records,
    rehydrate_sdtm_lineage,
    resolve_effective_as_sdtm,
    resolve_form_sdtm_domain,
    resolve_max_buffered_records,
    resolve_voided_sdtm_keys,
    run_streaming_export,
    should_preserve_sdtm_record_id,
    validate_max_buffered_records,
)
from imednet.models.sdtm import (
    SDTM_DOMAIN_SPECS,
    format_nested_variable_name,
    format_variable_name,
    get_sdtm_domain_variables,
    normalize_naming_convention,
    prefix_colliding_variable_name,
    resolve_sdtm_domain,
    transform_records_to_sdtm,
    transform_records_to_sdtm_rows,
)

__all__ = [
    "DEFAULT_MAX_BUFFERED_RECORDS",
    "SDTM_DOMAIN_SPECS",
    "STATE_PROVIDER_ENTRY_POINT_GROUP",
    "_SDTM_RECORDS_SORT",
    "AsyncBoundedFormBufferRouter",
    "AsyncExportSink",
    "BoundedFormBufferRouter",
    "CentralizedMapper",
    "EnrichmentPipeline",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "apply_quality_gate",
    "async_apply_quality_gate",
    "async_iter_batches",
    "async_iter_filtered_sdk_records",
    "async_iter_subject_ordered_sdtm_records",
    "async_run_streaming_export",
    "bind_sdtm_stream_metadata",
    "build_form_sdtm_domain_map",
    "build_sink_mapper",
    "commit_sdtm_lineage",
    "deduplicate_sdtm_rows",
    "evaluate_standards_readiness",
    "extract_record_form_ref",
    "extract_record_id",
    "extract_record_timestamp",
    "extract_sdtm_composite_key",
    "filter_forms_by_sdtm_domain",
    "format_nested_variable_name",
    "format_variable_name",
    "get_sdtm_domain_variables",
    "get_state_provider",
    "is_incremental_sink_config",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "iter_subject_ordered_sdtm_records",
    "merge_form_ids_into_record_filter",
    "normalize_naming_convention",
    "normalize_sink_value",
    "prefix_colliding_variable_name",
    "record_matches_sdtm_domain",
    "redact_batch_records",
    "redact_uri",
    "rehydrate_sdtm_lineage",
    "require_optional_dep",
    "resolve_effective_as_sdtm",
    "resolve_enrichment_pipeline",
    "resolve_form_sdtm_domain",
    "resolve_max_buffered_records",
    "resolve_sdtm_domain",
    "resolve_sink_enrichment_pipeline",
    "resolve_sink_phi_keys",
    "resolve_study_configuration",
    "resolve_voided_sdtm_keys",
    "run_streaming_export",
    "should_preserve_sdtm_record_id",
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
    domain_seq_counters: dict[str, dict[str, int]] | None = None,
    form_domain_map: Mapping[Any, str] | None = None,
    seq_overrides: Mapping[str, int] | None = None,
    lineage_out: dict[str, dict[str, Any]] | None = None,
    record_seqs: dict[str, int] | None = None,
    preserve_record_id: bool | None = None,
) -> CentralizedMapper:
    """Create a CentralizedMapper configured with SinkConfig PHI, SDTM, and naming rules."""
    extra = getattr(config, "extra", None)
    extra_dict = extra if isinstance(extra, dict) else {}
    effective_preserve_record_id = should_preserve_sdtm_record_id(config, preserve_record_id)
    mapper = CentralizedMapper(
        mode=mode,
        post_processor=post_processor,
        mask_phi=getattr(config, "mask_phi", True),
        study_config=extra_dict.get("study_config"),
        naming_convention=getattr(config, "naming_convention", None),
        as_sdtm=resolve_effective_as_sdtm(config),
        sdtm_domain=getattr(config, "sdtm_domain", None),
        seq_counters=seq_counters,
        domain_seq_counters=domain_seq_counters,
        form_domain_map=form_domain_map,
        seq_overrides=seq_overrides,
        lineage_out=lineage_out,
        record_seqs=record_seqs,
        preserve_record_id=effective_preserve_record_id,
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
