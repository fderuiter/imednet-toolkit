"""Two-stage write ingestion pipeline with raw record envelopes and deferred schema binding."""

from __future__ import annotations

import ast
import csv
import datetime
import decimal
import hashlib
import io
import json
import math
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from imednet.errors import ValidationError
from imednet.utils.typing import JsonDict

if TYPE_CHECKING:
    from imednet.validation.cache import ValidationSchemaProtocol

MASKED_SENTINEL: str = "***MASKED***"


def _extract_form_ref(record: Mapping[str, Any]) -> tuple[str | None, Any]:
    """Extract ``(form_key, form_id)`` from a record payload without coalescing ``None`` to 0."""
    raw_fk = record.get("formKey") or record.get("form_key")
    fk = str(raw_fk) if raw_fk else None
    raw_fid = record.get("formId") if record.get("formId") is not None else record.get("form_id")
    fid = None if raw_fid == "" else raw_fid
    return fk, fid


SourceKind = Literal["csv", "tabular", "json", "mapping", "cli"]

_INT_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)$")
_DECIMAL_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)\.\d+$")
_SIGNED_DIGITS_RE = re.compile(r"^[+-]?\d+$")
_SIGNED_DECIMAL_RE = re.compile(r"^[+-]?\d+\.\d+$")
_SLASH_SPACE_RE = re.compile(r"\s*/\s*")

_READ_ONLY_RECORD_COLUMNS: frozenset[str] = frozenset(
    {
        "study_key",
        "studyKey",
        "record_type",
        "recordType",
        "deleted",
        "date_created",
        "dateCreated",
        "date_modified",
        "dateModified",
        "last_updated",
        "lastUpdated",
        "parent_record_id",
        "parentRecordId",
        "keywords",
    }
)

_NESTED_DATA_COLUMNS: frozenset[str] = frozenset({"record_data", "recordData", "data"})

_IDENTIFIER_COLUMNS: frozenset[str] = frozenset(
    {
        "record_id",
        "recordId",
        "record_oid",
        "recordOid",
        "visit_id",
        "visitId",
        "record_status",
        "recordStatus",
        "reason_for_change",
        "reasonForChange",
        "form_key",
        "formKey",
        "form_id",
        "formId",
        "subject_key",
        "subjectKey",
        "subject_oid",
        "subjectOid",
        "subject_id",
        "subjectId",
        "site_name",
        "siteName",
        "site_id",
        "siteId",
        "interval_name",
        "intervalName",
        "interval_id",
        "intervalId",
    }
)

_DATE_ONLY_TYPES: frozenset[str] = frozenset(
    {
        "date",
        "date ymd",
        "datedmy",
        "date dmy",
        "datemdy",
        "date mdy",
    }
)

_TIME_ONLY_TYPES: frozenset[str] = frozenset(
    {
        "time",
        "partialtime",
        "incompletetime",
    }
)

_DATETIME_TYPES: frozenset[str] = frozenset(
    {
        "datetime",
        "date/time",
        "date time",
        "durationdatetime",
        "intervaldatetime",
        "partialdate",
        "incompletedate",
        "partialdatetime",
        "incompletedatetime",
        "date/time precision",
        "datetime precision",
        "date time precision",
    }
)

_TEXT_LIKE_TYPES: frozenset[str] = (
    frozenset(
        {
            "string",
            "text",
            "alphanumeric",
            "memo",
            "textarea",
            "text area",
            "notes",
            "note",
            "file",
            "upload",
            "fileupload",
            "file upload",
            "signature",
            "descriptive",
            "label",
            "uri",
        }
    )
    | _DATE_ONLY_TYPES
    | _TIME_ONLY_TYPES
    | _DATETIME_TYPES
)

_SINGLE_CHOICE_TYPES: frozenset[str] = frozenset(
    {
        "coded",
        "radio",
        "radiobutton",
        "radio button",
        "dropdown",
        "drop down",
        "dropdownlist",
        "dropdown list",
        "select",
        "single select",
        "singleselect",
        "sql",
        "yes/no",
        "yesno",
        "yes no",
        "true/false",
        "truefalse",
        "true false",
    }
)

_MULTI_SELECT_TYPES: frozenset[str] = frozenset(
    {
        "multi select",
        "multiselect",
    }
)

_CHECKBOX_TYPES: frozenset[str] = frozenset(
    {
        "checkbox",
        "check box",
    }
)

_CATEGORICAL_CHOICE_TYPES: frozenset[str] = (
    _SINGLE_CHOICE_TYPES | _MULTI_SELECT_TYPES | _CHECKBOX_TYPES
)

_INT_LIKE_TYPES: frozenset[str] = frozenset({"int", "integer", "number"})
_FLOAT_LIKE_TYPES: frozenset[str] = frozenset({"float", "decimal", "double", "numeric"})
_NUMERIC_LIKE_TYPES: frozenset[str] = _INT_LIKE_TYPES | _FLOAT_LIKE_TYPES
_BOOL_LIKE_TYPES: frozenset[str] = frozenset({"bool", "boolean", "logic"})
_COMPUTED_FLEX_TYPES: frozenset[str] = frozenset({"calculation", "calc", "calculated", "slider"})


def _normalize_var_type_name(raw_type: Any) -> str | None:
    """Normalize casing, whitespace, and word separators for variable type lookup."""
    if not isinstance(raw_type, str):
        return None
    cleaned = re.sub(r"[\s_-]+", " ", raw_type.strip().lower())
    if not cleaned:
        return None
    if "/" in cleaned:
        cleaned = _SLASH_SPACE_RE.sub("/", cleaned)
    return cleaned


def _unwrap_scalar(value: Any) -> Any:
    """Unwrap NumPy/Pandas scalar wrappers into native Python primitives."""
    if value is None:
        return None
    cls_name = type(value).__name__
    if cls_name in {"NAType", "NaTType"}:
        return value
    if cls_name in {"Timestamp", "datetime64"}:
        return value
    if isinstance(
        value,
        (
            str,
            bytes,
            int,
            float,
            bool,
            decimal.Decimal,
            dict,
            list,
            tuple,
            set,
            frozenset,
            datetime.date,
            datetime.time,
        ),
    ):
        return value
    item_fn = getattr(value, "item", None)
    if callable(item_fn):
        try:
            return item_fn()
        except (ValueError, TypeError):
            return value
    return value


