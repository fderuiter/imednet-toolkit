"""Unit tests for url."""

import pytest

from imednet.errors.validation import PathTraversalValidationError
from imednet.utils.url import (
    build_safe_path,
    redact_url_query,
    sanitize_base_url,
    validate_path_segment,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x/api", "https://x"),
        ("https://x/api/", "https://x"),
        ("https://x/api/v1", "https://x"),
        ("https://x/api/v1/", "https://x"),
        ("https://x/api/v1/edc", "https://x"),
        ("https://x/api/v1/edc/", "https://x"),
        ("https://edc.trn.imednetapi.com/api/v1/edc", "https://edc.trn.imednetapi.com"),
        ("https://x/v1", "https://x/v1"),
        ("https://x//", "https://x"),
        ("https://x", "https://x"),
    ],
)
def test_sanitize_base_url(url: str, expected: str) -> None:
    """Test that sanitize base url."""
    assert sanitize_base_url(url) == expected


def test_redact_url_query() -> None:
    """Test URL query parameter redaction."""
    url = "https://api.example.com/v1/resource?api_key=secret123&token=abc&other=value"
    expected = "https://api.example.com/v1/resource?api_key=***&token=***&other=value"
    assert redact_url_query(url) == expected

    # Test custom sensitive params
    url = "https://example.com?user=me&pass=1234"
    expected = "https://example.com?user=me&pass=***"
    assert redact_url_query(url, sensitive_params={"pass"}) == expected

    # Test no sensitive params
    url = "https://example.com?q=search"
    assert redact_url_query(url) == url

    # Test empty params
    assert redact_url_query("https://example.com") == "https://example.com"


@pytest.mark.parametrize(
    ("base_path", "segments", "expected"),
    [
        ("/api/v1", ("records", 123), "api/v1/records/123"),
        ("/api//v1//", ("records//", "/123/"), "api/v1/records/123"),
        (
            "api",
            ("user@domain.com", "user%40domain.com"),
            "api/user@domain.com/user@domain.com",
        ),
        ("api", ("Site 1",), "api/Site%201"),
        ("/", (), ""),
        ("", (), ""),
    ],
)
def test_build_safe_path(base_path: str, segments: tuple[object, ...], expected: str) -> None:
    """Test that build_safe_path normalizes static slashes and encodes valid segments."""
    assert build_safe_path(base_path, *segments) == expected


@pytest.mark.parametrize(
    ("base_path", "segments"),
    [
        ("api", ("..",)),
        ("api", ("../users",)),
        ("api", ("%2e%2e",)),
        ("api", ("%2e%2e/users",)),
        ("api", (".",)),
        ("api", ("%2e",)),
        ("api", ("a%2Fb",)),
        ("api", ("a\\b",)),
        ("api", ("a%5Cb",)),
        ("api", ("my_study?role=admin",)),
        ("api", ("my_study%3Frole=admin",)),
        ("api", ("my_study#fragment",)),
        ("api", ("my_study%23fragment",)),
        ("api", ("study\x00null",)),
        ("api", ("study%0anewline",)),
        ("api", ("   ",)),
        ("api", (None,)),
        ("api", (True,)),
    ],
)
def test_build_safe_path_rejects_unsafe_segments(
    base_path: str, segments: tuple[object, ...]
) -> None:
    """Test that build_safe_path raises PathTraversalValidationError on unsafe segments."""
    with pytest.raises(PathTraversalValidationError):
        build_safe_path(base_path, *segments)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ALLOWED_STUDY", "ALLOWED_STUDY"),
        (123, "123"),
        ("user@domain.com", "user@domain.com"),
        ("user%40domain.com", "user@domain.com"),
        ("Site 1", "Site%201"),
    ],
)
def test_validate_path_segment_valid(raw: object, expected: str) -> None:
    """Test validate_path_segment encodes valid single segments while preserving @."""
    assert validate_path_segment(raw) == expected


@pytest.mark.parametrize(
    "unsafe_segment",
    [
        "",
        "   ",
        "%20",
        None,
        True,
        False,
        ".",
        "..",
        "%2e",
        "%2E",
        "%2e%2e",
        "%2E%2E",
        ".%2e",
        "%2e.",
        "CANARY_STUDY/records",
        "a%2fb",
        "a%2Fb",
        "a\\b",
        "a%5cb",
        "CANARY_STUDY/users?",
        "a%3fb",
        "CANARY_STUDY/records#",
        "a%23b",
        "bad\x00id",
        "bad%00id",
        "bad\nid",
        "bad\rid",
        "bad\x7fid",
    ],
)
def test_validate_path_segment_rejects_unsafe(unsafe_segment: object) -> None:
    """Test validate_path_segment raises PathTraversalValidationError on unsafe tokens."""
    with pytest.raises(PathTraversalValidationError):
        validate_path_segment(unsafe_segment)


def test_redact_url_query_decodes_and_masks_phi_in_filter_and_record_data_filter() -> None:
    """Decode and mask PHI inside URL filter/recordDataFilter params while keeping exempt IDs."""
    from urllib.parse import urlencode

    from imednet.utils.security import SensitivityRegistry, global_sensitivity_registry
    from imednet.utils.url import redact_sensitive_text

    custom_reg = SensitivityRegistry()
    custom_reg.add_sensitive_key("customChartNumber")

    query = urlencode(
        {
            "api_key": "raw-api-secret",  # pragma: allowlist secret
            "study_key": "STUDY-01",
            "subject_key": "SUBJ-01",
            "record_id": "99",
            "usubjid": "STUDY-01-SUBJ-01",
            "subjid": "SUBJ-01",
            "studyid": "STUDY-01",
            "ssn": "123-45-6789",
            "filter": 'subjectKey==SUBJ-01;ssn==123-45-6789;patientName=="Jane Doe"',
            "recordDataFilter": "email=~jane@example.com;customChartNumber!=CHART-7788;record_id>=99",
        }
    )
    raw_url = f"https://edc.example.com/api/v1/edc/studies/STUDY-01/records?{query}"

    sanitized_url = redact_url_query(raw_url, registry=custom_reg)
    assert "api_key=***" in sanitized_url
    assert "ssn=***MASKED***" in sanitized_url
    assert "***MASKED***" in sanitized_url
    for leaked in (
        "raw-api-secret",
        "123-45-6789",
        "Jane",
        "Doe",
        "jane%40example.com",
        "jane@example.com",
        "CHART-7788",
    ):
        assert leaked not in sanitized_url
    for exempt_val in ("STUDY-01", "SUBJ-01", "99", "STUDY-01-SUBJ-01"):
        assert exempt_val in sanitized_url
    assert not global_sensitivity_registry.is_sensitive("customChartNumber")

    # Relative path URLs and URI user:password@host coexist cleanly with @ in email filters
    prose = (
        "Failed mongodb://svc:dbpass123@mongo.internal:27017/edc and "  # pragma: allowlist secret
        f"GET /api/v1/edc/studies/STUDY-01/records?{query} HTTP/1.1"
    )
    sanitized_prose = redact_sensitive_text(prose, registry=custom_reg)
    assert "mongodb://svc:***@mongo.internal:27017/edc" in sanitized_prose
    assert "dbpass123" not in sanitized_prose
    assert "123-45-6789" not in sanitized_prose
    assert "CHART-7788" not in sanitized_prose
    assert "STUDY-01-SUBJ-01" in sanitized_prose
