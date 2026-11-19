"""Tests for standardized list parameter processing and studyKey path-only defaults."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from imednet.core.paginator import BasePaginator
from imednet.errors import ConfigurationError
from imednet.sdk import AsyncImednetSDK, ImednetSDK

ALL_LIST_ENDPOINTS: list[tuple[str, str, bool, dict[str, Any]]] = [
    ("studies", "/api/v1/edc/studies", False, {"studyKey": "S1"}),
    ("sites", "/api/v1/edc/studies/S1/sites", True, {"siteId": 1}),
    ("subjects", "/api/v1/edc/studies/S1/subjects", True, {"subjectKey": "SUB1"}),
    ("forms", "/api/v1/edc/studies/S1/forms", True, {"formId": 1}),
    ("intervals", "/api/v1/edc/studies/S1/intervals", True, {"intervalId": 1}),
    ("variables", "/api/v1/edc/studies/S1/variables", True, {"variableId": 1}),
    ("visits", "/api/v1/edc/studies/S1/visits", True, {"visitId": 1}),
    ("records", "/api/v1/edc/studies/S1/records", True, {"recordId": 1}),
    ("record_revisions", "/api/v1/edc/studies/S1/recordRevisions", True, {"recordRevisionId": 1}),
    ("queries", "/api/v1/edc/studies/S1/queries", True, {"annotationId": 1}),
    ("codings", "/api/v1/edc/studies/S1/codings", True, {"codingId": 1}),
    ("users", "/api/v1/edc/studies/S1/users", True, {"userId": "u1"}),
]


@respx.mock
@pytest.mark.parametrize(
    ("attr_name", "expected_path", "needs_study", "sample_item"), ALL_LIST_ENDPOINTS
)
def test_sync_list_endpoints_emit_top_level_controls(
    attr_name: str,
    expected_path: str,
    needs_study: bool,
    sample_item: dict[str, Any],
) -> None:
    """Every list endpoint emits page, size, and sort as top-level query params."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    route = respx.get(f"https://api.test{expected_path}").respond(
        200,
        json={"data": [sample_item], "pagination": {"page": 1, "size": 25, "totalPages": 2}},
    )

    endpoint = getattr(sdk, attr_name)
    if needs_study:
        items = list(endpoint.list("S1", page=1, size=25, sort="dateModified,desc"))
    else:
        items = list(endpoint.list(page=1, size=25, sort="dateModified,desc"))

    assert len(items) == 1
    params = dict(route.calls.last.request.url.params)
    assert params["page"] == "1"
    assert params["size"] == "25"
    assert params["sort"] == "dateModified,desc"
    assert "filter" not in params


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attr_name", "expected_path", "needs_study", "sample_item"), ALL_LIST_ENDPOINTS
)
async def test_async_list_endpoints_emit_top_level_controls(
    attr_name: str,
    expected_path: str,
    needs_study: bool,
    sample_item: dict[str, Any],
) -> None:
    """Every async list endpoint emits page, size, and sort as top-level query params."""
    sdk = AsyncImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    route = respx.get(f"https://api.test{expected_path}").respond(
        200,
        json={"data": [sample_item], "pagination": {"page": 1, "size": 25, "totalPages": 2}},
    )

    try:
        endpoint = getattr(sdk, attr_name)
        if needs_study:
            items = [
                x async for x in endpoint.list("S1", page=1, size=25, sort="dateModified,desc")
            ]
        else:
            items = [x async for x in endpoint.list(page=1, size=25, sort="dateModified,desc")]
    finally:
        await sdk.aclose()

    assert len(items) == 1
    params = dict(route.calls.last.request.url.params)
    assert params["page"] == "1"
    assert params["size"] == "25"
    assert params["sort"] == "dateModified,desc"
    assert "filter" not in params


