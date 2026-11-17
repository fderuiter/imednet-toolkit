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
        status="new",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/visits"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "new", "studyKey": "S1"}
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
    paginator_factory(visits, [{"visitId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)

    with pytest.raises(ImportError, match="pandas is required for visits_to_dataframe"):
        ep.to_dataframe("S1")


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
        status="new",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/visits"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "new", "studyKey": "S1"}
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
    async_paginator_factory(visits, [{"visitId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)

    with pytest.raises(ImportError, match="pandas is required for visits_to_dataframe"):
        await ep.to_dataframe("S1")
