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
from typing import TYPE_CHECKING, Any

from imednet.errors import ValidationError
from imednet.utils.typing import JsonDict, RecordsInput

if TYPE_CHECKING:
    from imednet.validation.cache import ValidationSchemaProtocol

MASKED_SENTINEL: str = "***MASKED***"

_INT_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)$")
_DECIMAL_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)\.\d+$")

_READ_ONLY_RECORD_COLUMNS: frozenset[str] = frozenset(
    {
        "study_key",
        "studyKey",
        "STUDY_KEY",
        "STUDYID",
        "record_type",
        "recordType",
        "RECORD_TYPE",
        "record_status",
        "recordStatus",
        "RECORD_STATUS",
        "deleted",
        "DELETED",
        "date_created",
        "dateCreated",
        "DATE_CREATED",
        "date_modified",
        "dateModified",
        "DATE_MODIFIED",
        "last_updated",
        "lastUpdated",
        "LAST_UPDATED",
        "visit_id",
        "visitId",
        "VISIT_ID",
        "VISITNUM",
        "parent_record_id",
        "parentRecordId",
        "PARENT_RECORD_ID",
        "keywords",
        "KEYWORDS",
        "record_revision",
        "recordRevision",
        "RECORD_REVISION",
        "record_revision_id",
        "recordRevisionId",
        "RECORD_REVISION_ID",
        "data_revision",
        "dataRevision",
        "DATA_REVISION",
    }
)

_NESTED_DATA_COLUMNS: frozenset[str] = frozenset({"record_data", "recordData", "data"})

_IDENTIFIER_COLUMNS: frozenset[str] = frozenset(
    {
        "record_id",
        "recordId",
        "RECORD_ID",
        "record_oid",
        "recordOid",
        "RECORD_OID",
        "form_key",
        "formKey",
        "FORM_KEY",
        "form_id",
        "formId",
        "FORM_ID",
        "subject_key",
        "subjectKey",
        "SUBJECT_KEY",
        "subject_oid",
        "subjectOid",
        "SUBJECT_OID",
        "subject_id",
        "subjectId",
        "SUBJECT_ID",
        "site_name",
        "siteName",
        "SITE_NAME",
        "site_id",
        "siteId",
        "SITE_ID",
        "SITEID",
        "interval_name",
        "intervalName",
        "INTERVAL_NAME",
        "interval_id",
        "intervalId",
        "INTERVAL_ID",
    }
)

_RECORD_ID_KEYS: frozenset[str] = frozenset(
    {"record_id", "recordId", "RECORD_ID", "record_oid", "recordOid", "RECORD_OID"}
)

_CANONICAL_WRITE_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "recordId",
        "recordOid",
        "formKey",
        "formId",
        "subjectKey",
        "subjectOid",
        "subjectId",
        "siteName",
        "siteId",
        "intervalName",
        "intervalId",
        "data",
    }
)

