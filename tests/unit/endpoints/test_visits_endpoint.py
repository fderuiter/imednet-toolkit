"""Unit tests for visits endpoint."""

from unittest.mock import MagicMock

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import visits
from imednet.errors.validation import ConfigurationError
from imednet.models.visits import Visit


def test_list_filters_and_path(dummy_client, paginator_factory, patch_build_filter):
    """Test that list builds path and omits studyKey from filter."""
    set_study_context("S1")
    ep = visits.VisitsEndpoint(dummy_client)
    capture = paginator_factory(visits, [{"visitId": 1}])
    patch = patch_build_filter(visits)

    result = ep.list(intervalName="Baseline")

    assert capture["path"] == "/api/v1/edc/studies/S1/visits"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"intervalName": "Baseline"}
    assert isinstance(result[0], Visit)


def test_visits_endpoint_is_list_only_and_rejects_undocumented_filters(dummy_client):
    """Test that VisitsEndpoint exposes no get method and rejects visitId/status."""
    ep = visits.VisitsEndpoint(dummy_client)
    async_ep = visits.AsyncVisitsEndpoint(dummy_client)

    assert not hasattr(ep, "get")
    assert not hasattr(async_ep, "get")
    assert not hasattr(visits.VisitsOperationDef, "_id_param")

    with pytest.raises(ConfigurationError, match="visitId"):
        ep.list("S1", visitId=1)

    with pytest.raises(ConfigurationError, match="status"):
        ep.list("S1", status="open")


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried visits to a pandas DataFrame."""
    ep = visits.VisitsEndpoint(dummy_client)
    paginator_factory(visits, [{"visitId": 1, "subjectId": 101, "intervalId": 5}])

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "visit_id"] == 1
    assert df.loc[0, "subject_id"] == 101
    assert df.loc[0, "interval_id"] == 5


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory, patch_build_filter):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = visits.VisitsEndpoint(dummy_client)
    captured = paginator_factory(visits, [{"visitId": 1}])
    filter_capture = patch_build_filter(visits)

    df = ep.to_dataframe(
        study_key="S1",
        interval_name="Baseline",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/visits"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"interval_name": "Baseline"}
    assert len(df) == 1


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = visits.VisitsEndpoint(dummy_client)
    paginator_factory(visits, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = visits.VisitsEndpoint(dummy_client)
    captured = paginator_factory(visits, [{"visitId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for visits_to_dataframe"):
        ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, async_paginator_factory):
    """Test that async to_dataframe exports queried visits to a pandas DataFrame."""
    ep = visits.AsyncVisitsEndpoint(dummy_client)
    async_paginator_factory(visits, [{"visitId": 1, "subjectId": 101, "intervalId": 5}])

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "visit_id"] == 1
    assert df.loc[0, "subject_id"] == 101
    assert df.loc[0, "interval_id"] == 5


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(
    dummy_client, async_paginator_factory, patch_build_filter
):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = visits.AsyncVisitsEndpoint(dummy_client)
    captured = async_paginator_factory(visits, [{"visitId": 1}])
    filter_capture = patch_build_filter(visits)

    df = await ep.to_dataframe(
        study_key="S1",
        interval_name="Baseline",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/visits"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"interval_name": "Baseline"}
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = visits.AsyncVisitsEndpoint(dummy_client)
    async_paginator_factory(visits, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = visits.AsyncVisitsEndpoint(dummy_client)
    captured = async_paginator_factory(visits, [{"visitId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for visits_to_dataframe"):
        await ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0
