"""Unit tests for Airflow state provider."""

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    FileStateProvider,
    InvalidStateTransitionError,
    LedgerState,
    StreamExecutionStatus,
    get_state_provider,
)


@pytest.fixture
def mock_airflow():
    """Mock airflow modules."""
    airflow_mock = MagicMock()
    xcom_mock = MagicMock()
    session_mock = MagicMock()
    python_mock = MagicMock()

    sys.modules["airflow"] = airflow_mock
    sys.modules["airflow.models"] = MagicMock()
    sys.modules["airflow.models.xcom"] = xcom_mock
    sys.modules["airflow.utils"] = MagicMock()
    sys.modules["airflow.utils.session"] = session_mock
    sys.modules["airflow.operators"] = MagicMock()
    sys.modules["airflow.operators.python"] = python_mock

    yield {
        "xcom": xcom_mock,
        "session": session_mock,
        "python": python_mock,
    }

    del sys.modules["airflow"]
    del sys.modules["airflow.models"]
    del sys.modules["airflow.models.xcom"]
    del sys.modules["airflow.utils"]
    del sys.modules["airflow.utils.session"]
    del sys.modules["airflow.operators"]
    del sys.modules["airflow.operators.python"]


def test_get_state_provider(monkeypatch):
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


def test_airflow_get_last_timestamp_context(mock_airflow):
    provider = AirflowStateProvider()

    context_mock = MagicMock()
    ti_mock = MagicMock()
    context_mock.__getitem__.return_value = ti_mock
    mock_airflow["python"].get_current_context.return_value = context_mock

    ti_mock.xcom_pull.return_value = {"last_timestamp": "2026-05-22T12:00:00+00:00"}

    ts = provider.get_last_timestamp("ST1", "recs")
    assert ts is not None
    assert ts.year == 2026
    ti_mock.xcom_pull.assert_called_with(key="state_ST1_recs", include_prior_dates=True)


def test_airflow_get_last_timestamp_db(mock_airflow):
    provider = AirflowStateProvider()

    mock_airflow["python"].get_current_context.side_effect = Exception("No context")

    def provide_session_decorator(func):
        def wrapper(*args, **kwargs):
            return func(session=MagicMock(), *args, **kwargs)

        return wrapper

    mock_airflow["session"].provide_session = provide_session_decorator

    # Needs to mock session query
    mock_airflow["xcom"].XCom
    # Well, it's easier to mock the provide_session decorator such that it runs the func and the func works. But wait, it's defined inside the method!
    # If provide_session is mocked, it will be used as a decorator.

    xcom_obj = MagicMock()
    xcom_obj.value = {"last_timestamp": "2026-05-22T12:00:00+00:00"}

    class MockQuery:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def first(self):
            return xcom_obj

    class MockSession:
        def query(self, *args, **kwargs):
            return MockQuery()

    def provide_session_mock(func):
        def wrapper(*args, **kwargs):
            return func(session=MockSession())

        return wrapper

    mock_airflow["session"].provide_session = provide_session_mock

    ts = provider.get_last_timestamp("ST1", "recs")
    assert ts is not None
    assert ts.year == 2026


def test_airflow_set_last_timestamp_context(mock_airflow):
    provider = AirflowStateProvider()
    context_mock = MagicMock()
    ti_mock = MagicMock()
    context_mock.__getitem__.return_value = ti_mock
    mock_airflow["python"].get_current_context.return_value = context_mock

    ts = datetime(2026, 5, 22, 12, 0, 0)
    provider.set_last_timestamp("ST1", "recs", ts)

    ti_mock.xcom_push.assert_called()


def test_airflow_set_last_timestamp_db(mock_airflow, monkeypatch):
    provider = AirflowStateProvider()
    mock_airflow["python"].get_current_context.side_effect = Exception("No context")

    xcom_model = mock_airflow["xcom"].XCom

    monkeypatch.setenv("AIRFLOW_CTX_DAG_ID", "dag1")
    monkeypatch.setenv("AIRFLOW_CTX_EXECUTION_DATE", "2026-05-22T12:00:00+00:00")

    ts = datetime(2026, 5, 22, 12, 0, 0)
    provider.set_last_timestamp("ST1", "recs", ts)

    xcom_model.set.assert_called()


def test_airflow_transaction(mock_airflow):
    provider = AirflowStateProvider()

    context_mock = MagicMock()
    ti_mock = MagicMock()
    context_mock.__getitem__.return_value = ti_mock
    mock_airflow["python"].get_current_context.return_value = context_mock

    ti_mock.xcom_pull.return_value = {"last_timestamp": "2026-05-22T12:00:00+00:00"}

    with provider.transaction("ST1", "recs") as tx:
        assert tx["last_timestamp"] is not None
        tx["new_timestamp"] = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)

    ti_mock.xcom_push.assert_called()


