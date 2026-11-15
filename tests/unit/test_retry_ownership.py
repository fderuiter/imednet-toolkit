"""Exercise retry ownership across real pagination, transport and sink seams."""

from unittest.mock import MagicMock

import httpx
import pytest

from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.operations.circuit_breaker import get_global_circuit_breaker
from imednet.core.paginator import (
    AsyncJsonListPaginator,
    AsyncPaginator,
    JsonListPaginator,
    Paginator,
)
from imednet.core.retry import RetryConfig
from imednet.errors import ApiError, ExportBatchError, ExportConfigurationError, RequestError
from imednet.integrations.sink_base import ExportSink, SinkConfig

BASE_URL = "https://synthetic.example"


@pytest.mark.parametrize("paginator_type", [Paginator, JsonListPaginator])
@pytest.mark.parametrize("status,attempts", [(500, 3), (429, 3), (400, 1), (0, 3)])
def test_sync_pagination_has_one_retry_owner(respx_mock, paginator_type, status, attempts):
    route = respx_mock.get(BASE_URL + "/records")
    if status:
        route.respond(status)
    else:
        route.mock(side_effect=httpx.ConnectError("synthetic connection failure"))
    with (
        Client(
            api_key="synthetic",
            security_key="synthetic",
            base_url=BASE_URL,
            retry_config=RetryConfig(retries=2, backoff_factor=0),
        ) as client,
        pytest.raises((ApiError, RequestError)),
    ):
        list(paginator_type(client, "/records"))
    assert route.call_count == attempts
    # The outer paginator must not double-count exhausted requests.
    assert get_global_circuit_breaker()._consecutive_failures == 1


@pytest.mark.parametrize("paginator_type", [AsyncPaginator, AsyncJsonListPaginator])
@pytest.mark.parametrize("status,attempts", [(500, 3), (429, 3), (400, 1), (0, 3)])
async def test_async_pagination_has_one_retry_owner(respx_mock, paginator_type, status, attempts):
    route = respx_mock.get(BASE_URL + "/records")
    if status:
        route.respond(status)
    else:
        route.mock(side_effect=httpx.ConnectError("synthetic connection failure"))
    async with AsyncClient(
        api_key="synthetic",
        security_key="synthetic",
        base_url=BASE_URL,
        retry_config=RetryConfig(retries=2, backoff_factor=0),
    ) as client:
        with pytest.raises((ApiError, RequestError)):
            _ = [item async for item in paginator_type(client, "/records")]
    assert route.call_count == attempts
    assert get_global_circuit_breaker()._consecutive_failures == 1


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_pagination_recovery_stops_after_success(respx_mock, asynchronous):
    route = respx_mock.get(BASE_URL + "/records").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"data": [1, 2], "pagination": {"totalPages": 1}}),
        ]
    )
    kwargs = dict(
        api_key="synthetic",
        security_key="synthetic",
        base_url=BASE_URL,
        retry_config=RetryConfig(retries=2, backoff_factor=0),
    )
    if asynchronous:
        async with AsyncClient(**kwargs) as client:
            assert [item async for item in AsyncPaginator(client, "/records")] == [1, 2]
    else:
        with Client(**kwargs) as client:
            assert list(Paginator(client, "/records")) == [1, 2]
    assert route.call_count == 2
    assert get_global_circuit_breaker()._consecutive_failures == 0


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_early_consumer_exit_never_fetches_next_page(respx_mock, asynchronous):
    route = respx_mock.get(BASE_URL + "/records").respond(
        200,
        json={"data": [1, 2], "pagination": {"totalPages": 2}},
    )
    kwargs = dict(api_key="synthetic", security_key="synthetic", base_url=BASE_URL)
    if asynchronous:
        async with AsyncClient(**kwargs) as client:
            iterator = AsyncPaginator(client, "/records").__aiter__()
            assert await anext(iterator) == 1
            await iterator.aclose()
    else:
        with Client(**kwargs) as client:
            iterator = iter(Paginator(client, "/records"))
            assert next(iterator) == 1
            iterator.close()
    assert route.call_count == 1


class SyntheticSink(ExportSink):
    """Use the real sink/executor boundary with a controlled destination."""

    def write_batch(self, records, *, batch_id):
        return len(records)

    def flush(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("retries,expected_sleeps", [(0, []), (2, [0.25, 0.5])])
def test_sink_retries_use_configured_attempts_and_backoff(monkeypatch, retries, expected_sleeps):
    sleeps = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
    sink = SyntheticSink(SinkConfig(study_key="synthetic", max_retries=retries, retry_backoff=0.25))
    destination = MagicMock(side_effect=RuntimeError("destination unavailable"))
    with pytest.raises(ExportBatchError, match=f"after {retries + 1} attempts") as caught:
        sink._execute_with_retry("synthetic_write", "synthetic/0", destination)
    assert destination.call_count == retries + 1
    assert sleeps == expected_sleeps
    assert caught.value.batch_id == "synthetic/0"


def test_sink_success_stops_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
    sink = SyntheticSink(SinkConfig(study_key="synthetic", max_retries=3, retry_backoff=0.25))
    destination = MagicMock(side_effect=[RuntimeError("unavailable"), 7])
    assert sink._execute_with_retry("synthetic_write", "synthetic/0", destination) == 7
    assert destination.call_count == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize("error_type", [ExportConfigurationError, ValueError, TypeError])
def test_sink_permanent_failures_do_not_retry_or_expose_driver_message(
    monkeypatch, caplog, error_type
):
    sleeps = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
    tracer = MagicMock()
    sentinel = "synthetic-private-driver-message"
    sink = SyntheticSink(SinkConfig(study_key="synthetic", max_retries=2, tracer=tracer))
    destination = MagicMock(side_effect=error_type(sentinel))
    with pytest.raises(ExportBatchError, match="after 1 attempts") as caught:
        sink._execute_with_retry("synthetic_write", "synthetic/0", destination)
    assert destination.call_count == 1
    assert sleeps == []
    assert sentinel not in str(caught.value)
    assert sentinel not in caplog.text
    assert sentinel not in str(tracer.mock_calls)


def test_sink_transient_failures_are_redacted_before_tracing(monkeypatch, caplog):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
    tracer = MagicMock()
    sink = SyntheticSink(SinkConfig(study_key="synthetic", max_retries=0, tracer=tracer))
    destination = MagicMock(side_effect=RuntimeError("synthetic-private-driver-message"))
    with pytest.raises(ExportBatchError) as caught:
        sink._execute_with_retry("synthetic_write", "synthetic/0", destination)
    assert "synthetic-private-driver-message" not in str(caught.value)
    assert "synthetic-private-driver-message" not in caplog.text
    assert "synthetic-private-driver-message" not in str(tracer.mock_calls)


def test_sink_reports_zero_attempts_when_breaker_prevents_execution():
    breaker = get_global_circuit_breaker()
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()
    destination = MagicMock(return_value=7)
    sink = SyntheticSink(SinkConfig(study_key="synthetic"))
    with pytest.raises(ExportBatchError, match="after 0 attempts"):
        sink._execute_with_retry("synthetic_write", "synthetic/0", destination)
    destination.assert_not_called()


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_sink_rejects_invalid_retry_counts(value):
    with pytest.raises(ValueError, match="max_retries"):
        SinkConfig(study_key="synthetic", max_retries=value)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "slow"])
def test_sink_rejects_invalid_retry_backoff(value):
    with pytest.raises(ValueError, match="retry_backoff"):
        SinkConfig(study_key="synthetic", retry_backoff=value)
