"""Unit tests for security."""

import pytest

from imednet.errors import ClientError, PathTraversalValidationError
from imednet.utils.security import (
    SensitivityRegistry,
    _normalize_key,
    _normalize_key_cached,
    _normalize_key_uncached,
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


def test_normalize_key_cache_hits():
    """Verify that repeated field key normalizations hit the LRU cache."""
    _normalize_key.cache_clear()
    info_start = _normalize_key.cache_info()
    assert info_start.hits == 0
    assert info_start.misses == 0
    assert info_start.maxsize == 4096

    # First call: cache miss
    result1 = _normalize_key("patientName")
    assert result1 == "patient_name"
    assert _normalize_key.cache_info().misses == 1
    assert _normalize_key.cache_info().hits == 0

    # Second call with identical key: cache hit
    result2 = _normalize_key("patientName")
    assert result2 == "patient_name"
    assert _normalize_key.cache_info().misses == 1
    assert _normalize_key.cache_info().hits == 1

    # Multiple clinical keys in loop
    keys = ["patientName", "DOB", "dateOfBirth", "patientName", "DOB", "patientName"]
    for k in keys:
        _normalize_key(k)
    info = _normalize_key.cache_info()
    assert info.misses == 3
    assert info.hits == 5


def test_normalize_key_equivalence():
    """Verify equivalence between cached and uncached normalization across clinical keys."""
    sample_keys = [
        "patient_name",
        "patientName",
        "PatientName",
        "PATIENT_NAME",
        "patient-name",
        "patient.name",
        "DOB",
        "SSN",
        "dateOfBirth",
        "DATE_OF_BIRTH",
        "subject_key",
        "record_id",
        123,
        "",
        None,
        "  custom_field_name  ",
    ]
    for key in sample_keys:
        assert _normalize_key(key) == _normalize_key_uncached(key)
        assert _normalize_key(key) == _normalize_key_cached(key)


def test_normalize_key_unhashable_fallback():
    """Verify that unhashable keys fall back to uncached normalization without TypeError."""
    unhashable_keys = [
        ["patient", "name"],
        {"field": "patientName"},
        {"sub": 1},
        {"dob"},
    ]
    for key in unhashable_keys:
        norm = _normalize_key(key)
        expected = _normalize_key_uncached(key)
        assert norm == expected


def test_normalize_key_cache_clear():
    """Verify that cache_clear properly clears cached items."""
    _normalize_key("clinical_test_key_1")
    _normalize_key("clinical_test_key_2")
    assert _normalize_key.cache_info().currsize >= 2
    _normalize_key.cache_clear()
    assert _normalize_key.cache_info().currsize == 0
    assert _normalize_key.cache_info().hits == 0
    assert _normalize_key.cache_info().misses == 0


def test_sensitivity_registry_with_unhashable_and_repeated_keys():
    """Verify SensitivityRegistry handles unhashable and repeated keys safely."""
    registry = SensitivityRegistry()
    assert not registry.is_sensitive(["unhashable_key"])
    assert not registry.is_sensitive({"nested": "dict"})
    registry.add_sensitive_key("patientName")
    assert registry.is_sensitive("patientName")
    assert registry.is_sensitive("patient_name")


def test_spi_utils_reexports_sensitivity_helpers():
    """Verify extension-facing imednet.spi.utils re-exports core PHI registry and masker."""
    from imednet.spi import utils as spi_utils
    from imednet.utils import security as core_security

    assert spi_utils.SensitivityRegistry is core_security.SensitivityRegistry
    assert spi_utils.global_sensitivity_registry is core_security.global_sensitivity_registry
    assert spi_utils.mask_clinical_phi is core_security.mask_clinical_phi


def test_sensitivity_registry_is_exempt_and_custom_registry_masking():
    """Verify is_exempt checks and isolated custom registry masking without mutating global state."""
    from imednet.utils.security import global_sensitivity_registry

    custom_registry = SensitivityRegistry()
    assert custom_registry.is_exempt("subject_key")
    assert custom_registry.is_exempt("SubjectKey")
    assert custom_registry.is_exempt("study_key")
    assert custom_registry.is_exempt("record_id")
    assert custom_registry.is_exempt("RecordID")
    assert not custom_registry.is_exempt("patientName")
    assert not custom_registry.is_exempt("")
    assert not custom_registry.is_exempt(None)

    custom_registry.add_sensitive_key("chartCode")
    payload = {
        "chart_code": "CH-001",
        "subjectKey": "SUBJ-01",
        "nested_tuple": ({"chartCode": "CH-002", "vital": 98.6},),
    }

    masked_custom = mask_clinical_phi(payload, registry=custom_registry)
    assert masked_custom["chart_code"] == "***MASKED***"
    assert masked_custom["subjectKey"] == "SUBJ-01"
    assert masked_custom["nested_tuple"][0]["chartCode"] == "***MASKED***"
    assert masked_custom["nested_tuple"][0]["vital"] == 98.6
    assert not global_sensitivity_registry.is_sensitive("chartCode")


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "BRTHDTC",
        "brthdtc",
        "brthDtc",
        "BrthDtc",
        "brth_dtc",
        "BRTHDAT",
        "brthdat",
        "brthDat",
        "BrthDat",
        "brth_dat",
        "BRTHYR",
        "brthyr",
        "brthYr",
        "BrthYr",
        "brth_yr",
        "SUBJINIT",
        "subjinit",
        "subjInit",
        "SubjInit",
        "subj_init",
    ],
)
def test_sensitivity_registry_marks_cdisc_phi_keys_sensitive_across_casings(
    sensitive_key: str,
) -> None:
    """Verify SensitivityRegistry recognizes CDISC birth and initials variables across casings."""
    registry = SensitivityRegistry()
    assert registry.is_sensitive(sensitive_key)
    assert not registry.is_exempt(sensitive_key)


