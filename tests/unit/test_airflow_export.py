"""Unit tests for airflow export."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tests.utils.airflow_mocks import setup_dummy_airflow


# Setup dummy airflow modules to prevent import errors if airflow is not installed
def _setup_airflow(monkeypatch):
    """Helper function to setup airflow."""
    setup_dummy_airflow(monkeypatch)


EXPORT_FUNCTIONS = [
    "export_to_csv",
    "export_to_duckdb",
    "export_to_parquet",
    "export_to_excel",
    "export_to_json",
    "export_to_sql",
    "export_to_sql_by_form",
    "export_to_long_sql",
]


@pytest.fixture(autouse=True, scope="module")
def _isolate_provider_modules():
    """Prevent dummy Airflow subclasses from leaking into or out of this module."""
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
            sys.modules.pop(mod, None)
    yield
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
            sys.modules.pop(mod, None)


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
    env = os.environ.copy()
    if env.get("IMEDNET_TEST_WHEELS") != "1":
        package_paths = [
            str(repo_root / "packages" / "providers-airflow" / "src"),
            str(repo_root / "packages" / "core" / "src"),
        ]
        existing_pythonpath = env.get("PYTHONPATH")
        if existing_pythonpath:
            env["PYTHONPATH"] = os.pathsep.join([*package_paths, existing_pythonpath])
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
        "clinical_records",
        quality_gate_enabled=False,
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
    mock_csv_export.assert_called_once_with(
        mock_sdk, "STUDY-CSV", "/tmp/output.csv", quality_gate_enabled=False
    )


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
    mock_csv.assert_called_once_with(
        mock_sdk, "STUDY-FALLBACK", "/tmp/default.csv", quality_gate_enabled=False
    )

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
    mock_sql_by_form.assert_called_once_with(
        mock_sdk, "STUDY-FALLBACK", "sqlite:///:memory:", quality_gate_enabled=False
    )

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
        quality_gate_enabled=False,
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


def test_operator_propagates_mask_phi_opt_out_to_tabular_and_plugin_sinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ImednetExportOperator defaults to mask_phi=True and forwards mask_phi=False to tabular and sink targets."""
    from types import SimpleNamespace

    from apache_airflow_providers_imednet import export
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.models.records import Record
    from imednet.utils.security import global_sensitivity_registry

    registry_snapshot = set(global_sensitivity_registry._sensitive_keys)

    # 1. Tabular opt-out via operator parameter
    mock_csv = MagicMock()
    monkeypatch.setattr(export, "export_to_csv", mock_csv)
    mock_sdk_tab = MagicMock(sinks=None)
    op_csv = ImednetExportOperator(
        task_id="csv_opt_out",
        study_key="STUDY-PHI",
        destination="csv",
        output_path="/tmp/authorized.csv",
        mask_phi=False,
    )
    assert op_csv.mask_phi is False
    monkeypatch.setattr(op_csv, "_get_sdk", lambda: mock_sdk_tab)
    op_csv.execute({})
    mock_csv.assert_called_once_with(
        mock_sdk_tab,
        "STUDY-PHI",
        "/tmp/authorized.csv",
        mask_phi=False,
        quality_gate_enabled=False,
    )

    # 2. Neo4j sink opt-out via operator parameter and MongoDB via export_kwargs
    mock_sink_instance = MagicMock()
    mock_neo_cls = MagicMock(return_value=mock_sink_instance)
    mock_neo_cfg = MagicMock()
    mock_mongo_cls = MagicMock(return_value=mock_sink_instance)
    mock_mongo_cfg = MagicMock()
    mock_sinks = MagicMock(
        Neo4jExportSink=mock_neo_cls,
        Neo4jSinkConfig=mock_neo_cfg,
        MongoDbExportSink=mock_mongo_cls,
        MongoDbSinkConfig=mock_mongo_cfg,
    )
    mock_sdk_sinks = MagicMock(sinks=mock_sinks)
    mock_sdk_sinks.records.list.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"SSN": "123-45-6789"})
    ]

    conns = {
        "neo_dest": SimpleNamespace(
            conn_type="bolt",
            host="neo4j.local",
            port=7687,
            login="neo4j",
            password="pw",  # pragma: allowlist secret
            schema="neo4j",
            extra={},
        ),
        "mongo_dest": SimpleNamespace(
            conn_type="mongo",
            host="mongo.local",
            port=27017,
            login=None,
            password=None,
            schema="clinical",
            extra={"collection": "raw_records"},
        ),
    }
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: conns[conn_id]),
    )

    op_neo = ImednetExportOperator(
        task_id="neo_opt_out",
        study_key="STUDY-PHI",
        destination="neo4j",
        destination_conn_id="neo_dest",
        mask_phi=False,
    )
    monkeypatch.setattr(op_neo, "_get_sdk", lambda: mock_sdk_sinks)
    op_neo.execute({})
    assert mock_neo_cfg.call_args.kwargs["mask_phi"] is False

    op_mongo = ImednetExportOperator(
        task_id="mongo_opt_out",
        study_key="STUDY-PHI",
        destination="mongodb",
        destination_conn_id="mongo_dest",
        export_kwargs={"mask_phi": False},
    )
    monkeypatch.setattr(op_mongo, "_get_sdk", lambda: mock_sdk_sinks)
    op_mongo.execute({})
    assert mock_mongo_cfg.call_args.kwargs["mask_phi"] is False
    assert global_sensitivity_registry._sensitive_keys == registry_snapshot


