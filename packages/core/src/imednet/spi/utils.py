# pylint: disable=duplicate-code
"""SPI for utility functions."""

from imednet.core.context import (
    get_base_url_context,
    get_study_context,
    reset_base_url_context,
    set_base_url_context,
)
from imednet.errors import PathTraversalValidationError
from imednet.integrations.enrichment import (
    EnrichmentPipeline,
    mask_custom_phi_recursive,
    resolve_enrichment_pipeline,
    resolve_study_configuration,
)
from imednet.utils.batch_coordinator import (
    DEFAULT_BATCH_CHUNK_SIZE,
    AsyncBatchCoordinator,
    BatchCoordinator,
    BatchSubmissionSummary,
    chunk_records,
    poll_facade_many_async,
    poll_facade_many_sync,
    poll_facade_single_async,
    poll_facade_single_sync,
)
from imednet.utils.dates import (
    format_iso_datetime,
    normalize_iso_date,
    normalize_utc_timestamp_bound,
    parse_iso_datetime,
)
from imednet.utils.db import get_sqlite_connection, sqlite_connection
from imednet.utils.enrollment import (
    ELIGIBLE_SITE_ENROLLMENT_STATUSES,
    extract_candidate_subject_key,
    filter_existing_subject_registrations,
    is_site_enrollment_eligible,
    normalize_subject_registration_inputs,
    validate_subject_registration_sites,
)
from imednet.utils.evaluator import (
    RestrictedAstEvaluator,
    evaluate_expression,
    parse_expression,
)
from imednet.utils.filters import (
    ALLOWED_OPERATORS,
    ORDERED_FILTER_OPERATORS,
    build_filter_string,
    parse_filter_expression,
    parse_filter_expressions,
    validate_filter_key,
)
from imednet.utils.job_poller import (
    AsyncJobPoller,
    InvalidJobStateError,
    InvalidJobTransitionError,
    InvalidStateTransitionError,
    JobFailedError,
    JobPoller,
    JobPollSummary,
    JobProgressCallback,
    JobProgressHandler,
    JobStatusEvent,
    JobTimeoutError,
    evaluate_job_state,
)
from imednet.utils.pandas import (
    csv_to_records,
    dataframe_to_records,
    import_records_csv,
)
from imednet.utils.secrets import redact_sensitive_payload
from imednet.utils.security import (
    SensitivityRegistry,
    global_sensitivity_registry,
    mask_clinical_phi,
    sanitize_csv_formula,
    validate_partition_key,
)
from imednet.utils.serialization import flatten
from imednet.utils.tabular import (
    MASKED_SENTINEL,
    ensure_no_masked_sentinel,
    normalize_records_input,
    normalize_tabular_row,
)
from imednet.utils.typing import FilterScalar, FilterValue, ItemId, JsonDict, RecordsInput
from imednet.utils.url import redact_sensitive_text, validate_path_segment
from imednet.utils.validators import is_boolean_token, is_missing_value, parse_bool

__all__ = [
    "ALLOWED_OPERATORS",
    "DEFAULT_BATCH_CHUNK_SIZE",
    "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
    "MASKED_SENTINEL",
    "ORDERED_FILTER_OPERATORS",
    "AsyncBatchCoordinator",
    "AsyncJobPoller",
    "BatchCoordinator",
    "BatchSubmissionSummary",
    "EnrichmentPipeline",
    "FilterScalar",
    "FilterValue",
    "InvalidJobStateError",
    "InvalidJobTransitionError",
    "InvalidStateTransitionError",
    "ItemId",
    "JobFailedError",
    "JobPollSummary",
    "JobPoller",
    "JobProgressCallback",
    "JobProgressHandler",
    "JobStatusEvent",
    "JobTimeoutError",
    "JsonDict",
    "PathTraversalValidationError",
    "RecordsInput",
    "RestrictedAstEvaluator",
    "SensitivityRegistry",
    "build_filter_string",
    "chunk_records",
    "csv_to_records",
    "dataframe_to_records",
    "ensure_no_masked_sentinel",
    "evaluate_expression",
    "evaluate_job_state",
    "extract_candidate_subject_key",
    "filter_existing_subject_registrations",
    "flatten",
    "format_iso_datetime",
    "get_base_url_context",
    "get_sqlite_connection",
    "get_study_context",
    "global_sensitivity_registry",
    "import_records_csv",
    "is_boolean_token",
    "is_missing_value",
    "is_site_enrollment_eligible",
    "mask_clinical_phi",
    "mask_custom_phi_recursive",
    "normalize_iso_date",
    "normalize_records_input",
    "normalize_subject_registration_inputs",
    "normalize_tabular_row",
    "normalize_utc_timestamp_bound",
    "parse_bool",
    "parse_expression",
    "parse_filter_expression",
    "parse_filter_expressions",
    "parse_iso_datetime",
    "poll_facade_many_async",
    "poll_facade_many_sync",
    "poll_facade_single_async",
    "poll_facade_single_sync",
    "redact_sensitive_payload",
    "redact_sensitive_text",
    "reset_base_url_context",
    "resolve_enrichment_pipeline",
    "resolve_study_configuration",
    "sanitize_csv_formula",
    "set_base_url_context",
    "sqlite_connection",
    "validate_filter_key",
    "validate_partition_key",
    "validate_path_segment",
    "validate_subject_registration_sites",
]
