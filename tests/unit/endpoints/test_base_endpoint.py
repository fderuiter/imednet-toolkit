"""Unit tests for base endpoint."""

import inspect
from unittest.mock import MagicMock

import pytest

from imednet.core.client import Client
from imednet.core.context import set_study_context
from imednet.core.endpoint.base import (
    AsyncListEndpoint,
    GenericEndpoint,
    SyncListEndpoint,
)
from imednet.core.endpoint.edc_mixin import (
    EdcAsyncGetEndpoint,
    EdcAsyncListEndpoint,
    EdcSyncGetEndpoint,
    EdcSyncListEndpoint,
    EdcSyncListGetEndpoint,
)
from imednet.endpoints import (
    forms,
    intervals,
    jobs,
    queries,
    records,
    sites,
    subjects,
    variables,
    visits,
)
from imednet.models.base import ImednetBaseModel


class MockModel(ImednetBaseModel):
    """Test suite for MockModel."""

    item_id: int | None = None
    label: str | None = None


class MockGenericEndpoint(GenericEndpoint[MockModel]):
    """Concrete mock endpoint subclass inheriting directly from GenericEndpoint."""

    PATH = "/generic"
    MODEL = MockModel


class MockEndpointImpl(EdcSyncGetEndpoint[MockModel]):
    """Test suite for MockEndpointImpl."""

    PATH = "/test"
    MODEL = MockModel

    def __init__(self, client=None, async_client=None):
        """Initialize the test object."""
        super().__init__(client=client)
        if async_client is not None:
            self._async_client = async_client


class MockListGetEndpoint(EdcSyncListGetEndpoint[MockModel]):
    """Test suite for MockListGetEndpoint."""

    PATH = "/items"
    MODEL = MockModel
    _id_param = "recordId"


class MockListEndpoint(EdcSyncListEndpoint[MockModel]):
    """Test suite for MockListEndpoint."""

    PATH = "/list-items"
    MODEL = MockModel


class MockAsyncListEndpoint(EdcAsyncListEndpoint[MockModel]):
    """Test suite for MockAsyncListEndpoint."""

    PATH = "/async-list-items"
    MODEL = MockModel


class MockBaseSyncListEndpoint(SyncListEndpoint[MockModel]):
    """Test suite for MockBaseSyncListEndpoint."""

    PATH = "/base-list-items"
    MODEL = MockModel


class MockBaseAsyncListEndpoint(AsyncListEndpoint[MockModel]):
    """Test suite for MockBaseAsyncListEndpoint."""

    PATH = "/base-async-list-items"
    MODEL = MockModel


