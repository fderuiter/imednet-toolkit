"""Unit tests for subjects endpoint."""

from unittest.mock import MagicMock

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import subjects
from imednet.errors import NotFoundError
from imednet.errors.validation import ConfigurationError
from imednet.models.subjects import Subject


def test_list_builds_path_with_default(dummy_client, paginator_factory, patch_build_filter):
    """Test that list builds path from study context without injecting studyKey into filter."""
    set_study_context("S1")
    ep = subjects.SubjectsEndpoint(dummy_client)
    capture = paginator_factory(subjects, [{"subjectKey": "x"}])
    patch = patch_build_filter(subjects)

    result = ep.list(subjectStatus="Enrolled")

    assert capture["path"] == "/api/v1/edc/studies/S1/subjects"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"subjectStatus": "Enrolled"}
    assert isinstance(result[0], Subject)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get raises NotFoundError when integer subjectId returns no items."""
    ep = subjects.SubjectsEndpoint(dummy_client)
    called = {}

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        called["filters"] = filters
        return []

    monkeypatch.setattr(subjects.SubjectsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 370)
    assert called["filters"] == {"subjectId": 370}


@pytest.mark.parametrize("invalid_id", ["SUB1", True, False, -1, 1.5])
def test_get_rejects_non_integer_subject_id(dummy_client, invalid_id):
    """Test that get validates non-negative integer subjectId inputs before dispatch."""
    ep = subjects.SubjectsEndpoint(dummy_client)
    with pytest.raises(ValueError, match="subjectId"):
        ep.get("S1", invalid_id)


def test_subjects_rejects_undocumented_filters(dummy_client):
    """Test that undocumented filter attributes raise ConfigurationError."""
    ep = subjects.SubjectsEndpoint(dummy_client)
    with pytest.raises(ConfigurationError, match="status"):
        ep.list("S1", status="active")


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried subjects to a pandas DataFrame."""
    ep = subjects.SubjectsEndpoint(dummy_client)
    paginator_factory(subjects, [{"subjectKey": "SUB1", "siteId": 10, "subjectId": 100}])

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "subject_key"] == "SUB1"
    assert df.loc[0, "site_id"] == 10
    assert df.loc[0, "subject_id"] == 100


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory, patch_build_filter):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = subjects.SubjectsEndpoint(dummy_client)
    captured = paginator_factory(subjects, [{"subjectKey": "SUB1"}])
    filter_capture = patch_build_filter(subjects)

    df = ep.to_dataframe(
        study_key="S1",
        site_id="101",
        subject_status="Enrolled",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/subjects"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"site_id": "101", "subject_status": "Enrolled"}
    assert len(df) == 1


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = subjects.SubjectsEndpoint(dummy_client)
    paginator_factory(subjects, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = subjects.SubjectsEndpoint(dummy_client)
    captured = paginator_factory(subjects, [{"subjectKey": "SUB1"}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for subjects_to_dataframe"):
        ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, async_paginator_factory):
    """Test that async to_dataframe exports queried subjects to a pandas DataFrame."""
    ep = subjects.AsyncSubjectsEndpoint(dummy_client)
    async_paginator_factory(subjects, [{"subjectKey": "SUB1", "siteId": 10, "subjectId": 100}])

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "subject_key"] == "SUB1"
    assert df.loc[0, "site_id"] == 10
    assert df.loc[0, "subject_id"] == 100


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(
    dummy_client, async_paginator_factory, patch_build_filter
):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = subjects.AsyncSubjectsEndpoint(dummy_client)
    captured = async_paginator_factory(subjects, [{"subjectKey": "SUB1"}])
    filter_capture = patch_build_filter(subjects)

    df = await ep.to_dataframe(
        study_key="S1",
        site_id="101",
        subject_status="Enrolled",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/subjects"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"site_id": "101", "subject_status": "Enrolled"}
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = subjects.AsyncSubjectsEndpoint(dummy_client)
    async_paginator_factory(subjects, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = subjects.AsyncSubjectsEndpoint(dummy_client)
    captured = async_paginator_factory(subjects, [{"subjectKey": "SUB1"}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for subjects_to_dataframe"):
        await ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0