def test_airflow_transaction_error(mock_airflow):
    provider = AirflowStateProvider()

    context_mock = MagicMock()
    ti_mock = MagicMock()
    context_mock.__getitem__.return_value = ti_mock
    mock_airflow["python"].get_current_context.return_value = context_mock

    ti_mock.xcom_pull.return_value = {"last_timestamp": "2026-05-22T12:00:00+00:00"}

    with pytest.raises(ValueError), provider.transaction("ST1", "recs"):
        raise ValueError("boom")

    # Push should have been called with failed
    args = ti_mock.xcom_push.call_args[1]
    assert args["value"]["status"] == "failed"


def test_airflow_delete_entry(mock_airflow):
    provider = AirflowStateProvider()

    class MockQuery:
        def filter(self, *args, **kwargs):
            return self

        def delete(self, *args, **kwargs):
            return 1

    class MockSession:
        def query(self, *args, **kwargs):
            return MockQuery()

    def provide_session_mock(func):
        def wrapper(*args, **kwargs):
            return func(session=MockSession())

        return wrapper

    mock_airflow["session"].provide_session = provide_session_mock

    res = provider.delete_entry("ST1")
    assert res is True
    res2 = provider.delete_entry("ST1", "recs")
    assert res2 is True


def test_airflow_read_state(mock_airflow):
    provider = AirflowStateProvider()

    xcom_obj = MagicMock()
    xcom_obj.key = "state_ST1_recs"
    xcom_obj.value = {"last_timestamp": "2026-05-22T12:00:00+00:00", "records_processed": 5}

    xcom_obj2 = MagicMock()
    xcom_obj2.key = "state_ST1_recs"  # Duplicate key
    xcom_obj2.value = {"last_timestamp": "2026-05-21T12:00:00+00:00"}

    class MockQuery:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return [xcom_obj, xcom_obj2]

    class MockSession:
        def query(self, *args, **kwargs):
            return MockQuery()

    def provide_session_mock(func):
        def wrapper(*args, **kwargs):
            return func(session=MockSession())

        return wrapper

    mock_airflow["session"].provide_session = provide_session_mock

    state = provider.read_state()
    assert "ST1" in state.studies
    assert "recs" in state.studies["ST1"].streams
    assert state.studies["ST1"].streams["recs"].records_processed == 5


def test_airflow_read_state_no_imports(monkeypatch):
    provider = AirflowStateProvider()
    import sys

    monkeypatch.setitem(sys.modules, "airflow.models.xcom", None)

    state = provider.read_state()
    assert isinstance(state, LedgerState)

    # Test delete no imports
    assert provider.delete_entry("ST1") is False
    assert provider.get_last_timestamp("ST1", "rec") is None

    # Set without imports
    monkeypatch.setitem(sys.modules, "airflow.operators.python", None)
    provider.set_last_timestamp("ST1", "rec", datetime.now(timezone.utc))  # Shouldn't error


def test_airflow_transaction_guard_running(mock_airflow):
    """Airflow provider rejects transaction if stream is already RUNNING."""
    provider = AirflowStateProvider()

    context_mock = MagicMock()
    ti_mock = MagicMock()
    context_mock.__getitem__.return_value = ti_mock
    mock_airflow["python"].get_current_context.return_value = context_mock

    # Current stream is already in running status
    ti_mock.xcom_pull.return_value = {
        "last_timestamp": "2026-05-22T12:00:00+00:00",
        "status": "running",
    }

    with pytest.raises(
        InvalidStateTransitionError,
        match="Cannot transition stream state from 'running' to 'running'",
    ):
        with provider.transaction("ST1", "recs"):
            pass


def test_airflow_transaction_error_preserves_high_water_mark(mock_airflow):
    """Airflow provider transaction failure preserves previous high-water mark."""
    provider = AirflowStateProvider()

    context_mock = MagicMock()
    ti_mock = MagicMock()
    context_mock.__getitem__.return_value = ti_mock
    mock_airflow["python"].get_current_context.return_value = context_mock

    previous_iso = "2026-05-22T10:00:00+00:00"
    partial_ts = datetime(2026, 5, 22, 11, 0, 0, tzinfo=timezone.utc)
    ti_mock.xcom_pull.return_value = {
        "last_timestamp": previous_iso,
        "status": "success",
    }

    with pytest.raises(RuntimeError, match="Airflow worker crash"):
        with provider.transaction("ST1", "recs") as tx:
            tx["new_timestamp"] = partial_ts
            tx["records_processed"] = 99
            raise RuntimeError("Airflow worker crash")

    # Verify that the failure push preserves previous_iso, not partial_ts
    last_call_args = ti_mock.xcom_push.call_args[1]["value"]
    assert last_call_args["status"] == StreamExecutionStatus.FAILED.value
    assert last_call_args["last_timestamp"] == previous_iso
    assert last_call_args["records_processed"] == 99
    assert last_call_args["error_message"] == "Airflow worker crash"


def test_airflow_set_last_timestamp_invalid_status(mock_airflow):
    """Airflow provider validates status argument."""
    provider = AirflowStateProvider()
    with pytest.raises(ValueError, match="Invalid stream execution status"):
        provider.set_last_timestamp(
            "ST1", "recs", datetime.now(timezone.utc), status="unsupported_status"
        )
