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
    ValidationError,
    get_error_class,
)


def test_api_error_str_representation():
    """Test that api error str representation."""
    # Test basic instantiation with string
    err = ApiError("Something went wrong")
    assert str(err) == "Something went wrong (Response: Something went wrong)"

    # Test with status code
    err2 = ApiError("Not Found", status_code=404)
    assert str(err2) == "Not Found (Status Code: 404, Response: Not Found)"

    # Test with dictionary response
    err3 = ApiError({"error": "Bad Request"}, status_code=400)
    assert str(err3) == "Bad Request (Status Code: 400, Response: {'error': 'Bad Request'})"


def test_api_error_empty_response():
    """Test that api error empty response."""
    # Test instantiation with empty response string
    err = ApiError("", status_code=500)
    assert str(err) == " (Status Code: 500)"

    # Test instantiation with None response
    err2 = ApiError(None, status_code=500)
    assert str(err2) == "None (Status Code: 500)"


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
    assert err_cls is ValidationError

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
        (400, "1000", "BAD_REQUEST", "Field raised validation errors", ValidationError),
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
            ServerError,
        ),
        (
            401,
            "9001",
            "UNAUTHORIZED",
            "Unauthorized error. Insufficient permission to retrieve data.",
            AuthenticationError,
        ),
        (
            403,
            "9001",
            "FORBIDDEN",
            "Unauthorized error. Insufficient permission to retrieve data.",
            AuthenticationError,
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
    assert get_error_class(200, {"metadata": {"status": "FORBIDDEN"}}) is ForbiddenError
    assert get_error_class(200, {"metadata": {"status": "NOT_FOUND"}}) is NotFoundError

    # Legacy flat status overrides
    assert get_error_class(400, {"status": "validation_error"}) is ValidationError
    assert get_error_class(401, {"status": "authentication_failed"}) is AuthenticationError

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