def _contains_masked_sentinel(value: Any) -> bool:
    """Return True if *value* or any nested element contains ``***MASKED***``."""
    unwrapped = _unwrap_scalar(value)
    if isinstance(unwrapped, str):
        return MASKED_SENTINEL in unwrapped
    if isinstance(unwrapped, Mapping):
        return any(_contains_masked_sentinel(v) for v in unwrapped.values())
    if isinstance(unwrapped, (list, tuple, set, frozenset)):
        return any(_contains_masked_sentinel(v) for v in unwrapped)
    return False


def ensure_no_masked_sentinel(records: Sequence[Mapping[str, Any]]) -> None:
    """Raise :class:`ValidationError` if any record contains ``***MASKED***``."""
    for row_idx, rec in enumerate(records):
        if not isinstance(rec, Mapping):
            continue
        for key, value in rec.items():
            if key in _NESTED_DATA_COLUMNS and isinstance(value, Mapping):
                for var_name, var_val in value.items():
                    if _contains_masked_sentinel(var_val):
                        raise ValidationError(
                            f"Row {row_idx}: variable '{var_name}' contains masked PHI sentinel "
                            f"'{MASKED_SENTINEL}' and cannot be submitted."
                        )
            elif _contains_masked_sentinel(value):
                raise ValidationError(
                    f"Row {row_idx}: column '{key}' contains masked PHI sentinel "
                    f"'{MASKED_SENTINEL}' and cannot be submitted."
                )


def _is_missing_cell(value: Any) -> bool:
    """Return True when a tabular cell represents a null, NaN, or blank value."""
    unwrapped = _unwrap_scalar(value)
    if unwrapped is None:
        return True
    if isinstance(unwrapped, (dict, list, tuple, set, frozenset)):
        return False
    if isinstance(unwrapped, float) and math.isnan(unwrapped):
        return True
    if isinstance(unwrapped, str):
        return unwrapped.strip() == ""
    cls_name = type(unwrapped).__name__
    if cls_name in {"NAType", "NaTType"}:
        return True
    if cls_name == "Timestamp" and str(unwrapped) == "NaT":
        return True
    return cls_name == "datetime64" and str(unwrapped) == "NaT"


def _is_dataframe_instance(obj: object) -> bool:
    """Return True if *obj* is a ``pandas.DataFrame`` without requiring ``pandas`` import."""
    cls = type(obj)
    return cls.__name__ == "DataFrame" and cls.__module__.startswith("pandas")


def _coerce_str_identifier(value: Any) -> str:
    """Convert an identifier cell to a clean string, coercing integer floats."""
    unwrapped = _unwrap_scalar(value)
    if isinstance(unwrapped, float) and math.isfinite(unwrapped) and unwrapped.is_integer():
        return str(int(unwrapped))
    if isinstance(unwrapped, decimal.Decimal) and unwrapped.is_finite():
        if unwrapped == unwrapped.to_integral_value():
            return str(int(unwrapped))
        return str(unwrapped)
    return str(unwrapped).strip()


def _coerce_int_identifier(value: Any, *, row_index: int, column_name: str) -> int:
    """Convert an integer identifier cell (e.g. ``formId``, ``siteId``) to ``int``."""
    unwrapped = _unwrap_scalar(value)
    if isinstance(unwrapped, bool):
        raise ValidationError(f"Row {row_index}: identifier '{column_name}' must be an integer.")
    if isinstance(unwrapped, int):
        return unwrapped
    if isinstance(unwrapped, float) and math.isfinite(unwrapped) and unwrapped.is_integer():
        return int(unwrapped)
    if (
        isinstance(unwrapped, decimal.Decimal)
        and unwrapped.is_finite()
        and unwrapped == unwrapped.to_integral_value()
    ):
        return int(unwrapped)
    if isinstance(unwrapped, str):
        stripped = unwrapped.strip()
        if _INT_LITERAL_RE.match(stripped):
            return int(stripped)
        if _DECIMAL_LITERAL_RE.match(stripped):
            fval = float(stripped)
            if math.isfinite(fval) and fval.is_integer():
                return int(fval)
    raise ValidationError(f"Row {row_index}: identifier '{column_name}' must be an integer.")


def _parse_nested_data_cell(
    raw_nested: Any,
    *,
    row_index: int,
    column_name: str,
) -> Mapping[str, Any]:
    """Parse a ``record_data`` / ``recordData`` / ``data`` cell from dict or serialized text."""
    unwrapped = _unwrap_scalar(raw_nested)
    if isinstance(unwrapped, Mapping):
        return unwrapped
    if isinstance(unwrapped, str):
        text = unwrapped.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError) as exc:
                raise ValidationError(
                    f"Row {row_index}: column '{column_name}' is not a valid JSON object."
                ) from exc
        if isinstance(parsed, Mapping):
            return parsed
    raise ValidationError(
        f"Row {row_index}: column '{column_name}' must be a mapping of variable values."
    )


def _unquote_double_quoted_literal(raw_val: str) -> str | None:
    """Return unescaped string if ``raw_val`` is wrapped in double quotes, else ``None``."""
    stripped = raw_val.strip()
    if len(stripped) < 2 or stripped[0] != '"' or stripped[-1] != '"':
        return None
    try:
        loaded = json.loads(stripped)
        if isinstance(loaded, str):
            return loaded
    except ValueError:
        pass
    return stripped[1:-1].replace('\\"', '"').replace("\\\\", "\\")


def _lookup_form_key_from_id(
    schema: ValidationSchemaProtocol,
    form_id: int | str,
    *,
    study_key: str | None = None,
) -> str | None:
    """Resolve ``formId`` to ``formKey`` while forwarding ``study_key`` when supported."""
    if study_key is not None:
        try:
            return schema.form_key_from_id(form_id, study_key=study_key)  # type: ignore[call-arg]
        except TypeError:
            pass
    return schema.form_key_from_id(form_id)


def _lookup_variables_for_form(
    schema: ValidationSchemaProtocol,
    form_key: str,
    *,
    study_key: str | None = None,
) -> Mapping[str, Any]:
    """Resolve form variables while forwarding ``study_key`` when supported."""
    if study_key is not None:
        try:
            return schema.variables_for_form(form_key, study_key=study_key)  # type: ignore[call-arg]
        except TypeError:
            pass
    return schema.variables_for_form(form_key)


def _extract_choice_codes(var_obj: Any) -> list[str]:
    """Return defined choice code strings from a DataDictionary or API Variable if present."""
    if var_obj is None:
        return []
    raw_choices = getattr(var_obj, "choices", None)
    if not isinstance(raw_choices, Iterable) or isinstance(raw_choices, (str, bytes, Mapping)):
        return []
    codes: list[str] = []
    for item in raw_choices:
        val = (
            item.get("choiceValue") or item.get("value")
            if isinstance(item, Mapping)
            else getattr(item, "choiceValue", getattr(item, "value", None))
        )
        if val is not None:
            codes.append(str(val))
    return codes


