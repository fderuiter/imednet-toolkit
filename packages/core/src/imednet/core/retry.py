"""Retry logic and policies for iMedNet API requests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx
from tenacity import AsyncRetrying, Retrying, stop_after_attempt, wait_random_exponential

from imednet.constants import DEFAULT_BACKOFF_FACTOR, DEFAULT_RETRIES

#: HTTP methods that are safe to retry automatically. Mutating methods such as
#: POST and PATCH are intentionally excluded because a server may have already
#: processed the request before the connection dropped, and retrying would risk
#: creating duplicate records or corrupting state.
IDEMPOTENT_METHODS: frozenset[str] = frozenset({"GET", "PUT", "DELETE", "HEAD", "OPTIONS"})


@dataclass
class RetryState:
    """State information passed to :class:`RetryPolicy`."""

    attempt_number: int
    exception: BaseException | None = None
    result: Any | None = None
    method: str | None = None


@runtime_checkable
class RetryPolicy(Protocol):
    """Interface to determine whether a request should be retried."""

    def should_retry(self, state: RetryState) -> bool:
        """Return ``True`` to retry the request for the given state."""


class DefaultRetryPolicy:
    """Retry policy with idempotency-aware retry logic.

    - **Network errors** (``httpx.RequestError``) and **server errors** (HTTP
      500-599) are only retried for idempotent HTTP methods:
      ``GET``, ``PUT``, ``DELETE``, ``HEAD``, and ``OPTIONS``.
    - **Rate-limit responses** (HTTP 429) are retried for *all* methods because
      the server rejected the request before processing the payload, so there is
      no risk of duplicate side-effects.
    - Requests with an unknown or missing method are treated as non-idempotent
      (fail-safe default): they are *not* retried on network errors or 5xx
      responses.

    **Overriding this behaviour**

    If you need retries on a ``POST`` endpoint that is internally deduplicated
    (e.g. the server uses an idempotency key), subclass :class:`RetryPolicy`
    and pass an instance to the client::

        from imednet.core.retry import RetryPolicy, RetryState, IDEMPOTENT_METHODS
        import httpx

        class IdempotentPostPolicy(RetryPolicy):
            def should_retry(self, state: RetryState) -> bool:
                method = (state.method or "").upper()
                if state.exception:
                    return isinstance(state.exception, httpx.RequestError)
                response = state.result
                if isinstance(response, httpx.Response):
                    return (
                        response.status_code == 429
                        or 500 <= response.status_code < 600
                    )
                return False

        sdk = ImednetSDK(..., retry_config=RetryConfig(retry_policy=IdempotentPostPolicy()))
    """

    def should_retry(self, state: RetryState) -> bool:
        """Return True if the request should be retried based on the state."""
        method = (state.method or "").upper()
        is_idempotent = method in IDEMPOTENT_METHODS

        if state.exception:
            return is_idempotent and isinstance(state.exception, httpx.RequestError)

        response = state.result
        if isinstance(response, httpx.Response):
            if response.status_code == 429:
                return True
            return is_idempotent and 500 <= response.status_code < 600

        return False


@dataclass
class RetryConfig:
    """Centralized configuration for retry behaviors."""

    retries: int = DEFAULT_RETRIES
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR
    retry_policy: Any = field(default_factory=DefaultRetryPolicy)

    def __post_init__(self) -> None:
        """Normalize None retry policies to DefaultRetryPolicy."""
        if self.retry_policy is None:
            self.retry_policy = DefaultRetryPolicy()

    def create_retryer(
        self,
        wait_strategy: Callable[[Any], float] | None = None,
        retry_predicate: Callable[[Any], bool] | None = None,
        **kwargs: Any,
    ) -> Retrying:
        """Create a synchronous retryer based on the configuration."""
        wait = wait_strategy or wait_random_exponential(multiplier=self.backoff_factor)
        return Retrying(
            stop=stop_after_attempt(self.retries + 1),
            wait=wait,
            retry=retry_predicate,  # type: ignore
            reraise=False,
            **kwargs,
        )

    def create_async_retryer(
        self,
        wait_strategy: Callable[[Any], float] | None = None,
        retry_predicate: Callable[[Any], bool] | None = None,
        **kwargs: Any,
    ) -> AsyncRetrying:
        """Create an asynchronous retryer based on the configuration."""
        wait = wait_strategy or wait_random_exponential(multiplier=self.backoff_factor)
        return AsyncRetrying(
            stop=stop_after_attempt(self.retries + 1),
            wait=wait,
            retry=retry_predicate,  # type: ignore
            reraise=False,
            **kwargs,
        )
