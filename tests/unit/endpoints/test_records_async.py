"""Unit tests for records async."""

from unittest.mock import AsyncMock

import pytest

from imednet.endpoints import records
from imednet.errors import ValidationError
from imednet.models.variables import Variable
from imednet.validation.cache import SchemaCache


@pytest.fixture
def schema():
    """Helper function to schema."""
    s = SchemaCache()
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    s._form_variables = {"F1": {"age": var}}
    s._form_id_to_key = {1: "F1"}
    return s


@pytest.mark.asyncio
async def test_async_create_validates_data(dummy_client, context, response_factory, schema):
    """Test that async create validates data asynchronously."""
    ep = records.AsyncRecordsEndpoint(dummy_client, context)
    dummy_client.post = AsyncMock(return_value=response_factory({"jobId": "1"}))

    # Validation error: unknown form
    with pytest.raises(ValidationError, match="Unknown form BAD"):
        await ep.create("S1", [{"formKey": "BAD", "data": {}}], schema=schema)
    dummy_client.post.assert_not_called()

    # Validation error: invalid data
    with pytest.raises(ValidationError):
        await ep.create("S1", [{"formKey": "F1", "data": {"bad": 1}}], schema=schema)
    dummy_client.post.assert_not_called()

    # Valid data
    await ep.create("S1", [{"formKey": "F1", "data": {"age": 5}}], schema=schema)
    dummy_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_async_create_validates_data_with_snake_case_keys(
    dummy_client, context, response_factory, schema
):
    """Test that async create validates data with snake case keys asynchronously."""
    ep = records.AsyncRecordsEndpoint(dummy_client, context)
    dummy_client.post = AsyncMock(return_value=response_factory({"jobId": "1"}))

    # Should raise validation error because "bad" is not in schema
    # Even if we use snake_case "form_key"
    with pytest.raises(ValidationError):
        await ep.create("S1", [{"form_key": "F1", "data": {"bad": 1}}], schema=schema)


@pytest.mark.asyncio
async def test_async_create_resolves_form_id(dummy_client, context, response_factory, schema):
    """Test that async create resolves form id asynchronously."""
    ep = records.AsyncRecordsEndpoint(dummy_client, context)
    dummy_client.post = AsyncMock(return_value=response_factory({"jobId": "1"}))

    # Valid data using formId instead of formKey
    await ep.create("S1", [{"formId": 1, "data": {"age": 5}}], schema=schema)
    dummy_client.post.assert_called_once()

    # reset mock
    dummy_client.post.reset_mock()

    # Valid data using form_id (snake case)
    await ep.create("S1", [{"form_id": 1, "data": {"age": 5}}], schema=schema)
    dummy_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, context, async_paginator_factory):
    """Test that async to_dataframe exports queried records to a pandas DataFrame."""
    ep = records.AsyncRecordsEndpoint(dummy_client, context)
    async_paginator_factory(records, [{"recordId": 1, "recordData": {"age": 25}}])

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "record_id"] == 1
    assert df.loc[0, "record_data"] == {"age": 25}


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(
    dummy_client, context, async_paginator_factory, patch_build_filter
):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = records.AsyncRecordsEndpoint(dummy_client, context)
    captured = async_paginator_factory(records, [{"recordId": 1}])
    filter_capture = patch_build_filter(records)

    df = await ep.to_dataframe(
        study_key="S1",
        record_data_filter="age>10",
        status="open",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/records"
    assert captured["params"] == {"filter": "FILTERED", "recordDataFilter": "age>10"}
    assert filter_capture["filters"] == {"status": "open", "studyKey": "S1"}
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_flatten_option(dummy_client, context, async_paginator_factory):
    """Test that async to_dataframe supports flatten option."""
    ep = records.AsyncRecordsEndpoint(dummy_client, context)
    async_paginator_factory(records, [{"recordId": 1, "recordData": {"age": 25}}])

    df = await ep.to_dataframe("S1", flatten=True)

    assert len(df) == 1
    assert "record_data" not in df.columns
    assert df.loc[0, "age"] == 25


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, context, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = records.AsyncRecordsEndpoint(dummy_client, context)
    async_paginator_factory(records, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, context, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = records.AsyncRecordsEndpoint(dummy_client, context)
    async_paginator_factory(records, [{"recordId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)

    with pytest.raises(ImportError, match="pandas is required for records_to_dataframe"):
        await ep.to_dataframe("S1")
