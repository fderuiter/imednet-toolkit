"""Base mixin for schema validation logic."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _extract_form_ref(record: Mapping[str, Any]) -> tuple[str | None, Any]:
    """Extract (form_key, form_id) from a record payload without coalescing None to 0."""
    raw_fk = record.get("formKey") or record.get("form_key")
    fk = str(raw_fk) if raw_fk else None
    raw_fid = record.get("formId") if record.get("formId") is not None else record.get("form_id")
    fid = None if raw_fid == "" else raw_fid
    return fk, fid


class _ValidatorMixin:
    """Shared logic for schema validators."""

    schema: Any

    def _resolve_form_key(self, record: dict[str, Any]) -> str | None:
        """Resolve the form key from a record payload.

        Args:
            record: The record data dictionary.

        Returns:
            Optional[str]: The resolved form key.
        """
        fk, fid = _extract_form_ref(record)
        if fk:
            return fk
        if fid is not None and not isinstance(fid, bool):
            return self.schema.form_key_from_id(fid)  # type: ignore[no-any-return]
        return None

    def _validate_cached(self, form_key: str | None, data: dict[str, Any]) -> None:
        """Validate data against a cached form schema.

        Args:
            form_key: The form key to validate against.
            data: The record data to validate.
        """
        if form_key:
            from .cache import validate_record_data

            validate_record_data(self.schema, form_key, data)
