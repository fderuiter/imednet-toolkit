# pylint: disable=duplicate-code
"""SPI for utility functions."""

from imednet.core.context import (
    get_base_url_context,
    get_study_context,
    reset_base_url_context,
    set_base_url_context,
)
from imednet.utils.dates import format_iso_datetime, parse_iso_datetime
from imednet.utils.db import get_sqlite_connection, sqlite_connection
from imednet.utils.evaluator import (
    RestrictedAstEvaluator,
    evaluate_expression,
    parse_expression,
)
from imednet.utils.filters import build_filter_string
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
from imednet.utils.security import mask_clinical_phi, sanitize_csv_formula
from imednet.utils.serialization import flatten
from imednet.utils.url import redact_sensitive_text
from imednet.utils.validators import is_boolean_token, is_missing_value, parse_bool

__all__ = [
    "AsyncJobPoller",
    "InvalidJobStateError",
    "InvalidJobTransitionError",
    "InvalidStateTransitionError",
    "JobFailedError",
    "JobPollSummary",
    "JobPoller",
    "JobProgressCallback",
    "JobProgressHandler",
    "JobStatusEvent",
    "JobTimeoutError",
    "RestrictedAstEvaluator",
    "build_filter_string",
    "evaluate_expression",
    "evaluate_job_state",
    "flatten",
    "format_iso_datetime",
    "get_base_url_context",
    "get_sqlite_connection",
    "get_study_context",
    "is_boolean_token",
    "is_missing_value",
    "mask_clinical_phi",
    "parse_bool",
    "parse_expression",
    "parse_iso_datetime",
    "redact_sensitive_payload",
    "redact_sensitive_text",
    "reset_base_url_context",
    "sanitize_csv_formula",
    "set_base_url_context",
    "sqlite_connection",
]