@pytest.mark.parametrize(
    "exempt_key",
    [
        "USUBJID",
        "usubjid",
        "uSubjId",
        "USubjId",
        "u_subj_id",
        "usubj_id",
        "SUBJID",
        "subjid",
        "subjId",
        "SubjId",
        "subj_id",
        "STUDYID",
        "studyid",
        "studyId",
        "StudyId",
        "study_id",
    ],
)
def test_sensitivity_registry_exempts_cdisc_identifiers_across_casings(exempt_key: str) -> None:
    """Verify SensitivityRegistry exempts USUBJID, SUBJID, and STUDYID across all casing styles."""
    registry = SensitivityRegistry()
    assert registry.is_exempt(exempt_key)
    assert not registry.is_sensitive(exempt_key)
    registry.add_sensitive_key(exempt_key)
    assert not registry.is_sensitive(exempt_key)


def test_mask_clinical_phi_preserves_masked_sentinel_and_cdisc_exemptions() -> None:
    """Verify generic mask_clinical_phi emits ***MASKED*** for CDISC PHI while keeping IDs intact."""
    payload = {
        "STUDYID": "STUDY-01",
        "USUBJID": "STUDY-01-001",
        "subjId": "001",
        "BRTHDTC": "1985-07-19",
        "brthDat": "1985-07-19",
        "brth_yr": 1985,
        "SubjInit": "JD",
    }
    masked = mask_clinical_phi(payload)
    assert masked["STUDYID"] == "STUDY-01"
    assert masked["USUBJID"] == "STUDY-01-001"
    assert masked["subjId"] == "001"
    assert masked["BRTHDTC"] == "***MASKED***"
    assert masked["brthDat"] == "***MASKED***"
    assert masked["brth_yr"] == "***MASKED***"
    assert masked["SubjInit"] == "***MASKED***"


