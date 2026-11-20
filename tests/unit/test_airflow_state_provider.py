import contextlib
import json
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    StreamExecutionStatus,
    get_state_provider,
)


@pytest.fixture
def mock_airflow(monkeypatch):
    mock_airflow_mod = MagicMock()
    monkeypatch.setitem(sys.modules, "airflow", mock_airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", None)
    monkeypatch.setitem(sys.modules, "airflow.operators.python", MagicMock())
    monkeypatch.setitem(sys.modules, "airflow.models.xcom", MagicMock())
    monkeypatch.setitem(sys.modules, "airflow.utils.session", MagicMock())
    return mock_airflow_mod


def test_airflow_provider_fallback_transaction(mock_airflow):
    provider = AirflowStateProvider()

    mock_get_current_context = sys.modules["airflow.operators.python"].get_current_context
    mock_ti = MagicMock()
    mock_get_current_context.return_value = {"ti": mock_ti}

    with provider.transaction(
        "study1", "stream1", fallback_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
    ) as tx:
        tx["new_timestamp"] = datetime(2026, 1, 2, tzinfo=timezone.utc)
        tx["records_processed"] = 5

    assert mock_ti.xcom_push.called


def test_airflow_state_provider_sdk_context_scopes_task_id_and_skips_db_on_initial_run(
    monkeypatch,
):
    provider = AirflowStateProvider()

    sdk_mod = ModuleType("airflow.sdk")
    mock_ti = MagicMock()
    mock_ti.task_id = "export_study_task"
    mock_ti.map_index = -1
    mock_ti.xcom_pull.return_value = None
    sdk_mod.get_current_context = lambda: {"ti": mock_ti}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)

    session_mod = ModuleType("airflow.utils.session")
    create_session_spy = MagicMock()
    session_mod.create_session = create_session_spy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow.utils.session", session_mod)

    assert provider.get_last_timestamp("STUDY_A", "csv") is None
    assert provider.get_last_status("STUDY_A", "csv") is None
    create_session_spy.assert_not_called()

    mock_ti.xcom_pull.assert_called_with(
        task_ids="export_study_task",
        key="state_STUDY_A_csv",
        include_prior_dates=True,
        map_indexes=-1,
    )


def test_airflow_state_provider_sdk_context_type_error_fallback_and_list_selection(monkeypatch):
    provider = AirflowStateProvider()

    calls: list[dict[str, object]] = []

    class LegacyStubTI:
        task_id = "mapped_export"
        map_index = 0

        def xcom_pull(self, *, task_ids=None, key=None, include_prior_dates=False):
            calls.append(
                {
                    "task_ids": task_ids,
                    "key": key,
                    "include_prior_dates": include_prior_dates,
                }
            )
            return [
                {"last_timestamp": "2026-01-01T00:00:00", "status": "running"},
                {"last_timestamp": "2026-01-05T12:30:00+00:00", "status": "SUCCESS"},
            ]

    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.get_current_context = lambda: {"ti": LegacyStubTI()}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)

    ts = provider.get_last_timestamp("STUDY_B", "parquet")
    assert ts == datetime(2026, 1, 5, 12, 30, tzinfo=timezone.utc)
    assert provider.get_last_status("STUDY_B", "parquet") == StreamExecutionStatus.SUCCESS
    assert calls[0] == {
        "task_ids": "mapped_export",
        "key": "state_STUDY_B_parquet",
        "include_prior_dates": True,
    }


def test_airflow_state_provider_transaction_failure_records_failed_status(monkeypatch):
    provider = AirflowStateProvider()
    pushed: list[tuple[str, dict[str, object]]] = []

    class RecordingTI:
        task_id = "export_fail_task"
        map_index = -1

        def xcom_pull(self, **_kwargs):
            return pushed[-1][1] if pushed else None

        def xcom_push(self, *, key: str, value: dict[str, object]) -> None:
            pushed.append((key, value))

    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.get_current_context = lambda: {"ti": RecordingTI()}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)

    with pytest.raises(RuntimeError, match="sink boom"):
        with provider.transaction(
            "STUDY_ERR",
            "duckdb",
            fallback_timestamp=datetime(2026, 2, 1),
        ) as tx:
            tx["records_processed"] = 3
            raise RuntimeError("sink boom")

    assert len(pushed) == 2
    assert pushed[0][1]["status"] == "running"
    assert pushed[1][1]["status"] == "failed"
    assert pushed[1][1]["error_message"] == "sink boom"
    assert pushed[1][1]["records_processed"] == 3


