"""Unit and simulation tests for MultiStudyOrchestrator asynchronous execution."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet.core.context import get_current_study
from imednet.errors import ConfigurationError, FilterConflictError
from imednet.models import Study
from imednet.orchestration import MultiStudyOrchestrator
from imednet.orchestration.orchestrator import AsyncAdaptiveConcurrencyLimiter
from imednet.sdk import AsyncImednetSDK, ImednetSDK


class _SyncStudiesStub:
    """Custom sync-only SDK stub for orchestrator guard tests."""

    def get_studies(self) -> list[Study]:
        return []


class _AsyncStudiesStub:
    """Custom async-only SDK stub for orchestrator guard tests."""

    async def async_get_studies(self) -> list[Study]:
        return []


@pytest.mark.asyncio
async def test_async_orchestrator_simulation_discovery_context_and_fault_isolation() -> None:
    """Scenario 2: MultiStudyOrchestrator discovers studies on AsyncImednetSDK, isolates contextvars, and isolates per-study failures."""
    async with AsyncImednetSDK(simulation_mode=True) as sdk:
        orchestrator = MultiStudyOrchestrator(sdk, max_workers=2)

        active = await orchestrator.async_resolve_active_studies()
        assert len(active) >= 2

        observed_contexts: dict[str, tuple[str | None, str | None]] = {}

        async def worker(study_key: str, client: Any, log: Any) -> str:
            before = get_current_study()
            await asyncio.sleep(0.01)
            after = get_current_study()
            observed_contexts[study_key] = (before, after)
            if study_key == active[0]:
                raise RuntimeError(f"boom in {study_key}")
            queries = await client.workflows.query_management.async_get_open_queries(study_key)
            return f"{study_key}:{len(queries)}"

        results = await orchestrator.async_execute_pipeline(worker)

        assert set(results.keys()) == set(active)
        # Failing study captured in OrchestratorResult without aborting siblings
        assert results[active[0]]["status"] == "FAILED"
        assert f"boom in {active[0]}" in (results[active[0]]["error"] or "")
        assert results[active[0]]["data"] is None

        for healthy_study in active[1:]:
            assert results[healthy_study]["status"] == "SUCCESS"
            assert results[healthy_study]["error"] is None
            assert str(results[healthy_study]["data"]).startswith(f"{healthy_study}:")

        # Each concurrent task maintained its own isolated study_context across awaits
        for sk in active:
            assert observed_contexts[sk] == (sk, sk)

        # Ambient context outside orchestrator remains unset
        with pytest.raises(ConfigurationError):
            get_current_study()


@pytest.mark.asyncio
async def test_async_resolve_active_studies_filtering_and_conflict() -> None:
    """Verify whitelist, blacklist, FilterConflictError, and awaitable list support."""
    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.studies = MagicMock()
    sdk.studies.list = AsyncMock(
        return_value=[
            Study(study_key="ST-A"),
            Study(study_key="ST-B"),
            Study(study_key="ST-C"),
        ]
    )

    orch = MultiStudyOrchestrator(sdk, max_workers=3)
    assert await orch.async_resolve_active_studies(whitelist={"ST-A", "ST-C"}) == ["ST-A", "ST-C"]
    assert await orch.async_resolve_active_studies(blacklist={"ST-B"}) == ["ST-A", "ST-C"]

    with pytest.raises(FilterConflictError):
        await orch.async_resolve_active_studies(whitelist={"ST-A"}, blacklist={"ST-B"})


@pytest.mark.asyncio
async def test_async_execute_pipeline_bounds_max_workers_and_supports_sync_callable() -> None:
    """Verify max_workers bounds concurrent async tasks and sync worker callables also succeed."""
    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.studies = MagicMock()
    sdk.studies.list = AsyncMock(return_value=[Study(study_key=f"ST-{i}") for i in range(6)])

    orch = MultiStudyOrchestrator(sdk, max_workers=2)
    current = 0
    peak = 0

    async def bounded_worker(study_key: str, client: Any, log: Any, prefix: str) -> str:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.02)
        current -= 1
        return f"{prefix}-{study_key}"

    results = await orch.async_execute_pipeline(bounded_worker, None, None, "ok")
    assert len(results) == 6
    assert peak <= 2
    assert all(r["status"] == "SUCCESS" for r in results.values())

    # Sync worker callable inside async_execute_pipeline
    def sync_worker(study_key: str, client: Any, log: Any) -> str:
        return f"sync:{get_current_study()}"

    sync_results = await orch.async_execute_pipeline(sync_worker, whitelist={"ST-0", "ST-1"})
    assert sync_results["ST-0"]["data"] == "sync:ST-0"
    assert sync_results["ST-1"]["data"] == "sync:ST-1"


@pytest.mark.asyncio
async def test_async_execute_pipeline_empty_studies_and_global_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify empty study inventory returns {} and global timeout produces FAILED timeout results."""
    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.studies = MagicMock()
    sdk.studies.list = AsyncMock(return_value=[])

    orch = MultiStudyOrchestrator(sdk, max_workers=2)

    async def slow_worker(study_key: str, client: Any, log: Any) -> str:
        await asyncio.sleep(0.5)
        return study_key

    assert await orch.async_execute_pipeline(slow_worker) == {}

    sdk.studies.list = AsyncMock(return_value=[Study(study_key="ST-SLOW")])
    monkeypatch.setattr("imednet.orchestration.orchestrator._GLOBAL_TIMEOUT_SECONDS", 0.02)
    timed_out = await orch.async_execute_pipeline(slow_worker)
    assert timed_out["ST-SLOW"]["status"] == "FAILED"
    assert "TimeoutError" in (timed_out["ST-SLOW"]["error"] or "")