def _resolve_variable_definition(
    schema: ValidationSchemaProtocol | None,
    *,
    form_key: str | None,
    form_id: int | str | None,
    var_name: str,
    study_key: str | None = None,
    allow_formless_fallback: bool = False,
) -> tuple[str | None, Any]:
    """Return ``(normalized_var_type, var_obj)`` from *schema* scoped to *study_key*."""
    if schema is None:
        return None, None

    resolved_fk = form_key
    if not resolved_fk and form_id is not None and not isinstance(form_id, bool):
        resolved_fk = _lookup_form_key_from_id(schema, form_id, study_key=study_key)

    if resolved_fk:
        variables = _lookup_variables_for_form(schema, resolved_fk, study_key=study_key)
        if variables and var_name in variables:
            var_obj = variables[var_name]
            raw_type = (
                getattr(var_obj, "extraAttributes", {}).get("odmDataType")
                if isinstance(getattr(var_obj, "extraAttributes", None), Mapping)
                else None
            ) or (getattr(var_obj, "variable_type", None) or getattr(var_obj, "variableType", None))
            return _normalize_var_type_name(raw_type), var_obj
        return None, None

    if not allow_formless_fallback or form_id is not None:
        return None, None

    forms_for_study_fn = getattr(schema, "forms_for_study", None)
    forms_attr = getattr(schema, "forms", None)
    candidate_fks: list[str] = []
    if callable(forms_for_study_fn) and study_key:
        candidate_fks = list(forms_for_study_fn(study_key))
    elif isinstance(forms_attr, Mapping):
        candidate_fks = [str(k) for k in forms_attr]
    elif isinstance(forms_attr, Sequence) and not isinstance(forms_attr, (str, bytes)):
        candidate_fks = [
            str(getattr(f, "form_key", None) or getattr(f, "formKey", ""))
            for f in forms_attr
            if getattr(f, "form_key", None) or getattr(f, "formKey", None)
        ]

    matched: list[tuple[str, Any]] = []
    seen_types: set[str] = set()
    for fk in candidate_fks:
        form_vars = _lookup_variables_for_form(schema, fk, study_key=study_key)
        if form_vars and var_name in form_vars:
            vo = form_vars[var_name]
            raw_type = getattr(vo, "variable_type", None) or getattr(vo, "variableType", None)
            norm = _normalize_var_type_name(raw_type)
            if norm:
                seen_types.add(norm)
                matched.append((norm, vo))
    if len(seen_types) == 1 and matched:
        return matched[0]
    return None, None


def _resolve_expected_var_type(
    schema: ValidationSchemaProtocol | None,
    form_key: str | None,
    form_id: int | None,
    var_name: str,
    *,
    study_key: str | None = None,
) -> str | None:
    """Backward-compatible helper returning normalized variable type from *schema*."""
    vtype, _ = _resolve_variable_definition(
        schema,
        form_key=form_key,
        form_id=form_id,
        var_name=var_name,
        study_key=study_key,
    )
    return vtype


def _match_choice_case(candidate: str, choices: Sequence[str]) -> str:
    """Match *candidate* against *choices* preserving defined choice code casing when unambiguous."""
    if not choices or candidate in choices:
        return candidate
    low = candidate.lower()
    ci_matches = [c for c in choices if c.lower() == low]
    if len(ci_matches) == 1:
        return ci_matches[0]
    return candidate


def _serialize_temporal_value(
    unwrapped: Any,
    *,
    expected_var_type: str | None,
) -> str | None:
    """Serialize datetime, date, time, pd.Timestamp, or np.datetime64 into ISO-8601 text."""
    cls_name = type(unwrapped).__name__
    if cls_name in {"NAType", "NaTType"}:
        return None

    if cls_name == "Timestamp":
        text_ts = str(unwrapped)
        if text_ts == "NaT":
            return None
        to_pydt = getattr(unwrapped, "to_pydatetime", None)
        dt_obj: datetime.datetime | None = None
        if callable(to_pydt):
            try:
                cand = to_pydt(warn=False)
                if isinstance(cand, datetime.datetime):
                    dt_obj = cand
            except TypeError:
                cand = to_pydt()
                if isinstance(cand, datetime.datetime):
                    dt_obj = cand
        if dt_obj is not None:
            return _format_datetime_for_var_type(dt_obj, expected_var_type)
        iso_fn = getattr(unwrapped, "isoformat", None)
        return str(iso_fn()) if callable(iso_fn) else text_ts

    if cls_name == "datetime64":
        text_dt = str(unwrapped)
        if text_dt == "NaT":
            return None
        if expected_var_type in _DATE_ONLY_TYPES and "T00:00:00" in text_dt:
            return text_dt.split("T", 1)[0]
        if expected_var_type in _DATETIME_TYPES and "T" not in text_dt and len(text_dt) == 10:
            return f"{text_dt}T00:00:00"
        return text_dt

    if isinstance(unwrapped, datetime.datetime):
        return _format_datetime_for_var_type(unwrapped, expected_var_type)

    if isinstance(unwrapped, datetime.date):
        if expected_var_type in _DATETIME_TYPES:
            return f"{unwrapped.isoformat()}T00:00:00"
        return unwrapped.isoformat()

    if isinstance(unwrapped, datetime.time):
        return unwrapped.isoformat()

    return None


def _format_datetime_for_var_type(
    dt_obj: datetime.datetime,
    expected_var_type: str | None,
) -> str:
    """Format a ``datetime`` honoring target Date vs Time vs DateTime variable types."""
    if (
        expected_var_type in _DATE_ONLY_TYPES
        and dt_obj.hour == 0
        and dt_obj.minute == 0
        and dt_obj.second == 0
        and dt_obj.microsecond == 0
    ):
        return dt_obj.date().isoformat()
    if expected_var_type in _TIME_ONLY_TYPES:
        return dt_obj.time().isoformat()
    return dt_obj.isoformat()


