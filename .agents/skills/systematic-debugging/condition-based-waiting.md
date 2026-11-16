# Condition-Based Waiting (`pytest` & Python)

Flaky tests often guess timing with arbitrary `time.sleep()` or `asyncio.sleep()` calls. Wait for the actual observable condition with a bounded deadline instead of guessing wall-clock durations.

## When to Use

- Tests sleep arbitrarily (`time.sleep(0.2)`, `await asyncio.sleep(0.1)`) waiting for async jobs, polling tasks, or file creation.
- **Exception:** When testing *actual timed behavior* (e.g., backoff delay or debouncing windows), wait for the trigger condition first, then wait the exact deterministic interval and comment why.

## Core Pattern (`asyncio` / Sync)

```python
import asyncio
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


async def wait_for(
    predicate: Callable[[], T | None],
    description: str,
    *,
    timeout_s: float = 5.0,
    poll_interval_s: float = 0.01,
) -> T:
    """Poll *predicate* until truthy or raise TimeoutError."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timeout waiting for {description} after {timeout_s}s")
        await asyncio.sleep(poll_interval_s)
```

For fast unit tests covering backoff/polling logic, prefer injected monotonic clocks or fake sleep hooks where interfaces allow.

## Common Mistakes

- **Polling with zero yield (`poll_interval_s=0`):** Starves single-threaded event loops; yield briefly (`0.01s`).
- **Missing timeout:** Unbounded `while True` loops hang CI workers.
- **Caching condition before loop:** Always re-read state inside `predicate()`.
