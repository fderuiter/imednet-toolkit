"""Unit tests for client."""

import httpx
import pytest

from imednet.core.retry import RetryConfig
from imednet.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ServerError,
    UnauthorizedError,
)


def test_get_success(http_client, respx_mock_client, sample_data):
    """Test that get success."""
    respx_mock_client.get("/items").mock(return_value=httpx.Response(200, json=sample_data))

    response = http_client.get("/items")

    assert response.status_code == 200
    assert response.json() == sample_data


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (400, BadRequestError),
        (401, UnauthorizedError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (409, ConflictError),
        (429, RateLimitError),
        (500, ServerError),
    ],
)
def test_request_error_mapping(http_client, respx_mock_client, status, exc):
    """Test that request error mapping."""
    http_client._executor.retry_config = RetryConfig(backoff_factor=0.0)
    respx_mock_client.get("/path").mock(return_value=httpx.Response(status, json={"err": "x"}))

    with pytest.raises(exc):
        http_client.get("/path")


def test_client_sends_auth_headers(http_client, respx_mock_client):
    """Test that client sends auth headers."""
    route = respx_mock_client.get("/headers").mock(return_value=httpx.Response(200, json={}))

    http_client.get("/headers")

    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "key"
    assert sent.headers["x-imn-security-key"] == "secret"
