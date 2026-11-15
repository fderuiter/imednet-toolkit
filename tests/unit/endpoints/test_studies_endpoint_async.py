"""Unit tests for studies endpoint async."""

import pytest

from imednet.endpoints import studies
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

    result = await ep.list(status="active")

    assert captured["path"] == "/api/v1/edc/studies"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "active"}
    assert isinstance(result[0], Study)
