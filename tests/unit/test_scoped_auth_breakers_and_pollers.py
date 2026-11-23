"""Regression tests for origin-scoped HTTP auth, instance breakers, and cooperative pollers."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from imednet import AsyncImednetSDK, ImednetSDK, errors
from imednet.auth.api_key import ApiKeyAuth
from imednet.auth.oidc import OIDCAuth
from imednet.constants import DEFAULT_MAX_RETRY_AFTER
from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.context import (
    get_base_url_context,
    get_study_context,
    is_strict_mode,
    reset_base_url_context,
    set_base_url_context,
    strict_mode_context,
    study_context,
)
from imednet.core.http.executor import AsyncRequestExecutor, SyncRequestExecutor
from imednet.core.operations.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
    get_global_circuit_breaker,
)
from imednet.core.operations.executor import UniversalExecutor
from imednet.core.retry import RetryConfig
from imednet.datadict.models import DataDictionary
from imednet.errors import ExportBatchError, ExportConfigurationError
from imednet.integrations.sink_base import ExportSink, SinkConfig
from imednet.models.jobs import JobStatus
from imednet.utils.job_poller import (
    AsyncJobPoller,
    JobFailedError,
    JobPoller,
    JobTimeoutError,
)
from imednet_workflows.uat.engine import EditCheckResultStatus, UATExecutionEngine

EDC_BASE_URL = "https://edc.synthetic.imednet.test"
S3_RESULT_URL = (
    "https://imednet-results.s3.us-east-1.amazonaws.com/"
    "studies/STUDY1/batches/BATCH-1/results.json?X-Amz-Signature=presigned"
)
CREDENTIAL_HEADERS = {"x-api-key", "x-imn-security-key", "authorization"}


class _DummySink(ExportSink):
    """Minimal concrete ExportSink for breaker isolation tests."""

    def write_batch(self, records: Any, *, batch_id: str) -> int:
        return len(records)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("auth_strategy", "expected_edc_headers"),
    [
        (
            ApiKeyAuth("secret-api-key", "secret-sec-key"),
            {"x-api-key": "secret-api-key", "x-imn-security-key": "secret-sec-key"},
        ),
        (
            OIDCAuth("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.synthetic"),
            {"authorization": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.synthetic"},
        ),
    ],
)
def test_sync_poll_job_strips_credentials_on_cross_origin_s3_result_url(
    auth_strategy: Any,
    expected_edc_headers: dict[str, str],
) -> None:
    """Polling a Job with an S3 resultUrl sends auth to EDC and zero credentials to S3."""
    captured_edc_headers: list[dict[str, str]] = []
    captured_s3_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lowered = {k.lower(): v for k, v in request.headers.items()}
        if request.url.host == "edc.synthetic.imednet.test":
            captured_edc_headers.append(lowered)
            return httpx.Response(
                200,
                json={
                    "jobId": "JOB-1",
                    "batchId": "BATCH-1",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": S3_RESULT_URL,
                },
            )
        if request.url.host == "imednet-results.s3.us-east-1.amazonaws.com":
            captured_s3_headers.append(lowered)
            return httpx.Response(200, json=[{"recordId": "R-1", "status": "SUCCESS"}])
        return httpx.Response(404)

    with ImednetSDK(
        auth=auth_strategy,
        base_url=EDC_BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as sdk:
        status = sdk.poll_job("STUDY1", "BATCH-1", interval=0, timeout=5)

    assert status.results == [{"recordId": "R-1", "status": "SUCCESS"}]
    assert len(captured_edc_headers) == 1
    for hdr, val in expected_edc_headers.items():
        assert captured_edc_headers[0][hdr] == val
    assert len(captured_s3_headers) == 1
    assert CREDENTIAL_HEADERS.isdisjoint(captured_s3_headers[0].keys())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_strategy", "expected_edc_headers"),
    [
        (
            ApiKeyAuth("async-api-key", "async-sec-key"),
            {"x-api-key": "async-api-key", "x-imn-security-key": "async-sec-key"},
        ),
        (
            OIDCAuth("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.async-synthetic"),
            {"authorization": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.async-synthetic"},
        ),
    ],
)
async def test_async_poll_job_strips_credentials_on_cross_origin_s3_result_url(
    auth_strategy: Any,
    expected_edc_headers: dict[str, str],
) -> None:
    """Async polling a Job with an S3 resultUrl sends auth to EDC and zero credentials to S3."""
    captured_edc_headers: list[dict[str, str]] = []
    captured_s3_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lowered = {k.lower(): v for k, v in request.headers.items()}
        if request.url.host == "edc.synthetic.imednet.test":
            captured_edc_headers.append(lowered)
            return httpx.Response(
                200,
                json={
                    "jobId": "JOB-1",
                    "batchId": "BATCH-1",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": S3_RESULT_URL,
                },
            )
        if request.url.host == "imednet-results.s3.us-east-1.amazonaws.com":
            captured_s3_headers.append(lowered)
            return httpx.Response(200, json=[{"recordId": "R-2", "status": "SUCCESS"}])
        return httpx.Response(404)

    async with AsyncImednetSDK(
        auth=auth_strategy,
        base_url=EDC_BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as sdk:
        summary = await sdk.async_poll_jobs("STUDY1", ["BATCH-1"], interval=0, timeout=5)

    assert summary.all_successful is True
    assert summary.results["BATCH-1"].results == [{"recordId": "R-2", "status": "SUCCESS"}]
    assert len(captured_edc_headers) == 1
    for hdr, val in expected_edc_headers.items():
        assert captured_edc_headers[0][hdr] == val
    assert len(captured_s3_headers) == 1
    assert CREDENTIAL_HEADERS.isdisjoint(captured_s3_headers[0].keys())


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://imednet-results.s3.amazonaws.com/results.json",
        "file:///etc/passwd",
        "//evil.example.com/results.json",
        "https://localhost/results.json",
        "https://sub.localhost/results.json",
        "https://cluster.local/results.json",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://127.0.0.1/results.json",
        "https://[::1]/results.json",
        "https://10.0.0.42/results.json",
        "https://172.16.5.4/results.json",
        "https://192.168.1.10/results.json",
        "https://169.254.169.254/latest/meta-data/",
        "https://100.100.100.200/latest/meta-data/",
        "https://[fd00:ec2::254]/latest/meta-data/",
        "https://[::ffff:169.254.169.254]/latest/meta-data/",
        "https://2130706433/results.json",
        "https://0x7f000001/results.json",
        "https://user:pass@s3.amazonaws.com/results.json",  # pragma: allowlist secret
    ],
)
def test_external_result_url_ssrf_and_scheme_violations_fail_closed(unsafe_url: str) -> None:
    """Unsafe external resultUrl schemes, hosts, and IPs fail closed with JobFailedError."""
    external_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal external_called
        if request.url.host == "edc.synthetic.imednet.test":
            return httpx.Response(
                200,
                json={
                    "jobId": "JOB-SSRF",
                    "batchId": "BATCH-SSRF",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": unsafe_url,
                },
            )
        external_called = True
        return httpx.Response(200, json={"unexpected": True})

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url=EDC_BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as sdk:
        with pytest.raises(JobFailedError, match="failed to fetch resultUrl"):
            sdk.poll_job("STUDY1", "BATCH-SSRF", interval=0, timeout=5)

    assert external_called is False


@pytest.mark.asyncio
async def test_external_result_url_dns_private_ip_and_cross_origin_redirects_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS resolution to metadata/private IPs and cross-origin redirects raise JobFailedError."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "edc.synthetic.imednet.test":
            batch_id = request.url.path.rsplit("/", 1)[-1]
            target = (
                "https://rebind.synthetic.example/results.json"
                if batch_id == "B-DNS"
                else "https://s3.synthetic.example/redirect"
            )
            return httpx.Response(
                200,
                json={
                    "jobId": f"J-{batch_id}",
                    "batchId": batch_id,
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": target,
                },
            )
        if str(request.url) == "https://s3.synthetic.example/redirect":
            return httpx.Response(
                302,
                headers={"Location": "https://attacker.synthetic.example/steal"},
            )
        if str(request.url) == "https://s3.synthetic.example/same-origin-start":
            return httpx.Response(
                307,
                headers={"Location": "/same-origin-final"},
            )
        if str(request.url) == "https://s3.synthetic.example/same-origin-final":
            assert CREDENTIAL_HEADERS.isdisjoint({k.lower() for k in request.headers})
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url=EDC_BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as sdk:
        # Cross-origin redirect fails closed with JobFailedError
        with pytest.raises(JobFailedError, match="Cross-origin redirects are not allowed"):
            await sdk.async_poll_job("STUDY1", "B-REDIRECT", interval=0, timeout=5)

        # Same-origin HTTPS redirect succeeds without leaking credentials
        same_origin_resp = await sdk._async_client.fetch_external_url(
            "https://s3.synthetic.example/same-origin-start",
            headers={"Authorization": "Bearer should-be-stripped"},
        )
        assert same_origin_resp.json() == {"ok": True}

        # DNS rebinding to AWS metadata IP fails closed with JobFailedError
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, **_kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))
            ],
        )
        with pytest.raises(JobFailedError, match="Blocked non-public IP"):
            await sdk.async_poll_job("STUDY1", "B-DNS", interval=0, timeout=5)


@pytest.mark.asyncio
async def test_mock_transport_skips_live_dns_lookup() -> None:
    """Injected httpx.MockTransport on Client and AsyncClient avoids live DNS lookups."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"offline": True}))

    with Client(
        api_key="k",
        security_key="s",
        base_url=EDC_BASE_URL,
        transport=transport,
    ) as sync_client:
        assert sync_client._should_resolve_external_dns() is False
        resp = sync_client.fetch_external_url("https://unresolvable-offline-host.invalid/data.json")
        assert resp.json() == {"offline": True}

    async with AsyncClient(
        api_key="k",
        security_key="s",
        base_url=EDC_BASE_URL,
        transport=transport,
    ) as async_client:
        assert async_client._should_resolve_external_dns() is False
        resp_async = await async_client.fetch_external_url(
            "https://unresolvable-offline-host.invalid/data.json"
        )
        assert resp_async.json() == {"offline": True}


