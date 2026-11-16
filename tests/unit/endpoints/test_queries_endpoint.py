"""Unit tests for queries endpoint."""

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
    assert patch["filters"] == {"status": "new", "studyKey": "S1"}
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
