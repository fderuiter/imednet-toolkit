"""Private shared UAT domain contracts across engine, generator, submission, and orchestrator."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from typing import Any

from imednet.spi.validation import DataDictionary, ExtractedCondition

_TYPE_SEP_RE = re.compile(r"[\s_-]+")
_SLASH_SPACE_RE = re.compile(r"\s*/\s*")

INT_TYPES = frozenset({"number", "integer", "int"})
FLOAT_TYPES = frozenset({"float", "decimal", "double", "numeric", "slider"})
BOOL_TYPES = frozenset({"boolean", "bool"})
YES_NO_TYPES = frozenset({"yes/no", "yesno", "yes no", "true/false", "truefalse", "true false"})
SINGLE_CHOICE_TYPES = frozenset(
    {
        "coded",
        "radio",
        "radiobutton",
        "radio button",
        "single select",
        "singleselect",
        "dropdown",
        "drop down",
        "dropdownlist",
        "dropdown list",
        "select",
        "sql",
    }
)
MULTI_CHOICE_TYPES = frozenset({"checkbox", "check box", "multi select", "multiselect"})
TEXT_TYPES = frozenset({"text", "string", "alphanumeric"})
MEMO_TYPES = frozenset({"textarea", "text area", "memo", "notes", "note"})
CALC_TYPES = frozenset({"calculation", "calc", "calculated"})
DATE_TYPES = frozenset(
    {
        "date",
        "partialdate",
        "incompletedate",
        "date ymd",
        "date mdy",
        "date dmy",
        "date/time precision",
        "datetime precision",
        "date time precision",
    }
)
DATETIME_TYPES = frozenset(
    {
        "datetime",
        "partialdatetime",
        "incompletedatetime",
        "date/time",
        "date time",
        "durationdatetime",
        "intervaldatetime",
    }
)
TIME_TYPES = frozenset({"time", "partialtime", "incompletetime"})
TEMPORAL_PRECISION_TYPES = frozenset(
    {
        "partialdate",
        "incompletedate",
        "partialdatetime",
        "incompletedatetime",
        "durationdatetime",
        "intervaldatetime",
        "partialtime",
        "incompletetime",
    }
)
SKIP_EMPTY_TYPES = frozenset(
    {"signature", "file", "upload", "fileupload", "file upload", "descriptive", "label"}
)

_NON_SUBJECT_FORM_TYPES = frozenset({"site", "study", "admin"})
_LEGACY_ENROLLMENT_FORM_TYPES = frozenset({"enrollment", "registration"})
_SUBJECT_ENROLLMENT_FORM_TYPES = frozenset({"subject", "crf", ""})

_TABULAR_OP_ALIASES: dict[str, str] = {
    "=": "==",
    "eq": "==",
    "equals": "==",
    "ne": "!=",
    "neq": "!=",
    "gt": ">",
    "gte": ">=",
    "ge": ">=",
    "lt": "<",
    "lte": "<=",
    "le": "<=",
}


def coerce_optional_bool(value: Any) -> bool | None:
    """Parse boolean or truthy/falsy string tokens into ``bool | None``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "yes", "y", "1"}:
            return True
        if token in {"false", "no", "n", "0"}:
            return False
    return None


def normalize_variable_type(var_type: str | None) -> str:
    """Normalize variable type string for canonical taxonomy matching."""
    if not var_type:
        return ""
    cleaned = _TYPE_SEP_RE.sub(" ", var_type.strip().lower())
    if "/" in cleaned:
        cleaned = _SLASH_SPACE_RE.sub("/", cleaned)
    return cleaned