def test_operator_forwards_snowflake_prefix_and_hard_delete_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ImednetExportOperator._resolve_sink forwards stage_prefix, prefix, local_staging_dir, manifest_path, and hard_delete."""
    from types import SimpleNamespace

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.spi import sink_base

    mock_sink_cls = MagicMock()
    mock_config_cls = MagicMock()
    mock_sdk = MagicMock(
        sinks=SimpleNamespace(
            SnowflakeExportSink=mock_sink_cls,
            SnowflakeSinkConfig=mock_config_cls,
        )
    )

    fake_conn = SimpleNamespace(
        host="conn_acct",
        login="conn_usr",
        password="conn_pwd",  # pragma: allowlist secret
        schema="PUBLIC",
        extra={
            "database": "DB",
            "warehouse": "WH",
            "stage": "STG",
            "table": "TBL",
            "hard_delete": "true",
        },
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_conn if conn_id == "sf_conn" else None),
    )

    # 1. Explicit export_kwargs forwarding for prefix, local_staging_dir, manifest_path, and hard_delete
    op_kw = ImednetExportOperator(
        task_id="sf_forward_kwargs",
        study_key="STUDY-SF-FWD",
        destination="snowflake",
        destination_conn_id="sf_conn",
        export_kwargs={
            "prefix": "imednet/custom_prefix",
            "local_staging_dir": "/tmp/custom_stage",
            "manifest_path": "/tmp/custom_manifest.jsonl",
            "hard_delete": False,
        },
    )
    op_kw._resolve_sink(sink_base.SinkConfig(study_key="STUDY-SF-FWD"), sdk=mock_sdk)
    kw_passed = mock_config_cls.call_args.kwargs
    assert kw_passed["stage_prefix"] == "imednet/custom_prefix"
    assert kw_passed["prefix"] == "imednet/custom_prefix"
    assert kw_passed["local_staging_dir"] == "/tmp/custom_stage"
    assert kw_passed["manifest_path"] == "/tmp/custom_manifest.jsonl"
    assert kw_passed["hard_delete"] is False

    # 2. Connection extra fallback for hard_delete=True and explicit stage_prefix
    mock_config_cls.reset_mock()
    op_conn = ImednetExportOperator(
        task_id="sf_forward_conn",
        study_key="STUDY-SF-FWD",
        destination="snowflake",
        destination_conn_id="sf_conn",
        export_kwargs={"stage_prefix": "imednet/stage_pfx"},
    )
    op_conn._resolve_sink(sink_base.SinkConfig(study_key="STUDY-SF-FWD"), sdk=mock_sdk)
    conn_passed = mock_config_cls.call_args.kwargs
    assert conn_passed["stage_prefix"] == "imednet/stage_pfx"
    assert conn_passed["prefix"] is None
    assert conn_passed["hard_delete"] is True


def test_operator_repeated_snowflake_reruns_merge_evolve_and_handle_deletes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeated ImednetExportOperator.execute runs generate run-scoped batch IDs, evolve schema, and merge/delete without duplicates."""
    import re
    from types import SimpleNamespace

    import pyarrow.parquet as pq

    import imednet_sinks.warehouse as wh_mod
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.models.records import Record

    sf_mod = MagicMock()
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    sf_mod.connect.return_value = conn

    orig_require = wh_mod._require_optional_dep
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, extras: sf_mod if pkg == "snowflake.connector" else orig_require(pkg, extras),
    )

    known_cols: list[str] = []
    warehouse_rows: dict[tuple[str, int], dict[str, object]] = {}
    staged_rows: list[dict[str, object]] = []
    stage_paths: list[str] = []

    def fake_execute(sql: str, params: object = None) -> None:
        nonlocal staged_rows
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            cursor.fetchall.return_value = [(c,) for c in known_cols]
        elif sql.startswith("CREATE TABLE IF NOT EXISTS"):
            for col in re.findall(r'"([A-Z0-9_$]+)"\s+[A-Z_]+', sql.split("(", 1)[1]):
                if col not in known_cols:
                    known_cols.append(col)
            cursor.fetchall.return_value = []
        elif sql.startswith("ALTER TABLE"):
            for col in re.findall(r'"([A-Z0-9_$]+)"\s+[A-Z_]+', sql.split("ADD COLUMN", 1)[1]):
                if col not in known_cols:
                    known_cols.append(col)
            cursor.fetchall.return_value = []
        elif sql.startswith("PUT file://"):
            local_file = sql.split("PUT file://", 1)[1].split(" ", 1)[0]
            staged_rows = pq.read_table(local_file).to_pylist()
            cursor.fetchall.return_value = []
        elif sql.startswith("COPY INTO") and "__imednet_stage" in sql:
            stage_paths.append(sql.split("FROM '", 1)[1].split("'", 1)[0])
            cursor.fetchall.return_value = []
        elif sql.startswith("MERGE INTO"):
            hard_del = "THEN DELETE" in sql
            for row in staged_rows:
                key = (str(row["study_key"]), int(row["record_id"]))
                is_del = bool(row.get("deleted"))
                if key in warehouse_rows:
                    if hard_del and is_del:
                        del warehouse_rows[key]
                    else:
                        warehouse_rows[key].update(row)
                elif not (hard_del and is_del):
                    warehouse_rows[key] = dict(row)
            cursor.fetchall.return_value = []
        elif sql.startswith("DELETE FROM"):
            assert isinstance(params, list) and len(params) >= 2
            study_val = str(params[0])
            for rid in params[1:]:
                warehouse_rows.pop((study_val, int(rid)), None)
            cursor.fetchall.return_value = []
        else:
            cursor.fetchall.return_value = []

    cursor.execute.side_effect = fake_execute

    fake_conn = SimpleNamespace(
        host="acct",
        login="loader",
        password="secret",  # pragma: allowlist secret
        schema="PUBLIC",
        extra={
            "database": "DB",
            "warehouse": "WH",
            "stage": "STG",
            "table": "RECORDS",
        },
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_conn if conn_id == "sf_dest" else None),
    )

    sdk = MagicMock(
        sinks=SimpleNamespace(
            SnowflakeExportSink=wh_mod.SnowflakeExportSink,
            SnowflakeSinkConfig=wh_mod.SnowflakeSinkConfig,
        )
    )

    run1 = [
        Record(
            record_id=1,
            form_id=10,
            form_key="F1",
            subject_key="S1",
            deleted=False,
            date_modified="2026-04-01T08:00:00Z",
            record_data={"score": 10},
        ),
        Record(
            record_id=2,
            form_id=10,
            form_key="F1",
            subject_key="S2",
            deleted=False,
            date_modified="2026-04-01T08:05:00Z",
            record_data={"score": 20},
        ),
    ]
    run2 = [
        Record(
            record_id=1,
            form_id=10,
            form_key="F1",
            subject_key="S1",
            deleted=False,
            date_modified="2026-04-02T08:00:00Z",
            record_data={"score": 15, "newBiomarker": "POS"},
        ),
        Record(
            record_id=2,
            form_id=10,
            form_key="F1",
            subject_key="S2",
            deleted=True,
            date_modified="2026-04-02T08:05:00Z",
            record_data={"score": 20, "newBiomarker": "NEG"},
        ),
    ]

    manifest_file = tmp_path / "manifest.jsonl"
    op_default = ImednetExportOperator(
        task_id="daily_sf_sync_default",
        study_key="STUDY_AF_SF",
        destination="snowflake",
        destination_conn_id="sf_dest",
        export_kwargs={
            "stage_prefix": "imednet/airflow",
            "local_staging_dir": str(tmp_path / "stage_default"),
            "manifest_path": str(manifest_file),
            "include_deleted": False,
            "hard_delete": False,
        },
    )
    monkeypatch.setattr(op_default, "_get_sdk", lambda: sdk)

    sdk.records.list.side_effect = lambda **_kw: iter(run1)
    op_default.execute({"run_id": "scheduled__2026-04-01T00:00:00+00:00"})

    unmatched_voided = Record(
        record_id=99,
        form_id=10,
        form_key="F1",
        subject_key="S99",
        deleted=True,
        date_modified="2026-04-02T08:10:00Z",
        record_data={"score": 0, "newBiomarker": "NA"},
    )
    sdk.records.list.side_effect = lambda **_kw: iter([*run2, unmatched_voided])
    op_default.execute({"run_id": "scheduled__2026-04-02T00:00:00+00:00"})

    assert len(stage_paths) == 2
    assert stage_paths[0] != stage_paths[1]
    assert "NEW_BIOMARKER" in known_cols
    assert list(warehouse_rows.keys()) == [("STUDY_AF_SF", 1)]
    assert warehouse_rows[("STUDY_AF_SF", 1)]["score"] == 15
    assert warehouse_rows[("STUDY_AF_SF", 1)]["new_biomarker"] == "POS"

    # Explicit include_deleted=True, hard_delete=False retains deleted=True tombstones
    op_soft = ImednetExportOperator(
        task_id="daily_sf_sync_soft",
        study_key="STUDY_AF_SF",
        destination="snowflake",
        destination_conn_id="sf_dest",
        export_kwargs={
            "stage_prefix": "imednet/airflow",
            "local_staging_dir": str(tmp_path / "stage_soft"),
            "include_deleted": True,
            "hard_delete": False,
        },
    )
    monkeypatch.setattr(op_soft, "_get_sdk", lambda: sdk)
    sdk.records.list.side_effect = lambda **_kw: iter(run1)
    op_soft.execute({"run_id": "scheduled__2026-04-02T12:00:00+00:00"})
    sdk.records.list.side_effect = lambda **_kw: iter(run2)
    op_soft.execute({"run_id": "scheduled__2026-04-02T18:00:00+00:00"})
    assert len(warehouse_rows) == 2
    assert warehouse_rows[("STUDY_AF_SF", 2)]["deleted"] is True

    # Run with include_deleted=True, hard_delete=True removes matched deleted=True record 2
    op_hard = ImednetExportOperator(
        task_id="daily_sf_sync_hard",
        study_key="STUDY_AF_SF",
        destination="snowflake",
        destination_conn_id="sf_dest",
        export_kwargs={
            "stage_prefix": "imednet/airflow",
            "local_staging_dir": str(tmp_path / "stage_hard"),
            "include_deleted": True,
            "hard_delete": True,
        },
    )
    monkeypatch.setattr(op_hard, "_get_sdk", lambda: sdk)
    sdk.records.list.side_effect = lambda **_kw: iter([*run2, unmatched_voided])
    op_hard.execute({"run_id": "scheduled__2026-04-03T00:00:00+00:00"})

    assert list(warehouse_rows.keys()) == [("STUDY_AF_SF", 1)]


