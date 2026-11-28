"""HTTP request execution with retries and monitoring."""

from __future__ import annotations

import logging
import math
import threading
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
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
    CircuitState,
    ProbeLease,
    get_global_circuit_breaker,
)
from imednet.core.retry import RetryConfig, RetryState
from imednet.errors import DiagnosticContext
from imednet.utils.url import redact_url_query

_SUPPRESSED_LOG_LEVEL = logging.CRITICAL + 1
_SUPPRESSED_LOGGER_NAMES: tuple[str, ...] = ("httpx", "httpcore")
_LOGGER_SUPPRESSION_LOCK = threading.Lock()
_LOGGER_SUPPRESSION_COUNT = 0
_SAVED_LOGGER_LEVELS: dict[str, int] = {}

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer
else:
    Tracer = Any


@dataclass
class _AttemptTracker:
    """Mutable per-call tracking for executed wire attempts and their latest outcome."""

    attempts: int = 0
    last_response: httpx.Response | None = None
    last_request_error: httpx.RequestError | None = None


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
        """Context manager to suppress low-level HTTPX logging across concurrent calls.

        Yields:
            None
        """
        global _LOGGER_SUPPRESSION_COUNT
        with _LOGGER_SUPPRESSION_LOCK:
            if _LOGGER_SUPPRESSION_COUNT == 0:
                _SAVED_LOGGER_LEVELS.clear()
                for name in _SUPPRESSED_LOGGER_NAMES:
                    target_logger = logging.getLogger(name)
                    _SAVED_LOGGER_LEVELS[name] = target_logger.level
                    target_logger.setLevel(_SUPPRESSED_LOG_LEVEL)
            else:
                for name in _SUPPRESSED_LOGGER_NAMES:
                    logging.getLogger(name).setLevel(_SUPPRESSED_LOG_LEVEL)
            _LOGGER_SUPPRESSION_COUNT += 1
        try:
            yield
        finally:
            with _LOGGER_SUPPRESSION_LOCK:
                _LOGGER_SUPPRESSION_COUNT = max(0, _LOGGER_SUPPRESSION_COUNT - 1)
                if _LOGGER_SUPPRESSION_COUNT == 0:
                    for name, original_level in _SAVED_LOGGER_LEVELS.items():
                        logging.getLogger(name).setLevel(original_level)
                    _SAVED_LOGGER_LEVELS.clear()

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
            if exc is not None and (
                isinstance(exc, CircuitBreakerError) or not isinstance(exc, Exception)
            ):
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
        if 200 <= status < 300:
            lease.record_success()
        elif status == 429:
            lease.record_rate_limit()
        elif status == 408 or 500 <= status < 600:
            lease.record_failure()

    @staticmethod
    def _build_monitor_url(url: str, kwargs: Mapping[str, Any]) -> str:
        """Include query params in the monitored URL when present and sanitize sensitive values."""
        params = kwargs.get("params")
        if isinstance(params, Mapping) and params and "?" not in url:
            query = urlencode(list(params.items()), doseq=True)
            if query:
                return redact_url_query(f"{url}?{query}")
        return url

    def _record_attempt_response(
        self,
        response: httpx.Response,
        lease: ProbeLease,
        tracker: _AttemptTracker,
    ) -> httpx.Response:
        """Track the latest wire response and settle the per-attempt probe lease."""
        tracker.last_request_error = None
        tracker.last_response = response
        self._record_response_outcome(response, lease)
        return response

    @staticmethod
    def _record_attempt_exception(
        error: BaseException,
        lease: ProbeLease,
        tracker: _AttemptTracker,
    ) -> None:
        """Track retriable transport failures and settle non-input exceptions on the attempt lease."""
        if isinstance(error, httpx.RequestError):
            tracker.last_response = None
            tracker.last_request_error = error
            lease.record_failure()
        elif isinstance(error, Exception) and not isinstance(error, (ValueError, TypeError)):
            lease.record_failure()

    def _finalize_response(
        self,
        response: httpx.Response | None,
        monitor: RequestMonitor,
        *,
        attempt_count: int | None = None,
    ) -> httpx.Response:
        """Record telemetry and map HTTP error responses after per-attempt lease settlement."""
        if response is None:
            raise RuntimeError("Request failed without response or exception")
        monitor.on_response(response)
        return handle_response(
            response,
            method=monitor.method,
            url=monitor.safe_url,
            attempt_count=attempt_count,
            max_retry_after=self.retry_config.max_retry_after,
        )

    @staticmethod
    def _extract_or_fail_retry_error(
        error: RetryError,
        monitor: RequestMonitor,
        *,
        attempt_count: int | None = None,
    ) -> httpx.Response:
        """Extract response from exhausted retries or escalate transport failure."""
        if error.last_attempt and not error.last_attempt.failed:
            response: httpx.Response = error.last_attempt.result()
            return response
        return monitor.on_retry_error(error, attempt_count=attempt_count)

    @staticmethod
    def _resolve_breaker_abort(
        tracker: _AttemptTracker,
        monitor: RequestMonitor,
    ) -> httpx.Response:
        """Re-raise pre-flight open-circuit errors or finalize the prior executed attempt."""
        if tracker.attempts > 0:
            if tracker.last_response is not None:
                return tracker.last_response
            if tracker.last_request_error is not None:
                monitor.on_request_error(
                    tracker.last_request_error,
                    attempt_count=tracker.attempts,
                )
        raise

    @staticmethod
    def _parse_retry_after_seconds(
        response: httpx.Response,
        max_retry_after: float | None = None,
    ) -> float | None:
        """Parse the 'Retry-After' header from an HTTP response."""
        return parse_retry_after_seconds(response, max_retry_after=max_retry_after)

    def _compute_wait_seconds(
        self,
        retry_state: RetryCallState,
        circuit_breaker: CircuitBreaker,
    ) -> float:
        """Compute retry wait seconds, skipping sleep immediately when the breaker is OPEN."""
        if circuit_breaker.state is CircuitState.OPEN:
            return 0.0
        if retry_state.outcome and not retry_state.outcome.failed:
            result = retry_state.outcome.result()
            if isinstance(result, httpx.Response):
                retry_after_seconds = self._parse_retry_after_seconds(result)
                if retry_after_seconds is not None and math.isfinite(retry_after_seconds):
                    return min(max(retry_after_seconds, 0.0), self.retry_config.max_retry_after)
        return float(self._jitter_wait(retry_state))

    def _wait_strategy(self, retry_state: RetryCallState) -> float:
        """Calculate the wait time before the next retry attempt.

        Args:
            retry_state: Tenacity's retry state.

        Returns:
            float: Number of seconds to wait.
        """
        return self._compute_wait_seconds(retry_state, self.circuit_breaker)

    def _get_retryer_kwargs(
        self,
        method: str,
        circuit_breaker: CircuitBreaker,
    ) -> dict[str, Any]:
        """Get common arguments for Retrying and AsyncRetrying."""
        return {
            "wait_strategy": lambda rs: self._compute_wait_seconds(rs, circuit_breaker),
            "retry_predicate": self._get_retry_predicate(method),
        }

    def _prepare_request(
        self,
        method: str | None = None,
        url: str | None = None,
        *,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> ProbeLease:
        """Admit a single attempt through the circuit breaker and return its lease."""
        active_breaker = circuit_breaker if circuit_breaker is not None else self.circuit_breaker
        try:
            return active_breaker.acquire()
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

    def execute_with_breaker(
        self,
        circuit_breaker: CircuitBreaker,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute a synchronous request using an explicit per-attempt circuit breaker."""
        tracker = _AttemptTracker()
        monitor_url = self._build_monitor_url(url, kwargs)
        retryer = self.retry_config.create_retryer(
            **self._get_retryer_kwargs(method, circuit_breaker)
        )

        def send_fn() -> httpx.Response:
            """Acquire one ProbeLease for this attempt, dispatch, and settle outcome."""
            with self._prepare_request(
                method, monitor_url, circuit_breaker=circuit_breaker
            ) as lease:
                tracker.attempts += 1
                try:
                    with self._suppress_httpx_request_logging():
                        raw_resp: httpx.Response | None = self.send(method, url, **kwargs)
                    if raw_resp is None:
                        raise RuntimeError("Request failed without response or exception")
                except BaseException as err:
                    self._record_attempt_exception(err, lease, tracker)
                    raise
                return self._record_attempt_response(raw_resp, lease, tracker)

        with RequestMonitor(self.tracer, method, monitor_url) as monitor:
            try:
                response: httpx.Response | None = retryer(send_fn)
            except RetryError as err:
                response = self._extract_or_fail_retry_error(
                    err, monitor, attempt_count=tracker.attempts or None
                )
            except CircuitBreakerError:
                response = self._resolve_breaker_abort(tracker, monitor)
            except httpx.RequestError as err:
                monitor.on_request_error(err, attempt_count=tracker.attempts or None)
            return self._finalize_response(
                response, monitor, attempt_count=tracker.attempts or None
            )

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
        return self.execute_with_breaker(self.circuit_breaker, method, url, **kwargs)


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

    async def execute_with_breaker(
        self,
        circuit_breaker: CircuitBreaker,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an asynchronous request using an explicit per-attempt circuit breaker."""
        tracker = _AttemptTracker()
        monitor_url = self._build_monitor_url(url, kwargs)
        retryer = self.retry_config.create_async_retryer(
            **self._get_retryer_kwargs(method, circuit_breaker)
        )

        async def send_fn() -> httpx.Response:
            """Acquire one ProbeLease for this async attempt, dispatch, and settle outcome."""
            async with self._prepare_request(
                method, monitor_url, circuit_breaker=circuit_breaker
            ) as lease:
                tracker.attempts += 1
                try:
                    with self._suppress_httpx_request_logging():
                        raw_resp: httpx.Response | None = await self.send(method, url, **kwargs)
                    if raw_resp is None:
                        raise RuntimeError("Request failed without response or exception")
                except BaseException as err:
                    self._record_attempt_exception(err, lease, tracker)
                    raise
                return self._record_attempt_response(raw_resp, lease, tracker)

        async with RequestMonitor(self.tracer, method, monitor_url) as monitor:
            try:
                response: httpx.Response | None = await retryer(send_fn)
            except RetryError as err:
                response = self._extract_or_fail_retry_error(
                    err, monitor, attempt_count=tracker.attempts or None
                )
            except CircuitBreakerError:
                response = self._resolve_breaker_abort(tracker, monitor)
            except httpx.RequestError as err:
                monitor.on_request_error(err, attempt_count=tracker.attempts or None)
            return self._finalize_response(
                response, monitor, attempt_count=tracker.attempts or None
            )

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
        return await self.execute_with_breaker(self.circuit_breaker, method, url, **kwargs)
