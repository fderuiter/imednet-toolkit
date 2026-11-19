"""Deterministic example values for variable types.

Used in tests and smoke scripts to exercise typed fields.
"""

from __future__ import annotations

from typing import Any

from imednet.validation.cache import (
    _CANONICAL_VARIABLE_TAXONOMY,
    _normalize_variable_type_key,
)

# Maps normalized variable type synonyms to deterministic example values.
_TYPED_VALUES: dict[str, Any] = {key: spec[1] for key, spec in _CANONICAL_VARIABLE_TAXONOMY.items()}

# Maps normalized variable type synonyms to canonical categories.
_CANONICAL_TYPES: dict[str, str] = {
    key: spec[0] for key, spec in _CANONICAL_VARIABLE_TAXONOMY.items()
}


def canonical_type(var_type: str) -> str | None:
    """Return the canonical type for ``var_type`` or ``None`` if unsupported."""
    return _CANONICAL_TYPES.get(_normalize_variable_type_key(var_type))


def value_for(var_type: str) -> Any | None:
    """Return a deterministic example value for ``var_type`` if supported."""
    return _TYPED_VALUES.get(_normalize_variable_type_key(var_type))
