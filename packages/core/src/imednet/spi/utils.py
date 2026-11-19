# pylint: disable=duplicate-code
"""SPI for utility functions."""

from imednet.core.context import (
    get_base_url_context,
    get_study_context,
    reset_base_url_context,
    set_base_url_context,
)
from imednet.errors import PathTraversalValidationError
from imednet.utils.dates import (
    format_iso_datetime,
    normalize_iso_date,
    normalize_utc_timestamp_bound,
    parse_iso_datetime,
)
from imednet.utils.db import get_sqlite_connection, sqlite_connection
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
from imednet.utils.job_ledger import (
    JobLedger,
    JobLedgerEntry,
    get_default_job_ledger_path,
    record_study_job,
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
from imednet.utils.secrets import redact_sensitive_payload
from imednet.utils.security import (
    mask_clinical_phi,
    sanitize_csv_formula,
    validate_partition_key,
)
from imednet.utils.serialization import flatten
from imednet.utils.url import redact_sensitive_text
from imednet.utils.validators import is_boolean_token, is_missing_value, parse_bool

__all__ = [
    "ALLOWED_OPERATORS",
    "ORDERED_FILTER_OPERATORS",
    "AsyncJobPoller",
    "InvalidJobStateError",
    "InvalidJobTransitionError",
    "InvalidStateTransitionError",
    "JobFailedError",
    "JobLedger",
    "JobLedgerEntry",
    "JobPollSummary",
    "JobPoller",
    "JobProgressCallback",
    "JobProgressHandler",
    "JobStatusEvent",
    "JobTimeoutError",
    "PathTraversalValidationError",
    "RestrictedAstEvaluator",
    "build_filter_string",
    "evaluate_expression",
    "evaluate_job_state",
    "flatten",
    "format_iso_datetime",
    "get_base_url_context",
    "get_default_job_ledger_path",
    "get_sqlite_connection",
    "get_study_context",
    "is_boolean_token",
    "is_missing_value",
    "mask_clinical_phi",
    "normalize_iso_date",
    "normalize_utc_timestamp_bound",
    "parse_bool",
    "parse_expression",
    "parse_filter_expression",
    "parse_filter_expressions",
    "parse_iso_datetime",
    "record_study_job",
    "redact_sensitive_payload",
    "redact_sensitive_text",
    "reset_base_url_context",
    "sanitize_csv_formula",
    "set_base_url_context",
    "sqlite_connection",
    "validate_filter_key",
    "validate_partition_key",
]
