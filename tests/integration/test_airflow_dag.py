"""Unit tests for airflow dag."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _purge_partial_airflow_modules() -> None:
    import sys

    if (
        "airflow" not in sys.modules
        or getattr(sys.modules.get("airflow"), "__path__", None) is None
    ):
        for mod in list(sys.modules):
            if mod == "airflow" or mod.startswith(("airflow.", "apache_airflow_providers_imednet")):
                sys.modules.pop(mod, None)


def test_dag_runs(monkeypatch, tmp_path):
    """Test that dag runs."""
    _purge_partial_airflow_modules()
    pytest.importorskip("airflow")
    out_csv = tmp_path / "out.csv"
    from airflow.models import DAG  # noqa: I001

    from apache_airflow_providers_imednet import ImednetJobSensor, ImednetExportOperator

    sdk = MagicMock()

    import pandas as pd

    df = pd.DataFrame({"id": [1, 2]})
    mapper_inst = MagicMock()
    mapper_inst.dataframe.return_value = df
    mapper_inst._fetch_variable_metadata.return_value = (["id"], {"id": "id"})
    mapper_inst._iter_records.return_value = [MagicMock()]
    mapper_inst._parse_records.return_value = ([{}], 0)
    mapper_inst._build_dataframe.return_value = df
    from imednet.integrations import export as export_mod

    monkeypatch.setattr(export_mod, "apply_quality_gate", lambda s, sk, rr, c: rr)
    monkeypatch.setattr(
        export_mod, "_record_mapper", MagicMock(return_value=MagicMock(return_value=mapper_inst))
    )

    sdk.jobs.get.return_value = SimpleNamespace(state="COMPLETED", is_terminal=True)
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk)
    monkeypatch.setattr(ImednetJobSensor, "_get_sdk", lambda self: sdk)

    with DAG("d", start_date=datetime(2024, 1, 1)):
        export = ImednetExportOperator(
            task_id="export",
            study_key="S",
            destination="csv",
            output_path=str(out_csv),
        )
        wait = ImednetJobSensor(task_id="wait", study_key="S", batch_id="ID")
        export >> wait

    export.execute(context={})
    wait.execute(context={})
    with open(str(out_csv)) as f:
        body = f.read()
    assert "id" in body
    assert "id" in body


@pytest.fixture
def airflow3_sqlite_env(monkeypatch, tmp_path):
    """Initialize an isolated Airflow 3 SQLite metadata database and restore logging/ORM state."""
    _purge_partial_airflow_modules()
    pytest.importorskip("airflow")
    import json
    import logging
    import os
    from pathlib import Path

    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    from airflow import settings
    from airflow.configuration import conf
    from airflow.dag_processing import bundles
    from airflow.utils.db import initdb

    bundle_manager_mod = getattr(bundles, "manager", None)
    snapshot_loader = getattr(bundle_manager_mod, "_load_bundle_config_snapshot", None)

    af_home = tmp_path / "airflow_home"
    dags_dir = af_home / "dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    db_file = af_home / "airflow.db"
    db_uri = f"sqlite:///{db_file}"

    prev_conn = conf.get("database", "sql_alchemy_conn", fallback=None)
    prev_pool_enabled = conf.get("database", "sql_alchemy_pool_enabled", fallback=None)
    prev_dags = conf.get("core", "dags_folder", fallback=None)
    prev_examples = conf.get("core", "load_examples", fallback=None)
    prev_bundles = conf.get("dag_processor", "dag_bundle_config_list", fallback=None)
    prev_settings_conn = settings.SQL_ALCHEMY_CONN
    prev_settings_conn_async = getattr(settings, "SQL_ALCHEMY_CONN_ASYNC", None)
    prev_settings_dags = settings.DAGS_FOLDER

    bundle_cfg = json.dumps(
        [
            {
                "name": "dags-folder",
                "classpath": "airflow.dag_processing.bundles.local.LocalDagBundle",
                "kwargs": {"path": str(dags_dir), "refresh_interval": 0},
            }
        ]
    )

    monkeypatch.setenv("AIRFLOW_HOME", str(af_home))
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", db_uri)
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_ENABLED", "False")
    monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", str(dags_dir))
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    monkeypatch.setenv("AIRFLOW__DAG_PROCESSOR__DAG_BUNDLE_CONFIG_LIST", bundle_cfg)

    conf.set("database", "sql_alchemy_conn", db_uri)
    conf.set("database", "sql_alchemy_pool_enabled", "False")
    conf.set("core", "dags_folder", str(dags_dir))
    conf.set("core", "load_examples", "False")
    conf.set("dag_processor", "dag_bundle_config_list", bundle_cfg)
    if callable(getattr(snapshot_loader, "cache_clear", None)):
        snapshot_loader.cache_clear()

    settings.dispose_orm()
    settings.configure_vars()
    monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN", db_uri)
    monkeypatch.setattr(settings, "DAGS_FOLDER", str(dags_dir))
    settings.configure_orm()
    initdb()

    yield {
        "af_home": af_home,
        "dags_dir": dags_dir,
        "db_file": db_file,
        "db_uri": db_uri,
        "env": {
            **{k: v for k, v in os.environ.items() if not k.startswith("AIRFLOW_CTX_")},
            "AIRFLOW_HOME": str(af_home),
            "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": db_uri,
            "AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_ENABLED": "False",
            "AIRFLOW__CORE__DAGS_FOLDER": str(dags_dir),
            "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
            "AIRFLOW__DAG_PROCESSOR__DAG_BUNDLE_CONFIG_LIST": bundle_cfg,
        },
        "repo_root": Path(__file__).resolve().parents[2],
    }

    for env_key in list(os.environ):
        if env_key.startswith("AIRFLOW_CTX_"):
            os.environ.pop(env_key, None)

    settings.dispose_orm()
    if prev_conn is not None:
        conf.set("database", "sql_alchemy_conn", prev_conn)
    if prev_pool_enabled is not None:
        conf.set("database", "sql_alchemy_pool_enabled", prev_pool_enabled)
    if prev_dags is not None:
        conf.set("core", "dags_folder", prev_dags)
    if prev_examples is not None:
        conf.set("core", "load_examples", prev_examples)
    if prev_bundles is not None:
        conf.set("dag_processor", "dag_bundle_config_list", prev_bundles)
    if callable(getattr(snapshot_loader, "cache_clear", None)):
        snapshot_loader.cache_clear()
    settings.configure_vars()
    settings.SQL_ALCHEMY_CONN = prev_settings_conn
    if prev_settings_conn_async is not None and hasattr(settings, "SQL_ALCHEMY_CONN_ASYNC"):
        settings.SQL_ALCHEMY_CONN_ASYNC = prev_settings_conn_async
    settings.DAGS_FOLDER = prev_settings_dags
    settings.configure_orm()

    root_logger.handlers = saved_handlers
    root_logger.setLevel(saved_level)


def _register_dag_for_airflow3_test(dag, dags_dir):
    """Register an in-memory DAG in Airflow 3's metadata DB so ``dag.test()`` executes cleanly."""
    from airflow import settings
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.dag_processing.dagbag import BundleDagBag, sync_bag_to_db

    session = settings.Session()
    try:
        DagBundlesManager().sync_bundles_to_db(session=session)
        session.commit()
    finally:
        session.close()

    bag = BundleDagBag(
        dag_folder=str(dags_dir),
        bundle_path=dags_dir,
        bundle_name="dags-folder",
        safe_mode=True,
    )
    bag.bag_dag(dag=dag)
    sync_bag_to_db(bag, "dags-folder", None)


