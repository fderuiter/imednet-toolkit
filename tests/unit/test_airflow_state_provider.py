"""Real Airflow 3 tests for AirflowStateProvider Variable checkpoints."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("airflow")

from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    InvalidStateTransitionError,
    LedgerState,
    StreamExecutionStatus,
)


@pytest.fixture(scope="module")
def airflow_state_db(tmp_path_factory: pytest.TempPathFactory):
    """Initialize an isolated SQLite Airflow 3 metadata database for Variable checkpoints."""
    import airflow.settings
    from airflow.configuration import conf
    from airflow.utils.db import initdb

    db_dir = tmp_path_factory.mktemp("airflow_state_provider")
    db_url = f"sqlite:///{db_dir / 'airflow.db'}"
    prev_conn = conf.get("database", "sql_alchemy_conn", fallback=None)

    conf.set("database", "sql_alchemy_conn", db_url)
    airflow.settings.SQL_ALCHEMY_CONN = db_url
    airflow.settings.configure_orm()
    initdb()

    yield db_dir

    airflow.settings.dispose_orm()
    if prev_conn:
        conf.set("database", "sql_alchemy_conn", prev_conn)
        airflow.settings.SQL_ALCHEMY_CONN = prev_conn


@pytest.fixture(autouse=True)
def _clean_variables(airflow_state_db: Path):
    """Clear Airflow Variable rows before each test for deterministic isolation."""
    _ = airflow_state_db
    from airflow.models import Variable
    from airflow.utils.session import create_session
    from sqlalchemy import delete

    with create_session() as session:
        session.execute(delete(Variable))
    yield


def test_airflow_state_provider_set_get_and_timezone_normalization() -> None:
    """AirflowStateProvider persists and reads Variable JSON state with UTC normalization."""
    from airflow.models import Variable

    provider = AirflowStateProvider()
    assert provider.get_last_timestamp("STUDY_01", "records_stream") is None
    assert provider.get_last_status("STUDY_01", "records_stream") is None

    naive_ts = datetime(2026, 4, 10, 8, 30, 0)
    provider.set_last_timestamp(
        "STUDY_01",
        "records_stream",
        naive_ts,
        records_processed=42,
        status=StreamExecutionStatus.SUCCESS,
        metadata={"batch_size": 500},
    )

    assert provider.get_last_timestamp("STUDY_01", "records_stream") == datetime(
        2026, 4, 10, 8, 30, 0, tzinfo=UTC
    )
    assert provider.get_last_status("STUDY_01", "records_stream") == StreamExecutionStatus.SUCCESS

    raw_var = Variable.get("imednet_state__STUDY_01__records_stream", deserialize_json=True)
    assert raw_var["study_key"] == "STUDY_01"
    assert raw_var["stream_name"] == "records_stream"
    assert raw_var["records_processed"] == 42
    assert raw_var["last_run_status"] == "success"


def test_airflow_state_provider_transition_validation_and_force_override() -> None:
    """AirflowStateProvider enforces transition rules unless force=True is supplied."""
    provider = AirflowStateProvider()
    t1 = datetime(2026, 4, 10, 9, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC)

    provider.set_last_timestamp("STUDY_TR", "stream_a", t1, status=StreamExecutionStatus.RUNNING)

    with pytest.raises(InvalidStateTransitionError):
        provider.set_last_timestamp(
            "STUDY_TR", "stream_a", t2, status=StreamExecutionStatus.RUNNING
        )

    provider.set_last_timestamp(
        "STUDY_TR",
        "stream_a",
        t2,
        status=StreamExecutionStatus.PENDING,
        force=True,
    )
    assert provider.get_last_status("STUDY_TR", "stream_a") == StreamExecutionStatus.PENDING


def test_airflow_state_provider_transaction_commit_and_rollback() -> None:
    """AirflowStateProvider commits watermarks on success and preserves prior watermark on error."""
    provider = AirflowStateProvider()
    fallback_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    committed_ts = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)

    with provider.transaction("STUDY_TX", "airflow_csv", fallback_timestamp=fallback_ts) as tx:
        assert tx["last_timestamp"] == fallback_ts
        assert provider.get_last_status("STUDY_TX", "airflow_csv") == StreamExecutionStatus.RUNNING
        tx["new_timestamp"] = committed_ts
        tx["records_processed"] = 15
        tx["metadata"] = {"dest": "csv"}

    assert provider.get_last_timestamp("STUDY_TX", "airflow_csv") == committed_ts
    assert provider.get_last_status("STUDY_TX", "airflow_csv") == StreamExecutionStatus.SUCCESS

    with (
        pytest.raises(RuntimeError, match="sink write failure"),
        provider.transaction("STUDY_TX", "airflow_csv") as tx2,
    ):
        tx2["new_timestamp"] = datetime(2026, 1, 3, 12, 0, 0, tzinfo=UTC)
        tx2["records_processed"] = 3
        err_msg = (
            "sink write failure for "
            "postgresql://loader:secret@db.internal/edc"  # pragma: allowlist secret
        )
        raise RuntimeError(err_msg)

    assert provider.get_last_timestamp("STUDY_TX", "airflow_csv") == committed_ts
    assert provider.get_last_status("STUDY_TX", "airflow_csv") == StreamExecutionStatus.FAILED

    ledger = provider.read_state()
    entry = ledger.studies["STUDY_TX"].streams["airflow_csv"]
    assert entry.last_timestamp == committed_ts
    assert entry.records_processed == 3
    assert entry.error_message is not None
    assert "secret" not in entry.error_message


def test_airflow_state_provider_redacts_sensitive_metadata() -> None:
    """AirflowStateProvider redacts secret keys in metadata before storing Variable JSON."""
    from airflow.models import Variable

    provider = AirflowStateProvider()
    provider.set_last_timestamp(
        "STUDY_SEC",
        "sql_records",
        datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC),
        metadata={
            "api_key": "plaintext-key",  # pragma: allowlist secret
            "safe_table": "clinical_records",
        },
    )
    stored = Variable.get("imednet_state__STUDY_SEC__sql_records", deserialize_json=True)
    assert stored["metadata"]["api_key"] != "plaintext-key"  # pragma: allowlist secret
    assert stored["metadata"]["safe_table"] == "clinical_records"


def test_airflow_state_provider_read_state_and_delete_entry_underscore_safety() -> None:
    """read_state and delete_entry preserve study/stream underscores without prefix collisions."""
    from airflow.models import Variable

    provider = AirflowStateProvider()
    ts = datetime(2026, 5, 1, 6, 0, 0, tzinfo=UTC)

    provider.set_last_timestamp("STUDY_1", "sql_demographics", ts, records_processed=10)
    provider.set_last_timestamp("STUDY_1", "sql_vitals", ts, records_processed=20)
    provider.set_last_timestamp("STUDY_10", "sql_demographics", ts, records_processed=30)

    # Also store a legacy-shaped payload without explicit study_key/stream_name fields
    Variable.set(
        "imednet_state__LEGACY_STUDY__airflow_json",
        {"last_timestamp": "2026-05-01T06:00:00", "records_processed": 7, "status": "success"},
        serialize_json=True,
    )
    # Unrelated Airflow Variable should be ignored by read_state()
    Variable.set("unrelated_pipeline_setting", {"enabled": True}, serialize_json=True)

    state = provider.read_state()
    assert set(state.studies.keys()) == {"STUDY_1", "STUDY_10", "LEGACY_STUDY"}
    assert set(state.studies["STUDY_1"].streams.keys()) == {"sql_demographics", "sql_vitals"}
    assert state.studies["LEGACY_STUDY"].streams["airflow_json"].records_processed == 7

    assert provider.delete_entry("STUDY_1", "missing_stream") is False
    assert provider.delete_entry("STUDY_1", "sql_vitals") is True
    assert "sql_vitals" not in provider.read_state().studies["STUDY_1"].streams

    # Deleting whole STUDY_1 must not delete STUDY_10
    assert provider.delete_entry("STUDY_1") is True
    assert provider.delete_entry("STUDY_1") is False
    remaining = provider.read_state()
    assert "STUDY_1" not in remaining.studies
    assert "STUDY_10" in remaining.studies


def test_airflow_state_provider_executes_inside_real_airflow_task_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AirflowStateProvider round-trips checkpoints via Task SDK Variable inside dag.test()."""
    from airflow.configuration import conf
    from airflow.dag_processing.dagbag import DagBag

    dags_dir = tmp_path / "dags"
    dags_dir.mkdir()
    dag_file = dags_dir / "state_checkpoint_dag.py"
    dag_file.write_text(
        """from datetime import datetime
from airflow.sdk import DAG, task
from imednet_workflows.state_ledger import AirflowStateProvider

with DAG(
    dag_id="verify_variable_state_provider",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
) as dag:

    @task
    def checkpoint_roundtrip() -> str:
        provider = AirflowStateProvider()
        with provider.transaction("SDK_STUDY", "sdk_stream") as tx:
            tx["new_timestamp"] = datetime.fromisoformat("2026-06-15T10:30:00+00:00")
            tx["records_processed"] = 12
        state = provider.read_state()
        assert state.studies["SDK_STUDY"].streams["sdk_stream"].records_processed == 12
        ts = provider.get_last_timestamp("SDK_STUDY", "sdk_stream")
        assert ts is not None
        return ts.isoformat()

    checkpoint_roundtrip()
""",
        encoding="utf-8",
    )

    import json
    import os

    import airflow.settings
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.models.dagbundle import DagBundleModel
    from airflow.utils.session import create_session
    from sqlalchemy import delete

    prev_ctx_env = {k: v for k, v in os.environ.items() if k.startswith("AIRFLOW_CTX_")}
    prev_dags_folder = conf.get("core", "dags_folder", fallback=None)
    prev_load_examples = conf.get("core", "load_examples", fallback=None)
    prev_bundle_cfg = conf.get("dag_processor", "dag_bundle_config_list", fallback=None)
    prev_settings_dags = airflow.settings.DAGS_FOLDER

    bundle_cfg = json.dumps(
        [
            {
                "name": "dags-folder",
                "classpath": "airflow.dag_processing.bundles.local.LocalDagBundle",
                "kwargs": {"path": str(dags_dir), "refresh_interval": 1},
            }
        ]
    )
    monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", str(dags_dir))
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    monkeypatch.setenv("AIRFLOW__DAG_PROCESSOR__DAG_BUNDLE_CONFIG_LIST", bundle_cfg)
    conf.set("core", "dags_folder", str(dags_dir))
    conf.set("core", "load_examples", "False")
    conf.set("dag_processor", "dag_bundle_config_list", bundle_cfg)
    airflow.settings.DAGS_FOLDER = str(dags_dir)
    DagBundlesManager()._bundle_config = {}
    with create_session() as session:
        session.execute(delete(DagBundleModel))
    try:
        bag = DagBag(dag_folder=str(dags_dir))
        assert not bag.import_errors
        dag_run = bag.dags["verify_variable_state_provider"].test()
        assert str(dag_run.state) == "success"
    finally:
        for key in [k for k in os.environ if k.startswith("AIRFLOW_CTX_")]:
            if key in prev_ctx_env:
                os.environ[key] = prev_ctx_env[key]
            else:
                os.environ.pop(key, None)
        airflow.settings.DAGS_FOLDER = prev_settings_dags
        if prev_dags_folder:
            conf.set("core", "dags_folder", prev_dags_folder)
        if prev_load_examples is not None:
            conf.set("core", "load_examples", prev_load_examples)
        if prev_bundle_cfg is not None:
            conf.set("dag_processor", "dag_bundle_config_list", prev_bundle_cfg)
        DagBundlesManager()._bundle_config = {}

    provider = AirflowStateProvider()
    assert provider.get_last_timestamp("SDK_STUDY", "sdk_stream") == datetime(
        2026, 6, 15, 10, 30, 0, tzinfo=UTC
    )
    assert provider.delete_entry("SDK_STUDY", "sdk_stream") is True


def test_airflow_state_provider_offline_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """AirflowStateProvider degrades cleanly when Airflow modules are unavailable."""
    provider = AirflowStateProvider()
    monkeypatch.setitem(sys.modules, "airflow", None)
    monkeypatch.setitem(sys.modules, "airflow.models", None)
    monkeypatch.setitem(sys.modules, "airflow.sdk", None)

    assert provider.get_last_timestamp("OFFLINE", "s1") is None
    assert provider.get_last_status("OFFLINE", "s1") is None
    provider.set_last_timestamp("OFFLINE", "s1", datetime(2026, 1, 1, tzinfo=UTC))
    assert provider.delete_entry("OFFLINE") is False
    assert provider.read_state() == LedgerState()
