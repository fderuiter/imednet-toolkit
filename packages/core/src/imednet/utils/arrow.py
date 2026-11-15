"""PyArrow serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from imednet.utils.validators import is_boolean_token, parse_bool

try:
    import pyarrow as pa
except ImportError:  # pragma: no cover - exercised when optional dependency is absent
    pa: Any = None  # type: ignore[no-redef]


class _ModelDumpable(Protocol):
    """Protocol for objects that can be dumped to a dictionary (e.g., Pydantic models)."""

    def model_dump(self) -> dict[str, Any]: ...


def _normalize_datetime(value: datetime) -> datetime:
    """Ensure a datetime object is timezone-aware and set to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_record(record: Any) -> dict[str, Any]:
    """Convert an input record (dict or model) into a standard dictionary."""
    if isinstance(record, dict):
        return record

    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped

    raise TypeError("Each record must be a dictionary or expose model_dump().")


def _normalize_value(value: Any) -> Any:
    """Normalize individual values for Arrow serialization (empty strings to None, etc.)."""
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        return None
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, float):
        return float(value)
    return value


def _infer_type(values: list[Any]) -> pa.DataType:
    """Infer the Arrow data type from a list of normalized values."""
    non_null_values = [v for v in values if v is not None]
    if not non_null_values:
        return pa.null()
    if all(isinstance(v, bool) for v in non_null_values):
        return pa.bool_()
    if all(isinstance(v, datetime) for v in non_null_values):
        return pa.timestamp("us")
    if all(isinstance(v, float) for v in non_null_values):
        return pa.float64()
    return pa.infer_type(non_null_values)


def _coerce_value(value: Any, target_type: pa.DataType) -> Any:
    """Coerce a value to the target Arrow data type."""
    if value is None:
        return None
    if pa.types.is_null(target_type):
        return None
    if pa.types.is_timestamp(target_type):
        return value if isinstance(value, datetime) else None
    if pa.types.is_boolean(target_type):
        if isinstance(value, str):
            return parse_bool(value) if is_boolean_token(value) else None
        return bool(value)
    if pa.types.is_floating(target_type):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return value


def to_arrow_table(
    data_records: list[dict[str, Any] | _ModelDumpable], schema: pa.Schema | None = None
) -> pa.Table:
    """Serialize record dictionaries (or Pydantic-like objects) into a ``pyarrow.Table``.

    Args:
        data_records: Record payloads to serialize. Each item must be a dictionary
            or expose a ``model_dump()`` method that returns a dictionary.
        schema: Optional explicit Arrow schema. When provided, output columns follow
            schema order and types; when omitted, columns and types are inferred.
            Naive ``datetime`` values are interpreted as UTC.
            When schema inference is used, datetime columns use microsecond precision.
            Boolean strings accept ``true/false``, ``1/0``, ``yes/no``, ``y/n``,
            and ``t/f``.

    Returns:
        A fully initialized ``pyarrow.Table`` with deterministic columns and null
        values for missing or empty-string inputs.

    Raises:
        ImportError: If ``pyarrow`` is not installed.
        TypeError: If a record is not dict-like and does not expose ``model_dump``.
    """
    if pa is None:
        raise ImportError(
            "pyarrow is required for to_arrow_table. Install with \"pip install 'imednet[export]'\"."
        )

    records = [_normalize_record(record) for record in data_records]

    if schema is None:
        if not records:
            return pa.table({})
        column_names = sorted({key for record in records for key in record})
    else:
        column_names = list(schema.names)

    arrays: list[pa.Array] = []
    for name in column_names:
        values = [_normalize_value(record.get(name)) for record in records]
        target_type = schema.field(name).type if schema is not None else _infer_type(values)

        # Optimize by avoiding pa.types.* checks in a tight loop
        coerced_values: list[Any]
        if pa.types.is_null(target_type):
            coerced_values = [None] * len(values)
        elif pa.types.is_timestamp(target_type):
            coerced_values = [v if isinstance(v, datetime) else None for v in values]
        elif pa.types.is_boolean(target_type):
            # Fast path for boolean coercion leveraging core validator
            coerced_values = []
            for value in values:
                if value is None:
                    coerced_values.append(None)
                elif isinstance(value, str):
                    coerced_values.append(parse_bool(value) if is_boolean_token(value) else None)
                else:
                    coerced_values.append(bool(value))
        elif pa.types.is_floating(target_type):
            coerced_values = []
            for value in values:
                if value is None:
                    coerced_values.append(None)
                else:
                    try:
                        coerced_values.append(float(value))
                    except (TypeError, ValueError):
                        coerced_values.append(None)
        else:
            coerced_values = [None if v is None else v for v in values]

        arrays.append(pa.array(coerced_values, type=target_type))

    if schema is not None:
        return pa.Table.from_arrays(arrays, schema=schema)
    return pa.Table.from_arrays(arrays, names=column_names)
