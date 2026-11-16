"""Unit tests for intervals endpoint."""

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
