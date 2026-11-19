"""Field normalization and date policy for data dictionary."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Regex to validate MM-DD-YYYY with optional time and timezone
MM_DD_YYYY_PATTERN = re.compile(
    r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])-([0-9]{4})"
    r"(?:[ T]([0-2][0-9]):([0-5][0-9]):([0-5][0-9])(?:\.[0-9]+)?)?"
    r"(?:Z|[+-][0-9]{2}:?[0-9]{2})?$"
)


def to_camel_case(text: str) -> str:
    """Strict lexical camelCasing for headers without semantic renames.

    Examples:
        'Patient Record Report' -> 'patientRecordReport'
        'Special Patient ID' -> 'specialPatientId'
        'Form Key' -> 'formKey'
        'Form ID' -> 'formId'
        'BUSINESS_LOGIC' -> 'businessLogic'
        'variableName' -> 'variableName'
        'Form' -> 'form'

    Args:
        text: The string to convert.

    Returns:
        The camelCased string.
    """
    if not text:
        return ""

    text = text.lstrip("\ufeff")
    if not text:
        return ""

    # If already standard camelCase (starts with lowercase, alphanumeric only, has uppercase)
    if re.match(r"^[a-z]+[A-Za-z0-9]*$", text) and not any(c in text for c in " _-"):
        return text

    # If PascalCase without spaces/delimiters
    if re.match(r"^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)*$", text) and not any(c in text for c in " _-"):
        return text[0].lower() + text[1:]

    # Remove special punctuation except spaces, underscores, hyphens
    cleaned = re.sub(r"[^a-zA-Z0-9\s_-]", "", text).strip()
    if not cleaned:
        return text.lower()

    words = [w for w in re.split(r"[\s_-]+", cleaned) if w]
    if not words:
        return ""

    first_word = words[0].lower()
    other_words = [w.capitalize() for w in words[1:]]
    return first_word + "".join(other_words)


def validate_timestamp(timestamp_str: str) -> str:
    """Assert that a timestamp string adheres strictly to the MM-DD-YYYY format.

    Args:
        timestamp_str: The timestamp string to validate.

    Returns:
        The validated timestamp string.

    Raises:
        ValueError: If the timestamp does not adhere to MM-DD-YYYY or has invalid calendar date.
    """
    cleaned = timestamp_str.strip()
    match = MM_DD_YYYY_PATTERN.match(cleaned)
    if not match:
        raise ValueError(
            f"Invalid timestamp '{timestamp_str}': timestamps must adhere to MM-DD-YYYY format"
        )
    month_str, day_str, year_str = match.group(1), match.group(2), match.group(3)
    try:
        from datetime import datetime

        datetime.strptime(f"{month_str}-{day_str}-{year_str}", "%m-%d-%Y")
    except ValueError as exc:
        raise ValueError(
            f"Invalid timestamp '{timestamp_str}': timestamps must adhere to MM-DD-YYYY format"
        ) from exc
    return cleaned


def normalize_row_data(
    row: dict[str, str], known_camel_fields: set[str], source_name: str = "CSV"
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Normalize a CSV row: map known headers to camelCase and isolate unknown attributes.

    Args:
        row: Raw dictionary of CSV header to string value.
        known_camel_fields: Set of known fields in camelCase.
        source_name: Name of the source file for warning messages.

    Returns:
        Tuple of (known_fields_dict, extra_attributes_dict, warning_messages).
    """
    known_data: dict[str, Any] = {}
    extra_attributes: dict[str, Any] = {}
    warnings: list[str] = []

    for raw_header, raw_value in row.items():
        if raw_header is None:
            continue
        cleaned_val: str | None = raw_value.strip() if isinstance(raw_value, str) else raw_value
        if cleaned_val == "":
            cleaned_val = None

        camel_key = to_camel_case(raw_header)
        if camel_key in known_camel_fields:
            known_data[camel_key] = cleaned_val
        else:
            extra_attributes[camel_key] = cleaned_val
            warnings.append(
                f"Unknown column '{raw_header}' ({camel_key}) encountered in {source_name}"
            )

    return known_data, extra_attributes, warnings


