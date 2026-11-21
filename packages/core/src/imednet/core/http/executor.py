"""HTTP request execution with retries and monitoring."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import urlencode

import httpx
from tenacity import (
    RetryCallState,
    RetryError,
    wait_random_exponential,
)

from imednet.core.http.handlers import handle_response, parse_retry_after_seconds
from imednet.core.http.monitor import RequestMonitor
from imednet.core.operations.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    ProbeLease,
    get_global_circuit_breaker,
)
from imednet.core.retry import RetryConfig, RetryState
from imednet.errors import DiagnosticContext
from imednet.utils.url import redact_url_query

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
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize the request executor.

        Args:
            send: The function to use for sending requests (sync or async).
            tracer: Optional OpenTelemetry tracer for monitoring.
            retry_config: Centralized configuration for retry behaviors.
            circuit_breaker: Scoped circuit breaker instance for this executor.
        """
        self.send = send
        self.tracer = tracer
        self.retry_config = retry_config or RetryConfig()
        self.circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else get_global_circuit_breaker()
        )
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
            exc = (
                retry_state.outcome.exception()
                if retry_state.outcome and retry_state.outcome.failed
                else None
            )
            if exc is not None and not isinstance(exc, Exception):
                return False
            state = RetryState(
                attempt_number=retry_state.attempt_number,
                exception=exc,
                result=(
                    retry_state.outcome.result()
                    if retry_state.outcome and not retry_state.outcome.failed
                    else None
                ),
                method=method,
            )
            return policy.should_retry(state)  # type: ignore[no-any-return]

        return should_retry

    @staticmethod
    def _record_response_outcome(response: httpx.Response, lease: ProbeLease) -> None:
        """Record a single circuit-breaker outcome for an HTTP response before error mapping."""
        status = response.status_code
        if status == 429:
            lease.record_rate_limit()
        elif status >= 500:
            lease.record_failure()
        else:
            lease.record_success()

    @staticmethod
    def _build_monitor_url(url: str, kwargs: Mapping[str, Any]) -> str:
        """Include query params in the monitored URL when present and sanitize sensitive values."""
        params = kwargs.get("params")
        if isinstance(params, Mapping) and params and "?" not in url:
            query = urlencode(list(params.items()), doseq=True)
            if query:
                return redact_url_query(f"{url}?{query}")
        return url

    def _finalize_response(
        self,
        response: httpx.Response | None,
        lease: ProbeLease,
        monitor: RequestMonitor,
        *,
        attempt_count: int | None = None,
    ) -> httpx.Response:
        """Classify breaker outcome once, record telemetry, and map HTTP error responses."""
        if response is None:
            lease.record_failure()
            raise RuntimeError("Request failed without response or exception")
        self._record_response_outcome(response, lease)
        monitor.on_success(response)
        return handle_response(
            response,
            method=monitor.method,
            url=monitor.safe_url,
            attempt_count=attempt_count,
        )

    def _extract_or_fail_retry_error(
        self,
        error: RetryError,
        lease: ProbeLease,
        monitor: RequestMonitor,
        *,
        attempt_count: int | None = None,
    ) -> httpx.Response:
        """Extract response from exhausted retries or record one transport failure and escalate."""
        if error.last_attempt and not error.last_attempt.failed:
            response: httpx.Response = error.last_attempt.result()
            return response
        lease.record_failure()
        return monitor.on_retry_error(error, attempt_count=attempt_count)

    @staticmethod
    def _handle_request_error(
        error: httpx.RequestError,
        lease: ProbeLease,
        monitor: RequestMonitor,
        *,
        attempt_count: int | None = None,
    ) -> NoReturn:
        """Record one breaker failure and wrap an unretried transport error in RequestError."""
        lease.record_failure()
        monitor.on_request_error(error, attempt_count=attempt_count)

    @staticmethod
    def _parse_retry_after_seconds(response: httpx.Response) -> float | None:
        """Parse the 'Retry-After' header from an HTTP response."""
        return parse_retry_after_seconds(response)

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
                    return min(retry_after_seconds, self.retry_config.max_retry_after)
        return float(self._jitter_wait(retry_state))

    def _get_retryer_kwargs(self, method: str) -> dict[str, Any]:
        """Get common arguments for Retrying and AsyncRetrying."""
        return {
            "wait_strategy": self._wait_strategy,
            "retry_predicate": self._get_retry_predicate(method),
        }

    def _prepare_request(
        self,
        method: str | None = None,
        url: str | None = None,
    ) -> ProbeLease:
        """Admit a new request through the circuit breaker and return its lease."""
        try:
            return self.circuit_breaker.acquire()
        except CircuitBreakerError as err:
            if method is not None or url is not None:
                ctx = DiagnosticContext(
                    method=method,
                    url=url,
                    attempt_count=0,
                )
                raise CircuitBreakerError(
                    err.message,
                    status_code=err.status_code,
                    code=err.code,
                    status=err.status,
                    diagnostic_context=ctx,
                ) from err.__cause__
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
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize the synchronous request executor.

        Args:
            send: The synchronous function to use for sending requests.
            tracer: Optional OpenTelemetry tracer for monitoring.
            retry_config: Centralized configuration for retry behaviors.
            circuit_breaker: Scoped circuit breaker instance for this executor.
        """
        super().__init__(send, tracer, retry_config, circuit_breaker=circuit_breaker)

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
            RequestError: If an HTTP transport error occurs.
        """
        attempts = 0

        def send_fn() -> httpx.Response:
            """Wrapper to send the request with suppressed logging."""
            nonlocal attempts
            attempts += 1
            with self._suppress_httpx_request_logging():
                return self.send(method, url, **kwargs)  # type: ignore[no-any-return]

        retryer = self.retry_config.create_retryer(**self._get_retryer_kwargs(method))
        monitor_url = self._build_monitor_url(url, kwargs)

        with (
            self._prepare_request(method, monitor_url) as lease,
            RequestMonitor(self.tracer, method, monitor_url) as monitor,
        ):
            try:
                response: httpx.Response | None = retryer(send_fn)
            except RetryError as err:
                response = self._extract_or_fail_retry_error(
                    err, lease, monitor, attempt_count=attempts or None
                )
            except httpx.RequestError as err:
                self._handle_request_error(err, lease, monitor, attempt_count=attempts or None)
            except Exception:
                lease.record_failure()
                raise
            return self._finalize_response(response, lease, monitor, attempt_count=attempts or None)


