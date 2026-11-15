"""Unit tests for studies endpoint."""

import pytest

from imednet.endpoints import studies
from imednet.errors import NotFoundError
from imednet.models.studies import Study


def test_list_builds_path_and_filters(
    monkeypatch, dummy_client, paginator_factory, patch_build_filter
):
    """Test that list builds path and filters."""
    ep = studies.StudiesEndpoint(dummy_client)
    captured = paginator_factory(studies, [{"studyKey": "S1"}])
    filter_capture = patch_build_filter(studies)

    result = ep.list(status="active")

    assert captured["path"] == "/api/v1/edc/studies"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "active"}
    assert isinstance(result[0], Study)


def test_get_success(monkeypatch, dummy_client, paginator_factory, patch_build_filter):
    """Test that get success."""
    ep = studies.StudiesEndpoint(dummy_client)
    captured = paginator_factory(studies, [{"studyKey": "S1"}])
    filter_capture = patch_build_filter(studies)

    res = ep.get(None, "S1")

    assert captured["path"] == "/api/v1/edc/studies"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"studyKey": "S1"}
    assert isinstance(res, Study)


def test_get_not_found(monkeypatch, dummy_client, paginator_factory):
    """Test that get not found."""
    ep = studies.StudiesEndpoint(dummy_client)
    paginator_factory(studies, [])
    with pytest.raises(NotFoundError):
        ep.get(None, "missing")


def test_list_each_call_makes_request(dummy_client, paginator_factory):
    """Test that list each call makes request."""
    ep = studies.StudiesEndpoint(dummy_client)
    captured = paginator_factory(studies, [{"studyKey": "S1"}])

    ep.list()
    ep.list()

    assert captured["count"] == 2
