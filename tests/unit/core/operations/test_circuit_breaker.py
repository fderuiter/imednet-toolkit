"""Unit tests for scoped circuit breakers, probe leases, epochs, and status accounting."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.http.executor import AsyncRequestExecutor, SyncRequestExecutor
from imednet.core.operations.circuit_breaker import (
    ALLOWED_CIRCUIT_TRANSITIONS,
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
    InvalidCircuitStateError,
    InvalidCircuitTransitionError,
    get_global_circuit_breaker,
    is_valid_circuit_transition,
    normalize_circuit_state,
    validate_circuit_transition,
)
from imednet.core.retry import RetryConfig, RetryPolicy, RetryState
from imednet.errors import (
    ExportBatchError,
    ExportConfigurationError,
    NotFoundError,
    RateLimitError,
    RequestError,
    ServerError,
)
from imednet.integrations.sink_base import ExportSink, SinkConfig
from imednet.sdk import AsyncImednetSDK, ImednetSDK

BASE_URL = "https://cb-synthetic.example"


class _StubExportSink(ExportSink):
    """Minimal concrete ExportSink exercising _execute_with_retry."""

    def __init__(self, config: SinkConfig, writer_fn):
        super().__init__(config)
        self._writer_fn = writer_fn

    def write_batch(self, records, *, batch_id: str) -> int:
        return self._execute_with_retry(
            "stub_sink_write",
            batch_id,
            lambda: self._writer_fn(records),
        )

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_circuit_state_transition_validation_and_normalization() -> None:
    """Verify explicit CircuitState transition rules and error reporting."""
    assert normalize_circuit_state(CircuitState.CLOSED) is CircuitState.CLOSED
    assert normalize_circuit_state(" half_open ") is CircuitState.HALF_OPEN

    with pytest.raises(InvalidCircuitStateError, match="Invalid circuit state"):
        normalize_circuit_state("BROKEN")

    with pytest.raises(InvalidCircuitStateError, match="Invalid circuit state type"):
        normalize_circuit_state(123)  # type: ignore[arg-type]

    assert (
        CircuitState.CLOSED.allowed_transitions == ALLOWED_CIRCUIT_TRANSITIONS[CircuitState.CLOSED]
    )
    assert CircuitState.CLOSED.can_transition_to(CircuitState.OPEN) is True
    assert CircuitState.CLOSED.can_transition_to(CircuitState.HALF_OPEN) is False
    assert CircuitState.OPEN.can_transition_to(CircuitState.HALF_OPEN) is True
    assert CircuitState.OPEN.can_transition_to(CircuitState.CLOSED) is False
    assert CircuitState.HALF_OPEN.can_transition_to(CircuitState.CLOSED) is True
    assert CircuitState.HALF_OPEN.can_transition_to(CircuitState.OPEN) is True
    assert is_valid_circuit_transition("OPEN", "INVALID") is False

    assert CircuitState.OPEN.validate_transition_to(CircuitState.HALF_OPEN) is True
    with pytest.raises(InvalidCircuitTransitionError, match="from OPEN to CLOSED"):
        validate_circuit_transition(CircuitState.OPEN, CircuitState.CLOSED)
    with pytest.raises(InvalidCircuitTransitionError, match="from CLOSED to HALF_OPEN"):
        CircuitState.CLOSED.validate_transition_to(CircuitState.HALF_OPEN)


def test_state_reader_is_side_effect_free_even_after_recovery_timeout(monkeypatch, caplog) -> None:
    """Reading breaker.state must never mutate _state, _half_open_probes, or emit logs."""
    now = 100.0
    monkeypatch.setattr("imednet.core.operations.circuit_breaker.time.monotonic", lambda: now)

    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.epoch == 1

    now = 120.0  # Past recovery_timeout
    caplog.clear()
    for _ in range(3):
        assert breaker.state is CircuitState.OPEN
    assert breaker._state is CircuitState.OPEN
    assert breaker.half_open_probes == 0
    assert caplog.text == ""

    # Admission performs the validated OPEN -> HALF_OPEN transition.
    with breaker.acquire() as lease:
        assert lease.is_probe is True
        assert breaker.state is CircuitState.HALF_OPEN
        assert breaker.half_open_probes == 1
        lease.record_success()

    assert breaker.state is CircuitState.CLOSED
    assert breaker.half_open_probes == 0
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_cancelled_async_half_open_probe_releases_slot_and_recovers(
    respx_mock, monkeypatch
) -> None:
    """Cancelling an async HALF_OPEN probe returns _half_open_probes to 0 and allows recovery."""
    now = 50.0
    monkeypatch.setattr("imednet.core.operations.circuit_breaker.time.monotonic", lambda: now)

    probe_started = asyncio.Event()
    allow_finish = asyncio.Event()
    call_count = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return httpx.Response(500, json={"message": "server down"})
        if call_count == 3:
            probe_started.set()
            await allow_finish.wait()
            return httpx.Response(200, json={"jobId": "J1", "state": "RUNNING"})
        return httpx.Response(200, json={"jobId": "J1", "state": "COMPLETED"})

    respx_mock.get(f"{BASE_URL}/api/v1/edc/studies/STUDY1/jobs/J1").mock(side_effect=handler)

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url=BASE_URL,
        retries=0,
    ) as sdk:
        breaker = sdk._async_client._circuit_breaker
        breaker.failure_threshold = 2
        breaker.recovery_timeout = 10.0

        for _ in range(2):
            with pytest.raises(ServerError):
                await sdk.jobs.get("STUDY1", "J1")

        assert breaker.state is CircuitState.OPEN
        assert breaker.epoch == 1

        # Advance monotonic clock past recovery_timeout so next call becomes HALF_OPEN probe.
        now = 65.0
        probe_task = asyncio.create_task(sdk.jobs.get("STUDY1", "J1"))
        await probe_started.wait()

        assert breaker.state is CircuitState.HALF_OPEN
        assert breaker._half_open_probes == 1
        with pytest.raises(CircuitBreakerError, match="probe limit reached"):
            await sdk.jobs.get("STUDY1", "J1")

        # Cancel the in-flight probe coroutine and verify direct CancelledError propagation.
        probe_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe_task

        assert breaker._half_open_probes == 0
        assert breaker.state is CircuitState.HALF_OPEN

        # Subsequent probe is admitted immediately and closes the circuit.
        job = await sdk.jobs.get("STUDY1", "J1")
        assert str(job.state) == "COMPLETED"
        assert breaker.state is CircuitState.CLOSED
        assert breaker._half_open_probes == 0
        assert breaker.consecutive_failures == 0


def test_sync_base_exception_releases_half_open_probe_slot(monkeypatch) -> None:
    """Sync BaseException (e.g. KeyboardInterrupt) releases _half_open_probes on exit."""
    now = 10.0
    monkeypatch.setattr("imednet.core.operations.circuit_breaker.time.monotonic", lambda: now)

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0)
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

    now = 20.0
    with pytest.raises(KeyboardInterrupt):
        with breaker.acquire() as lease:
            assert lease.is_probe is True
            assert breaker._half_open_probes == 1
            raise KeyboardInterrupt

    assert breaker._half_open_probes == 0
    assert breaker.state is CircuitState.HALF_OPEN

    with breaker.acquire() as next_lease:
        assert next_lease.is_probe is True
        next_lease.record_success()

    assert breaker.state is CircuitState.CLOSED


def test_http_404_during_half_open_closes_breaker_and_raises_not_found(
    respx_mock, monkeypatch
) -> None:
    """An HTTP 404 during HALF_OPEN closes the client breaker and raises NotFoundError with 0 failures."""
    now = 100.0
    monkeypatch.setattr("imednet.core.operations.circuit_breaker.time.monotonic", lambda: now)

    respx_mock.get(f"{BASE_URL}/subjects/missing").mock(
        side_effect=[
            httpx.Response(503, json={"message": "unavailable"}),
            httpx.Response(404, json={"message": "subject not found"}),
        ]
    )

    with Client(
        api_key="k",
        security_key="s",
        base_url=BASE_URL,
        retry_config=RetryConfig(retries=0, backoff_factor=0),
    ) as client:
        breaker = client._circuit_breaker
        breaker.failure_threshold = 1
        breaker.recovery_timeout = 5.0

        with pytest.raises(ServerError):
            client.get("/subjects/missing")
        assert breaker.state is CircuitState.OPEN
        assert breaker.consecutive_failures == 1

        now = 110.0
        with pytest.raises(NotFoundError):
            client.get("/subjects/missing")

        assert breaker.state is CircuitState.CLOSED
        assert breaker.consecutive_failures == 0
        assert breaker.half_open_probes == 0


def test_http_3xx_and_non_429_4xx_count_as_healthy_connectivity(respx_mock) -> None:
    """HTTP 2xx, 3xx, and non-429 4xx reset consecutive failures rather than counting as outages."""
    respx_mock.get(f"{BASE_URL}/redirect").respond(302, headers={"Location": f"{BASE_URL}/dest"})
    respx_mock.get(f"{BASE_URL}/fail").respond(500)

    with Client(
        api_key="k",
        security_key="s",
        base_url=BASE_URL,
        retry_config=RetryConfig(retries=0, backoff_factor=0),
    ) as client:
        with pytest.raises(ServerError):
            client.get("/fail")
        assert client._circuit_breaker.consecutive_failures == 1

        resp = client.get("/redirect")
        assert resp.status_code == 302
        assert client._circuit_breaker.consecutive_failures == 0
        assert client._circuit_breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_non_retried_post_500_and_connect_error_increment_failure_once(
    respx_mock, asynchronous: bool
) -> None:
    """Each non-retried POST 500 or network error increments client failures by exactly 1."""
    post_500 = respx_mock.post(f"{BASE_URL}/records").respond(500)
    post_net = respx_mock.post(f"{BASE_URL}/records-net").mock(
        side_effect=httpx.ConnectError("connection reset")
    )

    if asynchronous:
        async with AsyncClient(
            api_key="k",
            security_key="s",
            base_url=BASE_URL,
            retry_config=RetryConfig(retries=2, backoff_factor=0),
        ) as client:
            breaker = client._circuit_breaker
            for expected in (1, 2):
                with pytest.raises(ServerError):
                    await client.post("/records", json={"recordData": {}})
                assert breaker.consecutive_failures == expected

            with pytest.raises(RequestError) as net_exc:
                await client.post("/records-net", json={"recordData": {}})
            assert isinstance(net_exc.value.__cause__, httpx.ConnectError)
            assert net_exc.value.method == "POST"
            assert net_exc.value.url == f"{BASE_URL}/records-net"
            assert net_exc.value.attempt_count == 1
            assert breaker.consecutive_failures == 3
            assert breaker.state is CircuitState.CLOSED
    else:
        with Client(
            api_key="k",
            security_key="s",
            base_url=BASE_URL,
            retry_config=RetryConfig(retries=2, backoff_factor=0),
        ) as client:
            breaker = client._circuit_breaker
            for expected in (1, 2):
                with pytest.raises(ServerError):
                    client.post("/records", json={"recordData": {}})
                assert breaker.consecutive_failures == expected

            with pytest.raises(RequestError) as net_exc:
                client.post("/records-net", json={"recordData": {}})
            assert isinstance(net_exc.value.__cause__, httpx.ConnectError)
            assert net_exc.value.method == "POST"
            assert net_exc.value.url == f"{BASE_URL}/records-net"
            assert net_exc.value.attempt_count == 1
            assert breaker.consecutive_failures == 3
            assert breaker.state is CircuitState.CLOSED

    assert post_500.call_count == 2
    assert post_net.call_count == 1


def test_http_429_preserves_failure_count_and_returns_half_open_probe_to_open(
    respx_mock, monkeypatch
) -> None:
    """HTTP 429 leaves consecutive failures unchanged and returns HALF_OPEN probes to OPEN."""
    now = 10.0
    monkeypatch.setattr("imednet.core.operations.circuit_breaker.time.monotonic", lambda: now)

    respx_mock.get(f"{BASE_URL}/server-err").respond(500)
    respx_mock.get(f"{BASE_URL}/rate-limit").respond(429)

    with Client(
        api_key="k",
        security_key="s",
        base_url=BASE_URL,
        retry_config=RetryConfig(retries=1, backoff_factor=0),
    ) as client:
        breaker = client._circuit_breaker
        breaker.failure_threshold = 2
        breaker.recovery_timeout = 5.0

        # 1st 500 -> 1 failure
        with pytest.raises(ServerError):
            client.get("/server-err")
        assert breaker.consecutive_failures == 1
        assert breaker.state is CircuitState.CLOSED

        # 429 in CLOSED leaves consecutive_failures at 1 (neither incremented nor reset to 0)
        with pytest.raises(RateLimitError):
            client.get("/rate-limit")
        assert breaker.consecutive_failures == 1
        assert breaker.state is CircuitState.CLOSED

        # 2nd 500 -> reaches threshold 2 -> OPEN (epoch 1)
        with pytest.raises(ServerError):
            client.get("/server-err")
        assert breaker.state is CircuitState.OPEN
        assert breaker.epoch == 1

        # Advance past recovery_timeout; HALF_OPEN probe receives 429 -> returns to OPEN (epoch 2)
        now = 20.0
        with pytest.raises(RateLimitError):
            client.get("/rate-limit")
        assert breaker.state is CircuitState.OPEN
        assert breaker.epoch == 2
        assert breaker.consecutive_failures == 2
        assert breaker.half_open_probes == 0

        # Immediate follow-up call fails fast because breaker returned to OPEN
        with pytest.raises(CircuitBreakerError, match="circuit is open"):
            client.get("/rate-limit")


@pytest.mark.asyncio
async def test_slow_pre_outage_response_does_not_reset_open_circuit(respx_mock) -> None:
    """Completing a slow pre-outage request while breaker is OPEN leaves breaker in OPEN."""
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()

    async def slow_handler(_request: httpx.Request) -> httpx.Response:
        slow_started.set()
        await release_slow.wait()
        return httpx.Response(200, json={"ok": True})

    respx_mock.get(f"{BASE_URL}/slow").mock(side_effect=slow_handler)
    respx_mock.post(f"{BASE_URL}/outage").respond(500)

    async with AsyncClient(
        api_key="k",
        security_key="s",
        base_url=BASE_URL,
        retry_config=RetryConfig(retries=0, backoff_factor=0),
    ) as client:
        breaker = client._circuit_breaker
        breaker.failure_threshold = 2
        breaker.recovery_timeout = 60.0

        slow_task = asyncio.create_task(client.get("/slow"))
        await slow_started.wait()
        assert breaker.epoch == 0

        # Outage trips breaker CLOSED -> OPEN (epoch 1) while slow_task from epoch 0 is in flight.
        for _ in range(2):
            with pytest.raises(ServerError):
                await client.post("/outage", json={})

        assert breaker.state is CircuitState.OPEN
        assert breaker.epoch == 1

        # Slow pre-outage 200 response completes now; breaker must stay OPEN.
        release_slow.set()
        slow_resp = await slow_task
        assert slow_resp.status_code == 200
        assert breaker.state is CircuitState.OPEN
        assert breaker.epoch == 1

        with pytest.raises(CircuitBreakerError, match="circuit is open"):
            await client.get("/slow")


def test_stale_probe_from_prior_epoch_never_corrupts_newer_epoch(monkeypatch) -> None:
    """Stale completions or cancellations from an earlier epoch do not affect a newer epoch."""
    now = 10.0
    monkeypatch.setattr("imednet.core.operations.circuit_breaker.time.monotonic", lambda: now)

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5.0, half_open_max_probes=2)
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.epoch == 1

    now = 20.0
    probe_a = breaker.acquire()
    probe_b = breaker.acquire()
    assert breaker.half_open_probes == 2

    # Probe A fails -> HALF_OPEN -> OPEN (epoch 2).
    probe_a.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.epoch == 2
    assert breaker.half_open_probes == 0

    # Advance into epoch 2 HALF_OPEN with a fresh probe C.
    now = 30.0
    probe_c = breaker.acquire()
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.half_open_probes == 1

    # Stale probe B from epoch 1 releasing or succeeding must be ignored completely.
    probe_b.release()
    assert breaker.half_open_probes == 1
    assert breaker.state is CircuitState.HALF_OPEN

    breaker.record_success(epoch=1)
    assert breaker.state is CircuitState.HALF_OPEN

    probe_c.record_success()
    assert breaker.state is CircuitState.CLOSED


def test_five_value_error_batches_in_export_sink_leave_sink_and_http_breakers_closed(
    respx_mock,
) -> None:
    """Five ValueError batches in ExportSink leave both sink and HTTP client breakers CLOSED."""
    respx_mock.get(f"{BASE_URL}/api/v1/edc/studies").respond(
        200,
        json={"data": [{"studyKey": "STUDY1"}], "pagination": {"totalPages": 1}},
    )

    writer = MagicMock(side_effect=ValueError("invalid clinical row"))
    sink = _StubExportSink(SinkConfig(study_key="STUDY1", max_retries=2, retry_backoff=0.0), writer)

    with ImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as sdk:
        for idx in range(5):
            with pytest.raises(ExportBatchError, match="after 1 attempts"):
                sink.write_batch([{"id": idx}], batch_id=f"STUDY1/DM/{idx}")

        for extra_err in (TypeError("wrong record type"), ExportConfigurationError("bad uri")):
            extra_sink = _StubExportSink(
                SinkConfig(study_key="STUDY1", max_retries=2, retry_backoff=0.0),
                MagicMock(side_effect=extra_err),
            )
            with pytest.raises(ExportBatchError, match="after 1 attempts"):
                extra_sink.write_batch([{"id": 1}], batch_id="STUDY1/DM/extra")
            assert extra_sink._circuit_breaker.state is CircuitState.CLOSED
            assert extra_sink._circuit_breaker.consecutive_failures == 0

        assert writer.call_count == 5
        assert sink._circuit_breaker.state is CircuitState.CLOSED
        assert sink._circuit_breaker.consecutive_failures == 0
        assert sdk._client._circuit_breaker.state is CircuitState.CLOSED
        assert sdk._client._circuit_breaker.consecutive_failures == 0

        studies = list(sdk.studies.list())
        assert len(studies) == 1
        assert studies[0].study_key == "STUDY1"


def test_export_sink_outage_isolates_breaker_from_http_clients_and_other_sinks(
    respx_mock,
) -> None:
    """Destination outages in ExportSink open only that sink breaker without blocking SDK calls."""
    respx_mock.get(f"{BASE_URL}/api/v1/edc/studies").respond(
        200,
        json={"data": [{"studyKey": "STUDY1"}], "pagination": {"totalPages": 1}},
    )

    failing_writer = MagicMock(side_effect=RuntimeError("mongodb://user:secret@host:27017 down"))
    healthy_writer = MagicMock(side_effect=lambda records: len(records))

    broken_sink = _StubExportSink(
        SinkConfig(study_key="STUDY1", max_retries=0, retry_backoff=0.0),
        failing_writer,
    )
    healthy_sink = _StubExportSink(
        SinkConfig(study_key="STUDY1", max_retries=0, retry_backoff=0.0),
        healthy_writer,
    )

    with ImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as sdk:
        for idx in range(broken_sink._circuit_breaker.failure_threshold):
            with pytest.raises(ExportBatchError, match="after 1 attempts") as exc_info:
                broken_sink.write_batch([{"id": idx}], batch_id=f"STUDY1/AE/{idx}")
            assert "secret" not in str(exc_info.value)

        assert broken_sink._circuit_breaker.state is CircuitState.OPEN
        assert healthy_sink._circuit_breaker.state is CircuitState.CLOSED
        assert sdk._client._circuit_breaker.state is CircuitState.CLOSED
        assert get_global_circuit_breaker().state is CircuitState.CLOSED

        with pytest.raises(ExportBatchError, match="after 0 attempts"):
            broken_sink.write_batch([{"id": 99}], batch_id="STUDY1/AE/99")
        assert failing_writer.call_count == broken_sink._circuit_breaker.failure_threshold

        # Sibling sink and SDK HTTP reads continue unaffected.
        assert healthy_sink.write_batch([{"id": 1}, {"id": 2}], batch_id="STUDY1/DM/0") == 2
        studies = list(sdk.studies.list())
        assert len(studies) == 1


@pytest.mark.asyncio
async def test_direct_executor_fallback_uses_global_circuit_breaker_and_skips_cancelled_retry() -> (
    None
):
    """Direct executors fall back to get_global_circuit_breaker and never retry CancelledError."""
    global_breaker = get_global_circuit_breaker()
    global_breaker.reset()

    class RetryEverythingPolicy(RetryPolicy):
        def should_retry(self, state: RetryState) -> bool:
            _ = state
            return True

    sync_exec = SyncRequestExecutor(
        send=MagicMock(side_effect=httpx.ConnectError("boom")),
        retry_config=RetryConfig(retries=1, backoff_factor=0),
    )
    assert sync_exec.circuit_breaker is global_breaker
    with pytest.raises(RequestError):
        sync_exec("GET", f"{BASE_URL}/ping")
    assert global_breaker.consecutive_failures == 1

    calls = 0

    async def cancelled_send(*_args, **_kwargs) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    async_exec = AsyncRequestExecutor(
        send=cancelled_send,
        retry_config=RetryConfig(
            retries=3,
            backoff_factor=0,
            retry_policy=RetryEverythingPolicy(),
        ),
    )
    assert async_exec.circuit_breaker is global_breaker
    with pytest.raises(asyncio.CancelledError):
        await async_exec("GET", f"{BASE_URL}/ping")
    assert calls == 1
    assert global_breaker.consecutive_failures == 1


@pytest.mark.asyncio
async def test_circuit_breaker_error_subclasses_imednet_error_and_attaches_context(
    respx_mock,
) -> None:
    """CircuitBreakerError subclasses ImednetError in errors & spi.errors and carries DiagnosticContext."""
    from imednet.errors import CircuitBreakerError as PublicCircuitBreakerError
    from imednet.errors import ImednetError
    from imednet.spi.errors import CircuitBreakerError as SpiCircuitBreakerError

    assert PublicCircuitBreakerError is CircuitBreakerError
    assert SpiCircuitBreakerError is CircuitBreakerError
    assert issubclass(CircuitBreakerError, ImednetError)

    respx_mock.get(f"{BASE_URL}/down").respond(500)

    with Client(
        api_key="k",
        security_key="s",
        base_url=BASE_URL,
        retry_config=RetryConfig(retries=0, backoff_factor=0),
    ) as client:
        client._circuit_breaker.failure_threshold = 1
        with pytest.raises(ServerError):
            client.get("/down")

        with pytest.raises(ImednetError) as cb_exc:
            client.get("/down?api_key=raw-cb-secret")

    assert isinstance(cb_exc.value, CircuitBreakerError)
    assert cb_exc.value.method == "GET"
    assert cb_exc.value.url == "/down?api_key=***"
    assert cb_exc.value.attempt_count == 0
