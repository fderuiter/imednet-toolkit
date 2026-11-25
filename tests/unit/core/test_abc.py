"""Unit tests for abc."""

from typing import Any

import pytest

from imednet.core.endpoint.abc import EndpointABC
from imednet.models.base import ImednetBaseModel


class MockModel(ImednetBaseModel):
    """Test suite for MockModel."""

    id: int


class ConcreteEndpoint(EndpointABC[MockModel]):
    """Test suite for ConcreteEndpoint."""

    PATH = "mock"
    MODEL = MockModel

    def _build_path(self, *segments: Any) -> str:
        """Helper function to  build path."""
        return "/".join(["mock", *(str(s) for s in segments)])

    def _auto_filter(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Helper function to  auto filter."""
        return {"auto": True, **filters}


class UnimplementedEndpoint(EndpointABC[MockModel]):
    """Test suite for UnimplementedEndpoint."""


def test_endpoint_abc_properties():
    """Test that endpoint abc properties."""
    endpoint = ConcreteEndpoint()
    assert endpoint.PATH == "mock"
    assert MockModel == endpoint.MODEL
    assert endpoint.requires_study_key is True
    assert endpoint._id_param == "id"


def test_endpoint_abc_methods():
    """Test that endpoint abc methods."""
    endpoint = ConcreteEndpoint()
    assert endpoint._build_path(1, "test") == "mock/1/test"
    assert endpoint._auto_filter({"test": "value"}) == {"auto": True, "test": "value"}


def test_endpoint_abc_abstract_instantiation_fails():
    """Test that endpoint abc abstract instantiation fails."""
    with pytest.raises(TypeError) as exc_info:
        UnimplementedEndpoint()  # type: ignore[abstract]
    assert "Can't instantiate abstract class" in str(exc_info.value)


def test_endpoint_abc_pass_coverage():
    """Test that endpoint abc pass coverage."""
    # Hit the `pass` statements in abstract methods to ensure 100% coverage
    assert EndpointABC._build_path(None) is None  # type: ignore[arg-type]
    assert EndpointABC._auto_filter(None, {}) is None  # type: ignore[arg-type]
