"""Unit tests for transport contract."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from imednet import errors
from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.retry import RetryConfig
from imednet.sdk import AsyncImednetSDK, ImednetSDK


@respx.mock(base_url="https://example.com")
def test_retry_contract_by_method_class() -> None:
    """Test that retry contract by method class."""
    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=2, backoff_factor=0.0),
    )

    get_route = respx.get("/retry-get").mock(return_value=httpx.Response(503))
    with pytest.raises(errors.ServerError):
        sdk._client.get("/retry-get")
    assert get_route.call_count == 3

    post_server_error = respx.post("/retry-post-503").mock(return_value=httpx.Response(503))
    with pytest.raises(errors.ServerError):
        sdk._client.post("/retry-post-503", json={})
    assert post_server_error.call_count == 1

    post_rate_limited = respx.post("/retry-post-429").mock(return_value=httpx.Response(429))
    with pytest.raises(errors.RateLimitError):
        sdk._client.post("/retry-post-429", json={})
    assert post_rate_limited.call_count == 3


@pytest.mark.asyncio
@respx.mock(base_url="https://example.com")
async def test_transport_clients_use_base_url_for_relative_paths() -> None:
    """Test that transport clients use base url for relative paths asynchronously."""
    sync_client = Client(api_key="k", security_key="s", base_url="https://example.com")
    assert str(sync_client._client.base_url) == "https://example.com"

    sync_get_route = respx.get("/relative-get").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    sync_post_route = respx.post("/relative-post").mock(
        return_value=httpx.Response(201, json={"created": True})
    )

    get_response = sync_client.get("/relative-get")
    post_response = sync_client.post("/relative-post", json={"x": 1})

    assert get_response.status_code == 200
    assert get_response.json() == {"ok": True}
    assert post_response.status_code == 201
    assert post_response.json() == {"created": True}
    assert sync_get_route.call_count == 1
    assert sync_post_route.call_count == 1
    sync_client.close()

    async with AsyncClient(
        api_key="k", security_key="s", base_url="https://example.com"
    ) as async_client:
        assert str(async_client._client.base_url) == "https://example.com"

        async_get_route = respx.get("/async-relative-get").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async_post_route = respx.post("/async-relative-post").mock(
            return_value=httpx.Response(201, json={"created": True})
        )

        async_get_response = await async_client.get("/async-relative-get")
        async_post_response = await async_client.post("/async-relative-post", json={"x": 1})

        assert async_get_response.status_code == 200
        assert async_get_response.json() == {"ok": True}
        assert async_post_response.status_code == 201
        assert async_post_response.json() == {"created": True}
        assert async_get_route.call_count == 1
        assert async_post_route.call_count == 1


@respx.mock(base_url="https://example.com")
def test_retry_after_header_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that retry after header is respected."""
    import imednet.core.retry as retry_module

    sleeps: list[float] = []
    original_retrying = retry_module.Retrying

    def retrying_with_captured_sleep(*args, **kwargs):
        """Helper function to retrying with captured sleep."""
        kwargs["sleep"] = lambda seconds: sleeps.append(seconds)
        return original_retrying(*args, **kwargs)

    monkeypatch.setattr(retry_module, "Retrying", retrying_with_captured_sleep)

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=1),
    )
    route = respx.get("/rate-limited").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    response = sdk._client.get("/rate-limited")

    assert response.status_code == 200
    assert route.call_count == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_timeout_propagates_to_httpx_clients() -> None:
    """Test that timeout propagates to httpx clients asynchronously."""
    timeout = httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)

    sync_client = Client(
        api_key="k", security_key="s", base_url="https://example.com", timeout=timeout
    )
    assert sync_client._client.timeout.connect == 1.0
    assert sync_client._client.timeout.read == 2.0
    assert sync_client._client.timeout.write == 3.0
    assert sync_client._client.timeout.pool == 4.0
    sync_client.close()

    async_client = AsyncClient(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        timeout=timeout,
    )
    assert async_client._client.timeout.connect == 1.0
    assert async_client._client.timeout.read == 2.0
    assert async_client._client.timeout.write == 3.0
    assert async_client._client.timeout.pool == 4.0
    await async_client.aclose()

    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://example.com", timeout=7.5)
    assert sdk._client._client.timeout.connect == 7.5
    sdk.close()

    async_sdk = AsyncImednetSDK(
        api_key="k", security_key="s", base_url="https://example.com", timeout=6.5
    )
    assert async_sdk._async_client._client.timeout.connect == 6.5
    await async_sdk.aclose()


@pytest.mark.parametrize(
    ("status_code", "exception_type"),
    [
        (400, errors.BadRequestError),
        (401, errors.UnauthorizedError),
        (403, errors.ForbiddenError),
        (404, errors.NotFoundError),
        (409, errors.ConflictError),
        (429, errors.RateLimitError),
        (500, errors.ServerError),
        (502, errors.ServerError),
        (503, errors.ServerError),
    ],
)
@respx.mock(base_url="https://example.com")
def test_status_code_error_mapping_contract(
    status_code: int, exception_type: type[Exception]
) -> None:
    """Test that status code error mapping contract."""
    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=0),
    )
    respx.get("/error-map").mock(return_value=httpx.Response(status_code, json={"error": "x"}))

    with pytest.raises(exception_type):
        sdk._client.get("/error-map")


@respx.mock(base_url="https://example.com")
def test_credentials_are_redacted_from_transport_logs_and_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that credentials are redacted from transport logs and exceptions."""
    api_key = "very-secret-api-key"
    security_key = "very-secret-security-key"
    token = "very-secret-token"
    sensitive_path = f"/resource?api_key={api_key}&security_key={security_key}&token={token}"

    sdk = ImednetSDK(
        api_key=api_key,
        security_key=security_key,
        base_url="https://example.com",
        retry_config=RetryConfig(retries=0),
    )

    respx.get(sensitive_path).mock(return_value=httpx.Response(200, json={"ok": True}))
    with caplog.at_level(logging.INFO, logger="imednet.core.http.monitor"):
        sdk._client.get(sensitive_path)

    assert caplog.records
    success_record = caplog.records[-1]
    assert api_key not in success_record.url
    assert security_key not in success_record.url
    assert token not in success_record.url
    assert "api_key=***" in success_record.url
    assert "security_key=***" in success_record.url
    assert "token=***" in success_record.url

    def connect_error(request: httpx.Request) -> httpx.Response:
        """Helper function to connect error."""
        raise httpx.ConnectError("connection failed", request=request)

    respx.get("/network-failure").mock(side_effect=connect_error)
    with caplog.at_level(logging.ERROR, logger="imednet.core.http.monitor"):
        with pytest.raises(errors.RequestError) as exc_info:
            sdk._client.get("/network-failure")

    monitor_logs = [
        record for record in caplog.records if record.name == "imednet.core.http.monitor"
    ]

    assert api_key not in str(exc_info.value)
    assert security_key not in str(exc_info.value)
    assert token not in str(exc_info.value)
    assert any("Request failed after retries" in record.getMessage() for record in monitor_logs)
    assert all(api_key not in record.getMessage() for record in monitor_logs)
    assert all(security_key not in record.getMessage() for record in monitor_logs)
    assert all(token not in record.getMessage() for record in monitor_logs)