def test_airflow_export_operator_sdtm_standards_gating_and_naming_convention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ImednetExportOperator exposes SDTM, standards_profile/min_standards_score gating, and naming_convention."""
    import pandas as pd

    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.errors import ExportError
    from imednet.models.forms import Form
    from imednet.models.records import Record

    sdk = MagicMock()
    sdk.forms.list.return_value = [Form(form_id=1, form_key="AE", form_name="Adverse Events")]
    sdk.variables.list.return_value = []

    # 1. Non-conforming study gated before write when standards_profile='drug' and min_standards_score=95
    bad_records = [
        Record(
            record_id=1,
            study_key="AF_GATED",
            form_id=1,
            form_key="AE",
            subject_key="S01",
            record_data={"aeTerm": "Headache", "aeSeverity": "SEVERE"},
        )
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(bad_records)

    blocked_csv = tmp_path / "af_blocked.csv"
    op_blocked = ImednetExportOperator(
        task_id="blocked_sdtm_ae",
        study_key="AF_GATED",
        destination="csv",
        output_path=str(blocked_csv),
        as_sdtm=True,
        domain="AE",
        standards_profile="drug",
        min_standards_score=95.0,
    )
    monkeypatch.setattr(op_blocked, "_get_sdk", lambda: sdk)
    with pytest.raises(ExportError, match="Standards readiness check failed"):
        op_blocked.execute({})
    assert not blocked_csv.exists()

    # 2. Conforming study succeeds with SDTM AE output
    good_records = [
        Record(
            record_id=1,
            study_key="AF_READY",
            form_id=1,
            form_key="AE",
            subject_key="S01",
            record_data={
                "aeTerm": "Headache",
                "aeDecod": "HEADACHE",
                "aeStartDate": "2026-04-01",
                "aeSeverity": "1",
                "aeRelationship": "POSSIBLE",
                "aeActionTaken": "NONE",
                "aeOutcome": "RECOVERED",
            },
        )
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(good_records)
    ready_pq = tmp_path / "af_ready_ae.parquet"
    op_ready = ImednetExportOperator(
        task_id="ready_sdtm_ae",
        study_key="AF_READY",
        destination="parquet",
        output_path=str(ready_pq),
        as_sdtm=True,
        domain="AE",
        standards_profile="drug",
        min_standards_score=95.0,
        naming_convention="sdtm",
    )
    monkeypatch.setattr(op_ready, "_get_sdk", lambda: sdk)
    op_ready.execute({})
    out_df = pd.read_parquet(ready_pq)
    assert list(out_df["DOMAIN"]) == ["AE"]
    assert list(out_df["AESEQ"]) == [1]
    assert list(out_df["AETERM"]) == ["Headache"]


def test_export_operator_duckdb_default_table_custom_table_and_conn_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify DuckDB operator/hook resolution across default table, custom table, and destination_conn_id."""
    from types import SimpleNamespace

    import duckdb

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    conn_obj = SimpleNamespace(
        host=str(tmp_path / "from_conn.duckdb"),
        schema=None,
        extra_dejson={"table": "conn_records"},
    )
    monkeypatch.setattr(
        ImednetHook, "_get_airflow_connection", classmethod(lambda cls, cid: conn_obj)
    )
    resolved = ImednetHook.resolve_destination_connection("duckdb_target", "duckdb")
    assert resolved == {
        "db_path": str(tmp_path / "from_conn.duckdb"),
        "table_name": "conn_records",
    }

    records = [
        Record.model_validate(
            {
                "studyKey": "STUDY_DUCK",
                "recordId": 101,
                "subjectId": 1,
                "subjectKey": "SUBJ-01",
                "formKey": "DEMO",
                "dateModified": "2026-01-10T00:00:00Z",
                "recordData": {"AGE": 44, "PATIENT_NAME": "Alice Doe"},
            }
        ),
        Record.model_validate(
            {
                "studyKey": "STUDY_DUCK",
                "recordId": 102,
                "subjectId": 2,
                "subjectKey": "SUBJ-02",
                "formKey": "DEMO",
                "dateModified": "2026-01-11T00:00:00Z",
                "recordData": {"AGE": 51, "PATIENT_NAME": "Bob Roe"},
            }
        ),
    ]
    variables = [
        Variable.model_validate(
            {
                "studyKey": "STUDY_DUCK",
                "variableId": 1,
                "variableName": "AGE",
                "formId": 10,
                "formKey": "DEMO",
                "formName": "Demographics",
                "label": "Age",
            }
        ),
        Variable.model_validate(
            {
                "studyKey": "STUDY_DUCK",
                "variableId": 2,
                "variableName": "PATIENT_NAME",
                "formId": 10,
                "formKey": "DEMO",
                "formName": "Demographics",
                "label": "Patient Name",
            }
        ),
    ]

    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: iter(records)
    sdk.get_variables.return_value = variables
    sdk.variables.list.return_value = variables
    sdk.studies.get_configuration = MagicMock(side_effect=AttributeError)

    default_db = tmp_path / "default_tbl.duckdb"
    op_default = ImednetExportOperator(
        task_id="duckdb_default",
        study_key="STUDY_DUCK",
        destination="duckdb",
        output_path=str(default_db),
    )
    monkeypatch.setattr(op_default, "_get_sdk", lambda: sdk)
    assert op_default.execute({}) == str(default_db)

    custom_db = tmp_path / "custom_tbl.duckdb"
    op_custom = ImednetExportOperator(
        task_id="duckdb_custom_table",
        study_key="STUDY_DUCK",
        destination="duckdb",
        output_path=str(custom_db),
        export_kwargs={"table": "study_records_v1"},
    )
    monkeypatch.setattr(op_custom, "_get_sdk", lambda: sdk)
    assert op_custom.execute({}) == str(custom_db)

    op_conn = ImednetExportOperator(
        task_id="duckdb_conn",
        study_key="STUDY_DUCK",
        destination="duckdb",
        destination_conn_id="duckdb_target",
    )
    monkeypatch.setattr(op_conn, "_get_sdk", lambda: sdk)
    assert op_conn.execute({}) == str(tmp_path / "from_conn.duckdb")

    for db_file, tbl_name in [
        (default_db, "records"),
        (custom_db, "study_records_v1"),
        (tmp_path / "from_conn.duckdb", "conn_records"),
    ]:
        with duckdb.connect(str(db_file), read_only=True, config={"threads": "1"}) as con:
            rows = con.execute(
                f'SELECT "recordId", "subjectKey", "PATIENT_NAME", "AGE" FROM {tbl_name} ORDER BY "recordId"'
            ).fetchall()
        assert rows == [
            (101, "SUBJ-01", "***MASKED***", 44),
            (102, "SUBJ-02", "***MASKED***", 51),
        ]