def _build_synthetic_sdk_for_incremental(records_all):
    """Create a synthetic SDK whose ``records.list`` filters by ``date_modified_since`` / ``filter``."""
    from datetime import timezone

    from imednet.models.variables import Variable

    variables = [
        Variable.model_validate(
            {
                "studyKey": "STUDY_INC",
                "variableId": 1,
                "variableName": "SCORE",
                "formId": 1,
                "formKey": "DEMO",
                "formName": "Demographics",
                "label": "Score",
            }
        )
    ]

    observed_calls = []
    sdk = MagicMock()
    sdk.get_variables.return_value = variables
    sdk.variables.list.return_value = variables
    sdk.studies.get_configuration = MagicMock(side_effect=AttributeError)

    def _list_records(_study_key=None, **kwargs):
        observed_calls.append(dict(kwargs))
        cutoff = None
        raw_dm = kwargs.get("dateModified") or kwargs.get("date_modified")
        raw_filter = kwargs.get("record_filter") or kwargs.get("filter")
        if raw_dm is not None:
            raw_ts = raw_dm[1] if isinstance(raw_dm, tuple) else raw_dm
            cutoff = datetime.fromisoformat(str(raw_ts))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        elif isinstance(raw_filter, str) and "dateModified>" in raw_filter:
            # Extract ISO timestamp token after dateModified>= or dateModified>
            token = raw_filter.split("dateModified")[-1].lstrip(">=").split(";")[0].strip()
            cutoff = datetime.fromisoformat(token)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        elif isinstance(raw_filter, dict) and "date_modified" in raw_filter:
            val = raw_filter["date_modified"]
            raw_ts = val[1] if isinstance(val, tuple) else val
            cutoff = datetime.fromisoformat(str(raw_ts))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)

        active = []
        for rec in records_all["items"]:
            raw_rec_dt = rec.date_modified
            rec_dt = None
            if isinstance(raw_rec_dt, str) and raw_rec_dt:
                rec_dt = datetime.fromisoformat(raw_rec_dt)
            elif isinstance(raw_rec_dt, datetime):
                rec_dt = raw_rec_dt
            if rec_dt is not None and rec_dt.tzinfo is None:
                rec_dt = rec_dt.replace(tzinfo=timezone.utc)
            if cutoff is not None and rec_dt is not None and rec_dt <= cutoff:
                continue
            active.append(rec)
        return iter(active)

    sdk.records.list.side_effect = _list_records
    return sdk, observed_calls


