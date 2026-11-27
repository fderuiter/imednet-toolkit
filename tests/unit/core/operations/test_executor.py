"""Unit tests for executor."""

import pytest

from imednet.core.operations.circuit_breaker import get_global_circuit_breaker
from imednet.core.operations.executor import DefaultOperationRetryPolicy, UniversalExecutor
from imednet.core.retry import RetryConfig


class RESTTask:
    """Test suite for RESTTask."""

    def __init__(self, fail_times=0):
        """Initialize the test object."""
        self.fail_times = fail_times
        self.attempts = 0

    def execute(self) -> str:
        """Helper function to execute."""
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ValueError("HTTP Error")
        return "REST Success"


class NonRESTTask:
    """Test suite for NonRESTTask."""

    def __init__(self, fail_times=0):
        """Initialize the test object."""
        self.fail_times = fail_times
        self.attempts = 0

    def execute(self) -> str:
        """Helper function to execute."""
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("DB Error")
        return "Non-REST Success"


def test_universal_executor_supports_rest_and_non_rest():
    """Test that universal executor supports rest and non rest."""
    get_global_circuit_breaker().reset()
    executor = UniversalExecutor(
        retry_config=RetryConfig(
            retries=2, backoff_factor=0.01, retry_policy=DefaultOperationRetryPolicy()
        )
    )

    rest_task = RESTTask(fail_times=1)
    result = executor.execute(rest_task.execute)
    assert result == "REST Success"
    assert rest_task.attempts == 2

    non_rest_task = NonRESTTask(fail_times=1)
    result = executor.execute(non_rest_task.execute)
    assert result == "Non-REST Success"
    assert non_rest_task.attempts == 2


def test_universal_executor_fails_after_retries():
    """Test that universal executor fails after retries."""
    get_global_circuit_breaker().reset()
    executor = UniversalExecutor(
        retry_config=RetryConfig(
            retries=1, backoff_factor=0.01, retry_policy=DefaultOperationRetryPolicy()
        )
    )

    task = RESTTask(fail_times=5)
    with pytest.raises(ValueError, match="HTTP Error"):
        executor.execute(task.execute)
    assert task.attempts == 2


@pytest.mark.asyncio
async def test_universal_executor_async():
    """Test that universal executor async asynchronously."""
    get_global_circuit_breaker().reset()
    executor = UniversalExecutor(
        retry_config=RetryConfig(
            retries=1, backoff_factor=0.01, retry_policy=DefaultOperationRetryPolicy()
        )
    )

    attempts = 0

    async def failing_task():
        """Helper function to failing task."""
        nonlocal attempts
        attempts += 1
        raise ValueError("Async Error")

    # Use a regular function returning a coroutine, to match Callable[[], Awaitable[T]]
    def task_factory():
        """Helper function to task factory."""
        return failing_task()

    with pytest.raises(ValueError, match="Async Error"):
        await executor.execute_async(task_factory)
    assert attempts == 2


class SelectiveOperationRetryPolicy(DefaultOperationRetryPolicy):
    """Retry RuntimeError but reject ValueError as deterministic caller input error."""

    def should_retry(self, exception: Exception) -> bool:
        return not isinstance(exception, ValueError)


@pytest.mark.asyncio
async def test_universal_executor_skips_failure_accounting_for_non_retriable_errors():
    """Verify non-retriable input errors do not increment circuit breaker failures."""
    from imednet.core.operations.circuit_breaker import CircuitBreaker, CircuitState

    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0)
    executor = UniversalExecutor(
        retry_config=RetryConfig(
            retries=2,
            backoff_factor=0.0,
            retry_policy=SelectiveOperationRetryPolicy(),
        ),
        circuit_breaker=breaker,
    )

    for _ in range(5):
        with pytest.raises(ValueError, match="bad payload"):
            executor.execute(lambda: (_ for _ in ()).throw(ValueError("bad payload")))

    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 0

    async def bad_async() -> str:
        raise ValueError("bad async payload")

    for _ in range(5):
        with pytest.raises(ValueError, match="bad async payload"):
            await executor.execute_async(bad_async)

    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_universal_executor_opens_default_breaker_after_5_attempts_and_aborts_open_retries(
    monkeypatch: pytest.MonkeyPatch,
    asynchronous: bool,
) -> None:
    """UniversalExecutor acquires one ProbeLease per attempt, opens after 5 failures, and sends 0 OPEN retries."""
    from imednet.core.operations.circuit_breaker import (
        CircuitBreaker,
        CircuitBreakerError,
        CircuitState,
    )

    now = 10.0
    monkeypatch.setattr("imednet.core.operations.circuit_breaker.time.monotonic", lambda: now)

    breaker = CircuitBreaker()
    executor = UniversalExecutor(
        retry_config=RetryConfig(
            retries=3,
            backoff_factor=0.0,
            retry_policy=DefaultOperationRetryPolicy(),
        ),
        circuit_breaker=breaker,
    )

    calls = 0
    succeed = False

    def sync_op() -> str:
        nonlocal calls
        calls += 1
        if not succeed:
            raise RuntimeError("transient sink outage")
        return "ok"

    async def async_op() -> str:
        return sync_op()

    async def invoke() -> str:
        if asynchronous:
            return await executor.execute_async(async_op)
        return executor.execute(sync_op)

    # Call 1: 4 attempts -> 4 failures, still CLOSED
    with pytest.raises(RuntimeError, match="transient sink outage"):
        await invoke()
    assert calls == 4
    assert breaker.consecutive_failures == 4
    assert breaker.state is CircuitState.CLOSED

    # Call 2: 5th failure opens breaker on attempt 1 and aborts remaining 3 retries immediately
    with pytest.raises(RuntimeError, match="transient sink outage"):
        await invoke()
    assert calls == 5
    assert breaker.state is CircuitState.OPEN
    assert breaker.epoch == 1

    # Call 3 while OPEN: 0 attempts, raises CircuitBreakerError
    with pytest.raises(CircuitBreakerError):
        await invoke()
    assert calls == 5

    # Advance past recovery_timeout: HALF_OPEN sends 1 probe and 0 OPEN retries on failure
    now = 25.0
    with pytest.raises(RuntimeError, match="transient sink outage"):
        await invoke()
    assert calls == 6
    assert breaker.state is CircuitState.OPEN
    assert breaker.epoch == 2

    # Advance past recovery_timeout again: healthy probe closes HALF_OPEN and increments _epoch
    now = 40.0
    succeed = True
    assert await invoke() == "ok"
    assert calls == 7
    assert breaker.state is CircuitState.CLOSED
    assert breaker.epoch == 3
