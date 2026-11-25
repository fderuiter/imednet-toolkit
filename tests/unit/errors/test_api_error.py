"""Unit tests for api error."""

from unittest.mock import MagicMock

import pytest

from imednet.errors import (
    ApiError,
    AuthenticationError,
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    ServerError,
    UnauthorizedError,
    ValidationError,
    get_error_class,
)


def test_api_error_str_representation():
    """Test that api error str representation."""
    # Test basic instantiation with string (omits duplicate Response: text)
    err = ApiError("Something went wrong")
    assert str(err) == "Something went wrong"

    # Test with status code
    err2 = ApiError("Not Found", status_code=404)
    assert str(err2) == "Not Found (Status Code: 404)"

    # Test with dictionary response
    err3 = ApiError({"error": "Bad Request"}, status_code=400)
    assert str(err3) == "Bad Request (Status Code: 400, Response: {'error': 'Bad Request'})"


def test_top_level_error_exports_and_not_found_str():
    """Top-level imednet exports ImednetError, ApiError, and NotFoundError cleanly."""
    from imednet import ApiError as TopApiError
    from imednet import ImednetError as TopImednetError
    from imednet import NotFoundError as TopNotFoundError

    assert TopApiError is ApiError
    assert TopNotFoundError is NotFoundError
    assert issubclass(TopNotFoundError, TopApiError)
    assert issubclass(TopApiError, TopImednetError)
    assert str(TopNotFoundError("msg")) == "msg"


def test_api_error_empty_response():
    """Empty or None responses format cleanly without leading spaces and carry DiagnosticContext."""
    import httpx

    from imednet.core.http.handlers import handle_response

    err = ApiError("", status_code=500)
    assert str(err) == "HTTP 500 Internal Server Error (Status Code: 500)"
    assert not str(err).startswith(" ")

    err2 = ApiError(None, status_code=500)
    assert str(err2) == "HTTP 500 Internal Server Error (Status Code: 500)"
    assert not str(err2).startswith(" ")

    req = httpx.Request(
        "GET",
        "https://api.example.com/api/v1/edc/studies/MISSING?api_key=raw-query-key&page=1",
    )
    resp_404 = httpx.Response(404, text="", request=req)
    with pytest.raises(NotFoundError) as exc_info:
        handle_response(resp_404, attempt_count=1)

    not_found = exc_info.value
    assert not_found.method == "GET"
    assert not_found.url == "https://api.example.com/api/v1/edc/studies/MISSING?api_key=***&page=1"
    assert not_found.attempt_count == 1
    assert not str(not_found).startswith(" ")
    assert str(not_found).startswith("HTTP 404 Not Found (Status Code: 404")
    assert "raw-query-key" not in str(not_found)
    assert "Method: GET" in str(not_found)
    assert "URL: https://api.example.com/api/v1/edc/studies/MISSING?api_key=***&page=1" in str(
        not_found
    )


def test_api_error_base_str_only():
    """Test that api error base str only."""
    err = ApiError(None)
    assert str(err) == "None"


def test_api_error_nested_validation_envelope():
    """Test parsing of nested metadata.error envelope for code 1000."""
    payload = {
        "metadata": {
            "status": "BAD_REQUEST",
            "path": "/api/v1/edc/studies",
            "timestamp": "2018-10-18 05:46:29",
            "error": {
                "code": "1000",
                "description": "Field raised validation errors",
                "field": {
                    "attribute": "page",
                    "value": "XX",
                },
            },
        }
    }
    err_cls = get_error_class(400, payload)
    assert err_cls is BadRequestError
    assert issubclass(err_cls, ValidationError)

    err = err_cls(payload, status_code=400)
    assert isinstance(err, ValidationError)
    assert err.code == 1000
    assert err.status == "BAD_REQUEST"
    assert err.status_code == 400
    assert err.description == "Field raised validation errors"
    assert err.message == "Field raised validation errors"
    assert err.field == {"attribute": "page", "value": "XX"}
    assert str(err).startswith("Field raised validation errors (Status Code: 400, Response:")


