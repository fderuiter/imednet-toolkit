"""Universal execution wrapper that applies exponential backoff retries and circuit breaking.

to any compliant operation.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar

from tenacity import (
    RetryCallState,
    RetryError,
    wait_random_exponential,
)

from imednet.core.operations.circuit_breaker import CircuitBreaker, get_global_circuit_breaker
from imednet.core.operations.monitor import OperationMonitor
from imednet.core.retry import RetryConfig

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer
else:
    Tracer = Any

logger = logging.getLogger(__name__)

T = TypeVar("T")


class OperationRetryPolicy(ABC):
    """Base class for defining retry policies for operations."""

    @abstractmethod
    def should_retry(self, exception: Exception) -> bool:
        """Return True if the exception should trigger a retry."""


class DefaultOperationRetryPolicy(OperationRetryPolicy):
    """Default retry policy that retries on all exceptions."""

    def should_retry(self, exception: Exception) -> bool:
        """Return True if the exception should trigger a retry.

        Args:
            exception: The exception that occurred during execution.

        Returns:
            bool: True, as the default policy retries everything.
        """
        # Default fallback: retry on any exception?
        # Usually we only retry on specific ones, but for universal wrapper, we can allow everything or leave it configurable
        return True


class UniversalExecutor:
    """Execute arbitrary operations with retry, circuit breaking, and telemetry."""

    def __init__(
        self,
        tracer: Tracer | None = None,
        retry_config: RetryConfig | None = None,
        operation_name: str = "operation",
        wait_strategy: Callable[[RetryCallState], float] | None = None,
        retry_predicate: Callable[[RetryCallState], bool] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        **attributes: Any,
    ) -> None:
        """Initialize the executor.

        Args:
            tracer: Optional OpenTelemetry tracer for monitoring.
            retry_config: Centralized configuration for retry behaviors.
            operation_name: Name of the operation (used in logs and spans).
            wait_strategy: Optional custom wait strategy function.
            retry_predicate: Optional custom retry predicate function.
            circuit_breaker: Optional instance-scoped circuit breaker.
            **attributes: Additional attributes to attach to logs and spans.
        """
        self.tracer = tracer
        if retry_config is None:
            self.retry_config = RetryConfig(retry_policy=DefaultOperationRetryPolicy())
        else:
            self.retry_config = retry_config
        self.operation_name = operation_name
        self.circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else get_global_circuit_breaker()
        )
        self.attributes = attributes
        self._jitter_wait = wait_random_exponential(multiplier=self.retry_config.backoff_factor)
        self.wait_strategy = wait_strategy or (lambda rs: float(self._jitter_wait(rs)))
        self.retry_predicate = retry_predicate or self._should_retry_wrapper

    def _should_retry_wrapper(self, retry_state: RetryCallState) -> bool:
        """Internal wrapper to adapt Tenacity's retry state to the retry policy.

        Args:
            retry_state: Tenacity's retry state.

        Returns:
            bool: True if the operation should be retried.
        """
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, Exception):  # noqa: SIM102
                if hasattr(self.retry_config.retry_policy, "should_retry"):
                    return self.retry_config.retry_policy.should_retry(exc)  # type: ignore[no-any-return]
        return False

    def _handle_retry_error(self, e: RetryError, monitor: OperationMonitor) -> NoReturn:
        """Record retry exhaustion on the circuit breaker and escalate the underlying cause."""
        self.circuit_breaker.record_failure()
        cause = e.last_attempt.exception() if e.last_attempt else e
        if isinstance(cause, Exception):
            try:
                monitor.on_retry_error(cause, self.retry_config.retries)
            except Exception as _exc:
                if _exc is not cause:
                    raise
        if cause is not None and cause is not e:
            raise cause
        raise e

    def execute(self, func: Callable[[], T]) -> T:
        """Synchronous execution."""
        probe_acquired = self.circuit_breaker.check_request_allowed()
        settled = False

        retryer = self.retry_config.create_retryer(
            wait_strategy=self.wait_strategy,
            retry_predicate=self.retry_predicate,
        )

        try:
            with OperationMonitor(self.tracer, self.operation_name, **self.attributes) as monitor:
                try:
                    result: Any = retryer(func)
                    self.circuit_breaker.record_success()
                    settled = True
                    monitor.on_success()
                    return result  # type: ignore[no-any-return]
                except RetryError as e:
                    settled = True
                    self._handle_retry_error(e, monitor)
                except Exception as e:
                    monitor.on_failure(e)
                    raise
        finally:
            if probe_acquired and not settled:
                self.circuit_breaker.release_probe()

    async def execute_async(self, func: Callable[[], Awaitable[T]]) -> T:
        """Asynchronous execution."""
        probe_acquired = self.circuit_breaker.check_request_allowed()
        settled = False

        retryer = self.retry_config.create_async_retryer(
            wait_strategy=self.wait_strategy,
            retry_predicate=self.retry_predicate,
        )

        try:
            async with OperationMonitor(
                self.tracer, self.operation_name, **self.attributes
            ) as monitor:
                try:

                    async def _async_wrapper() -> T:
                        """Wrapper to await the function.

                        Returns:
                            T: Result of the operation.
                        """
                        return await func()

                    result: Any = await retryer(_async_wrapper)
                    self.circuit_breaker.record_success()
                    settled = True
                    monitor.on_success()
                    return result  # type: ignore[no-any-return]
                except RetryError as e:
                    settled = True
                    self._handle_retry_error(e, monitor)
                except Exception as e:
                    monitor.on_failure(e)
                    raise
        finally:
            if probe_acquired and not settled:
                self.circuit_breaker.release_probe()