def coerce_variable_scalar(
    var_type: str | None,
    raw_value: Any,
    *,
    has_choices: bool = False,
) -> Any:
    """Coerce a raw scalar or string code into the canonical Python scalar for ``var_type``."""
    if raw_value is None:
        return ""
    if raw_value == "":
        return ""

    v_norm = normalize_variable_type(var_type)
    if v_norm in INT_TYPES:
        if isinstance(raw_value, bool):
            return int(raw_value)
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, float):
            return int(raw_value) if math.isfinite(raw_value) else raw_value
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if stripped.lstrip("+-").isdigit():
                return int(stripped)
            try:
                parsed_i = float(stripped)
                return int(parsed_i) if math.isfinite(parsed_i) else raw_value
            except ValueError:
                return raw_value
        return raw_value

    if v_norm in FLOAT_TYPES:
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, int):
            return raw_value if has_choices else float(raw_value)
        if isinstance(raw_value, float):
            return raw_value if math.isfinite(raw_value) else str(raw_value)
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if has_choices and stripped.lstrip("+-").isdigit():
                return int(stripped)
            try:
                parsed_f = float(stripped)
                return parsed_f if math.isfinite(parsed_f) else raw_value
            except ValueError:
                return raw_value
        return raw_value

    if v_norm in BOOL_TYPES:
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return bool(raw_value)
        if isinstance(raw_value, str):
            low = raw_value.strip().lower()
            if low in {"true", "1", "yes"}:
                return True
            if low in {"false", "0", "no"}:
                return False
            return raw_value
        return raw_value

    if v_norm in CALC_TYPES:
        if isinstance(raw_value, int) and not isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, bool):
            return float(raw_value)
        if isinstance(raw_value, float):
            return raw_value if math.isfinite(raw_value) else str(raw_value)
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if stripped.lstrip("+-").isdigit():
                return int(stripped)
            try:
                parsed_c = float(stripped)
                return parsed_c if math.isfinite(parsed_c) else raw_value
            except ValueError:
                return raw_value
        return str(raw_value)

    if isinstance(raw_value, bool) and v_norm in {"checkbox", "check box"}:
        return raw_value
    return str(raw_value)


def _get_extra_attributes(obj: Any) -> Mapping[str, Any] | None:
    """Extract ``extraAttributes`` mapping from either a model attribute or mapping entry."""
    extra = (
        obj.get("extraAttributes")
        if isinstance(obj, Mapping)
        else getattr(obj, "extraAttributes", None)
    )
    return extra if isinstance(extra, Mapping) else None


def extract_schema_variable_type(schema_var: Any | None) -> str | None:
    """Extract effective variable type (preferring ``odmDataType``) from a schema Variable."""
    if schema_var is None:
        return None
    dd_extra = _get_extra_attributes(schema_var)
    raw_odm_dt = dd_extra.get("odmDataType") if dd_extra is not None else None
    if isinstance(raw_odm_dt, str) and raw_odm_dt.strip():
        return raw_odm_dt.strip()
    raw_type = (
        (schema_var.get("variable_type") or schema_var.get("variableType"))
        if isinstance(schema_var, Mapping)
        else getattr(schema_var, "variable_type", getattr(schema_var, "variableType", None))
    )
    return raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else None


def resolve_enriched_variable_type(snap_type: str | None, schema_var: Any | None) -> str:
    """Resolve the richest canonical variable type across a snapshot Variable and schema Variable."""
    dd_type = extract_schema_variable_type(schema_var)
    snap_norm = normalize_variable_type(snap_type)
    dd_norm = normalize_variable_type(dd_type)
    if dd_type and (
        not snap_norm
        or (
            snap_norm == "coded"
            and (
                dd_norm in SINGLE_CHOICE_TYPES
                or dd_norm in MULTI_CHOICE_TYPES
                or dd_norm in YES_NO_TYPES
                or dd_norm in INT_TYPES
                or dd_norm in FLOAT_TYPES
                or dd_norm in BOOL_TYPES
                or dd_norm in CALC_TYPES
            )
        )
        or (snap_norm == "number" and (dd_norm in FLOAT_TYPES or dd_norm in CALC_TYPES))
        or (snap_norm == "text" and dd_norm != "text")
        or (snap_norm in {"date", "datetime", "time"} and dd_norm in TEMPORAL_PRECISION_TYPES)
    ):
        return dd_type
    return snap_type or dd_type or "Text"


