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
