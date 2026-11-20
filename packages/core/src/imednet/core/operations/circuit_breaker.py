"""Circuit breaker implementation to prevent cascading failures."""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """Possible states of the circuit breaker."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerError(Exception):
    """Raised when the circuit breaker is open and a request is blocked."""


class CircuitBreaker:
    """Thread-safe circuit breaker to track consecutive failure rates.

    Transitions from CLOSED to OPEN after consecutive failures.
    Transitions from OPEN to HALF_OPEN after recovery_timeout.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 10.0,
        half_open_max_probes: int = 1,
    ):
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
        self._lock = threading.RLock()

    @property
    def state(self) -> CircuitState:
        """Get the current state of the circuit breaker, handling auto-recovery.

        Returns:
            CircuitState: The current state (CLOSED, OPEN, or HALF_OPEN).
        """
        with self._lock:
            # Check if we should transition from OPEN to HALF_OPEN
            if self._state == CircuitState.OPEN:  # noqa: SIM102
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_probes = 0
                    logger.warning("Circuit breaker transitioned to HALF_OPEN state.")
            return self._state

    def check_request_allowed(self) -> bool:
        """Check if a new outgoing request is allowed.

        Returns:
            bool: True if a HALF_OPEN probe slot was acquired, False otherwise.

        Raises:
            CircuitBreakerError: If the circuit is open or HALF_OPEN probe limit is reached.
        """
        with self._lock:
            state = self.state
            if state == CircuitState.OPEN:
                raise CircuitBreakerError("fail-fast is active: circuit is open")
            if state == CircuitState.HALF_OPEN:
                if self._half_open_probes >= self.half_open_max_probes:
                    raise CircuitBreakerError("fail-fast is active: probe limit reached")
                self._half_open_probes += 1
                return True
            return False

    def release_probe(self) -> None:
        """Release an unsettled HALF_OPEN probe slot when a probe is cancelled or aborted."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN and self._half_open_probes > 0:
                self._half_open_probes -= 1

    def record_success(self) -> None:
        """Record a successful request."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.warning(
                    "Circuit breaker transitioned to CLOSED state after successful probe."
                )
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._half_open_probes = 0

    def record_failure(self) -> None:
        """Record a failed request."""
        with self._lock:
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._half_open_probes = 0
                logger.warning("Circuit breaker transitioned to OPEN state (probe failed).")
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "Circuit breaker transitioned to OPEN state after consecutive failures."
                    )

    def reset(self) -> None:
        """Reset the circuit breaker to its initial CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._last_failure_time = 0.0
            self._half_open_probes = 0


# Global instance
_global_circuit_breaker = CircuitBreaker()


def get_global_circuit_breaker() -> CircuitBreaker:
    """Get the global circuit breaker instance.

    Returns:
        CircuitBreaker: The singleton circuit breaker instance.
    """
    return _global_circuit_breaker