def test_sensitivity_registry_resyncs_collapsed_sets_after_direct_set_restore() -> None:
    """Verify snapshot/clear/update patterns on _sensitive_keys and _exempt_keys resync collapsed sets."""
    registry = SensitivityRegistry()
    sens_snapshot = set(registry._sensitive_keys)
    exempt_snapshot = set(registry._exempt_keys)

    registry.add_sensitive_key("subject_oid")
    registry.add_exempt_key("custom_safe_id")
    assert registry.is_sensitive("subjectOid")
    assert registry.is_exempt("CUSTOMSAFEID")

    registry._sensitive_keys.clear()
    registry._sensitive_keys.update(sens_snapshot)
    registry._exempt_keys.clear()
    registry._exempt_keys.update(exempt_snapshot)

    assert not registry.is_sensitive("subject_oid")
    assert not registry.is_sensitive("subjectOid")
    assert not registry.is_exempt("custom_safe_id")
    assert not registry.is_exempt("CUSTOMSAFEID")


def test_sanitize_diagnostic_text_masks_url_plain_filters_and_multiline_address_json() -> None:
    """Mask PHI across URL filters, plain filter operators, multiline JSON, and custom registries."""
    from imednet.utils.security import (
        global_sensitivity_registry,
        sanitize_diagnostic_payload,
        sanitize_diagnostic_text,
    )

    custom_registry = SensitivityRegistry()
    custom_registry.add_sensitive_key("medicalChartId")
    custom_registry.add_sensitive_key("subject_key")  # Exempt key must remain unmasked

    # 1. Plain filter operators: ==, !=, =~, <=, >=, <, >, =, :
    plain_filters = (
        "Filter failure study_key==STUDY-100;subject_key==SUBJ-200;record_id=300;"
        "usubjid==USUBJ-400;subjid:SUBJID-500;studyid==STUDYID-600;"
        'ssn==123-45-6789;patientName!="Jane Doe";email=~jane.doe@example.com;'
        "dob>=1980-01-01;dob<=1990-12-31;brthyr>1975;brthyr<1995;"
        "medicalChartId: CHART-9988"
    )
    sanitized_plain = sanitize_diagnostic_text(plain_filters, registry=custom_registry)
    assert sanitized_plain.count("***MASKED***") >= 8
    for leaked in (
        "123-45-6789",
        "Jane Doe",
        "jane.doe@example.com",
        "1980-01-01",
        "1990-12-31",
        "1975",
        "1995",
        "CHART-9988",
    ):
        assert leaked not in sanitized_plain
    for preserved in (
        "STUDY-100",
        "SUBJ-200",
        "300",
        "USUBJ-400",
        "SUBJID-500",
        "STUDYID-600",
    ):
        assert preserved in sanitized_plain

    # 2. Multiline "address" JSON (valid outer JSON, key-prefixed fragment, and multiline string)
    multiline_json_log = """Validation error payload={
  "study_key": "STUDY-100",
  "subject_key": "SUBJ-200",
  "record_id": 300,
  "usubjid": "USUBJ-400",
  "subjid": "SUBJID-500",
  "studyid": "STUDYID-600",
  "patientName": "Homer Simpson",
  "ssn": "123-45-6789",
  "email": "homer@example.com",
  "medical_chart_id": "CHART-1122",
  "address": {
    "street": "742 Evergreen Terrace",
    "city": "Springfield",
    "postalCode": "62704"
  }
}"""
    sanitized_json = sanitize_diagnostic_text(multiline_json_log, registry=custom_registry)
    assert '"address": "***MASKED***"' in sanitized_json
    for leaked in (
        "Homer Simpson",
        "123-45-6789",
        "homer@example.com",
        "CHART-1122",
        "742 Evergreen Terrace",
        "Springfield",
        "62704",
    ):
        assert leaked not in sanitized_json
    for preserved in ("STUDY-100", "SUBJ-200", "USUBJ-400", "SUBJID-500", "STUDYID-600"):
        assert preserved in sanitized_json

    fragment_and_multiline_str = (
        'Fragment "address": {\n  "street": "221B Baker St",\n  "city": "London"\n} '
        'and multiline "address": "1600 Pennsylvania Ave\nWashington, DC"'
    )
    sanitized_frag = sanitize_diagnostic_text(fragment_and_multiline_str, registry=custom_registry)
    assert "221B Baker St" not in sanitized_frag
    assert "London" not in sanitized_frag
    assert "1600 Pennsylvania Ave" not in sanitized_frag
    assert sanitized_frag.count("***MASKED***") == 2

    # 3. URL filter & recordDataFilter alongside credentials
    url_log = (
        "GET https://edc.example.com/api/v1/edc/studies/STUDY-100/records"
        "?api_key=sec-999&filter=ssn%3D%3D123-45-6789%3BsubjectKey%3D%3DSUBJ-200"
        "&recordDataFilter=patientName%3D%3D%22Jane+Doe%22%3Bemail%3D~jane%40example.com "
        "Authorization: Bearer tok-abc"  # pragma: allowlist secret
    )
    sanitized_url_log = sanitize_diagnostic_text(url_log, registry=custom_registry)
    assert "api_key=***" in sanitized_url_log
    assert "Authorization: ***" in sanitized_url_log
    assert "***MASKED***" in sanitized_url_log
    assert "SUBJ-200" in sanitized_url_log
    for leaked in ("sec-999", "tok-abc", "123-45-6789", "Jane", "Doe", "jane"):
        assert leaked not in sanitized_url_log

    # 4. Indirect field target dict and global registry isolation
    indirect = {
        "error": [
            {"variable": "ssn", "value": "123-45-6789"},
            {"variable": "subject_key", "value": "SUBJ-200"},
        ]
    }
    sanitized_indirect = sanitize_diagnostic_payload(indirect, registry=custom_registry)
    assert sanitized_indirect["error"][0]["value"] == "***MASKED***"
    assert sanitized_indirect["error"][1]["value"] == "SUBJ-200"
    assert not global_sensitivity_registry.is_sensitive("medicalChartId")