def test_airflow3_providers_manager_and_task_runtime_discovery(airflow3_sqlite_env):
    """AC 1: ProvidersManager and ProvidersManagerTaskRuntime discover provider and ImednetHook."""
    from airflow.providers_manager import ProvidersManager
    from airflow.sdk.providers_manager_runtime import ProvidersManagerTaskRuntime

    from apache_airflow_providers_imednet.hooks import ImednetHook

    _ = airflow3_sqlite_env

    pm = ProvidersManager()
    pm._cleanup()
    assert "apache-airflow-providers-imednet" in pm.providers

    runtime_pm = ProvidersManagerTaskRuntime()
    runtime_pm._cleanup()
    assert "imednet" in runtime_pm.hooks
    hook_info = runtime_pm.hooks["imednet"]
    assert hook_info is not None
    assert hook_info.connection_type == "imednet"
    assert hook_info.hook_class_name == "airflow.providers.imednet.hooks.ImednetHook"

    from airflow.providers.imednet.hooks import ImednetHook as DiscoveredHook

    assert DiscoveredHook is ImednetHook


def test_airflow3_incremental_export_runs_across_csv_parquet_sql_duckdb_and_sink(
    monkeypatch, airflow3_sqlite_env, tmp_path
):
    """AC 2: ImednetExportOperator(incremental=True) succeeds on run 1 and run 2 across all target classes."""
    from datetime import timezone

    import duckdb
    import pandas as pd
    import sqlalchemy as sa
    from airflow.sdk import DAG, DagRunState

    from apache_airflow_providers_imednet import ImednetExportOperator
    from imednet.models.records import Record

    t1 = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 11, 12, 0, tzinfo=timezone.utc)

    rec1 = Record.model_validate(
        {
            "studyKey": "STUDY_INC",
            "recordId": 1,
            "subjectId": 10,
            "subjectKey": "SUBJ-01",
            "formKey": "DEMO",
            "dateModified": t1.isoformat(),
            "recordData": {"SCORE": 10},
        }
    )
    rec2 = Record.model_validate(
        {
            "studyKey": "STUDY_INC",
            "recordId": 2,
            "subjectId": 20,
            "subjectKey": "SUBJ-02",
            "formKey": "DEMO",
            "dateModified": t2.isoformat(),
            "recordData": {"SCORE": 25},
        }
    )
    records_box = {"items": [rec1]}
    sdk, _calls = _build_synthetic_sdk_for_incremental(records_box)

    sink_batches = []

    class DummyMongoSinkConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class DummyMongoSink:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

        def write_batch(self, records, *, batch_id=None):
            batch = list(records)
            sink_batches.append((batch_id, [r.record_id for r in batch]))
            return len(batch)

        def flush(self):
            pass

        def close(self):
            pass

    sdk.sinks = SimpleNamespace(
        MongoDbExportSink=DummyMongoSink,
        MongoDbSinkConfig=DummyMongoSinkConfig,
    )
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk)

    csv_path = tmp_path / "inc.csv"
    parquet_path = tmp_path / "inc.parquet"
    sqlite_db = tmp_path / "inc_sql.db"
    sqlite_uri = f"sqlite:///{sqlite_db}"
    duckdb_path = tmp_path / "inc.duckdb"

    monkeypatch.setattr(
        ImednetExportOperator,
        "_resolve_destination_connection",
        lambda self, dest: (
            {"conn_str": sqlite_uri}
            if dest == "sql"
            else {
                "uri": "mongodb://localhost:27017",
                "database": "clinical",
                "collection": "records",
            }
        ),
    )

    with DAG(
        dag_id="imednet_airflow3_incremental_all_targets",
        schedule=None,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        catchup=False,
    ) as dag:
        op_csv = ImednetExportOperator(
            task_id="inc_csv",
            study_key="STUDY_INC",
            destination="csv",
            output_path=str(csv_path),
            incremental=True,
        )
        op_parquet = ImednetExportOperator(
            task_id="inc_parquet",
            study_key="STUDY_INC",
            destination="parquet",
            output_path=str(parquet_path),
            incremental=True,
        )
        op_sql = ImednetExportOperator(
            task_id="inc_sql",
            study_key="STUDY_INC",
            destination="sql",
            output_path="sql_records",
            destination_conn_id="sqlite_dest",
            incremental=True,
        )
        op_duckdb = ImednetExportOperator(
            task_id="inc_duckdb",
            study_key="STUDY_INC",
            destination="duckdb",
            output_path=str(duckdb_path),
            incremental=True,
        )
        op_sink = ImednetExportOperator(
            task_id="inc_mongodb",
            study_key="STUDY_INC",
            destination="mongodb",
            destination_conn_id="mongo_dest",
            incremental=True,
        )
        _ = (op_csv, op_parquet, op_sql, op_duckdb, op_sink)

    _register_dag_for_airflow3_test(dag, airflow3_sqlite_env["dags_dir"])

    # Run 1 (no prior XComs exist in metadata DB -> must succeed without BaseXCom errors)
    dr1 = dag.test(
        logical_date=datetime(2026, 1, 10, 13, 0, tzinfo=timezone.utc),
        run_after=datetime(2026, 1, 10, 13, 0, tzinfo=timezone.utc),
    )
    assert dr1.state == DagRunState.SUCCESS
    assert len(sink_batches) == 1
    assert sink_batches[0][1] == [1]

    # Add second record with newer modification timestamp for Run 2
    records_box["items"] = [rec1, rec2]

    dr2 = dag.test(
        logical_date=datetime(2026, 1, 11, 13, 0, tzinfo=timezone.utc),
        run_after=datetime(2026, 1, 11, 13, 0, tzinfo=timezone.utc),
    )
    assert dr2.state == DagRunState.SUCCESS

    # Sink received ONLY the delta record [2] on Run 2 thanks to XCom high-water mark from Run 1!
    assert len(sink_batches) == 2
    assert sink_batches[1][1] == [2]

    # Verify outputs exist and contain latest exported data across CSV, Parquet, SQL, and DuckDB
    assert len(pd.read_csv(csv_path)) >= 1
    assert len(pd.read_parquet(parquet_path)) >= 1
    engine = sa.create_engine(sqlite_uri)
    try:
        with engine.connect() as conn:
            sql_count = conn.execute(sa.text("SELECT COUNT(*) FROM sql_records")).scalar()
            assert (sql_count or 0) >= 1
    finally:
        engine.dispose()

    with duckdb.connect(str(duckdb_path), read_only=True, config={"threads": "1"}) as con:
        duck_count = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        assert duck_count >= 1


