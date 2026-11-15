"""Unit tests for subjects endpoint."""

import pytest

from imednet.endpoints import subjects
from imednet.errors import NotFoundError
from imednet.models.subjects import Subject


def test_list_builds_path_with_default(
    dummy_client, context, paginator_factory, patch_build_filter
):
    """Test that list builds path with default."""
    context.set_default_study_key("S1")
    ep = subjects.SubjectsEndpoint(dummy_client)
    capture = paginator_factory(subjects, [{"subjectKey": "x"}])
    patch = patch_build_filter(subjects)

    result = ep.list()

    assert capture["path"] == "/api/v1/edc/studies/S1/subjects"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"studyKey": "S1"}
    assert isinstance(result[0], Subject)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = subjects.SubjectsEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(subjects.SubjectsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", "X")
