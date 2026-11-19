"""Unit tests for record revisions endpoint."""

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import record_revisions
from imednet.errors import NotFoundError
from imednet.models.record_revisions import RecordRevision


def test_list_uses_filters(dummy_client, paginator_factory, patch_build_filter):
    """Test that list uses filters."""
    set_study_context("S1")
    ep = record_revisions.RecordRevisionsEndpoint(dummy_client)
    capture = paginator_factory(record_revisions, [{"recordRevisionId": 1}])
    patch = patch_build_filter(record_revisions)

    result = ep.list(status="closed")

    assert capture["path"] == "/api/v1/edc/studies/S1/recordRevisions"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"status": "closed"}
    assert isinstance(result[0], RecordRevision)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = record_revisions.RecordRevisionsEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(record_revisions.RecordRevisionsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)
