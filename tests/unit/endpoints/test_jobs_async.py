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