def _serialize_decimal_value(
    val: decimal.Decimal,
    *,
    expected_var_type: str | None,
    choices: Sequence[str],
    row_index: int = 0,
    var_name: str | None = None,
) -> int | float | str:
    """Convert a ``Decimal`` into a JSON-safe ``int``, ``float``, or ``str``."""
    if not val.is_finite():
        loc = f" for variable '{var_name}'" if var_name else ""
        raise ValidationError(f"Row {row_index}: non-finite Decimal value{loc} is not valid.")

    is_integral = val == val.to_integral_value()

    if expected_var_type in _TEXT_LIKE_TYPES:
        return str(val)

    if expected_var_type in _CATEGORICAL_CHOICE_TYPES:
        raw_str = str(val)
        if choices:
            if raw_str in choices:
                return raw_str
            if is_integral:
                int_str = str(int(val))
                if int_str in choices:
                    return int_str
        return str(int(val)) if is_integral else raw_str

    if expected_var_type in _INT_LIKE_TYPES:
        return int(val) if is_integral else float(val)

    if expected_var_type in _FLOAT_LIKE_TYPES or expected_var_type in _COMPUTED_FLEX_TYPES:
        return float(val)

    # Untyped / schema-less fallback: integral Decimals become int (valid for both int & float vars)
    return int(val) if is_integral else float(val)


def _bind_scalar_value(
    value: Any,
    *,
    source_kind: SourceKind,
    is_quoted: bool = False,
    expected_var_type: str | None = None,
    var_obj: Any = None,
    row_index: int = 0,
    var_name: str | None = None,
) -> Any:
    """Bind a raw variable value against ``expected_var_type`` and ``var_obj``."""
    if isinstance(value, (list, tuple, set, frozenset)):
        bound_items = [
            _bind_scalar_value(
                elem,
                source_kind=source_kind,
                is_quoted=False,
                expected_var_type=None,
                var_obj=None,
                row_index=row_index,
                var_name=var_name,
            )
            for elem in value
        ]
        return bound_items if isinstance(value, list) else type(value)(bound_items)

    if isinstance(value, Mapping):
        return {
            str(k): _bind_scalar_value(
                v,
                source_kind=source_kind,
                is_quoted=False,
                expected_var_type=None,
                var_obj=None,
                row_index=row_index,
                var_name=str(k),
            )
            for k, v in value.items()
        }

    unwrapped = _unwrap_scalar(value)
    if unwrapped is None:
        return None

    cls_name = type(unwrapped).__name__
    if cls_name in {"NAType", "NaTType"}:
        return None

    temporal_str = _serialize_temporal_value(unwrapped, expected_var_type=expected_var_type)
    if temporal_str is not None or cls_name in {"Timestamp", "datetime64"}:
        return temporal_str

    choices = _extract_choice_codes(var_obj)

    if isinstance(unwrapped, decimal.Decimal):
        return _serialize_decimal_value(
            unwrapped,
            expected_var_type=expected_var_type,
            choices=choices,
            row_index=row_index,
            var_name=var_name,
        )

    if is_quoted and isinstance(unwrapped, str):
        return unwrapped

    from_string_only = source_kind in {"csv", "cli"}
    from_tabular = source_kind in {"csv", "tabular", "cli"}

    if expected_var_type is not None:
        if expected_var_type in _BOOL_LIKE_TYPES:
            if isinstance(unwrapped, bool):
                return unwrapped
            if isinstance(unwrapped, str):
                low = unwrapped.strip().lower()
                if low in {"true", "false"}:
                    return low == "true"
            return unwrapped

        if expected_var_type in _CHECKBOX_TYPES:
            if isinstance(unwrapped, str):
                stripped = unwrapped.strip()
                if choices and (
                    stripped in choices or any(c.lower() == stripped.lower() for c in choices)
                ):
                    return _match_choice_case(stripped, choices)
                if stripped.lower() in {"true", "false"}:
                    return stripped.lower() == "true"
                return unwrapped
            if isinstance(unwrapped, bool):
                if choices:
                    bool_str = "true" if unwrapped else "false"
                    if any(c.lower() == bool_str for c in choices):
                        return _match_choice_case(bool_str, choices)
                return unwrapped
            if from_tabular and isinstance(unwrapped, float) and math.isfinite(unwrapped):
                return str(int(unwrapped)) if unwrapped.is_integer() else str(unwrapped)
            return unwrapped

        if expected_var_type in _SINGLE_CHOICE_TYPES or expected_var_type in _MULTI_SELECT_TYPES:
            if isinstance(unwrapped, str):
                return unwrapped
            if isinstance(unwrapped, bool) and from_tabular:
                bool_str = "true" if unwrapped else "false"
                return _match_choice_case(bool_str, choices)
            if from_tabular and isinstance(unwrapped, float) and math.isfinite(unwrapped):
                return str(int(unwrapped)) if unwrapped.is_integer() else str(unwrapped)
            return unwrapped

        if expected_var_type in _TEXT_LIKE_TYPES:
            if isinstance(unwrapped, str):
                return unwrapped
            if from_tabular:
                if isinstance(unwrapped, bool):
                    return "true" if unwrapped else "false"
                if isinstance(unwrapped, float) and math.isfinite(unwrapped):
                    return str(int(unwrapped)) if unwrapped.is_integer() else str(unwrapped)
                return str(unwrapped)
            return unwrapped

        if expected_var_type in _NUMERIC_LIKE_TYPES:
            if isinstance(unwrapped, bool):
                return unwrapped
            if isinstance(unwrapped, int):
                return unwrapped
            if isinstance(unwrapped, float):
                if (
                    expected_var_type in _INT_LIKE_TYPES
                    and math.isfinite(unwrapped)
                    and unwrapped.is_integer()
                ):
                    return int(unwrapped)
                return unwrapped
            if isinstance(unwrapped, str):
                stripped = unwrapped.strip()
                if _SIGNED_DIGITS_RE.match(stripped):
                    return int(stripped)
                if _SIGNED_DECIMAL_RE.match(stripped):
                    fval = float(stripped)
                    if math.isfinite(fval):
                        if expected_var_type in _INT_LIKE_TYPES and fval.is_integer():
                            return int(fval)
                        return fval
            return unwrapped

        if expected_var_type in _COMPUTED_FLEX_TYPES:
            if isinstance(unwrapped, float) and math.isfinite(unwrapped) and unwrapped.is_integer():
                return int(unwrapped)
            return unwrapped

    # Schema-less / unknown variable fallback:
    if isinstance(unwrapped, bool):
        return unwrapped
    if isinstance(unwrapped, int):
        return unwrapped
    if isinstance(unwrapped, float):
        if source_kind == "tabular" and math.isfinite(unwrapped) and unwrapped.is_integer():
            return int(unwrapped)
        return unwrapped
    if source_kind == "cli" and isinstance(unwrapped, str):
        stripped = unwrapped.strip()
        if _INT_LITERAL_RE.match(stripped):
            return int(stripped)
        return unwrapped
    if from_string_only and isinstance(unwrapped, str):
        return unwrapped
    return unwrapped


