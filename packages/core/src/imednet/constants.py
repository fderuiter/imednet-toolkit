"""Common constants used throughout the iMednet SDK.

This module centralizes configuration constants to avoid magic numbers
and improve maintainability.
"""

from __future__ import annotations

__all__ = [  # noqa: RUF022
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_RETRIES",
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_PAGE_SIZE",
    "LARGE_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "MAX_SQLITE_COLUMNS",
    "TERMINAL_JOB_STATES",
    # HTTP Headers
    "HEADER_ACCEPT",
    "HEADER_CONTENT_TYPE",
    "HEADER_API_KEY",
    "HEADER_SECURITY_KEY",
    "HEADER_EMAIL_NOTIFY",
    "CONTENT_TYPE_JSON",
]

# API Configuration
DEFAULT_BASE_URL = "https://edc.prod.imednetapi.com"
"""Default base URL for the iMednet API."""

# HTTP Client Configuration
DEFAULT_TIMEOUT = 30.0
"""Default timeout in seconds for HTTP requests."""

DEFAULT_RETRIES = 3
"""Default number of retry attempts for failed requests."""

DEFAULT_BACKOFF_FACTOR = 1.0
"""Default backoff factor for exponential retry delays."""

# Pagination Configuration
DEFAULT_PAGE_SIZE = 100
"""Default number of items to fetch per page."""

LARGE_PAGE_SIZE = 500
"""Page size for endpoints with large metadata (forms, intervals, variables)."""

MAX_PAGE_SIZE = 500
"""Maximum number of items per page allowed by the upstream iMednet API."""

# Database Limits
MAX_SQLITE_COLUMNS = 2000
"""Maximum number of columns allowed in a SQLite table.

When exporting data to SQLite, tables exceeding this limit must be split
into multiple tables.
"""

# Job States
TERMINAL_JOB_STATES = frozenset({"COMPLETED", "SUCCESS", "FAILED", "CANCELLED", ""})
"""Job states that indicate the job has finished processing.

These states are used by the job poller to determine when to stop polling.
Jobs in these states will not transition to another state.
"""

# HTTP Headers
HEADER_ACCEPT = "Accept"
"""HTTP Accept header name."""

HEADER_CONTENT_TYPE = "Content-Type"
"""HTTP Content-Type header name."""

HEADER_API_KEY = "x-api-key"
"""iMednet API key header name."""

HEADER_SECURITY_KEY = "x-imn-security-key"
"""iMednet security key header name."""

HEADER_EMAIL_NOTIFY = "x-email-notify"
"""iMednet email notification header name."""

# Content Types
CONTENT_TYPE_JSON = "application/json"
"""JSON content type value."""
