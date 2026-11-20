"""Demonstrate custom retry logic with simulated rate limit and server errors.

The script uses a mock transport that returns a 429, then a 500, before
succeeding. Set ``IMEDNET_API_KEY`` and ``IMEDNET_SECURITY_KEY`` to any value
(or enable ``IMEDNET_SIMULATION_MODE=true``) before running this script.

Example:

    export IMEDNET_API_KEY="dummy"
    export IMEDNET_SECURITY_KEY="dummy"
    uv run --with "imednet[cli]" python examples/custom_retry.py
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator

import httpx

from imednet.auth.strategy import AuthStrategy
from imednet.core.client import Client
from imednet.core.retry import RetryConfig, RetryPolicy, RetryState
from imednet.errors import RateLimitError, ServerError
from imednet.utils import configure_json_logging


class RateLimitServerRetry(RetryPolicy):
    """Retry requests that fail with rate limit (429) or server (5xx) errors."""

    def should_retry(self, state: RetryState) -> bool:
        """Return True when the failed attempt encountered a retryable error or status."""
        if isinstance(state.exception, (RateLimitError, ServerError)):
            return True
        if isinstance(state.result, httpx.Response):
            status = state.result.status_code
            return status == 429 or 500 <= status < 600
        return False


def _build_responses() -> Iterator[httpx.Response]:
    """Return a fresh sequence of 429, 500, and 200 mock responses."""
    return iter(
        [
            httpx.Response(429, json={"metadata": {"status": "TOO_MANY_REQUESTS"}}),
            httpx.Response(500, json={"metadata": {"status": "SERVER_ERROR"}}),
            httpx.Response(200, json={"data": "ok"}),
        ]
    )


class MockClient(Client):
    """HTTP client wired to an in-memory mock transport for retry testing."""

    def _create_client(self, auth: AuthStrategy) -> httpx.Client:
        """Create an httpx.Client backed by a fresh mock responder sequence."""
        responses = _build_responses()

        def responder(_: httpx.Request) -> httpx.Response:
            return next(responses)

        return httpx.Client(
            base_url=self.base_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **auth.get_headers(),
            },
            timeout=self.timeout,
            transport=httpx.MockTransport(responder),
        )


def main() -> None:
    """Run a request with custom retry logic."""
    configure_json_logging()

    sim_mode = os.getenv("IMEDNET_SIMULATION_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    api_key = os.getenv("IMEDNET_API_KEY") or ("sim-api-placeholder" if sim_mode else None)
    security_key = os.getenv("IMEDNET_SECURITY_KEY") or (
        "sim-sec-placeholder" if sim_mode else None
    )
    missing = [
        name
        for name, val in (
            ("IMEDNET_API_KEY", api_key),
            ("IMEDNET_SECURITY_KEY", security_key),
        )
        if not val
    ]
    if missing:
        vars_ = ", ".join(missing)
        print(f"Missing required environment variable(s): {vars_}", file=sys.stderr)
        sys.exit(1)

    client = MockClient(
        api_key=api_key,
        security_key=security_key,
        retry_config=RetryConfig(
            retries=3,
            backoff_factor=0.01,
            retry_policy=RateLimitServerRetry(),
        ),
    )

    try:
        with client:
            client.get("/studies")
    except RateLimitError:
        print("Request failed with rate limit error", file=sys.stderr)
        sys.exit(1)
    except ServerError:
        print("Request failed with server error", file=sys.stderr)
        sys.exit(1)
    else:
        print("Request succeeded")


if __name__ == "__main__":
    main()
