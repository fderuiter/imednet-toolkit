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