def is_retired_variable_def(var_obj: Any) -> bool:
    """Return True when a snapshot or DataDictionary Variable is marked deleted or disabled."""
    extra_map = _get_extra_attributes(var_obj) or {}
    for attr in ("deleted", "disabled"):
        val = var_obj.get(attr) if isinstance(var_obj, Mapping) else getattr(var_obj, attr, None)
        coerced = coerce_optional_bool(val)
        if coerced is True:
            return True
        if attr in extra_map and coerce_optional_bool(extra_map[attr]) is True:
            return True
    return False


def lookup_schema_form_variables(
    schema_obj: Any | None,
    form_key: str,
    *,
    study_key: str | None = None,
) -> Mapping[str, Any]:
    """Lookup form variable definitions from a ``DataDictionary`` or ``ValidationSchemaProtocol``."""
    if schema_obj is None or not form_key:
        return {}
    vars_fn = getattr(schema_obj, "variables_for_form", None)
    if not callable(vars_fn):
        return {}
    if study_key is not None:
        try:
            res = vars_fn(form_key, study_key=study_key)
        except TypeError:
            res = vars_fn(form_key)
    else:
        res = vars_fn(form_key)
    return res if isinstance(res, Mapping) else {}


def _extract_form_bool_flag(obj: Any, *attrs: str) -> bool:
    """Return True when an attribute, mapping key, or ``extraAttributes`` entry is truthy."""
    extra_map = _get_extra_attributes(obj)
    for attr in attrs:
        val = obj.get(attr) if isinstance(obj, Mapping) else getattr(obj, attr, None)
        coerced = coerce_optional_bool(val)
        if coerced is not None:
            if coerced:
                return True
            continue
        if extra_map is not None:
            if attr in extra_map and coerce_optional_bool(extra_map[attr]) is True:
                return True
            low = attr.lower()
            for k, v in extra_map.items():
                if str(k).lower() == low and coerce_optional_bool(v) is True:
                    return True
    return False


def _normalized_form_type(form: Any) -> str:
    """Extract lowercase ``form_type`` / ``formType`` string when available."""
    raw = (
        (form.get("form_type") or form.get("formType"))
        if isinstance(form, Mapping)
        else (getattr(form, "form_type", None) or getattr(form, "formType", None))
    )
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    extra = _get_extra_attributes(form)
    if extra is not None:
        raw_extra = extra.get("formType", extra.get("form_type"))
        if isinstance(raw_extra, str) and raw_extra.strip():
            return raw_extra.strip().lower()
    return ""


def is_disabled_form(form: Any) -> bool:
    """Return True when ``form`` has ``disabled`` set to True."""
    return _extract_form_bool_flag(form, "disabled")


def is_non_subject_form(form: Any) -> bool:
    """Return True when ``form`` is an ``otherForms`` or Site/Study/Admin form."""
    return (
        _extract_form_bool_flag(form, "other_forms", "otherForms")
        or _normalized_form_type(form) in _NON_SUBJECT_FORM_TYPES
    )


def is_enrollment_form(form: Any) -> bool:
    """Return True when ``form`` is an active Subject registration/enrollment form."""
    if is_disabled_form(form) or is_non_subject_form(form):
        return False
    ft = _normalized_form_type(form)
    if ft in _LEGACY_ENROLLMENT_FORM_TYPES:
        return True
    return (
        _extract_form_bool_flag(form, "subject_record_report", "subjectRecordReport")
        and ft in _SUBJECT_ENROLLMENT_FORM_TYPES
    )


def is_unscheduled_form(form: Any) -> bool:
    """Return True when ``form`` has ``unscheduledVisit`` set to True."""
    return _extract_form_bool_flag(form, "unscheduled_visit", "unscheduledVisit")