def test_uat_execution_engine_routes_polling_through_facade_poll_jobs() -> None:
    """UATExecutionEngine delegates batch polling to ImednetFacade.poll_jobs without leaking auth."""
    s3_headers_seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "edc.synthetic.imednet.test":
            if request.method == "GET" and request.url.path.endswith(
                ("/forms", "/variables", "/intervals", "/sites", "/subjects")
            ):
                return httpx.Response(200, json={"data": []})
            if request.method == "POST" and request.url.path.endswith("/records"):
                return httpx.Response(
                    200,
                    json={"jobId": "J-UAT-1", "batchId": "B-UAT-1", "state": "PROCESSING"},
                )
            if request.method == "GET" and request.url.path.endswith("/jobs/B-UAT-1"):
                return httpx.Response(
                    200,
                    json={
                        "jobId": "J-UAT-1",
                        "batchId": "B-UAT-1",
                        "state": "COMPLETED",
                        "progress": 100,
                        "failedRecords": 1,
                        "resultUrl": S3_RESULT_URL,
                    },
                )
        if request.url.host == "imednet-results.s3.us-east-1.amazonaws.com":
            s3_headers_seen.append({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(
                200,
                json=[{"status": "FAILED", "error": "AGE must be >= 18"}],
            )
        return httpx.Response(404)

    with ImednetSDK(
        api_key="uat-key",  # pragma: allowlist secret
        security_key="uat-sec",  # pragma: allowlist secret
        base_url=EDC_BASE_URL,
        transport=httpx.MockTransport(handler),
    ) as sdk:
        poll_jobs_spy = MagicMock(wraps=sdk.poll_jobs)
        object.__setattr__(sdk, "poll_jobs", poll_jobs_spy)

        dd = DataDictionary(
            business_logic=[
                {
                    "Name": "AGE_MIN",
                    "Status": "Active",
                    "Form": "DEMOG",
                    "Variable": "AGE",
                    "Operator": ">=",
                    "Value": "18",
                }
            ],
            choices=[],
            forms=[],
            questions=[],
        )
        engine = UATExecutionEngine(
            sdk=sdk,
            data_dictionary=dd,
            poll_interval=0.0,
            poll_timeout=5.0,
        )
        report = engine.run_verification("STUDY1")

    poll_jobs_spy.assert_called_once_with(
        "STUDY1",
        ["B-UAT-1"],
        interval=0.0,
        timeout=5.0,
        fail_fast=False,
    )
    assert report.passed_rules == 1
    assert report.results[0]["status"] == EditCheckResultStatus.PASS
    assert len(s3_headers_seen) == 1
    assert CREDENTIAL_HEADERS.isdisjoint(s3_headers_seen[0].keys())


@pytest.mark.asyncio
async def test_export_sink_configuration_errors_leave_breaker_closed_and_isolated() -> None:
    """Five ExportConfigurationError failures keep the sink breaker CLOSED and isolate peers."""
    get_global_circuit_breaker().reset()
    sink_a = _DummySink(SinkConfig(study_key="STUDY1", max_retries=2, retry_backoff=0.0))
    sink_b = _DummySink(SinkConfig(study_key="STUDY1", max_retries=0, retry_backoff=0.0))

    for idx in range(5):
        with pytest.raises(ExportBatchError, match="after 1 attempts"):
            sink_a._execute_with_retry(
                "export_batch",
                f"STUDY1/{idx}",
                MagicMock(side_effect=ExportConfigurationError("bad URI")),
            )

    assert sink_a._circuit_breaker.state == CircuitState.CLOSED
    assert sink_a._circuit_breaker._consecutive_failures == 0
    assert sink_a._execute_with_retry("export_batch", "STUDY1/ok", lambda: 10) == 10

    # Now trip sink_b via transient destination failures and verify sink_a + SDK clients stay CLOSED
    for idx in range(sink_b._circuit_breaker.failure_threshold):
        with pytest.raises(ExportBatchError):
            sink_b._execute_with_retry(
                "export_batch",
                f"STUDY1/ transient-{idx}",
                MagicMock(side_effect=RuntimeError("warehouse timeout")),
            )

    assert sink_b._circuit_breaker.state == CircuitState.OPEN
    assert sink_a._circuit_breaker.state == CircuitState.CLOSED
    assert get_global_circuit_breaker().state == CircuitState.CLOSED

    ok_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"data": []}))
    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url=EDC_BASE_URL,
        transport=ok_transport,
    ) as sync_sdk:
        assert sync_sdk._client.get("/studies").status_code == 200
        assert sync_sdk._client._circuit_breaker.state == CircuitState.CLOSED

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url=EDC_BASE_URL,
        transport=ok_transport,
    ) as async_sdk:
        resp = await async_sdk._async_client.get("/studies")
        assert resp.status_code == 200
        assert async_sdk._async_client._circuit_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_cancelled_half_open_probes_release_slots_in_http_and_universal_executors() -> None:
    """Cancelling an async probe in HALF_OPEN releases _half_open_probes for subsequent recovery."""
    # 1. AsyncRequestExecutor
    http_breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout=0.01, half_open_max_probes=1
    )
    http_breaker.record_failure()
    await asyncio.sleep(0.02)
    assert http_breaker.state == CircuitState.OPEN

    in_flight = asyncio.Event()
    allow_finish = asyncio.Event()

    async def slow_send(method: str, url: str, **kwargs: Any) -> httpx.Response:
        in_flight.set()
        await allow_finish.wait()
        return httpx.Response(200, json={"recovered": True})

    http_exec = AsyncRequestExecutor(
        send=slow_send,
        retry_config=RetryConfig(retries=0),
        circuit_breaker=http_breaker,
    )

    probe_task = asyncio.create_task(http_exec("GET", "/probe"))
    await in_flight.wait()
    assert http_breaker.state == CircuitState.HALF_OPEN
    assert http_breaker._half_open_probes == 1

    # While probe is active, second request is blocked
    with pytest.raises(CircuitBreakerError, match="probe limit reached"):
        await http_exec("GET", "/blocked")

    probe_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe_task

    assert http_breaker.state == CircuitState.HALF_OPEN
    assert http_breaker._half_open_probes == 0

    allow_finish.set()
    recovered = await http_exec("GET", "/recover")
    assert recovered.status_code == 200
    assert http_breaker.state == CircuitState.CLOSED

    # 2. UniversalExecutor.execute_async
    op_breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01, half_open_max_probes=1)
    op_breaker.record_failure()
    await asyncio.sleep(0.02)
    assert op_breaker.state == CircuitState.OPEN

    op_started = asyncio.Event()
    op_gate = asyncio.Event()

    async def slow_op() -> str:
        op_started.set()
        await op_gate.wait()
        return "ok"

    u_exec = UniversalExecutor(
        retry_config=RetryConfig(retries=0),
        circuit_breaker=op_breaker,
    )
    op_task = asyncio.create_task(u_exec.execute_async(slow_op))
    await op_started.wait()
    assert op_breaker.state == CircuitState.HALF_OPEN
    assert op_breaker._half_open_probes == 1

    op_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await op_task

    assert op_breaker.state == CircuitState.HALF_OPEN
    assert op_breaker._half_open_probes == 0

    op_gate.set()
    assert await u_exec.execute_async(slow_op) == "ok"
    assert op_breaker.state == CircuitState.CLOSED