@dataclass
class RawRecordEnvelope:
    """Raw, uncoerced Record write payload produced prior to Study schema binding."""

    envelope: JsonDict
    raw_data: dict[str, Any] = field(default_factory=dict)
    data_key: str = "data"
    source_kind: SourceKind = "mapping"
    var_sources: dict[str, SourceKind] = field(default_factory=dict)
    quoted_vars: frozenset[str] = field(default_factory=frozenset)
    study_key_hint: str | None = None
    row_index: int = 0

    @property
    def data(self) -> Mapping[str, Any]:
        """Return the uncoerced raw clinical variable mapping on this envelope."""
        return self.raw_data

    @property
    def form_key(self) -> str | None:
        """Return explicit ``formKey`` / ``form_key`` if present on the envelope."""
        fk, _ = _extract_form_ref(self.envelope)
        return fk

    @property
    def form_id(self) -> Any:
        """Return explicit ``formId`` / ``form_id`` if present on the envelope."""
        _, fid = _extract_form_ref(self.envelope)
        return fid


def _build_tabular_raw_envelope(
    raw_row: Mapping[str, Any],
    *,
    row_index: int,
    source_kind: Literal["csv", "tabular"],
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> RawRecordEnvelope | None:
    """Parse one CSV, Parquet, or DataFrame row into a :class:`RawRecordEnvelope`."""
    for col_name, raw_val in raw_row.items():
        if col_name is None:
            raise ValidationError(
                f"Row {row_index}: CSV row contains more values than header columns."
            )
        if _contains_masked_sentinel(raw_val):
            raise ValidationError(
                f"Row {row_index}: column '{col_name}' contains masked PHI sentinel "
                f"'{MASKED_SENTINEL}' and cannot be imported."
            )

    if all(_is_missing_cell(v) for v in raw_row.values()):
        return None

    def _first_present(*names: str) -> tuple[str, Any] | None:
        for name in names:
            if name in raw_row and not _is_missing_cell(raw_row[name]):
                return name, raw_row[name]
        return None

    study_hint: str | None = None
    study_pair = _first_present("studyKey", "study_key")
    if study_pair is not None:
        study_hint = _coerce_str_identifier(study_pair[1])

    envelope: JsonDict = {}

    rec_id_pair = _first_present("recordId", "record_id")
    if rec_id_pair is not None:
        envelope["recordId"] = _coerce_int_identifier(
            rec_id_pair[1], row_index=row_index, column_name=rec_id_pair[0]
        )

    rec_oid_pair = _first_present("recordOid", "record_oid")
    if rec_oid_pair is not None:
        envelope["recordOid"] = _coerce_str_identifier(rec_oid_pair[1])

    visit_id_pair = _first_present("visitId", "visit_id")
    if visit_id_pair is not None:
        envelope["visitId"] = _coerce_int_identifier(
            visit_id_pair[1], row_index=row_index, column_name=visit_id_pair[0]
        )

    rec_status_pair = _first_present("recordStatus", "record_status")
    if rec_status_pair is not None:
        envelope["recordStatus"] = _coerce_str_identifier(rec_status_pair[1])

    reason_pair = _first_present("reasonForChange", "reason_for_change")
    if reason_pair is not None:
        envelope["reasonForChange"] = _coerce_str_identifier(reason_pair[1])

    form_key_pair = _first_present("formKey", "form_key")
    form_id_pair = _first_present("formId", "form_id")
    if form_key_pair is not None:
        envelope["formKey"] = _coerce_str_identifier(form_key_pair[1])
    elif form_id_pair is not None:
        envelope["formId"] = _coerce_int_identifier(
            form_id_pair[1], row_index=row_index, column_name=form_id_pair[0]
        )
    elif default_form_key:
        envelope["formKey"] = default_form_key
    elif default_form_id is not None:
        envelope["formId"] = default_form_id

    subj_key_pair = _first_present("subjectKey", "subject_key")
    subj_oid_pair = _first_present("subjectOid", "subject_oid")
    subj_id_pair = _first_present("subjectId", "subject_id")
    if subj_key_pair is not None:
        envelope["subjectKey"] = _coerce_str_identifier(subj_key_pair[1])
    if subj_oid_pair is not None:
        envelope["subjectOid"] = _coerce_str_identifier(subj_oid_pair[1])
    if subj_key_pair is None and subj_oid_pair is None and subj_id_pair is not None:
        envelope["subjectId"] = _coerce_int_identifier(
            subj_id_pair[1], row_index=row_index, column_name=subj_id_pair[0]
        )

    site_name_pair = _first_present("siteName", "site_name")
    site_id_pair = _first_present("siteId", "site_id")
    if site_name_pair is not None:
        envelope["siteName"] = _coerce_str_identifier(site_name_pair[1])
    elif site_id_pair is not None:
        envelope["siteId"] = _coerce_int_identifier(
            site_id_pair[1], row_index=row_index, column_name=site_id_pair[0]
        )

    interval_name_pair = _first_present("intervalName", "interval_name")
    interval_id_pair = _first_present("intervalId", "interval_id")
    if interval_name_pair is not None:
        envelope["intervalName"] = _coerce_str_identifier(interval_name_pair[1])
    elif interval_id_pair is not None:
        envelope["intervalId"] = _coerce_int_identifier(
            interval_id_pair[1], row_index=row_index, column_name=interval_id_pair[0]
        )

    raw_data: dict[str, Any] = {}
    var_sources: dict[str, SourceKind] = {}

    for nested_col in ("record_data", "recordData", "data"):
        if nested_col in raw_row and not _is_missing_cell(raw_row[nested_col]):
            parsed_nested = _parse_nested_data_cell(
                raw_row[nested_col], row_index=row_index, column_name=nested_col
            )
            for var_key, var_val in parsed_nested.items():
                if _contains_masked_sentinel(var_val):
                    raise ValidationError(
                        f"Row {row_index}: variable '{var_key}' contains masked PHI sentinel "
                        f"'{MASKED_SENTINEL}' and cannot be imported."
                    )
                if _is_missing_cell(var_val):
                    continue
                key_str = str(var_key)
                raw_data[key_str] = var_val
                var_sources[key_str] = "tabular"

    for col_name, raw_val in raw_row.items():
        if (
            col_name in _READ_ONLY_RECORD_COLUMNS
            or col_name in _IDENTIFIER_COLUMNS
            or col_name in _NESTED_DATA_COLUMNS
            or _is_missing_cell(raw_val)
        ):
            continue
        raw_data[col_name] = raw_val
        var_sources[col_name] = source_kind

    return RawRecordEnvelope(
        envelope=envelope,
        raw_data=raw_data,
        data_key="data",
        source_kind=source_kind,
        var_sources=var_sources,
        study_key_hint=study_hint,
        row_index=row_index,
    )


def _build_mapping_raw_envelope(
    item: Mapping[str, Any],
    *,
    row_index: int,
    source_kind: Literal["json", "mapping"],
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> RawRecordEnvelope:
    """Wrap a JSON object or Python dict into a :class:`RawRecordEnvelope`."""
    ensure_no_masked_sentinel([item])
    copied = dict(item)
    if not (copied.get("formKey") or copied.get("form_key")):
        has_fid = copied.get("formId") is not None or copied.get("form_id") is not None
        if not has_fid:
            if default_form_key:
                copied["formKey"] = default_form_key
            elif default_form_id is not None:
                copied["formId"] = default_form_id

    raw_study = copied.get("studyKey") or copied.get("study_key")
    study_hint = str(raw_study).strip() if raw_study else None

    data_key = ""
    raw_data: dict[str, Any] = {}
    for cand_key in ("data", "recordData"):
        cand_val = copied.get(cand_key)
        if isinstance(cand_val, Mapping):
            data_key = cand_key
            raw_data = {str(k): v for k, v in cand_val.items()}
            break

    envelope = dict(copied)
    return RawRecordEnvelope(
        envelope=envelope,
        raw_data=raw_data,
        data_key=data_key,
        source_kind=source_kind,
        var_sources=dict.fromkeys(raw_data, source_kind),
        study_key_hint=study_hint,
        row_index=row_index,
    )


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    """Read rows from a Parquet file via pyarrow or pandas."""
    try:
        import pyarrow.parquet as pq

        pq_mod: Any = pq
        table = pq_mod.read_table(path)
        rows = table.to_pylist()
        return [dict(r) for r in rows]
    except ImportError:
        pass

    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError(
            "pyarrow or pandas is required to read Parquet files. "
            "Install with \"pip install 'imednet[export]'\"."
        ) from exc

    df = pd.read_parquet(path)
    if df.empty:
        return []
    cols = [str(c) for c in df.columns]
    return [dict(zip(cols, vals, strict=False)) for vals in df.itertuples(index=False, name=None)]


class WriteIngestionPipeline:
    """Two-stage write ingestion pipeline separating raw parsing from Study schema binding."""

    @staticmethod
    def read_csv_stream(
        stream: Any,
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Parse a CSV character stream into raw uncoerced :class:`RawRecordEnvelope` items."""
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            return []
        envelopes: list[RawRecordEnvelope] = []
        for idx, raw_row in enumerate(reader):
            env = _build_tabular_raw_envelope(
                raw_row,
                row_index=idx,
                source_kind="csv",
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
            if env is not None:
                envelopes.append(env)
        return envelopes

    @classmethod
    def read_csv_text(
        cls,
        raw_csv: str,
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Parse CSV text into raw uncoerced :class:`RawRecordEnvelope` items."""
        return cls.read_csv_stream(
            io.StringIO(raw_csv.lstrip("\ufeff")),
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )

    @classmethod
    def read_csv(
        cls,
        path: str | os.PathLike[str],
        *,
        encoding: str = "utf-8-sig",
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Read a ``.csv`` file into raw uncoerced :class:`RawRecordEnvelope` items."""
        csv_path = Path(path)
        with csv_path.open("r", encoding=encoding, newline="") as handle:
            return cls.read_csv_stream(
                handle,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )

    @staticmethod
    def read_json_text(
        raw_text: str,
        *,
        source_label: str = "JSON",
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Parse JSON text (object or array of objects) into :class:`RawRecordEnvelope` items."""
        try:
            loaded = json.loads(raw_text.lstrip("\ufeff"))
        except ValueError as exc:
            raise ValueError(f"Invalid JSON in {source_label}: {exc}") from exc

        if isinstance(loaded, dict):
            if ("items" in loaded and ("schemaVersion" in loaded or "resource" in loaded)) or (
                isinstance(loaded.get("data"), list)
                and not any(k in loaded for k in ("formKey", "form_key", "formId", "form_id"))
            ):
                raise ValueError(
                    f"JSON payload in {source_label} is a list response envelope, "
                    "not a Record write payload or array of Record payloads."
                )
            items = [loaded]
        elif isinstance(loaded, list) and all(isinstance(x, dict) for x in loaded):
            items = list(loaded)
        else:
            raise ValueError(
                f"JSON payload in {source_label} must be a JSON object or array of objects."
            )

        return [
            _build_mapping_raw_envelope(
                item,
                row_index=idx,
                source_kind="json",
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
            for idx, item in enumerate(items)
        ]

    @classmethod
    def read_json(
        cls,
        path: str | os.PathLike[str],
        *,
        encoding: str = "utf-8",
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Read a ``.json`` file into raw uncoerced :class:`RawRecordEnvelope` items."""
        json_path = Path(path)
        return cls.read_json_text(
            json_path.read_text(encoding=encoding),
            source_label=str(json_path),
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )

    @staticmethod
    def read_parquet(
        path: str | os.PathLike[str],
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Read a ``.parquet`` file into raw uncoerced :class:`RawRecordEnvelope` items."""
        raw_rows = _read_parquet_rows(Path(path))
        envelopes: list[RawRecordEnvelope] = []
        for idx, raw_row in enumerate(raw_rows):
            env = _build_tabular_raw_envelope(
                raw_row,
                row_index=idx,
                source_kind="tabular",
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
            if env is not None:
                envelopes.append(env)
        return envelopes

    @staticmethod
    def read_dataframe(
        df: Any,
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Read a ``pandas.DataFrame`` into raw uncoerced :class:`RawRecordEnvelope` items."""
        if getattr(df, "empty", False):
            return []
        columns = [str(col) for col in df.columns]
        envelopes: list[RawRecordEnvelope] = []
        for idx, values in enumerate(df.itertuples(index=False, name=None)):
            raw_row = dict(zip(columns, values, strict=False))
            env = _build_tabular_raw_envelope(
                raw_row,
                row_index=idx,
                source_kind="tabular",
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
            if env is not None:
                envelopes.append(env)
        return envelopes

    @staticmethod
    def read_mappings(
        items: Sequence[Any],
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Read a sequence of dicts or Pydantic models into :class:`RawRecordEnvelope` items."""
        envelopes: list[RawRecordEnvelope] = []
        for idx, item in enumerate(items):
            if isinstance(item, RawRecordEnvelope):
                ensure_no_masked_sentinel([item.envelope, item.raw_data])
                envelopes.append(item)
                continue
            if hasattr(item, "model_dump") and callable(item.model_dump):
                dumped = item.model_dump(by_alias=True, exclude_none=True)
                envelopes.append(
                    _build_mapping_raw_envelope(
                        dumped,
                        row_index=idx,
                        source_kind="mapping",
                        default_form_key=default_form_key,
                        default_form_id=default_form_id,
                    )
                )
                continue
            if not isinstance(item, Mapping):
                raise TypeError("All items in records_data sequence must be mappings.")
            envelopes.append(
                _build_mapping_raw_envelope(
                    item,
                    row_index=idx,
                    source_kind="mapping",
                    default_form_key=default_form_key,
                    default_form_id=default_form_id,
                )
            )
        return envelopes

    @classmethod
    def read_stdin_text(
        cls,
        raw_text: str,
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Auto-detect and parse piped JSON or CSV text into :class:`RawRecordEnvelope` items."""
        cleaned = raw_text.lstrip("\ufeff")
        stripped = cleaned.lstrip()
        if not stripped:
            raise ValueError("Empty input received from stdin ('-').")
        if stripped[0] in {"{", "["}:
            return cls.read_json_text(
                cleaned,
                source_label="stdin",
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
        if "\n" not in stripped.rstrip():
            try:
                json.loads(stripped)
            except ValueError:
                pass
            else:
                raise ValueError("JSON payload in stdin must be a JSON object or array of objects.")
        return cls.read_csv_text(
            cleaned,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )

    @classmethod
    def read_stdin(
        cls,
        stream: Any = None,
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Read piped JSON or CSV text from ``stream`` (defaults to ``sys.stdin``)."""
        import sys

        active_stream = sys.stdin if stream is None else stream
        raw_text = active_stream.read()
        return cls.read_stdin_text(
            raw_text,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )

    @classmethod
    def read_cli_data_pairs(
        cls,
        pairs: Sequence[str] | None,
        *,
        envelope_overrides: Mapping[str, Any] | None = None,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Build a single :class:`RawRecordEnvelope` from repeatable CLI ``-d KEY=VALUE`` pairs."""
        base_env: JsonDict = {}
        if envelope_overrides:
            ensure_no_masked_sentinel([envelope_overrides])
            base_env.update(envelope_overrides)
        fk, fid = _extract_form_ref(base_env)
        if not fk and fid is None:
            if default_form_key and default_form_key.strip():
                base_env["formKey"] = default_form_key.strip()
            elif default_form_id is not None:
                base_env["formId"] = default_form_id
        initial = RawRecordEnvelope(
            envelope=base_env,
            raw_data={},
            data_key="data",
            source_kind="cli",
        )
        return [cls.merge_cli_pairs_into_envelope(initial, pairs)]

    @classmethod
    def read_path(
        cls,
        path: str | os.PathLike[str],
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Route ``.csv``, ``.json``, and ``.parquet`` file paths to the appropriate raw reader."""
        file_path = Path(path)
        suffix = file_path.suffix.lower()
        if suffix == ".json":
            return cls.read_json(
                file_path,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
        if suffix in {".parquet", ".pq"}:
            return cls.read_parquet(
                file_path,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
        return cls.read_csv(
            file_path,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )

    @classmethod
    def read_input(
        cls,
        records_data: Any,
        *,
        default_form_key: str | None = None,
        default_form_id: int | None = None,
    ) -> list[RawRecordEnvelope]:
        """Ingest any supported ``RecordsInput`` into raw uncoerced :class:`RawRecordEnvelope` items."""
        if isinstance(records_data, (str, os.PathLike)):
            return cls.read_path(
                records_data,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
        if _is_dataframe_instance(records_data):
            return cls.read_dataframe(
                records_data,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
        if isinstance(records_data, Sequence) and not isinstance(records_data, (str, bytes)):
            return cls.read_mappings(
                records_data,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
        raise TypeError(
            "records_data must be a list of dicts, pandas.DataFrame, or CSV/JSON/Parquet path; "
            f"got {type(records_data).__name__}."
        )

    @staticmethod
    def parse_raw_cli_pairs(
        pairs: Sequence[str] | None,
    ) -> tuple[dict[str, str], set[str]]:
        """Parse repeatable ``-d KEY=VALUE`` flags into raw strings and quoted variable names."""
        if not pairs:
            return {}, set()
        raw_map: dict[str, str] = {}
        quoted: set[str] = set()
        for item in pairs:
            if "=" not in item:
                raise ValueError(f"Invalid --data entry '{item}': expected KEY=VALUE format.")
            raw_key, _, raw_val = item.partition("=")
            key = raw_key.strip()
            if not key:
                raise ValueError(f"Invalid --data entry '{item}': variable name cannot be empty.")
            if _contains_masked_sentinel(raw_val):
                raise ValidationError(
                    f"Variable '{key}' contains masked PHI sentinel '{MASKED_SENTINEL}' and cannot be submitted."
                )
            unquoted = _unquote_double_quoted_literal(raw_val)
            if unquoted is not None:
                if _contains_masked_sentinel(unquoted):
                    raise ValidationError(
                        f"Variable '{key}' contains masked PHI sentinel '{MASKED_SENTINEL}' and cannot be submitted."
                    )
                raw_map[key] = unquoted
                quoted.add(key)
            else:
                raw_map[key] = raw_val
                quoted.discard(key)
        return raw_map, quoted

    @classmethod
    def merge_cli_pairs_into_envelope(
        cls,
        envelope: RawRecordEnvelope,
        pairs: Sequence[str] | None,
    ) -> RawRecordEnvelope:
        """Merge raw ``-d KEY=VALUE`` assignments into ``envelope`` prior to schema binding."""
        raw_cli_map, cli_quoted = cls.parse_raw_cli_pairs(pairs)
        if not raw_cli_map:
            return envelope
        merged_raw = dict(envelope.raw_data)
        merged_sources = dict(envelope.var_sources)
        merged_quoted = set(envelope.quoted_vars)
        for key, raw_val in raw_cli_map.items():
            merged_raw[key] = raw_val
            merged_sources[key] = "cli"
            if key in cli_quoted:
                merged_quoted.add(key)
            else:
                merged_quoted.discard(key)
        return RawRecordEnvelope(
            envelope=dict(envelope.envelope),
            raw_data=merged_raw,
            data_key=envelope.data_key or "data",
            source_kind=envelope.source_kind,
            var_sources=merged_sources,
            quoted_vars=frozenset(merged_quoted),
            study_key_hint=envelope.study_key_hint,
            row_index=envelope.row_index,
        )

    @staticmethod
    def collect_referenced_forms(
        envelopes: Sequence[RawRecordEnvelope],
    ) -> tuple[list[str], list[Any]]:
        """Collect distinct ``formKey`` and ``formId`` values across ``envelopes`` in order."""
        seen_keys: set[str] = set()
        form_keys: list[str] = []
        seen_ids: set[Any] = set()
        form_ids: list[Any] = []
        for env in envelopes:
            fk = env.form_key
            fid = env.form_id
            if fk:
                if fk not in seen_keys:
                    seen_keys.add(fk)
                    form_keys.append(fk)
                continue
            if fid is not None:
                if isinstance(fid, bool):
                    raise ValidationError(f"Unknown form ID {fid}")
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    form_ids.append(fid)
        return form_keys, form_ids

    @classmethod
    def needs_schema_refresh(
        cls,
        schema: ValidationSchemaProtocol | None,
        envelopes: Sequence[RawRecordEnvelope],
        *,
        study_key: str | None = None,
        allow_formless_cli_fallback: bool = False,
    ) -> bool:
        """Return True if ``schema`` is a ``BaseSchemaCache`` missing referenced Study forms."""
        if schema is None or not envelopes:
            return False
        from imednet.validation.cache import BaseSchemaCache

        if not isinstance(schema, BaseSchemaCache):
            return False

        form_keys, form_ids = cls.collect_referenced_forms(envelopes)
        if any(not schema._has_form(fk, study_key=study_key) for fk in form_keys):
            return True
        for fid in form_ids:
            resolved_fk = schema.form_key_from_id(fid, study_key=study_key)
            if not resolved_fk or not schema._has_form(resolved_fk, study_key=study_key):
                return True
        if not form_keys and not form_ids:
            needs_formless = any(
                bool(env.raw_data)
                and (
                    allow_formless_cli_fallback
                    or env.source_kind in {"csv", "tabular", "cli"}
                    or "cli" in env.var_sources.values()
                )
                for env in envelopes
            )
            if needs_formless:
                refreshed: set[str] = getattr(schema, "_refreshed_studies", set())
                if (
                    study_key
                    and study_key not in refreshed
                    and not schema.forms_for_study(study_key)
                ):
                    return True
        return False

    @classmethod
    def bind(
        cls,
        envelopes: Sequence[RawRecordEnvelope],
        *,
        schema: ValidationSchemaProtocol | None = None,
        study_key: str | None = None,
        refresh_sync: Callable[[str], None] | None = None,
        allow_formless_cli_fallback: bool = False,
    ) -> list[JsonDict]:
        """Refresh cold Study schema if needed, then bind all raw envelopes into JSON dicts."""
        if (
            study_key
            and refresh_sync is not None
            and cls.needs_schema_refresh(
                schema,
                envelopes,
                study_key=study_key,
                allow_formless_cli_fallback=allow_formless_cli_fallback,
            )
        ):
            refresh_sync(study_key)
        return cls._bind_ready_envelopes(
            envelopes,
            schema=schema,
            study_key=study_key,
            allow_formless_cli_fallback=allow_formless_cli_fallback,
        )

    @staticmethod
    def bind_async(
        envelopes: Sequence[RawRecordEnvelope],
        *,
        schema: ValidationSchemaProtocol | None = None,
        study_key: str | None = None,
        refresh_async: Callable[[str], Awaitable[None]] | None = None,
        allow_formless_cli_fallback: bool = False,
    ) -> Awaitable[list[JsonDict]]:
        """Asynchronously refresh cold Study schema if needed, then bind all raw envelopes."""

        async def _run() -> list[JsonDict]:
            if (
                study_key
                and refresh_async is not None
                and WriteIngestionPipeline.needs_schema_refresh(
                    schema,
                    envelopes,
                    study_key=study_key,
                    allow_formless_cli_fallback=allow_formless_cli_fallback,
                )
            ):
                await refresh_async(study_key)
            return WriteIngestionPipeline._bind_ready_envelopes(
                envelopes,
                schema=schema,
                study_key=study_key,
                allow_formless_cli_fallback=allow_formless_cli_fallback,
            )

        return _run()

    @staticmethod
    def _bind_ready_envelopes(
        envelopes: Sequence[RawRecordEnvelope],
        *,
        schema: ValidationSchemaProtocol | None,
        study_key: str | None,
        allow_formless_cli_fallback: bool,
    ) -> list[JsonDict]:
        bound_records: list[JsonDict] = []
        for env in envelopes:
            ensure_no_masked_sentinel([env.envelope])
            eff_study_key = study_key or env.study_key_hint
            fk = env.form_key
            fid = env.form_id
            record_out = dict(env.envelope)

            if not env.data_key and not env.raw_data:
                bound_records.append(record_out)
                continue

            bound_data: JsonDict = {}
            for var_name, raw_val in env.raw_data.items():
                if _contains_masked_sentinel(raw_val):
                    raise ValidationError(
                        f"Row {env.row_index}: variable '{var_name}' contains masked PHI sentinel "
                        f"'{MASKED_SENTINEL}' and cannot be submitted."
                    )
                v_source = env.var_sources.get(var_name, env.source_kind)
                is_quoted = var_name in env.quoted_vars
                vtype, var_obj = _resolve_variable_definition(
                    schema,
                    form_key=fk,
                    form_id=fid,
                    var_name=var_name,
                    study_key=eff_study_key,
                    allow_formless_fallback=allow_formless_cli_fallback or v_source == "cli",
                )
                bound_data[var_name] = _bind_scalar_value(
                    raw_val,
                    source_kind=v_source,
                    is_quoted=is_quoted,
                    expected_var_type=vtype,
                    var_obj=var_obj,
                    row_index=env.row_index,
                    var_name=var_name,
                )

            target_data_key = env.data_key or "data"
            record_out[target_data_key] = bound_data
            bound_records.append(record_out)

        return bound_records


def compute_chunk_sha256(study_key: str, chunk: Sequence[Mapping[str, Any]]) -> str:
    """Compute a deterministic SHA-256 hex digest for one normalized chunk."""
    canonical = json.dumps(
        {"study_key": study_key, "chunk": list(chunk)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_sequence_sha256(
    study_key: str,
    chunk_size: int,
    chunk_digests: Sequence[str],
) -> str:
    """Compute a deterministic SHA-256 hex digest over ``(study_key, chunk_size, chunk_digests)``."""
    canonical = json.dumps(
        {
            "study_key": study_key,
            "chunk_size": chunk_size,
            "chunk_digests": list(chunk_digests),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "MASKED_SENTINEL",
    "RawRecordEnvelope",
    "SourceKind",
    "WriteIngestionPipeline",
    "compute_chunk_sha256",
    "compute_sequence_sha256",
    "ensure_no_masked_sentinel",
]