class TestBaseEndpoint:
    """Test suite for BaseEndpoint."""

    @pytest.fixture
    def client(self):
        """Helper function to client."""
        return MagicMock(spec=Client)

    def test_require_async_client_raises(self, client):
        """Test that require async client raises."""
        ep = MockEndpointImpl(client, async_client=None)
        with pytest.raises(RuntimeError, match="Async client not configured"):
            ep._require_async_client()

    def test_auto_filter_injects_study_key(self, client):
        """Test that auto filter injects study key."""
        set_study_context("DEFAULT")
        ep = MockEndpointImpl(client)

        filters = {"foo": "bar"}
        result = ep._auto_filter(filters)
        assert result["studyKey"] == "DEFAULT"
        assert result["foo"] == "bar"

    def test_auto_filter_preserves_existing_study_key(self, client):
        """Test that auto filter preserves existing study key."""
        set_study_context("DEFAULT")
        ep = MockEndpointImpl(client)

        filters = {"studyKey": "EXPLICIT", "foo": "bar"}
        result = ep._auto_filter(filters)
        assert result["studyKey"] == "EXPLICIT"

    def test_get_signature_is_explicit(self, client):
        """Test that get signature is explicit."""
        ep = MockListGetEndpoint(client)
        signature = inspect.signature(ep.get)

        assert list(signature.parameters.keys()) == ["self", "study_key", "item_id"]

    def test_get_requires_item_id(self, client):
        """Test that get requires item id."""
        ep = MockListGetEndpoint(client)

        with pytest.raises(TypeError, match="Missing required argument: item_id"):
            ep.get("S1", None)

    def test_ctx_parameter_raises_type_error(self, client):
        """Test that passing ctx to GenericEndpoint or derived endpoints raises TypeError."""
        with pytest.raises(TypeError, match="unexpected keyword argument 'ctx'"):
            MockGenericEndpoint(client, ctx=object())
        with pytest.raises(
            TypeError, match=r"takes from 1 to 3 positional arguments but 4 were given"
        ):
            MockGenericEndpoint(client, None, object())
        with pytest.raises(TypeError, match="unexpected keyword argument 'ctx'"):
            MockListGetEndpoint(client, ctx=object())
        with pytest.raises(TypeError, match=r"takes 2 positional arguments but 3 were given"):
            MockListGetEndpoint(client, object())

    def test_list_endpoint_has_no_get(self, client):
        """Test that EdcSyncListEndpoint and SyncListEndpoint have list and no get method."""
        ep = MockListEndpoint(client)
        assert hasattr(ep, "list")
        assert not hasattr(ep, "get")

        base_ep = MockBaseSyncListEndpoint(client)
        assert hasattr(base_ep, "list")
        assert not hasattr(base_ep, "get")

    def test_async_list_endpoint_has_no_get(self):
        """Test that EdcAsyncListEndpoint and AsyncListEndpoint have list and no get method."""
        async_client = MagicMock()
        ep = MockAsyncListEndpoint(async_client=async_client)
        assert hasattr(ep, "list")
        assert not hasattr(ep, "get")

        base_ep = MockBaseAsyncListEndpoint(async_client=async_client)
        assert hasattr(base_ep, "list")
        assert not hasattr(base_ep, "get")

    def test_sync_list_endpoint_to_dataframe(self, monkeypatch, client):
        """Test that EdcSyncListEndpoint provides a default synchronous to_dataframe method."""
        ep = MockListEndpoint(client)
        called = {}

        def fake_list(*, study_key=None, **filters):
            called["study_key"] = study_key
            called["filters"] = filters
            return iter([MockModel(item_id=1, label="Alpha")])

        monkeypatch.setattr(ep, "list", fake_list)

        df = ep.to_dataframe(study_key="S1", status="active")

        assert called == {"study_key": "S1", "filters": {"status": "active"}}
        assert len(df) == 1
        assert df.loc[0, "item_id"] == 1
        assert df.loc[0, "label"] == "Alpha"

    def test_sync_list_endpoint_to_dataframe_missing_pandas(self, monkeypatch, client):
        """Test that EdcSyncListEndpoint.to_dataframe raises ImportError before calling list."""
        ep = MockListEndpoint(client)
        monkeypatch.setattr("imednet.utils.pandas.pd", None)
        mock_list = MagicMock()
        monkeypatch.setattr(ep, "list", mock_list)

        with pytest.raises(ImportError, match="pandas is required for list-items_to_dataframe"):
            ep.to_dataframe("S1")

        mock_list.assert_not_called()

    def test_sync_list_endpoint_to_dataframe_empty_path_feature_name(self, monkeypatch, client):
        """Test that an empty PATH uses 'to_dataframe' in missing-pandas ImportError."""

        class EmptyPathEndpoint(EdcSyncListEndpoint[MockModel]):
            PATH = ""
            MODEL = MockModel

        ep = EmptyPathEndpoint(client)
        monkeypatch.setattr("imednet.utils.pandas.pd", None)

        with pytest.raises(ImportError, match="pandas is required for to_dataframe"):
            ep.to_dataframe()

    @pytest.mark.asyncio
    async def test_async_list_endpoint_to_dataframe(self, monkeypatch):
        """Test that EdcAsyncListEndpoint provides a default asynchronous to_dataframe method."""
        async_client = MagicMock()
        ep = MockAsyncListEndpoint(async_client=async_client)
        called = {}

        async def fake_gen():
            yield MockModel(item_id=2, label="Beta")

        def fake_list(*, study_key=None, **filters):
            called["study_key"] = study_key
            called["filters"] = filters
            return fake_gen()

        monkeypatch.setattr(ep, "list", fake_list)

        df = await ep.to_dataframe(study_key="S1", status="open")

        assert called == {"study_key": "S1", "filters": {"status": "open"}}
        assert len(df) == 1
        assert df.loc[0, "item_id"] == 2
        assert df.loc[0, "label"] == "Beta"

    @pytest.mark.asyncio
    async def test_async_list_endpoint_to_dataframe_missing_pandas(self, monkeypatch):
        """Test that EdcAsyncListEndpoint.to_dataframe raises ImportError before calling list."""
        async_client = MagicMock()
        ep = MockAsyncListEndpoint(async_client=async_client)
        monkeypatch.setattr("imednet.utils.pandas.pd", None)
        mock_list = MagicMock()
        monkeypatch.setattr(ep, "list", mock_list)

        with pytest.raises(
            ImportError, match="pandas is required for async-list-items_to_dataframe"
        ):
            await ep.to_dataframe("S1")

        mock_list.assert_not_called()

    def test_non_list_endpoints_do_not_expose_to_dataframe(self, client):
        """Test that get-only and non-EDC generic endpoints do not expose to_dataframe."""
        assert not hasattr(EdcSyncGetEndpoint, "to_dataframe")
        assert not hasattr(EdcAsyncGetEndpoint, "to_dataframe")
        assert not hasattr(jobs.JobsEndpoint, "to_dataframe")
        assert not hasattr(jobs.AsyncJobsEndpoint, "to_dataframe")
        assert not hasattr(MockEndpointImpl(client), "to_dataframe")
        assert not hasattr(SyncListEndpoint, "to_dataframe")
        assert not hasattr(AsyncListEndpoint, "to_dataframe")

    def test_concrete_endpoints_inherit_to_dataframe_without_duplication(self):
        """Test that the 7 endpoint modules inherit to_dataframe and records overrides it."""
        inherited_sync = [
            forms.FormsEndpoint,
            intervals.IntervalsEndpoint,
            queries.QueriesEndpoint,
            sites.SitesEndpoint,
            subjects.SubjectsEndpoint,
            variables.VariablesEndpoint,
            visits.VisitsEndpoint,
        ]
        inherited_async = [
            forms.AsyncFormsEndpoint,
            intervals.AsyncIntervalsEndpoint,
            queries.AsyncQueriesEndpoint,
            sites.AsyncSitesEndpoint,
            subjects.AsyncSubjectsEndpoint,
            variables.AsyncVariablesEndpoint,
            visits.AsyncVisitsEndpoint,
        ]

        for sync_cls in inherited_sync:
            assert "to_dataframe" not in sync_cls.__dict__
            assert sync_cls.to_dataframe is EdcSyncListEndpoint.to_dataframe

        for async_cls in inherited_async:
            assert "to_dataframe" not in async_cls.__dict__
            assert async_cls.to_dataframe is EdcAsyncListEndpoint.to_dataframe

        assert "to_dataframe" in records.RecordsEndpoint.__dict__
        assert "to_dataframe" in records.AsyncRecordsEndpoint.__dict__