def test_passphrase_standalone_jwt_and_clinical_eyj_identifier_guards() -> None:
    """is_sensitive_key and redact_sensitive_payload scrub passphrases and valid JWTs while preserving clinical eyJ IDs."""
    from imednet.utils.secrets import is_sensitive_key, redact_sensitive_payload
    from imednet.utils.url import redact_sensitive_text, redact_url_query

    for secret_key in ("passphrase", "passphrases", "ssh_passphrase", "pgpPassphrase"):
        assert is_sensitive_key(secret_key), secret_key
    for benign_key in (
        "passphrase_required",
        "passphrase_id",
        "passphrase_length",
        "passphrase_enabled",
    ):
        assert not is_sensitive_key(benign_key), benign_key

    unsigned_jwt = (
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."  # pragma: allowlist secret
        "eyJzdWIiOiJjYW5hcnktbWFya2VyIn0.canary_jwt_marker"
    )
    jwe_token = (
        "eyJhbGciOiJSU0EtT0FFUCIsImVuYyI6IkEyNTZHQ00ifQ."  # pragma: allowlist secret
        "encrypted_key.iv_seg.cipher_seg.auth_tag"
    )
    clinical_eyj_ids = (
        "eyJ123.456.789",
        "eyJ_SUBJ_01.VISIT_1.FORM_2",
        "eyJbe10.payload.sig",  # base64url of non-JSON-object bytes
        "eyJh.payload.sig",  # invalid base64url length (% 4 == 1)
    )

    payload = {
        "passphrase": "CANARY_PASSPHRASE_MARKER",  # pragma: allowlist secret
        "ssh_passphrase": "CANARY_SSH_PASSPHRASE",  # pragma: allowlist secret
        "passphrase_required": True,
        "indirect": {
            "variableName": "passphrase",
            "value": "CANARY_INDIRECT_PASSPHRASE",  # pragma: allowlist secret
        },
        "assertion": unsigned_jwt,
        "jwe": jwe_token,
        "inline": (
            f"Auth failed passphrase=CANARY_INLINE_PASSPHRASE "
            f"assertion {unsigned_jwt} jwe={jwe_token} " + " ".join(clinical_eyj_ids)
        ),
    }
    redacted = redact_sensitive_payload(payload)
    assert redacted["passphrase"] == "***"
    assert redacted["ssh_passphrase"] == "***"
    assert redacted["passphrase_required"] is True
    assert redacted["indirect"]["value"] == "***"
    assert redacted["assertion"] == "***"
    assert redacted["jwe"] == "***"
    for secret_marker in (
        "CANARY_PASSPHRASE_MARKER",
        "CANARY_SSH_PASSPHRASE",
        "CANARY_INDIRECT_PASSPHRASE",
        "CANARY_INLINE_PASSPHRASE",
        "canary_jwt_marker",
        "cipher_seg",
    ):
        assert secret_marker not in str(redacted)
    for clinical_id in clinical_eyj_ids:
        assert clinical_id in redacted["inline"]

    url_with_passphrase = "https://edc.example.com/export?passphrase=CANARY_URL_PASSPHRASE&study_key=STUDY-1"  # pragma: allowlist secret
    assert "CANARY_URL_PASSPHRASE" not in redact_url_query(url_with_passphrase)
    assert "passphrase=%2A%2A%2A" in redact_url_query(url_with_passphrase) or (
        "passphrase=***" in redact_url_query(url_with_passphrase)
    )
    uri_err = "Sink error (postgresql://svc_user:CANARY_URI_PASS@127.0.0.1:5432/edc);"  # pragma: allowlist secret
    scrubbed_uri_err = redact_sensitive_text(uri_err)
    assert "CANARY_URI_PASS" not in scrubbed_uri_err
    assert "postgresql://svc_user:***@127.0.0.1:5432/edc" in scrubbed_uri_err

    empty_user_uri = (
        "Cache error redis://:CANARY_REDIS_PASS@127.0.0.1:6379/0"  # pragma: allowlist secret
    )
    scrubbed_redis = redact_sensitive_text(empty_user_uri)
    assert "CANARY_REDIS_PASS" not in scrubbed_redis
    assert "redis://:***@127.0.0.1:6379/0" in scrubbed_redis

    import base64 as _b64

    deep_json = ('{"a":' * 100_000) + "1" + ("}" * 100_000)
    deep_header = _b64.urlsafe_b64encode(deep_json.encode("utf-8")).decode("ascii").rstrip("=")
    deep_token = f"{deep_header}.payload.sig"
    assert redact_sensitive_payload(deep_token) == deep_token


