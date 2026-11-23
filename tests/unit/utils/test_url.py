"""Unit tests for url."""

import pytest

from imednet.errors.validation import PathTraversalValidationError
from imednet.utils.url import (
    build_safe_path,
    redact_sensitive_text,
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
    url = "https://api.example.com/v1/resource?api_key=secret123&token=abc&other=value"  # pragma: allowlist secret
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


def test_redact_url_query_and_sensitive_text_presigned_sas_userinfo_and_file_uris() -> None:
    """Verify cloud pre-signed signatures, SAS sig, userinfo, and file:/sqlite: URIs are scrubbed."""
    from imednet.spi import utils as spi_utils

    assert spi_utils.redact_url_query is redact_url_query
    assert spi_utils.redact_sensitive_text is redact_sensitive_text

    presigned_url = (
        "https://exports.example.test/study-export.json"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=CANARY_CRED_000"  # pragma: allowlist secret
        "&X-Amz-Signature=CANARY_S3_SIG_111"  # pragma: allowlist secret
        "&X-Goog-Credential=CANARY_GOOG_CRED_001&X-Goog-Signature=CANARY_GCS_SIG_222"  # pragma: allowlist secret
        "&Signature=CANARY_LEGACY_SIG_223&sv=2024-11-04&sig=CANARY_AZURE_SAS_333"  # pragma: allowlist secret
        "&page=2&size=50&sort=recordId&filter=studyKey==S1"
    )
    sqlite_uri = (
        "file:triage_corrupt.db?mode=rw"
        "&key=CANARY_SQLCIPHER_KEY_444&password=CANARY_SQLITE_PASS_555"  # pragma: allowlist secret
        "&passphrase=CANARY_SQL_PASSPHRASE_666"  # pragma: allowlist secret
    )
    sqlite_scheme_uri = (
        "sqlite:///tmp/local.db?mode=ro&key=CANARY_SQLITE3_KEY_777"  # pragma: allowlist secret
    )
    userinfo_single = (
        "Request failed (Method: GET, URL: "
        "https://CANARY_USERINFO_PAT_AAA@edc.example.test/api/v1/edc/studies)"  # pragma: allowlist secret
    )
    userinfo_pair = "Connection error for postgresql://admin:CANARY_PG_PASS_BBB@db.example.test:5432/edc"  # pragma: allowlist secret

    redacted_presigned = redact_url_query(presigned_url)
    redacted_presigned_text = redact_sensitive_text(f"Fetching ({presigned_url}).")
    redacted_sqlite = redact_sensitive_text(sqlite_uri)
    redacted_sqlite_scheme = redact_sensitive_text(sqlite_scheme_uri)
    redacted_single = redact_sensitive_text(userinfo_single)
    redacted_pair = redact_sensitive_text(userinfo_pair)
    redacted_direct_userinfo = redact_url_query(
        "https://CANARY_USERINFO_PAT_AAA@edc.example.test/api/v1/edc/studies"  # pragma: allowlist secret
    )

    for canary in (
        "CANARY_CRED_000",
        "CANARY_S3_SIG_111",
        "CANARY_GOOG_CRED_001",
        "CANARY_GCS_SIG_222",
        "CANARY_LEGACY_SIG_223",
        "CANARY_AZURE_SAS_333",
        "CANARY_SQLCIPHER_KEY_444",
        "CANARY_SQLITE_PASS_555",
        "CANARY_SQL_PASSPHRASE_666",
        "CANARY_SQLITE3_KEY_777",
        "CANARY_USERINFO_PAT_AAA",
        "CANARY_PG_PASS_BBB",
    ):
        for output in (
            redacted_presigned,
            redacted_presigned_text,
            redacted_sqlite,
            redacted_sqlite_scheme,
            redacted_single,
            redacted_pair,
            redacted_direct_userinfo,
        ):
            assert canary not in output

    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in redacted_presigned
    assert "sv=2024-11-04" in redacted_presigned
    assert "page=2" in redacted_presigned
    assert "size=50" in redacted_presigned
    assert "sort=recordId" in redacted_presigned
    assert "mode=rw" in redacted_sqlite
    assert redacted_sqlite_scheme == "sqlite:///tmp/local.db?mode=ro&key=***"
    assert (
        redacted_single
        == "Request failed (Method: GET, URL: https://***@edc.example.test/api/v1/edc/studies)"
    )
    assert redacted_pair == "Connection error for postgresql://***@db.example.test:5432/edc"
    assert redacted_direct_userinfo == "https://***@edc.example.test/api/v1/edc/studies"


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