def is_enrollment_form_key(
    form_key: str | None,
    snapshot: Any | None = None,
    data_dictionary: Any | None = None,
) -> bool:
    """Return True when ``form_key`` resolves to an enrollment Form in ``snapshot`` or ``data_dictionary``."""
    if not isinstance(form_key, str) or not form_key.strip():
        return False
    fk = form_key.strip()

    if snapshot is not None:
        forms_by_key = getattr(snapshot, "forms_by_key", None)
        snap_form = forms_by_key.get(fk) if isinstance(forms_by_key, Mapping) else None
        if snap_form is not None:
            if is_disabled_form(snap_form) or is_non_subject_form(snap_form):
                return False
            if is_enrollment_form(snap_form):
                return True

    if isinstance(data_dictionary, DataDictionary):
        for dd_form in data_dictionary.forms:
            if dd_form.formKey == fk:
                if is_disabled_form(dd_form) or is_non_subject_form(dd_form):
                    return False
                if is_enrollment_form(dd_form):
                    return True
                break

    return False


def subject_has_keyword(subj: Any, keyword_tag: str) -> bool:
    """Return True if ``subj`` has a keyword matching ``keyword_tag``."""
    if not keyword_tag:
        return True
    keywords = (
        subj.get("keywords") if isinstance(subj, Mapping) else getattr(subj, "keywords", None)
    )
    if not keywords:
        return False
    for kw in keywords:
        if isinstance(kw, str):
            if kw == keyword_tag:
                return True
            continue
        if isinstance(kw, Mapping):
            candidates = (
                kw.get("keyword_key"),
                kw.get("keywordKey"),
                kw.get("keyword_name"),
                kw.get("keywordName"),
            )
        else:
            candidates = (
                getattr(kw, "keyword_key", None),
                getattr(kw, "keywordKey", None),
                getattr(kw, "keyword_name", None),
                getattr(kw, "keywordName", None),
            )
        if keyword_tag in candidates:
            return True
    return False


def is_subject_deleted(subj: Any) -> bool:
    """Return True if ``subj`` is marked deleted."""
    deleted = (
        subj.get("deleted", False) if isinstance(subj, Mapping) else getattr(subj, "deleted", False)
    )
    return coerce_optional_bool(deleted) is True


def extract_subject_key(subj: Any) -> str | None:
    """Extract a stripped non-empty ``subjectKey`` from a ``Subject`` model or mapping."""
    raw = (
        (subj.get("subject_key") or subj.get("subjectKey"))
        if isinstance(subj, Mapping)
        else (getattr(subj, "subject_key", None) or getattr(subj, "subjectKey", None))
    )
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def extract_subject_site_name(subj: Any) -> str | None:
    """Extract a stripped non-empty ``siteName`` from a ``Subject`` model or mapping."""
    raw = (
        (subj.get("site_name") or subj.get("siteName"))
        if isinstance(subj, Mapping)
        else (getattr(subj, "site_name", None) or getattr(subj, "siteName", None))
    )
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def extract_subject_site_id(subj: Any) -> int | None:
    """Extract a positive integer ``siteId`` from a ``Subject`` model or mapping."""
    raw = (
        (subj.get("site_id") if subj.get("site_id") is not None else subj.get("siteId"))
        if isinstance(subj, Mapping)
        else (
            getattr(subj, "site_id", None)
            if getattr(subj, "site_id", None) is not None
            else getattr(subj, "siteId", None)
        )
    )
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        parsed = int(raw.strip())
        return parsed if parsed > 0 else None
    return None


def subject_matches_site(
    subj: Any,
    *,
    site_name: str | None = None,
    site_id: int | None = None,
) -> bool:
    """Return True when ``subj`` belongs to the requested ``site_name`` / ``site_id``."""
    target_name = site_name.strip() if isinstance(site_name, str) and site_name.strip() else None
    target_id = (
        site_id
        if isinstance(site_id, int) and not isinstance(site_id, bool) and site_id > 0
        else None
    )
    if target_name is None and target_id is None:
        return True

    subj_name = extract_subject_site_name(subj)
    subj_id = extract_subject_site_id(subj)

    if target_name is not None and target_id is not None:
        if subj_id is not None and subj_id != target_id:
            return False
        if subj_name is not None and subj_name != target_name:
            return False
        return subj_id == target_id or subj_name == target_name

    if target_name is not None:
        return subj_name == target_name

    return subj_id == target_id