def test_resolve_study_sensitivity_registry_commits_exemptions_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """resolve_study_sensitivity_registry loads StudyConfiguration.phi_fields via SPI and fails closed on errors."""
    import json
    from pathlib import Path
    from unittest.mock import MagicMock

    assert isinstance(tmp_path, Path)

    from imednet.integrations.enrichment import StudyConfiguration
    from imednet.spi.utils import (
        resolve_study_sensitivity_registry as spi_resolve_study_registry,
    )
    from imednet.utils.security import (
        global_sensitivity_registry,
        resolve_study_sensitivity_registry,
        sanitize_diagnostic_payload,
        sanitize_diagnostic_text,
    )
    from imednet_workflows.config_version_control import ConfigVersionStore

    assert spi_resolve_study_registry is resolve_study_sensitivity_registry

    db_file = tmp_path / "study_gov.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_file))
    monkeypatch.setenv(
        "IMEDNET_ENCRYPTION_KEY",
        "test-study-registry-key",  # pragma: allowlist secret
    )
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)

    admin_sdk = MagicMock()
    admin_sdk.auth.get_user_roles.return_value = ["admin"]
    admin_sdk.auth.get_user_id.return_value = "sec-admin"

    store = ConfigVersionStore(db_path=db_file)
    study_cfg = StudyConfiguration(
        study_key="STUDY_CANARY",
        phi_fields=[
            "medicalChartId",
            "study_key",
            "subject_key",
            "record_id",
            "usubjid",
            "subjid",
            "studyid",
        ],
    )
    store.commit_config(
        "STUDY_CANARY", study_cfg, "Register custom Study PHI Variable", sdk=admin_sdk
    )

    reg = resolve_study_sensitivity_registry("STUDY_CANARY")
    assert reg is not global_sensitivity_registry
    assert reg.is_sensitive("medicalChartId")
    assert reg.is_sensitive("medical_chart_id")
    for exempt_key in ("study_key", "subject_key", "record_id", "usubjid", "subjid", "studyid"):
        assert not reg.is_sensitive(exempt_key), exempt_key
    assert not global_sensitivity_registry.is_sensitive("medicalChartId")

    # Blank, None, UNKNOWN, and unconfigured studies return global_sensitivity_registry
    assert resolve_study_sensitivity_registry(None) is global_sensitivity_registry
    assert resolve_study_sensitivity_registry("   ") is global_sensitivity_registry
    assert resolve_study_sensitivity_registry("UNKNOWN") is global_sensitivity_registry
    assert resolve_study_sensitivity_registry("UNCONFIGURED_STUDY") is global_sensitivity_registry

    # Composite diagnostic text and payload scrub all 8 canary classes while keeping structural IDs
    m_jwt = (
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."  # pragma: allowlist secret
        "eyJzdWIiOiJjYW5hcnktbWFya2VyIn0.canary_jwt_marker"
    )
    composite_err = (
        "HTTP 502 Export failed for postgresql://svc_user:CANARY_URI_USERINFO_MARKER@127.0.0.1:5432/edc "  # pragma: allowlist secret
        "api_key=CANARY_API_KEY_MARKER Authorization: Bearer CANARY_BEARER_MARKER "  # pragma: allowlist secret
        f"passphrase=CANARY_PASSPHRASE_MARKER assertion {m_jwt} "
        'ssn=CANARY_DEFAULT_PHI_SSN patientName="CANARY_DEFAULT_PHI_NAME" '
        "medicalChartId=CANARY_CUSTOM_PHI_CHART subject_key=SUBJ-42 record_id=108"
    )
    scrubbed_err = sanitize_diagnostic_text(composite_err, registry=reg)
    assert "HTTP 502 Export failed" in scrubbed_err
    assert "postgresql://svc_user:***@127.0.0.1:5432/edc" in scrubbed_err
    assert "SUBJ-42" in scrubbed_err
    assert "108" in scrubbed_err
    for marker in (
        "CANARY_URI_USERINFO_MARKER",
        "CANARY_API_KEY_MARKER",
        "CANARY_BEARER_MARKER",
        "CANARY_PASSPHRASE_MARKER",
        m_jwt,
        "canary_jwt_marker",
        "CANARY_DEFAULT_PHI_SSN",
        "CANARY_DEFAULT_PHI_NAME",
        "CANARY_CUSTOM_PHI_CHART",
    ):
        assert marker not in scrubbed_err

    sample_meta = {
        "study_key": "STUDY_CANARY",
        "subject_key": "SUBJ-42",
        "ssn": "CANARY_DEFAULT_PHI_SSN",
        "api_key": "CANARY_API_KEY_MARKER",  # pragma: allowlist secret
        "passphrase": "CANARY_PASSPHRASE_MARKER",  # pragma: allowlist secret
        "destination_uri": "postgresql://svc_user:CANARY_URI_USERINFO_MARKER@127.0.0.1:5432/edc",  # pragma: allowlist secret
        "assertion": m_jwt,
        "medicalChartId": "CANARY_CUSTOM_PHI_CHART",
        "note": "ssn=CANARY_DEFAULT_PHI_SSN medicalChartId=CANARY_CUSTOM_PHI_CHART",
    }
    scrubbed_meta = sanitize_diagnostic_payload(sample_meta, registry=reg)
    assert scrubbed_meta["study_key"] == "STUDY_CANARY"
    assert scrubbed_meta["subject_key"] == "SUBJ-42"
    for marker in (
        "CANARY_URI_USERINFO_MARKER",
        "CANARY_API_KEY_MARKER",
        "CANARY_PASSPHRASE_MARKER",
        m_jwt,
        "canary_jwt_marker",
        "CANARY_DEFAULT_PHI_SSN",
        "CANARY_CUSTOM_PHI_CHART",
    ):
        assert marker not in json.dumps(scrubbed_meta)

    # Decryption failure or resolver error fails closed to global_sensitivity_registry
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    assert resolve_study_sensitivity_registry("STUDY_CANARY") is global_sensitivity_registry
    assert (
        resolve_study_sensitivity_registry(
            "STUDY_CANARY",
            resolver=lambda _sk: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        is global_sensitivity_registry
    )
