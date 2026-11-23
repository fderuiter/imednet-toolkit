# pylint: disable=duplicate-code
"""Validation utilities for iMednet data.

This package provides tools for validating record data against study schemas,
including caching of variable metadata and data dictionary management.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cache import (
        AsyncSchemaCache,
        AsyncSchemaValidator,
        BaseSchemaCache,
        BaseSchemaValidator,
        BatchValidationReport,
        RecordValidationIssue,
        SchemaCache,
        SchemaValidator,
        ValidationSchemaProtocol,
        calculate_readiness_score,
        validate_record_data,
        validate_record_entry,
        validate_records_batch,
    )

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "AsyncSchemaCache": ("imednet.validation.cache", "AsyncSchemaCache"),
    "AsyncSchemaValidator": ("imednet.validation.cache", "AsyncSchemaValidator"),
    "BaseSchemaCache": ("imednet.validation.cache", "BaseSchemaCache"),
    "BaseSchemaValidator": ("imednet.validation.cache", "BaseSchemaValidator"),
    "BatchValidationReport": ("imednet.validation.cache", "BatchValidationReport"),
    "RecordValidationIssue": ("imednet.validation.cache", "RecordValidationIssue"),
    "SchemaCache": ("imednet.validation.cache", "SchemaCache"),
    "SchemaValidator": ("imednet.validation.cache", "SchemaValidator"),
    "ValidationSchemaProtocol": ("imednet.validation.cache", "ValidationSchemaProtocol"),
    "calculate_readiness_score": ("imednet.validation.cache", "calculate_readiness_score"),
    "validate_record_data": ("imednet.validation.cache", "validate_record_data"),
    "validate_record_entry": ("imednet.validation.cache", "validate_record_entry"),
    "validate_records_batch": ("imednet.validation.cache", "validate_records_batch"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve schema validation classes and functions on demand."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy validation exports."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AsyncSchemaCache",
    "AsyncSchemaValidator",
    "BaseSchemaCache",
    "BaseSchemaValidator",
    "BatchValidationReport",
    "RecordValidationIssue",
    "SchemaCache",
    "SchemaValidator",
    "ValidationSchemaProtocol",
    "calculate_readiness_score",
    "validate_record_data",
    "validate_record_entry",
    "validate_records_batch",
]
