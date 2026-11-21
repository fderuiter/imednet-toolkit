"""Unit tests for retry."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from imednet.core.http.executor import AsyncRequestExecutor, SyncRequestExecutor
from imednet.core.retry import RetryConfig
from imednet.errors import RequestError


def test_retry_exhaustion_sync():
    """Verify that RequestError is raised after retries are exhausted in sync mode."""
    req = httpx.Request("GET", "http://test.com/studies?api_key=raw-secret-val&page=1")
    mock_send = Mock(side_effect=httpx.ConnectError("Connection failed", request=req))

    executor = SyncRequestExecutor(
        send=mock_send, retry_config=RetryConfig(retries=2, backoff_factor=0.01)
    )

    with pytest.raises(RequestError) as exc_info:
        executor("GET", "/studies")

    err = exc_info.value
    assert "Network request failed after retries" in str(err)
    assert "raw-secret-val" not in str(err)
    assert err.method == "GET"
    assert err.url == "http://test.com/studies?api_key=***&page=1"
    assert err.attempt_count == 3
    assert err.cause_type == "ConnectError"
    assert isinstance(err.__cause__, httpx.ConnectError)
    assert mock_send.call_count == 3


@pytest.mark.asyncio
async def test_retry_exhaustion_async():
    """Verify that RequestError is raised after retries are exhausted in async mode."""
    mock_send = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))

    executor = AsyncRequestExecutor(
        send=mock_send, retry_config=RetryConfig(retries=2, backoff_factor=0.01)
    )

    with pytest.raises(RequestError) as exc_info:
        await executor("GET", "http://test.com?token=raw-token-val")

    err = exc_info.value
    assert "Network request failed after retries" in str(err)
    assert "raw-token-val" not in str(err)
    assert err.method == "GET"
    assert err.url == "http://test.com?token=***"
    assert err.attempt_count == 3
    assert err.cause_type == "ConnectError"
    assert isinstance(err.__cause__, httpx.ConnectError)
    assert mock_send.call_count == 3


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_non_idempotent_method_does_not_retry_on_connect_error_sync(method: str):
    """Non-idempotent methods must not retry on network errors and wrap in RequestError (sync)."""
    req = httpx.Request(method, "http://test.com/records?security_key=raw-sec")
    mock_send = Mock(side_effect=httpx.ConnectError("Connection failed", request=req))

    executor = SyncRequestExecutor(
        send=mock_send, retry_config=RetryConfig(retries=3, backoff_factor=0.01)
    )

    with pytest.raises(RequestError) as exc_info:
        executor(method, "/records")

    # Must fail fast: only one attempt, no retries, wrapped in RequestError with context
    assert mock_send.call_count == 1
    err = exc_info.value
    assert isinstance(err.__cause__, httpx.ConnectError)
    assert err.method == method
    assert err.url == "http://test.com/records?security_key=***"
    assert err.attempt_count == 1
    assert err.cause_type == "ConnectError"
    assert "raw-sec" not in str(err)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PATCH"])
async def test_non_idempotent_method_does_not_retry_on_connect_error_async(method: str):
    """Non-idempotent methods must not retry on network errors and wrap in RequestError (async)."""
    mock_send = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))

    executor = AsyncRequestExecutor(
        send=mock_send, retry_config=RetryConfig(retries=3, backoff_factor=0.01)
    )

    with pytest.raises(RequestError) as exc_info:
        await executor(method, "http://test.com?api_key=raw-key")

    # Must fail fast: only one attempt, no retries, wrapped in RequestError with context
    assert mock_send.call_count == 1
    err = exc_info.value
    assert isinstance(err.__cause__, httpx.ConnectError)
    assert err.method == method
    assert err.url == "http://test.com?api_key=***"
    assert err.attempt_count == 1
    assert err.cause_type == "ConnectError"
    assert "raw-key" not in str(err)
