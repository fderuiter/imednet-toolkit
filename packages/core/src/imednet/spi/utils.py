# pylint: disable=duplicate-code
"""SPI for utility functions."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imednet.core.context import (
        extract_sdk_security_key,
        get_base_url_context,
        get_security_key_context,
        get_study_context,
        reset_base_url_context,
        reset_security_key_context,
        sdk_governance_context,
        security_key_context,
        set_base_url_context,
        set_security_key_context,
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
        fetch_terminal_job_result,
        resolve_sdk_result_fetcher,
    )
    from imednet.utils.pandas import (
        csv_to_records,
        dataframe_to_records,
        import_records_csv,
    )
    from imednet.utils.secrets import is_sensitive_key, redact_sensitive_payload
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
    from imednet.utils.url import (
        redact_sensitive_text,
        redact_url_query,
        sanitize_base_url,
        validate_path_segment,
    )
    from imednet.utils.validators import is_boolean_token, is_missing_value, parse_bool

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "ALLOWED_OPERATORS": ("imednet.utils.filters", "ALLOWED_OPERATORS"),
    "AsyncBatchCoordinator": ("imednet.utils.batch_coordinator", "AsyncBatchCoordinator"),
    "AsyncJobPoller": ("imednet.utils.job_poller", "AsyncJobPoller"),
    "BatchCoordinator": ("imednet.utils.batch_coordinator", "BatchCoordinator"),
    "BatchSubmissionSummary": ("imednet.utils.batch_coordinator", "BatchSubmissionSummary"),
    "DEFAULT_BATCH_CHUNK_SIZE": ("imednet.utils.batch_coordinator", "DEFAULT_BATCH_CHUNK_SIZE"),
    "ELIGIBLE_SITE_ENROLLMENT_STATUSES": (
        "imednet.utils.enrollment",
        "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
    ),
    "EnrichmentPipeline": ("imednet.integrations.enrichment", "EnrichmentPipeline"),
    "FilterScalar": ("imednet.utils.typing", "FilterScalar"),
    "FilterValue": ("imednet.utils.typing", "FilterValue"),
    "InvalidJobStateError": ("imednet.utils.job_poller", "InvalidJobStateError"),
    "InvalidJobTransitionError": ("imednet.utils.job_poller", "InvalidJobTransitionError"),
    "InvalidStateTransitionError": ("imednet.utils.job_poller", "InvalidStateTransitionError"),
    "ItemId": ("imednet.utils.typing", "ItemId"),
    "JobFailedError": ("imednet.utils.job_poller", "JobFailedError"),
    "JobPollSummary": ("imednet.utils.job_poller", "JobPollSummary"),
    "JobPoller": ("imednet.utils.job_poller", "JobPoller"),
    "JobProgressCallback": ("imednet.utils.job_poller", "JobProgressCallback"),
    "JobProgressHandler": ("imednet.utils.job_poller", "JobProgressHandler"),
    "JobStatusEvent": ("imednet.utils.job_poller", "JobStatusEvent"),
    "JobTimeoutError": ("imednet.utils.job_poller", "JobTimeoutError"),
    "JsonDict": ("imednet.utils.typing", "JsonDict"),
    "MASKED_SENTINEL": ("imednet.utils.tabular", "MASKED_SENTINEL"),
    "ORDERED_FILTER_OPERATORS": ("imednet.utils.filters", "ORDERED_FILTER_OPERATORS"),
    "PathTraversalValidationError": ("imednet.errors", "PathTraversalValidationError"),
    "RecordsInput": ("imednet.utils.typing", "RecordsInput"),
    "RestrictedAstEvaluator": ("imednet.utils.evaluator", "RestrictedAstEvaluator"),
    "SensitivityRegistry": ("imednet.utils.security", "SensitivityRegistry"),
    "build_filter_string": ("imednet.utils.filters", "build_filter_string"),
    "chunk_records": ("imednet.utils.batch_coordinator", "chunk_records"),
    "csv_to_records": ("imednet.utils.pandas", "csv_to_records"),
    "dataframe_to_records": ("imednet.utils.pandas", "dataframe_to_records"),
    "ensure_no_masked_sentinel": ("imednet.utils.tabular", "ensure_no_masked_sentinel"),
    "evaluate_expression": ("imednet.utils.evaluator", "evaluate_expression"),
    "evaluate_job_state": ("imednet.utils.job_poller", "evaluate_job_state"),
    "extract_candidate_subject_key": (
        "imednet.utils.enrollment",
        "extract_candidate_subject_key",
    ),
    "extract_sdk_security_key": ("imednet.core.context", "extract_sdk_security_key"),
    "fetch_terminal_job_result": ("imednet.utils.job_poller", "fetch_terminal_job_result"),
    "filter_existing_subject_registrations": (
        "imednet.utils.enrollment",
        "filter_existing_subject_registrations",
    ),
    "flatten": ("imednet.utils.serialization", "flatten"),
    "format_iso_datetime": ("imednet.utils.dates", "format_iso_datetime"),
    "get_base_url_context": ("imednet.core.context", "get_base_url_context"),
    "get_security_key_context": ("imednet.core.context", "get_security_key_context"),
    "get_sqlite_connection": ("imednet.utils.db", "get_sqlite_connection"),
    "get_study_context": ("imednet.core.context", "get_study_context"),
    "global_sensitivity_registry": ("imednet.utils.security", "global_sensitivity_registry"),
    "import_records_csv": ("imednet.utils.pandas", "import_records_csv"),
    "is_boolean_token": ("imednet.utils.validators", "is_boolean_token"),
    "is_missing_value": ("imednet.utils.validators", "is_missing_value"),
    "is_sensitive_key": ("imednet.utils.secrets", "is_sensitive_key"),
    "is_site_enrollment_eligible": ("imednet.utils.enrollment", "is_site_enrollment_eligible"),
    "mask_clinical_phi": ("imednet.utils.security", "mask_clinical_phi"),
    "mask_custom_phi_recursive": (
        "imednet.integrations.enrichment",
        "mask_custom_phi_recursive",
    ),
    "normalize_iso_date": ("imednet.utils.dates", "normalize_iso_date"),
    "normalize_records_input": ("imednet.utils.tabular", "normalize_records_input"),
    "normalize_subject_registration_inputs": (
        "imednet.utils.enrollment",
        "normalize_subject_registration_inputs",
    ),
    "normalize_tabular_row": ("imednet.utils.tabular", "normalize_tabular_row"),
    "normalize_utc_timestamp_bound": ("imednet.utils.dates", "normalize_utc_timestamp_bound"),
    "parse_bool": ("imednet.utils.validators", "parse_bool"),
    "parse_expression": ("imednet.utils.evaluator", "parse_expression"),
    "parse_filter_expression": ("imednet.utils.filters", "parse_filter_expression"),
    "parse_filter_expressions": ("imednet.utils.filters", "parse_filter_expressions"),
    "parse_iso_datetime": ("imednet.utils.dates", "parse_iso_datetime"),
    "poll_facade_many_async": ("imednet.utils.batch_coordinator", "poll_facade_many_async"),
    "poll_facade_many_sync": ("imednet.utils.batch_coordinator", "poll_facade_many_sync"),
    "poll_facade_single_async": ("imednet.utils.batch_coordinator", "poll_facade_single_async"),
    "poll_facade_single_sync": ("imednet.utils.batch_coordinator", "poll_facade_single_sync"),
    "redact_sensitive_payload": ("imednet.utils.secrets", "redact_sensitive_payload"),
    "redact_sensitive_text": ("imednet.utils.url", "redact_sensitive_text"),
    "redact_url_query": ("imednet.utils.url", "redact_url_query"),
    "reset_base_url_context": ("imednet.core.context", "reset_base_url_context"),
    "reset_security_key_context": ("imednet.core.context", "reset_security_key_context"),
    "resolve_enrichment_pipeline": (
        "imednet.integrations.enrichment",
        "resolve_enrichment_pipeline",
    ),
    "resolve_sdk_result_fetcher": ("imednet.utils.job_poller", "resolve_sdk_result_fetcher"),
    "resolve_study_configuration": (
        "imednet.integrations.enrichment",
        "resolve_study_configuration",
    ),
    "sanitize_base_url": ("imednet.utils.url", "sanitize_base_url"),
    "sanitize_csv_formula": ("imednet.utils.security", "sanitize_csv_formula"),
    "sdk_governance_context": ("imednet.core.context", "sdk_governance_context"),
    "security_key_context": ("imednet.core.context", "security_key_context"),
    "set_base_url_context": ("imednet.core.context", "set_base_url_context"),
    "set_security_key_context": ("imednet.core.context", "set_security_key_context"),
    "sqlite_connection": ("imednet.utils.db", "sqlite_connection"),
    "validate_filter_key": ("imednet.utils.filters", "validate_filter_key"),
    "validate_partition_key": ("imednet.utils.security", "validate_partition_key"),
    "validate_path_segment": ("imednet.utils.url", "validate_path_segment"),
    "validate_subject_registration_sites": (
        "imednet.utils.enrollment",
        "validate_subject_registration_sites",
    ),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve SPI utility functions from core submodules."""
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
    "extract_sdk_security_key",
    "fetch_terminal_job_result",
    "filter_existing_subject_registrations",
    "flatten",
    "format_iso_datetime",
    "get_base_url_context",
    "get_security_key_context",
    "get_sqlite_connection",
    "get_study_context",
    "global_sensitivity_registry",
    "import_records_csv",
    "is_boolean_token",
    "is_missing_value",
    "is_sensitive_key",
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
    "redact_url_query",
    "reset_base_url_context",
    "reset_security_key_context",
    "resolve_enrichment_pipeline",
    "resolve_sdk_result_fetcher",
    "resolve_study_configuration",
    "sanitize_base_url",
    "sanitize_csv_formula",
    "sdk_governance_context",
    "security_key_context",
    "set_base_url_context",
    "set_security_key_context",
    "sqlite_connection",
    "validate_filter_key",
    "validate_partition_key",
    "validate_path_segment",
    "validate_subject_registration_sites",
]
