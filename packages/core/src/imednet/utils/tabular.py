"""Standard-library CSV and tabular record normalization utilities."""

from __future__ import annotations

import ast
import csv
import datetime
import io
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from imednet.core.context import get_study_context
from imednet.errors import ValidationError
from imednet.utils.typing import JsonDict, RecordsInput

if TYPE_CHECKING:
    from imednet.validation.cache import ValidationSchemaProtocol

MASKED_SENTINEL: str = "***MASKED***"

_INT_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)$")
_DECIMAL_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)\.\d+$")
_SLASH_SPACE_RE = re.compile(r"\s*/\s*")
_SPACE_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")
_MIDNIGHT_TIME_SUFFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]00:00(?::00(?:\.0+)?)?$")

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

_DATE_ONLY_TYPES: frozenset[str] = frozenset({"date", "date ymd", "date mdy", "date dmy"})

_TEXT_LIKE_TYPES: frozenset[str] = frozenset(
    {
        "string",
        "text",
        "alphanumeric",
        "memo",
        "textarea",
        "text area",
        "notes",
        "note",
        "date",
        "date ymd",
        "date mdy",
        "date dmy",
        "datetime",
        "date/time",
        "date time",
        "durationdatetime",
        "intervaldatetime",
        "date/time precision",
        "datetime precision",
        "date time precision",
        "partialdate",
        "incompletedate",
        "partialdatetime",
        "incompletedatetime",
        "partialtime",
        "incompletetime",
        "time",
        "uri",
        "file",
        "upload",
        "fileupload",
        "file upload",
        "signature",
        "descriptive",
        "label",
    }
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
_MULTI_CHOICE_TYPES: frozenset[str] = frozenset(
    {"checkbox", "check box", "multi select", "multiselect"}
)
_CHOICE_TYPES: frozenset[str] = _SINGLE_CHOICE_TYPES | _MULTI_CHOICE_TYPES

_INT_LIKE_TYPES: frozenset[str] = frozenset({"int", "integer", "number"})
_FLOAT_LIKE_TYPES: frozenset[str] = frozenset({"float", "decimal", "double", "numeric"})
_NUMERIC_LIKE_TYPES: frozenset[str] = _INT_LIKE_TYPES | _FLOAT_LIKE_TYPES
_BOOL_LIKE_TYPES: frozenset[str] = frozenset({"bool", "boolean"})
_COMPUTED_FLEX_TYPES: frozenset[str] = frozenset({"calculation", "calc", "calculated", "slider"})


def _unwrap_scalar(value: Any) -> Any:
    """Unwrap NumPy/Pandas scalar wrappers into native Python primitives."""
    if value is None:
        return None
    cls_name = type(value).__name__
    if cls_name in {"NAType", "NaTType"}:
        return value
    if cls_name == "datetime64":
        return value
    if isinstance(
        value,
        (
            str,
            bytes,
            int,
            float,
            bool,
            Decimal,
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
    if isinstance(unwrapped, Decimal) and unwrapped.is_nan():
        return True
    if isinstance(unwrapped, str):
        return unwrapped.strip() == ""
    cls_name = type(unwrapped).__name__
    if cls_name in {"NAType", "NaTType"}:
        return True
    return cls_name == "datetime64" and str(unwrapped) == "NaT"


def _coerce_str_identifier(value: Any) -> str:
    """Convert an identifier cell to a clean string, coercing integer floats."""
    unwrapped = _unwrap_scalar(value)
    if isinstance(unwrapped, float) and math.isfinite(unwrapped) and unwrapped.is_integer():
        return str(int(unwrapped))
    if isinstance(unwrapped, Decimal) and unwrapped.is_finite() and unwrapped == int(unwrapped):
        return str(int(unwrapped))
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
    if isinstance(unwrapped, Decimal) and unwrapped.is_finite() and unwrapped == int(unwrapped):
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


def _lookup_form_key_from_id(
    schema: ValidationSchemaProtocol,
    form_id: int,
    *,
    study_key: str | None = None,
) -> str | None:
    """Resolve ``formId`` to ``formKey`` while forwarding ``study_key`` when supported."""
    eff_study = study_key or get_study_context()
    if eff_study is not None:
        try:
            return schema.form_key_from_id(form_id, study_key=eff_study)  # type: ignore[call-arg]
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
    eff_study = study_key or get_study_context()
    if eff_study is not None:
        try:
            return schema.variables_for_form(form_key, study_key=eff_study)  # type: ignore[call-arg]
        except TypeError:
            pass
    return schema.variables_for_form(form_key)


def _normalize_var_type_name(raw_type: Any) -> str | None:
    if not isinstance(raw_type, str) or not raw_type.strip():
        return None
    cleaned = re.sub(r"[\s_-]+", " ", raw_type.strip().lower())
    if "/" in cleaned:
        cleaned = _SLASH_SPACE_RE.sub("/", cleaned)
    return cleaned


def _extract_var_type_from_obj(var_obj: Any) -> str | None:
    raw_type = getattr(var_obj, "variable_type", None) or getattr(var_obj, "variableType", None)
    norm = _normalize_var_type_name(raw_type)
    extra = getattr(var_obj, "extra_attributes", None) or getattr(var_obj, "extraAttributes", None)
    if isinstance(extra, Mapping):
        odm_type = _normalize_var_type_name(extra.get("odmDataType") or extra.get("odm_data_type"))
        if (
            odm_type
            and (norm is None or norm in {"text", "string"})
            and odm_type not in {"text", "string"}
        ):
            return odm_type
    return norm


def _extract_choices_from_obj(var_obj: Any) -> tuple[str, ...] | None:
    raw_choices = getattr(var_obj, "choices", None)
    if raw_choices is None:
        extra = getattr(var_obj, "extra_attributes", None) or getattr(
            var_obj, "extraAttributes", None
        )
        if isinstance(extra, Mapping):
            raw_choices = extra.get("choices") or extra.get("allowedValues")
    if not isinstance(raw_choices, Sequence) or isinstance(raw_choices, (str, bytes)):
        return None
    extracted: list[str] = []
    for item in raw_choices:
        if isinstance(item, str):
            extracted.append(item)
        elif isinstance(item, Mapping):
            cval = item.get("choiceValue")
            if cval is None:
                cval = item.get("choice_value", item.get("Choice Value", item.get("value")))
            if cval is not None:
                extracted.append(str(cval))
        else:
            cval = getattr(item, "choice_value", None)
            if cval is None:
                cval = getattr(item, "choiceValue", None)
            if cval is not None:
                extracted.append(str(cval))
    return tuple(extracted) if extracted else None


def _candidate_form_keys_for_schema(
    schema: ValidationSchemaProtocol,
    *,
    study_key: str | None = None,
) -> list[str]:
    eff_study = study_key or get_study_context()
    forms_for_study = getattr(schema, "forms_for_study", None)
    if callable(forms_for_study) and eff_study:
        scoped = forms_for_study(eff_study)
        if isinstance(scoped, Mapping):
            return list(scoped.keys())
    forms_attr = getattr(schema, "forms", None)
    if isinstance(forms_attr, Mapping):
        return [str(k) for k in forms_attr]
    if isinstance(forms_attr, Sequence) and not isinstance(forms_attr, (str, bytes)):
        return [
            str(fk)
            for f in forms_attr
            if (fk := (getattr(f, "form_key", None) or getattr(f, "formKey", None)))
        ]
    return []


def _resolve_variable_metadata(
    schema: ValidationSchemaProtocol | None,
    form_key: str | None,
    form_id: int | None,
    var_name: str,
    *,
    study_key: str | None = None,
) -> tuple[str | None, tuple[str, ...] | None]:
    """Resolve ``(expected_var_type, configured_choices)`` from *schema* when available."""
    if schema is None:
        return None, None
    resolved_fk = form_key
    if not resolved_fk and form_id is not None:
        resolved_fk = _lookup_form_key_from_id(schema, form_id, study_key=study_key)
    if resolved_fk:
        variables = _lookup_variables_for_form(schema, resolved_fk, study_key=study_key)
        if not variables or var_name not in variables:
            return None, None
        var_obj = variables[var_name]
        return _extract_var_type_from_obj(var_obj), _extract_choices_from_obj(var_obj)

    matched_types: set[str] = set()
    matched_choices: set[tuple[str, ...] | None] = set()
    for cand_fk in _candidate_form_keys_for_schema(schema, study_key=study_key):
        variables = _lookup_variables_for_form(schema, cand_fk, study_key=study_key)
        if variables and var_name in variables:
            var_obj = variables[var_name]
            vtype = _extract_var_type_from_obj(var_obj)
            if vtype is not None:
                matched_types.add(vtype)
            matched_choices.add(_extract_choices_from_obj(var_obj))
    resolved_type = next(iter(matched_types)) if len(matched_types) == 1 else None
    resolved_choices = next(iter(matched_choices)) if len(matched_choices) == 1 else None
    return resolved_type, resolved_choices


def _resolve_expected_var_type(
    schema: ValidationSchemaProtocol | None,
    form_key: str | None,
    form_id: int | None,
    var_name: str,
    *,
    study_key: str | None = None,
) -> str | None:
    """Look up normalized variable type from *schema* when available."""
    vtype, _ = _resolve_variable_metadata(
        schema,
        form_key,
        form_id,
        var_name,
        study_key=study_key,
    )
    return vtype


def _to_finite_decimal(raw: Any) -> Decimal | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw if raw.is_finite() else None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw)) if math.isfinite(raw) else None
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            dec = Decimal(stripped)
        except InvalidOperation:
            return None
        return dec if dec.is_finite() else None
    return None


def _match_choice_code(value: Any, choices: Sequence[str] | None) -> str | None:
    """Match a numeric or string input to a configured choice code when unambiguous."""
    if not choices:
        return None
    if isinstance(value, str):
        if value in choices:
            return value
        stripped = value.strip()
        if stripped in choices:
            return stripped
        # Match numeric string literals (e.g. CLI -d CODE=1 against '01' or '1.0')
        if not (_INT_LITERAL_RE.match(stripped) or _DECIMAL_LITERAL_RE.match(stripped)):
            return None
        target_dec = _to_finite_decimal(stripped)
    else:
        if isinstance(value, bool):
            return None
        if isinstance(value, Decimal):
            if not value.is_finite():
                return None
            exact_str = str(value)
            if exact_str in choices:
                return exact_str
            target_dec = value
        elif isinstance(value, int):
            exact_str = str(value)
            if exact_str in choices:
                return exact_str
            target_dec = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                return None
            exact_str = str(value)
            if exact_str in choices:
                return exact_str
            if value.is_integer():
                int_str = str(int(value))
                if int_str in choices:
                    return int_str
            target_dec = Decimal(exact_str)
        else:
            return None

    if target_dec is None:
        return None
    numeric_matches = [c for c in choices if _to_finite_decimal(c) == target_dec]
    if len(numeric_matches) == 1:
        return numeric_matches[0]
    return None


def _format_temporal_iso(unwrapped: Any, *, expected_var_type: str | None) -> str | None:
    cls_name = type(unwrapped).__name__
    if cls_name == "datetime64":
        text_dt = str(unwrapped)
        if text_dt == "NaT":
            return None
        iso_dt = _SPACE_DATETIME_RE.sub(r"\1T\2", text_dt)
        if expected_var_type in _DATE_ONLY_TYPES:
            m_mid = _MIDNIGHT_TIME_SUFFIX_RE.match(iso_dt)
            if m_mid is not None:
                return m_mid.group(1)
        return iso_dt
    if isinstance(unwrapped, datetime.datetime):
        if (
            expected_var_type in _DATE_ONLY_TYPES
            and unwrapped.tzinfo is None
            and unwrapped.hour == 0
            and unwrapped.minute == 0
            and unwrapped.second == 0
            and unwrapped.microsecond == 0
            and getattr(unwrapped, "nanosecond", 0) == 0
        ):
            return unwrapped.date().isoformat()
        return unwrapped.isoformat()
    if isinstance(unwrapped, (datetime.date, datetime.time)):
        return unwrapped.isoformat()
    return None


def _coerce_choice_item(
    item: Any,
    *,
    expected_var_type: str,
    choices: Sequence[str] | None,
    from_tabular_cell: bool,
) -> Any:
    unwrapped = _unwrap_scalar(item)
    if isinstance(unwrapped, str):
        matched_str = _match_choice_code(unwrapped, choices)
        return matched_str if matched_str is not None else unwrapped
    if isinstance(unwrapped, bool):
        bool_token = "true" if unwrapped else "false"
        if choices:
            ci_matches = [c for c in choices if c.strip().lower() == bool_token]
            if len(ci_matches) == 1:
                return ci_matches[0]
        if expected_var_type in {"checkbox", "check box"}:
            return unwrapped
        return bool_token
    matched = _match_choice_code(unwrapped, choices)
    if matched is not None:
        return matched
    if isinstance(unwrapped, Decimal):
        if not unwrapped.is_finite():
            return float(unwrapped)
        if unwrapped == int(unwrapped) and int(unwrapped.as_tuple().exponent) >= 0:
            return str(int(unwrapped))
        return str(unwrapped)
    if isinstance(unwrapped, float):
        if math.isfinite(unwrapped):
            return str(int(unwrapped)) if unwrapped.is_integer() else str(unwrapped)
        return unwrapped
    if isinstance(unwrapped, int):
        if expected_var_type in _MULTI_CHOICE_TYPES and from_tabular_cell:
            return str(unwrapped)
        return unwrapped
    return unwrapped


def _coerce_clinical_value(
    value: Any,
    *,
    from_csv_str: bool,
    expected_var_type: str | None = None,
    choices: Sequence[str] | None = None,
    from_tabular_cell: bool = True,
) -> Any:
    """Coerce integer floats, timestamps, Decimals, and typed CSV/choice scalars cleanly."""
    unwrapped = _unwrap_scalar(value)
    if unwrapped is None:
        return None

    cls_name = type(unwrapped).__name__
    if cls_name in {"NAType", "NaTType"}:
        return None
    if cls_name == "datetime64" or isinstance(
        unwrapped, (datetime.datetime, datetime.date, datetime.time)
    ):
        return _format_temporal_iso(unwrapped, expected_var_type=expected_var_type)

    if isinstance(unwrapped, bool):
        if expected_var_type in _CHOICE_TYPES:
            return _coerce_choice_item(
                unwrapped,
                expected_var_type=expected_var_type,
                choices=choices,
                from_tabular_cell=from_tabular_cell,
            )
        if expected_var_type in _TEXT_LIKE_TYPES:
            return "true" if unwrapped else "false"
        return unwrapped

    if expected_var_type is not None:
        if expected_var_type in _BOOL_LIKE_TYPES:
            if isinstance(unwrapped, str):
                low = unwrapped.strip().lower()
                if low in {"true", "false"}:
                    return low == "true"
            return unwrapped

        if expected_var_type in _NUMERIC_LIKE_TYPES:
            if isinstance(unwrapped, int):
                return unwrapped
            if isinstance(unwrapped, Decimal):
                if not unwrapped.is_finite():
                    return float(unwrapped)
                if expected_var_type in _INT_LIKE_TYPES and unwrapped == int(unwrapped):
                    return int(unwrapped)
                return float(unwrapped)
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
                if _INT_LITERAL_RE.match(stripped):
                    return int(stripped)
                if _DECIMAL_LITERAL_RE.match(stripped):
                    fval = float(stripped)
                    if math.isfinite(fval):
                        if expected_var_type in _INT_LIKE_TYPES and fval.is_integer():
                            return int(fval)
                        return fval
            return unwrapped

        if expected_var_type in _CHOICE_TYPES:
            if isinstance(unwrapped, (list, tuple, set, frozenset)):
                return [
                    _coerce_choice_item(
                        elem,
                        expected_var_type=expected_var_type,
                        choices=choices,
                        from_tabular_cell=False,
                    )
                    for elem in unwrapped
                ]
            if isinstance(unwrapped, int) and expected_var_type in _MULTI_CHOICE_TYPES:
                matched_multi = _match_choice_code(unwrapped, choices)
                return matched_multi if matched_multi is not None else str(unwrapped)
            return _coerce_choice_item(
                unwrapped,
                expected_var_type=expected_var_type,
                choices=choices,
                from_tabular_cell=from_tabular_cell,
            )

        if expected_var_type in _COMPUTED_FLEX_TYPES:
            if isinstance(unwrapped, Decimal):
                if not unwrapped.is_finite():
                    return float(unwrapped)
                if unwrapped == int(unwrapped) and int(unwrapped.as_tuple().exponent) >= 0:
                    return int(unwrapped)
                return float(unwrapped)
            return unwrapped

        if isinstance(unwrapped, str):
            return unwrapped
        if isinstance(unwrapped, Decimal):
            if not unwrapped.is_finite():
                return float(unwrapped)
            return str(unwrapped)
        if isinstance(unwrapped, float) and math.isfinite(unwrapped):
            return str(int(unwrapped)) if unwrapped.is_integer() else str(unwrapped)
        if expected_var_type in _TEXT_LIKE_TYPES:
            return str(unwrapped)
        return unwrapped

    if isinstance(unwrapped, int):
        return unwrapped

    if isinstance(unwrapped, Decimal):
        if not unwrapped.is_finite():
            return float(unwrapped)
        if unwrapped == int(unwrapped):
            return int(unwrapped)
        return float(unwrapped)

    if isinstance(unwrapped, float):
        if math.isfinite(unwrapped) and unwrapped.is_integer():
            return int(unwrapped)
        return unwrapped

    if isinstance(unwrapped, (list, tuple)):
        return [
            _coerce_clinical_value(
                elem,
                from_csv_str=False,
                expected_var_type=None,
                from_tabular_cell=from_tabular_cell,
            )
            for elem in unwrapped
        ]

    if from_csv_str and isinstance(unwrapped, str):
        return unwrapped

    return unwrapped


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


def normalize_tabular_row(
    raw_row: Mapping[str, Any],
    *,
    row_index: int = 0,
    from_csv_str: bool = False,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> JsonDict | None:
    """Normalize a single DataFrame or CSV row into a REST create-records payload dict.

    Returns ``None`` if every cell in the row is empty.
    """
    # 1. Reject masked PHI sentinels across all columns before any stripping or coercion
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

    eff_study_key = study_key
    if not eff_study_key:
        study_pair = _first_present("studyKey", "study_key")
        if study_pair is not None:
            eff_study_key = _coerce_str_identifier(study_pair[1])

    envelope: JsonDict = {}

    # 2. Map identifier and update-envelope columns according to REST create-records contract
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
    resolved_form_key: str | None = None
    resolved_form_id: int | None = None
    if form_key_pair is not None:
        resolved_form_key = _coerce_str_identifier(form_key_pair[1])
        envelope["formKey"] = resolved_form_key
    elif form_id_pair is not None:
        resolved_form_id = _coerce_int_identifier(
            form_id_pair[1], row_index=row_index, column_name=form_id_pair[0]
        )
        envelope["formId"] = resolved_form_id
    elif default_form_key:
        resolved_form_key = default_form_key
        envelope["formKey"] = default_form_key
    elif default_form_id is not None:
        resolved_form_id = default_form_id
        envelope["formId"] = default_form_id

    subj_key_pair = _first_present("subjectKey", "subject_key")
    subj_oid_pair = _first_present("subjectOid", "subject_oid")
    subj_id_pair = _first_present("subjectId", "subject_id")
    if subj_key_pair is not None:
        envelope["subjectKey"] = _coerce_str_identifier(subj_key_pair[1])
    elif subj_oid_pair is not None:
        envelope["subjectOid"] = _coerce_str_identifier(subj_oid_pair[1])
    elif subj_id_pair is not None:
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

    # 3. Assemble clinical data from nested data column(s) + flattened variable columns
    clinical_data: JsonDict = {}
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
                vtype, vchoices = _resolve_variable_metadata(
                    schema,
                    resolved_form_key,
                    resolved_form_id,
                    str(var_key),
                    study_key=eff_study_key,
                )
                clinical_data[str(var_key)] = _coerce_clinical_value(
                    var_val,
                    from_csv_str=False,
                    expected_var_type=vtype,
                    choices=vchoices,
                    from_tabular_cell=True,
                )

    for col_name, raw_val in raw_row.items():
        if (
            col_name in _READ_ONLY_RECORD_COLUMNS
            or col_name in _IDENTIFIER_COLUMNS
            or col_name in _NESTED_DATA_COLUMNS
            or _is_missing_cell(raw_val)
        ):
            continue
        vtype, vchoices = _resolve_variable_metadata(
            schema,
            resolved_form_key,
            resolved_form_id,
            col_name,
            study_key=eff_study_key,
        )
        clinical_data[col_name] = _coerce_clinical_value(
            raw_val,
            from_csv_str=from_csv_str,
            expected_var_type=vtype,
            choices=vchoices,
            from_tabular_cell=True,
        )

    envelope["data"] = clinical_data
    return envelope


def _read_csv_stream_records(
    stream: Any,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    records: list[JsonDict] = []
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        return []
    for idx, raw_row in enumerate(reader):
        normalized = normalize_tabular_row(
            raw_row,
            row_index=idx,
            from_csv_str=True,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )
        if normalized is not None:
            records.append(normalized)
    return records


def csv_text_to_records(
    raw_csv: str,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Parse CSV text into REST create-records payload dicts."""
    cleaned = raw_csv.lstrip("\ufeff")
    return _read_csv_stream_records(
        io.StringIO(cleaned),
        schema=schema,
        study_key=study_key,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )


def csv_to_records(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8-sig",
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Load a CSV file into REST create-records payload dicts using the standard library.

    Works in ``packages/core`` even when ``pandas`` is not installed.
    """
    csv_path = Path(path)
    with csv_path.open("r", encoding=encoding, newline="") as handle:
        return _read_csv_stream_records(
            handle,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )


def _is_dataframe_instance(obj: object) -> bool:
    """Return True if *obj* is a ``pandas.DataFrame`` without requiring ``pandas`` import."""
    cls = type(obj)
    return cls.__name__ == "DataFrame" and cls.__module__.startswith("pandas")


def dataframe_rows_to_records(
    df: Any,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Convert a ``pandas.DataFrame`` into REST create-records payload dicts."""
    if getattr(df, "empty", False):
        return []
    columns = [str(col) for col in df.columns]
    records: list[JsonDict] = []
    for idx, values in enumerate(df.itertuples(index=False, name=None)):
        raw_row = dict(zip(columns, values, strict=False))
        normalized = normalize_tabular_row(
            raw_row,
            row_index=idx,
            from_csv_str=False,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )
        if normalized is not None:
            records.append(normalized)
    return records


def _extract_mapping_form_context(
    rec: Mapping[str, Any],
    *,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> tuple[str | None, int | None]:
    raw_fk = rec.get("formKey") or rec.get("form_key")
    resolved_fk = (
        str(raw_fk).strip() if isinstance(raw_fk, str) and raw_fk.strip() else default_form_key
    )
    raw_fid = rec.get("formId")
    if raw_fid is None:
        raw_fid = rec.get("form_id")
    resolved_fid: int | None = None
    if raw_fid is not None and not isinstance(raw_fid, bool):
        if isinstance(raw_fid, int):
            resolved_fid = raw_fid
        elif isinstance(raw_fid, (float, Decimal)) and float(raw_fid).is_integer():
            resolved_fid = int(raw_fid)
        elif isinstance(raw_fid, str) and _INT_LITERAL_RE.match(raw_fid.strip()):
            resolved_fid = int(raw_fid.strip())
    elif resolved_fk is None and default_form_id is not None:
        resolved_fid = default_form_id
    return resolved_fk, resolved_fid


def normalize_record_mapping(
    raw_rec: Mapping[str, Any],
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
    allow_flat_columns: bool = False,
    row_index: int = 0,
) -> JsonDict:
    """Normalize clinical scalars and choice codes inside a single record mapping without mutating input."""
    has_nested = any(col in raw_rec for col in _NESTED_DATA_COLUMNS)
    if allow_flat_columns and not has_nested:
        non_meta_keys = [
            k
            for k in raw_rec
            if k not in _IDENTIFIER_COLUMNS and k not in _READ_ONLY_RECORD_COLUMNS
        ]
        has_snake_meta = any("_" in k for k in raw_rec if k in _IDENTIFIER_COLUMNS)
        if non_meta_keys or has_snake_meta:
            tab_row = normalize_tabular_row(
                raw_rec,
                row_index=row_index,
                from_csv_str=False,
                schema=schema,
                study_key=study_key,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
            )
            return tab_row if tab_row is not None else {}

    mutated = not isinstance(raw_rec, dict)
    rec: JsonDict = dict(raw_rec)
    eff_study = study_key
    if not eff_study:
        raw_sk = rec.get("studyKey") or rec.get("study_key")
        if isinstance(raw_sk, str) and raw_sk.strip():
            eff_study = raw_sk.strip()

    has_fk = bool(rec.get("formKey") or rec.get("form_key"))
    has_fid = rec.get("formId") is not None or rec.get("form_id") is not None
    if not has_fk and not has_fid:
        if default_form_key:
            rec["formKey"] = default_form_key
            mutated = True
        elif default_form_id is not None:
            rec["formId"] = default_form_id
            mutated = True

    resolved_fk, resolved_fid = _extract_mapping_form_context(
        rec,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )

    for data_col in ("data", "recordData", "record_data"):
        raw_nested = rec.get(data_col)
        if isinstance(raw_nested, Mapping):
            data_mutated = not isinstance(raw_nested, dict)
            coerced_data: JsonDict = {}
            for var_key, var_val in raw_nested.items():
                if not isinstance(var_key, str):
                    data_mutated = True
                vname = str(var_key)
                if var_val is None:
                    coerced_data[vname] = None
                    continue
                vtype, vchoices = _resolve_variable_metadata(
                    schema,
                    resolved_fk,
                    resolved_fid,
                    vname,
                    study_key=eff_study,
                )
                coerced_val = _coerce_clinical_value(
                    var_val,
                    from_csv_str=False,
                    expected_var_type=vtype,
                    choices=vchoices,
                    from_tabular_cell=False,
                )
                if not (
                    coerced_val is var_val
                    or (type(coerced_val) is type(var_val) and coerced_val == var_val)
                ):
                    data_mutated = True
                coerced_data[vname] = coerced_val
            if data_mutated:
                rec[data_col] = coerced_data
                mutated = True
    if not mutated and isinstance(raw_rec, dict):
        return raw_rec
    return rec


def json_to_records(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8-sig",
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Load a JSON object or array of record payloads (REST or tabular) from *path*."""
    json_path = Path(path)
    raw_text = json_path.read_text(encoding=encoding)
    try:
        loaded = json.loads(raw_text)
    except ValueError as exc:
        raise ValidationError(f"Invalid JSON in '{json_path}': {exc}") from exc

    if isinstance(loaded, dict):
        if ("items" in loaded and ("schemaVersion" in loaded or "resource" in loaded)) or (
            isinstance(loaded.get("data"), list)
            and not any(k in loaded for k in ("formKey", "form_key", "formId", "form_id"))
        ):
            raise ValidationError(
                f"JSON payload in '{json_path}' is a list response envelope, "
                "not a Record write payload or array of Record payloads."
            )
        raw_items: list[Mapping[str, Any]] = [loaded]
    elif isinstance(loaded, list) and all(isinstance(x, Mapping) for x in loaded):
        raw_items = list(loaded)
    else:
        raise ValidationError(
            f"JSON payload in '{json_path}' must be a JSON object or array of objects."
        )

    ensure_no_masked_sentinel(raw_items)
    normalized: list[JsonDict] = []
    for idx, item in enumerate(raw_items):
        rec = normalize_record_mapping(
            item,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
            allow_flat_columns=True,
            row_index=idx,
        )
        if rec:
            normalized.append(rec)
    return normalized


def parquet_to_records(
    path: str | os.PathLike[str],
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Load a Parquet file into REST create-records payload dicts via lazy pandas check."""
    from imednet.utils.pandas import check_pandas

    check_pandas("parquet_to_records")
    import pandas as pd

    df = pd.read_parquet(Path(path))
    return dataframe_rows_to_records(
        df,
        schema=schema,
        study_key=study_key,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )


def load_tabular_file_records(
    path: str | os.PathLike[str],
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Route ``.csv``, ``.json``, and ``.parquet`` files to format-specific readers."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        return json_to_records(
            file_path,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )
    if suffix in {".parquet", ".pq"}:
        return parquet_to_records(
            file_path,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )
    return csv_to_records(
        file_path,
        schema=schema,
        study_key=study_key,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )


def _collect_mapping_form_refs(
    items: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[Any]]:
    seen_keys: set[str] = set()
    explicit_form_keys: list[str] = []
    seen_ids: set[Any] = set()
    form_ids: list[Any] = []
    for rec in items:
        raw_fk = rec.get("formKey") or rec.get("form_key")
        fk = str(raw_fk).strip() if raw_fk is not None and not _is_missing_cell(raw_fk) else ""
        if fk:
            if fk not in seen_keys:
                seen_keys.add(fk)
                explicit_form_keys.append(fk)
            continue
        raw_fid = rec.get("formId") if rec.get("formId") is not None else rec.get("form_id")
        unwrapped_fid = _unwrap_scalar(raw_fid)
        if unwrapped_fid is not None and not _is_missing_cell(unwrapped_fid):
            if isinstance(unwrapped_fid, bool):
                raise ValidationError(f"Unknown form ID {unwrapped_fid}")
            fid_val: Any = unwrapped_fid
            if isinstance(unwrapped_fid, str) and _INT_LITERAL_RE.match(unwrapped_fid.strip()):
                fid_val = int(unwrapped_fid.strip())
            elif (
                isinstance(unwrapped_fid, float)
                and math.isfinite(unwrapped_fid)
                and unwrapped_fid.is_integer()
            ):
                fid_val = int(unwrapped_fid)
            if fid_val not in seen_ids:
                seen_ids.add(fid_val)
                form_ids.append(fid_val)
    return explicit_form_keys, form_ids


def extract_records_input_form_refs(
    records_data: RecordsInput,
) -> tuple[list[str], list[Any]]:
    """Extract distinct ``(form_keys, form_ids)`` referenced by *records_data* before coercion."""
    if isinstance(records_data, (str, os.PathLike)):
        file_path = Path(records_data)
        suffix = file_path.suffix.lower()
        if suffix == ".json":
            try:
                loaded = json.loads(file_path.read_text(encoding="utf-8-sig"))
            except ValueError:
                return [], []
            if isinstance(loaded, Mapping):
                return _collect_mapping_form_refs([loaded])
            if isinstance(loaded, list):
                return _collect_mapping_form_refs([x for x in loaded if isinstance(x, Mapping)])
            return [], []
        if suffix in {".parquet", ".pq"}:
            from imednet.utils.pandas import check_pandas

            check_pandas("parquet_to_records")
            import pandas as pd

            df = pd.read_parquet(file_path)
            return extract_records_input_form_refs(df)
        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [row for row in reader if isinstance(row, Mapping)]
        return _collect_mapping_form_refs(rows)

    if _is_dataframe_instance(records_data):
        df_any: Any = records_data
        if getattr(df_any, "empty", False):
            return [], []
        cols = [str(c) for c in df_any.columns]
        form_cols = [c for c in cols if c in {"formKey", "form_key", "formId", "form_id"}]
        if not form_cols:
            return [], []
        sub_df = df_any[form_cols]
        row_dicts = [
            dict(zip(form_cols, vals, strict=False))
            for vals in sub_df.itertuples(index=False, name=None)
        ]
        return _collect_mapping_form_refs(row_dicts)

    if isinstance(records_data, Sequence) and not isinstance(records_data, (str, bytes)):
        mappings: list[Mapping[str, Any]] = []
        for item in records_data:
            if isinstance(item, Mapping):
                mappings.append(item)
            elif hasattr(item, "form_key") or hasattr(item, "form_id"):
                mappings.append(
                    {
                        "formKey": getattr(item, "form_key", None),
                        "formId": getattr(item, "form_id", None),
                    }
                )
        return _collect_mapping_form_refs(mappings)

    return [], []


def normalize_records_input(
    records_data: RecordsInput,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[JsonDict]:
    """Normalize ``DataFrame``, CSV/JSON/Parquet path, or ``list[JsonDict]`` into record payload dicts."""
    if isinstance(records_data, (str, os.PathLike)):
        return load_tabular_file_records(
            records_data,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )
    if _is_dataframe_instance(records_data):
        return dataframe_rows_to_records(
            records_data,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
        )
    if isinstance(records_data, Sequence) and not isinstance(records_data, (str, bytes)):
        materialized: list[Mapping[str, Any]] = [
            item for item in records_data if isinstance(item, Mapping)
        ]
        if len(materialized) != len(records_data):
            raise TypeError("All items in records_data sequence must be mappings.")
        ensure_no_masked_sentinel(materialized)
        return [
            normalize_record_mapping(
                item,
                schema=schema,
                study_key=study_key,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
                allow_flat_columns=False,
                row_index=idx,
            )
            for idx, item in enumerate(materialized)
        ]
    raise TypeError(
        "records_data must be a list of dicts, pandas.DataFrame, or CSV/JSON/Parquet path; "
        f"got {type(records_data).__name__}."
    )


__all__ = [
    "MASKED_SENTINEL",
    "csv_text_to_records",
    "csv_to_records",
    "dataframe_rows_to_records",
    "ensure_no_masked_sentinel",
    "extract_records_input_form_refs",
    "json_to_records",
    "load_tabular_file_records",
    "normalize_record_mapping",
    "normalize_records_input",
    "normalize_tabular_row",
    "parquet_to_records",
]
