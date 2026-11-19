"""Unit tests for async paginator."""

import pytest

from imednet.core.paginator import AsyncPaginator


class DummyAsyncClient:
    """Test suite for DummyAsyncClient."""

    def __init__(self, responses):
        """Initialize the test object."""
        self.responses = responses
        self.calls = []

    async def get(self, path, params=None):
        """Helper function to get."""
        self.calls.append({"path": path, "params": params})
        data = self.responses.pop(0)
        return type("Resp", (), {"json": lambda self, d=data: d})()


@pytest.mark.asyncio
async def test_iterates_pages():
    """Test that iterates pages asynchronously."""
    client = DummyAsyncClient(
        [
            {"data": [1], "pagination": {"totalPages": 2}},
            {"data": [2], "pagination": {"totalPages": 2}},
        ]
    )
    paginator = AsyncPaginator(client, "/items", page_size=10)

    items = []
    async for item in paginator:
        items.append(item)

    assert items == [1, 2]
    assert client.calls[0]["params"] == {"page": 0, "size": 10}
    assert client.calls[1]["params"] == {"page": 1, "size": 10}


@pytest.mark.asyncio
async def test_async_paginator_bounds_and_tracks_has_more() -> None:
    """AsyncPaginator enforces 1 <= size <= 500 and tracks has_more across page boundaries."""
    client = DummyAsyncClient(
        [
            {"data": [10, 20], "pagination": {"page": 0, "size": 500, "totalPages": 3}},
            {"data": [30], "pagination": {"page": 1, "size": 500, "totalPages": 2}},
        ]
    )
    paginator = AsyncPaginator(client, "/items", params={"size": 500}, page_size=100)
    assert paginator.page_size == 500
    assert paginator.has_more is False

    with pytest.raises(ValueError, match="between 1 and 500"):
        AsyncPaginator(client, "/items", page_size=0)
    with pytest.raises(ValueError, match="between 1 and 500"):
        AsyncPaginator(client, "/items", params={"size": 1200})

    ait = paginator.__aiter__()
    first = await ait.__anext__()
    assert first == 10
    # Mid-page: second item of page 0 is still buffered AND cursor points to page 1
    assert paginator.has_more is True
    assert len(client.calls) == 1
    assert client.calls[0]["params"] == {"page": 0, "size": 500}

    second = await ait.__anext__()
    assert second == 20
    # End of page 0, before fetching page 1: cursor is 1 so has_more remains True without 2nd call
    assert paginator.has_more is True
    assert len(client.calls) == 1

    third = await ait.__anext__()
    assert third == 30
    assert paginator.has_more is False
    with pytest.raises(StopAsyncIteration):
        await ait.__anext__()
    assert paginator.has_more is False
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_async_sdk_list_forwards_params_and_supports_bounded_consumption() -> None:
    """AsyncImednetSDK endpoints forward filter/sort/size/recordDataFilter and track has_more."""
    import httpx
    import respx

    from imednet import AsyncImednetSDK
    from imednet.errors.validation import ConfigurationError

    with respx.mock(assert_all_called=False) as router:
        records_route = router.get("https://api.test/api/v1/edc/studies/S1/records").mock(
            side_effect=lambda req: httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "recordId": int(req.url.params["page"]) * 2 + idx + 1,
                            "studyKey": "S1",
                            "subjectKey": f"SUB-{idx + 1}",
                            "formKey": "AE",
                        }
                        for idx in range(2)
                    ],
                    "pagination": {
                        "page": int(req.url.params["page"]),
                        "size": int(req.url.params["size"]),
                        "totalPages": 3,
                        "totalItems": 6,
                    },
                },
            )
        )
        users_route = router.get("https://api.test/api/v1/edc/studies/S1/users").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"userId": "u-1", "login": "clinician"}],
                    "pagination": {"page": 0, "size": 500, "totalPages": 1, "totalItems": 1},
                },
            )
        )

        async with AsyncImednetSDK(
            api_key="test-key",  # pragma: allowlist secret
            security_key="test-secret",  # pragma: allowlist secret
            base_url="https://api.test",
        ) as sdk:
            record_iter = sdk.records.list(
                "S1",
                filter="formKey==AE;recordId>=1",
                record_data_filter="AESER==Serious;AGE>=18",
                sort="dateCreated,desc",
                size=2,
            )
            bounded = [await record_iter.__anext__(), await record_iter.__anext__()]
            assert [r.record_id for r in bounded] == [1, 2]
            assert record_iter.has_more is True
            assert records_route.call_count == 1

            sent_params = dict(records_route.calls[0].request.url.params)
            assert sent_params["page"] == "0"
            assert sent_params["size"] == "2"
            assert sent_params["sort"] == "dateCreated,desc"
            assert sent_params["recordDataFilter"] == "AESER==Serious;AGE>=18"
            assert "formKey==AE" in sent_params["filter"]
            assert "recordId>=1" in sent_params["filter"]

            users = [
                u
                async for u in sdk.users.list(
                    "S1",
                    include_inactive=True,
                    sort="login,asc",
                    size=900,
                )
            ]
            assert len(users) == 1
            assert dict(users_route.calls[0].request.url.params) == {
                "includeInactive": "true",
                "sort": "login,asc",
                "size": "500",
                "page": "0",
            }

            with pytest.raises(ConfigurationError, match="does not support query filtering"):
                sdk.users.list("S1", login="clinician")