def iter_matching_uat_subjects(
    subjects: Iterable[Any],
    *,
    keyword_tag: str = "UAT",
    require_keyword: bool = True,
    site_name: str | None = None,
    site_id: int | None = None,
    exclude_keys: AbstractSet[str] | None = None,
) -> list[tuple[str, Any]]:
    """Return ordered ``(subject_key, subject)`` pairs matching UAT keyword and Site constraints."""
    excluded = exclude_keys or set()
    seen: set[str] = set()
    matched: list[tuple[str, Any]] = []
    for subj in subjects:
        if is_subject_deleted(subj):
            continue
        if require_keyword and not subject_has_keyword(subj, keyword_tag):
            continue
        key = extract_subject_key(subj)
        if key is None or key in excluded or key in seen:
            continue
        if not subject_matches_site(subj, site_name=site_name, site_id=site_id):
            continue
        seen.add(key)
        matched.append((key, subj))
    return matched


def iter_matching_uat_subject_keys(
    subjects: Iterable[Any],
    *,
    keyword_tag: str = "UAT",
    require_keyword: bool = True,
    site_name: str | None = None,
    site_id: int | None = None,
    exclude_keys: AbstractSet[str] | None = None,
) -> list[str]:
    """Return ordered ``subjectKey`` strings matching UAT keyword and Site constraints."""
    return [
        key
        for key, _ in iter_matching_uat_subjects(
            subjects,
            keyword_tag=keyword_tag,
            require_keyword=require_keyword,
            site_name=site_name,
            site_id=site_id,
            exclude_keys=exclude_keys,
        )
    ]


def normalize_tabular_rule_conditions(rule: Mapping[str, Any]) -> list[ExtractedCondition]:
    """Normalize a non-XML tabular business rule into triggering ``ExtractedCondition`` items."""
    raw_var = rule.get("variable")
    if not isinstance(raw_var, str) or not raw_var.strip():
        return []
    variable = raw_var.strip()

    raw_op = rule.get("operator")
    op_str = str(raw_op).strip() if raw_op is not None else "=="
    op_low = op_str.lower()
    canon_op = _TABULAR_OP_ALIASES.get(op_low, op_str)

    raw_val = rule.get("value")
    check_type = str(rule.get("check_type") or "").strip().lower()

    conditions: list[ExtractedCondition] = []
    dep_var_raw = rule.get("dependency_variable")
    dep_val = rule.get("dependency_value")
    has_dep = (
        isinstance(dep_var_raw, str)
        and bool(dep_var_raw.strip())
        and dep_val is not None
        and dep_val != ""
    )
    if has_dep:
        assert isinstance(dep_var_raw, str)
        conditions.append(
            ExtractedCondition(
                variable=dep_var_raw.strip(),
                operator="==",
                value=str(dep_val),
                expected_truth=True,
            )
        )

    is_required_rule = (
        op_low in {"required", "not_empty", "notempty"}
        or (isinstance(raw_val, str) and raw_val.strip().lower() == "required")
        or (check_type == "required" and raw_op in (None, "", "==", "Required"))
    )
    if is_required_rule:
        conditions.append(
            ExtractedCondition(
                variable=variable,
                operator="empty",
                value=None,
                expected_truth=True,
            )
        )
        return conditions

    if canon_op in {"==", "!="}:
        str_val = "None" if raw_val is None else str(raw_val)
        conditions.append(
            ExtractedCondition(
                variable=variable,
                operator=canon_op,
                value=str_val,
                expected_truth=False,
            )
        )
        return conditions

    if canon_op in {">=", ">", "<=", "<"}:
        if has_dep and raw_val is None:
            conditions.append(
                ExtractedCondition(
                    variable=variable,
                    operator="literal_invalid",
                    value="Invalid_Value",
                    expected_truth=True,
                )
            )
        else:
            conditions.append(
                ExtractedCondition(
                    variable=variable,
                    operator=canon_op,
                    value="0" if raw_val is None else str(raw_val),
                    expected_truth=False,
                )
            )
        return conditions

    conditions.append(
        ExtractedCondition(
            variable=variable,
            operator=canon_op,
            value="" if raw_val is None else str(raw_val),
            expected_truth=False,
        )
    )
    return conditions
