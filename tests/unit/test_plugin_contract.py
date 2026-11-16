"""Tests for the plugin contract: PluginProtocol, WorkflowsNamespaceProtocol, PluginLoadError."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from imednet.errors import PluginLoadError
from imednet.plugins import PluginProtocol, WorkflowsNamespaceProtocol
from imednet.sdk import _BaseSDK

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ValidNamespace:
    """A minimal object satisfying WorkflowsNamespaceProtocol."""

    data_extraction: Any = None
    duckdb_centralizer: Any = None
    query_management: Any = None
    record_mapper: Any = None
    record_update: Any = None
    register_subjects: Any = None
    subject_data: Any = None
    uat: Any = None
    uat_inspector: Any = None


def _valid_factory(sdk_instance: Any) -> _ValidNamespace:
    """Helper function to  valid factory."""
    return _ValidNamespace()


# ---------------------------------------------------------------------------
# WorkflowsNamespaceProtocol runtime check
# ---------------------------------------------------------------------------


def test_valid_namespace_satisfies_protocol() -> None:
    """Test that valid namespace satisfies protocol."""
    assert isinstance(_ValidNamespace(), WorkflowsNamespaceProtocol)


def test_object_missing_attributes_does_not_satisfy_namespace_protocol() -> None:
    """Test that object missing attributes does not satisfy namespace protocol."""

    class _Incomplete:
        """Test suite for  Incomplete."""

        data_extraction: Any = None
        # missing: duckdb_centralizer, query_management, record_mapper, record_update, register_subjects, subject_data, uat, uat_inspector

    assert not isinstance(_Incomplete(), WorkflowsNamespaceProtocol)


# ---------------------------------------------------------------------------
# PluginProtocol runtime check
# ---------------------------------------------------------------------------


def test_callable_factory_satisfies_plugin_protocol() -> None:
    """Test that callable factory satisfies plugin protocol."""
    assert isinstance(_valid_factory, PluginProtocol)


def test_non_callable_does_not_satisfy_plugin_protocol() -> None:
    """Test that non callable does not satisfy plugin protocol."""
    assert not isinstance("not-a-callable", PluginProtocol)


# ---------------------------------------------------------------------------
# PluginLoadError is a subclass of ImednetError
# ---------------------------------------------------------------------------


def test_plugin_load_error_is_imednet_error() -> None:
    """Test that plugin load error is imednet error."""
    from imednet.errors import ImednetError

    err = PluginLoadError("boom")
    assert isinstance(err, ImednetError)
    assert str(err) == "boom"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sdk() -> _BaseSDK:
    """Return a _BaseSDK instance without triggering full ImednetSDK init."""
    return object.__new__(_BaseSDK)


# ---------------------------------------------------------------------------
# _BaseSDK._init_workflows — plugin not installed
# ---------------------------------------------------------------------------


def test_init_workflows_returns_missing_workflows_when_no_plugin() -> None:
    """Test that init workflows returns missing workflows when no plugin."""
    sdk = _make_sdk()
    with patch("imednet.sdk.entry_points", return_value=[]):
        ns = sdk._init_workflows()

    # Accessing any attribute should raise ImportError (not PluginLoadError)
    with pytest.raises(
        ImportError,
        match="Workflow 'data_extraction' not found. Please install the required package.",
    ):
        _ = ns.data_extraction


# ---------------------------------------------------------------------------
# _BaseSDK._init_workflows — plugin installed but broken import
# ---------------------------------------------------------------------------


def test_init_workflows_raises_plugin_load_error_on_broken_entry_point() -> None:
    """Test that init workflows raises plugin load error on broken entry point."""
    ep = MagicMock(spec=EntryPoint)
    ep.name = "data_extraction"
    ep.value = "broken.module:factory"
    ep.load.side_effect = ModuleNotFoundError("No module named 'broken'")

    sdk = _make_sdk()
    with patch("imednet.sdk.entry_points", return_value=[ep]):
        ns = sdk._init_workflows()
        with pytest.raises(PluginLoadError, match="Failed to load workflow plugin"):
            _ = ns.data_extraction


# ---------------------------------------------------------------------------
# _BaseSDK._init_workflows — plugin loaded but not callable
# ---------------------------------------------------------------------------


def test_init_workflows_raises_plugin_load_error_when_entry_point_not_callable() -> None:
    """Test that init workflows raises plugin load error when entry point not callable."""
    ep = MagicMock(spec=EntryPoint)
    ep.name = "data_extraction"
    ep.value = "myplugin.ns:NOT_A_CALLABLE"
    ep.load.return_value = "I am a string, not a callable"  # type: ignore[assignment]

    sdk = _make_sdk()
    with patch("imednet.sdk.entry_points", return_value=[ep]):
        ns = sdk._init_workflows()
        with pytest.raises(PluginLoadError, match="must be a callable"):
            _ = ns.data_extraction


# ---------------------------------------------------------------------------
# _BaseSDK._init_workflows — plugin installed and valid
# ---------------------------------------------------------------------------


def test_init_workflows_returns_namespace_from_valid_plugin() -> None:
    """Test that init workflows returns namespace from valid plugin."""
    ep = MagicMock(spec=EntryPoint)
    ep.name = "data_extraction"
    ep.value = "myplugin.ns:factory"
    ep.load.return_value = _valid_factory

    sdk = _make_sdk()
    with patch("imednet.sdk.entry_points", return_value=[ep]):
        ns = sdk._init_workflows()

    assert isinstance(ns.data_extraction, _ValidNamespace)


# ---------------------------------------------------------------------------
# _BaseSDK._init_workflows — plugin factory raises TypeError
# ---------------------------------------------------------------------------


def test_init_workflows_raises_plugin_load_error_when_factory_raises_type_error() -> None:
    """Test that init workflows raises plugin load error when factory raises type error."""

    def _bad_factory(*args: Any, **kwargs: Any) -> None:
        """Helper function to  bad factory."""
        raise TypeError("unexpected argument")

    ep = MagicMock(spec=EntryPoint)
    ep.name = "data_extraction"
    ep.value = "myplugin.ns:bad_factory"
    ep.load.return_value = _bad_factory

    sdk = _make_sdk()
    with patch("imednet.sdk.entry_points", return_value=[ep]):
        ns = sdk._init_workflows()
        with pytest.raises(PluginLoadError, match="Failed to instantiate workflows"):
            _ = ns.data_extraction