@pytest.mark.parametrize(
    ("status_code", "error_code", "meta_status", "description", "expected_cls"),
    [
        (400, "1000", "BAD_REQUEST", "Field raised validation errors", BadRequestError),
        (
            500,
            "9000",
            "INTERNAL_SERVER_ERROR",
            "Unknown error. Please contact Mednet support for assistance.",
            ServerError,
        ),
        (
            400,
            "9000",
            "BAD_REQUEST",
            "Unknown error. Please contact Mednet support for assistance.",
            BadRequestError,
        ),
        (
            401,
            "9001",
            "UNAUTHORIZED",
            "Unauthorized error. Insufficient permission to retrieve data.",
            UnauthorizedError,
        ),
        (
            403,
            "9001",
            "FORBIDDEN",
            "Unauthorized error. Insufficient permission to retrieve data.",
            ForbiddenError,
        ),
    ],
)
def test_get_error_class_and_attributes_for_upstream_codes(
    status_code, error_code, meta_status, description, expected_cls
):
    """Test registry dispatch and attribute extraction for codes 1000, 9000, and 9001."""
    payload = {
        "metadata": {
            "status": meta_status,
            "path": "/api/v1/edc/studies",
            "timestamp": "2018-10-18 05:46:29",
            "error": {
                "code": error_code,
                "description": description,
            },
        }
    }
    err_cls = get_error_class(status_code, payload)
    assert err_cls is expected_cls

    err = err_cls(payload, status_code=status_code)
    assert err.code == int(error_code)
    assert err.status == meta_status
    assert err.description == description
    assert err.message == description
    assert err.field is None
    assert str(err).startswith(f"{description} (Status Code: {status_code}, Response:")


def test_get_error_class_metadata_status_and_fallbacks():
    """Test metadata.status dispatch and clean fallbacks for flat/malformed payloads."""
    assert (
        get_error_class(400, {"metadata": {"status": "BAD_REQUEST", "error": {}}})
        is BadRequestError
    )
    assert get_error_class(200, {"metadata": {"status": "UNAUTHORIZED"}}) is AuthenticationError
    assert get_error_class(401, {"metadata": {"status": "UNAUTHORIZED"}}) is UnauthorizedError
    assert get_error_class(403, {"metadata": {"status": "UNAUTHORIZED"}}) is ForbiddenError
    assert get_error_class(200, {"metadata": {"status": "FORBIDDEN"}}) is ForbiddenError
    assert get_error_class(200, {"metadata": {"status": "NOT_FOUND"}}) is NotFoundError

    # Legacy flat status overrides
    assert get_error_class(400, {"status": "validation_error"}) is BadRequestError
    assert get_error_class(401, {"status": "authentication_failed"}) is UnauthorizedError
    assert get_error_class(200, {"status": "validation_error"}) is ValidationError
    assert get_error_class(200, {"status": "authentication_failed"}) is AuthenticationError

    # Top-level flat fallback attributes
    flat_err = ValidationError(
        {
            "code": "1000",
            "message": "Top-level validation message",
            "field": {"attribute": "size", "value": "-1"},
        },
        status_code=400,
    )
    assert flat_err.code == 1000
    assert flat_err.message == "Top-level validation message"
    assert flat_err.field == {"attribute": "size", "value": "-1"}
    assert flat_err.status == "validation_error"

    # Non-dict / malformed metadata fallbacks
    malformed = ApiError({"metadata": "unexpected", "code": "not-an-int"}, status_code=502)
    assert malformed.code is None
    assert malformed.status is None
    assert malformed.field is None
    assert malformed.description is None


