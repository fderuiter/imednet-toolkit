"""Real Airflow 3 integration tests for DAG loading, checkpoints, Jinja rendering, and SQL sinks."""

from __future__ import annotations

import runpy
import sqlite3
import warnings
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

pytest.importorskip("airflow")

from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable as EdcVariable
from imednet_workflows.state_ledger import AirflowStateProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_AIRFLOW_DIR = REPO_ROOT / "examples" / "airflow"


@pytest.fixture(autouse=True, scope="module")
def _ensure_real_airflow_provider_modules():
    """Reload provider modules against real Airflow if earlier unit tests mocked sys.modules."""
    import sys

    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
            sys.modules.pop(mod, None)
    yield


@pytest.fixture(scope="module")
def airflow_integration_db(tmp_path_factory: pytest.TempPathFactory):
    """Initialize an isolated SQLite Airflow 3 database for DAG and Variable integration tests."""
    import airflow.settings
    from airflow.configuration import conf
    from airflow.utils.db import initdb

    db_dir = tmp_path_factory.mktemp("airflow_dag_integration")
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
def _clean_airflow_variables(airflow_integration_db: Path):
    """Ensure clean Airflow Variable state before every integration test."""
    _ = airflow_integration_db
    from airflow.models import Variable
    from airflow.utils.session import create_session
    from sqlalchemy import delete

    with create_session() as session:
        session.execute(delete(Variable))
    yield


def _build_record(
    record_id: int,
    *,
    form_id: int = 10,
    form_key: str = "demographics",
    subject_key: str = "SUBJ-01",
    date_modified: str = "2026-04-01T10:00:00Z",
    record_data: dict[str, Any] | None = None,
) -> Record:
    return Record(
        record_id=record_id,
        form_id=form_id,
        form_key=form_key,
        subject_key=subject_key,
        deleted=False,
        date_modified=date_modified,
        record_data=record_data or {"age": 45, "cohort": "A"},
    )


def _build_mock_sdk(records: list[Record]) -> tuple[MagicMock, list[dict[str, Any]]]:
    seen_calls: list[dict[str, Any]] = []
    sdk = MagicMock()
    form_list = [
        Form(form_id=10, form_key="demographics", form_name="Demographics"),
        Form(form_id=20, form_key="vitals", form_name="Vitals"),
    ]
    sdk.forms.list.return_value = form_list
    sdk.get_forms.return_value = form_list
    var_list = [
        EdcVariable(
            variable_id=1,
            form_id=10,
            form_key="demographics",
            variable_name="age",
            label="Age",
            variable_type="integer",
        ),
        EdcVariable(
            variable_id=2,
            form_id=10,
            form_key="demographics",
            variable_name="cohort",
            label="Cohort",
            variable_type="text",
        ),
        EdcVariable(
            variable_id=3,
            form_id=20,
            form_key="vitals",
            variable_name="hr",
            label="Heart Rate",
            variable_type="integer",
        ),
    ]
    sdk.variables.list.return_value = var_list
    sdk.get_variables.return_value = var_list

    def _list_records(**kwargs: Any) -> list[Record]:
        seen_calls.append(dict(kwargs))
        result = list(records)
        form_id_filter = kwargs.get("formId")
        if form_id_filter is not None:
            allowed = {form_id_filter} if isinstance(form_id_filter, int) else set(form_id_filter)
            result = [r for r in result if r.form_id in allowed]
        subject_filter = kwargs.get("subjectKey")
        if isinstance(subject_filter, str):
            result = [r for r in result if r.subject_key == subject_filter]
        return result

    sdk.records.list.side_effect = _list_records
    sdk.jobs.get.return_value = SimpleNamespace(
        state="COMPLETED", is_terminal=True, is_failed=False, is_successful=True
    )
    return sdk, seen_calls


