"""Utils init module."""

from typing import Any

"""Re-exports utility functions for easier access."""

from importlib import import_module

from .dates import (
    format_iso_datetime,
    normalize_iso_date,
    normalize_utc_timestamp_bound,
    parse_iso_datetime,
)
from .filters import (
    ALLOWED_OPERATORS,
    ORDERED_FILTER_OPERATORS,
    build_filter_string,
    parse_filter_expression,
    parse_filter_expressions,
    validate_connector_exclusivity,
    validate_filter_key,
    validate_filter_operator,
    validate_filter_syntax,
    validate_record_filter,
)
from .json_logging import configure_json_logging
from .typing import FilterValue, ItemId, JsonDict, RecordsInput

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "flatten": ("imednet.utils.serialization", "flatten"),
    "to_arrow_table": ("imednet.utils.arrow", "to_arrow_table"),
    "check_pandas": ("imednet.utils.pandas", "check_pandas"),
    "queries_to_dataframe": ("imednet.utils.pandas", "queries_to_dataframe"),
    "subjects_to_dataframe": ("imednet.utils.pandas", "subjects_to_dataframe"),
    "forms_to_dataframe": ("imednet.utils.pandas", "forms_to_dataframe"),
    "visits_to_dataframe": ("imednet.utils.pandas", "visits_to_dataframe"),
    "intervals_to_dataframe": ("imednet.utils.pandas", "intervals_to_dataframe"),
    "sites_to_dataframe": ("imednet.utils.pandas", "sites_to_dataframe"),
    "variables_to_dataframe": ("imednet.utils.pandas", "variables_to_dataframe"),
    "records_to_dataframe": ("imednet.utils.pandas", "records_to_dataframe"),
    "users_to_dataframe": ("imednet.utils.pandas", "users_to_dataframe"),
    "export_records_csv": ("imednet.utils.pandas", "export_records_csv"),
    "dataframe_to_records": ("imednet.utils.pandas", "dataframe_to_records"),
    "csv_to_records": ("imednet.utils.pandas", "csv_to_records"),
    "import_records_csv": ("imednet.utils.pandas", "import_records_csv"),
    "MASKED_SENTINEL": ("imednet.utils.tabular", "MASKED_SENTINEL"),
    "ensure_no_masked_sentinel": ("imednet.utils.tabular", "ensure_no_masked_sentinel"),
    "normalize_records_input": ("imednet.utils.tabular", "normalize_records_input"),
    "normalize_tabular_row": ("imednet.utils.tabular", "normalize_tabular_row"),
    "ELIGIBLE_SITE_ENROLLMENT_STATUSES": (
        "imednet.utils.enrollment",
        "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
    ),
    "extract_candidate_subject_key": ("imednet.utils.enrollment", "extract_candidate_subject_key"),
    "filter_existing_subject_registrations": (
        "imednet.utils.enrollment",
        "filter_existing_subject_registrations",
    ),
    "is_site_enrollment_eligible": ("imednet.utils.enrollment", "is_site_enrollment_eligible"),
    "normalize_subject_registration_inputs": (
        "imednet.utils.enrollment",
        "normalize_subject_registration_inputs",
    ),
    "validate_subject_registration_sites": (
        "imednet.utils.enrollment",
        "validate_subject_registration_sites",
    ),
    "parse_bool": ("imednet.utils.validators", "parse_bool"),
    "parse_datetime": ("imednet.utils.validators", "parse_datetime"),
    "parse_int_or_default": ("imednet.utils.validators", "parse_int_or_default"),
    "parse_str_or_default": ("imednet.utils.validators", "parse_str_or_default"),
    "parse_list_or_default": ("imednet.utils.validators", "parse_list_or_default"),
    "parse_dict_or_default": ("imednet.utils.validators", "parse_dict_or_default"),
    "sanitize_base_url": ("imednet.utils.url", "sanitize_base_url"),
    "build_safe_path": ("imednet.utils.url", "build_safe_path"),
    "validate_path_segment": ("imednet.utils.url", "validate_path_segment"),
    "sanitize_csv_formula": ("imednet.utils.security", "sanitize_csv_formula"),
    "sanitize_diagnostic_payload": ("imednet.utils.security", "sanitize_diagnostic_payload"),
    "sanitize_diagnostic_text": ("imednet.utils.security", "sanitize_diagnostic_text"),
    "validate_partition_key": ("imednet.utils.security", "validate_partition_key"),
    "RestrictedAstEvaluator": ("imednet.utils.evaluator", "RestrictedAstEvaluator"),
    "evaluate_expression": ("imednet.utils.evaluator", "evaluate_expression"),
    "parse_expression": ("imednet.utils.evaluator", "parse_expression"),
    "DEFAULT_BATCH_CHUNK_SIZE": ("imednet.utils.batch_coordinator", "DEFAULT_BATCH_CHUNK_SIZE"),
    "AsyncBatchCoordinator": ("imednet.utils.batch_coordinator", "AsyncBatchCoordinator"),
    "BatchCoordinator": ("imednet.utils.batch_coordinator", "BatchCoordinator"),
    "BatchSubmissionSummary": ("imednet.utils.batch_coordinator", "BatchSubmissionSummary"),
    "chunk_records": ("imednet.utils.batch_coordinator", "chunk_records"),
    "extract_single_submitted_job": (
        "imednet.utils.batch_coordinator",
        "extract_single_submitted_job",
    ),
    "poll_facade_many_async": ("imednet.utils.batch_coordinator", "poll_facade_many_async"),
    "poll_facade_many_sync": ("imednet.utils.batch_coordinator", "poll_facade_many_sync"),
    "poll_facade_single_async": ("imednet.utils.batch_coordinator", "poll_facade_single_async"),
    "poll_facade_single_sync": ("imednet.utils.batch_coordinator", "poll_facade_single_sync"),
    "validate_single_job_preflight": (
        "imednet.utils.batch_coordinator",
        "validate_single_job_preflight",
    ),
    "BatchExecutionLedger": ("imednet.utils.batch_ledger", "BatchExecutionLedger"),
    "LedgerChunkRecord": ("imednet.utils.batch_ledger", "LedgerChunkRecord"),
    "resolve_opt_in_ledger": ("imednet.utils.batch_ledger", "resolve_opt_in_ledger"),
    "RawRecordEnvelope": ("imednet.utils.write_pipeline", "RawRecordEnvelope"),
    "WriteIngestionPipeline": ("imednet.utils.write_pipeline", "WriteIngestionPipeline"),
    "compute_chunk_sha256": ("imednet.utils.write_pipeline", "compute_chunk_sha256"),
    "compute_sequence_sha256": ("imednet.utils.write_pipeline", "compute_sequence_sha256"),
}


