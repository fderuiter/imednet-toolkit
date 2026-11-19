"""Unit tests for uat inspector."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.sites import Site
from imednet.models.variables import Variable
from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet_workflows.uat import StudySchemaInspector, StudySnapshot


def test_study_snapshot_builds_indexes_and_filters() -> None:
    """Test that study snapshot builds indexes and filters."""
    enrollment = Form(form_key="ENR", form_type="Enrollment", form_name="Enroll")
    scheduled = Form(form_key="LABS", form_type="CRF", form_name="Labs")
    unscheduled = Form(
        form_key="UNS",
        form_type="CRF",
        form_name="Unscheduled",
        unscheduled_visit=True,
    )
    snapshot = StudySnapshot(
        study_key="ST",
        forms=[enrollment, scheduled, unscheduled],
        variables=[
            Variable(variable_id=1, variable_name="DOB", form_key="ENR", variable_type="Date"),
            Variable(variable_id=2, variable_name="HGB", form_key="LABS", variable_type="Number"),
        ],
        intervals=[Interval(interval_name="Baseline"), Interval(interval_name="Week 1")],
        sites=[
            Site(site_name="Main", site_enrollment_status="Active"),
            Site(site_name="Backup", site_enrollment_status="Inactive"),
        ],
    )

    assert snapshot.forms_by_key["ENR"] == enrollment
    assert [v.variable_name for v in snapshot.variables_by_form["ENR"]] == ["DOB"]
    assert snapshot.intervals_by_name["Baseline"].interval_name == "Baseline"
    assert snapshot.enrollment_forms() == [enrollment]
    assert snapshot.scheduled_forms() == [scheduled]
    assert snapshot.unscheduled_forms() == [unscheduled]
    assert [site.site_name for site in snapshot.active_sites()] == ["Main"]


def test_inspect_uses_cache_and_force_refresh() -> None:
    """Test that inspect uses cache and force refresh."""
    sdk = MagicMock()
    sdk.get_forms.return_value = [Form(form_key="F1", form_name="Form 1")]
    sdk.get_variables.return_value = [Variable(variable_name="V1", form_key="F1")]
    sdk.get_intervals.return_value = [Interval(interval_name="Baseline")]
    sdk.get_sites.return_value = [Site(site_name="Site A")]

    inspector = StudySchemaInspector(sdk)
    first = inspector.inspect("ST")
    second = inspector.inspect("ST")

    assert first is second
    sdk.get_forms.assert_called_once_with("ST")
    sdk.get_variables.assert_called_once_with("ST")
    sdk.get_intervals.assert_called_once_with("ST")
    sdk.get_sites.assert_called_once_with("ST")

    third = inspector.inspect("ST", force_refresh=True)
    assert third is not second
    assert sdk.get_forms.call_count == 2
    assert sdk.get_variables.call_count == 2
    assert sdk.get_intervals.call_count == 2
    assert sdk.get_sites.call_count == 2


def test_clear_cache_for_single_key_and_all_keys() -> None:
    """Test that clear cache for single key and all keys."""
    sdk = MagicMock()
    sdk.get_forms.return_value = []
    sdk.get_variables.return_value = []
    sdk.get_intervals.return_value = []
    sdk.get_sites.return_value = []

    inspector = StudySchemaInspector(sdk)
    inspector.inspect("S1")
    inspector.inspect("S2")

    inspector.clear_cache("S1")
    inspector.inspect("S1")
    assert sdk.get_forms.call_count == 3

    inspector.clear_cache()
    inspector.inspect("S1")
    inspector.inspect("S2")
    assert sdk.get_forms.call_count == 5


@pytest.mark.asyncio
async def test_async_inspect_fetches_all_endpoints_concurrently() -> None:
    """Test that async inspect fetches all endpoints concurrently asynchronously."""
    sdk = MagicMock()
    calls = {"forms": 0, "variables": 0, "intervals": 0, "sites": 0}
    started = 0
    release = asyncio.Event()

    async def delayed_result(name: str, payload: list[Any]) -> list[Any]:
        """Helper function to delayed result."""
        nonlocal started
        calls[name] += 1
        started += 1
        if started == 4:
            release.set()
        await release.wait()
        return payload

    sdk.async_get_forms = lambda study_key: delayed_result(
        "forms", [Form(form_key="F1", form_name="Form 1")]
    )
    sdk.async_get_variables = lambda study_key: delayed_result(
        "variables", [Variable(variable_name="V1", form_key="F1")]
    )
    sdk.async_get_intervals = lambda study_key: delayed_result(
        "intervals", [Interval(interval_name="Baseline")]
    )
    sdk.async_get_sites = lambda study_key: delayed_result("sites", [Site(site_name="Site A")])

    inspector = StudySchemaInspector(sdk)
    snapshot = await asyncio.wait_for(inspector.async_inspect("ST"), timeout=0.5)
    cached = await inspector.async_inspect("ST")

    assert snapshot is cached
    assert all(value == 1 for value in calls.values())


@pytest.mark.asyncio
async def test_async_inspect_force_refresh_bypasses_cache() -> None:
    """Test that async inspect force refresh bypasses cache asynchronously."""
    sdk = MagicMock()

    async def empty_async_gen(*args, **kwargs) -> list[Any]:
        """Helper function to empty async gen."""
        return []

    sdk.async_get_forms = MagicMock(side_effect=empty_async_gen)
    sdk.async_get_variables = MagicMock(side_effect=empty_async_gen)
    sdk.async_get_intervals = MagicMock(side_effect=empty_async_gen)
    sdk.async_get_sites = MagicMock(side_effect=empty_async_gen)

    inspector = StudySchemaInspector(sdk)
    await inspector.async_inspect("ST")
    await inspector.async_inspect("ST", force_refresh=True)

    assert sdk.async_get_forms.call_count == 2
    assert sdk.async_get_variables.call_count == 2
    assert sdk.async_get_intervals.call_count == 2
    assert sdk.async_get_sites.call_count == 2


def test_inspect_with_async_sdk_raises_type_error() -> None:
    """Test that inspect with async sdk raises type error."""
    sdk = AsyncImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    inspector = StudySchemaInspector(sdk)
    try:
        with pytest.raises(TypeError, match="AsyncImednetSDK"):
            inspector.inspect("ST")
        assert sdk.workflows is not None
        with pytest.raises(TypeError, match="AsyncImednetSDK"):
            sdk.workflows.uat_inspector.inspect("ST")
    finally:
        asyncio.run(sdk.aclose())


@pytest.mark.asyncio
async def test_async_inspect_with_sync_sdk_raises_type_error() -> None:
    """Test that async inspect with sync sdk raises type error asynchronously."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    inspector = StudySchemaInspector(sdk)
    with pytest.raises(TypeError, match="AsyncImednetSDK"):
        await inspector.async_inspect("ST")
    assert sdk.workflows is not None
    with pytest.raises(TypeError, match="AsyncImednetSDK"):
        await sdk.workflows.uat_inspector.async_inspect("ST")