def test_airflow3_two_operators_in_same_dag_isolate_xcom_high_water_marks(
    monkeypatch, airflow3_sqlite_env, tmp_path
):
    """AC 3: Two ImednetExportOperator tasks in the same DAG read only their own XCom high-water marks."""
    from datetime import timezone

    from airflow.sdk import DAG, DagRunState

    from apache_airflow_providers_imednet import ImednetExportOperator
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    t_early = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    t_late = datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)

    variables = [
        Variable.model_validate(
            {
                "studyKey": "STUDY_SHARED",
                "variableId": 1,
                "variableName": "VAL",
                "formId": 1,
                "formKey": "F1",
                "formName": "Form 1",
                "label": "Val",
            }
        )
    ]

    # Track modified_since filter observed by each output_path on Run 2
    observed_filters_by_task: dict[str, list[object]] = {"task_alpha": [], "task_beta": []}
    current_run = {"num": 1}

    def _make_sdk_for(task_label: str, run1_ts: datetime):
        sdk = MagicMock()
        sdk.get_variables.return_value = variables
        sdk.variables.list.return_value = variables
        sdk.studies.get_configuration = MagicMock(side_effect=AttributeError)

        def _list_records(_study_key=None, **kwargs):
            observed_filters_by_task[task_label].append(dict(kwargs))
            ts = run1_ts if current_run["num"] == 1 else datetime(2026, 4, 1, tzinfo=timezone.utc)
            return iter(
                [
                    Record.model_validate(
                        {
                            "studyKey": "STUDY_SHARED",
                            "recordId": 1,
                            "subjectId": 1,
                            "subjectKey": "S1",
                            "formKey": "F1",
                            "dateModified": ts.isoformat(),
                            "recordData": {"VAL": 1},
                        }
                    )
                ]
            )

        sdk.records.list.side_effect = _list_records
        return sdk

    sdk_alpha = _make_sdk_for("task_alpha", t_early)
    sdk_beta = _make_sdk_for("task_beta", t_late)

    monkeypatch.setattr(
        ImednetExportOperator,
        "_get_sdk",
        lambda self: sdk_alpha if self.task_id == "task_alpha" else sdk_beta,
    )

    with DAG(
        dag_id="imednet_airflow3_task_xcom_isolation",
        schedule=None,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        catchup=False,
    ) as dag:
        # Both tasks share study_key="STUDY_SHARED" and destination="csv" -> identical XCom key!
        op_alpha = ImednetExportOperator(
            task_id="task_alpha",
            study_key="STUDY_SHARED",
            destination="csv",
            output_path=str(tmp_path / "alpha.csv"),
            incremental=True,
        )
        op_beta = ImednetExportOperator(
            task_id="task_beta",
            study_key="STUDY_SHARED",
            destination="csv",
            output_path=str(tmp_path / "beta.csv"),
            incremental=True,
        )
        op_alpha >> op_beta

    _register_dag_for_airflow3_test(dag, airflow3_sqlite_env["dags_dir"])

    dr1 = dag.test(
        logical_date=datetime(2026, 3, 20, 0, 0, tzinfo=timezone.utc),
        run_after=datetime(2026, 3, 20, 0, 0, tzinfo=timezone.utc),
    )
    assert dr1.state == DagRunState.SUCCESS

    current_run["num"] = 2
    dr2 = dag.test(
        logical_date=datetime(2026, 3, 21, 0, 0, tzinfo=timezone.utc),
        run_after=datetime(2026, 3, 21, 0, 0, tzinfo=timezone.utc),
    )
    assert dr2.state == DagRunState.SUCCESS

    # On Run 2, task_alpha must read t_early (2026-02-01), NOT task_beta's t_late (2026-03-15)!
    alpha_run2_kwargs_repr = repr(observed_filters_by_task["task_alpha"][-1])
    beta_run2_kwargs_repr = repr(observed_filters_by_task["task_beta"][-1])
    assert "2026-02-01" in alpha_run2_kwargs_repr
    assert "2026-03-15" not in alpha_run2_kwargs_repr
    assert "2026-03-15" in beta_run2_kwargs_repr


