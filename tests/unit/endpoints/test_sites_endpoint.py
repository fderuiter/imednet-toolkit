"""Unit tests for sites endpoint."""

import pytest

from imednet.endpoints import sites
from imednet.errors import NotFoundError
from imednet.errors.validation import ConfigurationError
from imednet.models.sites import Site


def test_list_requires_study_key(dummy_client, paginator_factory, patch_build_filter):
    """Test that list requires study key."""
    ep = sites.SitesEndpoint(dummy_client)
    paginator_capture = paginator_factory(sites, [{"siteId": 1}])
    patch = patch_build_filter(sites)

    with pytest.raises(ConfigurationError):
        ep.list()

    result = ep.list(study_key="S1", status="ok")

    assert paginator_capture["path"] == "/api/v1/edc/studies/S1/sites"
    assert patch["filters"] == {"status": "ok"}
    assert paginator_capture["params"] == {"filter": "FILTERED"}
    assert isinstance(result[0], Site)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = sites.SitesEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(sites.SitesEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)
