"""Unit tests for jobs endpoint."""

import pytest

from imednet.endpoints import jobs
from imednet.errors import NotFoundError
from imednet.models.jobs import JobStatus


def test_get_success(dummy_client, response_factory):
    """Test that get success."""
    ep = jobs.JobsEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory({"jobId": "1"})

    result = ep.get("S1", "B1")

    dummy_client.get.assert_called_once_with("/api/v1/edc/studies/S1/jobs/B1")
    assert isinstance(result, JobStatus)


def test_get_not_found(dummy_client, response_factory):
    """Test that get not found."""
    ep = jobs.JobsEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory({})
    with pytest.raises(NotFoundError):
        ep.get("S1", "B1")


def test_jobs_endpoint_has_no_list_method(dummy_client):
    """Test that JobsEndpoint does not have or inherit a list method."""
    ep = jobs.JobsEndpoint(dummy_client)
    assert not hasattr(ep, "list")
    assert not hasattr(jobs.JobsEndpoint, "list")


def test_poll_and_poll_many_do_not_fetch_result_url(dummy_client, response_factory):
    """JobsEndpoint.poll and poll_many return six-field JobStatus without polling resultUrl."""
    ep = jobs.JobsEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory(
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

    res = ep.poll("S1", "B1", interval=0.01, timeout=1.0)
    assert res.state == "COMPLETED"
    assert res.total_records is None
    assert res.failed_records is None
    dummy_client.get.assert_called_once_with("/api/v1/edc/studies/S1/jobs/B1")

    dummy_client.get.reset_mock()
    summary = ep.poll_many("S1", ["B1"], interval=0.01, timeout=1.0)
    assert summary.all_successful is True
    assert summary.any_failed is False
    dummy_client.get.assert_called_once_with("/api/v1/edc/studies/S1/jobs/B1")