_SIMULATION_PASSTHROUGH_KEYS: frozenset[str] = frozenset(
    {
        "recordStatus",
        "record_status",
        "reasonForChange",
        "reason_for_change",
        "user",
        "role",
        "dateModified",
        "date_modified",
        "lastUpdated",
        "last_updated",
        "dateCreated",
        "date_created",
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

_BOOL_LIKE_TYPES: frozenset[str] = frozenset(
    {"bool", "boolean", "checkbox", "check box", "true/false", "truefalse", "true false"}
)

_SLASH_SPACE_RE = re.compile(r"\s*/\s*")


def reject_nonfinite_json_constant(token: str) -> Any:
    """Reject non-standard ``NaN``, ``Infinity``, and ``-Infinity`` JSON constants."""
    raise ValidationError(
        f"Invalid JSON numeric constant '{token}': NaN and Infinity are not allowed."
    )


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


def _contains_nonfinite_number(value: Any) -> bool:
    """Return True if *value* or any nested element contains a non-finite ``float``."""
    unwrapped = _unwrap_scalar(value)
    if isinstance(unwrapped, bool):
        return False
    if isinstance(unwrapped, float):
        return not math.isfinite(unwrapped)
    if isinstance(unwrapped, Mapping):
        return any(_contains_nonfinite_number(v) for v in unwrapped.values())
    if isinstance(unwrapped, (list, tuple, set, frozenset)):
        return any(_contains_nonfinite_number(v) for v in unwrapped)
    return False


def ensure_finite_numbers(records: Sequence[Mapping[str, Any]]) -> None:
    """Raise :class:`ValidationError` if any record contains ``NaN`` or ``Infinity``."""
    for row_idx, rec in enumerate(records):
        if not isinstance(rec, Mapping):
            continue
        for key, value in rec.items():
            if key in _NESTED_DATA_COLUMNS and isinstance(value, Mapping):
                for var_name, var_val in value.items():
                    if _contains_nonfinite_number(var_val):
                        raise ValidationError(
                            f"Row {row_idx}: variable '{var_name}' contains a non-finite number "
                            "(NaN or Infinity) and cannot be submitted."
                        )
            elif _contains_nonfinite_number(value):
                raise ValidationError(
                    f"Row {row_idx}: column '{key}' contains a non-finite number "
                    "(NaN or Infinity) and cannot be submitted."
                )


def _is_missing_cell(value: Any, *, allow_nan: bool = True) -> bool:
    """Return True when a tabular cell represents a null, NaN, or blank value."""
    unwrapped = _unwrap_scalar(value)
    if unwrapped is None:
        return True
    if isinstance(unwrapped, (dict, list, tuple, set, frozenset)):
        return False
    if isinstance(unwrapped, float):
        return allow_nan and math.isnan(unwrapped)
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


def _lookup_schema_variables(
    schema: ValidationSchemaProtocol | None,
    form_key: str | None,
    form_id: int | None,
    *,
    study_key: str | None = None,
) -> Mapping[str, Any] | None:
    """Resolve the form variable mapping from *schema*, forwarding *study_key* when supported."""
    if schema is None:
        return None
    resolved_fk = form_key
    if not resolved_fk and form_id is not None:
        if study_key is not None:
            try:
                resolved_fk = schema.form_key_from_id(form_id, study_key=study_key)  # type: ignore[call-arg]
            except TypeError:
                resolved_fk = schema.form_key_from_id(form_id)
        else:
            resolved_fk = schema.form_key_from_id(form_id)
    if not resolved_fk:
        return None
    if study_key is not None:
        try:
            return schema.variables_for_form(resolved_fk, study_key=study_key)  # type: ignore[call-arg]
        except TypeError:
            return schema.variables_for_form(resolved_fk)
    return schema.variables_for_form(resolved_fk)


def _resolve_expected_var_type(
    schema: ValidationSchemaProtocol | None,
    form_key: str | None,
    form_id: int | None,
    var_name: str,
    *,
    study_key: str | None = None,
) -> str | None:
    """Look up normalized variable type from *schema* when available."""
    variables = _lookup_schema_variables(schema, form_key, form_id, study_key=study_key)
    if not variables or var_name not in variables:
        return None
    var_obj = variables[var_name]
    raw_type = getattr(var_obj, "variable_type", None) or getattr(var_obj, "variableType", None)
    if not isinstance(raw_type, str):
        return None
    collapsed = re.sub(r"[\s_-]+", " ", raw_type.strip().lower())
    return _SLASH_SPACE_RE.sub("/", collapsed)


def _coerce_clinical_value(
    value: Any,
    *,
    from_csv_str: bool,
    expected_var_type: str | None = None,
    coerce_schema_types: bool = True,
) -> Any:
    """Coerce integer floats and CSV scalar tokens while preserving clinical strings."""
    unwrapped = _unwrap_scalar(value)
    if isinstance(unwrapped, bool):
        return unwrapped

    if coerce_schema_types and expected_var_type in _TEXT_LIKE_TYPES:
        if isinstance(unwrapped, float) and math.isfinite(unwrapped) and unwrapped.is_integer():
            return str(int(unwrapped))
        return str(unwrapped)

    if coerce_schema_types and expected_var_type in _BOOL_LIKE_TYPES and isinstance(unwrapped, str):
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
            parsed = json.loads(text, parse_constant=reject_nonfinite_json_constant)
        except ValidationError:
            raise
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
    create_only: bool = False,
    study_key: str | None = None,
    coerce_schema_types: bool = True,
    allow_nan_as_missing: bool = True,
) -> JsonDict | None:
    """Normalize a single DataFrame, CSV, or flat JSON row into a REST record payload dict.

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

    if all(_is_missing_cell(v, allow_nan=allow_nan_as_missing) for v in raw_row.values()):
        return None

    def _first_present(*names: str) -> tuple[str, Any] | None:
        for name in names:
            if name in raw_row and not _is_missing_cell(
                raw_row[name], allow_nan=allow_nan_as_missing
            ):
                return name, raw_row[name]
        return None

    envelope: JsonDict = {}

    # 2. Map identifier columns according to REST create/update-records payload contract
    if not create_only:
        record_id_pair = _first_present("recordId", "record_id", "RECORD_ID")
        if record_id_pair is not None:
            envelope["recordId"] = _coerce_int_identifier(
                record_id_pair[1], row_index=row_index, column_name=record_id_pair[0]
            )
        record_oid_pair = _first_present("recordOid", "record_oid", "RECORD_OID")
        if record_oid_pair is not None:
            envelope["recordOid"] = _coerce_str_identifier(record_oid_pair[1])

    form_key_pair = _first_present("formKey", "form_key", "FORM_KEY")
    form_id_pair = _first_present("formId", "form_id", "FORM_ID")
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

    subj_key_pair = _first_present("subjectKey", "subject_key", "SUBJECT_KEY")
    subj_oid_pair = _first_present("subjectOid", "subject_oid", "SUBJECT_OID")
    subj_id_pair = _first_present("subjectId", "subject_id", "SUBJECT_ID")
    usubjid_used_as_identifier = False
    if subj_key_pair is not None:
        envelope["subjectKey"] = _coerce_str_identifier(subj_key_pair[1])
    elif subj_oid_pair is not None:
        envelope["subjectOid"] = _coerce_str_identifier(subj_oid_pair[1])
    elif subj_id_pair is not None:
        envelope["subjectId"] = _coerce_int_identifier(
            subj_id_pair[1], row_index=row_index, column_name=subj_id_pair[0]
        )
    else:
        usubjid_pair = _first_present("USUBJID")
        if (
            usubjid_pair is not None
            and _resolve_expected_var_type(
                schema,
                resolved_form_key,
                resolved_form_id,
                "USUBJID",
                study_key=study_key,
            )
            is None
        ):
            envelope["subjectKey"] = _coerce_str_identifier(usubjid_pair[1])
            usubjid_used_as_identifier = True
        elif "USUBJID" in raw_row and _is_missing_cell(
            raw_row["USUBJID"], allow_nan=allow_nan_as_missing
        ):
            usubjid_used_as_identifier = True

    site_name_pair = _first_present("siteName", "site_name", "SITE_NAME")
    site_id_pair = _first_present("siteId", "site_id", "SITE_ID", "SITEID")
    if site_name_pair is not None:
        envelope["siteName"] = _coerce_str_identifier(site_name_pair[1])
    elif site_id_pair is not None:
        envelope["siteId"] = _coerce_int_identifier(
            site_id_pair[1], row_index=row_index, column_name=site_id_pair[0]
        )

    interval_name_pair = _first_present("intervalName", "interval_name", "INTERVAL_NAME")
    interval_id_pair = _first_present("intervalId", "interval_id", "INTERVAL_ID")
    if interval_name_pair is not None:
        envelope["intervalName"] = _coerce_str_identifier(interval_name_pair[1])
    elif interval_id_pair is not None:
        envelope["intervalId"] = _coerce_int_identifier(
            interval_id_pair[1], row_index=row_index, column_name=interval_id_pair[0]
        )

    # 3. Assemble clinical data from nested data column(s) + flattened variable columns
    clinical_data: JsonDict = {}
    for nested_col in ("record_data", "recordData", "data"):
        if nested_col in raw_row and not _is_missing_cell(
            raw_row[nested_col], allow_nan=allow_nan_as_missing
        ):
            parsed_nested = _parse_nested_data_cell(
                raw_row[nested_col], row_index=row_index, column_name=nested_col
            )
            for var_key, var_val in parsed_nested.items():
                if _contains_masked_sentinel(var_val):
                    raise ValidationError(
                        f"Row {row_index}: variable '{var_key}' contains masked PHI sentinel "
                        f"'{MASKED_SENTINEL}' and cannot be imported."
                    )
                if _is_missing_cell(var_val, allow_nan=allow_nan_as_missing):
                    continue
                vtype = _resolve_expected_var_type(
                    schema,
                    resolved_form_key,
                    resolved_form_id,
                    str(var_key),
                    study_key=study_key,
                )
                clinical_data[str(var_key)] = _coerce_clinical_value(
                    var_val,
                    from_csv_str=False,
                    expected_var_type=vtype,
                    coerce_schema_types=coerce_schema_types,
                )

    for col_name, raw_val in raw_row.items():
        if (
            col_name in _READ_ONLY_RECORD_COLUMNS
            or col_name in _IDENTIFIER_COLUMNS
            or col_name in _NESTED_DATA_COLUMNS
            or (usubjid_used_as_identifier and col_name == "USUBJID")
            or _is_missing_cell(raw_val, allow_nan=allow_nan_as_missing)
        ):
            continue
        vtype = _resolve_expected_var_type(
            schema,
            resolved_form_key,
            resolved_form_id,
            col_name,
            study_key=study_key,
        )
        clinical_data[col_name] = _coerce_clinical_value(
            raw_val,
            from_csv_str=from_csv_str,
            expected_var_type=vtype,
            coerce_schema_types=coerce_schema_types,
        )

    envelope["data"] = clinical_data
    return envelope


def csv_to_records(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8-sig",
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
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
                create_only=create_only,
                study_key=study_key,
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
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
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
            create_only=create_only,
            study_key=study_key,
        )
        if normalized is not None:
            records.append(normalized)
    return records


def _mapping_needs_tabular_normalization(rec: Mapping[str, Any]) -> bool:
    """Return True when a mapping in ``records_data`` uses export, snake_case, or flat layout."""
    keys = set(rec.keys())
    if not keys:
        return False
    # 1. Explicit nested export keys
    if "recordData" in keys or "record_data" in keys:
        return True
    # 2. Has neither record identifiers nor read-only metadata nor nested data (e.g. {"a": 1})
    known_meta_or_id = _IDENTIFIER_COLUMNS | _READ_ONLY_RECORD_COLUMNS | _NESTED_DATA_COLUMNS
    if not (keys & known_meta_or_id):
        return False
    # 3. Canonical dict with "data" mapping and only canonical write/simulation keys
    if isinstance(rec.get("data"), Mapping) and keys <= (
        _CANONICAL_WRITE_ENVELOPE_KEYS | _SIMULATION_PASSTHROUGH_KEYS
    ):
        return False
    # 4. Simulation status/audit-only update (e.g. {"recordId": 602, "recordStatus": "Verified"})
    return not (
        "data" not in keys
        and keys <= (_CANONICAL_WRITE_ENVELOPE_KEYS | _SIMULATION_PASSTHROUGH_KEYS)
    )


def _normalize_mapping_sequence(
    items: Sequence[Mapping[str, Any]],
    *,
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
) -> list[JsonDict]:
    """Normalize a sequence of record mappings while preserving canonical dict references."""
    ensure_no_masked_sentinel(items)
    any_needs_norm = any(_mapping_needs_tabular_normalization(rec) for rec in items)
    if not any_needs_norm and not create_only:
        if isinstance(items, list) and all(isinstance(rec, dict) for rec in items):
            return items  # type: ignore[return-value]
        return [dict(rec) for rec in items]

    normalized_list: list[JsonDict] = []
    for idx, rec in enumerate(items):
        if _mapping_needs_tabular_normalization(rec):
            norm = normalize_tabular_row(
                rec,
                row_index=idx,
                from_csv_str=False,
                schema=schema,
                create_only=create_only,
                study_key=study_key,
                coerce_schema_types=False,
                allow_nan_as_missing=False,
            )
            if norm is not None:
                normalized_list.append(norm)
        elif create_only and any(k in rec for k in _RECORD_ID_KEYS):
            stripped = {k: v for k, v in rec.items() if k not in _RECORD_ID_KEYS}
            normalized_list.append(stripped)
        elif isinstance(rec, dict):
            normalized_list.append(rec)
        else:
            normalized_list.append(dict(rec))
    return normalized_list


def extract_input_form_refs(records_data: RecordsInput) -> tuple[list[str], list[int]]:
    """Extract distinct ``(form_keys, form_ids)`` from raw ``RecordsInput`` prior to normalization."""
    seen_keys: dict[str, None] = {}
    seen_ids: dict[int, None] = {}

    def _record_ref(fk_val: Any, fid_val: Any) -> None:
        if fk_val is not None and not _is_missing_cell(fk_val):
            fk_str = _coerce_str_identifier(fk_val)
            if fk_str:
                seen_keys.setdefault(fk_str, None)
                return
        if fid_val is not None and not _is_missing_cell(fid_val):
            try:
                fid_int = _coerce_int_identifier(fid_val, row_index=0, column_name="formId")
                seen_ids.setdefault(fid_int, None)
            except ValidationError:
                pass

    if isinstance(records_data, (str, os.PathLike)):
        csv_path = Path(records_data)
        if csv_path.suffix.lower() == ".csv" and csv_path.exists():
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    fk = row.get("formKey") or row.get("form_key") or row.get("FORM_KEY")
                    fid = row.get("formId") or row.get("form_id") or row.get("FORM_ID")
                    _record_ref(fk, fid)
        return list(seen_keys), list(seen_ids)

    if _is_dataframe_instance(records_data):
        df_any: Any = records_data
        if getattr(df_any, "empty", False):
            return [], []
        cols = {str(c) for c in df_any.columns}
        fk_col = next((c for c in ("formKey", "form_key", "FORM_KEY") if c in cols), None)
        fid_col = next((c for c in ("formId", "form_id", "FORM_ID") if c in cols), None)
        if fk_col is not None:
            for val in df_any[fk_col]:
                _record_ref(val, None)
        if fid_col is not None:
            for val in df_any[fid_col]:
                _record_ref(None, val)
        return list(seen_keys), list(seen_ids)

    if isinstance(records_data, Sequence) and not isinstance(records_data, (str, bytes)):
        for item in records_data:
            if isinstance(item, Mapping):
                fk = item.get("formKey") or item.get("form_key") or item.get("FORM_KEY")
                fid = (
                    item.get("formId")
                    if item.get("formId") is not None
                    else (
                        item.get("form_id")
                        if item.get("form_id") is not None
                        else item.get("FORM_ID")
                    )
                )
                _record_ref(fk, fid)
    return list(seen_keys), list(seen_ids)


def normalize_records_input(
    records_data: RecordsInput,
    *,
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
) -> list[JsonDict]:
    """Normalize ``DataFrame``, CSV path, or ``list[JsonDict]`` into record payload dicts."""
    if isinstance(records_data, (str, os.PathLike)):
        return csv_to_records(
            records_data,
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    if _is_dataframe_instance(records_data):
        return dataframe_rows_to_records(
            records_data,
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    if isinstance(records_data, list):
        if not all(isinstance(item, Mapping) for item in records_data):
            raise TypeError("All items in records_data sequence must be mappings.")
        return _normalize_mapping_sequence(
            records_data,
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    if isinstance(records_data, Sequence) and not isinstance(records_data, (str, bytes)):
        materialized = [dict(item) for item in records_data if isinstance(item, Mapping)]
        if len(materialized) != len(records_data):
            raise TypeError("All items in records_data sequence must be mappings.")
        return _normalize_mapping_sequence(
            materialized,
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    raise TypeError(
        "records_data must be a list of dicts, pandas.DataFrame, or CSV path; "
        f"got {type(records_data).__name__}."
    )


__all__ = [
    "MASKED_SENTINEL",
    "csv_to_records",
    "dataframe_rows_to_records",
    "ensure_finite_numbers",
    "ensure_no_masked_sentinel",
    "extract_input_form_refs",
    "normalize_records_input",
    "normalize_tabular_row",
    "reject_nonfinite_json_constant",
]
