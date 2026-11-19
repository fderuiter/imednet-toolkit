"""Unit tests for queries endpoint."""

from unittest.mock import MagicMock

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import queries
from imednet.errors import NotFoundError
from imednet.models.queries import Query


def test_list_builds_path_and_filters(dummy_client, paginator_factory, patch_build_filter):
    """Test that list builds path and filters."""
    set_study_context("S1")
    ep = queries.QueriesEndpoint(dummy_client)
    capture = paginator_factory(queries, [{"annotationId": 1}])
    patch = patch_build_filter(queries)

    result = ep.list(status="new")

    assert capture["path"] == "/api/v1/edc/studies/S1/queries"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"status": "new"}
    assert isinstance(result[0], Query)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = queries.QueriesEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(queries.QueriesEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried queries to a pandas DataFrame."""
    ep = queries.QueriesEndpoint(dummy_client)
    paginator_factory(queries, [{"annotationId": 1, "status": "OPEN"}])

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "status"] == "OPEN"


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory, patch_build_filter):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = queries.QueriesEndpoint(dummy_client)
    captured = paginator_factory(queries, [{"annotationId": 1}])
    filter_capture = patch_build_filter(queries)

    df = ep.to_dataframe(
        study_key="S1",
        status="OPEN",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/queries"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "OPEN"}
    assert len(df) == 1


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = queries.QueriesEndpoint(dummy_client)
    paginator_factory(queries, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = queries.QueriesEndpoint(dummy_client)
    captured = paginator_factory(queries, [{"annotationId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for queries_to_dataframe"):
        ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, async_paginator_factory):
    """Test that async to_dataframe exports queried queries to a pandas DataFrame."""
    ep = queries.AsyncQueriesEndpoint(dummy_client)
    async_paginator_factory(queries, [{"annotationId": 1, "status": "OPEN"}])

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "status"] == "OPEN"


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(
    dummy_client, async_paginator_factory, patch_build_filter
):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = queries.AsyncQueriesEndpoint(dummy_client)
    captured = async_paginator_factory(queries, [{"annotationId": 1}])
    filter_capture = patch_build_filter(queries)

    df = await ep.to_dataframe(
        study_key="S1",
        status="OPEN",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/queries"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "OPEN"}
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = queries.AsyncQueriesEndpoint(dummy_client)
    async_paginator_factory(queries, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = queries.AsyncQueriesEndpoint(dummy_client)
    captured = async_paginator_factory(queries, [{"annotationId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for queries_to_dataframe"):
        await ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0
