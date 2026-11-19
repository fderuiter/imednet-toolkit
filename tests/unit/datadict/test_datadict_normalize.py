"""Tests for data dictionary normalization and date policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from imednet.datadict.normalize import (
    _is_file_target,
    _safe_is_file,
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


def test_is_file_target(tmp_path: Path) -> None:
    """Test file target discrimination helper."""
    # Path instance is always a file target
    assert _is_file_target(Path("something.csv")) is True
    assert _is_file_target(Path("foo/bar")) is True

    # Standard extensions
    assert _is_file_target("data.json") is True
    assert _is_file_target("study.xml") is True
    assert _is_file_target("dict.csv") is True
    assert _is_file_target("DATA.CSV") is True

    # Path delimiters
    assert _is_file_target("some/nested/path") is True
    assert _is_file_target("some\\nested\\path") is True

    # JSON structures
    assert _is_file_target('{"key": "value"}') is False
    assert _is_file_target('[{"key": "value"}]') is False

    # XML structures
    assert _is_file_target("<ODM>xml</ODM>") is False
    assert _is_file_target("  <Study/>  ") is False

    # Multiline text
    assert _is_file_target("line1\nline2.csv") is False
    assert _is_file_target("line1\rline2.xml") is False

    # Single line text without delimiters or standard extensions
    assert _is_file_target("single_column_name") is False
    assert _is_file_target("var1,var2,var3") is False

    # Non-string/non-path
    assert _is_file_target(12345) is False
    assert _is_file_target(None) is False


def test_safe_is_file(tmp_path: Path) -> None:
    """Test safe file check helper."""
    real_file = tmp_path / "exists.txt"
    real_file.write_text("hello", encoding="utf-8")
    assert _safe_is_file(real_file) is True

    fake_file = tmp_path / "does_not_exist.txt"
    assert _safe_is_file(fake_file) is False

    assert _safe_is_file(tmp_path) is False


def test_normalize_form_type_and_form_id() -> None:
    """Test canonical Subject formType and synthetic formId stripping."""
    from imednet.datadict.normalize import normalize_form_id, normalize_form_type

    assert normalize_form_type("Patient") == "Subject"
    assert normalize_form_type("subject") == "Subject"
    assert normalize_form_type("CRF") == "Subject"
    assert normalize_form_type("Site") == "Site"
    assert normalize_form_type("CustomType") == "CustomType"
    assert normalize_form_type(None, default="Subject") == "Subject"
    assert normalize_form_type("   ", default="Subject") == "Subject"

    assert normalize_form_id(None, "DM") is None
    assert normalize_form_id(True, "DM") is None
    assert normalize_form_id("DM", "DM") is None
    assert normalize_form_id("  ", "DM") is None
    assert normalize_form_id("1042", "DM") == 1042
    assert normalize_form_id(1042, "DM") == 1042
    assert normalize_form_id("EXT_DM_01", "DM") == "EXT_DM_01"


def test_normalize_variable_type() -> None:
    """Test canonical variableType alignment across CSV, ODM, REDCap, and SDK terms."""
    from imednet.datadict.normalize import normalize_variable_type

    assert normalize_variable_type(None) is None
    assert normalize_variable_type("   ") is None
    assert normalize_variable_type("Number") == "Integer"
    assert normalize_variable_type("Number", precision="0") == "Integer"
    assert normalize_variable_type("Number", precision="2") == "Float"
    assert normalize_variable_type("Numeric") == "Float"
    assert normalize_variable_type("RadioButton") == "Radio"
    assert normalize_variable_type("yesno") == "Radio"
    assert normalize_variable_type("dropdown") == "Single Select"
    assert normalize_variable_type("SingleSelect") == "Single Select"
    assert normalize_variable_type("checkbox") == "Multi Select"
    assert normalize_variable_type("MultiSelect") == "Multi Select"
    assert normalize_variable_type("memo") == "Text"
    assert normalize_variable_type("CustomWidget") == "CustomWidget"


def test_normalize_measurement_units_and_bounds() -> None:
    """Test extraction and deduplication of measurement units and variable bounds."""
    from imednet.datadict.normalize import normalize_measurement_units, normalize_variable_bounds

    assert normalize_measurement_units(None) == []
    assert normalize_variable_bounds(None) == []

    units = normalize_measurement_units(
        {
            "measurementUnits": [{"oid": "MU.KG", "name": "Kilograms", "symbol": "kg"}],
            "unit": "mmHg",
            "units": ["mmHg", "cm"],
        }
    )
    assert units == [
        {"oid": "MU.KG", "name": "Kilograms", "symbol": "kg"},
        {"oid": "MU.mmHg", "name": "mmHg", "symbol": "mmHg"},
        {"oid": "MU.cm", "name": "cm", "symbol": "cm"},
    ]

    bounds = normalize_variable_bounds({"textValidationMin": "70", "textValidationMax": "250"})
    assert bounds == [
        {"comparator": "GE", "softHard": "Soft", "checkValue": "70"},
        {"comparator": "LE", "softHard": "Soft", "checkValue": "250"},
    ]

    explicit_rc = normalize_variable_bounds(
        {
            "rangeChecks": [
                {
                    "comparator": "GT",
                    "softHard": "Hard",
                    "checkValue": "0",
                    "measurementUnitOid": "MU.KG",
                    "errorMessage": "Must be positive",
                }
            ]
        }
    )
    assert explicit_rc == [
        {
            "comparator": "GT",
            "softHard": "Hard",
            "checkValue": "0",
            "measurementUnitOid": "MU.KG",
            "errorMessage": "Must be positive",
        }
    ]
