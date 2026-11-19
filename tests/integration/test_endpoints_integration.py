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
    assert sent.url.params["filter"] == "status==Open;studyKey==ST"
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


@respx.mock
def test_endpoint_sort_forwarded_as_top_level_query_param():
    """Test that sort is forwarded as top-level params['sort'] rather than a filter term."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")

    studies_route = respx.get("https://api.test/api/v1/edc/studies").respond(
        json={"data": [{"studyKey": "S1"}]}
    )
    forms_route = respx.get("https://api.test/api/v1/edc/studies/ST/forms").respond(
        json={"data": [{"formId": 1, "formKey": "F_DM"}]}
    )
    records_route = respx.get("https://api.test/api/v1/edc/studies/ST/records").respond(
        json={"data": [{"recordId": 1}]}
    )
    users_route = respx.get("https://api.test/api/v1/edc/studies/ST/users").respond(
        json={"data": [{"userId": "u1"}]}
    )

    list(sdk.studies.list(sort="studyKey,asc"))
    studies_params = dict(studies_route.calls.last.request.url.params)
    assert studies_params["sort"] == "studyKey,asc"
    assert "filter" not in studies_params

    list(sdk.forms.list("ST", form_key="F_DM", sort="formKey,desc"))
    forms_params = dict(forms_route.calls.last.request.url.params)
    assert forms_params["sort"] == "formKey,desc"
    assert forms_params["filter"] == "formKey==F_DM"
    assert "sort" not in forms_params["filter"]

    list(
        sdk.records.list(
            "ST",
            status="Open",
            record_data_filter="V_AGE>=18",
            sort="recordId,desc",
        )
    )
    records_params = dict(records_route.calls.last.request.url.params)
    assert records_params["sort"] == "recordId,desc"
    assert records_params["recordDataFilter"] == "V_AGE>=18"
    assert records_params["filter"] == "status==Open;studyKey==ST"

    list(sdk.users.list("ST", include_inactive=True, sort="lastName,asc"))
    users_params = dict(users_route.calls.last.request.url.params)
    assert users_params["sort"] == "lastName,asc"
    assert users_params["includeInactive"] == "true"
    assert "filter" not in users_params


@respx.mock
def test_study_scoped_endpoint_repeated_equality_or_list():
    """Test that study-scoped endpoints send OR filter lists without mixing ';' and ','."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    route = respx.get("https://api.test/api/v1/edc/studies/ST/subjects").respond(
        json={"data": [{"subjectKey": "SUB1"}]}
    )

    subjects = list(sdk.subjects.list("ST", subject_status=["Screened", "Enrolled"]))
    params = dict(route.calls.last.request.url.params)
    assert params["filter"] == "subjectStatus==Screened,subjectStatus==Enrolled"
    assert len(subjects) == 1
