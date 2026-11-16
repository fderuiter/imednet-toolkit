"""Unit tests for security."""

import pytest

from imednet.errors import ClientError, PathTraversalValidationError
from imednet.utils.security import (
    SensitivityRegistry,
    _normalize_key,
    mask_clinical_phi,
    sanitize_csv_formula,
    validate_header_value,
    validate_partition_key,
)


@pytest.mark.parametrize(
    ("input_val", "expected"),
    [
        # Unsafe inputs (direct)
        ("=SUM(A1:A2)", "'=SUM(A1:A2)"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("@foo", "'@foo"),
        # Unsafe inputs (whitespace bypass)
        ("   =cmd", "'   =cmd"),
        ("\t+1", "'\t+1"),
        ("\n-1", "'\n-1"),
        # Safe inputs
        ("hello", "hello"),
        ("123", "123"),
        ("", ""),
        (None, None),
        (1, 1),
        (1.5, 1.5),
        (True, True),
        # Mixed (safe)
        ("foo=bar", "foo=bar"),
        (" foo=bar", " foo=bar"),
    ],
)
def test_sanitize_csv_formula(input_val, expected):
    """Test that sanitize csv formula."""
    assert sanitize_csv_formula(input_val) == expected


@pytest.mark.parametrize(
    "input_val",
    [
        "application/json",
        "Bearer my-token-123",
        "Mozilla/5.0",
        "en-US,en;q=0.5",
        "",
        "custom_header_value",
    ],
)
def test_validate_header_value_valid(input_val):
    """Test that validate header value valid."""
    validate_header_value(input_val)


@pytest.mark.parametrize(
    "input_val",
    [
        "invalid\nvalue",
        "invalid\rvalue",
        "invalid\r\nvalue",
        "\n",
        "\r",
        "Bearer \nmy-token",
        "Bearer my-token\n",
    ],
)
def test_validate_header_value_invalid(input_val):
    """Test that validate header value invalid."""
    with pytest.raises(ClientError, match="Header value must not contain newlines"):
        validate_header_value(input_val)


@pytest.mark.parametrize(
    ("input_val", "expected"),
    [
        (["=cmd", "safe"], ["'=cmd", "safe"]),
        (("unsafe", "+1"), ("unsafe", "'+1")),
        ([["-nested"], "@also_nested"], [["'-nested"], "'@also_nested"]),
    ],
)
def test_sanitize_csv_formula_collections(input_val, expected):
    """Test that sanitize csv formula collections."""
    assert sanitize_csv_formula(input_val) == expected


@pytest.mark.parametrize("key", ["STUDY_A", "study-001", "visit 1"])
def test_validate_partition_key_valid(key: str) -> None:
    """Test that validate partition key valid."""
    validate_partition_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "../study",
        "..\\study",
        "/rooted",
        "\\rooted",
        "C:\\Windows",
        "C:/Windows",
        "study/form",
        "study\\form",
        "study\x00key",
        "..",
    ],
)
def test_validate_partition_key_invalid(key: str) -> None:
    """Test that validate partition key invalid."""
    with pytest.raises(PathTraversalValidationError):
        validate_partition_key(key)


@pytest.mark.parametrize(
    ("input_key", "expected"),
    [
        ("patient_name", "patient_name"),
        ("patientName", "patient_name"),
        ("PatientName", "patient_name"),
        ("PATIENT_NAME", "patient_name"),
        ("patient-name", "patient_name"),
        ("patient name", "patient_name"),
        ("patient.name", "patient_name"),
        ("  patientName  ", "patient_name"),
        ("DOB", "dob"),
        ("SSN", "ssn"),
        ("patientSSN", "patient_ssn"),
        ("patientSSNNumber", "patient_ssn_number"),
        ("dateOfBirth", "date_of_birth"),
        ("DateOfBirth", "date_of_birth"),
        ("DATE_OF_BIRTH", "date_of_birth"),
        ("subject_key", "subject_key"),
        ("SubjectKey", "subject_key"),
        ("record_id", "record_id"),
        ("RecordID", "record_id"),
        ("RECORD_ID", "record_id"),
        ("recordId", "record_id"),
        ("RecordId", "record_id"),
        ("study_key", "study_key"),
        ("StudyKey", "study_key"),
        (123, "123"),
        ("", ""),
        (None, ""),
        ("___", ""),
        ("patient---name", "patient_name"),
        (" patient_name ", "patient_name"),
    ],
)
def test_normalize_key(input_key, expected):
    """Test key normalization for various casing and delimiter styles."""
    assert _normalize_key(input_key) == expected