def test_airflow_state_provider_helpers_and_missing_airflow_graceful_fallbacks(monkeypatch):
    provider = AirflowStateProvider()

    # Decode helper coverage across dict, deserialize_value, bytes, str, and invalid payloads
    assert provider._deserialize_xcom_row(object, SimpleNamespace(value={"a": 1})) == {"a": 1}

    class FakeXComModel:
        @staticmethod
        def deserialize_value(row):
            return json.loads(row.raw)

    assert provider._deserialize_xcom_row(
        FakeXComModel, SimpleNamespace(value=None, raw='{"last_timestamp": "2026-01-01T00:00:00Z"}')
    ) == {"last_timestamp": "2026-01-01T00:00:00Z"}
    assert provider._deserialize_xcom_row(
        object, SimpleNamespace(value=b'{"status": "success"}')
    ) == {"status": "success"}
    assert provider._deserialize_xcom_row(object, SimpleNamespace(value="not-json")) is None

    assert provider._parse_status(StreamExecutionStatus.PENDING) == StreamExecutionStatus.PENDING
    assert provider._parse_status("not_a_real_status") == StreamExecutionStatus.FAILED
    assert provider._parse_status(123) == StreamExecutionStatus.FAILED
    assert provider._parse_iso_utc(datetime(2026, 3, 1, 8, 0)) == datetime(
        2026, 3, 1, 8, 0, tzinfo=timezone.utc
    )
    assert provider._parse_iso_utc(42) is None

    # Simulate missing airflow imports for out-of-context calls
    monkeypatch.setitem(sys.modules, "airflow", None)
    monkeypatch.setitem(sys.modules, "airflow.sdk", None)
    monkeypatch.setitem(sys.modules, "airflow.operators.python", None)
    monkeypatch.setitem(sys.modules, "airflow.models.xcom", None)

    assert provider.get_last_timestamp("S1", "csv") is None
    assert provider.get_last_status("S1", "csv") is None
    provider.set_last_timestamp("S1", "csv", datetime(2026, 1, 1))
    assert provider.delete_entry("S1") is False
    assert provider.read_state().studies == {}

    monkeypatch.setenv("USE_AIRFLOW_STATE_PROVIDER", "1")
    assert isinstance(get_state_provider(), AirflowStateProvider)
    with contextlib.suppress(KeyError):
        monkeypatch.delenv("USE_AIRFLOW_STATE_PROVIDER")


def test_airflow_state_provider_xcom_list_tie_breaking_for_equal_or_none_timestamps(monkeypatch):
    """Latest entry wins when prior-run XCom list contains equal or None last_timestamp values."""
    provider = AirflowStateProvider()

    class TieBreakTI:
        task_id = "tie_break_export"
        map_index = -1

        def xcom_pull(self, **_kwargs):
            return [
                {"last_timestamp": None, "status": "running", "records_processed": 0},
                {"last_timestamp": None, "status": "failed", "error_message": "boom"},
                {"last_timestamp": None, "status": "success", "records_processed": 0},
            ]

    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.get_current_context = lambda: {"ti": TieBreakTI()}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)

    assert provider.get_last_timestamp("STUDY_TIE", "csv") is None
    assert provider.get_last_status("STUDY_TIE", "csv") == StreamExecutionStatus.SUCCESS


