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


def test_operator_csv_export_without_sinks_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify standard CSV export succeeds when sinks plugin is None."""
    from apache_airflow_providers_imednet import export
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    mock_sdk = MagicMock(sinks=None)
    mock_csv_export = MagicMock()
    monkeypatch.setattr(export, "export_to_csv", mock_csv_export)

    op = ImednetExportOperator(
        task_id="test_csv_export",
        study_key="STUDY-CSV",
        destination="csv",
        output_path="/tmp/output.csv",
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    result = op.execute({})
    assert result == "/tmp/output.csv"
    mock_csv_export.assert_called_once_with(mock_sdk, "STUDY-CSV", "/tmp/output.csv")


@pytest.mark.parametrize("destination", ["snowflake", "neo4j", "mongodb"])
def test_operator_missing_sinks_plugin_raises_airflow_exception(
    destination: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify missing sinks plugin raises an AirflowException with installation instructions."""
    from apache_airflow_providers_imednet._airflow_compat import AirflowException
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    # Ensure imednet_sinks is not in sys.modules for this test
    monkeypatch.delitem(sys.modules, "imednet_sinks", raising=False)

    mock_sdk = MagicMock(sinks=None)
    op = ImednetExportOperator(
        task_id=f"test_{destination}_missing_sinks",
        study_key="STUDY-123",
        destination=destination,
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    with pytest.raises(
        AirflowException,
        match=r"The sinks plugin is required for destination '.*' but is not installed\. Please install it using: pip install imednet-plugins-sinks",
    ):
        op.execute({})


def test_operator_resolves_snowflake_sink_via_sdk_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify operator resolves SnowflakeExportSink through sdk.sinks."""
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    monkeypatch.delitem(sys.modules, "imednet_sinks", raising=False)

    mock_sink_instance = MagicMock()
    mock_sink_cls = MagicMock(return_value=mock_sink_instance)
    mock_config_cls = MagicMock()

    mock_sinks = MagicMock(
        SnowflakeExportSink=mock_sink_cls,
        SnowflakeSinkConfig=mock_config_cls,
    )
    mock_sdk = MagicMock(sinks=mock_sinks)
    mock_sdk.records.list.return_value = [{"record_id": "rec_001"}]

    op = ImednetExportOperator(
        task_id="test_snowflake_export",
        study_key="STUDY-SF",
        destination="snowflake",
        batch_size=250,
        export_kwargs={
            "account": "test_account",
            "user": "test_user",
            "password": "secret_password",
            "database": "TEST_DB",
            "schema": "PUBLIC",
            "warehouse": "TEST_WH",
            "stage": "TEST_STAGE",
            "table": "TEST_TABLE",
        },
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    op.execute({})

    mock_config_cls.assert_called_once_with(
        study_key="STUDY-SF",
        batch_size=250,
        max_retries=3,
        idempotent=True,
        extra=op._get_runtime_export_kwargs(),
        account="test_account",
        user="test_user",
        password="secret_password",
        database="TEST_DB",
        schema="PUBLIC",
        warehouse="TEST_WH",
        stage="TEST_STAGE",
        table="TEST_TABLE",
    )
    mock_sink_cls.assert_called_once_with(config=mock_config_cls.return_value)
    mock_sink_instance.__enter__.assert_called_once()
    mock_sink_instance.write_batch.assert_called()


def test_operator_resolves_neo4j_sink_via_sdk_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify operator resolves Neo4jExportSink through sdk.sinks."""
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    monkeypatch.delitem(sys.modules, "imednet_sinks", raising=False)

    mock_sink_instance = MagicMock()
    mock_sink_cls = MagicMock(return_value=mock_sink_instance)
    mock_config_cls = MagicMock()

    mock_sinks = MagicMock(
        Neo4jExportSink=mock_sink_cls,
        Neo4jSinkConfig=mock_config_cls,
    )
    mock_sdk = MagicMock(sinks=mock_sinks)
    mock_sdk.records.list.return_value = [{"record_id": "rec_001"}]

    op = ImednetExportOperator(
        task_id="test_neo4j_export",
        study_key="STUDY-NEO",
        destination="neo4j",
        batch_size=100,
        export_kwargs={"uri": "bolt://localhost:7687", "auth": ("neo4j", "test_pass")},
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    op.execute({})

    mock_config_cls.assert_called_once_with(
        study_key="STUDY-NEO",
        batch_size=100,
        max_retries=3,
        idempotent=True,
        extra=op._get_runtime_export_kwargs(),
        uri="bolt://localhost:7687",
        auth=("neo4j", "test_pass"),
    )
    mock_sink_cls.assert_called_once_with(config=mock_config_cls.return_value)
    mock_sink_instance.__enter__.assert_called_once()
    mock_sink_instance.write_batch.assert_called()


def test_operator_resolves_mongodb_sink_via_sdk_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify operator resolves MongoDbExportSink through sdk.sinks."""
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    monkeypatch.delitem(sys.modules, "imednet_sinks", raising=False)

    mock_sink_instance = MagicMock()
    mock_sink_cls = MagicMock(return_value=mock_sink_instance)
    mock_config_cls = MagicMock()

    mock_sinks = MagicMock(
        MongoDbExportSink=mock_sink_cls,
        MongoDbSinkConfig=mock_config_cls,
    )
    mock_sdk = MagicMock(sinks=mock_sinks)
    mock_sdk.records.list.return_value = [{"record_id": "rec_001"}]

    op = ImednetExportOperator(
        task_id="test_mongodb_export",
        study_key="STUDY-MONGO",
        destination="mongodb",
        batch_size=50,
        export_kwargs={
            "uri": "mongodb://localhost:27017",
            "database": "clinical",
            "collection": "records",
        },
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    op.execute({})

    mock_config_cls.assert_called_once_with(
        study_key="STUDY-MONGO",
        batch_size=50,
        max_retries=3,
        idempotent=True,
        extra=op._get_runtime_export_kwargs(),
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="records",
    )
    mock_sink_cls.assert_called_once_with(config=mock_config_cls.return_value)
    mock_sink_instance.__enter__.assert_called_once()
    mock_sink_instance.write_batch.assert_called()


def test_operator_missing_sink_factory_raises_airflow_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that when a specific sink class is missing on sdk.sinks, an actionable error is raised."""
    from apache_airflow_providers_imednet._airflow_compat import AirflowException
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    monkeypatch.delitem(sys.modules, "imednet_sinks", raising=False)

    class IncompleteSinks:
        pass

    mock_sdk = MagicMock(sinks=IncompleteSinks())
    op = ImednetExportOperator(
        task_id="test_missing_factory",
        study_key="STUDY-123",
        destination="snowflake",
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    with pytest.raises(
        AirflowException,
        match=r"Snowflake sink is not available in the installed sinks plugin\. Please install the required sink dependencies: pip install 'imednet-plugins-sinks\[snowflake\]'",
    ):
        op.execute({})
