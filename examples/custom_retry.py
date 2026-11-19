"""TODO: Add docstring."""

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

"""Demonstrate custom retry logic with simulated rate limit and server errors.

The script uses a mock transport that returns a 429, then a 500, before
succeeding. Set ``IMEDNET_API_KEY`` and ``IMEDNET_SECURITY_KEY`` to any value
before running this script.

Example:

    export IMEDNET_API_KEY="dummy"
    export IMEDNET_SECURITY_KEY="dummy"
    uv run --with "imednet[cli]" python examples/custom_retry.py
"""


class RateLimitServerRetry(RetryPolicy):
    """TODO: Add docstring."""

    def should_retry(self, state: RetryState) -> bool:
        """TODO: Add docstring."""
        if isinstance(state.exception, (RateLimitError, ServerError)):
            return True
        if isinstance(state.result, httpx.Response):
            status = state.result.status_code
            return status == 429 or 500 <= status < 600
        return False


responses: Iterator[httpx.Response] = iter(
    [
        httpx.Response(429, json={"metadata": {"status": "TOO_MANY_REQUESTS"}}),
        httpx.Response(500, json={"metadata": {"status": "SERVER_ERROR"}}),
        httpx.Response(200, json={"data": "ok"}),
    ]
)


def responder(_: httpx.Request) -> httpx.Response:
    """TODO: Add docstring."""
    return next(responses)


class MockClient(Client):
    """TODO: Add docstring."""

    def _create_client(self, auth: AuthStrategy) -> httpx.Client:
        """TODO: Add docstring."""
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

    missing = [var for var in ("IMEDNET_API_KEY", "IMEDNET_SECURITY_KEY") if not os.getenv(var)]
    if missing:
        vars_ = ", ".join(missing)
        print(f"Missing required environment variable(s): {vars_}", file=sys.stderr)
        sys.exit(1)

    client = MockClient(
        os.environ["IMEDNET_API_KEY"],
        os.environ["IMEDNET_SECURITY_KEY"],
        retry_config=RetryConfig(
            retries=3,
            backoff_factor=0.01,
            retry_policy=RateLimitServerRetry(),
        ),
    )

    try:
        client.get("/studies")
    except RateLimitError:
        print("Request failed with rate limit error", file=sys.stderr)
    except ServerError:
        print("Request failed with server error", file=sys.stderr)
    else:
        print("Request succeeded")


if __name__ == "__main__":
    main()