def test_airflow_operator_consecutive_incremental_runs_retain_all_10_records_and_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify two consecutive incremental ImednetExportOperator runs retain all 10 records and reconcile deltas."""
    import json
    import sqlite3
    from datetime import UTC, datetime

    import pandas as pd

    pytest.importorskip("pyarrow")
    pytest.importorskip("openpyxl")
    duckdb = pytest.importorskip("duckdb")

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.models.forms import Form
    from imednet.models.intervals import FormSummary, Interval
    from imednet.models.records import Record
    from imednet.models.variables import Variable
    from imednet_workflows.state_ledger import FileStateProvider

    ledger_file = tmp_path / "af_ledger.json"

    vars_list = [
        Variable(
            variable_name="SCORE", label="Score", variable_type="int", form_id=10, form_key="F_VS"
        )
    ]
    forms_list = [
        Form(
            form_id=10,
            form_key="F_VS",
            form_name="Vital Signs",
            form_type="Standard",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
        )
    ]
    intervals_list = [
        Interval(
            interval_id=1,
            interval_name="V1",
            interval_sequence=1,
            interval_description="V1",
            interval_group_name="G1",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
            forms=[FormSummary(form_id=10, form_key="F_VS", form_name="Vital Signs")],
        )
    ]

    upstream_records: dict[int, Record] = {
        rid: Record(
            record_id=rid,
            study_key="STUDY_AF_10",
            subject_key=f"SUBJ-{rid:02d}",
            visit_id=1,
            form_id=10,
            form_key="F_VS",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 12, rid, tzinfo=UTC),
            record_data={"SCORE": rid * 10},
        )
        for rid in range(1, 11)
    }

    def _query_records(**kw: object) -> object:
        from imednet.integrations.sink_base import (
            extract_record_timestamp,
            normalize_watermark_datetime,
        )

        flt = str(kw.get("filter", "") or "")
        items = list(upstream_records.values())
        if kw.get("deleted") is False or "deleted==false" in flt:
            items = [r for r in items if not r.deleted]
        dm = kw.get("dateModified")
        if isinstance(dm, tuple) and len(dm) == 2 and dm[0] == ">=":
            wm_dt = normalize_watermark_datetime(str(dm[1]))
            if wm_dt is not None:
                items = [
                    r
                    for r in items
                    if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                ]
        elif "dateModified>=" in flt:
            wm_part = [p for p in flt.split(";") if p.startswith("dateModified>=")][0]
            wm_str = wm_part.split(">=", 1)[1]
            wm_dt = normalize_watermark_datetime(wm_str)
            if wm_dt is not None:
                items = [
                    r
                    for r in items
                    if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                ]
        return iter([r.model_copy(deep=True) for r in items])

    sdk = MagicMock(sinks=None)
    sdk.get_variables.return_value = vars_list
    sdk.variables.list.return_value = vars_list
    sdk.get_forms.return_value = forms_list
    sdk.forms.list.return_value = forms_list
    sdk.get_intervals.return_value = intervals_list
    sdk.records.list.side_effect = _query_records

    sql_db = tmp_path / "af_records.sqlite"
    monkeypatch.setattr(
        ImednetHook,
        "resolve_destination_connection",
        classmethod(
            lambda cls, cid, dest: {"conn_str": f"sqlite:///{sql_db}"} if dest == "sql" else {}
        ),
    )

    csv_out = tmp_path / "af.csv"
    pq_out = tmp_path / "af.parquet"
    xl_out = tmp_path / "af.xlsx"
    json_out = tmp_path / "af.json"
    hier_out = tmp_path / "af_hier.json"
    ddb_out = tmp_path / "af.duckdb"

    specs = [
        ("csv", str(csv_out), {}, None),
        ("parquet", str(pq_out), {}, None),
        ("excel", str(xl_out), {}, None),
        ("json", str(json_out), {}, None),
        ("json_hier", str(hier_out), {"hierarchical": True}, None),
        ("duckdb", str(ddb_out), {"table_name": "records"}, None),
        ("sql", None, {"table": "records"}, "sql_conn"),
    ]

    ops: list[tuple[str, ImednetExportOperator]] = []
    for label, out_path, extra_kw, dest_conn in specs:
        dest_name = "json" if label == "json_hier" else label
        op = ImednetExportOperator(
            task_id=f"sync_{label}",
            study_key="STUDY_AF_10",
            destination=dest_name,
            output_path=out_path,
            destination_conn_id=dest_conn,
            incremental=True,
            stream_name=f"stream_{label}",
            ledger_path=str(ledger_file),
            export_kwargs=extra_kw,
        )
        monkeypatch.setattr(op, "_get_sdk", lambda: sdk)
        op.execute({"run_id": "run_1"})
        ops.append((label, op))

    # Run 2: zero-delta incremental execution retains all 10 records across every destination
    for _label, op in ops:
        op.execute({"run_id": "run_2"})

    assert sorted(pd.read_csv(csv_out)["recordId"].tolist()) == list(range(1, 11))
    assert sorted(pd.read_parquet(pq_out)["recordId"].tolist()) == list(range(1, 11))
    assert sorted(pd.read_excel(xl_out)["recordId"].tolist()) == list(range(1, 11))
    assert sorted(r["recordId"] for r in json.loads(json_out.read_text(encoding="utf-8"))) == list(
        range(1, 11)
    )
    hier_loaded = json.loads(hier_out.read_text(encoding="utf-8"))
    hier_ids = sorted(
        rec["recordId"]
        for subj in hier_loaded
        for v in subj["visits"]
        for f in v["forms"]
        for rec in f["records"]
    )
    assert hier_ids == list(range(1, 11))
    with duckdb.connect(str(ddb_out), config={"threads": "1"}) as dconn:
        assert [
            r[0]
            for r in dconn.execute(
                'SELECT "recordId" FROM "records" ORDER BY "recordId"'
            ).fetchall()
        ] == list(range(1, 11))
    with sqlite3.connect(sql_db) as sconn:
        assert [
            r[0]
            for r in sconn.execute(
                'SELECT "recordId" FROM "records" ORDER BY "recordId"'
            ).fetchall()
        ] == list(range(1, 11))

    # Run 3: update record 1 and void record 10 (default include_deleted=False prunes voided records)
    upstream_records[1].record_data = {"SCORE": 999}
    upstream_records[1].date_modified = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    upstream_records[10].deleted = True
    upstream_records[10].date_modified = datetime(2026, 6, 2, 12, 1, tzinfo=UTC)

    for label, out_path, extra_kw, dest_conn in specs:
        dest_name = "json" if label == "json_hier" else label
        op3 = ImednetExportOperator(
            task_id=f"sync_{label}_3",
            study_key="STUDY_AF_10",
            destination=dest_name,
            output_path=out_path,
            destination_conn_id=dest_conn,
            incremental=True,
            stream_name=f"stream_{label}",
            ledger_path=str(ledger_file),
            export_kwargs=extra_kw,
        )
        monkeypatch.setattr(op3, "_get_sdk", lambda: sdk)
        op3.execute({"run_id": "run_3"})

    ledger = FileStateProvider(str(ledger_file))
    for label, *_ in specs:
        assert ledger.get_last_timestamp("STUDY_AF_10", f"stream_{label}") == datetime(
            2026, 6, 2, 12, 1, tzinfo=UTC
        )

    csv_df3 = pd.read_csv(csv_out)
    assert sorted(csv_df3["recordId"].tolist()) == list(range(1, 10))
    assert int(csv_df3.loc[csv_df3["recordId"] == 1, "SCORE"].iloc[0]) == 999
    assert sorted(pd.read_parquet(pq_out)["recordId"].tolist()) == list(range(1, 10))
    assert sorted(pd.read_excel(xl_out)["recordId"].tolist()) == list(range(1, 10))
    assert sorted(r["recordId"] for r in json.loads(json_out.read_text(encoding="utf-8"))) == list(
        range(1, 10)
    )
    hier_loaded3 = json.loads(hier_out.read_text(encoding="utf-8"))
    hier_ids3 = sorted(
        rec["recordId"]
        for subj in hier_loaded3
        for v in subj["visits"]
        for f in v["forms"]
        for rec in f["records"]
    )
    assert hier_ids3 == list(range(1, 10))


def test_operator_dispatches_sql_by_form_and_long_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify ImednetExportOperator dispatches sql_by_form and long_sql with connection resolution and URI redaction."""
    from apache_airflow_providers_imednet import export
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import (
        _TABULAR_EXPORTS,
        ImednetExportOperator,
    )

    assert _TABULAR_EXPORTS["sql_by_form"] is export.export_to_sql_by_form
    assert _TABULAR_EXPORTS["long_sql"] is export.export_to_long_sql

    sql_uri = "postgresql://sql_user:sql_secret_password@db.synthetic.example:5432/clinical"  # pragma: allowlist secret
    fake_sql_conn = SimpleNamespace(
        conn_type="postgres",
        host="db.synthetic.example",
        port=5432,
        login="sql_user",
        password="sql_secret_password",  # pragma: allowlist secret
        schema="clinical",
        extra={"table": "long_records"},
        get_uri=lambda: sql_uri,
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_sql_conn if conn_id == "sql_dest" else None),
    )

    mock_sdk = MagicMock()
    mock_by_form = MagicMock()
    mock_long_sql = MagicMock()
    monkeypatch.setattr(export, "export_to_sql_by_form", mock_by_form)
    monkeypatch.setattr(export, "export_to_long_sql", mock_long_sql)

    op_by_form = ImednetExportOperator(
        task_id="export_by_form",
        study_key="STUDY-SQL",
        destination="sql_by_form",
        destination_conn_id="sql_dest",
    )
    monkeypatch.setattr(op_by_form, "_get_sdk", lambda: mock_sdk)
    res_by_form = op_by_form.execute({})
    mock_by_form.assert_called_once_with(
        mock_sdk,
        "STUDY-SQL",
        sql_uri,
        quality_gate_enabled=False,
    )
    assert res_by_form is not None
    assert "sql_secret_password" not in res_by_form
    assert "***" in res_by_form

    op_long = ImednetExportOperator(
        task_id="export_long",
        study_key="STUDY-SQL",
        destination="long_sql",
        output_path="override_long_records",
        destination_conn_id="sql_dest",
        export_kwargs={"table_name": "ignored_duplicate", "if_exists": "append"},
    )
    monkeypatch.setattr(op_long, "_get_sdk", lambda: mock_sdk)
    res_long = op_long.execute({})
    mock_long_sql.assert_called_once_with(
        mock_sdk,
        "STUDY-SQL",
        "override_long_records",
        conn_str=sql_uri,
        if_exists="append",
        quality_gate_enabled=False,
    )
    assert res_long == "override_long_records"


