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
from typing import TYPE_CHECKING, Any, Literal, overload

from imednet.errors import ValidationError
from imednet.utils.typing import JsonDict, RecordsInput

if TYPE_CHECKING:
    from imednet.validation.cache import RecordValidationIssue, ValidationSchemaProtocol

MASKED_SENTINEL: str = "***MASKED***"

_INT_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)$")
_DECIMAL_LITERAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)\.\d+$")

_READ_ONLY_RECORD_COLUMNS: frozenset[str] = frozenset(
    {
        "study_key",
        "studyKey",
        "record_id",
        "recordId",
        "record_oid",
        "recordOid",
        "record_type",
        "recordType",
        "record_status",
        "recordStatus",
        "deleted",
        "date_created",
        "dateCreated",
        "date_modified",
        "dateModified",
        "last_updated",
        "lastUpdated",
        "visit_id",
        "visitId",
        "parent_record_id",
        "parentRecordId",
        "keywords",
    }
)

_NESTED_DATA_COLUMNS: frozenset[str] = frozenset({"record_data", "recordData", "data"})

_IDENTIFIER_COLUMNS: frozenset[str] = frozenset(
    {
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
        "dateymd",
        "date mdy",
        "datemdy",
        "date dmy",
        "datedmy",
        "datetime",
        "date/time",
        "date time",
        "durationdatetime",
        "duration date time",
        "intervaldatetime",
        "interval date time",
        "date/time precision",
        "datetime precision",
        "datetimeprecision",
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


_INT_IDENTIFIER_COLUMNS: frozenset[str] = frozenset(
    {
        "form_id",
        "formId",
        "subject_id",
        "subjectId",
        "site_id",
        "siteId",
        "interval_id",
        "intervalId",
    }
)


class NormalizedRecordBatch(list[JsonDict]):
    """List of normalized record payload dicts carrying any collected validation issues."""

    def __init__(
        self,
        iterable: Sequence[JsonDict] = (),
        *,
        issues: Sequence[RecordValidationIssue] | None = None,
    ) -> None:
        """Initialize the batch list and attach collected validation issues."""
        super().__init__(iterable)
        self.issues: list[RecordValidationIssue] = list(issues) if issues is not None else []


def _make_validation_issue(
    *,
    record_index: int,
    form_key: str | None,
    variable_name: str | None,
    message: str,
    rule_id: str | None = None,
) -> RecordValidationIssue:
    from imednet.validation.cache import RecordValidationIssue

    return RecordValidationIssue(
        record_index=record_index,
        form_key=form_key,
        variable_name=variable_name,
        message=message,
        rule_id=rule_id,
    )


def _extract_mapping_form_key(
    rec: Mapping[str, Any], default_form_key: str | None = None
) -> str | None:
    fk = rec.get("formKey") or rec.get("form_key")
    if fk is not None and str(fk).strip():
        return str(fk).strip()
    return default_form_key


@overload
def ensure_no_masked_sentinel(
    records: Sequence[Mapping[str, Any]],
    *,
    collect_errors: Literal[False] = False,
    issues: list[RecordValidationIssue] | None = None,
) -> None: ...


@overload
def ensure_no_masked_sentinel(
    records: Sequence[Mapping[str, Any]],
    *,
    collect_errors: Literal[True],
    issues: list[RecordValidationIssue] | None = None,
) -> list[RecordValidationIssue]: ...


@overload
def ensure_no_masked_sentinel(
    records: Sequence[Mapping[str, Any]],
    *,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> list[RecordValidationIssue] | None: ...


def ensure_no_masked_sentinel(
    records: Sequence[Mapping[str, Any]],
    *,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> list[RecordValidationIssue] | None:
    """Raise :class:`ValidationError` (or collect issues) if any record contains ``***MASKED***``."""
    collected: list[RecordValidationIssue] = []
    for row_idx, rec in enumerate(records):
        if not isinstance(rec, Mapping):
            continue
        row_fk = _extract_mapping_form_key(rec)
        for key, value in rec.items():
            if key in _NESTED_DATA_COLUMNS and isinstance(value, Mapping):
                for var_name, var_val in value.items():
                    if _contains_masked_sentinel(var_val):
                        msg = (
                            f"Row {row_idx}: variable '{var_name}' contains masked PHI sentinel "
                            f"'{MASKED_SENTINEL}' and cannot be submitted."
                        )
                        if not collect_errors:
                            raise ValidationError(msg)
                        issue = _make_validation_issue(
                            record_index=row_idx,
                            form_key=row_fk,
                            variable_name=str(var_name),
                            message=msg,
                            rule_id="masked_phi",
                        )
                        collected.append(issue)
                        if issues is not None:
                            issues.append(issue)
            elif _contains_masked_sentinel(value):
                msg = (
                    f"Row {row_idx}: column '{key}' contains masked PHI sentinel "
                    f"'{MASKED_SENTINEL}' and cannot be submitted."
                )
                if not collect_errors:
                    raise ValidationError(msg)
                issue = _make_validation_issue(
                    record_index=row_idx,
                    form_key=row_fk,
                    variable_name=str(key),
                    message=msg,
                    rule_id="masked_phi",
                )
                collected.append(issue)
                if issues is not None:
                    issues.append(issue)
    return collected if collect_errors else None


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
    schema: ValidationSchemaProtocol | None,
    form_key: str | None,
    form_id: int | None,
    var_name: str,
    *,
    study_key: str | None = None,
) -> str | None:
    """Look up normalized variable type from *schema* when available."""
    if schema is None:
        return None
    from imednet.validation.cache import BaseSchemaCache

    resolved_fk = form_key
    if not resolved_fk and form_id is not None:
        resolved_fk = (
            schema.form_key_from_id(form_id, study_key=study_key)
            if isinstance(schema, BaseSchemaCache)
            else schema.form_key_from_id(form_id)
        )
    if not resolved_fk:
        return None
    variables = (
        schema.variables_for_form(resolved_fk, study_key=study_key)
        if isinstance(schema, BaseSchemaCache)
        else schema.variables_for_form(resolved_fk)
    )
    if not variables or var_name not in variables:
        return None
    var_obj = variables[var_name]
    raw_type = getattr(var_obj, "variable_type", None) or getattr(var_obj, "variableType", None)
    if not isinstance(raw_type, str):
        return None
    cleaned = re.sub(r"[\s_-]+", " ", raw_type.strip().lower())
    if "/" in cleaned:
        cleaned = re.sub(r"\s*/\s*", "/", cleaned)
    return cleaned


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
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> JsonDict | None:
    """Normalize a single DataFrame or CSV row into a REST create-records payload dict.

    Returns ``None`` if every cell in the row is empty.
    """

    def _emit_issue(
        message: str,
        *,
        form_key: str | None = None,
        variable_name: str | None = None,
        rule_id: str | None = None,
    ) -> None:
        if not collect_errors:
            raise ValidationError(message)
        if issues is not None:
            issues.append(
                _make_validation_issue(
                    record_index=row_index,
                    form_key=form_key,
                    variable_name=variable_name,
                    message=message,
                    rule_id=rule_id,
                )
            )

    def _first_present(*names: str) -> tuple[str, Any] | None:
        for name in names:
            if name in raw_row and not _is_missing_cell(raw_row[name]):
                return name, raw_row[name]
        return None

    has_extra_csv_cols = any(col_name is None for col_name in raw_row)
    if not has_extra_csv_cols and all(_is_missing_cell(v) for v in raw_row.values()):
        return None

    initial_fk_pair = _first_present("formKey", "form_key")
    hint_form_key = (
        _coerce_str_identifier(initial_fk_pair[1])
        if initial_fk_pair is not None and not _contains_masked_sentinel(initial_fk_pair[1])
        else default_form_key
    )

    # 1. Reject (or collect) extra CSV columns and masked PHI sentinels before stripping/coercion
    for col_name, raw_val in raw_row.items():
        if col_name is None:
            _emit_issue(
                f"Row {row_index}: CSV row contains more values than header columns.",
                form_key=hint_form_key,
                variable_name=None,
                rule_id="extra_csv_columns",
            )
            continue
        if col_name not in _NESTED_DATA_COLUMNS and _contains_masked_sentinel(raw_val):
            _emit_issue(
                f"Row {row_index}: column '{col_name}' contains masked PHI sentinel "
                f"'{MASKED_SENTINEL}' and cannot be imported.",
                form_key=hint_form_key,
                variable_name=str(col_name),
                rule_id="masked_phi",
            )

    envelope: JsonDict = {}

    # 2. Map identifier columns (applying default Form identifiers before scalar coercion)
    form_key_pair = _first_present("formKey", "form_key")
    form_id_pair = _first_present("formId", "form_id")
    resolved_form_key: str | None = None
    resolved_form_id: int | None = None
    if form_key_pair is not None:
        resolved_form_key = _coerce_str_identifier(form_key_pair[1])
        envelope["formKey"] = resolved_form_key
    if form_id_pair is not None:
        try:
            resolved_form_id = _coerce_int_identifier(
                form_id_pair[1], row_index=row_index, column_name=form_id_pair[0]
            )
            envelope["formId"] = resolved_form_id
        except ValidationError as exc:
            _emit_issue(
                str(exc),
                form_key=hint_form_key,
                variable_name=form_id_pair[0],
                rule_id="invalid_identifier",
            )
    if resolved_form_key is None and resolved_form_id is None:
        if default_form_key:
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
    if subj_oid_pair is not None:
        envelope["subjectOid"] = _coerce_str_identifier(subj_oid_pair[1])
    if subj_id_pair is not None:
        try:
            envelope["subjectId"] = _coerce_int_identifier(
                subj_id_pair[1], row_index=row_index, column_name=subj_id_pair[0]
            )
        except ValidationError as exc:
            _emit_issue(
                str(exc),
                form_key=resolved_form_key,
                variable_name=subj_id_pair[0],
                rule_id="invalid_identifier",
            )

    site_name_pair = _first_present("siteName", "site_name")
    site_id_pair = _first_present("siteId", "site_id")
    if site_name_pair is not None:
        envelope["siteName"] = _coerce_str_identifier(site_name_pair[1])
    if site_id_pair is not None:
        try:
            envelope["siteId"] = _coerce_int_identifier(
                site_id_pair[1], row_index=row_index, column_name=site_id_pair[0]
            )
        except ValidationError as exc:
            _emit_issue(
                str(exc),
                form_key=resolved_form_key,
                variable_name=site_id_pair[0],
                rule_id="invalid_identifier",
            )

    interval_name_pair = _first_present("intervalName", "interval_name")
    interval_id_pair = _first_present("intervalId", "interval_id")
    if interval_name_pair is not None:
        envelope["intervalName"] = _coerce_str_identifier(interval_name_pair[1])
    if interval_id_pair is not None:
        try:
            envelope["intervalId"] = _coerce_int_identifier(
                interval_id_pair[1], row_index=row_index, column_name=interval_id_pair[0]
            )
        except ValidationError as exc:
            _emit_issue(
                str(exc),
                form_key=resolved_form_key,
                variable_name=interval_id_pair[0],
                rule_id="invalid_identifier",
            )

    # 3. Assemble clinical data from nested data column(s) + flattened variable columns
    clinical_data: JsonDict = {}
    for nested_col in ("record_data", "recordData", "data"):
        if nested_col in raw_row and not _is_missing_cell(raw_row[nested_col]):
            try:
                parsed_nested = _parse_nested_data_cell(
                    raw_row[nested_col], row_index=row_index, column_name=nested_col
                )
            except ValidationError as exc:
                _emit_issue(
                    str(exc),
                    form_key=resolved_form_key,
                    variable_name=nested_col,
                    rule_id="malformed_json",
                )
                continue
            for var_key, var_val in parsed_nested.items():
                if _contains_masked_sentinel(var_val):
                    _emit_issue(
                        f"Row {row_index}: variable '{var_key}' contains masked PHI sentinel "
                        f"'{MASKED_SENTINEL}' and cannot be imported.",
                        form_key=resolved_form_key,
                        variable_name=str(var_key),
                        rule_id="masked_phi",
                    )
                    clinical_data[str(var_key)] = var_val
                    continue
                if _is_missing_cell(var_val):
                    continue
                vtype = _resolve_expected_var_type(
                    schema,
                    resolved_form_key,
                    resolved_form_id,
                    str(var_key),
                    study_key=study_key,
                )
                clinical_data[str(var_key)] = _coerce_clinical_value(
                    var_val, from_csv_str=False, expected_var_type=vtype
                )

    for col_name, raw_val in raw_row.items():
        if (
            col_name is None
            or col_name in _READ_ONLY_RECORD_COLUMNS
            or col_name in _IDENTIFIER_COLUMNS
            or col_name in _NESTED_DATA_COLUMNS
            or _is_missing_cell(raw_val)
        ):
            continue
        if _contains_masked_sentinel(raw_val):
            clinical_data[col_name] = raw_val
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
        )

    envelope["data"] = clinical_data
    return envelope


def csv_to_records(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8-sig",
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> list[JsonDict]:
    """Load a CSV file into REST create-records payload dicts using the standard library.

    Works in ``packages/core`` even when ``pandas`` is not installed.
    """
    csv_path = Path(path)
    records: list[JsonDict] = []
    collected_issues: list[RecordValidationIssue] = []
    with csv_path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return NormalizedRecordBatch([], issues=collected_issues)
        record_idx = 0
        for raw_row in reader:
            normalized = normalize_tabular_row(
                raw_row,
                row_index=record_idx,
                from_csv_str=True,
                schema=schema,
                study_key=study_key,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
                collect_errors=collect_errors,
                issues=collected_issues,
            )
            if normalized is not None:
                records.append(normalized)
                record_idx += 1
    if issues is not None:
        issues.extend(collected_issues)
    return NormalizedRecordBatch(records, issues=collected_issues)


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
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> list[JsonDict]:
    """Convert a ``pandas.DataFrame`` into REST create-records payload dicts."""
    collected_issues: list[RecordValidationIssue] = []
    if getattr(df, "empty", False):
        return NormalizedRecordBatch([], issues=collected_issues)
    columns = [str(col) for col in df.columns]
    records: list[JsonDict] = []
    record_idx = 0
    for values in df.itertuples(index=False, name=None):
        raw_row = dict(zip(columns, values, strict=False))
        normalized = normalize_tabular_row(
            raw_row,
            row_index=record_idx,
            from_csv_str=False,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
            collect_errors=collect_errors,
            issues=collected_issues,
        )
        if normalized is not None:
            records.append(normalized)
            record_idx += 1
    if issues is not None:
        issues.extend(collected_issues)
    return NormalizedRecordBatch(records, issues=collected_issues)


def _normalize_dict_sequence(
    items: Sequence[Mapping[str, Any]],
    *,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> NormalizedRecordBatch:
    """Validate and apply form defaults onto a sequence of record dicts while preserving identity."""
    collected_issues: list[RecordValidationIssue] = []
    out: list[JsonDict] = []
    for idx, raw_item in enumerate(items):
        rec: JsonDict = raw_item if isinstance(raw_item, dict) else dict(raw_item)
        mutated = False

        has_fk = bool(rec.get("formKey") or rec.get("form_key"))
        has_fid = rec.get("formId") is not None or rec.get("form_id") is not None
        if not has_fk and not has_fid:
            if default_form_key:
                rec = dict(rec)
                rec["formKey"] = default_form_key
                mutated = True
            elif default_form_id is not None:
                rec = dict(rec)
                rec["formId"] = default_form_id
                mutated = True

        row_fk = _extract_mapping_form_key(rec, default_form_key)
        for id_col in _INT_IDENTIFIER_COLUMNS:
            if id_col not in rec or _is_missing_cell(rec[id_col]):
                continue
            try:
                coerced_int = _coerce_int_identifier(rec[id_col], row_index=idx, column_name=id_col)
                if type(rec[id_col]) is not int or rec[id_col] != coerced_int:
                    if not mutated:
                        rec = dict(rec)
                        mutated = True
                    rec[id_col] = coerced_int
            except ValidationError as exc:
                if not collect_errors:
                    raise
                collected_issues.append(
                    _make_validation_issue(
                        record_index=idx,
                        form_key=row_fk,
                        variable_name=id_col,
                        message=str(exc),
                        rule_id="invalid_identifier",
                    )
                )
                if not mutated:
                    rec = dict(rec)
                    mutated = True
                rec.pop(id_col, None)
                if (
                    id_col in {"formId", "form_id"}
                    and not (rec.get("formKey") or rec.get("form_key"))
                    and rec.get("formId") is None
                    and rec.get("form_id") is None
                ):
                    if default_form_key:
                        rec["formKey"] = default_form_key
                    elif default_form_id is not None:
                        rec["formId"] = default_form_id

        for nested_col in _NESTED_DATA_COLUMNS:
            if (
                nested_col in rec
                and rec[nested_col] is not None
                and not isinstance(rec[nested_col], Mapping)
            ):
                try:
                    parsed_nested = _parse_nested_data_cell(
                        rec[nested_col], row_index=idx, column_name=nested_col
                    )
                    if not mutated:
                        rec = dict(rec)
                        mutated = True
                    rec[nested_col] = dict(parsed_nested)
                except ValidationError as exc:
                    if not collect_errors:
                        raise
                    collected_issues.append(
                        _make_validation_issue(
                            record_index=idx,
                            form_key=row_fk,
                            variable_name=nested_col,
                            message=str(exc),
                            rule_id="malformed_json",
                        )
                    )
                    if not mutated:
                        rec = dict(rec)
                        mutated = True
                    rec[nested_col] = {}

        out.append(rec)

    ensure_no_masked_sentinel(out, collect_errors=collect_errors, issues=collected_issues)
    if issues is not None:
        issues.extend(collected_issues)
    return NormalizedRecordBatch(out, issues=collected_issues)


def _json_path_to_records(
    path: Path,
    *,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> NormalizedRecordBatch:
    """Load a ``.json`` file into normalized record payload dicts."""
    raw_text = path.read_text(encoding="utf-8")
    try:
        loaded = json.loads(raw_text)
    except ValueError as exc:
        msg = f"Invalid JSON in {path}: {exc}"
        if not collect_errors:
            raise ValidationError(msg) from exc
        issue = _make_validation_issue(
            record_index=0,
            form_key=default_form_key,
            variable_name=None,
            message=msg,
            rule_id="malformed_json",
        )
        if issues is not None:
            issues.append(issue)
        return NormalizedRecordBatch([], issues=[issue])

    if isinstance(loaded, Mapping):
        raw_items: list[Mapping[str, Any]] = [loaded]
    elif isinstance(loaded, list) and all(isinstance(x, Mapping) for x in loaded):
        raw_items = list(loaded)
    else:
        msg = f"JSON payload in {path} must be a JSON object or array of objects."
        if not collect_errors:
            raise ValidationError(msg)
        issue = _make_validation_issue(
            record_index=0,
            form_key=default_form_key,
            variable_name=None,
            message=msg,
            rule_id="malformed_json",
        )
        if issues is not None:
            issues.append(issue)
        return NormalizedRecordBatch([], issues=[issue])

    return _normalize_dict_sequence(
        raw_items,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
        collect_errors=collect_errors,
        issues=issues,
    )


def normalize_records_input(
    records_data: RecordsInput,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> list[JsonDict]:
    """Normalize ``DataFrame``, CSV/JSON path, or ``list[JsonDict]`` into record payload dicts."""
    if isinstance(records_data, NormalizedRecordBatch):
        prior_issues = list(records_data.issues)
        seen_masked = {
            (iss.record_index, iss.variable_name)
            for iss in prior_issues
            if iss.rule_id == "masked_phi"
        }
        batch = _normalize_dict_sequence(
            records_data,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
            collect_errors=collect_errors,
        )
        combined = prior_issues + [
            iss
            for iss in batch.issues
            if iss.rule_id != "masked_phi"
            or (iss.record_index, iss.variable_name) not in seen_masked
        ]
        if issues is not None:
            issues.extend(combined)
        return NormalizedRecordBatch(batch, issues=combined)

    if isinstance(records_data, (str, os.PathLike)):
        file_path = Path(records_data)
        if file_path.suffix.lower() == ".json":
            return _json_path_to_records(
                file_path,
                default_form_key=default_form_key,
                default_form_id=default_form_id,
                collect_errors=collect_errors,
                issues=issues,
            )
        return csv_to_records(
            file_path,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
            collect_errors=collect_errors,
            issues=issues,
        )
    if _is_dataframe_instance(records_data):
        return dataframe_rows_to_records(
            records_data,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            default_form_id=default_form_id,
            collect_errors=collect_errors,
            issues=issues,
        )
    if isinstance(records_data, Sequence) and not isinstance(records_data, (str, bytes)):
        if not all(isinstance(item, Mapping) for item in records_data):
            raise TypeError("All items in records_data sequence must be mappings.")
        return _normalize_dict_sequence(
            records_data,  # type: ignore[arg-type]
            default_form_key=default_form_key,
            default_form_id=default_form_id,
            collect_errors=collect_errors,
            issues=issues,
        )
    raise TypeError(
        "records_data must be a list of dicts, pandas.DataFrame, or CSV path; "
        f"got {type(records_data).__name__}."
    )


__all__ = [
    "MASKED_SENTINEL",
    "NormalizedRecordBatch",
    "csv_to_records",
    "dataframe_rows_to_records",
    "ensure_no_masked_sentinel",
    "normalize_records_input",
    "normalize_tabular_row",
]
