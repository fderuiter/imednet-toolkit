"""Base mixin for schema validation logic."""

from __future__ import annotations

from typing import Any

from imednet.utils.write_pipeline import _extract_form_ref


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


__all__ = ["_ValidatorMixin", "_extract_form_ref"]