def test_dag_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ImednetExportOperator and ImednetJobSensor execute cleanly inside an Airflow 3 DAG."""
    from airflow.sdk import DAG

    from apache_airflow_providers_imednet import ImednetExportOperator, ImednetJobSensor

    out_csv = tmp_path / "out.csv"
    sdk, _ = _build_mock_sdk([_build_record(1), _build_record(2)])

    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk)
    monkeypatch.setattr(ImednetJobSensor, "_get_sdk", lambda self: sdk)

    with DAG("d", start_date=datetime(2024, 1, 1), schedule=None):
        export_task = ImednetExportOperator(
            task_id="export",
            study_key="S",
            destination="csv",
            output_path=str(out_csv),
        )
        wait_task = ImednetJobSensor(task_id="wait", study_key="S", batch_id="ID")
        export_task >> wait_task

    export_task.execute(context={})
    assert wait_task.poke(context={}) is True
    exported_df = pd.read_csv(out_csv)
    assert len(exported_df) == 2
    assert "age" in exported_df.columns


def test_examples_airflow_dags_load_without_errors_or_deprecation_warnings() -> None:
    """Every DAG in examples/airflow/ imports and parses on Airflow 3 without warnings."""
    from airflow.configuration import conf
    from airflow.dag_processing.dagbag import DagBag

    dag_files = sorted(EXAMPLES_AIRFLOW_DIR.glob("*.py"))
    assert len(dag_files) == 3

    for dag_path in dag_files:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            namespace = runpy.run_path(str(dag_path))
        dep_warnings = [
            f"{w.category.__name__}: {w.message}"
            for w in caught
            if issubclass(w.category, DeprecationWarning)
        ]
        assert dep_warnings == [], f"Unexpected deprecation warnings in {dag_path}: {dep_warnings}"
        assert "dag" in namespace

    prev_load_examples = conf.get("core", "load_examples", fallback=None)
    conf.set("core", "load_examples", "False")
    try:
        with warnings.catch_warnings(record=True) as bag_warnings:
            warnings.simplefilter("always", DeprecationWarning)
            bag = DagBag(dag_folder=str(EXAMPLES_AIRFLOW_DIR))
    finally:
        if prev_load_examples is not None:
            conf.set("core", "load_examples", prev_load_examples)

    assert bag.import_errors == {}
    expected_dag_ids = {
        "imednet_multi_study_reference",
        "imednet_export_example",
        "imednet_job_sensor_example",
    }
    assert expected_dag_ids.issubset(bag.dags.keys())
    for dag_id in expected_dag_ids:
        assert bag.dags[dag_id].schedule is None
    imednet_warnings = [
        w
        for w in bag_warnings
        if issubclass(w.category, DeprecationWarning) and "imednet" in str(w.filename).lower()
    ]
    assert imednet_warnings == []


def test_export_operator_incremental_variable_checkpoints_across_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Running ImednetExportOperator with incremental=True commits and reads Variable checkpoints."""
    from apache_airflow_providers_imednet import ImednetExportOperator

    out_csv = tmp_path / "incremental.csv"
    run1_records = [
        _build_record(1, date_modified="2026-04-01T10:00:00Z"),
        _build_record(2, date_modified="2026-04-02T15:30:00Z"),
    ]
    sdk, seen_calls = _build_mock_sdk(run1_records)
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk)

    op_run1 = ImednetExportOperator(
        task_id="inc_export_1",
        study_key="STUDY_INC",
        destination="csv",
        output_path=str(out_csv),
        incremental=True,
        stream_name="daily_csv",
    )
    op_run1.execute(context={"run_id": "manual__1"})

    provider = AirflowStateProvider()
    assert provider.get_last_timestamp("STUDY_INC", "daily_csv") == datetime(
        2026, 4, 2, 15, 30, 0, tzinfo=UTC
    )

    run2_records = [_build_record(3, date_modified="2026-04-03T09:00:00Z")]
    sdk2, seen_calls_2 = _build_mock_sdk(run2_records)
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk2)

    op_run2 = ImednetExportOperator(
        task_id="inc_export_2",
        study_key="STUDY_INC",
        destination="csv",
        output_path=str(out_csv),
        incremental=True,
        stream_name="daily_csv",
    )
    op_run2.execute(context={"run_id": "manual__2"})

    assert seen_calls_2[0]["dateModified"] == (">=", "2026-04-02T15:30:00Z")
    assert provider.get_last_timestamp("STUDY_INC", "daily_csv") == datetime(
        2026, 4, 3, 9, 0, 0, tzinfo=UTC
    )