_CANONICAL_VARIABLE_TYPES: dict[str, str] = {
    "text": "Text",
    "string": "Text",
    "memo": "Text",
    "notes": "Text",
    "textarea": "Text",
    "alphanumeric": "Text",
    "int": "Integer",
    "integer": "Integer",
    "float": "Float",
    "double": "Float",
    "decimal": "Float",
    "numeric": "Float",
    "date": "Date",
    "date ymd": "Date",
    "date mdy": "Date",
    "date dmy": "Date",
    "datetime": "DateTime",
    "date/time": "DateTime",
    "date time": "DateTime",
    "date/time precision": "DateTime",
    "datetime precision": "DateTime",
    "durationdatetime": "DateTime",
    "intervaldatetime": "DateTime",
    "time": "Time",
    "bool": "Boolean",
    "boolean": "Boolean",
    "radio": "Radio",
    "radiobutton": "Radio",
    "radio button": "Radio",
    "yesno": "Radio",
    "yes/no": "Radio",
    "truefalse": "Radio",
    "true/false": "Radio",
    "dropdown": "Single Select",
    "dropdownlist": "Single Select",
    "drop down": "Single Select",
    "dropdown list": "Single Select",
    "select": "Single Select",
    "singleselect": "Single Select",
    "single select": "Single Select",
    "coded": "Single Select",
    "sql": "Single Select",
    "checkbox": "Multi Select",
    "check box": "Multi Select",
    "multiselect": "Multi Select",
    "multi select": "Multi Select",
    "calc": "Calculation",
    "calculation": "Calculation",
    "calculated": "Calculation",
    "file": "File",
    "upload": "File",
    "fileupload": "File",
    "file upload": "File",
    "slider": "Slider",
    "descriptive": "Descriptive",
    "label": "Descriptive",
}

_CANONICAL_FORM_TYPES: dict[str, str] = {
    "subject": "Subject",
    "patient": "Subject",
    "participant": "Subject",
    "crf": "Subject",
    "study": "Study",
    "site": "Site",
    "admin": "Admin",
    "administrative": "Admin",
    "screening": "Screening",
    "log": "Log",
}


def normalize_form_type(value: str | None, *, default: str | None = None) -> str | None:
    """Normalize formType values across loaders using canonical 'Subject' terminology.

    Legacy 'Patient' (and 'CRF'/'Participant') values normalize to 'Subject'.

    Args:
        value: Raw formType value.
        default: Default canonical formType when value is empty or None.

    Returns:
        Normalized formType string or default.
    """
    if value is None:
        return default
    cleaned = str(value).strip()
    if not cleaned:
        return default
    return _CANONICAL_FORM_TYPES.get(cleaned.lower(), cleaned)


def normalize_form_id(form_id: Any, form_key: str | None = None) -> int | str | None:
    """Normalize formId values, stripping synthetic formKey fallbacks and coercing numeric IDs.

    Args:
        form_id: Raw formId value from a loader or model.
        form_key: Associated formKey to detect synthetic formId == formKey defaults.

    Returns:
        Normalized integer or string formId, or None if unset or synthetic.
    """
    if form_id is None or isinstance(form_id, bool):
        return None
    if isinstance(form_id, int):
        return form_id
    cleaned = str(form_id).strip()
    if not cleaned:
        return None
    if form_key is not None and cleaned == form_key.strip():
        return None
    if cleaned.isdigit():
        return int(cleaned)
    return cleaned


def normalize_variable_type(
    value: str | None,
    *,
    precision: str | None = None,
) -> str | None:
    """Normalize variableType values across CSV, ODM, REDCap, and SDK sources.

    Args:
        value: Raw variableType string.
        precision: Optional precision string to disambiguate generic 'Number' types.

    Returns:
        Canonical variableType string, or None if empty.
    """
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    key = re.sub(r"[\s_-]+", " ", cleaned.lower()).strip()
    if key == "number":
        if precision is not None:
            prec_clean = str(precision).strip()
            if prec_clean.isdigit() and int(prec_clean) > 0:
                return "Float"
        return "Integer"
    return _CANONICAL_VARIABLE_TYPES.get(key, cleaned)


def _sanitize_unit_oid(symbol_or_name: str) -> str:
    """Build a deterministic ODM MeasurementUnit OID from a unit name or symbol."""
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol_or_name.strip()).strip("_")
    return f"MU.{token or 'UNIT'}"


