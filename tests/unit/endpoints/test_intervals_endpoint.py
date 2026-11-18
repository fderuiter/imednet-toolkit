"""Unit tests for intervals endpoint."""

from unittest.mock import MagicMock

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import intervals
from imednet.errors import NotFoundError
from imednet.models.intervals import Interval


def test_list_uses_default_study_and_page_size(dummy_client, paginator_factory, patch_build_filter):
    """Test that list uses default study and page size."""
    set_study_context("S1")
    ep = intervals.IntervalsEndpoint(dummy_client)
    captured = paginator_factory(intervals, [{"intervalId": 1}])
    filter_capture = patch_build_filter(intervals)

    result = ep.list(status="x")

    assert captured["path"] == "/api/v1/edc/studies/S1/intervals"
    assert captured["page_size"] == 500
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "x"}
    assert isinstance(result[0], Interval)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = intervals.IntervalsEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(intervals.IntervalsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)


def test_list_makes_request_per_call(dummy_client, paginator_factory):
    """Test that list makes request per call."""
    ep = intervals.IntervalsEndpoint(dummy_client)
    capture = paginator_factory(intervals, [{"intervalId": 1}])

    ep.list(study_key="S1")
    ep.list(study_key="S1")

    assert capture["count"] == 2


def test_list_different_study_keys_make_separate_requests(dummy_client, paginator_factory):
    """Test that list different study keys make separate requests."""
    ep = intervals.IntervalsEndpoint(dummy_client)
    capture = paginator_factory(intervals, [{"intervalId": 1}])

    ep.list(study_key="S1")
    ep.list(study_key="S2")

    assert capture["count"] == 2


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried intervals to a pandas DataFrame."""
    ep = intervals.IntervalsEndpoint(dummy_client)
    paginator_factory(
        intervals,
        [
            {
                "intervalId": 1,
                "intervalName": "Baseline",
                "forms": [{"formId": 10, "formKey": "F1", "formName": "Form 1"}],
            }
        ],
    )

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "interval_id"] == 1
    assert df.loc[0, "interval_name"] == "Baseline"
    forms_list = df.loc[0, "forms"]
    assert isinstance(forms_list, list)
    assert len(forms_list) == 1
    assert forms_list[0]["form_id"] == 10


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory, patch_build_filter):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = intervals.IntervalsEndpoint(dummy_client)
    captured = paginator_factory(intervals, [{"intervalId": 1}])
    filter_capture = patch_build_filter(intervals)

    df = ep.to_dataframe(
        study_key="S1",
        status="new",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/intervals"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "new"}
    assert len(df) == 1


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = intervals.IntervalsEndpoint(dummy_client)
    paginator_factory(intervals, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = intervals.IntervalsEndpoint(dummy_client)
    captured = paginator_factory(intervals, [{"intervalId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for intervals_to_dataframe"):
        ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, async_paginator_factory):
    """Test that async to_dataframe exports queried intervals to a pandas DataFrame."""
    ep = intervals.AsyncIntervalsEndpoint(dummy_client)
    async_paginator_factory(
        intervals,
        [
            {
                "intervalId": 1,
                "intervalName": "Baseline",
                "forms": [{"formId": 10, "formKey": "F1", "formName": "Form 1"}],
            }
        ],
    )

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "interval_id"] == 1
    assert df.loc[0, "interval_name"] == "Baseline"
    forms_list = df.loc[0, "forms"]
    assert isinstance(forms_list, list)
    assert len(forms_list) == 1
    assert forms_list[0]["form_id"] == 10


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(
    dummy_client, async_paginator_factory, patch_build_filter
):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = intervals.AsyncIntervalsEndpoint(dummy_client)
    captured = async_paginator_factory(intervals, [{"intervalId": 1}])
    filter_capture = patch_build_filter(intervals)

    df = await ep.to_dataframe(
        study_key="S1",
        status="new",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/intervals"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "new"}
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = intervals.AsyncIntervalsEndpoint(dummy_client)
    async_paginator_factory(intervals, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = intervals.AsyncIntervalsEndpoint(dummy_client)
    captured = async_paginator_factory(intervals, [{"intervalId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for intervals_to_dataframe"):
        await ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0
