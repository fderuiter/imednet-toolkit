"""Airflow 3.x ORM and RuntimeTaskInstance SDTM lineage caching tests."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sqlite3
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pandas as pd
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
    db_path = tmp_path / "airflow_sdtm.db"
    db_uri = f"sqlite:///{db_path}"
    async_db_uri = f"sqlite+aiosqlite:///{db_path}"

    from airflow import settings as _af_settings
    from airflow.configuration import conf
    from airflow.dag_processing import bundles
    from airflow.dag_processing.bundles import manager as _bundles_mgr
    from airflow.utils import db

    settings: Any = _af_settings
    dag_bundles_manager_cls: Any = _bundles_mgr.DagBundlesManager
    bundle_manager_mod = getattr(bundles, "manager", None)
    snapshot_cache: Any = getattr(bundle_manager_mod, "_load_bundle_config_snapshot", None)

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
    cache_clear_fn = getattr(snapshot_cache, "cache_clear", None)
    if callable(cache_clear_fn):
        cache_clear_fn()

    settings.dispose_orm()
    settings.configure_vars()
    monkeypatch.setattr(settings, "DAGS_FOLDER", str(dags_dir))
    monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN", db_uri)
    if has_async_conn_attr:
        monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN_ASYNC", async_db_uri)
    settings.configure_orm()
    db.initdb()
    dag_bundles_manager_cls().sync_bundles_to_db()

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
    cache_clear_restore = getattr(snapshot_cache, "cache_clear", None)
    if callable(cache_clear_restore):
        cache_clear_restore()
    settings.configure_vars()
    settings.SQL_ALCHEMY_CONN = prev_settings_conn
    if has_async_conn_attr:
        settings.SQL_ALCHEMY_CONN_ASYNC = prev_settings_async_conn
    settings.DAGS_FOLDER = prev_settings_dags
    settings.configure_orm()

    root_logger.handlers = saved_handlers
    root_logger.setLevel(saved_level)


def test_airflow_orm_20_batch_operator_executes_1_select_and_rolls_back_cleanly(
    airflow_runtime_env: Path,
    tmp_path: Path,
) -> None:
    """AirflowStateProvider ORM mode executes 0 get_sdtm_lineage and 1 SQL SELECT on 20-batch ImednetExportOperator."""
    _ = airflow_runtime_env
    from airflow import settings
    from airflow.models import Variable as ModelVariable
    from sqlalchemy import event

    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet import ImednetSDK
    from imednet.errors import ExportError
    from imednet_workflows.state_ledger import AirflowStateProvider

    forms_payload = [
        {"studyKey": "STUDY_AF_ORM", "formId": 303, "formKey": "F_AE", "formName": "Adverse Events"}
    ]
    vars_payload = [
        {
            "studyKey": "STUDY_AF_ORM",
            "variableId": 7,
            "variableName": "AETERM",
            "variableType": "text",
            "label": "AE Term",
            "formId": 303,
            "formKey": "F_AE",
            "formName": "Adverse Events",
        }
    ]
    seed_recs = [
        {
            "studyKey": "STUDY_AF_ORM",
            "recordId": i,
            "subjectId": 1,
            "subjectKey": "SUBJ-01",
            "formId": 303,
            "formKey": "F_AE",
            "dateCreated": "2026-09-01 00:00:00",
            "dateModified": f"2026-09-01 09:0{i}:00",
            "recordData": {"AETERM": f"Seed {i}"},
        }
        for i in (1, 2)
    ]
    recs_payload = [
        {
            "studyKey": "STUDY_AF_ORM",
            "recordId": 100 + i,
            "subjectId": 1,
            "subjectKey": "SUBJ-01",
            "formId": 303,
            "formKey": "F_AE",
            "dateCreated": "2026-09-01 00:00:00",
            "dateModified": f"2026-09-10 10:{i:02d}:00",
            "recordData": {"AETERM": f"ORM Event {i}"},
        }
        for i in range(1, 21)
    ]

    active_recs: list[dict[str, Any]] = list(seed_recs)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/forms"):
            return httpx.Response(
                200, json={"data": forms_payload, "pagination": {"totalPages": 1}}
            )
        if request.url.path.endswith("/variables"):
            return httpx.Response(200, json={"data": vars_payload, "pagination": {"totalPages": 1}})
        if request.url.path.endswith("/records"):
            return httpx.Response(200, json={"data": active_recs, "pagination": {"totalPages": 1}})
        return httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})

    def _make_sdk() -> ImednetSDK:
        return ImednetSDK(
            api_key="k",
            security_key="s",
            base_url="https://edc.trn.imednetapi.com",
            transport=httpx.MockTransport(_handler),
        )

    provider = AirflowStateProvider(variable_prefix="imednet_sdtm_orm")
    csv_out = tmp_path / "orm_20_batches.csv"

    op = ImednetExportOperator(
        task_id="export_ae_sdtm_orm",
        study_key="STUDY_AF_ORM",
        destination="csv",
        output_path=str(csv_out),
        as_sdtm=True,
        incremental=True,
        batch_size=1,
        quality_gate_enabled=False,
        export_kwargs={"state_provider": provider},
    )
    orm_ctx: Any = {"dag_id": "dag_orm_sdtm"}
    stream_name = op._default_airflow_stream_name("csv", context=orm_ctx)

    # Seed Variable row via a real initial 2-record export so masked USUBJID lineage is authentic
    with patch.object(op, "_get_sdk", side_effect=_make_sdk):
        op.execute(orm_ctx)

    active_recs = list(recs_payload)
    sql_statements: list[str] = []

    def _on_before_cursor(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        _ = (conn, cursor, parameters, context, executemany)
        sql_statements.append(str(statement).strip())

    assert settings.engine is not None
    event.listen(settings.engine, "before_cursor_execute", _on_before_cursor)
    try:
        with (
            patch.object(op, "_get_sdk", side_effect=_make_sdk),
            patch.object(
                provider, "get_sdtm_lineage", wraps=provider.get_sdtm_lineage
            ) as spy_get_lineage,
        ):
            op.execute(orm_ctx)
    finally:
        event.remove(settings.engine, "before_cursor_execute", _on_before_cursor)

    select_queries = [q for q in sql_statements if q.upper().startswith("SELECT")]
    assert spy_get_lineage.call_count == 0
    assert len(select_queries) == 1

    df = pd.read_csv(csv_out)
    assert len(df) == 22
    assert sorted(df["AESEQ"].tolist()) == list(range(1, 23))

    # Verify Variable JSON has no transient private keys
    raw_var = ModelVariable.get("imednet_sdtm_orm:STUDY_AF_ORM", deserialize_json=True)
    persisted_stream = raw_var["streams"][stream_name]
    assert "_lineage_hydrated" not in persisted_stream
    assert "_sdtm_lineage_session" not in persisted_stream
    assert "_lineage_hydrated" not in persisted_stream["metadata"]

    # Verify mid-stream min_standards_score and batch failure leave AirflowStateProvider unchanged
    committed_ts = provider.get_last_timestamp("STUDY_AF_ORM", stream_name)
    committed_lineage = provider.get_sdtm_lineage("STUDY_AF_ORM", stream_name)

    active_recs = [
        {
            "studyKey": "STUDY_AF_ORM",
            "recordId": 300 + i,
            "subjectId": 1,
            "subjectKey": "SUBJ-01",
            "formId": 303,
            "formKey": "F_AE",
            "dateCreated": "2026-09-11 00:00:00",
            "dateModified": f"2026-09-11 10:{i:02d}:00",
            "recordData": ({} if i == 2 else {"AETERM": f"Next Event {i}"}),
        }
        for i in range(1, 4)
    ]

    fail_score_op = ImednetExportOperator(
        task_id="export_ae_sdtm_orm",
        study_key="STUDY_AF_ORM",
        destination="csv",
        output_path=str(csv_out),
        as_sdtm=True,
        incremental=True,
        batch_size=1,
        quality_gate_enabled=False,
        standards_profile="drug",
        min_standards_score=100.0,
        export_kwargs={"state_provider": provider},
    )
    with (
        patch.object(fail_score_op, "_get_sdk", side_effect=_make_sdk),
        pytest.raises(ExportError, match="Standards readiness check failed"),
    ):
        fail_score_op.execute(orm_ctx)

    assert provider.get_last_timestamp("STUDY_AF_ORM", stream_name) == committed_ts
    assert provider.get_sdtm_lineage("STUDY_AF_ORM", stream_name) == committed_lineage

    # Also fail mid-stream on batch 2 and verify retry assigns AESEQ 23, 24, 25
    from imednet.integrations.export import _TabularTransformSink

    orig_tab_write = _TabularTransformSink.write_batch
    csv_calls = 0

    def _flaky_tab_write(self_sink: Any, records: Any, *, batch_id: str) -> int:
        nonlocal csv_calls
        csv_calls += 1
        if csv_calls == 2:
            raise RuntimeError("disk full mid-stream")
        return orig_tab_write(self_sink, records, batch_id=batch_id)

    fail_batch_op = ImednetExportOperator(
        task_id="export_ae_sdtm_orm",
        study_key="STUDY_AF_ORM",
        destination="csv",
        output_path=str(tmp_path / "failed_mid.csv"),
        as_sdtm=True,
        incremental=True,
        batch_size=1,
        max_retries=0,
        quality_gate_enabled=False,
        stream_name=stream_name,
        export_kwargs={"state_provider": provider},
    )
    with (
        patch.object(fail_batch_op, "_get_sdk", side_effect=_make_sdk),
        patch.object(_TabularTransformSink, "write_batch", _flaky_tab_write),
        pytest.raises(RuntimeError, match="disk full mid-stream"),
    ):
        fail_batch_op.execute(orm_ctx)

    assert provider.get_last_timestamp("STUDY_AF_ORM", stream_name) == committed_ts
    assert provider.get_sdtm_lineage("STUDY_AF_ORM", stream_name) == committed_lineage

    retry_csv = tmp_path / "retry_ok.csv"
    retry_op = ImednetExportOperator(
        task_id="export_ae_sdtm_orm",
        study_key="STUDY_AF_ORM",
        destination="csv",
        output_path=str(retry_csv),
        as_sdtm=True,
        incremental=True,
        batch_size=1,
        max_retries=0,
        quality_gate_enabled=False,
        stream_name=stream_name,
        export_kwargs={"state_provider": provider},
    )
    with patch.object(retry_op, "_get_sdk", side_effect=_make_sdk):
        retry_op.execute(orm_ctx)

    retry_df = pd.read_csv(retry_csv)
    assert list(retry_df["AESEQ"]) == [23, 24, 25]


def test_airflow_sdk_runtime_20_batch_operator_and_50_form_sql_getvariable_counts(
    airflow_runtime_env: Path,
    tmp_path: Path,
) -> None:
    """In airflow.sdk mode: <= 3 GetVariable on 20-batch ImednetExportOperator and 2 GetVariable on 50-Form sql_by_form."""
    op_csv = tmp_path / "sdk_20_batches.csv"
    sql_50_db = tmp_path / "sdk_50_forms.sqlite"
    report_path = tmp_path / "sdk_comms_report.json"

    dag_file = airflow_runtime_env / "dag_sdtm_sdk_comms.py"
    dag_file.write_text(
        textwrap.dedent(
            f"""\
            from __future__ import annotations
            import json
            from datetime import datetime, timezone
            from pathlib import Path
            from unittest.mock import patch
            import httpx
            from airflow.sdk import DAG, task
            from airflow.sdk.execution_time import task_runner
            from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
            from imednet import ImednetSDK
            from imednet.integrations.export import export_to_sql_by_form
            from imednet_workflows.state_ledger import AirflowStateProvider

            with DAG("dag_sdtm_sdk_comms", start_date=datetime(2026, 1, 1, tzinfo=timezone.utc)) as dag:
                @task
                def verify_sdk_sdtm_variable_reads() -> None:
                    orig_comms = task_runner.SUPERVISOR_COMMS

                    class _VarSpy:
                        def __init__(self, inner):
                            self._inner = inner
                            self.messages: list[str] = []

                        def send(self, msg, *args, **kwargs):
                            name = type(msg).__name__
                            if "Variable" in name:
                                self.messages.append(name)
                            return self._inner.send(msg, *args, **kwargs)

                        def __getattr__(self, attr):
                            return getattr(self._inner, attr)

                    # Part 1: 20-batch ImednetExportOperator (as_sdtm=True, incremental=True, batch_size=1)
                    forms_20 = [{{"studyKey": "STUDY_20_SDK", "formId": 303, "formKey": "F_AE", "formName": "Adverse Events"}}]
                    vars_20 = [{{
                        "studyKey": "STUDY_20_SDK",
                        "variableId": 7,
                        "variableName": "AETERM",
                        "variableType": "text",
                        "label": "AE Term",
                        "formId": 303,
                        "formKey": "F_AE",
                        "formName": "Adverse Events",
                    }}]
                    recs_20 = [
                        {{
                            "studyKey": "STUDY_20_SDK",
                            "recordId": 1000 + i,
                            "subjectId": 1,
                            "subjectKey": "SUBJ-01",
                            "formId": 303,
                            "formKey": "F_AE",
                            "dateCreated": "2026-09-01 00:00:00",
                            "dateModified": f"2026-09-12 10:{{i:02d}}:00",
                            "recordData": {{"AETERM": f"SDK AE {{i}}"}},
                        }}
                        for i in range(1, 21)
                    ]

                    def _handler_20(request: httpx.Request) -> httpx.Response:
                        if request.url.path.endswith("/forms"):
                            return httpx.Response(200, json={{"data": forms_20, "pagination": {{"totalPages": 1}}}})
                        if request.url.path.endswith("/variables"):
                            return httpx.Response(200, json={{"data": vars_20, "pagination": {{"totalPages": 1}}}})
                        if request.url.path.endswith("/records"):
                            return httpx.Response(200, json={{"data": recs_20, "pagination": {{"totalPages": 1}}}})
                        return httpx.Response(200, json={{"data": [], "pagination": {{"totalPages": 1}}}})

                    sdk_20 = ImednetSDK(
                        api_key="k",
                        security_key="s",
                        base_url="https://edc.trn.imednetapi.com",
                        transport=httpx.MockTransport(_handler_20),
                    )
                    provider_20 = AirflowStateProvider(variable_prefix="imednet_sdk_20")
                    op = ImednetExportOperator(
                        task_id="op_20_batch_sdtm",
                        study_key="STUDY_20_SDK",
                        destination="csv",
                        output_path={str(op_csv)!r},
                        as_sdtm=True,
                        incremental=True,
                        batch_size=1,
                        quality_gate_enabled=False,
                        export_kwargs={{"state_provider": provider_20}},
                    )

                    spy_20 = _VarSpy(orig_comms)
                    task_runner.SUPERVISOR_COMMS = spy_20
                    try:
                        with (
                            patch.object(op, "_get_sdk", return_value=sdk_20),
                            patch.object(provider_20, "get_sdtm_lineage", wraps=provider_20.get_sdtm_lineage) as spy_gl_20,
                        ):
                            op.execute({{"dag_id": "dag_sdtm_sdk_comms"}})
                            gl_20_calls = spy_gl_20.call_count
                    finally:
                        task_runner.SUPERVISOR_COMMS = orig_comms

                    # Part 2: 50-Form export_to_sql_by_form (as_sdtm=True, incremental=True)
                    forms_50 = [
                        {{"studyKey": "STUDY_50_SDTM", "formId": i, "formKey": f"F{{i:02d}}", "formName": f"Adverse Events {{i}}"}}
                        for i in range(1, 51)
                    ]
                    vars_50 = [
                        {{
                            "studyKey": "STUDY_50_SDTM",
                            "variableId": i,
                            "variableName": "AETERM",
                            "variableType": "text",
                            "label": "AE Term",
                            "formId": i,
                            "formKey": f"F{{i:02d}}",
                            "formName": f"Adverse Events {{i}}",
                        }}
                        for i in range(1, 51)
                    ]
                    recs_50 = [
                        {{
                            "studyKey": "STUDY_50_SDTM",
                            "recordId": 2000 + i,
                            "subjectId": 1,
                            "subjectKey": "SUBJ-01",
                            "formId": i,
                            "formKey": f"F{{i:02d}}",
                            "dateCreated": "2026-09-01 00:00:00",
                            "dateModified": "2026-09-12 12:00:00",
                            "recordData": {{"AETERM": f"Form {{i}} Event"}},
                        }}
                        for i in range(1, 51)
                    ]

                    def _handler_50(request: httpx.Request) -> httpx.Response:
                        if request.url.path.endswith("/forms"):
                            return httpx.Response(200, json={{"data": forms_50, "pagination": {{"totalPages": 1}}}})
                        if request.url.path.endswith("/variables"):
                            return httpx.Response(200, json={{"data": vars_50, "pagination": {{"totalPages": 1}}}})
                        if request.url.path.endswith("/records"):
                            return httpx.Response(200, json={{"data": recs_50, "pagination": {{"totalPages": 1}}}})
                        return httpx.Response(200, json={{"data": [], "pagination": {{"totalPages": 1}}}})

                    sdk_50 = ImednetSDK(
                        api_key="k",
                        security_key="s",
                        base_url="https://edc.trn.imednetapi.com",
                        transport=httpx.MockTransport(_handler_50),
                    )
                    provider_50 = AirflowStateProvider(variable_prefix="imednet_sdk_50")
                    spy_50 = _VarSpy(orig_comms)
                    task_runner.SUPERVISOR_COMMS = spy_50
                    try:
                        with patch.object(provider_50, "get_sdtm_lineage", wraps=provider_50.get_sdtm_lineage) as spy_gl_50:
                            export_to_sql_by_form(
                                sdk_50,
                                "STUDY_50_SDTM",
                                f"sqlite:///{str(sql_50_db)}",
                                as_sdtm=True,
                                incremental=True,
                                quality_gate_enabled=False,
                                mask_phi=True,
                                state_provider=provider_50,
                            )
                            gl_50_calls = spy_gl_50.call_count
                    finally:
                        task_runner.SUPERVISOR_COMMS = orig_comms

                    Path({str(report_path)!r}).write_text(
                        json.dumps({{
                            "op_20_msgs": spy_20.messages,
                            "op_20_get_sdtm_lineage": gl_20_calls,
                            "sql_50_msgs": spy_50.messages,
                            "sql_50_get_sdtm_lineage": gl_50_calls,
                        }}),
                        encoding="utf-8",
                    )

                verify_sdk_sdtm_variable_reads()
            """
        ),
        encoding="utf-8",
    )

    dag_run = _load_dag_from_file(dag_file, "dag").test(
        logical_date=datetime(2026, 9, 12, tzinfo=UTC)
    )
    assert str(dag_run.state) == "success"

    report = json.loads(report_path.read_text(encoding="utf-8"))
    op_get_vars = [m for m in report["op_20_msgs"] if m == "GetVariable"]
    sql_get_vars = [m for m in report["sql_50_msgs"] if m == "GetVariable"]

    assert report["op_20_get_sdtm_lineage"] == 0
    assert len(op_get_vars) <= 3
    assert report["sql_50_get_sdtm_lineage"] == 0
    assert len(sql_get_vars) == 2

    df_20 = pd.read_csv(op_csv)
    assert len(df_20) == 20
    assert list(df_20["AESEQ"]) == list(range(1, 21))

    with sqlite3.connect(sql_50_db) as conn:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
    assert len(tables) == 50
