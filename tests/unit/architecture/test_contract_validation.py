"""Tests for contract validation across namespaces and plugins."""

import asyncio
import inspect
import sys
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from imednet_sinks.plugin import SinksNamespace

from imednet.orchestration import MultiStudyOrchestrator
from imednet.plugins import WorkflowsNamespaceProtocol
from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow
from imednet_workflows.namespace import Workflows
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow


def test_sinks_namespace_contract():
    """Verify that MongoDbSinkConfig is accessible dynamically on SinksNamespace."""
    mock_sdk = MagicMock()
    sinks_ns = SinksNamespace(mock_sdk)

    # Should not raise an AttributeError
    try:
        mongo_config = sinks_ns.MongoDbSinkConfig
    except AttributeError as e:
        pytest.fail(f"Failed to access MongoDbSinkConfig: {e}")

    assert mongo_config is not None


def test_workflows_namespace_contract():
    """Verify that duckdb_centralizer and register_subjects are instantiated and accessible on Workflows."""
    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)

    # Should not raise an AttributeError
    try:
        duckdb_workflow = workflows_ns.duckdb_centralizer
    except AttributeError as e:
        pytest.fail(f"Failed to access duckdb_centralizer: {e}")

    assert duckdb_workflow is not None
    assert isinstance(duckdb_workflow, DuckDBIngestionWorkflow)
    assert workflows_ns.duckdb_centralizer is duckdb_workflow

    try:
        reg_workflow = workflows_ns.register_subjects
    except AttributeError as e:
        pytest.fail(f"Failed to access register_subjects: {e}")

    assert reg_workflow is not None
    assert isinstance(reg_workflow, RegisterSubjectsWorkflow)


def test_workflows_contract_parity():
    """Assert 100% parity between Workflows namespace attributes and registered entry points,
    and assert that every registered entry point resolves and instantiates through sdk.workflows.
    """
    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)

    # Public attributes exposed on the Workflows namespace
    namespace_attrs = {attr for attr in dir(workflows_ns) if not attr.startswith("_")}

    # Entry points registered in the 'imednet.workflows' entrypoint group
    eps = {ep.name: ep for ep in entry_points(group="imednet.workflows")}

    # Assert 100% parity between Workflows namespace attributes and registered entry points
    assert namespace_attrs == set(eps.keys()), (
        f"Parity mismatch between Workflows namespace attributes and registered entry points: "
        f"in namespace but not registered: {namespace_attrs - set(eps.keys())}, "
        f"registered but not in namespace: {set(eps.keys()) - namespace_attrs}"
    )

    # Protocol attributes exposed on WorkflowsNamespaceProtocol
    protocol_attrs = set(WorkflowsNamespaceProtocol.__annotations__.keys())

    # Assert 100% parity between Workflows namespace attributes and WorkflowsNamespaceProtocol attributes
    assert namespace_attrs == protocol_attrs, (
        f"Parity mismatch between Workflows namespace attributes and WorkflowsNamespaceProtocol: "
        f"in namespace but not protocol: {namespace_attrs - protocol_attrs}, "
        f"in protocol but not namespace: {protocol_attrs - namespace_attrs}"
    )

    # Assert 100% parity between registered entry points and WorkflowsNamespaceProtocol attributes
    assert set(eps.keys()) == protocol_attrs, (
        f"Parity mismatch between registered entry points and WorkflowsNamespaceProtocol: "
        f"registered but not in protocol: {set(eps.keys()) - protocol_attrs}, "
        f"in protocol but not registered: {protocol_attrs - set(eps.keys())}"
    )

    # Assert parity against packages/plugins-workflows/pyproject.toml if present
    pyproject_path = (
        Path(__file__).resolve().parents[3] / "packages" / "plugins-workflows" / "pyproject.toml"
    )
    if pyproject_path.exists():
        with open(pyproject_path, "rb") as f:
            pyproject_data = tomllib.load(f)
        toml_eps = (
            pyproject_data.get("project", {}).get("entry-points", {}).get("imednet.workflows", {})
        )
        assert namespace_attrs == set(toml_eps.keys()), (
            f"Parity mismatch between Workflows namespace and pyproject.toml entry points: "
            f"in namespace but not pyproject: {namespace_attrs - set(toml_eps.keys())}, "
            f"in pyproject but not namespace: {set(toml_eps.keys()) - namespace_attrs}"
        )
        assert protocol_attrs == set(toml_eps.keys()), (
            f"Parity mismatch between WorkflowsNamespaceProtocol and pyproject.toml entry points: "
            f"in protocol but not pyproject: {protocol_attrs - set(toml_eps.keys())}, "
            f"in pyproject but not protocol: {set(toml_eps.keys()) - protocol_attrs}"
        )

    # Assert that Workflows instance satisfies WorkflowsNamespaceProtocol
    assert isinstance(workflows_ns, WorkflowsNamespaceProtocol)

    # Assert that every registered entry point resolves and instantiates through sdk.workflows
    sdk = ImednetSDK(api_key="key", security_key="secret", base_url="https://example.com")
    try:
        for name, ep in eps.items():
            workflow_instance = getattr(sdk.workflows, name)
            assert workflow_instance is not None
            target_cls = ep.load()
            assert isinstance(workflow_instance, target_cls)
            assert sdk.workflows[name] is workflow_instance

        # Acceptance criteria verification
        assert isinstance(sdk.workflows.duckdb_centralizer, DuckDBIngestionWorkflow)
        assert isinstance(sdk.workflows.register_subjects, RegisterSubjectsWorkflow)
    finally:
        sdk.close()

    async_sdk = AsyncImednetSDK(
        api_key="key", security_key="secret", base_url="https://example.com"
    )
    try:
        for name, ep in eps.items():
            workflow_instance = getattr(async_sdk.workflows, name)
            assert workflow_instance is not None
            target_cls = ep.load()
            assert isinstance(workflow_instance, target_cls)
            assert async_sdk.workflows[name] is workflow_instance
    finally:
        asyncio.run(async_sdk.aclose())


