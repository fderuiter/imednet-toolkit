"""Standard-library CSV and tabular record normalization utilities."""

from __future__ import annotations

import ast
import csv
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import imednet.validation as _validation
from imednet.errors import ValidationError
from imednet.utils.typing import JsonDict, RecordsInput

MASKED_SENTINEL: str = "***MASKED***"

_INT_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)$")
_DECIMAL_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)\.\d+$")

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
        "time",
        "file",
        "upload",
        "fileupload",
        "file upload",
        "signature",
        "descriptive",
        "label",
    }
)

_BOOL_LIKE_TYPES: frozenset[str] = frozenset({"bool", "boolean"})


def _unwrap_scalar(value: Any) -> Any:
    """Unwrap NumPy/Pandas scalar wrappers into native Python primitives."""
    if value is not None and not isinstance(
        value, (str, bytes, int, float, bool, dict, list, tuple, set, frozenset)
    ):
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
    return cls_name in {"NAType", "NaTType"}


def _coerce_str_identifier(value: Any) -> str:
    """Convert an identifier cell to a clean string, coercing integer floats."""
    unwrapped = _unwrap_scalar(value)
    if isinstance(unwrapped, float) and math.isfinite(unwrapped) and unwrapped.is_integer():
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
    if isinstance(unwrapped, str):
        stripped = unwrapped.strip()
        if _INT_LITERAL_RE.match(stripped):
            return int(stripped)
        if _DECIMAL_LITERAL_RE.match(stripped):
            fval = float(stripped)
            if math.isfinite(fval) and fval.is_integer():
                return int(fval)
    raise ValidationError(f"Row {row_index}: identifier '{column_name}' must be an integer.")


def _resolve_expected_var_type(
    schema: _validation.ValidationSchemaProtocol | None,
    form_key: str | None,
    form_id: int | None,
    var_name: str,
) -> str | None:
    """Look up normalized variable type from *schema* when available."""
    if schema is None:
        return None
    resolved_fk = form_key
    if not resolved_fk and form_id is not None:
        resolved_fk = schema.form_key_from_id(form_id)
    if not resolved_fk:
        return None
    variables = schema.variables_for_form(resolved_fk)
    if not variables or var_name not in variables:
        return None
    var_obj = variables[var_name]
    raw_type = getattr(var_obj, "variable_type", None) or getattr(var_obj, "variableType", None)
    if not isinstance(raw_type, str):
        return None
    return re.sub(r"[\s_-]+", " ", raw_type.strip().lower())


def _coerce_clinical_value(
    value: Any,
    *,
    from_csv_str: bool,
    expected_var_type: str | None = None,
) -> Any:
    """Coerce integer floats and CSV scalar tokens while preserving clinical strings."""
    unwrapped = _unwrap_scalar(value)
    if isinstance(unwrapped, bool):
        return unwrapped

    if expected_var_type in _TEXT_LIKE_TYPES:
        if isinstance(unwrapped, float) and math.isfinite(unwrapped) and unwrapped.is_integer():
            return str(int(unwrapped))
        return str(unwrapped)

    if expected_var_type in _BOOL_LIKE_TYPES and isinstance(unwrapped, str):
        low = unwrapped.strip().lower()
        if low in {"true", "false"}:
            return low == "true"

    if isinstance(unwrapped, int):
        return unwrapped

    if isinstance(unwrapped, float):
        if math.isfinite(unwrapped) and unwrapped.is_integer():
            return int(unwrapped)
        return unwrapped

    if from_csv_str and isinstance(unwrapped, str):
        stripped = unwrapped.strip()
        if _INT_LITERAL_RE.match(stripped):
            return int(stripped)
        if _DECIMAL_LITERAL_RE.match(stripped):
            fval = float(stripped)
            if math.isfinite(fval):
                return int(fval) if fval.is_integer() else fval

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
    schema: _validation.ValidationSchemaProtocol | None = None,
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
                vtype = _resolve_expected_var_type(
                    schema, resolved_form_key, resolved_form_id, str(var_key)
                )
                clinical_data[str(var_key)] = _coerce_clinical_value(
                    var_val, from_csv_str=False, expected_var_type=vtype
                )

    for col_name, raw_val in raw_row.items():
        if (
            col_name in _READ_ONLY_RECORD_COLUMNS
            or col_name in _IDENTIFIER_COLUMNS
            or col_name in _NESTED_DATA_COLUMNS
            or _is_missing_cell(raw_val)
        ):
            continue
        vtype = _resolve_expected_var_type(schema, resolved_form_key, resolved_form_id, col_name)
        clinical_data[col_name] = _coerce_clinical_value(
            raw_val,
            from_csv_str=from_csv_str,
            expected_var_type=vtype,
        )

    envelope["data"] = clinical_data
    return envelope


def csv_to_records(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8-sig",
    schema: _validation.ValidationSchemaProtocol | None = None,
) -> list[JsonDict]:
    """Load a CSV file into REST create-records payload dicts using the standard library.

    Works in ``packages/core`` even when ``pandas`` is not installed.
    """
    csv_path = Path(path)
    records: list[JsonDict] = []
    with csv_path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        for idx, raw_row in enumerate(reader):
            normalized = normalize_tabular_row(
                raw_row,
                row_index=idx,
                from_csv_str=True,
                schema=schema,
            )
            if normalized is not None:
                records.append(normalized)
    return records


def _is_dataframe_instance(obj: object) -> bool:
    """Return True if *obj* is a ``pandas.DataFrame`` without requiring ``pandas`` import."""
    cls = type(obj)
    return cls.__name__ == "DataFrame" and cls.__module__.startswith("pandas")


def dataframe_rows_to_records(
    df: Any,
    *,
    schema: _validation.ValidationSchemaProtocol | None = None,
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
        )
        if normalized is not None:
            records.append(normalized)
    return records


def normalize_records_input(
    records_data: RecordsInput,
    *,
    schema: _validation.ValidationSchemaProtocol | None = None,
) -> list[JsonDict]:
    """Normalize ``DataFrame``, CSV path, or ``list[JsonDict]`` into record payload dicts."""
    if isinstance(records_data, (str, os.PathLike)):
        return csv_to_records(records_data, schema=schema)
    if _is_dataframe_instance(records_data):
        return dataframe_rows_to_records(records_data, schema=schema)
    if isinstance(records_data, list):
        ensure_no_masked_sentinel(records_data)
        return records_data
    if isinstance(records_data, Sequence) and not isinstance(records_data, (str, bytes)):
        materialized = [dict(item) for item in records_data if isinstance(item, Mapping)]
        if len(materialized) != len(records_data):
            raise TypeError("All items in records_data sequence must be mappings.")
        ensure_no_masked_sentinel(materialized)
        return materialized
    raise TypeError(
        "records_data must be a list of dicts, pandas.DataFrame, or CSV path; "
        f"got {type(records_data).__name__}."
    )


__all__ = [
    "MASKED_SENTINEL",
    "csv_to_records",
    "dataframe_rows_to_records",
    "ensure_no_masked_sentinel",
    "normalize_records_input",
    "normalize_tabular_row",
]
