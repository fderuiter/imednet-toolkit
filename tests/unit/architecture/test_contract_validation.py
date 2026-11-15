"""Tests for contract validation across namespaces and plugins."""

import inspect
import sys
from unittest.mock import MagicMock

import pytest
from imednet_sinks.plugin import SinksNamespace

from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow
from imednet_workflows.namespace import Workflows


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
    """Verify that duckdb_centralizer is instantiated and accessible on Workflows."""
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
