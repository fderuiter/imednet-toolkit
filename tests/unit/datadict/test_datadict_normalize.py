"""Tests for data dictionary normalization and date policy."""

from __future__ import annotations

import pytest

from imednet.datadict.normalize import (
    normalize_row_data,
    to_camel_case,
    validate_timestamp,
)


def test_to_camel_case() -> None:
    """Test strict lexical camelCasing without semantic renames."""
    assert to_camel_case("Patient Record Report") == "patientRecordReport"
    assert to_camel_case("Special Patient ID") == "specialPatientId"
    assert to_camel_case("Special Patient Id") == "specialPatientId"
    assert to_camel_case("Form Key") == "formKey"
    assert to_camel_case("Form ID") == "formId"
    assert to_camel_case("BUSINESS_LOGIC") == "businessLogic"
    assert to_camel_case("variableName") == "variableName"
    assert to_camel_case("Form") == "form"
    assert to_camel_case("choice_text") == "choiceText"
    assert to_camel_case("Required Field") == "requiredField"
    assert to_camel_case("") == ""


def test_validate_timestamp_valid() -> None:
    """Test valid MM-DD-YYYY timestamp validation."""
    assert validate_timestamp("09-28-2026") == "09-28-2026"
    assert validate_timestamp("12-31-2025 15:30:00") == "12-31-2025 15:30:00"
    assert validate_timestamp("01-01-2024T12:00:00Z") == "01-01-2024T12:00:00Z"


def test_validate_timestamp_invalid() -> None:
    """Test rejection of non-MM-DD-YYYY formats."""
    with pytest.raises(ValueError, match="timestamps must adhere to MM-DD-YYYY format"):
        validate_timestamp("2026-09-28")

    with pytest.raises(ValueError, match="timestamps must adhere to MM-DD-YYYY format"):
        validate_timestamp("28-09-2026")

    with pytest.raises(ValueError, match="timestamps must adhere to MM-DD-YYYY format"):
        validate_timestamp("invalid-date")


def test_normalize_row_data() -> None:
    """Test normalizing row headers and isolating unknown attributes."""
    row = {
        "Form Key": "AE",
        "Form Name": "Adverse Event",
        "Custom Study Property": "Value123",
    }
    known_fields = {"formKey", "formName"}
    known, extra, warnings = normalize_row_data(row, known_fields, "FORMS.csv")

    assert known == {"formKey": "AE", "formName": "Adverse Event"}
    assert extra == {"customStudyProperty": "Value123"}
    assert len(warnings) == 1
    assert "Unknown column 'Custom Study Property'" in warnings[0]


def test_to_camel_case_with_bom() -> None:
    """Test that leading UTF-8 BOM is stripped and camelCase preserved."""
    assert to_camel_case("\ufeffformId") == "formId"
    assert to_camel_case("\ufeffpatientRecordReport") == "patientRecordReport"
    assert to_camel_case("\ufeffForm Key") == "formKey"


def test_validate_timestamp_calendar_bounds() -> None:
    """Test rejection of out-of-range calendar months and days."""
    for invalid in [
        "13-01-2026",
        "19-39-2026",
        "00-15-2026",
        "02-31-2026",
        "04-31-2026",
    ]:
        with pytest.raises(ValueError, match="timestamps must adhere to MM-DD-YYYY format"):
            validate_timestamp(invalid)