def __getattr__(name: str) -> Any:
    """Lazy load utility functions from their respective modules."""
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
    "ALLOWED_OPERATORS",
    "DEFAULT_BATCH_CHUNK_SIZE",
    "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
    "MASKED_SENTINEL",
    "ORDERED_FILTER_OPERATORS",
    "AsyncBatchCoordinator",
    "BatchCoordinator",
    "BatchExecutionLedger",
    "BatchSubmissionSummary",
    "FilterValue",
    "ItemId",
    "JsonDict",
    "LedgerChunkRecord",
    "RawRecordEnvelope",
    "RecordsInput",
    "RestrictedAstEvaluator",
    "WriteIngestionPipeline",
    "build_filter_string",
    "build_safe_path",
    "check_pandas",
    "chunk_records",
    "compute_chunk_sha256",
    "compute_sequence_sha256",
    "configure_json_logging",
    "csv_to_records",
    "dataframe_to_records",
    "ensure_no_masked_sentinel",
    "evaluate_expression",
    "export_records_csv",
    "extract_candidate_subject_key",
    "extract_single_submitted_job",
    "filter_existing_subject_registrations",
    "flatten",
    "format_iso_datetime",
    "forms_to_dataframe",
    "import_records_csv",
    "intervals_to_dataframe",
    "is_site_enrollment_eligible",
    "normalize_iso_date",
    "normalize_records_input",
    "normalize_subject_registration_inputs",
    "normalize_tabular_row",
    "normalize_utc_timestamp_bound",
    "parse_bool",
    "parse_datetime",
    "parse_dict_or_default",
    "parse_expression",
    "parse_filter_expression",
    "parse_filter_expressions",
    "parse_int_or_default",
    "parse_iso_datetime",
    "parse_list_or_default",
    "parse_str_or_default",
    "poll_facade_many_async",
    "poll_facade_many_sync",
    "poll_facade_single_async",
    "poll_facade_single_sync",
    "queries_to_dataframe",
    "records_to_dataframe",
    "resolve_opt_in_ledger",
    "sanitize_base_url",
    "sanitize_csv_formula",
    "sanitize_diagnostic_payload",
    "sanitize_diagnostic_text",
    "sites_to_dataframe",
    "subjects_to_dataframe",
    "to_arrow_table",
    "users_to_dataframe",
    "validate_connector_exclusivity",
    "validate_filter_key",
    "validate_filter_operator",
    "validate_filter_syntax",
    "validate_partition_key",
    "validate_path_segment",
    "validate_record_filter",
    "validate_single_job_preflight",
    "validate_subject_registration_sites",
    "variables_to_dataframe",
    "visits_to_dataframe",
]
