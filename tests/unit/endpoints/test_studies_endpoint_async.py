"""Unit tests for studies endpoint async."""

from unittest.mock import AsyncMock

import pytest

from imednet.endpoints import studies
from imednet.errors import NotFoundError
from imednet.models.studies import Study


@pytest.mark.asyncio
async def test_async_list_builds_path_and_filters(
    monkeypatch,
    dummy_client,
    async_paginator_factory,
    patch_build_filter,
):
    """Test that async list builds path and filters asynchronously."""
    ep = studies.AsyncStudiesEndpoint(dummy_client)
    captured = async_paginator_factory(studies, [{"studyKey": "S1"}])
    filter_capture = patch_build_filter(studies)

    result = await ep.list(study_type="LIVE")

    assert captured["path"] == "/api/v1/edc/studies"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"study_type": "LIVE"}
    assert isinstance(result[0], Study)


@pytest.mark.asyncio
async def test_async_get_single_arg_success(response_factory):
    """Test async single-argument study retrieval targets /api/v1/edc/studies/{studyKey}."""
    async_client = AsyncMock()
    async_client.get.return_value = response_factory({"studyKey": "S1", "studyName": "Demo Trial"})
    ep = studies.AsyncStudiesEndpoint(async_client)

    res = await ep.get("S1")

    async_client.get.assert_called_once_with("/api/v1/edc/studies/S1")
    assert isinstance(res, Study)
    assert res.study_key == "S1"
    assert res.study_name == "Demo Trial"


@pytest.mark.asyncio
async def test_async_get_legacy_two_arg_success(response_factory):
    """Test async legacy two-argument study retrieval targets /api/v1/edc/studies/{studyKey}."""
    async_client = AsyncMock()
    async_client.get.return_value = response_factory({"studyKey": "S1"})
    ep = studies.AsyncStudiesEndpoint(async_client)

    res = await ep.get(None, "S1")

    async_client.get.assert_called_once_with("/api/v1/edc/studies/S1")
    assert isinstance(res, Study)
    assert res.study_key == "S1"


@pytest.mark.asyncio
async def test_async_get_keyword_arguments_success(response_factory):
    """Test async keyword argument forms for backward compatibility."""
    async_client = AsyncMock()
    async_client.get.return_value = response_factory({"studyKey": "S1"})
    ep = studies.AsyncStudiesEndpoint(async_client)

    res1 = await ep.get(study_key="S1")
    assert res1.study_key == "S1"

    res2 = await ep.get(study_key=None, item_id="S1")
    assert res2.study_key == "S1"

    res3 = await ep.get(study_key="S1", item_id="S1")
    assert res3.study_key == "S1"

    res4 = await ep.get(item_id="S1")
    assert res4.study_key == "S1"

    res5 = await ep.get("S1", item_id="S1")
    assert res5.study_key == "S1"


@pytest.mark.asyncio
async def test_async_get_not_found(response_factory):
    """Test async get raises NotFoundError on empty payload."""
    async_client = AsyncMock()
    async_client.get.return_value = response_factory({})
    ep = studies.AsyncStudiesEndpoint(async_client)

    with pytest.raises(NotFoundError):
        await ep.get("missing")


@pytest.mark.asyncio
async def test_async_get_no_args_raises_type_error():
    """Test calling async get without arguments raises TypeError."""
    async_client = AsyncMock()
    ep = studies.AsyncStudiesEndpoint(async_client)
    with pytest.raises(TypeError):
        await ep.get()