def test_operator_sdtm_phi_redaction_and_incremental_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ImednetExportOperator preserves BRTHDTC='1981'/None and reconciles incremental SDTM deltas."""
    import json
    from datetime import UTC, datetime

    import pandas as pd

    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.models.forms import Form
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    study_key = "STUDY_AF_SDTM"
    upstream = {
        1: Record(
            record_id=1,
            study_key=study_key,
            subject_key="S01",
            form_id=301,
            form_key="F_DM",
            deleted=False,
            date_modified=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            record_data={"BRTHDTC": "1981-05-14", "patientName": "Hidden Name"},
        ),
        2: Record(
            record_id=2,
            study_key=study_key,
            subject_key="S02",
            form_id=301,
            form_key="F_DM",
            deleted=False,
            date_modified=datetime(2026, 6, 1, 10, 1, tzinfo=UTC),
            record_data={"BRTHDTC": None, "patientName": "Hidden Name 2"},
        ),
    }

    sdk = MagicMock(sinks=None)
    forms = [Form(form_id=301, form_key="F_DM", form_name="Demographics", form_type="Enrollment")]
    variables = [
        Variable(
            variable_id=1,
            variable_name="BRTHDTC",
            variable_type="text",
            form_id=301,
            form_key="F_DM",
        ),
        Variable(
            variable_id=2,
            variable_name="patientName",
            variable_type="text",
            form_id=301,
            form_key="F_DM",
        ),
    ]
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.get_intervals.return_value = []
    sdk.records.list.side_effect = lambda **_kw: iter(
        [r.model_copy(deep=True) for r in upstream.values()]
    )

    ledger_path = tmp_path / "af_sdtm_ledger.json"
    xl_path = tmp_path / "af_sdtm.xlsx"
    json_path = tmp_path / "af_sdtm.json"

    for dest, out_path in [("excel", xl_path), ("json", json_path)]:
        op = ImednetExportOperator(
            task_id=f"af_sdtm_{dest}_1",
            study_key=study_key,
            destination=dest,
            output_path=str(out_path),
            as_sdtm=True,
            mask_phi=True,
            incremental=True,
            stream_name=f"af_{dest}",
            ledger_path=str(ledger_path),
        )
        monkeypatch.setattr(op, "_get_sdk", lambda: sdk)
        op.execute({"run_id": "r1"})

    xl_rows = pd.read_excel(xl_path, dtype=str).to_dict(orient="records")
    by_subj_xl = {r["USUBJID"]: r for r in xl_rows}
    assert by_subj_xl["S01"]["BRTHDTC"] == "1981"
    assert pd.isna(by_subj_xl["S02"]["BRTHDTC"])
    assert "recordId" not in by_subj_xl["S01"]

    json_rows = json.loads(json_path.read_text(encoding="utf-8"))
    by_subj_json = {r["USUBJID"]: r for r in json_rows}
    assert by_subj_json["S01"]["BRTHDTC"] == "1981"
    assert by_subj_json["S02"]["BRTHDTC"] is None

    # Void S02 on Run 2 and verify incremental reconciliation removes it cleanly
    upstream[2].deleted = True
    upstream[2].date_modified = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    for dest, out_path in [("excel", xl_path), ("json", json_path)]:
        op2 = ImednetExportOperator(
            task_id=f"af_sdtm_{dest}_2",
            study_key=study_key,
            destination=dest,
            output_path=str(out_path),
            as_sdtm=True,
            mask_phi=True,
            incremental=True,
            stream_name=f"af_{dest}",
            ledger_path=str(ledger_path),
        )
        monkeypatch.setattr(op2, "_get_sdk", lambda: sdk)
        op2.execute({"run_id": "r2"})

    xl_rows2 = pd.read_excel(xl_path, dtype=str).to_dict(orient="records")
    assert len(xl_rows2) == 1 and xl_rows2[0]["USUBJID"] == "S01"
    assert xl_rows2[0]["BRTHDTC"] == "1981"

    json_rows2 = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(json_rows2) == 1 and json_rows2[0]["USUBJID"] == "S01"
    assert json_rows2[0]["BRTHDTC"] == "1981"


def test_operator_explicit_ledger_path_uses_local_file_provider_under_airflow_ctx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ImednetExportOperator with explicit ledger_path writes to FileStateProvider even under Airflow env."""
    from datetime import UTC, datetime

    import imednet.integrations.sink_base as sink_base_mod
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet.models.records import Record
    from imednet.models.variables import Variable
    from imednet_workflows.state_ledger import AirflowStateProvider, FileStateProvider

    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "airflow_export_task")
    monkeypatch.setenv("USE_AIRFLOW_STATE_PROVIDER", "1")
    monkeypatch.setenv("IMEDNET_CACHE_DIR", str(tmp_path / "op_cache"))

    def _forbid_airflow_provider(*_a: object, **_kw: object) -> object:
        raise AssertionError("AirflowStateProvider must not be used when ledger_path is explicit")

    monkeypatch.setattr(AirflowStateProvider, "__init__", _forbid_airflow_provider)

    rec = Record(
        record_id=10,
        study_key="STUDY-LEDGER",
        subject_key="S01",
        form_id=1,
        form_key="DM",
        deleted=False,
        date_modified=datetime(2026, 6, 5, 14, 0, tzinfo=UTC),
        last_updated=datetime(2026, 6, 5, 14, 0, tzinfo=UTC),
        record_data={"AGE": 41},
    )
    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(variable_name="AGE", variable_type="int", form_id=1, form_key="DM")
    ]
    sdk.get_records.return_value = [rec]
    sdk.records.list.return_value = iter([rec])
    sdk.sinks = None

    # 1. Tabular destination path
    ledger_tab = tmp_path / "explicit_tab_ledger.json"
    out_csv = tmp_path / "explicit.csv"
    op_tab = ImednetExportOperator(
        task_id="tab_explicit_ledger",
        study_key="STUDY-LEDGER",
        destination="csv",
        output_path=str(out_csv),
        incremental=True,
        ledger_path=str(ledger_tab),
    )
    monkeypatch.setattr(op_tab, "_get_sdk", lambda: sdk)
    op_tab.execute({"run_id": "r1"})

    assert ledger_tab.exists()
    tab_streams = FileStateProvider(ledger_tab).read_state().studies["STUDY-LEDGER"].streams
    assert len(tab_streams) == 1
    assert next(iter(tab_streams.values())).last_timestamp == datetime(
        2026, 6, 5, 14, 0, tzinfo=UTC
    )

    # 2. Plugin sink destination path
    ledger_plugin = tmp_path / "explicit_plugin_ledger.json"

    class _DummyPluginSink(sink_base_mod.ExportSink):
        def write_batch(self, records, *, batch_id=None):
            _ = batch_id
            return len(list(records))

    sdk_plugin = MagicMock()
    sdk_plugin.records.list.return_value = iter([rec])
    sdk_plugin.sinks.create.side_effect = lambda _dest, config=None, **_kw: _DummyPluginSink(
        config or sink_base_mod.SinkConfig(study_key="STUDY-LEDGER")
    )

    op_plugin = ImednetExportOperator(
        task_id="plugin_explicit_ledger",
        study_key="STUDY-LEDGER",
        destination="snowflake",
        export_kwargs={"table": "RAW_DM"},
        incremental=True,
        ledger_path=str(ledger_plugin),
    )
    monkeypatch.setattr(op_plugin, "_get_sdk", lambda: sdk_plugin)
    op_plugin.execute({"run_id": "r1"})

    assert ledger_plugin.exists()
    plugin_streams = FileStateProvider(ledger_plugin).read_state().studies["STUDY-LEDGER"].streams
    assert len(plugin_streams) == 1
    assert next(iter(plugin_streams.values())).last_timestamp == datetime(
        2026, 6, 5, 14, 0, tzinfo=UTC
    )


