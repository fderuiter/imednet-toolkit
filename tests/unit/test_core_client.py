"""Unit tests for core client."""

from unittest.mock import MagicMock

import httpx
import pytest
import respx

from imednet import errors
from imednet.constants import DEFAULT_BASE_URL
from imednet.core.client import Client
from imednet.core.retry import RetryConfig, RetryPolicy


def test_initialization_sets_defaults() -> None:
    """Test that initialization sets defaults."""
    client = Client(api_key="A", security_key="B")
    assert client.base_url == DEFAULT_BASE_URL
    assert client._client.headers["x-api-key"] == "A"
    assert client._client.headers["x-imn-security-key"] == "B"


def test_retry_logic_retries_request_errors() -> None:
    """Test that retry logic retries request errors."""
    client = Client(
        api_key="A",
        security_key="B",
        base_url="https://api.test",
        retry_config=RetryConfig(retries=1),
    )
    call_count = {"count": 0}

    def side_effect(request: httpx.Request) -> httpx.Response:
        """Helper function to side effect."""
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise httpx.RequestError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    with respx.mock(assert_all_called=True, assert_all_mocked=True) as respx_mock:
        respx_mock.get("https://api.test/path").mock(side_effect=side_effect)
        response = client.get("/path")

    assert call_count["count"] == 2
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (400, errors.BadRequestError),
        (401, errors.UnauthorizedError),
        (403, errors.ForbiddenError),
        (404, errors.NotFoundError),
        (409, errors.ConflictError),
        (429, errors.RateLimitError),
        (500, errors.ServerError),
        (418, errors.ApiError),
    ],
)
def test_request_error_mapping(status, exc) -> None:
    """Test that request error mapping."""
    client = Client(api_key="A", security_key="B", base_url="https://api.test")

    with respx.mock(assert_all_called=True, assert_all_mocked=True) as respx_mock:
        respx_mock.get("https://api.test/some").respond(status_code=status, json={"error": status})

        with pytest.raises(exc):
            client.get("/some")


@pytest.mark.parametrize(
    ("http_status", "payload", "expected_exc", "expected_code", "expected_msg", "expected_field"),
    [
        (
            400,
            {
                "metadata": {
                    "status": "BAD_REQUEST",
                    "path": "/api/v1/edc/studies",
                    "timestamp": "2018-10-18 05:46:29",
                    "error": {
                        "code": "1000",
                        "description": "Field raised validation errors",
                        "field": {"attribute": "page", "value": "XX"},
                    },
                }
            },
            errors.ValidationError,
            1000,
            "Field raised validation errors",
            {"attribute": "page", "value": "XX"},
        ),
        (
            500,
            {
                "metadata": {
                    "status": "INTERNAL_SERVER_ERROR",
                    "path": "/api/v1/edc/studies",
                    "timestamp": "2018-10-18 05:46:29",
                    "error": {
                        "code": "9000",
                        "description": "Unknown error. Please contact Mednet support for assistance.",
                    },
                }
            },
            errors.ServerError,
            9000,
            "Unknown error. Please contact Mednet support for assistance.",
            None,
        ),
        (
            403,
            {
                "metadata": {
                    "status": "FORBIDDEN",
                    "path": "/api/v1/edc/studies/STUDY1/sites",
                    "timestamp": "2018-10-18 05:46:29",
                    "error": {
                        "code": "9001",
                        "description": "Unauthorized error. Insufficient permission to retrieve data.",
                    },
                }
            },
            errors.AuthenticationError,
            9001,
            "Unauthorized error. Insufficient permission to retrieve data.",
            None,
        ),
    ],
)
def test_nested_metadata_error_http_dispatch(
    http_status, payload, expected_exc, expected_code, expected_msg, expected_field
) -> None:
    """Test Client HTTP error dispatch and attribute parsing for nested metadata.error envelopes."""
    client = Client(
        api_key="A",
        security_key="B",
        base_url="https://api.test",
        retry_config=RetryConfig(retries=0),
    )

    with respx.mock(assert_all_called=True, assert_all_mocked=True) as respx_mock:
        respx_mock.get("https://api.test/nested").respond(status_code=http_status, json=payload)

        with pytest.raises(expected_exc) as exc_info:
            client.get("/nested")

    err = exc_info.value
    assert type(err) is expected_exc
    assert err.code == expected_code
    assert err.message == expected_msg
    assert err.description == expected_msg
    assert err.field == expected_field
    assert err.status == payload["metadata"]["status"]


def test_non_json_error_fallback() -> None:
    """Test Client HTTP fallback when non-2xx response is plain text."""
    client = Client(
        api_key="A",
        security_key="B",
        base_url="https://api.test",
        retry_config=RetryConfig(retries=0),
    )

    with respx.mock(assert_all_called=True, assert_all_mocked=True) as respx_mock:
        respx_mock.get("https://api.test/text-err").respond(status_code=502, text="Bad Gateway")

        with pytest.raises(errors.ServerError) as exc_info:
            client.get("/text-err")

    assert exc_info.value.message == "Bad Gateway"
    assert exc_info.value.code is None
    assert exc_info.value.field is None


def test_tracer_records_span() -> None:
    """Test that tracer records span."""
    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    client = Client(api_key="A", security_key="B", base_url="https://api.test", tracer=tracer)

    with respx.mock(assert_all_called=True, assert_all_mocked=True) as respx_mock:
        respx_mock.get("https://api.test/trace").respond(status_code=200, json={"ok": True})
        client.get("/trace")

    tracer.start_as_current_span.assert_called_with(
        "http_request", attributes={"endpoint": "/trace", "method": "GET"}
    )
    span.set_attribute.assert_called_with("status_code", 200)


def test_base_url_sanitized() -> None:
    """Test that base url sanitized."""
    client = Client(api_key="A", security_key="B", base_url="https://host/api/")
    assert client.base_url == "https://host"


def test_retry_policy_accessor_updates_executor() -> None:
    """Test that retry policy accessor updates executor."""
    client = Client(api_key="A", security_key="B")
    policy = MagicMock(spec=RetryPolicy)

    client.retry_policy = policy

    assert client.retry_policy is policy
    assert client._executor.retry_config.retry_policy is policy
