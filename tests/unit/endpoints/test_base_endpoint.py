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
    EdcAsyncListEndpoint,
    EdcSyncGetEndpoint,
    EdcSyncListEndpoint,
    EdcSyncListGetEndpoint,
)
from imednet.models.base import ImednetBaseModel


class MockModel(ImednetBaseModel):
    """Test suite for MockModel."""


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