def test_operator_default_stream_names_isolate_targets_and_form_filters() -> None:
    """ImednetExportOperator generates distinct default stream names per destination target and Form filter."""
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    # 1. Different output_path targets under identical task_id (e.g. dynamic task mapping)
    op_dm_file = ImednetExportOperator(
        task_id="mapped_export",
        study_key="S1",
        destination="parquet",
        output_path="/tmp/s1/dm.parquet",
        incremental=True,
    )
    op_ae_file = ImednetExportOperator(
        task_id="mapped_export",
        study_key="S1",
        destination="parquet",
        output_path="/tmp/s1/ae.parquet",
        incremental=True,
    )
    # Path separator vs underscore + destination_conn_id isolation
    op_slash = ImednetExportOperator(
        task_id="mapped_export",
        study_key="S1",
        destination="csv",
        output_path="/data/a/b.csv",
        incremental=True,
    )
    op_under = ImednetExportOperator(
        task_id="mapped_export",
        study_key="S1",
        destination="csv",
        output_path="/data/a_b.csv",
        incremental=True,
    )
    op_conn_a = ImednetExportOperator(
        task_id="sql_export",
        study_key="S1",
        destination="sql",
        export_kwargs={"table_name": "RECORDS"},
        destination_conn_id="pg_warehouse_a",
        incremental=True,
    )
    op_conn_b = ImednetExportOperator(
        task_id="sql_export",
        study_key="S1",
        destination="sql",
        export_kwargs={"table_name": "RECORDS"},
        destination_conn_id="pg_warehouse_b",
        incremental=True,
    )

    # 2. Different Form filters on same destination/task_id
    op_fw_dm = ImednetExportOperator(
        task_id="plugin_sink",
        study_key="S1",
        destination="snowflake",
        export_kwargs={"table": "CLINICAL", "form_whitelist": ["DM"]},
        incremental=True,
    )
    op_fw_ae = ImednetExportOperator(
        task_id="plugin_sink",
        study_key="S1",
        destination="snowflake",
        export_kwargs={"table": "CLINICAL", "form_whitelist": ["AE"]},
        incremental=True,
    )
    op_dom_dm = ImednetExportOperator(
        task_id="sdtm_export",
        study_key="S1",
        destination="parquet",
        output_path="/tmp/out",
        as_sdtm=True,
        sdtm_domain="DM",
        incremental=True,
    )
    op_dom_ae = ImednetExportOperator(
        task_id="sdtm_export",
        study_key="S1",
        destination="parquet",
        output_path="/tmp/out",
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
    )
    op_flt_dm = ImednetExportOperator(
        task_id="filtered_export",
        study_key="S1",
        destination="json",
        output_path="/tmp/records.json",
        record_filter="formKey==DM",
        incremental=True,
    )
    op_flt_ae = ImednetExportOperator(
        task_id="filtered_export",
        study_key="S1",
        destination="json",
        output_path="/tmp/records.json",
        record_filter="formKey==AE",
        incremental=True,
    )

    assert op_dm_file._default_airflow_stream_name(
        "parquet"
    ) != op_ae_file._default_airflow_stream_name("parquet")
    assert op_slash._default_airflow_stream_name("csv") != op_under._default_airflow_stream_name(
        "csv"
    )
    assert op_conn_a._default_airflow_stream_name("sql") != op_conn_b._default_airflow_stream_name(
        "sql"
    )
    assert op_fw_dm._default_airflow_stream_name(
        "snowflake"
    ) != op_fw_ae._default_airflow_stream_name("snowflake")
    assert op_dom_dm._default_airflow_stream_name(
        "parquet"
    ) != op_dom_ae._default_airflow_stream_name("parquet")
    assert op_flt_dm._default_airflow_stream_name("json") != op_flt_ae._default_airflow_stream_name(
        "json"
    )

    # 3. End-to-end mapped plugin sink runs with explicit state_provider isolate per-Form checkpoints
    import tempfile
    from datetime import UTC, datetime

    import imednet.integrations.sink_base as sink_base_mod
    from imednet.models.records import Record
    from imednet_workflows.state_ledger import FileStateProvider

    with tempfile.TemporaryDirectory() as td:
        mapped_provider = FileStateProvider(Path(td) / "mapped_sinks.json")

        class _NoopSink(sink_base_mod.ExportSink):
            def write_batch(self, records, *, batch_id=None):
                _ = batch_id
                return len(list(records))

        def _make_mapped_sdk(ts_hour: int) -> MagicMock:
            rec = Record(
                record_id=ts_hour,
                study_key="S1",
                form_id=1,
                form_key="DM",
                deleted=False,
                date_modified=datetime(2026, 6, 1, ts_hour, 0, tzinfo=UTC),
            )
            msdk = MagicMock()
            msdk.records.list.return_value = iter([rec])
            msdk.sinks.create.side_effect = lambda _d, config=None, **_kw: _NoopSink(
                config or sink_base_mod.SinkConfig(study_key="S1")
            )
            return msdk

        for idx, (form_code, hr) in enumerate((("DM", 10), ("AE", 14))):
            mapped_op = ImednetExportOperator(
                task_id="mapped_snowflake",
                study_key="S1",
                destination="snowflake",
                export_kwargs={"table": "CLINICAL", "form_whitelist": [form_code]},
                incremental=True,
                state_provider=mapped_provider,
            )
            mapped_op._get_sdk = lambda h=hr: _make_mapped_sdk(h)  # type: ignore[method-assign]
            mapped_op.execute({"run_id": f"r_{idx}"})

        streams = mapped_provider.read_state().studies["S1"].streams
        assert len(streams) == 2
        assert {st.last_timestamp for st in streams.values()} == {
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        }
