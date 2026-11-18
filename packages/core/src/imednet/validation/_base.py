"""Base mixin for schema validation logic."""

from __future__ import annotations

from typing import Any

from imednet.errors import ValidationError


def _extract_form_identifiers(record: dict[str, Any]) -> tuple[str | None, int | None]:
    """Extract and validate formKey/form_key and formId/form_id from a record payload."""
    if not isinstance(record, dict):
        raise ValidationError("Record entry must be a dictionary")

    fk: str | None = None
    for key_name in ("formKey", "form_key"):
        if key_name in record and record[key_name] is not None:
            val = record[key_name]
            if not isinstance(val, str):
                raise ValidationError("Record 'formKey' must be a string")
            stripped = val.strip()
            if stripped:
                fk = stripped
                break

    fid: int | None = None
    for id_name in ("formId", "form_id"):
        if id_name in record and record[id_name] is not None:
            val = record[id_name]
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                raise ValidationError("Record 'formId' must be a positive integer")
            if val > 0:
                fid = val
                break

    if fk is None and fid is None:
        raise ValidationError("Record must specify 'formKey' or 'formId'")

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
        fk, fid = _extract_form_identifiers(record)
        if fk:
            return fk
        if fid is not None:
            return self.schema.form_key_from_id(fid)  # type: ignore[no-any-return]
        return None

    def _validate_cached(self, form_key: str | None, data: dict[str, Any]) -> None:
        """Validate data against a cached form schema.

        Args:
            form_key: The form key to validate against.
            data: The record data to validate.

        Raises:
            ValidationError: If form_key is missing or record data fails validation.
        """
        if not form_key or not isinstance(form_key, str) or not form_key.strip():
            raise ValidationError("Record must specify 'formKey' or 'formId'")
        if not isinstance(data, dict):
            raise ValidationError("Record data must be a dictionary")
        from .cache import validate_record_data

        validate_record_data(self.schema, form_key, data)
