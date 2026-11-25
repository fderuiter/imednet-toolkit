"""Unit tests for retry."""

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from imednet.core.http.executor import (
    _SUPPRESSED_LOG_LEVEL,
    AsyncRequestExecutor,
    SyncRequestExecutor,
)
from imednet.core.http.handlers import handle_response, parse_retry_after_seconds
from imednet.core.operations.circuit_breaker import CircuitBreaker
from imednet.core.retry import RetryConfig
from imednet.errors import ImednetError, RateLimitError, RequestError


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


@pytest.mark.parametrize(
    "raw_header",
    ["NaN", "nan", "-nan", "inf", "+inf", "-inf", "Infinity", "-Infinity", "1e500", "   "],
)
def test_parse_retry_after_seconds_rejects_non_finite_values(raw_header: str) -> None:
    """parse_retry_after_seconds returns None for non-finite or blank Retry-After headers."""
    resp = httpx.Response(429, headers={"Retry-After": raw_header})
    assert parse_retry_after_seconds(resp) is None
    assert parse_retry_after_seconds(resp, max_retry_after=30.0) is None


def test_parse_retry_after_seconds_clamps_finite_delays_and_validates_bounds() -> None:
    """parse_retry_after_seconds clamps numeric and HTTP-date delays to max_retry_after."""
    resp_numeric = httpx.Response(429, headers={"Retry-After": "180"})
    assert parse_retry_after_seconds(resp_numeric) == 180.0
    assert parse_retry_after_seconds(resp_numeric, max_retry_after=45.0) == 45.0
    assert parse_retry_after_seconds(resp_numeric, max_retry_after=0.0) == 0.0

    resp_negative = httpx.Response(429, headers={"Retry-After": "-12"})
    assert parse_retry_after_seconds(resp_negative, max_retry_after=45.0) == 0.0

    future_date = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    resp_date = httpx.Response(429, headers={"Retry-After": future_date})
    assert parse_retry_after_seconds(resp_date, max_retry_after=12.5) == 12.5

    for invalid_cap in (-1.0, float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(ValueError, match="max_retry_after"):
            parse_retry_after_seconds(resp_numeric, max_retry_after=invalid_cap)  # type: ignore[arg-type]

    with pytest.raises(RateLimitError) as rl_capped:
        handle_response(resp_numeric, max_retry_after=22.0)
    assert rl_capped.value.retry_after == 22.0


@pytest.mark.parametrize("bad_header", ["NaN", "inf", "-inf"])
def test_sync_executor_429_non_finite_retry_after_uses_backoff_and_spares_breaker(
    bad_header: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SyncRequestExecutor retries 429 with non-finite Retry-After, raises RateLimitError(retry_after=None), and keeps breaker at 0."""
    sleeps: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)

    breaker = CircuitBreaker()
    mock_send = Mock(
        side_effect=lambda *_a, **_kw: httpx.Response(
            429,
            headers={"Retry-After": bad_header},
            json={"error": "rate limited"},
        )
    )
    executor = SyncRequestExecutor(
        send=mock_send,
        retry_config=RetryConfig(retries=2, backoff_factor=0.05, max_retry_after=15.0),
        circuit_breaker=breaker,
    )

    with (
        caplog.at_level(logging.INFO, logger="imednet.core.http.monitor"),
        pytest.raises(RateLimitError) as exc_info,
    ):
        executor("GET", "https://api.example.com/studies")

    err = exc_info.value
    assert isinstance(err, ImednetError)
    assert err.retry_after is None
    assert err.attempt_count == 3
    assert mock_send.call_count == 3
    assert len(sleeps) == 2
    assert all(math.isfinite(s) and s >= 0.0 for s in sleeps)
    assert breaker.consecutive_failures == 0
    monitor_logs = [r for r in caplog.records if r.name == "imednet.core.http.monitor"]
    assert [r.levelname for r in monitor_logs] == ["WARNING"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_header", ["NaN", "inf", "-inf"])
async def test_async_executor_429_non_finite_retry_after_uses_backoff_and_spares_breaker(
    bad_header: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AsyncRequestExecutor retries 429 with non-finite Retry-After, raises RateLimitError(retry_after=None), and keeps breaker at 0."""
    breaker = CircuitBreaker()
    mock_send = AsyncMock(
        side_effect=lambda *_a, **_kw: httpx.Response(
            429,
            headers={"Retry-After": bad_header},
            json={"error": "rate limited"},
        )
    )
    executor = AsyncRequestExecutor(
        send=mock_send,
        retry_config=RetryConfig(retries=2, backoff_factor=0.001, max_retry_after=10.0),
        circuit_breaker=breaker,
    )

    with (
        caplog.at_level(logging.INFO, logger="imednet.core.http.monitor"),
        pytest.raises(RateLimitError) as exc_info,
    ):
        await executor("GET", "https://api.example.com/studies")

    err = exc_info.value
    assert isinstance(err, ImednetError)
    assert err.retry_after is None
    assert err.attempt_count == 3
    assert mock_send.call_count == 3
    assert breaker.consecutive_failures == 0
    monitor_logs = [r for r in caplog.records if r.name == "imednet.core.http.monitor"]
    assert [r.levelname for r in monitor_logs] == ["WARNING"]

    # Also verify oversized finite Retry-After shares max_retry_after on exhausted RateLimitError
    capped_send = AsyncMock(
        return_value=httpx.Response(429, headers={"Retry-After": "900"}, json={"error": "slow"})
    )
    capped_exec = AsyncRequestExecutor(
        send=capped_send,
        retry_config=RetryConfig(retries=0, backoff_factor=0.0, max_retry_after=7.5),
        circuit_breaker=breaker,
    )
    with pytest.raises(RateLimitError) as capped_exc:
        await capped_exec("GET", "https://api.example.com/studies")
    assert capped_exc.value.retry_after == 7.5
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_interleaved_async_and_sync_executor_logger_suppression() -> None:
    """Interleaved calls keep httpx and httpcore suppressed until all complete and restore exact levels."""
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    prior_httpx = httpx_logger.level
    prior_httpcore = httpcore_logger.level

    httpx_logger.setLevel(logging.DEBUG)
    httpcore_logger.setLevel(logging.INFO)
    try:
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()
        call_index = 0

        async def controlled_send(method: str, url: str, **_kw: object) -> httpx.Response:
            nonlocal call_index
            call_index += 1
            idx = call_index
            assert httpx_logger.level == _SUPPRESSED_LOG_LEVEL
            assert httpcore_logger.level == _SUPPRESSED_LOG_LEVEL
            if idx == 1:
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()
                await release_second.wait()
            assert httpx_logger.level == _SUPPRESSED_LOG_LEVEL
            assert httpcore_logger.level == _SUPPRESSED_LOG_LEVEL
            return httpx.Response(200, json={"call": idx}, request=httpx.Request(method, url))

        async_exec = AsyncRequestExecutor(
            send=controlled_send,
            retry_config=RetryConfig(retries=0),
            circuit_breaker=CircuitBreaker(),
        )
        sync_exec = SyncRequestExecutor(
            send=lambda m, u, **_kw: httpx.Response(200, request=httpx.Request(m, u)),
            retry_config=RetryConfig(retries=0),
            circuit_breaker=CircuitBreaker(),
        )

        task_a = asyncio.create_task(async_exec("GET", "https://api.example.com/a"))
        await first_entered.wait()

        task_b = asyncio.create_task(async_exec("GET", "https://api.example.com/b"))
        await second_entered.wait()

        # Overlapping sync execution while both async calls are in flight
        sync_resp = sync_exec("GET", "https://api.example.com/sync")
        assert sync_resp.status_code == 200
        assert httpx_logger.level == _SUPPRESSED_LOG_LEVEL
        assert httpcore_logger.level == _SUPPRESSED_LOG_LEVEL

        # Finish task A first while task B remains in flight -> must stay suppressed
        release_first.set()
        resp_a = await task_a
        assert resp_a.status_code == 200
        assert httpx_logger.level == _SUPPRESSED_LOG_LEVEL
        assert httpcore_logger.level == _SUPPRESSED_LOG_LEVEL

        # Finish task B -> exact starting levels restored (not frozen at 51)
        release_second.set()
        resp_b = await task_b
        assert resp_b.status_code == 200
        assert httpx_logger.level == logging.DEBUG
        assert httpcore_logger.level == logging.INFO
    finally:
        httpx_logger.setLevel(prior_httpx)
        httpcore_logger.setLevel(prior_httpcore)