def test_export_operator_renders_jinja_watermark_and_filter_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Airflow 3 Jinja rendering populates modified_since, record_filter, record_data_filter, stream_name."""
    from airflow.sdk import DAG

    from apache_airflow_providers_imednet import ImednetExportOperator

    out_csv = tmp_path / "templated.csv"
    records = [
        _build_record(1, subject_key="SUBJ-01", record_data={"age": 52, "cohort": "A"}),
        _build_record(2, subject_key="SUBJ-02", record_data={"age": 29, "cohort": "B"}),
    ]
    sdk, seen_calls = _build_mock_sdk(records)
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk)

    with DAG("templated_export_dag", start_date=datetime(2026, 4, 1), schedule=None):
        op = ImednetExportOperator(
            task_id="templated_export",
            study_key="STUDY_{{ params.env }}",
            destination="csv",
            output_path=str(out_csv),
            incremental=True,
            modified_since="{{ ds }}T00:00:00Z",
            record_filter={"subjectKey": "{{ params.subject }}"},
            record_data_filter="age>={{ params.min_age }}",
            stream_name="csv_{{ ds_nodash }}",
        )

    render_ctx: Any = {
        "ds": "2026-04-05",
        "ds_nodash": "20260405",
        "params": {"env": "PROD", "subject": "SUBJ-01", "min_age": 40},
    }
    op.render_template_fields(render_ctx)

    assert op.study_key == "STUDY_PROD"
    assert op.modified_since == "2026-04-05T00:00:00Z"
    assert op.record_filter == {"subjectKey": "SUBJ-01"}
    assert op.record_data_filter == "age>=40"
    assert op.stream_name == "csv_20260405"

    op.execute(context=render_ctx)

    assert seen_calls[0]["dateModified"] == (">=", "2026-04-05T00:00:00Z")
    assert seen_calls[0]["subjectKey"] == "SUBJ-01"
    assert seen_calls[0]["record_data_filter"] == "age>=40"
    exported = pd.read_csv(out_csv)
    assert len(exported) == 1
    assert int(exported.iloc[0]["age"]) == 52

    provider = AirflowStateProvider()
    assert provider.get_last_timestamp("STUDY_PROD", "csv_20260405") is not None


def test_export_operator_executes_sql_by_form_and_long_sql_destinations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Executing ImednetExportOperator with sql_by_form and long_sql writes expected SQL tables."""
    from airflow.models.connection import Connection

    from apache_airflow_providers_imednet import ImednetExportOperator
    from apache_airflow_providers_imednet.hooks import ImednetHook

    sqlite_db = tmp_path / "clinical_exports.sqlite"
    dest_conn = Connection(
        conn_id="sqlite_export_dest",
        conn_type="sqlite",
        extra=f'{{"conn_str": "sqlite:///{sqlite_db}"}}',
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: dest_conn if conn_id == "sqlite_export_dest" else None),
    )

    records = [
        _build_record(
            101,
            form_id=10,
            form_key="demographics",
            record_data={"age": 48, "cohort": "ARM1"},
        ),
        _build_record(
            201,
            form_id=20,
            form_key="vitals",
            record_data={"hr": 72},
        ),
    ]
    sdk, _ = _build_mock_sdk(records)
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk)

    op_by_form = ImednetExportOperator(
        task_id="export_sql_by_form",
        study_key="STUDY_SQL_INT",
        destination="sql_by_form",
        destination_conn_id="sqlite_export_dest",
    )
    op_by_form.execute(context={})

    op_long_sql = ImednetExportOperator(
        task_id="export_long_sql",
        study_key="STUDY_SQL_INT",
        destination="long_sql",
        output_path="long_clinical_eav",
        destination_conn_id="sqlite_export_dest",
    )
    op_long_sql.execute(context={})

    with sqlite3.connect(sqlite_db) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"demographics", "vitals", "long_clinical_eav"}.issubset(tables)

        demo_rows = conn.execute("SELECT age, cohort FROM demographics").fetchall()
        assert demo_rows == [(48, "ARM1")]

        vitals_rows = conn.execute("SELECT hr FROM vitals").fetchall()
        assert vitals_rows == [(72,)]

        long_rows = conn.execute(
            "SELECT record_id, variable_name, value FROM long_clinical_eav ORDER BY record_id, variable_name"
        ).fetchall()
        assert (101, "age", "48") in [(r[0], r[1], str(r[2])) for r in long_rows]
        assert (201, "hr", "72") in [(r[0], r[1], str(r[2])) for r in long_rows]