def normalize_measurement_units(extra_attrs: dict[str, Any] | None) -> list[dict[str, str]]:
    """Extract and normalize measurement unit definitions from extraAttributes.

    Args:
        extra_attrs: Dictionary of extra attributes on Metadata or Variable.

    Returns:
        List of normalized unit dictionaries containing 'oid', 'name', and 'symbol'.
    """
    if not extra_attrs:
        return []

    units: list[dict[str, str]] = []
    seen_oids: set[str] = set()

    def _add_unit(item: Any) -> None:
        if isinstance(item, dict):
            raw_oid = str(item.get("oid") or item.get("OID") or "").strip()
            raw_name = str(item.get("name") or item.get("Name") or "").strip()
            raw_symbol = str(item.get("symbol") or item.get("Symbol") or "").strip()
            basis = raw_symbol or raw_name or raw_oid
            if not basis:
                return
            oid = raw_oid or _sanitize_unit_oid(basis)
            name = raw_name or raw_symbol or oid
            symbol = raw_symbol or raw_name or oid
            if oid not in seen_oids:
                seen_oids.add(oid)
                units.append({"oid": oid, "name": name, "symbol": symbol})
        elif isinstance(item, str):
            val = item.strip()
            if not val:
                return
            oid = _sanitize_unit_oid(val)
            if oid not in seen_oids:
                seen_oids.add(oid)
                units.append({"oid": oid, "name": val, "symbol": val})

    raw_mu = extra_attrs.get("measurementUnits")
    if isinstance(raw_mu, list):
        for entry in raw_mu:
            _add_unit(entry)
    elif raw_mu is not None:
        _add_unit(raw_mu)

    for alias_key in ("measurementUnit", "units", "unit"):
        alias_val = extra_attrs.get(alias_key)
        if isinstance(alias_val, list):
            for entry in alias_val:
                _add_unit(entry)
        elif alias_val is not None:
            _add_unit(alias_val)

    return units


def normalize_variable_bounds(extra_attrs: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract and normalize validation bounds or ODM RangeChecks from extraAttributes.

    Args:
        extra_attrs: Variable extraAttributes dictionary.

    Returns:
        List of normalized range check dictionaries ready for ODM serialization.
    """
    if not extra_attrs:
        return []

    raw_checks = extra_attrs.get("rangeChecks")
    if isinstance(raw_checks, list) and raw_checks:
        normalized_checks: list[dict[str, Any]] = []
        for rc in raw_checks:
            if not isinstance(rc, dict):
                continue
            entry: dict[str, Any] = {
                "softHard": str(rc.get("softHard") or "Soft").strip() or "Soft",
            }
            if comp := rc.get("comparator"):
                entry["comparator"] = str(comp).strip()
            if "checkValues" in rc and isinstance(rc["checkValues"], list):
                vals = [str(v).strip() for v in rc["checkValues"] if str(v).strip()]
                if vals:
                    entry["checkValues"] = vals
            elif (cv := rc.get("checkValue")) is not None and str(cv).strip():
                entry["checkValue"] = str(cv).strip()
            if (fe := rc.get("formalExpression")) is not None and str(fe).strip():
                entry["formalExpression"] = str(fe).strip()
            if (ctx := rc.get("context")) is not None and str(ctx).strip():
                entry["context"] = str(ctx).strip()
            if (mu_oid := rc.get("measurementUnitOid")) is not None and str(mu_oid).strip():
                entry["measurementUnitOid"] = str(mu_oid).strip()
            if (err := rc.get("errorMessage")) is not None and str(err).strip():
                entry["errorMessage"] = str(err).strip()
            normalized_checks.append(entry)
        if normalized_checks:
            return normalized_checks

    bounds: list[dict[str, Any]] = []
    min_val: Any = None
    for k in ("textValidationMin", "minValue", "min", "minimum"):
        if k in extra_attrs and extra_attrs[k] is not None and str(extra_attrs[k]).strip():
            min_val = str(extra_attrs[k]).strip()
            break
    if min_val is not None:
        bounds.append({"comparator": "GE", "softHard": "Soft", "checkValue": min_val})

    max_val: Any = None
    for k in ("textValidationMax", "maxValue", "max", "maximum"):
        if k in extra_attrs and extra_attrs[k] is not None and str(extra_attrs[k]).strip():
            max_val = str(extra_attrs[k]).strip()
            break
    if max_val is not None:
        bounds.append({"comparator": "LE", "softHard": "Soft", "checkValue": max_val})

    return bounds


def _is_file_target(target: Any) -> bool:
    """Determine whether the target was intended as a file path.

    Args:
        target: Target object to inspect.

    Returns:
        True if target is considered a file path, False otherwise.
    """
    if isinstance(target, Path):
        return True
    if isinstance(target, str):
        if "\n" in target or "\r" in target:
            return False
        target_str = target.strip()
        if target_str.startswith(("<", "{", "[")):
            return False
        if target_str.lower().endswith((".json", ".xml", ".csv")):
            return True
        if "/" in target_str or "\\" in target_str:
            return True
    return False


def _safe_is_file(p: Path) -> bool:
    """Safely check if a Path is an existing file, handling OSErrors.

    Args:
        p: Path object to check.

    Returns:
        True if path is an existing regular file, False otherwise.
    """
    try:
        return p.is_file()
    except OSError:
        return False
