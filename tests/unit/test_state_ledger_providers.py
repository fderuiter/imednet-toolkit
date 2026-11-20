"""Unit tests for state provider selection and Airflow 3 Variable state parity."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    FileStateProvider,
    InvalidStateTransitionError,
    LedgerState,
    StreamExecutionStatus,
    get_state_provider,
)


@pytest.fixture(scope="module")
def airflow_ledger_db(tmp_path_factory: pytest.TempPathFactory):
    """Configure an isolated SQLite Airflow 3 database when Airflow is installed."""
    pytest.importorskip("airflow")
    import airflow.settings
    from airflow.configuration import conf
    from airflow.utils.db import initdb

    db_dir = tmp_path_factory.mktemp("state_ledger_providers_af")
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


@pytest.fixture
def clean_airflow_vars(airflow_ledger_db: Path):
    """Clear Airflow Variable entries prior to each AirflowStateProvider test."""
    _ = airflow_ledger_db
    from airflow.models import Variable
    from airflow.utils.session import create_session
    from sqlalchemy import delete

    with create_session() as session:
        session.execute(delete(Variable))
    yield


def test_get_state_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_state_provider selects FileStateProvider by default and AirflowStateProvider in Airflow."""
    monkeypatch.delenv("AIRFLOW_CTX_TASK_ID", raising=False)
    monkeypatch.delenv("USE_AIRFLOW_STATE_PROVIDER", raising=False)

    provider = get_state_provider()
    assert isinstance(provider, FileStateProvider)

    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "test")
    provider2 = get_state_provider()
    assert isinstance(provider2, AirflowStateProvider)

    monkeypatch.delenv("AIRFLOW_CTX_TASK_ID", raising=False)
    monkeypatch.setenv("USE_AIRFLOW_STATE_PROVIDER", "1")
    provider3 = get_state_provider()
    assert isinstance(provider3, AirflowStateProvider)


def test_airflow_variable_roundtrip_and_status(clean_airflow_vars: None) -> None:
    """AirflowStateProvider stores and retrieves timestamps and statuses via Airflow Variable."""
    _ = clean_airflow_vars
    provider = AirflowStateProvider()

    ts = datetime(2026, 5, 22, 12, 0, 0)
    provider.set_last_timestamp("ST1", "recs", ts, records_processed=5)

    loaded_ts = provider.get_last_timestamp("ST1", "recs")
    assert loaded_ts == datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert provider.get_last_status("ST1", "recs") == StreamExecutionStatus.SUCCESS

    state = provider.read_state()
    assert "ST1" in state.studies
    assert state.studies["ST1"].streams["recs"].records_processed == 5


def test_airflow_transaction_and_delete(clean_airflow_vars: None) -> None:
    """AirflowStateProvider commits transactions and deletes stream/study Variable entries."""
    _ = clean_airflow_vars
    provider = AirflowStateProvider()
    provider.set_last_timestamp("ST1", "recs", datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))

    with provider.transaction("ST1", "recs") as tx:
        assert tx["last_timestamp"] == datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        tx["new_timestamp"] = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
        tx["records_processed"] = 11

    assert provider.get_last_timestamp("ST1", "recs") == datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
    assert provider.delete_entry("ST1", "recs") is True
    assert provider.get_last_timestamp("ST1", "recs") is None

    provider.set_last_timestamp("ST1", "s_a", datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC))
    assert provider.delete_entry("ST1") is True
    assert provider.read_state().studies == {}


def test_airflow_read_state_no_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """AirflowStateProvider returns empty state without errors when Airflow is unavailable."""
    provider = AirflowStateProvider()
    monkeypatch.setitem(sys.modules, "airflow", None)
    monkeypatch.setitem(sys.modules, "airflow.models", None)
    monkeypatch.setitem(sys.modules, "airflow.sdk", None)

    state = provider.read_state()
    assert isinstance(state, LedgerState)
    assert provider.delete_entry("ST1") is False
    assert provider.get_last_timestamp("ST1", "rec") is None
    provider.set_last_timestamp("ST1", "rec", datetime.now(UTC))


def test_airflow_transaction_guard_running(clean_airflow_vars: None) -> None:
    """AirflowStateProvider rejects transaction if stream is already RUNNING."""
    _ = clean_airflow_vars
    provider = AirflowStateProvider()
    provider.set_last_timestamp(
        "ST1",
        "recs",
        datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        status=StreamExecutionStatus.RUNNING,
    )

    with pytest.raises(
        InvalidStateTransitionError,
        match="Cannot transition stream state from 'running' to 'running'",
    ):
        with provider.transaction("ST1", "recs"):
            pass


def test_airflow_transaction_error_preserves_high_water_mark(clean_airflow_vars: None) -> None:
    """AirflowStateProvider transaction failure marks FAILED and preserves prior watermark."""
    _ = clean_airflow_vars
    provider = AirflowStateProvider()
    previous_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    partial_ts = datetime(2026, 5, 22, 11, 0, 0, tzinfo=UTC)
    provider.set_last_timestamp("ST1", "recs", previous_ts, status=StreamExecutionStatus.SUCCESS)

    with pytest.raises(RuntimeError, match="Airflow worker crash"):
        with provider.transaction("ST1", "recs") as tx:
            tx["new_timestamp"] = partial_ts
            tx["records_processed"] = 99
            raise RuntimeError("Airflow worker crash")

    assert provider.get_last_status("ST1", "recs") == StreamExecutionStatus.FAILED
    assert provider.get_last_timestamp("ST1", "recs") == previous_ts
    entry = provider.read_state().studies["ST1"].streams["recs"]
    assert entry.records_processed == 99
    assert entry.error_message == "Airflow worker crash"


def test_airflow_set_last_timestamp_invalid_status() -> None:
    """AirflowStateProvider validates status argument."""
    provider = AirflowStateProvider()
    with pytest.raises(ValueError, match="Invalid stream execution status"):
        provider.set_last_timestamp("ST1", "recs", datetime.now(UTC), status="unsupported_status")
