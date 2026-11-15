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
