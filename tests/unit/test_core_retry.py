"""Unit tests for core retry."""

import httpx
import pytest

from imednet.core.retry import DefaultRetryPolicy, RetryState


def test_default_retry_policy_retries_on_network_errors():
    """Test that default retry policy retries on network errors."""
    policy = DefaultRetryPolicy()

    # Simulate a network request error for an idempotent method (GET)
    request = httpx.Request("GET", "https://example.com")
    error = httpx.RequestError("Network connection failed", request=request)
    state = RetryState(attempt_number=1, exception=error, method="GET")

    assert policy.should_retry(state) is True


def test_default_retry_policy_retries_on_rate_limits():
    """Test that default retry policy retries on rate limits."""
    policy = DefaultRetryPolicy()

    # Simulate a 429 Too Many Requests response — retried regardless of method
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(429, request=request)
    state = RetryState(attempt_number=1, result=response)

    assert policy.should_retry(state) is True


def test_default_retry_policy_retries_on_server_errors():
    """Test that default retry policy retries on server errors."""
    policy = DefaultRetryPolicy()

    # Simulate 500-599 Server Error responses for an idempotent method (GET)
    request = httpx.Request("GET", "https://example.com")

    for status_code in [500, 502, 503, 504, 599]:
        response = httpx.Response(status_code, request=request)
        state = RetryState(attempt_number=1, result=response, method="GET")
        assert policy.should_retry(state) is True


def test_default_retry_policy_does_not_retry_on_client_errors():
    """Test that default retry policy does not retry on client errors."""
    policy = DefaultRetryPolicy()

    # Simulate 400-499 Client Error responses (excluding 429)
    request = httpx.Request("GET", "https://example.com")

    for status_code in [400, 401, 403, 404, 422]:
        response = httpx.Response(status_code, request=request)
        state = RetryState(attempt_number=1, result=response, method="GET")
        assert policy.should_retry(state) is False


def test_default_retry_policy_does_not_retry_on_success():
    """Test that default retry policy does not retry on success."""
    policy = DefaultRetryPolicy()

    # Simulate 200-299 Success responses
    request = httpx.Request("GET", "https://example.com")

    for status_code in [200, 201, 204]:
        response = httpx.Response(status_code, request=request)
        state = RetryState(attempt_number=1, result=response, method="GET")
        assert policy.should_retry(state) is False


def test_default_retry_policy_does_not_retry_on_unrelated_exceptions():
    """Test that default retry policy does not retry on unrelated exceptions."""
    policy = DefaultRetryPolicy()

    # Simulate an exception that is not a RequestError
    error = ValueError("Something went wrong")
    state = RetryState(attempt_number=1, exception=error, method="GET")

    assert policy.should_retry(state) is False


def test_default_retry_policy_with_no_result_or_exception():
    """Test that default retry policy with no result or exception."""
    policy = DefaultRetryPolicy()

    # Simulate an empty state
    state = RetryState(attempt_number=1)

    assert policy.should_retry(state) is False


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_default_retry_policy_does_not_retry_non_idempotent_on_network_error(method: str):
    """Test that default retry policy does not retry non idempotent on network error."""
    policy = DefaultRetryPolicy()

    request = httpx.Request(method, "https://example.com")
    error = httpx.ConnectError("Connection failed", request=request)
    state = RetryState(attempt_number=1, exception=error, method=method)

    # Non-idempotent methods must never be retried on network errors
    assert policy.should_retry(state) is False


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_default_retry_policy_does_not_retry_non_idempotent_on_server_error(method: str):
    """Test that default retry policy does not retry non idempotent on server error."""
    policy = DefaultRetryPolicy()

    request = httpx.Request(method, "https://example.com")
    response = httpx.Response(503, request=request)
    state = RetryState(attempt_number=1, result=response, method=method)

    # Non-idempotent methods must never be retried on 5xx responses
    assert policy.should_retry(state) is False


def test_default_retry_policy_retries_post_on_rate_limit():
    """Test that default retry policy retries post on rate limit."""
    policy = DefaultRetryPolicy()

    # 429 means the server rejected the request before processing it, so
    # retrying a POST is safe even under the default policy.
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(429, request=request)
    state = RetryState(attempt_number=1, result=response, method="POST")

    assert policy.should_retry(state) is True


def test_default_retry_policy_does_not_retry_unknown_method_on_network_error():
    """Test that default retry policy does not retry unknown method on network error."""
    policy = DefaultRetryPolicy()

    # Unknown / missing method → fail-safe: do not retry
    request = httpx.Request("GET", "https://example.com")
    error = httpx.RequestError("timeout", request=request)
    state = RetryState(attempt_number=1, exception=error, method=None)

    assert policy.should_retry(state) is False


def test_default_retry_policy_does_not_retry_unknown_method_on_server_error():
    """Test that default retry policy does not retry unknown method on server error."""
    policy = DefaultRetryPolicy()

    # Unknown / missing method → fail-safe: do not retry
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(500, request=request)
    state = RetryState(attempt_number=1, result=response, method=None)

    assert policy.should_retry(state) is False


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "HEAD", "OPTIONS"])
def test_default_retry_policy_retries_idempotent_methods_on_408(method: str) -> None:
    """DefaultRetryPolicy retries HTTP 408 Request Timeout for idempotent methods."""
    policy = DefaultRetryPolicy()
    request = httpx.Request(method, "https://example.com")
    response = httpx.Response(408, request=request)
    state = RetryState(attempt_number=1, result=response, method=method)

    assert policy.should_retry(state) is True


@pytest.mark.parametrize("method", ["POST", "PATCH", None])
def test_default_retry_policy_does_not_retry_non_idempotent_or_unknown_on_408(
    method: str | None,
) -> None:
    """DefaultRetryPolicy never retries HTTP 408 for non-idempotent or unknown methods."""
    policy = DefaultRetryPolicy()
    request = httpx.Request(method or "POST", "https://example.com")
    response = httpx.Response(408, request=request)
    state = RetryState(attempt_number=1, result=response, method=method)

    assert policy.should_retry(state) is False
