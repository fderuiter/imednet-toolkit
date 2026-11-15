"""Unit tests for parsing mixin."""

from unittest.mock import MagicMock

from imednet.core.endpoint.base import GenericListGetEndpoint
from imednet.models.base import ImednetBaseModel


class MockModel(ImednetBaseModel):
    """Test suite for MockModel."""

    id: int


class EndpointWithParsing(GenericListGetEndpoint[MockModel]):
    """Test suite for EndpointWithParsing."""

    PATH = "items"
    MODEL = MockModel

    def _auto_filter(self, filters):
        """Helper function to  auto filter."""
        return filters


def test_parsing_mixin_parse_item():
    """Test that parsing mixin parse item."""
    endpoint = EndpointWithParsing(client=MagicMock())
    result = endpoint._parse_item({"id": 1})
    assert isinstance(result, MockModel)
    assert result.id == 1