def test_workflows_namespace_lazy_instantiation_without_duckdb(monkeypatch):
    """Verify that Workflows instantiates cleanly without duckdb and defers ImportError."""
    builtin_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)

    # Protocol runtime check passes without importing duckdb
    assert isinstance(workflows_ns, WorkflowsNamespaceProtocol)

    assert workflows_ns.data_extraction is not None

    with pytest.raises(
        ImportError,
        match=(
            r"DuckDB ingestion requires the optional 'duckdb' dependency\. "
            r"Install with `pip install 'imednet\[duckdb\]'`\."
        ),
    ):
        _ = workflows_ns.duckdb_centralizer


def test_workflow_constructors_contract():
    """Verify that all workflow constructors accept at most one mandatory argument (the SDK client)."""
    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)
    _ = workflows_ns.duckdb_centralizer

    for attr_name, attr_val in vars(workflows_ns).items():
        if attr_name.startswith("_"):
            continue

        cls = attr_val.__class__
        sig = inspect.signature(cls.__init__)

        mandatory_params = []
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if param.default is inspect.Parameter.empty:
                mandatory_params.append(param_name)

        assert len(mandatory_params) <= 1, (
            f"Workflow class {cls.__name__} constructor requires more than one mandatory argument: "
            f"{mandatory_params}. It must accept only the SDK client as a mandatory argument."
        )


def test_workflows_sync_async_method_parity():
    """Assert 100% parity between sync network methods and async_* counterparts across all 9 workflows and MultiStudyOrchestrator."""
    local_only_methods: dict[str, set[str]] = {
        "duckdb_centralizer": {"build_silver_view"},
        "record_mapper": {"transform_to_sdtm"},
        "uat": {"build_spec", "generate", "reset", "transition_to"},
        "uat_inspector": {"clear_cache"},
    }

    eps = {ep.name: ep for ep in entry_points(group="imednet.workflows")}
    assert len(eps) == 9

    for name, ep in sorted(eps.items()):
        cls = ep.load()
        public_methods = {
            m: fn
            for m, fn in inspect.getmembers(cls, predicate=inspect.isfunction)
            if not m.startswith("_")
        }
        sync_network_methods = {
            m
            for m in public_methods
            if not m.startswith("async_") and m not in local_only_methods.get(name, set())
        }
        async_methods = {m for m in public_methods if m.startswith("async_")}
        expected_async_methods = {f"async_{m}" for m in sync_network_methods}

        assert async_methods == expected_async_methods, (
            f"Sync/async method parity mismatch on workflow '{name}' ({cls.__name__}): "
            f"missing async counterparts={expected_async_methods - async_methods}, "
            f"orphan async methods={async_methods - expected_async_methods}"
        )

        for async_name in sorted(async_methods):
            async_fn = public_methods[async_name]
            assert inspect.iscoroutinefunction(async_fn) or inspect.isasyncgenfunction(async_fn), (
                f"{cls.__name__}.{async_name} must be an async coroutine or async generator function."
            )

    orch_methods = {
        m: fn
        for m, fn in inspect.getmembers(MultiStudyOrchestrator, predicate=inspect.isfunction)
        if not m.startswith("_")
    }
    orch_sync = {m for m in orch_methods if not m.startswith("async_")}
    orch_async = {m for m in orch_methods if m.startswith("async_")}
    assert orch_async == {f"async_{m}" for m in orch_sync}
    for async_name in sorted(orch_async):
        assert inspect.iscoroutinefunction(orch_methods[async_name])
