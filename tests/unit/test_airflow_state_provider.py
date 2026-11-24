"""Real Apache Airflow 3.x execution tests for ``AirflowStateProvider``."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

if (
    "airflow" not in sys.modules
    or getattr(sys.modules.get("airflow"), "__path__", None) is None
    or not getattr(sys.modules.get("airflow"), "__file__", None)
):
    for _mod in list(sys.modules):
        if _mod == "airflow" or _mod.startswith(("airflow.", "apache_airflow_providers_imednet")):
            sys.modules.pop(_mod, None)

pytest.importorskip("airflow")


def _load_dag_from_file(dag_file: Path, dag_id: str) -> Any:
    """Load a DAG from a file inside ``AIRFLOW__CORE__DAGS_FOLDER``."""
    spec = importlib.util.spec_from_file_location(dag_file.stem, dag_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[dag_file.stem] = module
    spec.loader.exec_module(module)
    return getattr(module, dag_id)


@pytest.fixture
def airflow_runtime_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Initialize an isolated Apache Airflow 3 SQLite metadata DB and DAG bundle directory."""
    if (
        "airflow" not in sys.modules
        or getattr(sys.modules.get("airflow"), "__path__", None) is None
        or not getattr(sys.modules.get("airflow"), "__file__", None)
    ):
        for mod in list(sys.modules):
            if mod == "airflow" or mod.startswith(("airflow.", "apache_airflow_providers_imednet")):
                sys.modules.pop(mod, None)
    else:
        for mod in list(sys.modules):
            if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
                sys.modules.pop(mod, None)

    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    dags_dir = tmp_path / "dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "airflow.db"
    db_uri = f"sqlite:///{db_path}"
    async_db_uri = f"sqlite+aiosqlite:///{db_path}"

    from airflow import settings
    from airflow.configuration import conf
    from airflow.dag_processing import bundles
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.utils import db

    bundle_manager_mod = getattr(bundles, "manager", None)
    snapshot_cache = getattr(bundle_manager_mod, "_load_bundle_config_snapshot", None)

    prev_conn = conf.get("database", "sql_alchemy_conn", fallback=None)
    prev_async_conn = conf.get("database", "sql_alchemy_conn_async", fallback=None)
    prev_pool_enabled = conf.get("database", "sql_alchemy_pool_enabled", fallback=None)
    prev_dags = conf.get("core", "dags_folder", fallback=None)
    prev_examples = conf.get("core", "load_examples", fallback=None)
    prev_bundles = conf.get("dag_processor", "dag_bundle_config_list", fallback=None)
    prev_settings_conn = settings.SQL_ALCHEMY_CONN
    has_async_conn_attr = hasattr(settings, "SQL_ALCHEMY_CONN_ASYNC")
    prev_settings_async_conn = getattr(settings, "SQL_ALCHEMY_CONN_ASYNC", None)
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

    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", str(dags_dir))
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    monkeypatch.setenv("AIRFLOW__CORE__UNIT_TEST_MODE", "True")
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", db_uri)
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_ASYNC", async_db_uri)
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_ENABLED", "False")
    monkeypatch.setenv("AIRFLOW__DAG_PROCESSOR__DAG_BUNDLE_CONFIG_LIST", bundle_cfg)

    conf.set("core", "dags_folder", str(dags_dir))
    conf.set("core", "load_examples", "False")
    conf.set("database", "sql_alchemy_conn", db_uri)
    conf.set("database", "sql_alchemy_conn_async", async_db_uri)
    conf.set("database", "sql_alchemy_pool_enabled", "False")
    conf.set("dag_processor", "dag_bundle_config_list", bundle_cfg)
    if callable(getattr(snapshot_cache, "cache_clear", None)):
        snapshot_cache.cache_clear()

    settings.dispose_orm()
    settings.configure_vars()
    monkeypatch.setattr(settings, "DAGS_FOLDER", str(dags_dir))
    monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN", db_uri)
    if has_async_conn_attr:
        monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN_ASYNC", async_db_uri)
    settings.configure_orm()
    db.initdb()
    DagBundlesManager().sync_bundles_to_db()

    yield dags_dir

    for env_key in list(os.environ):
        if env_key.startswith("AIRFLOW_CTX_"):
            os.environ.pop(env_key, None)

    settings.dispose_orm()
    if prev_conn is not None:
        conf.set("database", "sql_alchemy_conn", prev_conn)
    if prev_async_conn is not None:
        conf.set("database", "sql_alchemy_conn_async", prev_async_conn)
    if prev_pool_enabled is not None:
        conf.set("database", "sql_alchemy_pool_enabled", prev_pool_enabled)
    if prev_dags is not None:
        conf.set("core", "dags_folder", prev_dags)
    if prev_examples is not None:
        conf.set("core", "load_examples", prev_examples)
    if prev_bundles is not None:
        conf.set("dag_processor", "dag_bundle_config_list", prev_bundles)
    if callable(getattr(snapshot_cache, "cache_clear", None)):
        snapshot_cache.cache_clear()
    settings.configure_vars()
    settings.SQL_ALCHEMY_CONN = prev_settings_conn
    if has_async_conn_attr:
        settings.SQL_ALCHEMY_CONN_ASYNC = prev_settings_async_conn
    settings.DAGS_FOLDER = prev_settings_dags
    settings.configure_orm()

    root_logger.handlers = saved_handlers
    root_logger.setLevel(saved_level)


