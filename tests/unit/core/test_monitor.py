"""Unit tests for base, request, and operation monitors."""

from unittest.mock import MagicMock

import httpx
import pytest
from tenacity import RetryError

from imednet.core.http.monitor import RequestMonitor
from imednet.core.operations.monitor import OperationMonitor
from imednet.core.operations.monitor_base import _BaseMonitor
from imednet.errors import RequestError


class DummyAsyncSpanContextManager:
    """Async context manager mock for tracer spans."""

    def __init__(self, span: MagicMock) -> None:
        """Initialize the dummy async context manager."""
        self.span = span
        self.entered = False
        self.exited = False
        self.exit_args: tuple[object, ...] | None = None

    async def __aenter__(self) -> MagicMock:
        """Enter async context."""
        self.entered = True
        return self.span

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit async context."""
        self.exited = True
        self.exit_args = (exc_type, exc_val, exc_tb)


# ---------------------------------------------------------------------------
# _BaseMonitor Tests
# ---------------------------------------------------------------------------


def test_base_monitor_sync_without_tracer() -> None:
    """Test synchronous _BaseMonitor execution when tracer is None."""
    monitor = _BaseMonitor(tracer=None, span_name="test_op", attributes={"key": "val"})

    with monitor as m:
        assert m is monitor
        assert m.span is None
        assert m.start_time > 0.0

    assert monitor.span_name == "test_op"
    assert monitor.attributes == {"key": "val"}


def test_base_monitor_sync_with_tracer() -> None:
    """Test synchronous _BaseMonitor lifecycle with a mock tracer."""
    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = _BaseMonitor(tracer=tracer, span_name="my_span", attributes={"foo": "bar"})

    with monitor as m:
        assert m.span is span
        assert m.start_time > 0.0
        tracer.start_as_current_span.assert_called_once_with(
            "my_span",
            attributes={"foo": "bar"},
        )
        span_cm.__enter__.assert_called_once()

    span_cm.__exit__.assert_called_once_with(None, None, None)


def test_base_monitor_sync_handles_exception() -> None:
    """Test that synchronous context manager forwards exceptions to __exit__."""
    tracer = MagicMock()
    span_cm = MagicMock()
    tracer.start_as_current_span.return_value = span_cm

    monitor = _BaseMonitor(tracer=tracer, span_name="err_span")

    with pytest.raises(RuntimeError, match="boom"), monitor:
        raise RuntimeError("boom")

    span_cm.__exit__.assert_called_once()
    args = span_cm.__exit__.call_args[0]
    assert args[0] is RuntimeError
    assert isinstance(args[1], RuntimeError)


@pytest.mark.asyncio
async def test_base_monitor_async_with_sync_span_fallback() -> None:
    """Test asynchronous _BaseMonitor falls back to sync span methods."""
    tracer = MagicMock()
    span_cm = MagicMock(spec=["__enter__", "__exit__"])
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = _BaseMonitor(tracer=tracer, span_name="fallback_span")

    async with monitor as m:
        assert m.span is span
        assert m.start_time > 0.0
        span_cm.__enter__.assert_called_once()

    span_cm.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.asyncio
async def test_base_monitor_async_with_async_span() -> None:
    """Test asynchronous _BaseMonitor uses native async span methods when present."""
    tracer = MagicMock()
    span = MagicMock()
    dummy_cm = DummyAsyncSpanContextManager(span)
    tracer.start_as_current_span.return_value = dummy_cm

    monitor = _BaseMonitor(tracer=tracer, span_name="async_span")

    async with monitor as m:
        assert m.span is span
        assert m.start_time > 0.0
        assert dummy_cm.entered is True

    assert dummy_cm.exited is True
    assert dummy_cm.exit_args == (None, None, None)


@pytest.mark.asyncio
async def test_base_monitor_async_handles_exception() -> None:
    """Test that asynchronous context manager forwards exceptions to __aexit__."""
    tracer = MagicMock()
    span = MagicMock()
    dummy_cm = DummyAsyncSpanContextManager(span)
    tracer.start_as_current_span.return_value = dummy_cm

    monitor = _BaseMonitor(tracer=tracer, span_name="async_err_span")

    with pytest.raises(ValueError, match="async failure"):
        async with monitor:
            raise ValueError("async failure")

    assert dummy_cm.exited is True
    assert dummy_cm.exit_args[0] is ValueError


def test_base_monitor_duration_tracking() -> None:
    """Test that duration calculation works correctly from start_time."""
    monitor = _BaseMonitor(tracer=None, span_name="timing_test")

    with monitor as m:
        duration = m.start_time
        assert duration > 0.0


# ---------------------------------------------------------------------------
# RequestMonitor Tests
# ---------------------------------------------------------------------------


def test_request_monitor_init_redacts_url_and_sets_attributes() -> None:
    """Test RequestMonitor initializes span name and redacts query parameters."""
    tracer = MagicMock()
    monitor = RequestMonitor(tracer, "GET", "https://api.example.com/v1/records?token=secret123")

    assert monitor.method == "GET"
    assert monitor.safe_url == "https://api.example.com/v1/records?token=***"
    assert monitor.span_name == "http_request"
    assert monitor.attributes == {
        "endpoint": "https://api.example.com/v1/records?token=***",
        "method": "GET",
    }


def test_request_monitor_sync_context_manager() -> None:
    """Test RequestMonitor executes synchronous context and starts span."""
    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = RequestMonitor(tracer, "POST", "https://api.example.com/data")

    with monitor as m:
        assert m.span is span
        tracer.start_as_current_span.assert_called_once_with(
            "http_request",
            attributes={"endpoint": "https://api.example.com/data", "method": "POST"},
        )


@pytest.mark.asyncio
async def test_request_monitor_async_context_manager() -> None:
    """Test RequestMonitor executes asynchronous context."""
    tracer = MagicMock()
    span_cm = MagicMock(spec=["__enter__", "__exit__"])
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = RequestMonitor(tracer, "DELETE", "https://api.example.com/data/1")

    async with monitor as m:
        assert m.span is span

    span_cm.__exit__.assert_called_once()


def test_request_monitor_on_success() -> None:
    """Test RequestMonitor on_success records status code on span and logs."""
    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = RequestMonitor(tracer, "GET", "https://api.example.com/items")
    with monitor:
        response = httpx.Response(
            200, request=httpx.Request("GET", "https://api.example.com/items")
        )
        monitor.on_success(response)

    span.set_attribute.assert_called_once_with("status_code", 200)


def test_request_monitor_on_retry_error_with_last_attempt() -> None:
    """Test RequestMonitor on_retry_error raises RequestError wrapping last exception."""
    monitor = RequestMonitor(None, "POST", "https://api.example.com/submit")
    underlying = ConnectionResetError("Connection lost")

    last_attempt = MagicMock()
    last_attempt.exception.return_value = underlying
    retry_error = RetryError(last_attempt=last_attempt)

    with pytest.raises(RequestError, match="Network request failed after retries") as exc_info:
        monitor.on_retry_error(retry_error)

    assert exc_info.value.__cause__ is underlying


def test_request_monitor_on_retry_error_without_last_attempt() -> None:
    """Test RequestMonitor on_retry_error raises RequestError wrapping RetryError when no last attempt."""
    monitor = RequestMonitor(None, "POST", "https://api.example.com/submit")
    retry_error = RetryError(last_attempt=None)

    with pytest.raises(RequestError, match="Network request failed after retries") as exc_info:
        monitor.on_retry_error(retry_error)

    assert exc_info.value.__cause__ is retry_error


# ---------------------------------------------------------------------------
# OperationMonitor Tests
# ---------------------------------------------------------------------------


def test_operation_monitor_init_and_sync_lifecycle() -> None:
    """Test OperationMonitor sets attributes and starts span correctly."""
    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = OperationMonitor(
        tracer,
        "study_export",
        study_id="STUDY-001",
        format="parquet",
    )
    assert monitor.operation_name == "study_export"
    assert monitor.span_name == "study_export"
    assert monitor.attributes == {"study_id": "STUDY-001", "format": "parquet"}

    with monitor as m:
        assert m.span is span
        tracer.start_as_current_span.assert_called_once_with(
            "study_export",
            attributes={"study_id": "STUDY-001", "format": "parquet"},
        )


@pytest.mark.asyncio
async def test_operation_monitor_async_lifecycle() -> None:
    """Test OperationMonitor works with async context manager."""
    tracer = MagicMock()
    span = MagicMock()
    dummy_cm = DummyAsyncSpanContextManager(span)
    tracer.start_as_current_span.return_value = dummy_cm

    monitor = OperationMonitor(tracer, "async_operation", attempt=1)

    async with monitor as m:
        assert m.span is span
        assert dummy_cm.entered is True

    assert dummy_cm.exited is True


def test_operation_monitor_on_success() -> None:
    """Test OperationMonitor on_success records extra attributes and success status."""
    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = OperationMonitor(tracer, "process_batch")
    with monitor:
        monitor.on_success(batch_size=50, verified=True)

    span.set_attribute.assert_any_call("batch_size", 50)
    span.set_attribute.assert_any_call("verified", True)
    span.set_attribute.assert_any_call("status", "success")


def test_operation_monitor_on_retry_error() -> None:
    """Test OperationMonitor on_retry_error records failed status and re-raises cause."""
    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = OperationMonitor(tracer, "fetch_records")
    cause = TimeoutError("Timed out")

    with monitor, pytest.raises(TimeoutError, match="Timed out"):
        monitor.on_retry_error(cause, retries=3)

    span.set_attribute.assert_any_call("status", "failed")
    span.set_attribute.assert_any_call("retries", 3)


def test_operation_monitor_on_failure() -> None:
    """Test OperationMonitor on_failure sets status to failed."""
    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    monitor = OperationMonitor(tracer, "sync_task")
    with monitor:
        monitor.on_failure(ValueError("Bad value"))

    span.set_attribute.assert_called_once_with("status", "failed")


@pytest.mark.asyncio
async def test_request_monitor_and_external_url_redact_presigned_and_userinfo_while_preserving_wire(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RequestMonitor logs/spans/errors mask pre-signed, SAS, and userinfo secrets without altering wire URLs."""
    import logging

    from imednet.core.async_client import AsyncClient
    from imednet.core.client import Client
    from imednet.errors import ApiError

    canary_s3 = "CANARY_S3_SIG_111"  # pragma: allowlist secret
    canary_cred = "CANARY_S3_CRED_112"  # pragma: allowlist secret
    canary_gcs = "CANARY_GCS_SIG_222"  # pragma: allowlist secret
    canary_sas = "CANARY_AZURE_SAS_333"  # pragma: allowlist secret
    canary_pat = "CANARY_USERINFO_PAT_AAA"  # pragma: allowlist secret

    userinfo_url = (
        f"https://{canary_pat}@exports.example.test/study-export.json"  # pragma: allowlist secret
        f"?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={canary_cred}"  # pragma: allowlist secret
        f"&X-Amz-Signature={canary_s3}&X-Goog-Signature={canary_gcs}"  # pragma: allowlist secret
        f"&sv=2024-11-04&sig={canary_sas}"  # pragma: allowlist secret
    )

    tracer = MagicMock()
    span_cm = MagicMock()
    span = MagicMock()
    span_cm.__enter__.return_value = span
    tracer.start_as_current_span.return_value = span_cm

    caplog.set_level(logging.INFO, logger="imednet.core.http.monitor")
    monitor = RequestMonitor(tracer, "GET", userinfo_url)
    with monitor:
        resp = httpx.Response(200, request=httpx.Request("GET", userinfo_url))
        monitor.on_success(resp)

    endpoint_attr = tracer.start_as_current_span.call_args.kwargs["attributes"]["endpoint"]
    assert "https://***@exports.example.test/study-export.json" in endpoint_attr
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in endpoint_attr
    assert "sv=2024-11-04" in endpoint_attr

    req_err_exc = httpx.ConnectError("boom", request=httpx.Request("GET", userinfo_url))
    with pytest.raises(RequestError) as req_exc_info:
        monitor.on_request_error(req_err_exc)

    for canary in (canary_s3, canary_cred, canary_gcs, canary_sas, canary_pat):
        assert canary not in monitor.safe_url
        assert canary not in endpoint_attr
        assert canary not in str(req_exc_info.value)
        for rec in caplog.records:
            assert canary not in rec.getMessage()
            assert canary not in str(getattr(rec, "url", ""))

    # Outbound Client.fetch_external_url and AsyncClient.fetch_external_url preserve wire query tokens
    presigned_wire_url = (
        "https://exports.example.test/study-export.json"
        f"?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature={canary_s3}"  # pragma: allowlist secret
        f"&sv=2024-11-04&sig={canary_sas}"  # pragma: allowlist secret
    )
    seen_sync_urls: list[str] = []
    seen_async_urls: list[str] = []

    def sync_handler(request: httpx.Request) -> httpx.Response:
        seen_sync_urls.append(str(request.url))
        if len(seen_sync_urls) == 1:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(403, text=f"Expired signature at {presigned_wire_url}")

    async def async_handler(request: httpx.Request) -> httpx.Response:
        seen_async_urls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(Client, "_should_resolve_external_dns", lambda self: False)
    monkeypatch.setattr(AsyncClient, "_should_resolve_external_dns", lambda self: False)

    with Client(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://edc.example.test",
        transport=httpx.MockTransport(sync_handler),
    ) as client:
        ok_resp = client.fetch_external_url(presigned_wire_url)
        assert ok_resp.status_code == 200
        assert seen_sync_urls[0] == presigned_wire_url

        with pytest.raises(ApiError) as api_exc:
            client.fetch_external_url(presigned_wire_url)
        assert canary_s3 not in str(api_exc.value)
        assert canary_sas not in str(api_exc.value)
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in str(api_exc.value)

    async with AsyncClient(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://edc.example.test",
        transport=httpx.MockTransport(async_handler),
    ) as aclient:
        a_resp = await aclient.fetch_external_url(presigned_wire_url)
        assert a_resp.status_code == 200
        assert seen_async_urls[0] == presigned_wire_url
