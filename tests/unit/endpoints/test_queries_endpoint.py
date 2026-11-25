"""Unit tests for queries endpoint."""

from unittest.mock import MagicMock

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import queries
from imednet.errors.validation import ConfigurationError
from imednet.models.queries import Query


def test_list_builds_path_and_filters(dummy_client, paginator_factory, patch_build_filter):
    """Test that list builds path, uses PAGE_SIZE=500, and omits studyKey from filter."""
    set_study_context("S1")
    ep = queries.QueriesEndpoint(dummy_client)
    capture = paginator_factory(queries, [{"annotationId": 1}])
    patch = patch_build_filter(queries)

    result = ep.list(variable="AETERM")

    assert queries.QueriesOperationDef.PAGE_SIZE == 500
    assert queries.QueriesEndpoint.PAGE_SIZE == 500
    assert queries.AsyncQueriesEndpoint.PAGE_SIZE == 500
    assert capture["path"] == "/api/v1/edc/studies/S1/queries"
    assert capture["page_size"] == 500
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"variable": "AETERM"}
    assert isinstance(result[0], Query)


def test_list_preserves_explicit_size_override(dummy_client):
    """Test that QueriesEndpoint defaults to size=500 and preserves explicit size overrides."""
    requested_params: list[dict[str, object]] = []

    def fake_get(path, params=None):
        assert path == "/api/v1/edc/studies/S1/queries"
        requested_params.append(dict(params or {}))
        return type(
            "Resp",
            (),
            {
                "json": lambda self: {
                    "data": [{"annotationId": 1, "status": "OPEN"}],
                    "pagination": {"totalPages": 1},
                }
            },
        )()

    dummy_client.get = fake_get
    ep = queries.QueriesEndpoint(dummy_client)

    default_items = list(ep.list("S1"))
    override_items = list(ep.list("S1", size=50))

    assert len(default_items) == 1
    assert len(override_items) == 1
    assert requested_params[0]["size"] == 500
    assert requested_params[1]["size"] == 50


def test_queries_endpoint_is_list_only_and_rejects_undocumented_filters(
    dummy_client, paginator_factory
):
    """Test that QueriesEndpoint exposes no get method, accepts annotationId, and rejects status."""
    ep = queries.QueriesEndpoint(dummy_client)
    async_ep = queries.AsyncQueriesEndpoint(dummy_client)

    assert not hasattr(ep, "get")
    assert not hasattr(async_ep, "get")
    assert not hasattr(queries.QueriesOperationDef, "_id_param")

    capture = paginator_factory(queries, [{"annotationId": 701, "status": "OPEN"}])
    assert len(ep.list("S1", annotationId=701)) == 1
    assert capture["params"]["filter"] == "annotationId==701"
    assert len(ep.list("S1", annotation_id=701)) == 1
    assert capture["params"]["filter"] == "annotationId==701"

    with pytest.raises(ConfigurationError, match="status"):
        ep.list("S1", status="OPEN")


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
        variable="AETERM",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/queries"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"variable": "AETERM"}
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
        variable="AETERM",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/queries"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"variable": "AETERM"}
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
