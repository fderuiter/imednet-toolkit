"""Unit tests for jobs async."""

from unittest.mock import AsyncMock

import pytest

from imednet.endpoints import jobs
from imednet.errors import NotFoundError
from imednet.models.jobs import JobStatus


@pytest.mark.asyncio
async def test_async_get_success(dummy_client, response_factory):
    """Test that async get success asynchronously."""
    # Setup async mock
    async_client = AsyncMock()
    async_client.get.return_value = response_factory({"jobId": "1"})

    ep = jobs.AsyncJobsEndpoint(async_client)

    result = await ep.get("S1", "B1")

    async_client.get.assert_called_once_with("/api/v1/edc/studies/S1/jobs/B1")
    assert isinstance(result, JobStatus)


@pytest.mark.asyncio
async def test_async_get_not_found(dummy_client, response_factory):
    """Test that async get not found asynchronously."""
    async_client = AsyncMock()
    async_client.get.return_value = response_factory({})

    ep = jobs.AsyncJobsEndpoint(async_client)

    with pytest.raises(NotFoundError):
        await ep.get("S1", "B1")


def test_async_jobs_endpoint_has_no_list_method():
    """Test that AsyncJobsEndpoint does not have or inherit a list method."""
    async_client = AsyncMock()
    ep = jobs.AsyncJobsEndpoint(async_client)
    assert not hasattr(ep, "list")
    assert not hasattr(jobs.AsyncJobsEndpoint, "list")


@pytest.mark.asyncio
async def test_async_poll_and_poll_many_do_not_fetch_result_url(response_factory):
    """AsyncJobsEndpoint.poll and poll_many return six-field JobStatus without polling resultUrl."""
    async_client = AsyncMock()
    async_client.get.return_value = response_factory(
        {
            "jobId": "J1",
            "batchId": "B1",
            "state": "COMPLETED",
            "dateCreated": "2026-07-01T10:00:00Z",
            "dateStarted": "2026-07-01T10:00:01Z",
            "dateFinished": "2026-07-01T10:00:05Z",
            "resultUrl": "https://example.com/unbacked/results",
        }
    )
    ep = jobs.AsyncJobsEndpoint(async_client)

    res = await ep.poll("S1", "B1", interval=0.01, timeout=1.0)
    assert res.state == "COMPLETED"
    assert res.total_records is None
    assert res.failed_records is None
    async_client.get.assert_called_once_with("/api/v1/edc/studies/S1/jobs/B1")

    async_client.get.reset_mock()
    summary = await ep.poll_many("S1", ["B1"], interval=0.01, timeout=1.0)
    assert summary.all_successful is True
    assert summary.any_failed is False
    async_client.get.assert_called_once_with("/api/v1/edc/studies/S1/jobs/B1")


@pytest.mark.asyncio
async def test_async_poll_and_poll_many_resolve_results_fetches_result_url_and_raises() -> None:
    """Async poll, poll_many, async_poll_job, and async_poll_jobs inspect resultUrl when resolve_results=True."""
    import httpx

    from imednet import AsyncImednetSDK
    from imednet.core.async_client import AsyncClient
    from imednet.utils.job_poller import JobFailedError

    result_hits: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/AB-FAIL"):
            return httpx.Response(
                200,
                json={
                    "jobId": "AJ-FAIL",
                    "batchId": "AB-FAIL",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": "/api/v1/edc/studies/S1/jobs/AB-FAIL/results",
                },
            )
        if request.url.path.endswith("/jobs/AB-FAIL/results"):
            result_hits.append("AB-FAIL")
            return httpx.Response(
                200,
                json=[{"recordId": 9, "status": "FAILED", "error": "Invalid V_AGE"}],
            )
        return httpx.Response(404)

    async with AsyncImednetSDK(
        async_client=AsyncClient(
            api_key="sim-k",  # pragma: allowlist secret
            security_key="sim-s",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=httpx.MockTransport(handler),
        )
    ) as async_sdk:
        default_status = await async_sdk.async_poll_job("S1", "AB-FAIL", interval=0, timeout=2.0)
        assert default_status.state == "COMPLETED"
        assert result_hits == []

        with pytest.raises(JobFailedError):
            await async_sdk.jobs.poll(
                "S1", "AB-FAIL", interval=0, timeout=2.0, resolve_results=True
            )
        with pytest.raises(JobFailedError):
            await async_sdk.async_poll_job(
                "S1", "AB-FAIL", interval=0, timeout=2.0, resolve_results=True
            )

        summary = await async_sdk.async_poll_jobs(
            "S1", ["AB-FAIL"], interval=0, timeout=2.0, resolve_results=True
        )
        assert summary.all_successful is False
        assert isinstance(summary.failures["AB-FAIL"], JobFailedError)

        with pytest.raises(JobFailedError):
            await async_sdk.jobs.poll_many(
                "S1", ["AB-FAIL"], interval=0, timeout=2.0, fail_fast=True, resolve_results=True
            )
        with pytest.raises(JobFailedError):
            await async_sdk.async_poll_jobs(
                "S1", ["AB-FAIL"], interval=0, timeout=2.0, fail_fast=True, resolve_results=True
            )
        assert len(result_hits) == 5