class AsyncRequestExecutor(BaseRequestExecutor):
    """Execute asynchronous HTTP requests with retry and error handling."""

    def __init__(
        self,
        send: Callable[..., Awaitable[httpx.Response]],
        tracer: Tracer | None = None,
        retry_config: RetryConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize the asynchronous request executor.

        Args:
            send: The asynchronous function to use for sending requests.
            tracer: Optional OpenTelemetry tracer for monitoring.
            retry_config: Centralized configuration for retry behaviors.
            circuit_breaker: Scoped circuit breaker instance for this executor.
        """
        super().__init__(send, tracer, retry_config, circuit_breaker=circuit_breaker)

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
            RequestError: If an HTTP transport error occurs.
        """
        attempts = 0

        async def send_fn() -> httpx.Response:
            """Wrapper to send the request with suppressed logging."""
            nonlocal attempts
            attempts += 1
            with self._suppress_httpx_request_logging():
                return await self.send(method, url, **kwargs)  # type: ignore[no-any-return]

        retryer = self.retry_config.create_async_retryer(**self._get_retryer_kwargs(method))
        monitor_url = self._build_monitor_url(url, kwargs)

        async with (
            self._prepare_request(method, monitor_url) as lease,
            RequestMonitor(self.tracer, method, monitor_url) as monitor,
        ):
            try:
                response: httpx.Response | None = await retryer(send_fn)
            except RetryError as err:
                response = self._extract_or_fail_retry_error(
                    err, lease, monitor, attempt_count=attempts or None
                )
            except httpx.RequestError as err:
                self._handle_request_error(err, lease, monitor, attempt_count=attempts or None)
            except Exception:
                lease.record_failure()
                raise
            return self._finalize_response(response, lease, monitor, attempt_count=attempts or None)
