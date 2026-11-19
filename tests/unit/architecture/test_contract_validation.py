"""Tests for contract validation across unified SDK namespaces and plugins."""

from __future__ import annotations

import asyncio
import inspect
import sys
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from imednet_sinks.plugin import SinksNamespace

from imednet.plugins import (
    AsyncWorkflowsNamespaceProtocol,
    SinksNamespaceProtocol,
    WorkflowsNamespaceProtocol,
)
from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow
from imednet_workflows.namespace import AsyncWorkflows, Workflows
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow

_REMOVED_WORKFLOW_GROUPS = (
    "imednet.workflows",
    "imednet.pollers",
    "imednet.mappers",
    "imednet.loaders",
    "imednet.stores",
)


def test_sinks_namespace_contract() -> None:
    """Verify live SDK sinks namespaces satisfy SinksNamespaceProtocol."""
    mock_sdk = MagicMock()
    sinks_ns = SinksNamespace(mock_sdk)
    assert sinks_ns.MongoDbSinkConfig is not None
    assert isinstance(sinks_ns, SinksNamespaceProtocol)

    sdk = ImednetSDK(api_key="key", security_key="secret", base_url="https://example.com")
    try:
        assert sdk.sinks is not None
        assert isinstance(sdk.sinks, SinksNamespaceProtocol)
    finally:
        sdk.close()

    async_sdk = AsyncImednetSDK(
        api_key="key", security_key="secret", base_url="https://example.com"
    )
    try:
        assert async_sdk.sinks is not None
        assert isinstance(async_sdk.sinks, SinksNamespaceProtocol)
    finally:
        asyncio.run(async_sdk.aclose())


def test_workflows_namespace_contract() -> None:
    """Verify lazy Workflows and AsyncWorkflows satisfy their respective protocols."""
    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)

    # Lazy startup: only _sdk is stored before workflow properties are accessed
    assert set(vars(workflows_ns).keys()) == {"_sdk"}

    duckdb_workflow = workflows_ns.duckdb_centralizer
    assert isinstance(duckdb_workflow, DuckDBIngestionWorkflow)
    assert workflows_ns.duckdb_centralizer is duckdb_workflow

    reg_workflow = workflows_ns.register_subjects
    assert isinstance(reg_workflow, RegisterSubjectsWorkflow)
    assert workflows_ns.register_subjects is reg_workflow