def test_airflow3_cli_workflows_state_show_set_reset(airflow3_sqlite_env):
    """AC 4: imednet workflows state show, set, and reset exit 0 with USE_AIRFLOW_STATE_PROVIDER=1."""
    import subprocess
    import sys
    from pathlib import Path

    env = dict(airflow3_sqlite_env["env"])
    env["USE_AIRFLOW_STATE_PROVIDER"] = "1"
    repo_root = airflow3_sqlite_env["repo_root"]
    imednet_bin = str(Path(sys.executable).with_name("imednet"))

    def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [imednet_bin, "workflows", "state", *args],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    res_empty = _run_cli("show")
    assert res_empty.returncode == 0, res_empty.stderr
    assert "No ledger entries found" in res_empty.stdout

    res_set = _run_cli(
        "set",
        "-s",
        "STUDY_CLI_AF3",
        "-m",
        "airflow_records",
        "-t",
        "2026-04-10T08:30:00Z",
        "-r",
        "42",
    )
    assert res_set.returncode == 0, res_set.stderr
    assert "Successfully set high-water mark" in res_set.stdout

    res_show = _run_cli("show", "-s", "STUDY_CLI_AF3")
    assert res_show.returncode == 0, res_show.stderr
    assert "STUDY_CLI_AF3" in res_show.stdout
    assert "airflow_records" in res_show.stdout
    assert "2026-04-10T08:30:00+00:00" in res_show.stdout
    assert "42" in res_show.stdout

    res_reset = _run_cli("reset", "-s", "STUDY_CLI_AF3", "-m", "airflow_records")
    assert res_reset.returncode == 0, res_reset.stderr
    assert "Successfully reset stream" in res_reset.stdout

    res_after = _run_cli("show", "-s", "STUDY_CLI_AF3")
    assert res_after.returncode == 0, res_after.stderr
    assert "No ledger entries found" in res_after.stdout