def test_nested_error_secret_redaction_and_cli_readable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Test secret redaction inside nested metadata.error and readable CLI formatting."""
    from imednet.cli.decorators import with_sdk

    raw_key_val = "sensitive-imn-header-val"
    raw_bearer_val = "Bearer sensitive-auth-val"
    payload = {
        "metadata": {
            "status": "UNAUTHORIZED",
            "path": "/api/v1/edc/studies",
            "timestamp": "2018-10-18 05:46:29",
            "error": {
                "code": "9001",
                "description": f"Unauthorized error for Authorization: {raw_bearer_val}",
                "field": {
                    "attribute": "x-api-key",
                    "value": raw_key_val,
                },
            },
        }
    }

    err = AuthenticationError(payload, status_code=401)
    assert raw_key_val not in str(err)
    assert raw_bearer_val not in str(err)
    assert err.field == {"attribute": "x-api-key", "value": "***"}
    assert "Unauthorized error for Authorization: ***" in err.message

    fake_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: fake_sdk)

    @with_sdk
    def failing_cmd(sdk):
        raise ValidationError(
            {
                "metadata": {
                    "status": "BAD_REQUEST",
                    "error": {
                        "code": "1000",
                        "description": "Field raised validation errors",
                        "field": {"attribute": "page", "value": "XX"},
                    },
                }
            },
            status_code=400,
        )

    with pytest.raises(SystemExit) as exc_info:
        failing_cmd()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "API Error: Field raised validation errors" in captured.err


def test_api_error_nested_variable_details_and_sensitive_field_value_redaction():
    """Test Variable detail attributes and redaction before formatting metadata.error.field.value."""
    payload = {
        "metadata": {
            "status": "BAD_REQUEST",
            "error": {
                "code": "1000",
                "description": "Invalid variable value",
                "field": {
                    "variable": "DEM_AGE",
                    "value": "not-an-integer",
                },
            },
        }
    }
    err_cls = get_error_class(400, payload)
    assert err_cls is BadRequestError
    assert issubclass(err_cls, ValidationError)

    err = err_cls(payload, status_code=400)
    assert err.code == 1000
    assert err.status == "BAD_REQUEST"
    assert err.message == "Invalid variable value"
    assert err.variable == "DEM_AGE"
    assert err.field_attribute == "DEM_AGE"
    assert err.field_value == "not-an-integer"
    assert "Field: DEM_AGE='not-an-integer'" in str(err)

    # Sensitive variable/attribute names and Bearer values are redacted before formatting str(err)
    raw_cred_val = "sensitive-subject-cred-val-999"
    cred_payload = {
        "metadata": {
            "status": "BAD_REQUEST",
            "error": {
                "code": "1000",
                "description": "Invalid credential variable",
                "field": {
                    "variable": "SUBJECT_API_TOKEN",
                    "value": raw_cred_val,
                },
            },
        }
    }
    redacted_err = ApiError(cred_payload, status_code=400)
    assert raw_cred_val not in str(redacted_err)
    assert redacted_err.variable == "SUBJECT_API_TOKEN"
    assert redacted_err.field_value == "***"
    assert "Field: SUBJECT_API_TOKEN='***'" in str(redacted_err)

    bearer_payload = {
        "metadata": {
            "status": "BAD_REQUEST",
            "error": {
                "code": "1000",
                "description": "Header validation failed",
                "field": {
                    "attribute": "customHeader",
                    "value": f"Bearer {raw_cred_val}",
                },
            },
        }
    }
    bearer_err = ApiError(bearer_payload, status_code=400)
    assert raw_cred_val not in str(bearer_err)
    assert bearer_err.field_value == "Bearer ***"
    assert "Field: customHeader='Bearer ***'" in str(bearer_err)


def test_clinical_variables_and_prose_preserved_while_credentials_redacted(monkeypatch):
    """Clinical variables and unquoted error prose stay unmasked while credentials render as ***."""
    from types import SimpleNamespace

    from imednet.cli.decorators import redact_cli_secrets
    from imednet.utils.job_poller import _extract_record_errors
    from imednet.utils.secrets import redact_sensitive_payload

    raw_api_key = "imn-live-api-val-777"  # pragma: allowlist secret
    raw_sec_key = "imn-live-sec-val-888"  # pragma: allowlist secret
    raw_oidc = "eyJhbGciOiJSUzI1NiJ9.payload.sig"  # pragma: allowlist secret
    raw_subj_tok = "subj-tok-val-555"  # pragma: allowlist secret
    prose = (
        "Token validation failed for subject 104 because record secret state was locked: "
        "verify site coordinator role before resubmitting"
    )

    clinical_payload = {
        "metadata": {
            "status": "BAD_REQUEST",
            "error": [
                {
                    "code": "1000",
                    "description": prose,
                    "field": {"variable": "INSULIN_SECRETION", "value": "14.2_uIU_mL"},
                },
                {
                    "code": "1000",
                    "description": "Out of range secretion rate",
                    "field": {"variable": "GASTRIC_SECRETION_RATE", "value": "85_mL_hr"},
                },
                {
                    "code": "1000",
                    "description": "Unrecognized study token identifier",
                    "field": {"variable": "STUDY_TOKEN_ID", "value": "ST-2026-ALPHA"},
                },
                {
                    "code": "1000",
                    "description": f"Header x-api-key: {raw_api_key} and Authorization: Bearer {raw_oidc}",
                    "field": {"variable": "SUBJECT_API_TOKEN", "value": raw_subj_tok},
                },
            ],
        },
        "headers": {
            "x-api-key": raw_api_key,
            "x-imn-security-key": raw_sec_key,
            "oidc_token": raw_oidc,
        },
    }

    redacted = redact_sensitive_payload(clinical_payload)
    assert isinstance(redacted, dict)
    err_list = redacted["metadata"]["error"]
    assert err_list[0]["description"] == prose
    assert err_list[0]["field"]["value"] == "14.2_uIU_mL"
    assert err_list[1]["field"]["value"] == "85_mL_hr"
    assert err_list[2]["field"]["value"] == "ST-2026-ALPHA"
    assert err_list[3]["field"]["value"] == "***"
    assert redacted["headers"] == {
        "x-api-key": "***",
        "x-imn-security-key": "***",
        "oidc_token": "***",
    }

    err_cls = get_error_class(400, clinical_payload)
    assert err_cls is BadRequestError
    assert issubclass(err_cls, ValidationError)
    api_err = err_cls(clinical_payload, status_code=400)
    assert api_err.code == 1000
    assert api_err.description is not None
    assert api_err.description.startswith(prose)
    assert "Out of range secretion rate" in api_err.description
    assert "Header x-api-key: *** and Authorization: ***" in api_err.description
    assert api_err.variable == "INSULIN_SECRETION"
    assert len(api_err.errors) == 4
    formatted = str(api_err)
    assert "14.2_uIU_mL" in formatted
    assert "85_mL_hr" in formatted
    assert "ST-2026-ALPHA" in formatted
    assert prose in formatted
    for secret in (raw_api_key, raw_sec_key, raw_oidc, raw_subj_tok):
        assert secret not in formatted

    # JobPoller _extract_record_errors preserves clinical variables while masking secrets
    job_status = SimpleNamespace(
        results=[
            {
                "status": "FAILED",
                "INSULIN_SECRETION": "14.2_uIU_mL",  # pragma: allowlist secret
                "GASTRIC_SECRETION_RATE": "85_mL_hr",  # pragma: allowlist secret
                "STUDY_TOKEN_ID": "ST-2026-ALPHA",  # pragma: allowlist secret
                "SUBJECT_API_TOKEN": raw_subj_tok,
                "error": f"{prose} (x-api-key={raw_api_key})",
            }
        ]
    )
    record_errors = _extract_record_errors(job_status)
    assert len(record_errors) == 1
    assert record_errors[0]["INSULIN_SECRETION"] == "14.2_uIU_mL"  # pragma: allowlist secret
    assert record_errors[0]["GASTRIC_SECRETION_RATE"] == "85_mL_hr"  # pragma: allowlist secret
    assert record_errors[0]["STUDY_TOKEN_ID"] == "ST-2026-ALPHA"
    assert record_errors[0]["SUBJECT_API_TOKEN"] == "***"
    assert prose in record_errors[0]["error"]
    assert raw_api_key not in record_errors[0]["error"]

    # CLI redact_cli_secrets preserves prose and clinical values while masking env & inline secrets
    monkeypatch.setenv("IMEDNET_API_KEY", raw_api_key)
    cli_msg = redact_cli_secrets(
        f"{prose}; INSULIN_SECRETION='14.2_uIU_mL'; STUDY_TOKEN_ID='ST-2026-ALPHA'; "  # pragma: allowlist secret
        f'Bearer {raw_oidc}; "security_key": "{raw_sec_key}"'  # pragma: allowlist secret
    )
    assert prose in cli_msg
    assert "INSULIN_SECRETION='14.2_uIU_mL'" in cli_msg  # pragma: allowlist secret
    assert "STUDY_TOKEN_ID='ST-2026-ALPHA'" in cli_msg  # pragma: allowlist secret
    assert raw_oidc not in cli_msg
    assert raw_sec_key not in cli_msg


def test_diagnostic_context_immutability_retry_after_and_cli_forbidden(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """DiagnosticContext is immutable, parses Retry-After on 429, and CLI prints Permission Denied on 403/9001."""
    import dataclasses
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    import httpx

    from imednet.cli.decorators import with_sdk
    from imednet.core.http.handlers import handle_response
    from imednet.errors import DiagnosticContext, RateLimitError

    ctx = DiagnosticContext(
        method="POST",
        url="https://api.example.com/records?token=raw-tok&study_token_id=ST1",
        retry_after=15.0,
        attempt_count=2,
        cause_type="RateLimit",
    )
    assert ctx.url == "https://api.example.com/records?token=***&study_token_id=ST1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.method = "GET"  # type: ignore[misc]

    # Call compatibility for ValidationError positional + keyword arguments
    val_err = ValidationError(
        {"error": "invalid"},
        422,
        validation_report={"missing": ["DEM_AGE"]},
        diagnostic_context=ctx,
    )
    assert val_err.status_code == 422
    assert val_err.validation_report == {"missing": ["DEM_AGE"]}
    assert val_err.method == "POST"
    assert val_err.retry_after == 15.0
    assert val_err.attempt_count == 2
    assert val_err.cause_type == "RateLimit"

    # Numeric Retry-After on 429
    req_429 = httpx.Request("GET", "https://api.example.com/studies")
    resp_429 = httpx.Response(
        429,
        headers={"Retry-After": "30"},
        json={"error": "Too Many Requests"},
        request=req_429,
    )
    with pytest.raises(RateLimitError) as rl_exc:
        handle_response(resp_429, attempt_count=3)
    assert rl_exc.value.retry_after == 30.0
    assert rl_exc.value.method == "GET"
    assert rl_exc.value.url == "https://api.example.com/studies"
    assert rl_exc.value.attempt_count == 3
    assert "Retry-After: 30s" in str(rl_exc.value)

    # HTTP-date Retry-After on 429
    future_dt = datetime.now(timezone.utc) + timedelta(seconds=60)
    resp_429_date = httpx.Response(
        429,
        headers={"Retry-After": format_datetime(future_dt, usegmt=True)},
        text="Slow down",
        request=req_429,
    )
    with pytest.raises(RateLimitError) as rl_date_exc:
        handle_response(resp_429_date)
    assert rl_date_exc.value.retry_after is not None
    assert 0.0 < rl_date_exc.value.retry_after <= 65.0

    # List metadata.error with code 9001 on 403 vs 401
    forbidden_list_payload = {
        "metadata": {
            "status": "FORBIDDEN",
            "error": [
                {
                    "code": "9001",
                    "description": "Unauthorized error. Insufficient permission to retrieve data.",
                }
            ],
        }
    }
    assert get_error_class(403, forbidden_list_payload) is ForbiddenError
    assert (
        get_error_class(
            403,
            {
                "metadata": {
                    "status": "UNAUTHORIZED",
                    "error": [{"description": "Insufficient study permission."}],
                }
            },
        )
        is ForbiddenError
    )
    assert (
        get_error_class(
            401,
            {
                "metadata": {
                    "status": "UNAUTHORIZED",
                    "error": [{"code": "9001", "description": "Invalid API credentials."}],
                }
            },
        )
        is UnauthorizedError
    )
    assert issubclass(UnauthorizedError, AuthenticationError)

    # CLI @with_sdk prints Permission Denied: when SDK raises 403 + 9001
    fake_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: fake_sdk)

    @with_sdk
    def unauthorized_study_cmd(sdk):
        resp_403 = httpx.Response(
            403,
            json=forbidden_list_payload,
            request=httpx.Request(
                "GET", "https://api.example.com/api/v1/edc/studies/STUDY1/records"
            ),
        )
        handle_response(resp_403)

    with pytest.raises(SystemExit) as cli_exc:
        unauthorized_study_cmd()
    assert cli_exc.value.code == 1
    captured = capsys.readouterr()
    assert "Permission Denied:" in captured.err
    assert "Authentication Failed:" not in captured.err
