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
        status="active",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/subjects"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"site_id": "101", "status": "active", "studyKey": "S1"}
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
    paginator_factory(subjects, [{"subjectKey": "SUB1"}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)

    with pytest.raises(ImportError, match="pandas is required for subjects_to_dataframe"):
        ep.to_dataframe("S1")


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
        status="active",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/subjects"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"site_id": "101", "status": "active", "studyKey": "S1"}
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
    async_paginator_factory(subjects, [{"subjectKey": "SUB1"}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)

    with pytest.raises(ImportError, match="pandas is required for subjects_to_dataframe"):
        await ep.to_dataframe("S1")