def test_airflow3_duckdb_operator_writes_default_and_custom_tables(
    monkeypatch, airflow3_sqlite_env, tmp_path
):
    """AC 5: ImednetExportOperator(destination='duckdb') writes Study Records for default and custom tables."""
    from datetime import timezone

    import duckdb
    from airflow.sdk import DAG, DagRunState

    from apache_airflow_providers_imednet import ImednetExportOperator
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    records = [
        Record.model_validate(
            {
                "studyKey": "STUDY_DUCK_AF3",
                "recordId": 501,
                "subjectId": 1,
                "subjectKey": "SUBJ-501",
                "formKey": "VITALS",
                "dateModified": "2026-04-01T10:00:00Z",
                "recordData": {"HR": 72, "PATIENT_NAME": "Jane Clinical"},
            }
        )
    ]
    variables = [
        Variable.model_validate(
            {
                "studyKey": "STUDY_DUCK_AF3",
                "variableId": 1,
                "variableName": "HR",
                "formId": 1,
                "formKey": "VITALS",
                "formName": "Vitals",
                "label": "Heart Rate",
            }
        ),
        Variable.model_validate(
            {
                "studyKey": "STUDY_DUCK_AF3",
                "variableId": 2,
                "variableName": "PATIENT_NAME",
                "formId": 1,
                "formKey": "VITALS",
                "formName": "Vitals",
                "label": "Patient Name",
            }
        ),
    ]

    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: iter(records)
    sdk.get_variables.return_value = variables
    sdk.variables.list.return_value = variables
    sdk.studies.get_configuration = MagicMock(side_effect=AttributeError)
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk)

    db_path = tmp_path / "af3_duckdb.duckdb"

    with DAG(
        dag_id="imednet_airflow3_duckdb_tables",
        schedule=None,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        catchup=False,
    ) as dag:
        t_default = ImednetExportOperator(
            task_id="duckdb_default_records",
            study_key="STUDY_DUCK_AF3",
            destination="duckdb",
            output_path=str(db_path),
        )
        t_table = ImednetExportOperator(
            task_id="duckdb_custom_table_key",
            study_key="STUDY_DUCK_AF3",
            destination="duckdb",
            output_path=str(db_path),
            export_kwargs={"table": "vitals_via_table"},
        )
        t_table_name = ImednetExportOperator(
            task_id="duckdb_custom_table_name_key",
            study_key="STUDY_DUCK_AF3",
            destination="duckdb",
            output_path=str(db_path),
            export_kwargs={"table_name": "vitals_via_table_name"},
        )
        t_default >> t_table >> t_table_name

    _register_dag_for_airflow3_test(dag, airflow3_sqlite_env["dags_dir"])
    dr = dag.test(
        logical_date=datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        run_after=datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
    )
    assert dr.state == DagRunState.SUCCESS

    with duckdb.connect(str(db_path), read_only=True, config={"threads": "1"}) as con:
        for table_ident in ("records", "vitals_via_table", "vitals_via_table_name"):
            rows = con.execute(
                f'SELECT "recordId", "subjectKey", "PATIENT_NAME", "HR" FROM {table_ident}'
            ).fetchall()
            assert rows == [(501, "SUBJ-501", "***MASKED***", 72)]