def test_airflow_state_provider_real_variable_lifecycle_and_runtime_task(
    airflow_runtime_env: Path,
    tmp_path: Path,
) -> None:
    """Verify all 5 AirflowStateProvider methods outside and inside RuntimeTaskInstance."""
    from imednet_workflows.state_ledger import (
        AirflowStateProvider,
        InvalidStateTransitionError,
        StreamExecutionStatus,
    )

    provider = AirflowStateProvider(variable_prefix="imednet_real_state")
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 12, 30, 0, tzinfo=timezone.utc)

    # 1. Verify out-of-task Variable operations (CLI / inspection path)
    assert provider.get_last_timestamp("STUDY:TRN_01", "records") is None
    assert provider.get_last_status("STUDY:TRN_01", "records") is None

    provider.set_last_timestamp(
        "STUDY:TRN_01",
        "records",
        t1,
        records_processed=15,
        metadata={"source": "seed"},
    )
    provider.set_last_timestamp(
        "STUDY:TRN_01",
        "queries",
        t1,
        records_processed=4,
    )
    assert provider.get_last_timestamp("STUDY:TRN_01", "records") == t1
    assert provider.get_last_status("STUDY:TRN_01", "records") == StreamExecutionStatus.SUCCESS

    # Enforce state transition validation when force=False
    provider.set_last_timestamp(
        "STUDY_LOCK",
        "records",
        t1,
        status=StreamExecutionStatus.RUNNING,
    )
    with pytest.raises(InvalidStateTransitionError):
        provider.set_last_timestamp(
            "STUDY_LOCK",
            "records",
            t2,
            status=StreamExecutionStatus.RUNNING,
        )
    assert provider.delete_entry("STUDY_LOCK", "records") is True
    assert provider.delete_entry("STUDY_LOCK", "records") is False

    # 2. Execute inside a real Airflow 3 RuntimeTaskInstance (SUPERVISOR_COMMS + airflow.sdk.Variable)
    report_file = tmp_path / "runtime_report.json"
    dag_file = airflow_runtime_env / "dag_state_lifecycle.py"
    dag_file.write_text(
        textwrap.dedent(
            f"""
            import json
            from datetime import datetime, timezone
            from pathlib import Path
            from airflow.sdk import DAG, task
            from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

            with DAG("dag_state_lifecycle", start_date=datetime(2026, 1, 1, tzinfo=timezone.utc)) as dag:
                @task
                def exercise_provider() -> None:
                    p = AirflowStateProvider(variable_prefix="imednet_real_state")
                    assert p._has_active_task_sdk_comms() is True
                    prior = p.get_last_timestamp("STUDY:TRN_01", "records")
                    with p.transaction("STUDY:TRN_01", "records") as tx:
                        assert tx["last_timestamp"] == prior
                        tx["new_timestamp"] = datetime(2026, 6, 2, 12, 30, 0, tzinfo=timezone.utc)
                        tx["records_processed"] = 28
                        tx["metadata"] = {{"run": "runtime_ti"}}

                    ledger = p.read_state()
                    deleted_queries = p.delete_entry("STUDY:TRN_01", "queries")
                    after_stream_delete = p.read_state()
                    Path({str(report_file)!r}).write_text(
                        json.dumps({{
                            "studies": list(ledger.studies.keys()),
                            "records_processed": ledger.studies["STUDY:TRN_01"].streams["records"].records_processed,
                            "deleted_queries": deleted_queries,
                            "remaining_streams": list(after_stream_delete.studies["STUDY:TRN_01"].streams.keys()),
                            "status": p.get_last_status("STUDY:TRN_01", "records").value,
                        }})
                    )

                exercise_provider()
            """
        ),
        encoding="utf-8",
    )

    dag = _load_dag_from_file(dag_file, "dag")
    dag_run = dag.test()
    assert str(dag_run.state) == "success"

    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["studies"] == ["STUDY:TRN_01"]
    assert report["records_processed"] == 28
    assert report["deleted_queries"] is True
    assert report["remaining_streams"] == ["records"]
    assert report["status"] == StreamExecutionStatus.SUCCESS.value

    assert provider.get_last_timestamp("STUDY:TRN_01", "records") == t2
    assert provider.delete_entry("STUDY:TRN_01") is True
    assert provider.delete_entry("STUDY:TRN_01") is False
    assert provider.read_state().studies == {}


