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
    airflow_mod.__path__ = []
    hooks_pkg = ModuleType("airflow.hooks")
    hooks_pkg.__path__ = []
    hooks_mod = ModuleType("airflow.sdk.bases.hook")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.__path__ = []
    sdk_bases = ModuleType("airflow.sdk.bases")
    sdk_bases.__path__ = []
    sdk_defs = ModuleType("airflow.sdk.definitions")
    sdk_defs.__path__ = []
    sdk_ctx = ModuleType("airflow.sdk.definitions.context")
    sdk_dag = ModuleType("airflow.sdk.definitions.dag")
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
    models_mod.DAG = DummyBaseOperator
    sdk_dag.DAG = DummyBaseOperator
    sdk_dag.dag = DummyBaseOperator

    sdk_bases.hook = hooks_mod
    sdk_mod.bases = sdk_bases
    sdk_defs.context = sdk_ctx
    sdk_defs.dag = sdk_dag
    sdk_mod.definitions = sdk_defs
    airflow_mod.sdk = sdk_mod
    airflow_mod.hooks = hooks_pkg
    airflow_mod.models = models_mod

    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.hooks", hooks_pkg)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk.bases", sdk_bases)
    monkeypatch.setitem(sys.modules, "airflow.sdk.bases.hook", hooks_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions", sdk_defs)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions.context", sdk_ctx)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions.dag", sdk_dag)
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
    """Verify operator resolves SnowflakeExportSink credentials from destination_conn_id and forwards staging/quality options."""
    from types import SimpleNamespace

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    mock_sink_instance = MagicMock()
    mock_sink_cls = MagicMock(return_value=mock_sink_instance)
    mock_config_cls = MagicMock()

    from imednet.models.records import Record
    from imednet.models.variables import Variable

    mock_sinks = MagicMock(
        SnowflakeExportSink=mock_sink_cls,
        SnowflakeSinkConfig=mock_config_cls,
    )
    mock_sdk = MagicMock(sinks=mock_sinks)
    mock_sdk.get_variables.return_value = [
        Variable(variable_name="x", variable_type="int", form_id=1, form_key="F1")
    ]
    mock_sdk.records.list.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"x": 1})
    ]

    fake_conn = SimpleNamespace(
        host="conn_account",
        login="conn_user",
        password="conn_password",  # pragma: allowlist secret
        schema="PUBLIC",
        extra={
            "database": "TEST_DB",
            "warehouse": "TEST_WH",
            "stage": "TEST_STAGE",
            "table": "DEFAULT_TABLE",
        },
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_conn if conn_id == "snowflake_dest" else None),
    )

    op = ImednetExportOperator(
        task_id="test_snowflake_export",
        study_key="STUDY-SF",
        destination="snowflake",
        destination_conn_id="snowflake_dest",
        batch_size=250,
        max_retries=4,
        idempotent=True,
        quality_gate_enabled=True,
        min_schema_readiness_score=92.5,
        export_kwargs={
            "table": "TEST_TABLE",
            "stage_prefix": "imednet/prod",
            "local_staging_dir": "/tmp/sf_stage",
            "manifest_path": "/tmp/sf_manifest.jsonl",
            "retry_backoff": 2.5,
        },
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    assert "password" not in op.export_kwargs
    op.execute({})

    mock_config_cls.assert_called_once_with(
        study_key="STUDY-SF",
        batch_size=250,
        max_retries=4,
        retry_backoff=2.5,
        idempotent=True,
        quality_gate_enabled=True,
        min_schema_readiness_score=92.5,
        extra=op._get_runtime_export_kwargs(),
        account="conn_account",
        user="conn_user",
        password="conn_password",  # pragma: allowlist secret
        database="TEST_DB",
        schema="PUBLIC",
        warehouse="TEST_WH",
        stage="TEST_STAGE",
        table="TEST_TABLE",
        stage_prefix="imednet/prod",
        prefix=None,
        local_staging_dir="/tmp/sf_stage",
        manifest_path="/tmp/sf_manifest.jsonl",
    )
    assert "password" not in mock_config_cls.call_args.kwargs["extra"]
    mock_sink_cls.assert_called_once_with(config=mock_config_cls.return_value)
    mock_sink_instance.__enter__.assert_called_once()
    mock_sink_instance.write_batch.assert_called()


def test_operator_resolves_neo4j_sink_via_sdk_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify operator resolves Neo4jExportSink credentials from destination_conn_id and forwards database and quality gate options."""
    from types import SimpleNamespace

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    mock_sink_instance = MagicMock()
    mock_sink_cls = MagicMock(return_value=mock_sink_instance)
    mock_config_cls = MagicMock()

    from imednet.models.records import Record
    from imednet.models.variables import Variable

    mock_sinks = MagicMock(
        Neo4jExportSink=mock_sink_cls,
        Neo4jSinkConfig=mock_config_cls,
    )
    mock_sdk = MagicMock(sinks=mock_sinks)
    mock_sdk.get_variables.return_value = [
        Variable(variable_name="x", variable_type="int", form_id=1, form_key="F1")
    ]
    mock_sdk.records.list.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"x": 1})
    ]

    fake_conn = SimpleNamespace(
        conn_type="bolt+s",
        host="neo4j.internal",
        port=7687,
        login="neo4j",
        password="conn_neo_password",  # pragma: allowlist secret
        schema="conn_graph_db",
        extra={},
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_conn if conn_id == "neo4j_dest" else None),
    )

    op = ImednetExportOperator(
        task_id="test_neo4j_export",
        study_key="STUDY-NEO",
        destination="neo4j",
        destination_conn_id="neo4j_dest",
        batch_size=100,
        max_retries=5,
        idempotent=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=88.0,
        export_kwargs={"database": "clinical_graph", "retry_backoff": 0.5},
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    assert "auth" not in op.export_kwargs
    op.execute({})

    mock_config_cls.assert_called_once_with(
        study_key="STUDY-NEO",
        batch_size=100,
        max_retries=5,
        retry_backoff=0.5,
        idempotent=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=88.0,
        extra=op._get_runtime_export_kwargs(),
        uri="bolt+s://neo4j.internal:7687",
        auth=("neo4j", "conn_neo_password"),  # pragma: allowlist secret
        database="clinical_graph",
    )
    assert "auth" not in mock_config_cls.call_args.kwargs["extra"]
    mock_sink_cls.assert_called_once_with(config=mock_config_cls.return_value)
    mock_sink_instance.__enter__.assert_called_once()
    mock_sink_instance.write_batch.assert_called()


def test_operator_resolves_mongodb_sink_via_sdk_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify operator resolves MongoDbExportSink credentials from destination_conn_id without leaking secrets into export_kwargs."""
    from types import SimpleNamespace

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    mock_sink_instance = MagicMock()
    mock_sink_cls = MagicMock(return_value=mock_sink_instance)
    mock_config_cls = MagicMock()

    from imednet.models.records import Record
    from imednet.models.variables import Variable

    mock_sinks = MagicMock(
        MongoDbExportSink=mock_sink_cls,
        MongoDbSinkConfig=mock_config_cls,
    )
    mock_sdk = MagicMock(sinks=mock_sinks)
    mock_sdk.get_variables.return_value = [
        Variable(variable_name="x", variable_type="int", form_id=1, form_key="F1")
    ]
    mock_sdk.records.list.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"x": 1})
    ]

    fake_conn = SimpleNamespace(
        conn_type="mongo",
        host="mongo.internal",
        port=27017,
        login="loader",
        password="mongo_secret_value",  # pragma: allowlist secret
        schema="clinical",
        extra={"authSource": "admin"},
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_conn if conn_id == "mongo_dest" else None),
    )

    op = ImednetExportOperator(
        task_id="test_mongodb_export",
        study_key="STUDY-MONGO",
        destination="mongodb",
        destination_conn_id="mongo_dest",
        batch_size=50,
        quality_gate_enabled=True,
        min_schema_readiness_score=95.0,
        export_kwargs={
            "collection": "records",
        },
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    op.execute({})

    mock_config_cls.assert_called_once_with(
        study_key="STUDY-MONGO",
        batch_size=50,
        max_retries=3,
        retry_backoff=1.0,
        idempotent=True,
        quality_gate_enabled=True,
        min_schema_readiness_score=95.0,
        extra=op._get_runtime_export_kwargs(),
        uri="mongodb://loader:mongo_secret_value@mongo.internal:27017?authSource=admin",  # pragma: allowlist secret
        database="clinical",
        collection="records",
    )
    assert "uri" not in op.export_kwargs
    assert "password" not in mock_config_cls.call_args.kwargs["extra"]
    mock_sink_cls.assert_called_once_with(config=mock_config_cls.return_value)
    mock_sink_instance.__enter__.assert_called_once()
    mock_sink_instance.write_batch.assert_called()


@pytest.mark.parametrize(
    ("destination", "label", "extra_name"),
    [
        ("snowflake", "Snowflake", "snowflake"),
        ("neo4j", "Neo4j", "neo4j"),
        ("mongodb", "MongoDB", "mongodb"),
    ],
)
def test_operator_missing_sink_factory_raises_airflow_exception(
    destination: str,
    label: str,
    extra_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that when a specific sink class is missing on sdk.sinks, an actionable error is raised."""
    from apache_airflow_providers_imednet._airflow_compat import AirflowException
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    class IncompleteSinks:
        pass

    mock_sdk = MagicMock(sinks=IncompleteSinks())
    op = ImednetExportOperator(
        task_id=f"test_missing_{destination}_factory",
        study_key="STUDY-123",
        destination=destination,
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: mock_sdk)

    with pytest.raises(
        AirflowException,
        match=(
            rf"{label} sink is not available in the installed sinks plugin\. "
            rf"Please install the required sink dependencies: pip install 'imednet-plugins-sinks\[{extra_name}\]'"
        ),
    ):
        op.execute({})


def test_resolve_sink_direct_fallbacks_and_legacy_export_func(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _resolve_sink handles non-sink destinations, lazy SDK lookup, and export_func prefix stripping."""
    from apache_airflow_providers_imednet import export
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.spi import sink_base

    cfg = sink_base.SinkConfig(study_key="STUDY-FALLBACK")

    # 1. Non-sink destination returns None without touching SDK
    csv_op = ImednetExportOperator(
        task_id="test_default_csv",
        study_key="STUDY-FALLBACK",
        output_path="/tmp/default.csv",
    )
    assert csv_op._resolve_sink(cfg) is None

    mock_sdk = MagicMock(sinks=None)
    mock_csv = MagicMock()
    monkeypatch.setattr(csv_op, "_get_sdk", lambda: mock_sdk)
    monkeypatch.setattr(export, "export_to_csv", mock_csv)
    assert csv_op.execute({}) == "/tmp/default.csv"
    mock_csv.assert_called_once_with(mock_sdk, "STUDY-FALLBACK", "/tmp/default.csv")

    # 2. Legacy tabular export_func not in _TABULAR_EXPORTS (export_to_sql_by_form)
    sql_by_form_op = ImednetExportOperator(
        task_id="test_sql_by_form",
        study_key="STUDY-FALLBACK",
        export_func="export_to_sql_by_form",
        output_path="sqlite:///:memory:",
    )
    mock_sql_by_form = MagicMock()
    monkeypatch.setattr(sql_by_form_op, "_get_sdk", lambda: mock_sdk)
    monkeypatch.setattr(export, "export_to_sql_by_form", mock_sql_by_form)
    assert sql_by_form_op.execute({}) == "sqlite:///:memory:"
    mock_sql_by_form.assert_called_once_with(mock_sdk, "STUDY-FALLBACK", "sqlite:///:memory:")

    # 3. _resolve_sink with export_func='export_to_neo4j' and sdk=None resolves via _get_sdk()
    mock_sink_cls = MagicMock()
    mock_config_cls = MagicMock()
    neo_sdk = MagicMock(
        sinks=MagicMock(Neo4jExportSink=mock_sink_cls, Neo4jSinkConfig=mock_config_cls)
    )
    neo_op = ImednetExportOperator(
        task_id="test_legacy_neo4j_func",
        study_key="STUDY-FALLBACK",
        export_func="export_to_neo4j",
    )
    monkeypatch.setattr(neo_op, "_get_sdk", lambda: neo_sdk)
    resolved = neo_op._resolve_sink(cfg)
    assert resolved is mock_sink_cls.return_value


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"password": "raw_password"},  # pragma: allowlist secret
        {"auth": ("neo4j", "raw_password")},  # pragma: allowlist secret
        {"api_key": "raw_api_key"},  # pragma: allowlist secret
        {"security_key": "raw_security_key"},  # pragma: allowlist secret
        {"access_token": "raw_token"},  # pragma: allowlist secret
        {"private_key": "raw_pk"},  # pragma: allowlist secret
        {
            "uri": "mongodb://admin:secret123@mongo.internal:27017/clinical"  # pragma: allowlist secret
        },
        {
            "conn_str": "postgresql://loader:secret123@pg.internal:5432/clinical"  # pragma: allowlist secret
        },
        {"nested": {"client_secret": "nested_secret"}},  # pragma: allowlist secret
    ],
)
def test_template_fields_reject_raw_secrets_in_export_kwargs(
    bad_kwargs: dict[str, object],
) -> None:
    """Confirm export_kwargs rejects raw credentials at init, template render assignment, and dict mutation."""
    from apache_airflow_providers_imednet._airflow_compat import AirflowException
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    with pytest.raises(AirflowException, match="destination_conn_id"):
        ImednetExportOperator(
            task_id="reject_init_secrets",
            study_key="STUDY-SEC",
            destination="snowflake",
            export_kwargs=bad_kwargs,
        )

    op = ImednetExportOperator(
        task_id="reject_rendered_secrets",
        study_key="STUDY-SEC",
        destination="snowflake",
        export_kwargs={"table": "SAFE_TABLE"},
    )
    with pytest.raises(AirflowException, match="destination_conn_id"):
        op.export_kwargs = bad_kwargs
    assert dict(op.export_kwargs) == {"table": "SAFE_TABLE"}

    with pytest.raises(AirflowException, match="destination_conn_id"):
        op.export_kwargs.update(bad_kwargs)
    assert dict(op.export_kwargs) == {"table": "SAFE_TABLE"}

    first_key, first_val = next(iter(bad_kwargs.items()))
    with pytest.raises(AirflowException, match="destination_conn_id"):
        op.export_kwargs[first_key] = first_val
    assert dict(op.export_kwargs) == {"table": "SAFE_TABLE"}

    with pytest.raises(AirflowException, match="destination_conn_id"):
        op.export_kwargs.setdefault(first_key, first_val)
    assert dict(op.export_kwargs) == {"table": "SAFE_TABLE"}


