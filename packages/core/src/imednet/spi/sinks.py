"""Supported core capabilities for external export sink implementations."""

from typing import Any

from imednet.integrations.enrichment import CentralizedMapper
from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    _redact_uri,
    _require_optional_dep,
    apply_quality_gate,
    iter_batches,
)

__all__ = [
    "CentralizedMapper",
    "ExportSink",
    "SinkConfig",
    "apply_quality_gate",
    "iter_batches",
    "redact_uri",
    "require_optional_dep",
]


def redact_uri(uri: str) -> str:
    """Return a destination URI with credentials masked."""
    return _redact_uri(uri)


def require_optional_dep(module_name: str, extra: str) -> Any:
    """Load a destination driver or raise the existing actionable install error."""
    return _require_optional_dep(module_name, extra)
