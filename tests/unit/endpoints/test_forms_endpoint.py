"""Unit tests for forms endpoint."""

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import forms
from imednet.errors import NotFoundError
from imednet.errors.validation import ConfigurationError
from imednet.models.forms import Form


def test_list_requires_study_key_and_page_size(
    monkeypatch, dummy_client, paginator_factory, patch_build_filter
):
    """Test that list requires study key and page size."""
    ep = forms.FormsEndpoint(dummy_client)
    captured = paginator_factory(forms, [{"formId": 1}])
    filter_capture = patch_build_filter(forms)

    with pytest.raises(ConfigurationError):
        ep.list()

    set_study_context("S1")
    result = ep.list(status="new")

    assert captured["path"] == "/api/v1/edc/studies/S1/forms"
    assert captured["page_size"] == 500
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "new"}
    assert isinstance(result[0], Form)


def test_get_success(monkeypatch, dummy_client):
    """Test that get success."""
    ep = forms.FormsEndpoint(dummy_client)
    called = {}

    def fake_impl(self, client, paginator, *, study_key=None, **filters):
        """Helper function to fake impl."""
        called["study_key"] = study_key
        called["filters"] = filters
        return [Form(form_id=1)]

    monkeypatch.setattr(forms.FormsEndpoint, "_list_sync", fake_impl)

    res = ep.get("S1", 1)

    assert called == {"study_key": "S1", "filters": {"formId": 1}}
    assert isinstance(res, Form)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = forms.FormsEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(forms.FormsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)


def test_list_makes_request_per_call(dummy_client, paginator_factory):
    """Test that list makes request per call."""
    ep = forms.FormsEndpoint(dummy_client)
    capture = paginator_factory(forms, [{"formId": 1}])

    ep.list(study_key="S1")
    ep.list(study_key="S1")

    assert capture["count"] == 2


def test_list_different_study_keys_make_separate_requests(dummy_client, paginator_factory):
    """Test that list different study keys make separate requests."""
    ep = forms.FormsEndpoint(dummy_client)
    capture = paginator_factory(forms, [{"formId": 1}])

    ep.list(study_key="S1")
    ep.list(study_key="S2")

    assert capture["count"] == 2


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried forms to a pandas DataFrame."""
    ep = forms.FormsEndpoint(dummy_client)
    paginator_factory(forms, [{"formId": 1, "formName": "Demographics", "formKey": "DEMO"}])

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "form_id"] == 1
    assert df.loc[0, "form_name"] == "Demographics"
    assert df.loc[0, "form_key"] == "DEMO"


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory, patch_build_filter):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = forms.FormsEndpoint(dummy_client)
    captured = paginator_factory(forms, [{"formId": 1}])
    filter_capture = patch_build_filter(forms)

    df = ep.to_dataframe(
        study_key="S1",
        status="new",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/forms"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "new"}
    assert len(df) == 1


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = forms.FormsEndpoint(dummy_client)
    paginator_factory(forms, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = forms.FormsEndpoint(dummy_client)
    paginator_factory(forms, [{"formId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)

    with pytest.raises(ImportError, match="pandas is required for forms_to_dataframe"):
        ep.to_dataframe("S1")


@pytest.mark.asyncio
async def test_async_to_dataframe_success(dummy_client, async_paginator_factory):
    """Test that async to_dataframe exports queried forms to a pandas DataFrame."""
    ep = forms.AsyncFormsEndpoint(dummy_client)
    async_paginator_factory(forms, [{"formId": 1, "formName": "Demographics", "formKey": "DEMO"}])

    df = await ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "form_id"] == 1
    assert df.loc[0, "form_name"] == "Demographics"
    assert df.loc[0, "form_key"] == "DEMO"


@pytest.mark.asyncio
async def test_async_to_dataframe_parameter_forwarding(
    dummy_client, async_paginator_factory, patch_build_filter
):
    """Test that async to_dataframe forwards filters and study key to list."""
    ep = forms.AsyncFormsEndpoint(dummy_client)
    captured = async_paginator_factory(forms, [{"formId": 1}])
    filter_capture = patch_build_filter(forms)

    df = await ep.to_dataframe(
        study_key="S1",
        status="new",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/forms"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "new"}
    assert len(df) == 1


@pytest.mark.asyncio
async def test_async_to_dataframe_empty_results(dummy_client, async_paginator_factory):
    """Test that async to_dataframe handles empty query results."""
    ep = forms.AsyncFormsEndpoint(dummy_client)
    async_paginator_factory(forms, [])

    df = await ep.to_dataframe("S1")

    assert df.empty


@pytest.mark.asyncio
async def test_async_to_dataframe_missing_dependency(
    monkeypatch, dummy_client, async_paginator_factory
):
    """Test that async to_dataframe raises ImportError when pandas is not installed."""
    ep = forms.AsyncFormsEndpoint(dummy_client)
    async_paginator_factory(forms, [{"formId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)

    with pytest.raises(ImportError, match="pandas is required for forms_to_dataframe"):
        await ep.to_dataframe("S1")
