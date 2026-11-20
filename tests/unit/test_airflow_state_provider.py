"""Unit and Apache Airflow 3.2+ integration tests for AirflowStateProvider."""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pyarrow.parquet as pq
import pytest

from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    StreamExecutionStatus,
    get_state_provider,
)


@pytest.fixture
def mock_airflow(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock Airflow modules cleanly using monkeypatch so real Airflow tests stay uncorrupted."""
    mock_airflow_mod = MagicMock()
    sdk_mock = MagicMock()
    sdk_mock.get_current_context.side_effect = RuntimeError("No SDK context")
    monkeypatch.setitem(sys.modules, "airflow", mock_airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mock)
    monkeypatch.setitem(sys.modules, "airflow.operators.python", MagicMock())
    monkeypatch.setitem(sys.modules, "airflow.models.xcom", MagicMock())
    monkeypatch.setitem(sys.modules, "airflow.utils.session", MagicMock())
    return mock_airflow_mod


def test_airflow_provider_fallback_transaction(mock_airflow: MagicMock) -> None:
    """Verify fallback to airflow.operators.python.get_current_context when SDK context is absent."""
    _ = mock_airflow
    provider = AirflowStateProvider()

    mock_get_current_context = sys.modules["airflow.operators.python"].get_current_context
    mock_ti = MagicMock()
    mock_ti.xcom_pull.return_value = None
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


@pytest.fixture
def isolated_airflow_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Initialize an isolated SQLite metadata DB on real Apache Airflow 3.2+."""
    if (
        "airflow" not in sys.modules
        or getattr(sys.modules.get("airflow"), "__path__", None) is None
    ):
        for mod in list(sys.modules):
            if mod == "airflow" or mod.startswith("airflow."):
                sys.modules.pop(mod, None)

    pytest.importorskip("airflow")
    from airflow import settings
    from airflow.configuration import conf
    from airflow.utils.db import initdb

    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir(parents=True, exist_ok=True)
    db_path = airflow_home / "airflow.db"
    db_uri = f"sqlite:///{db_path}"

    prev_settings_conn = settings.SQL_ALCHEMY_CONN
    prev_conf_conn = conf.get("database", "sql_alchemy_conn", fallback=None)
    monkeypatch.setenv("AIRFLOW_HOME", str(airflow_home))
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", db_uri)
    monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN", db_uri)
    conf.set("database", "sql_alchemy_conn", db_uri)

    try:
        settings.dispose_orm()
        settings.configure_orm()
        initdb()
        yield db_path
    finally:
        settings.dispose_orm()
        settings.SQL_ALCHEMY_CONN = prev_settings_conn
        if prev_conf_conn is not None:
            conf.set("database", "sql_alchemy_conn", prev_conf_conn)
        settings.configure_orm()
        root_logger.handlers = saved_handlers
        root_logger.setLevel(saved_level)


def test_real_airflow_xcom_model_crud_and_crash_recovery(
    isolated_airflow_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify AirflowStateProvider persists, recovers, and deletes state via real XComModel with run_id."""
    _ = isolated_airflow_db
    monkeypatch.setenv("AIRFLOW_CTX_DAG_ID", "clinical_export_dag")
    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "export_study_records")
    monkeypatch.setenv("AIRFLOW_CTX_DAG_RUN_ID", "scheduled__2026-01-10T00:00:00+00:00")

    provider = AirflowStateProvider()
    assert provider.get_last_timestamp("STUDY", "records") is None
    assert provider.get_last_status("STUDY", "records") is None

    # 1. Failed initial transaction leaves last_timestamp as None (not wall-clock datetime.now!)
    with pytest.raises(RuntimeError, match="initial API error"):
        with provider.transaction("STUDY", "records") as tx:
            assert tx["last_timestamp"] is None
            raise RuntimeError("initial API error")

    assert provider.get_last_timestamp("STUDY", "records") is None
    assert provider.get_last_status("STUDY", "records") == StreamExecutionStatus.FAILED
    failed_stream = provider.read_state().studies["STUDY"].streams["records"]
    assert failed_stream.last_timestamp is None
    assert failed_stream.last_run_status == StreamExecutionStatus.FAILED

    # 2. Simulate crashed task stuck in RUNNING status with expired lease and None watermark
    provider.set_last_timestamp(
        "STUDY",
        "records",
        None,
        records_processed=0,
        status=StreamExecutionStatus.RUNNING,
        lease_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert provider.get_last_status("STUDY", "records") == StreamExecutionStatus.RUNNING
    assert provider.get_last_timestamp("STUDY", "records") is None

    # 3. Retry recovers cleanly (RUNNING -> FAILED -> RUNNING -> SUCCESS)
    expected_ts = datetime(2026, 1, 10, 9, 35, tzinfo=UTC)
    with provider.transaction("STUDY", "records") as tx:
        assert tx["last_timestamp"] is None
        tx["new_timestamp"] = expected_ts
        tx["records_processed"] = 2

    assert provider.get_last_timestamp("STUDY", "records") == expected_ts
    assert provider.get_last_status("STUDY", "records") == StreamExecutionStatus.SUCCESS
    recovered_stream = provider.read_state().studies["STUDY"].streams["records"]
    assert recovered_stream.last_timestamp == expected_ts
    assert recovered_stream.records_processed == 2

    # 4. Delete entry clears XComModel rows
    assert provider.delete_entry("STUDY", "records") is True
    assert provider.get_last_timestamp("STUDY", "records") is None
    assert provider.delete_entry("STUDY", "records") is False


def test_real_airflow_export_operator_incremental_and_quality_gate(
    isolated_airflow_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ImednetExportOperator + AirflowStateProvider + SimulationTransport on Airflow 3.2+."""
    _ = isolated_airflow_db
    import airflow.sdk as airflow_sdk
    from airflow.models.xcom import XComModel
    from airflow.utils.session import create_session

    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet import ImednetSDK
    from imednet.simulation.state import SimulationState
    from imednet.simulation.transport import SimulationTransport

    dag_id = "imednet_incremental_dag"
    task_id = "export_parquet"
    monkeypatch.setenv("AIRFLOW_CTX_DAG_ID", dag_id)
    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", task_id)

    class _RealXComTaskInstance:
        """TaskInstance adapter wired directly to real Airflow 3.2+ XComModel."""

        def __init__(self, current_run_id: str) -> None:
            self.dag_id = dag_id
            self.task_id = task_id
            self.run_id = current_run_id
            self.map_index = -1

        def xcom_push(self, key: str, value: Any, **_kwargs: Any) -> None:
            with create_session() as session:
                AirflowStateProvider._ensure_manual_task_instance(
                    session,
                    dag_id=self.dag_id,
                    task_id=self.task_id,
                    run_id=self.run_id,
                    logical_date=datetime.now(UTC),
                )
                XComModel.set(
                    key=key,
                    value=value,
                    task_id=self.task_id,
                    dag_id=self.dag_id,
                    run_id=self.run_id,
                    map_index=self.map_index,
                    session=session,
                )

        def xcom_pull(
            self, key: str = "return_value", include_prior_dates: bool = False, **_kwargs: Any
        ) -> Any:
            with create_session() as session:
                AirflowStateProvider._ensure_manual_task_instance(
                    session,
                    dag_id=self.dag_id,
                    task_id=self.task_id,
                    run_id=self.run_id,
                    logical_date=datetime.now(UTC),
                )
                stmt = XComModel.get_many(
                    run_id=self.run_id,
                    key=key,
                    task_ids=self.task_id,
                    dag_ids=self.dag_id,
                    map_indexes=self.map_index,
                    include_prior_dates=include_prior_dates,
                    limit=1,
                )
                row = session.scalars(stmt).first()
                return XComModel.deserialize_value(row) if row is not None else None

    state = SimulationState()
    sim_transport = SimulationTransport(state)
    fail_records = {"enabled": True}

    class _ConditionalFailTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if fail_records["enabled"] and "/records" in request.url.path:
                raise httpx.ConnectError("Transient EDC outage", request=request)
            return sim_transport.handle_request(request)

    monkeypatch.setattr(
        ImednetExportOperator,
        "_get_sdk",
        lambda self: ImednetSDK(
            api_key="sim-key",  # pragma: allowlist secret
            security_key="sim-sec",  # pragma: allowlist secret
            base_url="https://sim.example",
            transport=_ConditionalFailTransport(),
            retries=0,
        ),
    )

    pq_out = tmp_path / "study_records.parquet"
    active_ti: dict[str, Any] = {}
    monkeypatch.setattr(airflow_sdk, "get_current_context", lambda: active_ti)

    def _make_op() -> ImednetExportOperator:
        return ImednetExportOperator(
            task_id=task_id,
            study_key="STUDY",
            destination="parquet",
            output_path=str(pq_out),
            incremental=True,
            idempotent=True,
            quality_gate_enabled=True,
            min_schema_readiness_score=100.0,
            max_retries=1,
        )

    provider = AirflowStateProvider()

    # Run 1: Initial failure leaves last_timestamp as None in real XComModel
    active_ti["ti"] = _RealXComTaskInstance("run__1")
    with pytest.raises(Exception):
        _make_op().execute(context=active_ti)  # type: ignore[arg-type]

    assert provider.get_last_timestamp("STUDY", "parquet") is None
    assert provider.get_last_status("STUDY", "parquet") == StreamExecutionStatus.FAILED

    # Run 2: Retry succeeds and exports all historical Study Records (601 @ 09:30Z, 602 @ 09:35Z)
    fail_records["enabled"] = False
    active_ti["ti"] = _RealXComTaskInstance("run__2")
    _make_op().execute(context=active_ti)  # type: ignore[arg-type]

    assert provider.get_last_timestamp("STUDY", "parquet") == datetime(
        2026, 1, 10, 9, 35, tzinfo=UTC
    )
    df_run2 = pq.read_table(pq_out).to_pandas().sort_values("recordId")
    assert list(df_run2["recordId"]) == [601, 602]

    # Run 3: Update 602 (10:00Z), add rejected draft 603 (10:30Z, missing V_GENDER),
    # and add valid 604 (11:00Z). Watermark MUST stop at 10:00Z (< 10:30Z)!
    for rec in state.records:
        if rec["recordId"] == 602:
            rec["recordData"] = {"V_WEIGHT": 74.0}
            rec["dateModified"] = "2026-01-10T10:00:00Z"
            rec["lastUpdated"] = "2026-01-10T10:00:00Z"
    state.records.extend(
        [
            {
                "recordId": 603,
                "studyKey": "STUDY",
                "subjectKey": "SUB1",
                "subjectId": 201,
                "formId": 301,
                "formKey": "F_DM",
                "visitId": 401,
                "intervalId": 501,
                "siteId": 101,
                "recordType": "Standard",
                "recordStatus": "Draft",
                "recordData": {"V_AGE": 33},  # Incomplete draft -> dropped by quality gate
                "deleted": False,
                "dateCreated": "2026-01-10T10:30:00Z",
                "dateModified": "2026-01-10T10:30:00Z",
                "lastUpdated": "2026-01-10T10:30:00Z",
            },
            {
                "recordId": 604,
                "studyKey": "STUDY",
                "subjectKey": "SUB1",
                "subjectId": 201,
                "formId": 301,
                "formKey": "F_DM",
                "visitId": 402,
                "intervalId": 502,
                "siteId": 101,
                "recordType": "Standard",
                "recordStatus": "Complete",
                "recordData": {"V_AGE": 49, "V_GENDER": "Male"},
                "deleted": False,
                "dateCreated": "2026-01-10T11:00:00Z",
                "dateModified": "2026-01-10T11:00:00Z",
                "lastUpdated": "2026-01-10T11:00:00Z",
            },
        ]
    )

    active_ti["ti"] = _RealXComTaskInstance("run__3")
    _make_op().execute(context=active_ti)  # type: ignore[arg-type]

    df_run3 = pq.read_table(pq_out).to_pandas().sort_values("recordId")
    assert list(df_run3["recordId"]) == [601, 602, 604]
    assert float(df_run3.loc[df_run3["recordId"] == 602, "V_WEIGHT"].iloc[0]) == 74.0
    assert provider.get_last_timestamp("STUDY", "parquet") == datetime(
        2026, 1, 10, 10, 0, tzinfo=UTC
    )

    # Run 4: Complete draft 603 -> next incremental run exports 603 and advances watermark to 11:00Z!
    for rec in state.records:
        if rec["recordId"] == 603:
            rec["recordData"] = {"V_AGE": 33, "V_GENDER": "Female"}

    active_ti["ti"] = _RealXComTaskInstance("run__4")
    _make_op().execute(context=active_ti)  # type: ignore[arg-type]

    df_run4 = pq.read_table(pq_out).to_pandas().sort_values("recordId")
    assert list(df_run4["recordId"]) == [601, 602, 603, 604]
    assert provider.get_last_timestamp("STUDY", "parquet") == datetime(
        2026, 1, 10, 11, 0, tzinfo=UTC
    )
    # Verify XCom payload is valid JSON serialized in XComModel
    raw_payload = active_ti["ti"].xcom_pull(key="state_STUDY_parquet", include_prior_dates=True)
    assert isinstance(raw_payload, dict)
    assert json.loads(json.dumps(raw_payload))["status"] == "success"
