"""Public endpoint and callback parity against synthetic HTTP responses."""

import asyncio
import inspect
from collections.abc import AsyncIterator, Iterator
from unittest.mock import Mock

import httpx
import pytest

from imednet import MultiStudyOrchestrator
from imednet.core.client import Client
from imednet.core.endpoint.base import GenericListGetEndpoint
from imednet.endpoints.jobs import JobsEndpoint
from imednet.models.jobs import JobStatus
from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet.utils.job_poller import AsyncJobPoller, JobPoller, JobStatusEvent


def response(request):
    if request.url.path.endswith("jobs/B"):
        return httpx.Response(200, json={"batchId": "B", "state": "COMPLETED"})
    return httpx.Response(200, json={"data": [{"recordId": 1}], "pagination": {"totalPages": 1}})


def test_public_sync_dispatch_context_and_polling():
    with ImednetSDK(api_key="a", security_key="b", transport=httpx.MockTransport(response)) as sdk:
        with sdk.study_context("S"):
            iterator = sdk.records.list()
            assert isinstance(iterator, Iterator)
            assert next(iterator).record_id == 1
        callback = Mock()
        status = sdk.poll_job("S", "B", interval=0, on_progress=callback)
        assert isinstance(status, JobStatus)
        assert status.is_successful
        callback.assert_called_once()
        assert len(callback.call_args.args) == 3
        assert sdk.jobs.poll_many("S", []).results == {}
    assert GenericListGetEndpoint is not None
    assert MultiStudyOrchestrator is not None


async def test_public_async_native_iterator_and_event_callback():
    async with AsyncImednetSDK(
        api_key="a", security_key="b", transport=httpx.MockTransport(response)
    ) as sdk:
        with sdk.study_context("S"):
            iterator = sdk.records.list()
            assert isinstance(iterator, AsyncIterator)
            assert not inspect.isawaitable(iterator)
            assert [r.record_id async for r in iterator] == [1]
        events = []

        def callback(event: JobStatusEvent):
            events.append(event)

        status = await sdk.async_poll_job("S", "B", interval=0, on_progress=callback)
        assert status.is_successful
        assert len(events) == 1 and events[0].is_terminal
        assert (await sdk.jobs.poll_many("S", [])).results == {}


@pytest.mark.parametrize("event_style", [False, True])
def test_callback_typeerror_invokes_user_code_once(event_style):
    calls = []

    def legacy(batch_id, status, elapsed):
        calls.append(status)
        raise TypeError("inside callback")

    def event_callback(event):
        calls.append(event)
        raise TypeError("inside callback")

    status = JobStatus(batchId="B", state="COMPLETED")
    with pytest.raises(TypeError, match="inside callback"):
        JobPoller(lambda *_: status).run(
            "S", "B", on_progress=event_callback if event_style else legacy
        )
    assert len(calls) == 1


async def test_async_callback_failure_and_cancellation_propagate():
    calls = []

    async def get_job(*_):
        return JobStatus(batchId="B", state="COMPLETED")

    def callback(event):
        calls.append(event)
        raise TypeError("inside async callback")

    with pytest.raises(TypeError, match="inside async callback"):
        await AsyncJobPoller(get_job).run("S", "B", on_progress=callback)
    assert len(calls) == 1

    async def cancelled(*_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await AsyncJobPoller(cancelled).run("S", "B")


def test_legacy_ctx_keyword_and_positional_on_concrete_endpoint():
    with Client(api_key="a", security_key="b") as client:
        with pytest.raises(TypeError):
            JobsEndpoint(client, ctx=object())
        with pytest.raises(TypeError):
            JobsEndpoint(client, object())