def test_sensitivity_registry_default_keys_casing():
    """Test SensitivityRegistry handles default sensitive and exempt keys with mixed casing."""
    registry = SensitivityRegistry()

    # Sensitive keys
    assert registry.is_sensitive("DOB")
    assert registry.is_sensitive("dob")
    assert registry.is_sensitive("SSN")
    assert registry.is_sensitive("ssn")
    assert registry.is_sensitive("patientName")
    assert registry.is_sensitive("PatientName")
    assert registry.is_sensitive("patient_name")
    assert registry.is_sensitive("dateOfBirth")
    assert registry.is_sensitive("DateOfBirth")
    assert registry.is_sensitive("PatientInitials")
    assert registry.is_sensitive("patient_initials")
    assert registry.is_sensitive("email")
    assert registry.is_sensitive("EMAIL")
    assert registry.is_sensitive("Phone")

    # Exempt keys must return False even if casing differs
    assert not registry.is_sensitive("subject_key")
    assert not registry.is_sensitive("SubjectKey")
    assert not registry.is_sensitive("record_id")
    assert not registry.is_sensitive("RecordID")
    assert not registry.is_sensitive("RecordId")
    assert not registry.is_sensitive("study_key")
    assert not registry.is_sensitive("StudyKey")

    # Non-sensitive keys
    assert not registry.is_sensitive("visit_date")
    assert not registry.is_sensitive("site_id")
    assert not registry.is_sensitive("")
    assert not registry.is_sensitive(None)


def test_sensitivity_registry_custom_keys():
    """Test adding and removing sensitive and exempt keys with mixed casing."""
    registry = SensitivityRegistry()

    # Add sensitive key in camelCase
    registry.add_sensitive_key("medicalRecordNumber")
    assert registry.is_sensitive("medicalRecordNumber")
    assert registry.is_sensitive("medical_record_number")
    assert registry.is_sensitive("MEDICAL_RECORD_NUMBER")

    # Remove sensitive key in snake_case
    registry.remove_sensitive_key("medical_record_number")
    assert not registry.is_sensitive("medicalRecordNumber")

    # Exempt key overrides sensitive key
    registry.add_sensitive_key("customIdentifier")
    assert registry.is_sensitive("customIdentifier")
    registry.add_exempt_key("customIdentifier")
    assert not registry.is_sensitive("customIdentifier")
    assert not registry.is_sensitive("custom_identifier")

    # Remove exempt key
    registry.remove_exempt_key("CustomIdentifier")
    # Now it is sensitive again because it was in _sensitive_keys
    assert registry.is_sensitive("custom_identifier")

    # Adding an exempt key to sensitive does not override exemption
    registry.add_exempt_key("sampleId")
    registry.add_sensitive_key("sampleId")
    assert not registry.is_sensitive("sampleId")


def test_mask_clinical_phi_nested_mixed_case():
    """Test mask_clinical_phi on deeply nested dictionaries with mixed-case keys."""
    data = {
        "patientName": "Jane Doe",
        "DOB": "1990-01-01",
        "SubjectKey": "SUBJ-101",
        "RecordID": "REC-001",
        "details": {
            "SSN": "123-45-6789",
            "nested_list": [
                {"email": "test@example.com", "study_key": "STUDY-01"},
                {"DateOfBirth": "1995-05-05", "safe_val": 42},
            ],
            123: "non_string_key",
        },
        "safe_field": "unmasked",
    }

    masked = mask_clinical_phi(data)

    assert masked["patientName"] == "***MASKED***"
    assert masked["DOB"] == "***MASKED***"
    assert masked["SubjectKey"] == "SUBJ-101"
    assert masked["RecordID"] == "REC-001"
    assert masked["details"]["SSN"] == "***MASKED***"
    assert masked["details"]["nested_list"][0]["email"] == "***MASKED***"
    assert masked["details"]["nested_list"][0]["study_key"] == "STUDY-01"
    assert masked["details"]["nested_list"][1]["DateOfBirth"] == "***MASKED***"
    assert masked["details"]["nested_list"][1]["safe_val"] == 42
    assert masked["details"][123] == "non_string_key"
    assert masked["safe_field"] == "unmasked"