def test_airflow3_dagbag_loads_examples_airflow_with_zero_import_errors(airflow3_sqlite_env):
    """AC 6: Airflow 3 DagBag loads examples/airflow/ with 0 import errors."""
    from airflow.dag_processing.dagbag import DagBag

    examples_dir = airflow3_sqlite_env["repo_root"] / "examples" / "airflow"
    dagbag = DagBag(dag_folder=str(examples_dir))

    assert dagbag.import_errors == {}, f"Unexpected DagBag import errors: {dagbag.import_errors}"
    expected_dags = {
        "imednet_multi_study_reference",
        "imednet_export_example",
        "imednet_job_sensor_example",
    }
    assert expected_dags.issubset(set(dagbag.dags.keys()))
    for dag_id in expected_dags:
        assert dagbag.dags[dag_id].schedule is None


def test_job_sensor_fails_in_dag_when_result_url_has_failed_records(monkeypatch):
    """ImednetJobSensor fails inside a real Airflow DAG when terminal resultUrl reports Record errors."""
    _purge_partial_airflow_modules()
    pytest.importorskip("airflow")
    import httpx

    from apache_airflow_providers_imednet import ImednetJobSensor
    from apache_airflow_providers_imednet.operators import AirflowException
    from imednet import ImednetSDK

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/BATCH_BAD"):
            return httpx.Response(
                200,
                json={
                    "batchId": "BATCH_BAD",
                    "state": "COMPLETED",
                    "resultUrl": "https://edc.trn.imednetapi.com/results/BATCH_BAD.json",
                },
            )
        return httpx.Response(
            200,
            json=[{"recordKey": "SUBJ-001-1", "status": "FAILED", "error": "Form locked"}],
        )

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.trn.imednetapi.com",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(ImednetJobSensor, "_get_sdk", lambda self: sdk)

    sensor = ImednetJobSensor(task_id="wait_bad_batch", study_key="STUDY_A", batch_id="BATCH_BAD")
    with pytest.raises(AirflowException, match="Form locked"):
        sensor.poke(context={})
