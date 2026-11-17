"""Unit tests for visits endpoint."""

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import visits
from imednet.errors import NotFoundError
from imednet.models.visits import Visit


def test_list_filters_and_path(dummy_client, paginator_factory, patch_build_filter):
    """Test that list filters and path."""
    set_study_context("S1")
    ep = visits.VisitsEndpoint(dummy_client)
    capture = paginator_factory(visits, [{"visitId": 1}])
    patch = patch_build_filter(visits)

    result = ep.list(status="x")

    assert capture["path"] == "/api/v1/edc/studies/S1/visits"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"status": "x", "studyKey": "S1"}
    assert isinstance(result[0], Visit)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = visits.VisitsEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(visits.VisitsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)
