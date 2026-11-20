"""Unit tests for core async client extended."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from imednet import errors
from imednet.core.async_client import AsyncClient
from imednet.core.retry import RetryConfig


@pytest.mark.asyncio
async def test_async_request_retries():
    """Test that async request retries asynchronously."""
    async with AsyncClient(
        "k",
        "s",
        base_url="https://api.test",
        retry_config=RetryConfig(retries=1, backoff_factor=0.0),
    ) as client:
        calls = {"count": 0}

        async def request(request: httpx.Request) -> httpx.Response:
            """Helper function to request."""
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpx.RequestError("boom", request=request)
            return httpx.Response(200, json={"ok": True})

        with respx.mock(assert_all_called=True, assert_all_mocked=True) as respx_mock:
            respx_mock.get("https://api.test/path").mock(side_effect=request)
            resp = await client.get("/path")

    assert resp.status_code == 200
    assert calls["count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (400, errors.ValidationError),
        (401, errors.AuthenticationError),
        (403, errors.AuthorizationError),
        (404, errors.NotFoundError),
        (429, errors.RateLimitError),
        (500, errors.ServerError),
        (418, errors.ApiError),
    ],
)
async def test_async_request_error_mapping(status, exc):
    """Test that async request error mapping asynchronously."""
    async with AsyncClient(
        "k",
        "s",
        base_url="https://api.test",
        retry_config=RetryConfig(backoff_factor=0.0),
    ) as client:
        with respx.mock(assert_all_called=True, assert_all_mocked=True) as respx_mock:
            respx_mock.get("https://api.test/some").respond(
                status_code=status, json={"error": status}
            )

            with pytest.raises(exc):
                await client.get("/some")


@pytest.mark.asyncio
async def test_tracing():
    """Test that tracing asynchronously."""
    tracer = MagicMock()
    span_cm = AsyncMock()
    span = MagicMock()
    span_cm.__aenter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    async with AsyncClient("k", "s", base_url="https://api.test", tracer=tracer) as client:
        with respx.mock(assert_all_called=True, assert_all_mocked=True) as respx_mock:
            respx_mock.get("https://api.test/trace").respond(status_code=200, json={"ok": True})
            await client.get("/trace")

    tracer.start_as_current_span.assert_called_with(
        "http_request", attributes={"endpoint": "/trace", "method": "GET"}
    )
    span.set_attribute.assert_called_with("status_code", 200)
