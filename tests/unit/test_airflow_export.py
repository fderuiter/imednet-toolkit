"""Unit tests for airflow export."""

import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


# Setup dummy airflow modules to prevent import errors if airflow is not installed
def _setup_airflow(monkeypatch):
    """Helper function to  setup airflow."""
    airflow_mod = ModuleType("airflow")
    hooks_pkg = ModuleType("airflow.hooks")
    hooks_mod = ModuleType("airflow.sdk.bases.hook")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_bases = ModuleType("airflow.sdk.bases")
    sdk_defs = ModuleType("airflow.sdk.definitions")
    sdk_ctx = ModuleType("airflow.sdk.definitions.context")
    models_mod = ModuleType("airflow.models")

    class DummyBaseHook:
        """Test suite for DummyBaseHook."""

        @classmethod
        def get_connection(cls, conn_id):
            """Helper function to get connection."""
            raise NotImplementedError

    class DummyBaseOperator:
        """Test suite for DummyBaseOperator."""

        template_fields = ()

        def __init__(self, **kwargs):
            """Initialize the test object."""

    hooks_mod.BaseHook = DummyBaseHook
    models_mod.BaseOperator = DummyBaseOperator

    sdk_bases.hook = hooks_mod
    sdk_mod.bases = sdk_bases
    sdk_defs.context = sdk_ctx
    sdk_mod.definitions = sdk_defs
    airflow_mod.sdk = sdk_mod
    airflow_mod.hooks = hooks_pkg
    airflow_mod.models = models_mod

    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.hooks", hooks_pkg)
    monkeypatch.setitem(sys.modules, "airflow.sdk.bases.hook", hooks_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk.bases", sdk_bases)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions", sdk_defs)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions.context", sdk_ctx)
    monkeypatch.setitem(sys.modules, "airflow.models", models_mod)


EXPORT_FUNCTIONS = [
    "export_to_csv",
    "export_to_duckdb",
    "export_to_parquet",
    "export_to_excel",
    "export_to_json",
    "export_to_sql",
    "export_to_sql_by_form",
]


@pytest.fixture(autouse=True)
def setup_airflow_mock(monkeypatch):
    """Helper function to setup airflow mock."""
    _setup_airflow(monkeypatch)


@pytest.mark.parametrize("func_name", EXPORT_FUNCTIONS)
def test_airflow_export_forwards_arguments(func_name: str) -> None:
    """Test that each airflow export function forwards arguments to the base export module."""
    # We import the export module inside the test to ensure the mock airflow modules are picked up.
    from apache_airflow_providers_imednet import export

    # Arrange
    airflow_func = getattr(export, func_name)
    base_export_path = f"imednet.integrations.export.{func_name}"

    args = ("arg1", 42)
    kwargs = {"key1": "value1", "key2": True}

    # Act & Assert
    with patch(base_export_path) as mock_base_func:
        airflow_func(*args, **kwargs)
        mock_base_func.assert_called_once_with(*args, **kwargs)


@pytest.mark.parametrize("func_name", EXPORT_FUNCTIONS)
def test_spi_export_delegation_with_dispatcher_binding(func_name: str) -> None:
    """Resolve real SPI bindings independently of package attribute import order."""
    script = '''
import importlib
from unittest.mock import patch
import imednet.integrations as integrations

# A cached submodule import must not decide whether `export` is callable or a module.
importlib.reload(integrations)
assert callable(integrations.export)
import imednet.spi as spi
importlib.reload(spi)
assert spi.export is integrations.export
from apache_airflow_providers_imednet import export
with patch("imednet.integrations.export." + FUNC, return_value=None) as destination:
    getattr(export, FUNC)("synthetic-study", path="synthetic-output")
    destination.assert_called_once_with("synthetic-study", path="synthetic-output")
with patch("imednet.integrations.export." + FUNC, side_effect=ImportError("optional-driver")):
    try:
        getattr(export, FUNC)("synthetic-study")
    except ImportError as error:
        assert str(error) == "optional-driver"
    else:
        raise AssertionError("dependency error was swallowed")
'''
    repo_root = Path(__file__).resolve().parents[2]
    package_paths = [
        str(repo_root / "packages" / "providers-airflow" / "src"),
        str(repo_root / "packages" / "core" / "src"),
    ]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(package_paths + [existing_pythonpath])
    else:
        env["PYTHONPATH"] = os.pathsep.join(package_paths)

    result = subprocess.run(
        [sys.executable, "-c", "FUNC = " + repr(func_name) + "\n" + script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_export_operator_dispatches_duckdb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify operator dispatch with destination set to DuckDB."""
    from apache_airflow_providers_imednet import export
    from apache_airflow_providers_imednet.operators.export import (
        _TABULAR_EXPORTS,
        ImednetExportOperator,
    )

    assert _TABULAR_EXPORTS["duckdb"] is export.export_to_duckdb

    mock_sdk = MagicMock()
    mock_export = MagicMock()
    monkeypatch.setattr(export, "export_to_duckdb", mock_export)

    op = ImednetExportOperator(
        task_id="test_duckdb_export",
        study_key="STUDY-123",
        destination="duckdb",
        output_path="/tmp/test_study.duckdb",
        export_kwargs={"table_name": "clinical_records"},
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    result = op.execute({})

    mock_export.assert_called_once_with(
        mock_sdk,
        "STUDY-123",
        "/tmp/test_study.duckdb",
        table_name="clinical_records",
    )
    assert result == "/tmp/test_study.duckdb"
