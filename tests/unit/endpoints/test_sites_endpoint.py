"""Unit tests for sites endpoint."""

import pytest
from unittest.mock import MagicMock

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


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried sites to a pandas DataFrame."""
    ep = sites.SitesEndpoint(dummy_client)
    paginator_factory(sites, [{"siteId": 1, "siteName": "General Hospital"}])

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "site_id"] == 1
    assert df.loc[0, "site_name"] == "General Hospital"


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory, patch_build_filter):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = sites.SitesEndpoint(dummy_client)
    captured = paginator_factory(sites, [{"siteId": 1}])
    filter_capture = patch_build_filter(sites)

    df = ep.to_dataframe(
        study_key="S1",
        status="active",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/sites"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "active"}
    assert len(df) == 1


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = sites.SitesEndpoint(dummy_client)
    paginator_factory(sites, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = sites.SitesEndpoint(dummy_client)
    captured = paginator_factory(sites, [{"siteId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for sites_to_dataframe"):
        ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, async_paginator_factory):
    """Test that async to_dataframe exports queried sites to a pandas DataFrame."""
    ep = sites.AsyncSitesEndpoint(dummy_client)
    async_paginator_factory(sites, [{"siteId": 1, "siteName": "General Hospital"}])

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "site_id"] == 1
    assert df.loc[0, "site_name"] == "General Hospital"


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(
    dummy_client, async_paginator_factory, patch_build_filter
):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = sites.AsyncSitesEndpoint(dummy_client)
    captured = async_paginator_factory(sites, [{"siteId": 1}])
    filter_capture = patch_build_filter(sites)

    df = await ep.to_dataframe(
        study_key="S1",
        status="active",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/sites"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "active"}
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = sites.AsyncSitesEndpoint(dummy_client)
    async_paginator_factory(sites, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = sites.AsyncSitesEndpoint(dummy_client)
    captured = async_paginator_factory(sites, [{"siteId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for sites_to_dataframe"):
        await ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0