def test_airflow_dynamic_mapping_order_changes_preserve_study_checkpoints(
    airflow_runtime_env: Path,
    tmp_path: Path,
) -> None:
    """Dynamic task mapping order changes never cross-contaminate per-Study Variable checkpoints."""
    from imednet_workflows.state_ledger import AirflowStateProvider

    order_file = tmp_path / "study_order.json"
    observed_file = tmp_path / "observed_watermarks.jsonl"
    dag_file = airflow_runtime_env / "dag_mapped_checkpoints.py"

    dag_file.write_text(
        textwrap.dedent(
            f"""
            import json
            from datetime import datetime, timezone
            from pathlib import Path
            from airflow.sdk import DAG, task
            from imednet_workflows.state_ledger import AirflowStateProvider

            WATERMARKS = {{
                "STUDY_ALPHA": "2026-07-01T09:00:00+00:00",
                "STUDY_BETA": "2026-08-15T18:45:00+00:00",
            }}

            with DAG("dag_mapped_checkpoints", start_date=datetime(2026, 1, 1, tzinfo=timezone.utc)) as dag:
                @task
                def discover_studies() -> list[dict[str, str]]:
                    studies = json.loads(Path({str(order_file)!r}).read_text(encoding="utf-8"))
                    return [{{"study_key": sk}} for sk in studies]

                @task
                def sync_study(study_key: str) -> None:
                    provider = AirflowStateProvider(variable_prefix="imednet_mapped_state")
                    with provider.transaction(study_key, "records") as tx:
                        prior = tx["last_timestamp"].isoformat() if tx["last_timestamp"] else None
                        with Path({str(observed_file)!r}).open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps({{"study_key": study_key, "prior": prior}}) + "\\n")
                        tx["new_timestamp"] = datetime.fromisoformat(WATERMARKS[study_key])
                        tx["records_processed"] = 10

                sync_study.expand_kwargs(discover_studies())
            """
        ),
        encoding="utf-8",
    )

    # Run 1: ["STUDY_ALPHA", "STUDY_BETA"] -> map_index 0 is ALPHA, map_index 1 is BETA
    order_file.write_text(json.dumps(["STUDY_ALPHA", "STUDY_BETA"]), encoding="utf-8")
    dag_run1 = _load_dag_from_file(dag_file, "dag").test(
        logical_date=datetime(2026, 9, 1, tzinfo=timezone.utc)
    )
    assert str(dag_run1.state) == "success"

    # Run 2: Reverse order ["STUDY_BETA", "STUDY_ALPHA"] -> map_index 0 is BETA, map_index 1 is ALPHA
    order_file.write_text(json.dumps(["STUDY_BETA", "STUDY_ALPHA"]), encoding="utf-8")
    dag_run2 = _load_dag_from_file(dag_file, "dag").test(
        logical_date=datetime(2026, 9, 2, tzinfo=timezone.utc)
    )
    assert str(dag_run2.state) == "success"

    events = [
        json.loads(line) for line in observed_file.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(events) == 4
    run2_events = {item["study_key"]: item["prior"] for item in events[2:]}
    assert run2_events == {
        "STUDY_BETA": "2026-08-15T18:45:00+00:00",
        "STUDY_ALPHA": "2026-07-01T09:00:00+00:00",
    }

    provider = AirflowStateProvider(variable_prefix="imednet_mapped_state")
    assert provider.get_last_timestamp("STUDY_ALPHA", "records") == datetime(
        2026, 7, 1, 9, 0, tzinfo=timezone.utc
    )
    assert provider.get_last_timestamp("STUDY_BETA", "records") == datetime(
        2026, 8, 15, 18, 45, tzinfo=timezone.utc
    )


def test_airflow_task_timeout_base_exception_rolls_back_and_retries(
    airflow_runtime_env: Path,
    tmp_path: Path,
) -> None:
    """AirflowTaskTimeout (BaseException) rolls RUNNING back to FAILED so retry succeeds."""
    from airflow.sdk.exceptions import AirflowTaskTimeout

    from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

    assert issubclass(AirflowTaskTimeout, BaseException)
    assert not issubclass(AirflowTaskTimeout, Exception)

    provider = AirflowStateProvider(variable_prefix="imednet_timeout_state")
    prior_ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    provider.set_last_timestamp("STUDY_TIMEOUT", "records", prior_ts, records_processed=5)

    flag_file = tmp_path / "should_timeout.json"
    dag_file = airflow_runtime_env / "dag_timeout_rollback.py"
    dag_file.write_text(
        textwrap.dedent(
            f"""
            import json
            from datetime import datetime, timezone
            from pathlib import Path
            from airflow.sdk import DAG, task
            from airflow.sdk.exceptions import AirflowTaskTimeout
            from imednet_workflows.state_ledger import AirflowStateProvider

            with DAG("dag_timeout_rollback", start_date=datetime(2026, 1, 1, tzinfo=timezone.utc)) as dag:
                @task
                def export_with_timeout() -> None:
                    provider = AirflowStateProvider(variable_prefix="imednet_timeout_state")
                    should_timeout = json.loads(Path({str(flag_file)!r}).read_text(encoding="utf-8"))
                    with provider.transaction("STUDY_TIMEOUT", "records") as tx:
                        tx["new_timestamp"] = datetime(2026, 4, 2, 0, 0, 0, tzinfo=timezone.utc)
                        tx["records_processed"] = 50
                        if should_timeout:
                            raise AirflowTaskTimeout("Execution timed out after 30m")

                export_with_timeout()
            """
        ),
        encoding="utf-8",
    )

    # Attempt 1: Task times out with AirflowTaskTimeout (BaseException)
    flag_file.write_text("true", encoding="utf-8")
    dag_run1 = _load_dag_from_file(dag_file, "dag").test(
        logical_date=datetime(2026, 4, 2, tzinfo=timezone.utc)
    )
    assert str(dag_run1.state) == "failed"
    assert provider.get_last_status("STUDY_TIMEOUT", "records") == StreamExecutionStatus.FAILED
    assert provider.get_last_timestamp("STUDY_TIMEOUT", "records") == prior_ts

    # Attempt 2: Retry succeeds cleanly (FAILED -> RUNNING -> SUCCESS) without wedging on RUNNING
    flag_file.write_text("false", encoding="utf-8")
    dag_run2 = _load_dag_from_file(dag_file, "dag").test(
        logical_date=datetime(2026, 4, 3, tzinfo=timezone.utc)
    )
    assert str(dag_run2.state) == "success"
    assert provider.get_last_status("STUDY_TIMEOUT", "records") == StreamExecutionStatus.SUCCESS
    assert provider.get_last_timestamp("STUDY_TIMEOUT", "records") == datetime(
        2026, 4, 2, 0, 0, 0, tzinfo=timezone.utc
    )


def test_imednet_export_operator_incremental_idempotent_in_real_airflow_dag(
    airflow_runtime_env: Path,
    tmp_path: Path,
) -> None:
    """ImednetExportOperator(idempotent=True, incremental=True) uses Variable checkpoints in RuntimeTaskInstance."""
    from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

    out_json = tmp_path / "exported_records.json"
    queries_log = tmp_path / "seen_queries.jsonl"
    dag_file = airflow_runtime_env / "dag_export_incremental.py"

    dag_file.write_text(
        textwrap.dedent(
            f"""
            import json
            import traceback
            from datetime import datetime, timezone
            from pathlib import Path
            import httpx
            import airflow.utils.session as af_session
            from airflow.sdk import DAG
            from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
            from imednet import ImednetSDK

            class MockExportOperator(ImednetExportOperator):
                def _get_sdk(self) -> ImednetSDK:
                    def handler(request: httpx.Request) -> httpx.Response:
                        if request.url.path.endswith("/variables"):
                            return httpx.Response(
                                200,
                                json={{
                                    "data": [
                                        {{
                                            "studyKey": "STUDY_EXPORT",
                                            "variableId": 1,
                                            "variableName": "AGE",
                                            "variableType": "integer",
                                            "label": "Age",
                                            "formId": 10,
                                            "formKey": "DM",
                                            "formName": "Demographics",
                                        }}
                                    ],
                                    "pagination": {{"totalPages": 1}},
                                }},
                            )
                        if request.url.path.endswith("/records"):
                            with Path({str(queries_log)!r}).open("a", encoding="utf-8") as fh:
                                fh.write(json.dumps(dict(request.url.params)) + "\\n")
                            return httpx.Response(
                                200,
                                json={{
                                    "data": [
                                        {{
                                            "studyKey": "STUDY_EXPORT",
                                            "recordId": 101,
                                            "subjectId": 1,
                                            "subjectKey": "SUBJ-1",
                                            "formId": 10,
                                            "formKey": "DM",
                                            "dateCreated": "2026-09-10 12:00:00",
                                            "dateModified": "2026-09-10 15:00:00",
                                            "recordData": {{"AGE": 42}},
                                        }}
                                    ],
                                    "pagination": {{"totalPages": 1}},
                                }},
                            )
                        return httpx.Response(200, json={{"data": [], "pagination": {{"totalPages": 1}}}})

                    return ImednetSDK(
                        api_key="k",
                        security_key="s",
                        base_url="https://edc.trn.imednetapi.com",
                        transport=httpx.MockTransport(handler),
                    )

                def execute(self, context):
                    # Verify RuntimeTaskInstance only accesses Airflow DB via the FastAPI Execution API,
                    # never via direct ORM create_session from imednet / provider / workflow modules.
                    orig_create_session = af_session.create_session
                    def _forbid_direct_orm(*args, **kwargs):
                        stack_files = [frame.filename for frame in traceback.extract_stack()]
                        has_imednet_caller = any(
                            "/packages/" in fn for fn in stack_files
                        )
                        via_execution_api = any(
                            "execution_api" in fn for fn in stack_files
                        )
                        if has_imednet_caller and not via_execution_api:
                            raise AssertionError(
                                "Direct SQLAlchemy session access forbidden inside RuntimeTaskInstance"
                            )
                        return orig_create_session(*args, **kwargs)
                    af_session.create_session = _forbid_direct_orm
                    try:
                        return super().execute(context)
                    finally:
                        af_session.create_session = orig_create_session

            with DAG("dag_export_incremental", start_date=datetime(2026, 1, 1, tzinfo=timezone.utc)) as dag:
                MockExportOperator(
                    task_id="export_records",
                    study_key="STUDY_EXPORT",
                    destination="json",
                    output_path={str(out_json)!r},
                    idempotent=True,
                    incremental=True,
                    quality_gate_enabled=False,
                )
            """
        ),
        encoding="utf-8",
    )

    dag_run1 = _load_dag_from_file(dag_file, "dag").test(
        logical_date=datetime(2026, 9, 10, tzinfo=timezone.utc)
    )
    assert str(dag_run1.state) == "success"

    dag_run2 = _load_dag_from_file(dag_file, "dag").test(
        logical_date=datetime(2026, 9, 11, tzinfo=timezone.utc)
    )
    assert str(dag_run2.state) == "success"

    seen_params = [
        json.loads(line) for line in queries_log.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(seen_params) == 3
    assert "filter" not in seen_params[0]
    assert "dateModified>=" in seen_params[1].get("filter", "")
    assert "lastUpdated>=" in seen_params[2].get("filter", "")

    provider = AirflowStateProvider()
    study_streams = provider.read_state().studies["STUDY_EXPORT"].streams
    assert len(study_streams) == 1
    stream_key, stream_state = next(iter(study_streams.items()))
    assert stream_key.startswith("json_export_records_")
    assert stream_state.last_run_status == StreamExecutionStatus.SUCCESS
    assert stream_state.last_timestamp == datetime(2026, 9, 10, 15, 0, 0, tzinfo=timezone.utc)


def test_airflow_state_provider_redacts_sensitive_error_and_metadata(
    airflow_runtime_env: Path,
) -> None:
    """AirflowStateProvider redacts URI credentials and secret tokens before writing Variables."""
    _ = airflow_runtime_env
    from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

    provider = AirflowStateProvider()
    provider.set_last_timestamp(
        study_key="STUDY_REDACT",
        stream_name="records",
        timestamp=datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc),
        status=StreamExecutionStatus.FAILED,
        error_message=(
            "Export failed for postgresql://loader:RAW_DB_PASS@db.internal:5432/edc "  # pragma: allowlist secret
            "with api_key=RAW_API_SECRET"  # pragma: allowlist secret
        ),
        metadata={
            "target": "edc",
            "password": "RAW_META_SECRET",  # pragma: allowlist secret
        },
        force=True,
    )

    stream_state = provider.read_state().studies["STUDY_REDACT"].streams["records"]
    assert stream_state.error_message is not None
    assert "RAW_DB_PASS" not in stream_state.error_message
    assert "RAW_API_SECRET" not in stream_state.error_message
    assert "RAW_META_SECRET" not in json.dumps(stream_state.metadata)
    assert stream_state.metadata["target"] == "edc"


def test_50_form_export_runtime_comms_orm_sql_budgets_and_legacy_migration(
    airflow_runtime_env: Path,
    tmp_path: Path,
) -> None:
    """Verify 50-form export_to_sql_by_form RPC/SQL budgets, 250-stream read_state, 1-DELETE reset, and legacy migration."""
    from airflow import settings
    from airflow.models.variable import Variable as ModelVariable
    from sqlalchemy import event

    from imednet.integrations.export import export_to_sql_by_form
    from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

    # 1. RuntimeTaskInstance: 50-form export_to_sql_by_form executes 1 GetVariable and 2 PutVariable calls
    comms_report = tmp_path / "comms_50_forms.json"
    rt_sqlite = tmp_path / "rt_50_forms.sqlite"
    provider_rt = AirflowStateProvider(variable_prefix="imednet_50f_rt")
    seed_ts = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    with provider_rt.batch_transaction(
        "STUDY_50_RT", [f"sql_F{i:02d}" for i in range(1, 51)]
    ) as seed_tx:
        for i in range(1, 51):
            seed_tx[f"sql_F{i:02d}"]["new_timestamp"] = seed_ts

    dag_file = airflow_runtime_env / "dag_50_form_comms.py"
    dag_file.write_text(
        textwrap.dedent(
            f"""
            import json
            import traceback
            from pathlib import Path
            import httpx
            import airflow.utils.session as af_session
            from airflow.sdk import DAG, task
            from airflow.sdk.execution_time import task_runner
            from imednet import ImednetSDK
            from imednet.integrations.export import export_to_sql_by_form
            from imednet_workflows.state_ledger import AirflowStateProvider
            from datetime import datetime, timezone

            with DAG("dag_50_form_comms", start_date=datetime(2026, 1, 1, tzinfo=timezone.utc)) as dag:
                @task
                def run_50_form_export() -> None:
                    forms_payload = [
                        {{"studyKey": "STUDY_50_RT", "formId": i, "formKey": f"F{{i:02d}}", "formName": f"Form {{i}}"}}
                        for i in range(1, 51)
                    ]
                    vars_payload = [
                        {{
                            "studyKey": "STUDY_50_RT",
                            "variableId": i,
                            "variableName": f"V_{{i:02d}}",
                            "variableType": "integer",
                            "label": f"Var {{i}}",
                            "formId": i,
                            "formKey": f"F{{i:02d}}",
                            "formName": f"Form {{i}}",
                        }}
                        for i in range(1, 51)
                    ]
                    recs_payload = [
                        {{
                            "studyKey": "STUDY_50_RT",
                            "recordId": 1000 + i,
                            "subjectId": 1,
                            "subjectKey": "SUBJ-1",
                            "formId": i,
                            "formKey": f"F{{i:02d}}",
                            "dateCreated": "2026-09-05 10:00:00",
                            "dateModified": "2026-09-05 12:00:00",
                            "recordData": {{f"V_{{i:02d}}": i}},
                        }}
                        for i in range(1, 51)
                    ]

                    def handler(request: httpx.Request) -> httpx.Response:
                        if request.url.path.endswith("/forms"):
                            return httpx.Response(200, json={{"data": forms_payload, "pagination": {{"totalPages": 1}}}})
                        if request.url.path.endswith("/variables"):
                            return httpx.Response(200, json={{"data": vars_payload, "pagination": {{"totalPages": 1}}}})
                        if request.url.path.endswith("/records"):
                            return httpx.Response(200, json={{"data": recs_payload, "pagination": {{"totalPages": 1}}}})
                        return httpx.Response(200, json={{"data": [], "pagination": {{"totalPages": 1}}}})

                    sdk = ImednetSDK(
                        api_key="k",
                        security_key="s",
                        base_url="https://edc.trn.imednetapi.com",
                        transport=httpx.MockTransport(handler),
                    )

                    seen_msgs: list[str] = []
                    orig_comms = task_runner.SUPERVISOR_COMMS

                    class _CommsSpy:
                        def __init__(self, inner):
                            self._inner = inner

                        def send(self, msg, *args, **kwargs):
                            msg_name = type(msg).__name__
                            if "Variable" in msg_name:
                                seen_msgs.append(msg_name)
                            return self._inner.send(msg, *args, **kwargs)

                        def __getattr__(self, name):
                            return getattr(self._inner, name)

                    orig_create_session = af_session.create_session
                    def _forbid_direct_orm(*args, **kwargs):
                        stack_files = [frame.filename for frame in traceback.extract_stack()]
                        if any("/packages/" in fn for fn in stack_files) and not any(
                            "execution_api" in fn for fn in stack_files
                        ):
                            raise AssertionError("Direct ORM forbidden inside RuntimeTaskInstance")
                        return orig_create_session(*args, **kwargs)

                    task_runner.SUPERVISOR_COMMS = _CommsSpy(orig_comms)
                    af_session.create_session = _forbid_direct_orm
                    try:
                        export_to_sql_by_form(
                            sdk,
                            "STUDY_50_RT",
                            f"sqlite:///{str(rt_sqlite)}",
                            incremental=True,
                            quality_gate_enabled=False,
                            mask_phi=False,
                            state_provider=AirflowStateProvider(variable_prefix="imednet_50f_rt"),
                        )
                    finally:
                        task_runner.SUPERVISOR_COMMS = orig_comms
                        af_session.create_session = orig_create_session

                    Path({str(comms_report)!r}).write_text(json.dumps(seen_msgs), encoding="utf-8")

                run_50_form_export()
            """
        ),
        encoding="utf-8",
    )

    dag_run = _load_dag_from_file(dag_file, "dag").test(
        logical_date=datetime(2026, 9, 5, tzinfo=timezone.utc)
    )
    assert str(dag_run.state) == "success"
    seen_rt_msgs = json.loads(comms_report.read_text(encoding="utf-8"))
    assert seen_rt_msgs == ["GetVariable", "PutVariable", "PutVariable"]
    rt_state = provider_rt.read_state()
    assert len(rt_state.studies["STUDY_50_RT"].streams) == 50
    assert provider_rt.get_last_timestamp("STUDY_50_RT", "sql_F50") == datetime(
        2026, 9, 5, 12, 0, tzinfo=timezone.utc
    )

    # 2. ORM mode: 50-form export_to_sql_by_form executes <= 3 SQL statements and <= 2 session commits
    import httpx

    from imednet import ImednetSDK

    forms_orm = [
        {"studyKey": "STUDY_50_ORM", "formId": i, "formKey": f"F{i:02d}", "formName": f"Form {i}"}
        for i in range(1, 51)
    ]
    vars_orm = [
        {
            "studyKey": "STUDY_50_ORM",
            "variableId": i,
            "variableName": f"V_{i:02d}",
            "variableType": "integer",
            "label": f"Var {i}",
            "formId": i,
            "formKey": f"F{i:02d}",
            "formName": f"Form {i}",
        }
        for i in range(1, 51)
    ]
    recs_orm = [
        {
            "studyKey": "STUDY_50_ORM",
            "recordId": 2000 + i,
            "subjectId": 1,
            "subjectKey": "SUBJ-1",
            "formId": i,
            "formKey": f"F{i:02d}",
            "dateCreated": "2026-09-06 10:00:00",
            "dateModified": "2026-09-06 14:00:00",
            "recordData": {f"V_{i:02d}": i},
        }
        for i in range(1, 51)
    ]

    def _orm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/forms"):
            return httpx.Response(200, json={"data": forms_orm, "pagination": {"totalPages": 1}})
        if request.url.path.endswith("/variables"):
            return httpx.Response(200, json={"data": vars_orm, "pagination": {"totalPages": 1}})
        if request.url.path.endswith("/records"):
            return httpx.Response(200, json={"data": recs_orm, "pagination": {"totalPages": 1}})
        return httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})

    orm_sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.trn.imednetapi.com",
        transport=httpx.MockTransport(_orm_handler),
    )
    provider_orm = AirflowStateProvider(variable_prefix="imednet_orm_bench")
    orm_sqlite = tmp_path / "orm_50_forms.sqlite"

    airflow_sql_statements: list[str] = []
    airflow_commits: list[int] = []

    def _on_before_cursor(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        _ = (conn, cursor, parameters, context, executemany)
        airflow_sql_statements.append(str(statement).strip())

    def _on_after_commit(session: Any) -> None:
        _ = session
        airflow_commits.append(1)

    assert settings.engine is not None
    event.listen(settings.engine, "before_cursor_execute", _on_before_cursor)
    event.listen(settings.Session, "after_commit", _on_after_commit)
    try:
        export_to_sql_by_form(
            orm_sdk,
            "STUDY_50_ORM",
            f"sqlite:///{orm_sqlite}",
            incremental=True,
            quality_gate_enabled=False,
            mask_phi=False,
            state_provider=provider_orm,
        )
        assert len(airflow_sql_statements) <= 3
        assert len(airflow_commits) <= 2

        # 3. 250 streams across 5 studies in ORM read_state -> 1 SQL SELECT, 0 commits, autoescape=True
        for s_idx in range(2, 6):
            with provider_orm.batch_transaction(
                f"STUDY_50_ORM_{s_idx}", [f"sql_F{i:02d}" for i in range(1, 51)]
            ) as btx:
                for i in range(1, 51):
                    btx[f"sql_F{i:02d}"]["new_timestamp"] = seed_ts
        # Decoy matching wildcard '_' if autoescape=True were missing on 'imednet_orm_bench:'
        ModelVariable.set(
            "imednetXormXbench:DECOY",
            {"study_key": "DECOY", "streams": {"s": {"status": "success"}}},
            serialize_json=True,
        )

        airflow_sql_statements.clear()
        airflow_commits.clear()
        orm_ledger = provider_orm.read_state()
        select_stmts = [s for s in airflow_sql_statements if s.upper().startswith("SELECT")]
        assert len(select_stmts) == 1
        assert len(airflow_sql_statements) == 1
        assert len(airflow_commits) == 0
        assert len(orm_ledger.studies) == 5
        assert "DECOY" not in orm_ledger.studies
        assert sum(len(s.streams) for s in orm_ledger.studies.values()) == 250

        # 4. ORM delete_entry(study_key) -> 1 SQL DELETE statement while preserving sibling studies
        ModelVariable.set(
            "imednet_orm_bench:STUDY_50XORM:decoy_stream",
            {"study_key": "STUDY_50XORM", "stream_name": "decoy_stream", "status": "success"},
            serialize_json=True,
        )
        airflow_sql_statements.clear()
        assert provider_orm.delete_entry("STUDY_50_ORM") is True
        delete_stmts = [s for s in airflow_sql_statements if s.upper().startswith("DELETE")]
        assert len(delete_stmts) == 1
        assert len(airflow_sql_statements) == 1
        assert "STUDY_50_ORM_2" in provider_orm.read_state().studies
        assert (
            ModelVariable.get("imednet_orm_bench:STUDY_50XORM:decoy_stream", default_var=None)
            is not None
        )

        # 5. ORM legacy per-stream keys load on read and consolidate + delete legacy rows on next write
        legacy_provider = AirflowStateProvider(variable_prefix="imednet_legacy_orm")
        ModelVariable.set(
            "imednet_legacy_orm:ST_UPG:sql_DM",
            {
                "study_key": "ST_UPG",
                "stream_name": "sql_DM",
                "last_timestamp": "2026-08-01T00:00:00+00:00",
                "records_processed": 9,
                "status": "success",
            },
            serialize_json=True,
        )
        ModelVariable.set(
            "imednet_legacy_orm:ST_UPG:sql_AE",
            {
                "study_key": "ST_UPG",
                "stream_name": "sql_AE",
                "last_timestamp": "2026-08-02T00:00:00+00:00",
                "records_processed": 4,
                "status": "success",
            },
            serialize_json=True,
        )
        assert legacy_provider.get_last_timestamp("ST_UPG", "sql_AE") == datetime(
            2026, 8, 2, 0, 0, tzinfo=timezone.utc
        )
        with legacy_provider.batch_transaction("ST_UPG", ["sql_DM", "sql_AE"]) as upg_tx:
            assert upg_tx["sql_DM"]["last_timestamp"] == datetime(
                2026, 8, 1, 0, 0, tzinfo=timezone.utc
            )
            upg_tx["sql_DM"]["new_timestamp"] = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)

        assert ModelVariable.get("imednet_legacy_orm:ST_UPG:sql_DM", default_var=None) is None
        assert ModelVariable.get("imednet_legacy_orm:ST_UPG:sql_AE", default_var=None) is None
        consolidated_upg = ModelVariable.get(
            "imednet_legacy_orm:ST_UPG", default_var=None, deserialize_json=True
        )
        assert set(consolidated_upg["streams"]) == {"sql_DM", "sql_AE"}
        assert legacy_provider.get_last_status("ST_UPG", "sql_DM") == StreamExecutionStatus.SUCCESS
    finally:
        event.remove(settings.engine, "before_cursor_execute", _on_before_cursor)
        event.remove(settings.Session, "after_commit", _on_after_commit)


