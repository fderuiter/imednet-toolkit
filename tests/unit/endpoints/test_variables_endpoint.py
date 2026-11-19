"""Unit tests for variables endpoint."""

from unittest.mock import MagicMock

import pytest

from imednet.endpoints import variables
from imednet.errors import NotFoundError
from imednet.errors.validation import ConfigurationError
from imednet.models.variables import Variable


def test_list_requires_study_key_page_size(dummy_client, paginator_factory, patch_build_filter):
    """Test that list requires study key page size."""
    ep = variables.VariablesEndpoint(dummy_client)
    capture = paginator_factory(variables, [{"variableId": 1}])
    patch = patch_build_filter(variables)

    with pytest.raises(ConfigurationError):
        ep.list()

    result = ep.list(study_key="S1", variable_name="x")

    assert capture["path"] == "/api/v1/edc/studies/S1/variables"
    assert capture["page_size"] == 500
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"variable_name": "x"}
    assert isinstance(result[0], Variable)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = variables.VariablesEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(variables.VariablesEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)


def test_list_makes_request_per_call(dummy_client, paginator_factory):
    """Test that list makes request per call."""
    ep = variables.VariablesEndpoint(dummy_client)
    capture = paginator_factory(variables, [{"variableId": 1}])

    ep.list(study_key="S1")
    ep.list(study_key="S1")

    assert capture["count"] == 2


def test_list_different_study_keys_make_separate_requests(dummy_client, paginator_factory):
    """Test that list different study keys make separate requests."""
    ep = variables.VariablesEndpoint(dummy_client)
    capture = paginator_factory(variables, [{"variableId": 1}])

    ep.list(study_key="S1")
    ep.list(study_key="S2")

    assert capture["count"] == 2


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried variables to a pandas DataFrame."""
    ep = variables.VariablesEndpoint(dummy_client)
    paginator_factory(variables, [{"variableId": 1, "label": "Age", "variableOid": "AGE"}])

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "variable_id"] == 1
    assert df.loc[0, "label"] == "Age"
    assert df.loc[0, "variable_oid"] == "AGE"


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory, patch_build_filter):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = variables.VariablesEndpoint(dummy_client)
    captured = paginator_factory(variables, [{"variableId": 1}])
    filter_capture = patch_build_filter(variables)

    df = ep.to_dataframe(
        study_key="S1",
        variable_name="x",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/variables"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"variable_name": "x"}
    assert len(df) == 1


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = variables.VariablesEndpoint(dummy_client)
    paginator_factory(variables, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = variables.VariablesEndpoint(dummy_client)
    captured = paginator_factory(variables, [{"variableId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for variables_to_dataframe"):
        ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, async_paginator_factory):
    """Test that async to_dataframe exports queried variables to a pandas DataFrame."""
    ep = variables.AsyncVariablesEndpoint(dummy_client)
    async_paginator_factory(variables, [{"variableId": 1, "label": "Age", "variableOid": "AGE"}])

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "variable_id"] == 1
    assert df.loc[0, "label"] == "Age"
    assert df.loc[0, "variable_oid"] == "AGE"


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(
    dummy_client, async_paginator_factory, patch_build_filter
):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = variables.AsyncVariablesEndpoint(dummy_client)
    captured = async_paginator_factory(variables, [{"variableId": 1}])
    filter_capture = patch_build_filter(variables)

    df = await ep.to_dataframe(
        study_key="S1",
        variable_name="x",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/variables"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"variable_name": "x"}
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = variables.AsyncVariablesEndpoint(dummy_client)
    async_paginator_factory(variables, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = variables.AsyncVariablesEndpoint(dummy_client)
    captured = async_paginator_factory(variables, [{"variableId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for variables_to_dataframe"):
        await ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0
