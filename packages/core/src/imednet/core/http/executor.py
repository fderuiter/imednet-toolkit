"""HTTP request execution with retries and monitoring."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    RetryCallState,
    RetryError,
    wait_random_exponential,
)

from imednet.core.http.handlers import handle_response
from imednet.core.http.monitor import RequestMonitor
from imednet.core.operations.circuit_breaker import get_global_circuit_breaker
from imednet.core.retry import RetryConfig, RetryState

_SUPPRESSED_LOG_LEVEL = logging.CRITICAL + 1

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer
else:
    Tracer = Any


class BaseRequestExecutor(ABC):
    """Abstract base for request executors."""

    def __init__(
        self,
        send: Any,
        tracer: Tracer | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Initialize the request executor.

        Args:
            send: The function to use for sending requests (sync or async).
            tracer: Optional OpenTelemetry tracer for monitoring.
            retry_config: Centralized configuration for retry behaviors.
        """
        self.send = send
        self.tracer = tracer
        self.retry_config = retry_config or RetryConfig()
        self._jitter_wait = wait_random_exponential(multiplier=self.retry_config.backoff_factor)

    @staticmethod
    @contextmanager
    def _suppress_httpx_request_logging() -> Iterator[None]:
        """Context manager to suppress low-level HTTPX logging.

        Yields:
            None
        """
        loggers = {
            "httpx": logging.getLogger("httpx"),
            "httpcore": logging.getLogger("httpcore"),
        }
        logger_states = {name: logger.level for name, logger in loggers.items()}
        for logger in loggers.values():
            logger.setLevel(_SUPPRESSED_LOG_LEVEL)
        try:
            yield
        finally:
            for name, original_level in logger_states.items():
                loggers[name].setLevel(original_level)

    def _get_retry_predicate(self, method: str) -> Callable[[RetryCallState], bool]:
        """Return a retry predicate that includes the HTTP method in state."""
        policy = self.retry_config.retry_policy

        def should_retry(retry_state: RetryCallState) -> bool:
            """Determine if the request should be retried based on retry state.

            Args:
                retry_state: Tenacity's retry state.

            Returns:
                bool: True if the request should be retried.
            """
            state = RetryState(
                attempt_number=retry_state.attempt_number,
                exception=(
                    retry_state.outcome.exception()
                    if retry_state.outcome and retry_state.outcome.failed
                    else None
                ),
                result=(
                    retry_state.outcome.result()
                    if retry_state.outcome and not retry_state.outcome.failed
                    else None
                ),
                method=method,
            )
            return policy.should_retry(state)  # type: ignore[no-any-return]

        return should_retry

    def _process_result(
        self, response: httpx.Response | None, monitor: RequestMonitor
    ) -> httpx.Response:
        """Process successful response or raise error if None."""
        if response is not None:
            # We treat successful HTTP requests (even 4xx/5xx that don't raise here)
            # as a successful "connection" probe for the circuit breaker, but let's
            # check the response. If it's a 5xx, it might be an outage.
            # Usually, retries cover 5xx, but if we reach here and it's a 5xx that wasn't retried,
            # we should record failure? No, let's just record success if we got a response
            # that is not an exception, OR wait, an API outage usually manifests as connection errors
            # or 5xx. If handle_response raises, it will throw an exception below.

            # Record success before handle_response so if it raises, we know we got a response at least.
            # But 5xx server errors indicate failures. Let's record success only if status is < 500.
            if response.status_code < 500:
                get_global_circuit_breaker().record_success()
            else:
                get_global_circuit_breaker().record_failure()

            monitor.on_success(response)
            return handle_response(response)
        raise RuntimeError("Request failed without response or exception")

    def _process_retry_error(self, e: RetryError, monitor: RequestMonitor) -> httpx.Response:
        """Handle RetryError, extracting successful result if present, else escalate."""
        # A RetryError means we exhausted retries (which indicates consecutive failures)
        get_global_circuit_breaker().record_failure()

        if e.last_attempt and not e.last_attempt.failed:
            response: httpx.Response = e.last_attempt.result()
            monitor.on_success(response)
            return handle_response(response)
        monitor.on_retry_error(e)
        raise e

    @staticmethod
    def _parse_retry_after_seconds(response: httpx.Response) -> float | None:
        """Parse the 'Retry-After' header from an HTTP response.

        Args:
            response: The HTTP response containing the header.

        Returns:
            Optional[float]: The number of seconds to wait, or None if not present or invalid.
        """
        value = response.headers.get("Retry-After")
        if not value:
            return None

        try:
            delay = float(value)
            return max(delay, 0.0)
        except ValueError:
            pass

        try:
            retry_time = parsedate_to_datetime(value)
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=UTC)
            delay = (retry_time - datetime.now(UTC)).total_seconds()
            return max(delay, 0.0)
        except (TypeError, ValueError, OverflowError):
            return None

    def _wait_strategy(self, retry_state: RetryCallState) -> float:
        """Calculate the wait time before the next retry attempt.

        Args:
            retry_state: Tenacity's retry state.

        Returns:
            float: Number of seconds to wait.
        """
        if retry_state.outcome and not retry_state.outcome.failed:
            result = retry_state.outcome.result()
            if isinstance(result, httpx.Response):
                retry_after_seconds = self._parse_retry_after_seconds(result)
                if retry_after_seconds is not None:
                    return retry_after_seconds
        return float(self._jitter_wait(retry_state))

    def _get_retryer_kwargs(self, method: str) -> dict[str, Any]:
        """Get common arguments for Retrying and AsyncRetrying."""
        return {
            "wait_strategy": self._wait_strategy,
            "retry_predicate": self._get_retry_predicate(method),
        }

    def _prepare_request(self) -> None:
        """Perform pre-flight checks before executing the request."""
        get_global_circuit_breaker().check_request_allowed()

    def _handle_exception(self, e: Exception, monitor: RequestMonitor) -> httpx.Response:
        """Handle exceptions raised during request execution."""
        if isinstance(e, RetryError):
            return self._process_retry_error(e, monitor)
        get_global_circuit_breaker().record_failure()
        raise

    @abstractmethod
    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        """Execute the request."""