def test_airflow_state_provider_escapes_sql_like_wildcards_on_list_read_and_delete(
    airflow_runtime_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AirflowStateProvider escapes _ and % SQL LIKE wildcards so delete_entry('STUDY_1') preserves 'STUDY-1' and 'STUDYX1'."""
    _ = airflow_runtime_env
    from airflow.models import Variable as ModelVariable

    from imednet_workflows.state_ledger import AirflowStateProvider, StreamExecutionStatus

    provider = AirflowStateProvider(variable_prefix="imednet_wild_1")
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

    for sk in ("STUDY_1", "STUDY-1", "STUDYX1", "STUDY_10", "STUDY%1"):
        provider.set_last_timestamp(sk, "records", ts, records_processed=5)

    # Also plant a legacy per-stream key for STUDY_1 and a wildcard-collision decoy legacy key for STUDYX1
    ModelVariable.set(
        "imednet_wild_1:STUDY_1:legacy_ae",
        {"study_key": "STUDY_1", "stream_name": "legacy_ae", "status": "success"},
        serialize_json=True,
    )
    ModelVariable.set(
        "imednet_wild_1:STUDYX1:legacy_ae",
        {"study_key": "STUDYX1", "stream_name": "legacy_ae", "status": "success"},
        serialize_json=True,
    )
    # Decoy variable prefix where '_' in variable_prefix would match 'X' if unescaped
    ModelVariable.set(
        "imednetXwildX1:STUDY_1",
        {"study_key": "STUDY_1", "streams": {"records": {"status": "success"}}},
        serialize_json=True,
    )

    assert provider.delete_entry("STUDY_1") is True
    assert provider.delete_entry("STUDY_1") is False

    remaining = provider.read_state().studies
    assert "STUDY_1" not in remaining
    assert set(remaining) == {"STUDY-1", "STUDYX1", "STUDY_10", "STUDY%1"}
    for preserved in ("STUDY-1", "STUDYX1", "STUDY_10", "STUDY%1"):
        assert provider.get_last_status(preserved, "records") == StreamExecutionStatus.SUCCESS
    assert ModelVariable.get("imednet_wild_1:STUDYX1:legacy_ae", default_var=None) is not None
    assert ModelVariable.get("imednetXwildX1:STUDY_1", default_var=None) is not None

    # Also verify Task SDK Variable.keys path filters exact prefix when backend ignores LIKE escaping
    class _UnescapedSdkVar:
        @staticmethod
        def keys(prefix: str = "") -> list[str]:
            _ = prefix
            return [
                "imednet_wild_1:STUDY_1:s1",
                "imednet_wild_1:STUDY-1:s1",
                "imednet_wild_1:STUDYX1:s1",
            ]

    monkeypatch.setattr(provider, "_has_active_task_sdk_comms", lambda: True)
    monkeypatch.setattr("airflow.sdk.Variable", _UnescapedSdkVar)
    assert provider._list_variable_keys("imednet_wild_1:STUDY_1:") == ["imednet_wild_1:STUDY_1:s1"]


def test_airflow_state_provider_retains_metadata_and_reclaims_retry_lease(
    airflow_runtime_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AirflowStateProvider retains metadata on metadata=None and reclaims crashed cross-host retry leases."""
    _ = airflow_runtime_env

    from imednet_workflows.state_ledger import (
        AirflowStateProvider,
        InvalidStateTransitionError,
        StreamExecutionStatus,
    )

    provider = AirflowStateProvider(variable_prefix="imednet_meta_retry")
    meta = {
        "sdtm_seq_counters": {"DOB": 6, "API_TOKEN": 2},
        "sdtm_record_seqs": {"DOB::10": 6},
        "custom_note": "keep-me",
    }
    provider.set_last_timestamp(
        "STUDY_MR",
        "ae",
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        records_processed=6,
        metadata=meta,
    )
    provider.set_last_timestamp(
        "STUDY_MR",
        "ae",
        datetime(2026, 7, 2, tzinfo=timezone.utc),
        records_processed=8,
        metadata=None,
    )
    loaded = provider.read_state().studies["STUDY_MR"].streams["ae"]
    assert loaded.last_timestamp == datetime(2026, 7, 2, tzinfo=timezone.utc)
    assert loaded.metadata == meta

    # Simulate crashed cross-host RUNNING lease in Airflow Variable via SIGKILL on try_number=1
    import socket

    provider.set_last_timestamp(
        "STUDY_CRASH",
        "records",
        datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
        records_processed=3,
    )
    monkeypatch.setenv("AIRFLOW_CTX_DAG_ID", "dag_crash")
    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "export_task")
    monkeypatch.setenv("AIRFLOW_CTX_DAG_RUN_ID", "run_01")
    monkeypatch.setenv("AIRFLOW_CTX_TRY_NUMBER", "1")
    with monkeypatch.context() as worker_a:
        worker_a.setattr(socket, "gethostname", lambda: "remote-celery-worker-1")
        worker_a.setattr("os.getpid", lambda: 77777)
        tx_cm = provider.transaction("STUDY_CRASH", "records")
        tx_cm.__enter__()
        tx_cm.gen.close()

    assert provider.get_last_status("STUDY_CRASH", "records") == StreamExecutionStatus.RUNNING

    # Concurrent unrelated task is blocked
    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "other_task")
    monkeypatch.setenv("AIRFLOW_CTX_TRY_NUMBER", "2")
    with pytest.raises(InvalidStateTransitionError):
        with provider.transaction("STUDY_CRASH", "records"):
            pass

    # Retry attempt (try_number=2) of the same task reclaims immediately without waiting 3600s
    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "export_task")
    recovered_ts = datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)
    with provider.transaction("STUDY_CRASH", "records") as tx:
        assert tx["last_timestamp"] == datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
        tx["new_timestamp"] = recovered_ts
        tx["records_processed"] = 11

    assert provider.get_last_status("STUDY_CRASH", "records") == StreamExecutionStatus.SUCCESS
    assert provider.get_last_timestamp("STUDY_CRASH", "records") == recovered_ts