@respx.mock
@pytest.mark.parametrize(
    ("attr_name", "expected_path", "needs_study", "sample_item"), ALL_LIST_ENDPOINTS
)
def test_limit_sets_paginator_page_size_and_omits_from_filter(
    monkeypatch: pytest.MonkeyPatch,
    attr_name: str,
    expected_path: str,
    needs_study: bool,
    sample_item: dict[str, Any],
) -> None:
    """Passing limit=25 sets BasePaginator.page_size to 25 and omits limit from filter."""
    observed_page_sizes: list[int] = []
    orig_init = BasePaginator.__init__

    def spy_init(self: BasePaginator[Any], *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        observed_page_sizes.append(self.page_size)

    monkeypatch.setattr(BasePaginator, "__init__", spy_init)

    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    route = respx.get(f"https://api.test{expected_path}").respond(
        200,
        json={"data": [sample_item], "pagination": {"page": 0, "size": 25, "totalPages": 1}},
    )

    endpoint = getattr(sdk, attr_name)
    items = list(endpoint.list("S1", limit=25)) if needs_study else list(endpoint.list(limit=25))

    assert len(items) == 1
    assert observed_page_sizes == [25]
    params = dict(route.calls.last.request.url.params)
    assert params["size"] == "25"
    assert "limit" not in params
    assert "limit" not in params.get("filter", "")


@respx.mock
@pytest.mark.parametrize(
    ("attr_name", "expected_path", "sample_item"),
    [
        ("subjects", "/api/v1/edc/studies/S1/subjects", {"subjectKey": "SUB1"}),
        ("visits", "/api/v1/edc/studies/S1/visits", {"visitId": 1}),
        ("records", "/api/v1/edc/studies/S1/records", {"recordId": 1}),
        ("record_revisions", "/api/v1/edc/studies/S1/recordRevisions", {"recordRevisionId": 1}),
        ("queries", "/api/v1/edc/studies/S1/queries", {"annotationId": 1}),
    ],
)
def test_clinical_conduct_endpoints_support_multi_value_or_filters(
    attr_name: str,
    expected_path: str,
    sample_item: dict[str, Any],
) -> None:
    """All five clinical conduct endpoints support multi-value OR filters without studyKey conflicts."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    route = respx.get(f"https://api.test{expected_path}").respond(
        200,
        json={"data": [sample_item], "pagination": {"page": 0, "size": 100, "totalPages": 1}},
    )

    endpoint = getattr(sdk, attr_name)
    items = list(endpoint.list("S1", subject_key=["SUB1", "SUB2"]))

    assert len(items) == 1
    params = dict(route.calls.last.request.url.params)
    assert params["filter"] == "subjectKey==SUB1,subjectKey==SUB2"
    assert "studyKey" not in params["filter"]


@respx.mock
def test_caller_filter_combines_with_attribute_filters_and_validates_syntax() -> None:
    """Caller filter strings combine with attribute filters via ';' and validate syntax."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    route = respx.get("https://api.test/api/v1/edc/studies/S1/records").respond(
        200,
        json={"data": [{"recordId": 1}], "pagination": {"page": 0, "size": 100, "totalPages": 1}},
    )

    items = list(
        sdk.records.list(
            "S1",
            filter='dateModified>="2026-01-01T00:00:00Z"',
            form_key="DEMOG",
        )
    )
    assert len(items) == 1
    assert (
        route.calls.last.request.url.params["filter"]
        == 'dateModified>="2026-01-01T00:00:00Z";formKey==DEMOG'
    )

    # Invalid filter syntax raises ValueError (unquoted whitespace and unclosed quotes)
    with pytest.raises(ValueError, match="contains whitespace"):
        list(sdk.records.list("S1", filter="status == Open"))
    with pytest.raises(ValueError, match="unclosed quotation marks"):
        list(sdk.records.list("S1", filter='siteName=="unclosed'))

    # Mixing OR caller filter with AND attribute filter raises ValueError
    with pytest.raises(ValueError, match="cannot combine both ';' \\(AND\\) and ',' \\(OR\\)"):
        list(sdk.records.list("S1", filter="subjectKey==SUB1,subjectKey==SUB2", form_key="DEMOG"))


@respx.mock
def test_record_data_filter_accepted_on_records_and_rejected_elsewhere() -> None:
    """RecordsEndpoint accepts both recordDataFilter and record_data_filter; others raise ConfigurationError."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    route = respx.get("https://api.test/api/v1/edc/studies/S1/records").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "data": [{"recordId": 1}],
                "pagination": {"page": 0, "size": 100, "totalPages": 1},
            },
        )
    )

    list(sdk.records.list("S1", recordDataFilter="V_AGE>30"))
    params_camel = dict(route.calls.last.request.url.params)
    assert params_camel["recordDataFilter"] == "V_AGE>30"
    assert "filter" not in params_camel

    list(sdk.records.list("S1", record_data_filter="V_AGE>30"))
    params_snake = dict(route.calls.last.request.url.params)
    assert params_snake["recordDataFilter"] == "V_AGE>30"
    assert "filter" not in params_snake

    with pytest.raises(ConfigurationError, match="Conflicting values"):
        list(sdk.records.list("S1", record_data_filter="V_AGE>30", recordDataFilter="V_AGE>40"))

    non_record_endpoints = [
        ("studies", False),
        ("sites", True),
        ("subjects", True),
        ("forms", True),
        ("intervals", True),
        ("variables", True),
        ("visits", True),
        ("record_revisions", True),
        ("queries", True),
        ("codings", True),
        ("users", True),
    ]
    for attr_name, needs_study in non_record_endpoints:
        endpoint = getattr(sdk, attr_name)
        with pytest.raises(ConfigurationError):
            if needs_study:
                list(endpoint.list("S1", record_data_filter="V_AGE>30"))
            else:
                list(endpoint.list(record_data_filter="V_AGE>30"))
        with pytest.raises(ConfigurationError):
            if needs_study:
                list(endpoint.list("S1", recordDataFilter="V_AGE>30"))
            else:
                list(endpoint.list(recordDataFilter="V_AGE>30"))


def test_users_endpoint_still_rejects_filter_keys() -> None:
    """UsersEndpoint continues to reject filter strings and unsupported filter attributes."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    with pytest.raises(ConfigurationError, match="does not support query filtering"):
        list(sdk.users.list("S1", filter="active==true"))
    with pytest.raises(ConfigurationError, match="does not support query filtering"):
        list(sdk.users.list("S1", last_name="Doe"))
