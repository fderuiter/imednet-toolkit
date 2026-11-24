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


def test_poll_and_poll_many_resolve_results_fetches_result_url_and_raises_on_failed_records() -> (
    None
):
    """Passing resolve_results=True to poll, poll_many, poll_job, or poll_jobs inspects resultUrl and raises JobFailedError."""
    import httpx

    from imednet import ImednetSDK
    from imednet.core.client import Client
    from imednet.utils.job_poller import JobFailedError

    result_hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/B-OK"):
            return httpx.Response(
                200,
                json={
                    "jobId": "J-OK",
                    "batchId": "B-OK",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": "/api/v1/edc/studies/S1/jobs/B-OK/results",
                },
            )
        if request.url.path.endswith("/jobs/B-OK/results"):
            result_hits.append("B-OK")
            return httpx.Response(200, json=[{"recordId": 1, "status": "SUCCESS"}])
        if request.url.path.endswith("/jobs/B-REC-FAIL"):
            return httpx.Response(
                200,
                json={
                    "jobId": "J-FAIL",
                    "batchId": "B-REC-FAIL",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": "/api/v1/edc/studies/S1/jobs/B-REC-FAIL/results",
                },
            )
        if request.url.path.endswith("/jobs/B-REC-FAIL/results"):
            result_hits.append("B-REC-FAIL")
            return httpx.Response(
                200,
                json=[
                    {"recordId": 1, "status": "SUCCESS"},
                    {
                        "recordId": 2,
                        "status": "FAILED",
                        "error": "Range error for ssn=123-45-6789 token=sec-999",
                    },
                ],
            )
        return httpx.Response(404)

    sdk = ImednetSDK(
        client=Client(
            api_key="sec-999",  # pragma: allowlist secret
            security_key="sec-s",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=httpx.MockTransport(handler),
        )
    )
    try:
        # Default resolve_results=False skips resultUrl
        default_status = sdk.poll_job("S1", "B-REC-FAIL", interval=0, timeout=2.0)
        assert default_status.state == "COMPLETED"
        assert result_hits == []

        # resolve_results=True succeeds when all records pass
        ok_status = sdk.poll_job("S1", "B-OK", interval=0, timeout=2.0, resolve_results=True)
        assert ok_status.total_records == 1
        assert ok_status.failed_records == 0
        assert result_hits == ["B-OK"]

        # resolve_results=True raises JobFailedError on failed records in poll & poll_job
        with pytest.raises(JobFailedError) as ep_exc:
            sdk.jobs.poll("S1", "B-REC-FAIL", interval=0, timeout=2.0, resolve_results=True)
        assert "123-45-6789" not in str(ep_exc.value)
        assert "sec-999" not in str(ep_exc.value)

        with pytest.raises(JobFailedError):
            sdk.poll_job("S1", "B-REC-FAIL", interval=0, timeout=2.0, resolve_results=True)

        # poll_many / poll_jobs with resolve_results=True records JobFailedError (and raises on fail_fast=True)
        many_summary = sdk.poll_jobs(
            "S1", ["B-OK", "B-REC-FAIL"], interval=0, timeout=2.0, resolve_results=True
        )
        assert many_summary.all_successful is False
        assert isinstance(many_summary.failures["B-REC-FAIL"], JobFailedError)

        with pytest.raises(JobFailedError):
            sdk.jobs.poll_many(
                "S1",
                ["B-REC-FAIL"],
                interval=0,
                timeout=2.0,
                fail_fast=True,
                resolve_results=True,
            )
        with pytest.raises(JobFailedError):
            sdk.poll_jobs(
                "S1",
                ["B-REC-FAIL"],
                interval=0,
                timeout=2.0,
                fail_fast=True,
                resolve_results=True,
            )
    finally:
        sdk.close()