def test_workflows_and_sinks_contract_parity() -> None:
    """Assert 100% parity across imednet.plugins entry points, namespaces, and protocols."""
    plugin_eps = {ep.name: ep for ep in entry_points(group="imednet.plugins")}
    assert "workflows" in plugin_eps
    assert "sinks" in plugin_eps
    assert plugin_eps["workflows"].value == "imednet_workflows.namespace:create_workflows"
    assert plugin_eps["sinks"].value == "imednet_sinks.plugin:create_sinks"

    for removed_group in _REMOVED_WORKFLOW_GROUPS:
        assert list(entry_points(group=removed_group)) == [], (
            f"Legacy entry-point group '{removed_group}' must not be registered"
        )

    repo_root = Path(__file__).resolve().parents[3]
    workflows_pyproject = repo_root / "packages" / "plugins-workflows" / "pyproject.toml"
    if workflows_pyproject.exists():
        with open(workflows_pyproject, "rb") as f:
            wf_toml = tomllib.load(f)
        ep_tables = wf_toml.get("project", {}).get("entry-points", {})
        assert ep_tables.get("imednet.plugins") == {
            "workflows": "imednet_workflows.namespace:create_workflows"
        }
        for removed_group in _REMOVED_WORKFLOW_GROUPS:
            assert removed_group not in ep_tables

    core_pyproject = repo_root / "packages" / "core" / "pyproject.toml"
    if core_pyproject.exists():
        with open(core_pyproject, "rb") as f:
            core_toml = tomllib.load(f)
        export_deps = (
            core_toml.get("project", {}).get("optional-dependencies", {}).get("export", [])
        )
        assert all("imednet-workflows" not in dep for dep in export_deps)

    sync_ns = Workflows(MagicMock())
    sync_attrs = {attr for attr in dir(sync_ns) if not attr.startswith("_")}
    sync_protocol_attrs = set(WorkflowsNamespaceProtocol.__annotations__.keys())
    assert sync_attrs == sync_protocol_attrs

    async_mock_sdk = MagicMock(spec=AsyncImednetSDK)
    async_ns = AsyncWorkflows(async_mock_sdk)
    async_attrs = {attr for attr in dir(async_ns) if not attr.startswith("_")}
    async_protocol_attrs = set(AsyncWorkflowsNamespaceProtocol.__annotations__.keys())
    assert async_attrs == async_protocol_attrs == {"record_update", "uat_inspector"}

    sinks_ns = SinksNamespace(MagicMock())
    sinks_attrs = {attr for attr in dir(sinks_ns) if not attr.startswith("_")}
    sinks_protocol_attrs = set(SinksNamespaceProtocol.__annotations__.keys())
    assert sinks_attrs == sinks_protocol_attrs

    sdk = ImednetSDK(api_key="key", security_key="secret", base_url="https://example.com")
    try:
        assert sdk.workflows is not None
        assert sdk.sinks is not None
        assert isinstance(sdk.workflows, WorkflowsNamespaceProtocol)
        assert isinstance(sdk.sinks, SinksNamespaceProtocol)
        # No workflows eagerly instantiated at SDK startup
        assert set(vars(sdk.workflows).keys()) == {"_sdk"}

        for name in sync_protocol_attrs:
            wf_instance = getattr(sdk.workflows, name)
            assert wf_instance is not None
            assert getattr(sdk.workflows, name) is wf_instance
    finally:
        sdk.close()

    async_sdk = AsyncImednetSDK(
        api_key="key", security_key="secret", base_url="https://example.com"
    )
    try:
        assert async_sdk.workflows is not None
        assert async_sdk.sinks is not None
        assert isinstance(async_sdk.workflows, AsyncWorkflowsNamespaceProtocol)
        assert not isinstance(async_sdk.workflows, WorkflowsNamespaceProtocol)
        assert isinstance(async_sdk.sinks, SinksNamespaceProtocol)
        assert set(vars(async_sdk.workflows).keys()) == {"_sdk"}
        assert async_sdk.workflows.record_update is not None
        assert async_sdk.workflows.uat_inspector is not None
    finally:
        asyncio.run(async_sdk.aclose())


def test_workflows_namespace_lazy_instantiation_without_duckdb(monkeypatch) -> None:
    """Verify SDK startup and WorkflowsNamespaceProtocol check defer duckdb import."""
    builtin_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    sdk = ImednetSDK(api_key="key", security_key="secret", base_url="https://example.com")
    try:
        assert sdk.workflows is not None
        assert isinstance(sdk.workflows, WorkflowsNamespaceProtocol)
        assert set(vars(sdk.workflows).keys()) == {"_sdk"}
        assert sdk.workflows.data_extraction is not None

        with pytest.raises(
            ImportError,
            match=(
                r"DuckDB ingestion requires the optional 'duckdb' dependency\. "
                r"Install with `pip install 'imednet\[duckdb\]'`\."
            ),
        ):
            _ = sdk.workflows.duckdb_centralizer
    finally:
        sdk.close()


def test_workflow_constructors_contract() -> None:
    """Verify all workflow constructors accept at most one mandatory argument (the SDK client)."""
    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)

    for attr_name in WorkflowsNamespaceProtocol.__annotations__:
        attr_val = getattr(workflows_ns, attr_name)
        cls = attr_val.__class__
        sig = inspect.signature(cls.__init__)

        mandatory_params = [
            param_name
            for param_name, param in sig.parameters.items()
            if param_name != "self"
            and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            and param.default is inspect.Parameter.empty
        ]

        assert len(mandatory_params) <= 1, (
            f"Workflow class {cls.__name__} constructor requires more than one mandatory argument: "
            f"{mandatory_params}. It must accept only the SDK client as a mandatory argument."
        )