def test_post_503_counts_once_per_call_and_retry_after_clamps_to_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three non-retried POST 503s count 3 breaker failures, and Retry-After clamps to max_retry_after."""
    assert RetryConfig().max_retry_after == DEFAULT_MAX_RETRY_AFTER == 60.0

    with pytest.raises(ValueError, match="max_retry_after"):
        RetryConfig(max_retry_after=-1.0)
    with pytest.raises(ValueError, match="max_retry_after"):
        RetryConfig(max_retry_after=float("inf"))

    def post_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/400":
            return httpx.Response(400, json={"error": "bad"})
        return httpx.Response(503, json={"error": "unavailable"})

    with Client(
        api_key="k",
        security_key="s",
        base_url=EDC_BASE_URL,
        transport=httpx.MockTransport(post_handler),
        retry_config=RetryConfig(retries=2, backoff_factor=0.0),
    ) as client:
        for _ in range(3):
            with pytest.raises(errors.ServerError):
                client.post("/records", json={"data": 1})
        assert client._circuit_breaker._consecutive_failures == 3
        assert client._circuit_breaker.state == CircuitState.CLOSED

        # 4xx response does not increment breaker failures (resets consecutive failures on connection)
        with pytest.raises(errors.BadRequestError):
            client.post("/400", json={"data": 1})
        assert client._circuit_breaker._consecutive_failures == 0

    # Verify Retry-After clamping on both 429 and 503 responses
    recorded_sleeps: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", recorded_sleeps.append)

    future_http_date = format_datetime(datetime.now(UTC) + timedelta(hours=2), usegmt=True)
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "3600"}),
            httpx.Response(503, headers={"Retry-After": future_http_date}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    exec_sync = SyncRequestExecutor(
        send=lambda *a, **kw: next(responses),
        retry_config=RetryConfig(retries=3, backoff_factor=0.0),
        circuit_breaker=CircuitBreaker(),
    )
    resp = exec_sync("GET", "/clamped")
    assert resp.status_code == 200
    assert recorded_sleeps == [60.0, 60.0]

    # Also verify custom max_retry_after
    recorded_sleeps.clear()
    custom_responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    exec_custom = SyncRequestExecutor(
        send=lambda *a, **kw: next(custom_responses),
        retry_config=RetryConfig(retries=2, backoff_factor=0.0, max_retry_after=4.5),
        circuit_breaker=CircuitBreaker(),
    )
    assert exec_custom("POST", "/custom").status_code == 200
    assert recorded_sleeps == [4.5]


def test_job_poller_poll_many_preserves_contextvars_and_wakes_immediately_on_fail_fast() -> None:
    """JobPoller.poll_many copies ContextVar state into threads and wakes siblings on fail_fast."""
    observed_contexts: dict[str, tuple[bool, str | None, str | None]] = {}

    def get_job_strict(study_key: str, batch_id: str) -> JobStatus:
        observed_contexts[batch_id] = (
            is_strict_mode(),
            get_study_context(),
            get_base_url_context(),
        )
        # Validate wire payload inside worker thread: strict_mode_context=True rejects extra fields
        if batch_id == "EXTRA_FIELD":
            return JobStatus.model_validate(
                {
                    "jobId": "J-EXTRA",
                    "batchId": batch_id,
                    "state": "COMPLETED",
                    "progress": 100,
                    "unexpectedWireField": "must-fail-in-strict-mode",
                }
            )
        return JobStatus.model_validate(
            {"jobId": "J-OK", "batchId": batch_id, "state": "COMPLETED", "progress": 100}
        )

    base_url_tok = set_base_url_context(EDC_BASE_URL)
    try:
        with strict_mode_context(True), study_context("CTX-STUDY-99"):
            summary = JobPoller(get_job_strict).poll_many(
                "CTX-STUDY-99",
                ["OK_BATCH", "EXTRA_FIELD"],
                interval=0.0,
                fail_fast=False,
            )
    finally:
        reset_base_url_context(base_url_tok)

    assert observed_contexts["OK_BATCH"] == (True, "CTX-STUDY-99", EDC_BASE_URL)
    assert observed_contexts["EXTRA_FIELD"] == (True, "CTX-STUDY-99", EDC_BASE_URL)
    assert "OK_BATCH" in summary.results
    assert isinstance(summary.failures["EXTRA_FIELD"], PydanticValidationError)

    # Verify cooperative threading.Event wakes sleeping sibling threads immediately on fail_fast=True
    sibling_entered_sleep = threading.Event()
    slow_polls = 0

    def get_job_fail_fast(study_key: str, batch_id: str) -> JobStatus:
        nonlocal slow_polls
        if batch_id == "SLOW":
            slow_polls += 1
            sibling_entered_sleep.set()
            return JobStatus(batchId="SLOW", jobId="J-S", state="PROCESSING", progress=10)
        sibling_entered_sleep.wait(timeout=2.0)
        time.sleep(0.02)
        return JobStatus(batchId="FAST_FAIL", jobId="J-F", state="FAILED", progress=100)

    start = time.monotonic()
    with pytest.raises(JobFailedError, match="FAST_FAIL"):
        JobPoller(get_job_fail_fast).poll_many(
            "STUDY1",
            ["SLOW", "FAST_FAIL"],
            interval=10.0,
            timeout=30.0,
            fail_fast=True,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
    assert slow_polls == 1


@pytest.mark.asyncio
async def test_async_poll_many_cancels_and_awaits_sibling_tasks_before_raising() -> None:
    """AsyncJobPoller.async_poll_many cancels and awaits all pending sibling Tasks on fail_fast."""
    sibling_started = asyncio.Event()
    sibling_cleaned_up = False

    async def get_job(study_key: str, batch_id: str) -> JobStatus:
        nonlocal sibling_cleaned_up
        if batch_id == "SIBLING":
            sibling_started.set()
            try:
                await asyncio.sleep(30.0)
            finally:
                await asyncio.sleep(0)
                sibling_cleaned_up = True
            return JobStatus(batchId="SIBLING", jobId="J-S", state="COMPLETED", progress=100)

        await sibling_started.wait()
        return JobStatus(batchId="FAIL_BATCH", jobId="J-F", state="FAILED", progress=100)

    poller = AsyncJobPoller(get_job)
    start = time.monotonic()
    with pytest.raises(JobFailedError, match="FAIL_BATCH"):
        await poller.async_poll_many(
            "STUDY1",
            ["SIBLING", "FAIL_BATCH"],
            interval=10.0,
            timeout=30.0,
            fail_fast=True,
        )
    elapsed = time.monotonic() - start

    assert sibling_cleaned_up is True
    assert elapsed < 1.0

    # Also verify JobTimeoutError propagates directly and cancels siblings
    timeout_sibling_cleaned = False
    timeout_sibling_started = asyncio.Event()

    async def get_job_timeout(study_key: str, batch_id: str) -> JobStatus:
        nonlocal timeout_sibling_cleaned
        if batch_id == "SLOW_SIBLING":
            timeout_sibling_started.set()
            try:
                await asyncio.sleep(30.0)
            finally:
                timeout_sibling_cleaned = True
            return JobStatus(batchId=batch_id, jobId="J-1", state="PROCESSING", progress=10)
        await timeout_sibling_started.wait()
        return JobStatus(batchId=batch_id, jobId="J-2", state="PROCESSING", progress=10)

    with pytest.raises(JobTimeoutError, match="TIMED_OUT"):
        await AsyncJobPoller(get_job_timeout).async_poll_many(
            "STUDY1",
            ["SLOW_SIBLING", "TIMED_OUT"],
            interval=10.0,
            timeout=0.0,
            fail_fast=True,
        )
    assert timeout_sibling_cleaned is True
