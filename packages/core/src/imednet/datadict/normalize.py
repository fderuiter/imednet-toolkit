"""Field normalization and date policy for data dictionary."""

from __future__ import annotations

import re
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
