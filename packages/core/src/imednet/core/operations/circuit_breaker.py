"""Circuit breaker implementation to prevent cascading failures."""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from types import TracebackType
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """Possible states of the circuit breaker."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def can_transition_to(self, target: CircuitState | str) -> bool:
        """Check whether transitioning from this state to ``target`` is permitted."""
        return is_valid_circuit_transition(self, target)

    def validate_transition_to(self, target: CircuitState | str) -> bool:
        """Validate transitioning from this state to ``target``."""
        return validate_circuit_transition(self, target)

    @property
    def allowed_transitions(self) -> set[CircuitState]:
        """Return the set of allowed target states from this state."""
        return set(ALLOWED_CIRCUIT_TRANSITIONS.get(self, set()))


ALLOWED_CIRCUIT_TRANSITIONS: dict[CircuitState, set[CircuitState]] = {
    CircuitState.CLOSED: {CircuitState.CLOSED, CircuitState.OPEN},
    CircuitState.OPEN: {CircuitState.OPEN, CircuitState.HALF_OPEN},
    CircuitState.HALF_OPEN: {
        CircuitState.HALF_OPEN,
        CircuitState.CLOSED,
        CircuitState.OPEN,
    },
}


class CircuitBreakerError(Exception):
    """Raised when the circuit breaker is open and a request is blocked."""


class InvalidCircuitStateError(ValueError):
    """Raised when an unrecognized circuit breaker state is encountered."""


class InvalidCircuitTransitionError(ValueError):
    """Raised when an invalid circuit breaker state transition is attempted."""


def normalize_circuit_state(state: CircuitState | str) -> CircuitState:
    """Normalize a ``CircuitState`` member or state string into ``CircuitState``."""
    if isinstance(state, CircuitState):
        return state
    if isinstance(state, str):
        cleaned = state.strip().upper()
        try:
            return CircuitState(cleaned)
        except ValueError as err:
            allowed = sorted(member.value for member in CircuitState)
            raise InvalidCircuitStateError(
                f"Invalid circuit state: '{state}'. Must be one of: {allowed}"
            ) from err
    raise InvalidCircuitStateError(f"Invalid circuit state type: {type(state).__name__}")


def is_valid_circuit_transition(
    from_state: CircuitState | str,
    to_state: CircuitState | str,
) -> bool:
    """Return True if transitioning from ``from_state`` to ``to_state`` is allowed."""
    try:
        origin = normalize_circuit_state(from_state)
        target = normalize_circuit_state(to_state)
    except InvalidCircuitStateError:
        return False
    return target in ALLOWED_CIRCUIT_TRANSITIONS.get(origin, set())


def validate_circuit_transition(
    from_state: CircuitState | str,
    to_state: CircuitState | str,
) -> bool:
    """Validate that transitioning from ``from_state`` to ``to_state`` is allowed."""
    origin = normalize_circuit_state(from_state)
    target = normalize_circuit_state(to_state)
    allowed = ALLOWED_CIRCUIT_TRANSITIONS.get(origin, set())
    if target not in allowed:
        allowed_names = sorted(member.value for member in allowed)
        raise InvalidCircuitTransitionError(
            f"Invalid circuit state transition from {origin.value} to {target.value}. "
            f"Allowed transitions from {origin.value}: {allowed_names}"
        )
    return True


class ProbeLease:
    """Sync and async context-managed lease for circuit-breaker request admission.

    Guarantees that ``HALF_OPEN`` probe slots are released if execution exits
    without settling an outcome (including ``BaseException`` cancellations such
    as ``asyncio.CancelledError`` or skipped non-retriable validation errors).
    """

    def __init__(self, breaker: CircuitBreaker, *, epoch: int, is_probe: bool) -> None:
        """Initialize the probe lease."""
        self._breaker = breaker
        self.epoch = epoch
        self.is_probe = is_probe
        self._settled = False

    def record_success(self) -> None:
        """Settle the lease with a healthy connectivity outcome."""
        if not self._settled:
            self._settled = True
            self._breaker.record_success(epoch=self.epoch)

    def record_failure(self) -> None:
        """Settle the lease with a retriable/upstream failure outcome."""
        if not self._settled:
            self._settled = True
            self._breaker.record_failure(epoch=self.epoch)

    def record_rate_limit(self) -> None:
        """Settle the lease with a rate-limited (HTTP 429) outcome."""
        if not self._settled:
            self._settled = True
            self._breaker.record_rate_limit(epoch=self.epoch)

    def release(self) -> None:
        """Release any unsettled ``HALF_OPEN`` probe slot back to the breaker."""
        if not self._settled:
            self._settled = True
            self._breaker._release_probe(epoch=self.epoch, is_probe=self.is_probe)

    def __enter__(self) -> ProbeLease:
        """Enter the synchronous lease context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the synchronous lease context, releasing unsettled probes."""
        _ = (exc_type, exc_val, exc_tb)
        self.release()

    async def __aenter__(self) -> ProbeLease:
        """Enter the asynchronous lease context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the asynchronous lease context, releasing unsettled probes."""
        _ = (exc_type, exc_val, exc_tb)
        self.release()


class CircuitBreaker:
    """Scoped circuit breaker tracking consecutive failures across threads and tasks.

    Transitions from ``CLOSED`` to ``OPEN`` after ``failure_threshold`` consecutive
    failures, and from ``OPEN`` to ``HALF_OPEN`` on probe admission once
    ``recovery_timeout`` has elapsed.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 10.0,
        half_open_max_probes: int = 1,
    ) -> None:
        """Initialize the circuit breaker.

        Args:
            failure_threshold: Number of consecutive failures before opening the circuit.
            recovery_timeout: Time in seconds to wait before transitioning to HALF_OPEN.
            half_open_max_probes: Max number of probes allowed in HALF_OPEN state.
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_probes = half_open_max_probes

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time = 0.0
        self._half_open_probes = 0
        self._epoch = 0
        self._lock = threading.RLock()

    @property
    def state(self) -> CircuitState:
        """Return the current state of the circuit breaker without mutating state."""
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        """Return the current consecutive failure count without side effects."""
        with self._lock:
            return self._consecutive_failures

    @property
    def half_open_probes(self) -> int:
        """Return the current number of active HALF_OPEN probes without side effects."""
        with self._lock:
            return self._half_open_probes

    @property
    def epoch(self) -> int:
        """Return the current monotonic circuit epoch without side effects."""
        with self._lock:
            return self._epoch

    def _transition_to(self, target: CircuitState) -> None:
        """Validate and execute an internal state transition under ``self._lock``."""
        previous = self._state
        validate_circuit_transition(previous, target)
        self._state = target
        if target == CircuitState.OPEN and previous != CircuitState.OPEN:
            self._epoch += 1
            self._half_open_probes = 0
        elif target == CircuitState.HALF_OPEN and previous != CircuitState.HALF_OPEN:
            self._half_open_probes = 0
        elif target == CircuitState.CLOSED:
            self._consecutive_failures = 0
            self._half_open_probes = 0

    def _release_probe(self, *, epoch: int, is_probe: bool) -> None:
        """Release an unsettled HALF_OPEN probe slot if still in the same epoch."""
        if not is_probe:
            return
        with self._lock:
            if (
                epoch == self._epoch
                and self._state == CircuitState.HALF_OPEN
                and self._half_open_probes > 0
            ):
                self._half_open_probes -= 1

    def acquire(self) -> ProbeLease:
        """Admit an outgoing operation and return its context-managed ``ProbeLease``.

        Raises:
            CircuitBreakerError: If the circuit is ``OPEN`` before ``recovery_timeout``
                or if ``HALF_OPEN`` probe capacity is exhausted.
        """
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    logger.warning("Circuit breaker transitioned to HALF_OPEN state.")
                else:
                    raise CircuitBreakerError("fail-fast is active: circuit is open")

            is_probe = False
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_probes >= self.half_open_max_probes:
                    raise CircuitBreakerError("fail-fast is active: probe limit reached")
                self._half_open_probes += 1
                is_probe = True

            return ProbeLease(self, epoch=self._epoch, is_probe=is_probe)

    def check_request_allowed(self) -> ProbeLease:
        """Check whether an outgoing request is allowed and return a ``ProbeLease``."""
        return self.acquire()

    def record_success(self, *, epoch: int | None = None) -> None:
        """Record a healthy request completion, ignoring stale epochs and OPEN state."""
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return
            if self._state == CircuitState.OPEN:
                return
            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.CLOSED)
                logger.warning(
                    "Circuit breaker transitioned to CLOSED state after successful probe."
                )
            else:
                self._transition_to(CircuitState.CLOSED)

    def record_failure(self, *, epoch: int | None = None) -> None:
        """Record a failed request completion, ignoring stale epochs."""
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
                logger.warning("Circuit breaker transitioned to OPEN state (probe failed).")
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._transition_to(CircuitState.OPEN)
                    logger.warning(
                        "Circuit breaker transitioned to OPEN state after consecutive failures."
                    )

    def record_rate_limit(self, *, epoch: int | None = None) -> None:
        """Handle HTTP 429 without incrementing consecutive failures or false recovery."""
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return
            if self._state == CircuitState.HALF_OPEN:
                self._last_failure_time = time.monotonic()
                self._transition_to(CircuitState.OPEN)
                logger.warning(
                    "Circuit breaker transitioned to OPEN state after rate-limited probe."
                )

    def reset(self) -> None:
        """Reset the circuit breaker to its initial CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._last_failure_time = 0.0
            self._half_open_probes = 0
            self._epoch += 1


# Global fallback instance for direct external callers
_global_circuit_breaker = CircuitBreaker()


def get_global_circuit_breaker() -> CircuitBreaker:
    """Get the global fallback circuit breaker instance.

    Returns:
        CircuitBreaker: The module-level fallback circuit breaker instance.
    """
    return _global_circuit_breaker


__all__ = [
    "ALLOWED_CIRCUIT_TRANSITIONS",
    "CircuitBreaker",
    "CircuitBreakerError",
    "CircuitState",
    "InvalidCircuitStateError",
    "InvalidCircuitTransitionError",
    "ProbeLease",
    "get_global_circuit_breaker",
    "is_valid_circuit_transition",
    "normalize_circuit_state",
    "validate_circuit_transition",
]
