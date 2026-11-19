"""Unit tests for endpoints integration."""

import httpx
import pytest
import respx

from imednet.sdk import AsyncImednetSDK, ImednetSDK


@respx.mock
def test_studies_list_pagination():
    """Test that studies list pagination."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    calls = []

    def responder(request):
        """Helper function to responder."""
        calls.append(dict(request.url.params))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [{"studyKey": "S1"}],
                    "pagination": {"totalPages": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"studyKey": "S2"}],
                "pagination": {"totalPages": 2},
            },
        )

    respx.get("https://api.test/api/v1/edc/studies").mock(side_effect=responder)

    studies = list(sdk.studies.list())

    assert [s.study_key for s in studies] == ["S1", "S2"]
    assert calls[0] == {"page": "0", "size": "100"}
    assert calls[1] == {"page": "1", "size": "100"}


@respx.mock
def test_records_list_filter_param():
    """Test that records list filter param."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")

    route = respx.get("https://api.test/api/v1/edc/studies/ST/records").respond(
        json={"data": [{"recordId": 1}]}
    )

    records = list(sdk.records.list("ST", status="Open"))

    sent = route.calls.last.request
    assert sent.url.params["filter"] == "status==Open"
    assert records[0].record_id == 1


@respx.mock
@pytest.mark.asyncio
async def test_async_endpoint_mirror():
    """Test that async endpoint mirror asynchronously."""
    sync_sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    async_sdk = AsyncImednetSDK(api_key="k", security_key="s", base_url="https://api.test")

    respx.get("https://api.test/api/v1/edc/studies/S1/sites").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"siteId": 1}]}),
            httpx.Response(200, json={"data": [{"siteId": 1}]}),
        ]
    )

    sync_res = list(sync_sdk.sites.list("S1"))
    async_res = [item async for item in async_sdk.sites.list("S1")]

    await async_sdk.aclose()

    assert [s.site_id for s in sync_res] == [1]
    assert [s.site_id for s in async_res] == [1]
    assert [s.site_id for s in sync_res] == [s.site_id for s in async_res]
