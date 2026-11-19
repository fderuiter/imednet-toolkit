"""Tests for the unified plugin contract: protocols and _BaseSDK._load_plugin."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from imednet.errors import PluginLoadError
from imednet.plugins import (
    AsyncWorkflowsNamespaceProtocol,
    PluginProtocol,
    SinksNamespaceProtocol,
    SinksPluginProtocol,
    WorkflowsNamespaceProtocol,
)
from imednet.sdk import AsyncImednetSDK, _BaseSDK
from imednet_workflows.namespace import AsyncWorkflows


class _ValidWorkflowsNamespace:
    """Minimal object satisfying WorkflowsNamespaceProtocol."""

    data_extraction: Any = None
    duckdb_centralizer: Any = None
    query_management: Any = None
    record_mapper: Any = None
    record_update: Any = None
    register_subjects: Any = None
    subject_data: Any = None
    uat: Any = None
    uat_inspector: Any = None


class _ValidAsyncWorkflowsNamespace:
    """Minimal object satisfying AsyncWorkflowsNamespaceProtocol."""

    record_update: Any = None
    uat_inspector: Any = None


class _ValidSinksNamespace:
    """Minimal object satisfying SinksNamespaceProtocol."""

    export_to_mongodb: Any = None
    MongoDbSinkConfig: Any = None
    MongoDbExportSink: Any = None
    export_to_neo4j: Any = None
    Neo4jSinkConfig: Any = None
    Neo4jExportSink: Any = None
    export_to_snowflake: Any = None
    SnowflakeSinkConfig: Any = None
    SnowflakeExportSink: Any = None


def _valid_workflows_factory(sdk_instance: Any) -> _ValidWorkflowsNamespace:
    """Return a valid workflows namespace."""
    _ = sdk_instance
    return _ValidWorkflowsNamespace()


def _valid_sinks_factory(sdk_instance: Any) -> _ValidSinksNamespace:
    """Return a valid sinks namespace."""
    _ = sdk_instance
    return _ValidSinksNamespace()


def test_valid_namespaces_satisfy_protocols() -> None:
    """Verify valid namespaces satisfy sync, async, and sinks runtime protocols."""
    assert isinstance(_ValidWorkflowsNamespace(), WorkflowsNamespaceProtocol)
    assert isinstance(_ValidAsyncWorkflowsNamespace(), AsyncWorkflowsNamespaceProtocol)
    assert isinstance(_ValidSinksNamespace(), SinksNamespaceProtocol)


def test_incomplete_namespaces_fail_protocol_checks() -> None:
    """Verify incomplete objects fail runtime protocol checks cleanly."""

    class _Incomplete:
        data_extraction: Any = None

    assert not isinstance(_Incomplete(), WorkflowsNamespaceProtocol)
    assert not isinstance(_Incomplete(), AsyncWorkflowsNamespaceProtocol)
    assert not isinstance(_Incomplete(), SinksNamespaceProtocol)


def test_async_workflows_safe_isinstance_against_sync_protocol() -> None:
    """Verify isinstance(AsyncWorkflows, WorkflowsNamespaceProtocol) returns False without raising."""
    async_sdk = MagicMock(spec=AsyncImednetSDK)
    ns = AsyncWorkflows(async_sdk)
    assert isinstance(ns, AsyncWorkflowsNamespaceProtocol)
    assert not isinstance(ns, WorkflowsNamespaceProtocol)


def test_callable_factories_satisfy_plugin_protocols() -> None:
    """Verify callable factories satisfy PluginProtocol and SinksPluginProtocol."""
    assert isinstance(_valid_workflows_factory, PluginProtocol)
    assert isinstance(_valid_sinks_factory, SinksPluginProtocol)
    assert not isinstance("not-a-callable", PluginProtocol)
    assert not isinstance("not-a-callable", SinksPluginProtocol)


def test_plugin_load_error_is_imednet_error() -> None:
    """Verify PluginLoadError inherits from ImednetError."""
    from imednet.errors import ImednetError

    err = PluginLoadError("boom")
    assert isinstance(err, ImednetError)
    assert str(err) == "boom"


def _make_sdk() -> _BaseSDK:
    """Return a bare _BaseSDK instance without running full HTTP client init."""
    return object.__new__(_BaseSDK)


def test_init_plugins_return_none_when_no_plugin_installed() -> None:
    """Verify _init_workflows and _init_sinks both return None when absent."""
    sdk = _make_sdk()
    with patch("imednet.sdk.entry_points", return_value=[]):
        assert sdk._init_workflows() is None
        assert sdk._init_sinks() is None


@pytest.mark.parametrize("plugin_name", ["workflows", "sinks"])
def test_load_plugin_raises_on_duplicate_entry_points(plugin_name: str) -> None:
    """Verify duplicate imednet.plugins registrations raise PluginLoadError."""
    ep1 = MagicMock(spec=EntryPoint)
    ep1.name = plugin_name
    ep1.value = "pkg_a:create"
    ep2 = MagicMock(spec=EntryPoint)
    ep2.name = plugin_name
    ep2.value = "pkg_b:create"

    sdk = _make_sdk()
    with (
        patch("imednet.sdk.entry_points", return_value=[ep1, ep2]),
        pytest.raises(PluginLoadError, match=f"Multiple '{plugin_name}' plugins registered"),
    ):
        sdk._load_plugin(plugin_name)


@pytest.mark.parametrize("plugin_name", ["workflows", "sinks"])
def test_load_plugin_raises_on_broken_entry_point(plugin_name: str) -> None:
    """Verify broken plugin entry point imports raise PluginLoadError."""
    ep = MagicMock(spec=EntryPoint)
    ep.name = plugin_name
    ep.value = "broken.module:factory"
    ep.load.side_effect = ModuleNotFoundError("No module named 'broken'")

    sdk = _make_sdk()
    with (
        patch("imednet.sdk.entry_points", return_value=[ep]),
        pytest.raises(PluginLoadError, match=f"Failed to load '{plugin_name}' plugin"),
    ):
        sdk._load_plugin(plugin_name)


@pytest.mark.parametrize("plugin_name", ["workflows", "sinks"])
def test_load_plugin_raises_when_entry_point_not_callable(plugin_name: str) -> None:
    """Verify non-callable plugin entry points raise PluginLoadError."""
    ep = MagicMock(spec=EntryPoint)
    ep.name = plugin_name
    ep.value = "myplugin.ns:NOT_A_CALLABLE"
    ep.load.return_value = "I am a string, not a callable"  # type: ignore[assignment]

    sdk = _make_sdk()
    with (
        patch("imednet.sdk.entry_points", return_value=[ep]),
        pytest.raises(PluginLoadError, match="must be a callable"),
    ):
        sdk._load_plugin(plugin_name)


def test_init_workflows_and_sinks_return_namespaces_from_valid_plugins() -> None:
    """Verify _init_workflows and _init_sinks invoke unified imednet.plugins factories."""
    wf_ep = MagicMock(spec=EntryPoint)
    wf_ep.name = "workflows"
    wf_ep.value = "myplugin.ns:create_workflows"
    wf_ep.load.return_value = _valid_workflows_factory

    sinks_ep = MagicMock(spec=EntryPoint)
    sinks_ep.name = "sinks"
    sinks_ep.value = "mysinks.ns:create_sinks"
    sinks_ep.load.return_value = _valid_sinks_factory

    def fake_eps(*, group: str, name: str | None = None) -> list[EntryPoint]:
        if group != "imednet.plugins":
            return []
        if name == "workflows":
            return [wf_ep]
        if name == "sinks":
            return [sinks_ep]
        return [wf_ep, sinks_ep]

    sdk = _make_sdk()
    with patch("imednet.sdk.entry_points", side_effect=fake_eps):
        wf_ns = sdk._init_workflows()
        sinks_ns = sdk._init_sinks()

    assert isinstance(wf_ns, _ValidWorkflowsNamespace)
    assert isinstance(sinks_ns, _ValidSinksNamespace)


@pytest.mark.parametrize("plugin_name", ["workflows", "sinks"])
def test_load_plugin_raises_when_factory_raises_type_error(plugin_name: str) -> None:
    """Verify factory TypeError at initialization raises PluginLoadError."""

    def _bad_factory(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("unexpected argument")

    ep = MagicMock(spec=EntryPoint)
    ep.name = plugin_name
    ep.value = "myplugin.ns:bad_factory"
    ep.load.return_value = _bad_factory

    sdk = _make_sdk()
    with (
        patch("imednet.sdk.entry_points", return_value=[ep]),
        pytest.raises(PluginLoadError, match=f"Failed to instantiate '{plugin_name}' plugin"),
    ):
        sdk._load_plugin(plugin_name)
