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

    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    dags_dir = tmp_path / "dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "airflow.db"
    db_uri = f"sqlite:///{db_path}"
    async_db_uri = f"sqlite+aiosqlite:///{db_path}"

    import airflow.dag_processing.bundles.manager as bundle_manager_mod
    from airflow import settings
    from airflow.configuration import conf
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.utils import db

    prev_conn = conf.get("database", "sql_alchemy_conn", fallback=None)
    prev_pool_enabled = conf.get("database", "sql_alchemy_pool_enabled", fallback=None)
    prev_dags = conf.get("core", "dags_folder", fallback=None)
    prev_examples = conf.get("core", "load_examples", fallback=None)
    prev_bundles = conf.get("dag_processor", "dag_bundle_config_list", fallback=None)
    prev_settings_conn = settings.SQL_ALCHEMY_CONN
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
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_ENABLED", "False")
    monkeypatch.setenv("AIRFLOW__DAG_PROCESSOR__DAG_BUNDLE_CONFIG_LIST", bundle_cfg)

    conf.set("core", "dags_folder", str(dags_dir))
    conf.set("core", "load_examples", "False")
    conf.set("database", "sql_alchemy_conn", db_uri)
    conf.set("database", "sql_alchemy_pool_enabled", "False")
    conf.set("dag_processor", "dag_bundle_config_list", bundle_cfg)
    if hasattr(bundle_manager_mod, "_load_bundle_config_snapshot"):
        bundle_manager_mod._load_bundle_config_snapshot.cache_clear()
    monkeypatch.setattr(settings, "DAGS_FOLDER", str(dags_dir))
    monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN", db_uri)
    if hasattr(settings, "SQL_ALCHEMY_CONN_ASYNC"):
        monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN_ASYNC", async_db_uri)
    settings.dispose_orm()
    settings.configure_orm()
    db.initdb()
    DagBundlesManager().sync_bundles_to_db()

    yield dags_dir

    for env_key in list(os.environ):
        if env_key.startswith("AIRFLOW_CTX_"):
            os.environ.pop(env_key, None)

    settings.dispose_orm()
    settings.SQL_ALCHEMY_CONN = prev_settings_conn
    if hasattr(settings, "SQL_ALCHEMY_CONN_ASYNC"):
        settings.SQL_ALCHEMY_CONN_ASYNC = prev_settings_async_conn
    settings.DAGS_FOLDER = prev_settings_dags
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
    if hasattr(bundle_manager_mod, "_load_bundle_config_snapshot"):
        bundle_manager_mod._load_bundle_config_snapshot.cache_clear()
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
    assert len(seen_params) == 2
    assert "filter" not in seen_params[0]
    assert "dateModified>=" in seen_params[1].get("filter", "")

    provider = AirflowStateProvider()
    assert (
        provider.get_last_status("STUDY_EXPORT", "json_export_records")
        == StreamExecutionStatus.SUCCESS
    )
    assert provider.get_last_timestamp("STUDY_EXPORT", "json_export_records") == datetime(
        2026, 9, 10, 15, 0, 0, tzinfo=timezone.utc
    )
