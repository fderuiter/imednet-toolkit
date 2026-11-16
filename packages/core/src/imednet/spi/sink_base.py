"""Public Service Provider Interface for export sinks.

Provides base classes and utilities for external export sink implementations,
decoupling them from internal integration components.
"""

from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    _redact_uri,
    _require_optional_dep,
    apply_quality_gate,
    iter_batches,
)

__all__ = [
    "ExportSink",
    "SinkConfig",
    "_redact_uri",
    "_require_optional_dep",
    "apply_quality_gate",
    "iter_batches",
]