@pytest.mark.asyncio
async def test_async_adaptive_concurrency_limiter_backpressure_and_recovery() -> None:
    """Verify AsyncAdaptiveConcurrencyLimiter scales concurrency down on latency spikes and back up on recovery."""
    limiter = AsyncAdaptiveConcurrencyLimiter(initial_concurrency=3)
    await limiter.record_latency(0.05)  # Establish baseline
    assert limiter._max_concurrency == 3

    await limiter.record_latency(0.25)  # > 2x baseline -> reduce concurrency
    assert limiter._max_concurrency == 2

    await limiter.record_latency(0.01)  # <= 1.2x baseline -> recover concurrency
    assert limiter._max_concurrency == 3


@pytest.mark.asyncio
async def test_orchestrator_client_mode_type_error_guards() -> None:
    """Verify MultiStudyOrchestrator enforces sync/async client-mode TypeError guards in both directions."""
    sync_sdk = ImednetSDK(simulation_mode=True)
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        async_orch = MultiStudyOrchestrator(async_sdk)
        with pytest.raises(TypeError, match="async_resolve_active_studies"):
            async_orch.resolve_active_studies()
        with pytest.raises(TypeError, match="async_execute_pipeline"):
            async_orch.execute_pipeline(lambda *a: None)

        sync_orch = MultiStudyOrchestrator(sync_sdk)
        with pytest.raises(TypeError, match="resolve_active_studies"):
            await sync_orch.async_resolve_active_studies()
        with pytest.raises(TypeError, match="execute_pipeline"):
            await sync_orch.async_execute_pipeline(lambda *a: None)

        # Custom stubs also trigger guards
        with pytest.raises(TypeError, match="async_resolve_active_studies"):
            MultiStudyOrchestrator(_AsyncStudiesStub()).resolve_active_studies()
        with pytest.raises(TypeError, match="resolve_active_studies"):
            await MultiStudyOrchestrator(_SyncStudiesStub()).async_resolve_active_studies()

    sync_sdk.close()


@pytest.mark.asyncio
async def test_async_execute_pipeline_parent_cancellation_cancels_and_awaits_owned_tasks() -> None:
    """Cancelling async_execute_pipeline cancels and awaits owned child tasks so gated writes never run."""
    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.studies = MagicMock()
    sdk.studies.list = AsyncMock(
        return_value=[
            Study(study_key="ST-1"),
            Study(study_key="ST-2"),
            Study(study_key="ST-3"),
        ]
    )

    orch = MultiStudyOrchestrator(sdk, max_workers=2)
    started_studies: list[str] = []
    workers_at_gate = asyncio.Event()
    release_gate = asyncio.Event()
    cleanup_completed: list[str] = []
    writes_after_gate: list[str] = []

    async def gated_worker(study_key: str, client: Any, log: Any) -> str:
        started_studies.append(study_key)
        if len(started_studies) == 2:
            workers_at_gate.set()
        try:
            await release_gate.wait()
            writes_after_gate.append(study_key)
            return study_key
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            cleanup_completed.append(study_key)
            raise

    parent_task = asyncio.create_task(orch.async_execute_pipeline(gated_worker))
    await asyncio.wait_for(workers_at_gate.wait(), timeout=1.0)

    parent_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent_task

    # Started child tasks must finish cancellation cleanup before parent propagates CancelledError
    assert set(cleanup_completed) == {"ST-1", "ST-2"}
    assert len(started_studies) == 2

    # Releasing the gate after parent cancellation must never allow started or queued child writes
    release_gate.set()
    await asyncio.sleep(0.02)
    assert writes_after_gate == []


@pytest.mark.asyncio
async def test_async_execute_pipeline_sync_blocking_callback_does_not_block_loop_or_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synchronous blocking callbacks run off the event loop so a 20ms deadline is observed promptly."""
    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.studies = MagicMock()
    sdk.studies.list = AsyncMock(return_value=[Study(study_key="ST-BLOCK")])

    orch = MultiStudyOrchestrator(sdk, max_workers=2)
    monkeypatch.setattr("imednet.orchestration.orchestrator._GLOBAL_TIMEOUT_SECONDS", 0.02)

    callback_entered = threading.Event()
    unblock_thread = threading.Event()

    def blocking_sync_worker(study_key: str, client: Any, log: Any) -> str:
        callback_entered.set()
        unblock_thread.wait(timeout=0.15)
        return "unexpected-success"

    loop_ticks = 0
    stop_ticker = False

    async def loop_ticker() -> None:
        nonlocal loop_ticks
        while not stop_ticker:
            await asyncio.sleep(0.004)
            loop_ticks += 1

    ticker_task = asyncio.create_task(loop_ticker())
    start = time.monotonic()
    try:
        results = await orch.async_execute_pipeline(blocking_sync_worker)
        elapsed = time.monotonic() - start
    finally:
        stop_ticker = True
        unblock_thread.set()
        await ticker_task

    assert callback_entered.is_set()
    assert results["ST-BLOCK"]["status"] == "FAILED"
    assert "TimeoutError" in (results["ST-BLOCK"]["error"] or "")
    assert results["ST-BLOCK"]["data"] is None
    assert elapsed < 0.12
    assert loop_ticks >= 2