@pytest.fixture
def isolated_airflow_db(monkeypatch, tmp_path):
    """Initialize an isolated real Airflow 3 SQLite DB and restore state on teardown."""
    airflow_mod = sys.modules.get("airflow")
    if (
        airflow_mod is None
        or getattr(airflow_mod, "__path__", None) is None
        or getattr(airflow_mod, "__file__", None) is None
    ):
        for mod in list(sys.modules):
            if mod == "airflow" or mod.startswith(("airflow.", "apache_airflow_providers_imednet")):
                sys.modules.pop(mod, None)

    pytest.importorskip("airflow")
    import logging

    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    from airflow import settings
    from airflow.configuration import conf
    from airflow.utils.db import initdb

    af_home = tmp_path / "af_state_home"
    af_home.mkdir(parents=True, exist_ok=True)
    db_uri = f"sqlite:///{af_home / 'airflow.db'}"

    prev_conn = conf.get("database", "sql_alchemy_conn", fallback=None)
    prev_examples = conf.get("core", "load_examples", fallback=None)
    monkeypatch.setenv("AIRFLOW_HOME", str(af_home))
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", db_uri)
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN", db_uri)
    conf.set("database", "sql_alchemy_conn", db_uri)
    conf.set("core", "load_examples", "False")

    settings.dispose_orm()
    settings.configure_orm()
    initdb()

    try:
        yield db_uri
    finally:
        settings.dispose_orm()
        if prev_conn is not None:
            conf.set("database", "sql_alchemy_conn", prev_conn)
        if prev_examples is not None:
            conf.set("core", "load_examples", prev_examples)
        for mod in list(sys.modules):
            if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
                sys.modules.pop(mod, None)
        root_logger.handlers = saved_handlers
        root_logger.setLevel(saved_level)


def test_real_airflow_xcom_model_crud_and_crash_recovery(isolated_airflow_db, monkeypatch):
    """Verify real Airflow 3 XComModel persistence, read_state, delete_entry, and stale RUNNING recovery."""
    from datetime import timedelta

    from airflow.models.xcom import XComModel
    from airflow.utils.session import create_session

    dag_id = "dag_state_crud"
    task_id = "export_records_task"
    run_id = "manual__2026-06-01T00:00:00+00:00"
    logical_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

    monkeypatch.setenv("AIRFLOW_CTX_DAG_ID", dag_id)
    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", task_id)
    monkeypatch.setenv("AIRFLOW_CTX_DAG_RUN_ID", run_id)

    provider = AirflowStateProvider()
    assert provider.get_last_timestamp("STUDY_REAL", "csv") is None
    assert provider.get_last_status("STUDY_REAL", "csv") is None

    # Simulate stale crashed worker RUNNING state via XComModel.set
    with create_session() as session:
        provider._ensure_manual_task_instance(
            session,
            dag_id=dag_id,
            task_id=task_id,
            run_id=run_id,
            logical_date=logical_date,
        )
        XComModel.set(
            key="state_STUDY_REAL_csv",
            value={
                "last_timestamp": None,
                "records_processed": 0,
                "status": "running",
                "owner_pid": 1,
                "owner_host": "crashed-worker-pod",
                "lease_expires_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=10)
                ).isoformat(),
            },
            dag_id=dag_id,
            task_id=task_id,
            run_id=run_id,
            session=session,
        )

    assert provider.get_last_status("STUDY_REAL", "csv") == StreamExecutionStatus.RUNNING

    # Recover from expired RUNNING lease and commit watermark
    committed_ts = datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc)
    with provider.transaction("STUDY_REAL", "csv") as tx:
        assert tx["last_timestamp"] is None
        tx["new_timestamp"] = committed_ts
        tx["records_processed"] = 12

    assert provider.get_last_timestamp("STUDY_REAL", "csv") == committed_ts
    assert provider.get_last_status("STUDY_REAL", "csv") == StreamExecutionStatus.SUCCESS

    ledger = provider.read_state()
    assert "STUDY_REAL" in ledger.studies
    assert ledger.studies["STUDY_REAL"].streams["csv"].last_timestamp == committed_ts
    assert ledger.studies["STUDY_REAL"].streams["csv"].records_processed == 12

    assert provider.delete_entry("STUDY_REAL", "csv") is True
    assert provider.get_last_timestamp("STUDY_REAL", "csv") is None