def test_template_fields_reject_embedded_uri_credentials_in_output_path() -> None:
    """Confirm output_path rejects embedded URI credentials at init and template render time."""
    from apache_airflow_providers_imednet._airflow_compat import AirflowException
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    secret_uri = (
        "postgresql://loader:supersecret@db.internal:5432/clinical"  # pragma: allowlist secret
    )
    with pytest.raises(AirflowException, match="destination_conn_id"):
        ImednetExportOperator(
            task_id="reject_output_path_init",
            study_key="STUDY-SEC",
            destination="sql",
            output_path=secret_uri,
        )

    op = ImednetExportOperator(
        task_id="reject_output_path_render",
        study_key="STUDY-SEC",
        destination="sql",
        output_path="clinical_records",
    )
    with pytest.raises(AirflowException, match="destination_conn_id"):
        op.output_path = secret_uri
    assert op.output_path == "clinical_records"


def test_operator_resolves_sql_and_sql_by_form_via_destination_conn_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify SQL exports resolve connection strings at runtime via destination_conn_id without persisting credentials in operator fields."""
    from types import SimpleNamespace

    from apache_airflow_providers_imednet import export
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    fake_pg_conn = SimpleNamespace(
        conn_type="postgres",
        host="pg.internal",
        port=5432,
        schema="clinical_wh",
        login="etl_user",
        password="pg_secret_password",  # pragma: allowlist secret
        extra={"table": "default_records"},
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_pg_conn if conn_id == "pg_dest" else None),
    )

    mock_sdk = MagicMock(sinks=None)
    mock_sql = MagicMock()
    monkeypatch.setattr(export, "export_to_sql", mock_sql)

    sql_op = ImednetExportOperator(
        task_id="test_sql_conn_export",
        study_key="STUDY-SQL",
        destination="sql",
        destination_conn_id="pg_dest",
        output_path="study_records",
        batch_size=200,
        max_retries=4,
        idempotent=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=93.0,
    )
    monkeypatch.setattr(sql_op, "_get_sdk", lambda: mock_sdk)

    result = sql_op.execute({})
    assert result == "study_records"
    assert "conn_str" not in sql_op.export_kwargs
    mock_sql.assert_called_once_with(
        mock_sdk,
        "STUDY-SQL",
        "study_records",
        conn_str="postgresql://etl_user:pg_secret_password@pg.internal:5432/clinical_wh",  # pragma: allowlist secret
        batch_size=200,
        max_retries=4,
        idempotent=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=93.0,
    )

    # Also verify export_to_sql_by_form resolves conn_str via destination_conn_id and returns redacted XCom URI
    mock_sql_by_form = MagicMock()
    monkeypatch.setattr(export, "export_to_sql_by_form", mock_sql_by_form)
    by_form_op = ImednetExportOperator(
        task_id="test_sql_by_form_conn",
        study_key="STUDY-SQL",
        export_func="export_to_sql_by_form",
        destination_conn_id="pg_dest",
        batch_size=150,
    )
    monkeypatch.setattr(by_form_op, "_get_sdk", lambda: mock_sdk)

    by_form_result = by_form_op.execute({})
    assert "pg_secret_password" not in str(by_form_result)
    assert "***" in str(by_form_result)
    mock_sql_by_form.assert_called_once_with(
        mock_sdk,
        "STUDY-SQL",
        "postgresql://etl_user:pg_secret_password@pg.internal:5432/clinical_wh",  # pragma: allowlist secret
        batch_size=150,
    )


def test_operator_forwards_quality_gate_and_retry_options_to_sink_and_tabular_configs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify SinkConfig and TabularSinkConfig receive all configured quality gate, batch_size, idempotent, and retry values."""
    import pandas as pd

    import imednet.integrations.export as core_export
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    df = pd.DataFrame([{"A": 1}])
    mapper_inst = MagicMock()
    mapper_inst._fetch_variable_metadata.return_value = (["A"], {"A": "A"})
    mapper_inst._build_record_model.return_value = MagicMock()
    mapper_inst._iter_records.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"A": 1})
    ]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df
    monkeypatch.setattr(core_export, "_record_mapper", lambda: MagicMock(return_value=mapper_inst))

    sdk = MagicMock(sinks=None)
    sdk.get_variables.return_value = [
        Variable(variable_name="A", variable_type="int", form_id=1, form_key="F1")
    ]

    captured_tabular_configs: list[core_export.TabularSinkConfig] = []
    orig_build = core_export._build_tabular_sink_config

    def spy_build(*args: object, **kwargs: object) -> core_export.TabularSinkConfig:
        cfg = orig_build(*args, **kwargs)  # type: ignore[arg-type]
        captured_tabular_configs.append(cfg)
        return cfg

    monkeypatch.setattr(core_export, "_build_tabular_sink_config", spy_build)

    csv_target = tmp_path / "operator_tabular.csv"
    op = ImednetExportOperator(
        task_id="test_tabular_config_forwarding",
        study_key="STUDY-TAB",
        destination="csv",
        output_path=str(csv_target),
        batch_size=321,
        max_retries=6,
        idempotent=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=87.5,
        export_kwargs={"sep": "|"},
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: sdk)

    result = op.execute({})
    assert result == str(csv_target)
    assert csv_target.exists()
    assert len(captured_tabular_configs) == 1
    tab_cfg = captured_tabular_configs[0]
    assert tab_cfg.study_key == "STUDY-TAB"
    assert tab_cfg.batch_size == 321
    assert tab_cfg.max_retries == 6
    assert tab_cfg.idempotent is False
    assert tab_cfg.quality_gate_enabled is True
    assert tab_cfg.min_schema_readiness_score == 87.5