class SyncRequestExecutor(BaseRequestExecutor):
    """Execute synchronous HTTP requests with retry and error handling."""

    def __init__(
        self,
        send: Callable[..., httpx.Response],
        tracer: Tracer | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Initialize the synchronous request executor.

        Args:
            send: The synchronous function to use for sending requests.
            tracer: Optional OpenTelemetry tracer for monitoring.
            retry_config: Centralized configuration for retry behaviors.
        """
        super().__init__(send, tracer, retry_config)
        # self.send is set in super

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Execute the request synchronously.

        Args:
            method: HTTP method.
            url: Request URL.
            **kwargs: Additional arguments for the request.

        Returns:
            httpx.Response: The HTTP response.

        Raises:
            CircuitBreakerError: If the circuit is open.
            Exception: Re-raises exceptions from the send function or retryer.
        """
        self._prepare_request()

        def send_fn() -> httpx.Response:
            """Wrapper to send the request with suppressed logging.

            Returns:
                httpx.Response: The HTTP response.
            """
            with self._suppress_httpx_request_logging():
                return self.send(method, url, **kwargs)  # type: ignore[no-any-return]

        retryer = self.retry_config.create_retryer(**self._get_retryer_kwargs(method))

        with RequestMonitor(self.tracer, method, url) as monitor:
            try:
                response: httpx.Response | None = retryer(send_fn)
                return self._process_result(response, monitor)
            except Exception as e:
                return self._handle_exception(e, monitor)


class AsyncRequestExecutor(BaseRequestExecutor):
    """Execute asynchronous HTTP requests with retry and error handling."""

    def __init__(
        self,
        send: Callable[..., Awaitable[httpx.Response]],
        tracer: Tracer | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Initialize the asynchronous request executor.

        Args:
            send: The asynchronous function to use for sending requests.
            tracer: Optional OpenTelemetry tracer for monitoring.
            retry_config: Centralized configuration for retry behaviors.
        """
        super().__init__(send, tracer, retry_config)
        # self.send is set in super

    async def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Execute the request asynchronously.

        Args:
            method: HTTP method.
            url: Request URL.
            **kwargs: Additional arguments for the request.

        Returns:
            httpx.Response: The HTTP response.

        Raises:
            CircuitBreakerError: If the circuit is open.
            Exception: Re-raises exceptions from the send function or retryer.
        """
        self._prepare_request()

        async def send_fn() -> httpx.Response:
            """Wrapper to send the request with suppressed logging.

            Returns:
                httpx.Response: The HTTP response.
            """
            with self._suppress_httpx_request_logging():
                return await self.send(method, url, **kwargs)  # type: ignore[no-any-return]

        retryer = self.retry_config.create_async_retryer(**self._get_retryer_kwargs(method))

        async with RequestMonitor(self.tracer, method, url) as monitor:
            try:
                response: httpx.Response | None = await retryer(send_fn)
                return self._process_result(response, monitor)
            except Exception as e:
                return self._handle_exception(e, monitor)
