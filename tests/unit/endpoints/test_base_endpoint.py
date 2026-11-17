"""Unit tests for base endpoint."""

import inspect
from unittest.mock import MagicMock

import pytest

from imednet.core.client import Client
from imednet.core.context import set_study_context
from imednet.core.endpoint.base import GenericEndpoint, GenericListGetEndpoint
from imednet.core.endpoint.edc_mixin import EdcEndpointMixin
from imednet.models.base import ImednetBaseModel


class MockModel(ImednetBaseModel):
    """Test suite for MockModel."""


class MockEndpointImpl(EdcEndpointMixin, GenericEndpoint[MockModel]):
    """Test suite for MockEndpointImpl."""

    PATH = "/test"
    MODEL = MockModel

    def __init__(self, client=None, async_client=None):
        """Initialize the test object."""
        super().__init__(client=client, async_client=async_client)


class MockListGetEndpoint(EdcEndpointMixin, GenericListGetEndpoint[MockModel]):
    """Test suite for MockListGetEndpoint."""

    PATH = "/items"
    MODEL = MockModel
    _id_param = "recordId"


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
        with pytest.raises(TypeError):
            GenericEndpoint(client, ctx=object())
        with pytest.raises(TypeError):
            GenericEndpoint(client, None, object())
        with pytest.raises(TypeError):
            MockListGetEndpoint(client, ctx=object())
        with pytest.raises(TypeError):
            MockListGetEndpoint(client, object())