def _form_payload(form_id: int) -> dict[str, object]:
    """Helper function to  form payload."""
    return {
        "studyKey": "ST",
        "formId": form_id,
        "formKey": f"FORM-{form_id}",
        "formName": f"Form {form_id}",
        "formType": "CRF",
    }


def _variable_payload(variable_id: int) -> dict[str, object]:
    """Helper function to  variable payload."""
    return {
        "studyKey": "ST",
        "variableId": variable_id,
        "variableName": f"VAR-{variable_id}",
        "variableType": "Text",
        "formId": variable_id,
        "formKey": f"FORM-{variable_id}",
        "formName": f"Form {variable_id}",
        "label": f"Label {variable_id}",
    }


@respx.mock
def test_inspect_consumes_paginated_forms_and_variables() -> None:
    """Test that inspect consumes paginated forms and variables."""
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
    forms_route = respx.get("https://api.test/api/v1/edc/studies/ST/forms").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [_form_payload(i) for i in range(1, 501)],
                    "pagination": {"totalPages": 2},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [_form_payload(501)],
                    "pagination": {"totalPages": 2},
                },
            ),
        ]
    )
    variables_route = respx.get("https://api.test/api/v1/edc/studies/ST/variables").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [_variable_payload(i) for i in range(1, 501)],
                    "pagination": {"totalPages": 2},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [_variable_payload(501)],
                    "pagination": {"totalPages": 2},
                },
            ),
        ]
    )
    respx.get("https://api.test/api/v1/edc/studies/ST/intervals").respond(
        json={"data": [{"intervalId": 1, "intervalName": "Baseline"}]}
    )
    respx.get("https://api.test/api/v1/edc/studies/ST/sites").respond(
        json={"data": [{"siteId": 1, "siteName": "Main", "siteEnrollmentStatus": "Active"}]}
    )

    snapshot = StudySchemaInspector(sdk).inspect("ST")

    assert len(snapshot.forms) == 501
    assert len(snapshot.variables) == 501
    assert forms_route.call_count == 2
    assert variables_route.call_count == 2
