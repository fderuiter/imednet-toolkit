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

    def _resolve_form_key(
        self,
        record: dict[str, Any],
        study_key: str | None = None,
    ) -> str | None:
        """Resolve the form key from a record payload.

        Args:
            record: The record data dictionary.
            study_key: Optional study key used for study-partitioned form ID lookup.

        Returns:
            Optional[str]: The resolved form key.
        """
        fk, fid = _extract_form_ref(record)
        if fk:
            return fk
        if fid is not None and not isinstance(fid, bool):
            from .cache import BaseSchemaCache

            if isinstance(self.schema, BaseSchemaCache):
                return self.schema.form_key_from_id(fid, study_key=study_key)
            return self.schema.form_key_from_id(fid)  # type: ignore[no-any-return]
        return None

    def _validate_cached(
        self,
        form_key: str | None,
        data: dict[str, Any],
        study_key: str | None = None,
    ) -> None:
        """Validate data against a cached form schema.

        Args:
            form_key: The form key to validate against.
            data: The record data to validate.
            study_key: Optional study key used for study-partitioned schema lookup.
        """
        if form_key:
            from .cache import validate_record_data

            validate_record_data(self.schema, form_key, data, study_key=study_key)