def test_hook_resolve_destination_connection_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise destination connection resolution across SQLite, get_uri, MongoDB SRV, and Neo4j extras."""
    from types import SimpleNamespace

    from apache_airflow_providers_imednet.hooks import ImednetHook

    conns = {
        "sqlite_conn": SimpleNamespace(
            conn_type="sqlite",
            host=None,
            schema="local.db",
            login=None,
            password=None,
            port=None,
            extra={},
        ),
        "uri_conn": SimpleNamespace(
            conn_type="postgres",
            extra={"table": "from_extra"},
            get_uri=lambda: "postgres://u:p@pg:5432/db",  # pragma: allowlist secret
        ),
        "srv_mongo": SimpleNamespace(
            conn_type="mongodb+srv",
            host="cluster0.example.mongodb.net",
            port="27017",
            login="atlas",
            password=None,
            schema="study_db",
            extra={"srv": True, "collection": "forms"},
        ),
        "neo_extra": SimpleNamespace(
            conn_type="generic",
            host="graph.internal",
            port="7687",
            login=None,
            password=None,
            schema=None,
            extra={
                "encrypted": True,
                "auth": ["neo4j", "graph_pw"],
                "database": "neo_db",
            },  # pragma: allowlist secret
        ),
    }
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: conns[conn_id]),
    )

    assert ImednetHook.resolve_destination_connection("sqlite_conn", "sql") == {
        "conn_str": "sqlite:///local.db"
    }
    assert ImednetHook.resolve_destination_connection("uri_conn", "sql") == {
        "conn_str": "postgresql://u:p@pg:5432/db",  # pragma: allowlist secret
        "table": "from_extra",
    }
    assert ImednetHook.resolve_destination_connection("srv_mongo", "mongodb") == {
        "uri": "mongodb+srv://atlas@cluster0.example.mongodb.net",
        "user": "atlas",
        "database": "study_db",
        "collection": "forms",
    }
    assert ImednetHook.resolve_destination_connection("neo_extra", "neo4j") == {
        "uri": "bolt+s://graph.internal:7687",
        "auth": ("neo4j", "graph_pw"),  # pragma: allowlist secret
        "database": "neo_db",
    }
    assert ImednetHook.resolve_destination_connection("sqlite_conn", "csv") == {}
