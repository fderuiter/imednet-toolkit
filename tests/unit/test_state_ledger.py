"""Unit tests for state ledger."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import imednet_workflows.state_ledger as _state_ledger_module
from imednet_workflows.cli import setup_parser, state_app
from imednet_workflows.state_ledger import (
    DEFAULT_LEDGER_PATH,
    ExtractionStateLedger,
    FileStateProvider,
    InvalidStateTransitionError,
    LedgerState,
    StreamExecutionStatus,
    get_state_provider,
    is_valid_transition,
    validate_transition,
)


def test_state_ledger_read_write(tmp_path) -> None:
    """Test that state ledger read write."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    # Initial state should be empty
    state = ledger.read_state()
    assert isinstance(state, LedgerState)
    assert len(state.studies) == 0

    # Write state
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp(
        study_key="STUDY-01",
        stream_name="records",
        timestamp=ts,
        records_processed=42,
        status="success",
        metadata={"foo": "bar"},
    )

    # Read state back
    state2 = ledger.read_state()
    assert "STUDY-01" in state2.studies
    stream = state2.studies["STUDY-01"].streams["records"]
    assert stream.last_timestamp == ts
    assert stream.records_processed == 42
    assert stream.last_run_status == "success"
    assert stream.metadata == {"foo": "bar"}

    # Get last timestamp convenience method
    retrieved_ts = ledger.get_last_timestamp("STUDY-01", "records")
    assert retrieved_ts == ts

    # Missing study/stream should return None
    assert ledger.get_last_timestamp("STUDY-02", "records") is None
    assert ledger.get_last_timestamp("STUDY-01", "queries") is None


def test_state_ledger_transaction_success(tmp_path) -> None:
    """Test that state ledger transaction success."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    fallback_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)

    # Successful transaction
    new_ts = datetime(2026, 5, 22, 11, 0, 0, tzinfo=timezone.utc)
    with ledger.transaction("STUDY-01", "records", fallback_timestamp=fallback_ts) as tx:
        assert tx["last_timestamp"] == fallback_ts
        tx["new_timestamp"] = new_ts
        tx["records_processed"] = 100
        tx["metadata"] = {"batch": 1}

    # Verify state was committed
    retrieved_ts = ledger.get_last_timestamp("STUDY-01", "records")
    assert retrieved_ts == new_ts

    state = ledger.read_state()
    stream = state.studies["STUDY-01"].streams["records"]
    assert stream.records_processed == 100
    assert stream.last_run_status == "success"
    assert stream.metadata == {"batch": 1}


def test_state_ledger_transaction_failure(tmp_path) -> None:
    """Test that state ledger transaction failure."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    fallback_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)

    # Failed transaction due to an raised exception
    with pytest.raises(ValueError, match="API Error"):
        with ledger.transaction("STUDY-01", "records", fallback_timestamp=fallback_ts) as tx:
            assert tx["last_timestamp"] == fallback_ts
            tx["records_processed"] = 10
            raise ValueError("API Error")

    # Verify state is updated to failed and error message registered
    state = ledger.read_state()
    stream = state.studies["STUDY-01"].streams["records"]
    assert stream.last_run_status == "failed"
    assert stream.error_message == "API Error"
    assert stream.records_processed == 10


def test_state_ledger_atomic_write_failure(tmp_path, monkeypatch) -> None:
    """Test that state ledger atomic write failure."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    # Pre-populate state
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp("STUDY-01", "records", ts)

    # Mock os.replace to raise an error
    def mock_replace(src, dst):
        """Helper function to mock replace."""
        raise OSError("Disk full")

    monkeypatch.setattr(os, "replace", mock_replace)

    # Attempt to write new state should raise OSError
    new_ts = datetime(2026, 5, 22, 13, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(OSError, match="Disk full"):
        ledger.set_last_timestamp("STUDY-01", "records", new_ts)

    # Verify original state was preserved
    monkeypatch.undo()
    assert ledger.get_last_timestamp("STUDY-01", "records") == ts


@pytest.mark.skipif(
    _state_ledger_module.fcntl is None,
    reason="flock not available on this platform (no fcntl)",
)
def test_state_ledger_flock_concurrency(tmp_path) -> None:
    """Test that state ledger flock concurrency."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    # Fill state
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp("STUDY-01", "records", ts)

    lock_acquired = threading.Event()
    release_lock = threading.Event()
    thread_done = threading.Event()

    def locking_thread():
        """Helper function to locking thread."""
        with ledger._lock():
            lock_acquired.set()
            release_lock.wait()
        thread_done.set()

    # Start a thread to acquire lock and hold it
    t = threading.Thread(target=locking_thread)
    t.start()

    # Wait for the thread to acquire the lock
    lock_acquired.wait(timeout=2.0)
    assert lock_acquired.is_set()

    # Try to acquire lock in main thread, it should block/wait.
    # Since we can't easily wait with timeout on flock directly in blocking way,
    # we verify that a second thread trying to acquire lock takes time.
    time.time()
    lock_acquired_in_t2 = threading.Event()

    def second_thread():
        """Helper function to second thread."""
        with ledger._lock():
            lock_acquired_in_t2.set()

    t2 = threading.Thread(target=second_thread)
    t2.start()

    # Main thread sleeps a bit, then releases the lock from the first thread
    time.sleep(0.5)
    assert not lock_acquired_in_t2.is_set()

    release_lock.set()
    lock_acquired_in_t2.wait(timeout=2.0)
    assert lock_acquired_in_t2.is_set()

    # Cleanup threads
    t.join()
    t2.join()


def test_delete_entry_removes_whole_study(tmp_path) -> None:
    """Test that delete entry removes whole study."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp("STUDY-01", "records", ts)
    ledger.set_last_timestamp("STUDY-02", "records", ts)

    removed = ledger.delete_entry("STUDY-01")

    assert removed is True
    state = ledger.read_state()
    assert "STUDY-01" not in state.studies
    assert "STUDY-02" in state.studies


def test_delete_entry_removes_specific_stream(tmp_path) -> None:
    """Test that delete entry removes specific stream."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp("STUDY-01", "records", ts)
    ledger.set_last_timestamp("STUDY-01", "queries", ts)

    removed = ledger.delete_entry("STUDY-01", stream_name="records")

    assert removed is True
    state = ledger.read_state()
    assert "STUDY-01" in state.studies
    assert "records" not in state.studies["STUDY-01"].streams
    assert "queries" in state.studies["STUDY-01"].streams


def test_delete_entry_returns_false_when_study_not_found(tmp_path) -> None:
    """Test that delete entry returns false when study not found."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    removed = ledger.delete_entry("NONEXISTENT-STUDY")

    assert removed is False


def test_delete_entry_returns_false_when_stream_not_found(tmp_path) -> None:
    """Test that delete entry returns false when stream not found."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp("STUDY-01", "records", ts)

    removed = ledger.delete_entry("STUDY-01", stream_name="nonexistent-stream")

    assert removed is False
    state = ledger.read_state()
    assert "records" in state.studies["STUDY-01"].streams


def test_corrupted_ledger_recovery(tmp_path) -> None:
    """Test that corrupted ledger recovery."""
    ledger_file = tmp_path / "ledger.json"
    # Write garbage content to file
    with open(ledger_file, "w") as f:
        f.write("corrupted JSON data {")

    ledger = ExtractionStateLedger(str(ledger_file))
    state = ledger.read_state()
    # Should recover gracefully with empty state
    assert len(state.studies) == 0


def test_get_state_provider_airflow(monkeypatch):
    """Test get_state_provider returns AirflowStateProvider when USE_AIRFLOW_STATE_PROVIDER=1."""
    monkeypatch.setenv("USE_AIRFLOW_STATE_PROVIDER", "1")
    from imednet_workflows.state_ledger import AirflowStateProvider, get_state_provider

    provider = get_state_provider()
    assert isinstance(provider, AirflowStateProvider)


def test_airflow_state_provider_methods():
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules,
        {
            'airflow': None,
            'airflow.models.xcom': None,
            'airflow.utils.session': None,
            'airflow.operators.python': None,
        },
    ):
        from imednet_workflows.state_ledger import AirflowStateProvider

        provider = AirflowStateProvider()
        assert provider.get_last_timestamp("STUDY-01", "records") is None
        ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
        provider.set_last_timestamp("STUDY-01", "records", ts)
        assert provider.delete_entry("STUDY-01") is False
        state = provider.read_state()
        assert len(state.studies) == 0


def test_airflow_state_provider_transaction_success():
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules,
        {
            'airflow': None,
            'airflow.models.xcom': None,
            'airflow.utils.session': None,
            'airflow.operators.python': None,
        },
    ):
        from imednet_workflows.state_ledger import AirflowStateProvider

        provider = AirflowStateProvider()
        fallback_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
        with provider.transaction("STUDY-01", "records", fallback_timestamp=fallback_ts) as tx:
            tx["new_timestamp"] = datetime(2026, 5, 22, 11, 0, 0, tzinfo=timezone.utc)


def test_airflow_state_provider_transaction_error():
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules,
        {
            'airflow': None,
            'airflow.models.xcom': None,
            'airflow.utils.session': None,
            'airflow.operators.python': None,
        },
    ):
        from imednet_workflows.state_ledger import AirflowStateProvider

        provider = AirflowStateProvider()
        fallback_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            with provider.transaction("STUDY-01", "records", fallback_timestamp=fallback_ts):
                raise ValueError("Test error")


def test_stream_execution_status_enum() -> None:
    """Test StreamExecutionStatus enumeration values and string representation."""
    assert StreamExecutionStatus.PENDING == "pending"
    assert StreamExecutionStatus.RUNNING == "running"
    assert StreamExecutionStatus.SUCCESS == "success"
    assert StreamExecutionStatus.FAILED == "failed"
    assert str(StreamExecutionStatus.PENDING) == "pending"
    assert str(StreamExecutionStatus.RUNNING) == "running"
    assert str(StreamExecutionStatus.SUCCESS) == "success"
    assert str(StreamExecutionStatus.FAILED) == "failed"
    assert f"{StreamExecutionStatus.SUCCESS}" == "success"


def test_valid_state_transitions() -> None:
    """Verify all valid lifecycle status transitions."""
    valid_cases = [
        (None, StreamExecutionStatus.PENDING),
        (None, StreamExecutionStatus.RUNNING),
        (None, StreamExecutionStatus.SUCCESS),
        (None, StreamExecutionStatus.FAILED),
        (StreamExecutionStatus.PENDING, StreamExecutionStatus.RUNNING),
        (StreamExecutionStatus.PENDING, StreamExecutionStatus.FAILED),
        (StreamExecutionStatus.PENDING, StreamExecutionStatus.PENDING),
        (StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS),
        (StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED),
        (StreamExecutionStatus.SUCCESS, StreamExecutionStatus.PENDING),
        (StreamExecutionStatus.SUCCESS, StreamExecutionStatus.RUNNING),
        (StreamExecutionStatus.SUCCESS, StreamExecutionStatus.SUCCESS),
        (StreamExecutionStatus.FAILED, StreamExecutionStatus.PENDING),
        (StreamExecutionStatus.FAILED, StreamExecutionStatus.RUNNING),
        (StreamExecutionStatus.FAILED, StreamExecutionStatus.FAILED),
    ]
    for current, target in valid_cases:
        assert is_valid_transition(current, target) is True
        result = validate_transition(current, target)
        assert result == target

    # Also verify string arguments
    assert is_valid_transition("pending", "running") is True
    assert validate_transition("pending", "running") == StreamExecutionStatus.RUNNING


def test_invalid_state_transitions_rejected() -> None:
    """Verify invalid lifecycle transitions are rejected with InvalidStateTransitionError."""
    invalid_cases = [
        (StreamExecutionStatus.PENDING, StreamExecutionStatus.SUCCESS),
        (StreamExecutionStatus.RUNNING, StreamExecutionStatus.RUNNING),
        (StreamExecutionStatus.RUNNING, StreamExecutionStatus.PENDING),
        (StreamExecutionStatus.SUCCESS, StreamExecutionStatus.FAILED),
        (StreamExecutionStatus.FAILED, StreamExecutionStatus.SUCCESS),
    ]
    for current, target in invalid_cases:
        assert is_valid_transition(current, target) is False
        with pytest.raises(InvalidStateTransitionError, match="Cannot transition stream state"):
            validate_transition(current, target)

    # Invalid status string argument
    assert is_valid_transition("pending", "not_a_status") is False
    with pytest.raises(InvalidStateTransitionError, match="Invalid stream execution status"):
        validate_transition("pending", "not_a_status")


def test_set_last_timestamp_validation_and_rejections(tmp_path) -> None:
    """Verify set_last_timestamp validates requested status values and transitions."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)

    # Invalid status value must raise ValueError
    with pytest.raises(ValueError, match="Invalid stream execution status"):
        ledger.set_last_timestamp("STUDY-01", "records", ts, status="invalid_status")

    # Valid status as enum and string succeeds
    ledger.set_last_timestamp("STUDY-01", "records", ts, status=StreamExecutionStatus.PENDING)
    state = ledger.read_state()
    assert (
        state.studies["STUDY-01"].streams["records"].last_run_status
        == StreamExecutionStatus.PENDING
    )

    # Transition from PENDING directly to SUCCESS must be rejected
    with pytest.raises(InvalidStateTransitionError, match="Cannot transition stream state"):
        ledger.set_last_timestamp("STUDY-01", "records", ts, status=StreamExecutionStatus.SUCCESS)

    # Transition from PENDING to RUNNING is valid
    ledger.set_last_timestamp("STUDY-01", "records", ts, status=StreamExecutionStatus.RUNNING)
    state2 = ledger.read_state()
    assert (
        state2.studies["STUDY-01"].streams["records"].last_run_status
        == StreamExecutionStatus.RUNNING
    )

    # Transition from RUNNING to SUCCESS is valid
    ledger.set_last_timestamp("STUDY-01", "records", ts, status=StreamExecutionStatus.SUCCESS)
    state3 = ledger.read_state()
    assert (
        state3.studies["STUDY-01"].streams["records"].last_run_status
        == StreamExecutionStatus.SUCCESS
    )


def test_legacy_ledger_backward_compatibility(tmp_path) -> None:
    """Legacy ledger files with unknown raw status strings safely fallback to failed."""
    ledger_file = tmp_path / "ledger.json"
    legacy_data = {
        "studies": {
            "STUDY-LEGACY": {
                "streams": {
                    "records": {
                        "last_timestamp": "2026-05-22T10:00:00+00:00",
                        "records_processed": 50,
                        "last_run_status": "legacy_in_progress",
                        "error_message": None,
                        "metadata": {"legacy": True},
                    }
                }
            }
        }
    }
    with open(ledger_file, "w", encoding="utf-8") as f:
        import json

        json.dump(legacy_data, f)

    ledger = ExtractionStateLedger(str(ledger_file))
    state = ledger.read_state()
    stream = state.studies["STUDY-LEGACY"].streams["records"]

    assert stream.last_run_status == StreamExecutionStatus.FAILED
    assert stream.records_processed == 50
    assert stream.metadata == {"legacy": True}


def test_transaction_atomic_guard_prevents_concurrent_runs(tmp_path) -> None:
    """Stream in RUNNING status rejects starting a second transaction."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    fallback_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)

    # Simulate an active or crashed worker that left stream in RUNNING state
    ledger.set_last_timestamp(
        "STUDY-01", "records", fallback_ts, status=StreamExecutionStatus.RUNNING
    )
    state = ledger.read_state()
    assert (
        state.studies["STUDY-01"].streams["records"].last_run_status
        == StreamExecutionStatus.RUNNING
    )

    # Attempting transaction for the stream while RUNNING must be rejected by atomic guard
    with pytest.raises(
        InvalidStateTransitionError,
        match="Cannot transition stream state from 'running' to 'running'",
    ):
        with ledger.transaction("STUDY-01", "records", fallback_timestamp=fallback_ts):
            pass


def test_transaction_failure_preserves_previous_high_water_mark(tmp_path) -> None:
    """Unexpected transaction exception transitions to failed and preserves previous high-water mark."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    previous_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    partial_ts = datetime(2026, 5, 22, 11, 0, 0, tzinfo=timezone.utc)

    # Establish an initial successful checkpoint
    ledger.set_last_timestamp("STUDY-01", "records", previous_ts, records_processed=50)

    # Start a subsequent transaction that sets new_timestamp then encounters an exception
    with pytest.raises(RuntimeError, match="Worker crash"):
        with ledger.transaction("STUDY-01", "records") as tx:
            assert tx["last_timestamp"] == previous_ts
            tx["new_timestamp"] = partial_ts
            tx["records_processed"] = 75
            raise RuntimeError("Worker crash")

    # High-water mark must NOT advance to partial_ts; previous_ts must be preserved
    retrieved_ts = ledger.get_last_timestamp("STUDY-01", "records")
    assert retrieved_ts == previous_ts

    state = ledger.read_state()
    stream = state.studies["STUDY-01"].streams["records"]
    assert stream.last_timestamp == previous_ts
    assert stream.last_run_status == StreamExecutionStatus.FAILED
    assert stream.error_message == "Worker crash"
    assert stream.records_processed == 75


def test_default_ledger_path_resolution(monkeypatch) -> None:
    """Verify that default ledger path points to user home directory."""
    monkeypatch.delenv("IMEDNET_WORKFLOW_LEDGER_PATH", raising=False)
    monkeypatch.delenv("AIRFLOW_CTX_TASK_ID", raising=False)
    monkeypatch.delenv("USE_AIRFLOW_STATE_PROVIDER", raising=False)

    expected_path = Path.home() / ".imednet" / "pipeline_ledger.json"
    assert DEFAULT_LEDGER_PATH == str(expected_path)

    provider = FileStateProvider()
    assert provider.ledger_path == expected_path

    factory_provider = get_state_provider()
    assert isinstance(factory_provider, FileStateProvider)
    assert factory_provider.ledger_path == expected_path


def test_default_ledger_path_env_override(monkeypatch, tmp_path) -> None:
    """Verify that IMEDNET_WORKFLOW_LEDGER_PATH overrides default ledger path."""
    custom_ledger = str(tmp_path / "custom" / "pipeline_ledger.json")
    monkeypatch.setenv("IMEDNET_WORKFLOW_LEDGER_PATH", custom_ledger)
    monkeypatch.delenv("AIRFLOW_CTX_TASK_ID", raising=False)
    monkeypatch.delenv("USE_AIRFLOW_STATE_PROVIDER", raising=False)

    provider = FileStateProvider()
    assert provider.ledger_path == Path(custom_ledger)

    factory_provider = get_state_provider()
    assert isinstance(factory_provider, FileStateProvider)
    assert factory_provider.ledger_path == Path(custom_ledger)

    # Explicit argument should take precedence over env var
    explicit_path = str(tmp_path / "explicit" / "ledger.json")
    explicit_provider = FileStateProvider(explicit_path)
    assert explicit_provider.ledger_path == Path(explicit_path)

    explicit_factory = get_state_provider(explicit_path)
    assert isinstance(explicit_factory, FileStateProvider)
    assert explicit_factory.ledger_path == Path(explicit_path)

    # Subprocess check to verify import-time DEFAULT_LEDGER_PATH resolution with env var set
    code = (
        "import os, sys; "
        f"os.environ['IMEDNET_WORKFLOW_LEDGER_PATH'] = {custom_ledger!r}; "
        "from imednet_workflows.state_ledger import DEFAULT_LEDGER_PATH; "
        f"assert DEFAULT_LEDGER_PATH == {custom_ledger!r}"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, f"Subprocess failed: {res.stderr}"


def test_cli_default_ledger_path_behavior() -> None:
    """Verify that workflow CLI commands use DEFAULT_LEDGER_PATH by default."""
    # 1. Test state_app show default
    with patch("imednet_workflows.cli.get_state_provider") as mock_get_state:
        mock_get_state.return_value.read_state.return_value = LedgerState(studies={})
        state_app(["show"])
        mock_get_state.assert_called_with(DEFAULT_LEDGER_PATH)

    # 2. Test state_app set default
    with patch("imednet_workflows.cli.get_state_provider") as mock_get_state:
        state_app(["set", "-s", "TEST-01", "-m", "records", "-t", "2026-05-22T12:00:00Z"])
        mock_get_state.assert_called_with(DEFAULT_LEDGER_PATH)

    # 3. Test state_app reset default
    with patch("imednet_workflows.cli.get_state_provider") as mock_get_state:
        mock_get_state.return_value.delete_entry.return_value = True
        state_app(["reset", "-s", "TEST-01", "-m", "records"])
        mock_get_state.assert_called_with(DEFAULT_LEDGER_PATH)

    # 4. Test setup_parser subcommand defaults
    root_parser = argparse.ArgumentParser()
    subparsers = root_parser.add_subparsers(dest="subcommand")
    setup_parser(subparsers)

    # Parse arguments without --ledger-path
    show_args = root_parser.parse_args(["workflows", "state", "show"])
    assert show_args.ledger_path == DEFAULT_LEDGER_PATH

    set_args = root_parser.parse_args(
        [
            "workflows",
            "state",
            "set",
            "-s",
            "TEST-01",
            "-m",
            "records",
            "-t",
            "2026-05-22T12:00:00Z",
        ]
    )
    assert set_args.ledger_path == DEFAULT_LEDGER_PATH

    reset_args = root_parser.parse_args(["workflows", "state", "reset", "-s", "TEST-01"])
    assert reset_args.ledger_path == DEFAULT_LEDGER_PATH


def test_file_state_provider_transaction_rolls_back_on_base_exception(tmp_path: Path) -> None:
    """FileStateProvider.transaction catches BaseException (e.g. task timeout) and allows retry."""

    class SimulatedTaskTimeout(BaseException):
        """Synthetic BaseException matching AirflowTaskTimeout hierarchy."""

    ledger = FileStateProvider(str(tmp_path / "ledger.json"))
    initial_ts = datetime(2026, 4, 1, 8, 0, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp("STUDY-TIMEOUT", "records", initial_ts)

    with pytest.raises(SimulatedTaskTimeout, match="execution_timeout exceeded"):
        with ledger.transaction("STUDY-TIMEOUT", "records") as tx:
            tx["new_timestamp"] = datetime(2026, 4, 2, 8, 0, 0, tzinfo=timezone.utc)
            tx["records_processed"] = 42
            raise SimulatedTaskTimeout("execution_timeout exceeded")

    assert ledger.get_last_status("STUDY-TIMEOUT", "records") == StreamExecutionStatus.FAILED
    assert ledger.get_last_timestamp("STUDY-TIMEOUT", "records") == initial_ts
    failed_stream = ledger.read_state().studies["STUDY-TIMEOUT"].streams["records"]
    assert failed_stream.error_message == "execution_timeout exceeded"

    retry_ts = datetime(2026, 4, 2, 9, 30, 0, tzinfo=timezone.utc)
    with ledger.transaction("STUDY-TIMEOUT", "records") as retry_tx:
        assert retry_tx["last_timestamp"] == initial_ts
        retry_tx["new_timestamp"] = retry_ts
        retry_tx["records_processed"] = 100

    assert ledger.get_last_status("STUDY-TIMEOUT", "records") == StreamExecutionStatus.SUCCESS
    assert ledger.get_last_timestamp("STUDY-TIMEOUT", "records") == retry_ts


def test_airflow_state_provider_sdk_variable_lifecycle_and_edge_cases(monkeypatch) -> None:
    """AirflowStateProvider reads, writes, lists, deletes, and rolls back via airflow.sdk.Variable."""
    import json
    import types

    from imednet_workflows.state_ledger import AirflowStateProvider

    store: dict[str, str] = {}

    class FakeSdkVariable:
        @staticmethod
        def get(key: str, default=None, deserialize_json: bool = False):
            if key not in store:
                return default
            raw = store[key]
            return json.loads(raw) if deserialize_json else raw

        @staticmethod
        def set(key: str, value, serialize_json: bool = False) -> None:
            store[key] = json.dumps(value) if serialize_json else str(value)

        @staticmethod
        def delete(key: str) -> None:
            store.pop(key, None)

        @staticmethod
        def keys(prefix: str = "") -> list[str]:
            return [k for k in sorted(store) if k.startswith(prefix)]

    fake_airflow = types.ModuleType("airflow")
    fake_sdk = types.ModuleType("airflow.sdk")
    fake_sdk.Variable = FakeSdkVariable  # type: ignore[attr-defined]
    fake_exec = types.ModuleType("airflow.sdk.execution_time")
    fake_runner = types.ModuleType("airflow.sdk.execution_time.task_runner")
    fake_runner.SUPERVISOR_COMMS = object()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "airflow", fake_airflow)
    monkeypatch.setitem(sys.modules, "airflow.sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "airflow.sdk.execution_time", fake_exec)
    monkeypatch.setitem(sys.modules, "airflow.sdk.execution_time.task_runner", fake_runner)

    provider = AirflowStateProvider()

    # Special characters (colon, tilde, slash) round-trip without key collisions
    ts_naive = datetime(2026, 6, 1, 10, 0, 0)
    provider.set_last_timestamp(
        "STUDY:01~A",
        "records/dm",
        ts_naive,
        status=StreamExecutionStatus.RUNNING,
    )
    assert provider.get_last_timestamp("STUDY:01~A", "records/dm") == datetime(
        2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc
    )
    assert provider.get_last_status("STUDY:01~A", "records/dm") == StreamExecutionStatus.RUNNING

    # RUNNING -> RUNNING without force=True is rejected
    with pytest.raises(InvalidStateTransitionError):
        provider.set_last_timestamp(
            "STUDY:01~A",
            "records/dm",
            ts_naive,
            status=StreamExecutionStatus.RUNNING,
            force=False,
        )

    # Complete RUNNING -> SUCCESS and add second stream + unrelated/malformed keys
    ts_done = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    provider.set_last_timestamp(
        "STUDY:01~A",
        "records/dm",
        ts_done,
        status=StreamExecutionStatus.SUCCESS,
        force=False,
    )
    provider.set_last_timestamp("STUDY-02", "queries", ts_done)
    store["unrelated_prefix:foo:bar"] = "{}"
    store["imednet_state:missing_stream_separator"] = "{}"
    store["imednet_state:STUDY-BAD:broken_json"] = "{not-json"

    state = provider.read_state()
    assert set(state.studies) == {"STUDY:01~A", "STUDY-02"}
    assert state.studies["STUDY:01~A"].streams["records/dm"].last_timestamp == ts_done

    # Parsing resilience for non-string timestamp and unknown/enum status
    assert AirflowStateProvider._parse_timestamp({"last_timestamp": 123}) is None
    assert (
        AirflowStateProvider._parse_status({"status": StreamExecutionStatus.PENDING})
        == StreamExecutionStatus.PENDING
    )
    assert AirflowStateProvider._parse_status({"status": "corrupt"}) == StreamExecutionStatus.FAILED
    assert AirflowStateProvider._parse_status({"status": 999}) == StreamExecutionStatus.FAILED
    assert provider._parse_variable_key("other:study:stream") is None
    assert provider._parse_variable_key("imednet_state:only_one_part") is None

    # BaseException rollback during transaction preserves prior high-water mark
    class SyntheticTimeout(BaseException):
        pass

    with pytest.raises(SyntheticTimeout):
        with provider.transaction("STUDY-02", "queries") as tx:
            tx["new_timestamp"] = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
            raise SyntheticTimeout("timed out")

    assert provider.get_last_status("STUDY-02", "queries") == StreamExecutionStatus.FAILED
    assert provider.get_last_timestamp("STUDY-02", "queries") == ts_done

    # Retry succeeds and advances checkpoint
    retry_ts = datetime(2026, 6, 2, 1, 0, 0, tzinfo=timezone.utc)
    with provider.transaction("STUDY-02", "queries") as tx:
        tx["new_timestamp"] = retry_ts
        tx["records_processed"] = 7
    assert provider.get_last_status("STUDY-02", "queries") == StreamExecutionStatus.SUCCESS
    assert provider.get_last_timestamp("STUDY-02", "queries") == retry_ts

    # Delete specific stream, missing stream, and entire Study
    assert provider.delete_entry("STUDY:01~A", "missing") is False
    assert provider.delete_entry("STUDY:01~A", "records/dm") is True
    assert provider.delete_entry("STUDY-02") is True
    assert provider.delete_entry("STUDY-02") is False


def test_airflow_state_provider_out_of_context_model_variable_crud(tmp_path, monkeypatch) -> None:
    """Verify out-of-context set, get, read_state, and delete_entry against Airflow 3 Variable ORM."""
    import logging

    if (
        "airflow" not in sys.modules
        or getattr(sys.modules.get("airflow"), "__path__", None) is None
    ):
        for mod in list(sys.modules):
            if mod == "airflow" or mod.startswith("airflow."):
                sys.modules.pop(mod, None)

    airflow = pytest.importorskip("airflow")
    _ = airflow
    from airflow import settings
    from airflow.configuration import conf
    from airflow.utils.db import initdb

    from imednet_workflows.state_ledger import AirflowStateProvider

    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    af_home = tmp_path / "af_unit_db"
    af_home.mkdir(parents=True, exist_ok=True)
    db_uri = f"sqlite:///{af_home / 'airflow.db'}"

    prev_settings_conn = settings.SQL_ALCHEMY_CONN
    prev_settings_conn_async = getattr(settings, "SQL_ALCHEMY_CONN_ASYNC", None)
    prev_conf_conn = conf.get("database", "sql_alchemy_conn", fallback=None)
    prev_pool_enabled = conf.get("database", "sql_alchemy_pool_enabled", fallback=None)
    monkeypatch.setenv("AIRFLOW_HOME", str(af_home))
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", db_uri)
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_ENABLED", "False")
    conf.set("database", "sql_alchemy_conn", db_uri)
    conf.set("database", "sql_alchemy_pool_enabled", "False")

    try:
        settings.dispose_orm()
        settings.configure_vars()
        monkeypatch.setattr(settings, "SQL_ALCHEMY_CONN", db_uri)
        settings.configure_orm()
        initdb()

        provider = AirflowStateProvider()
        assert provider.get_last_timestamp("STUDY_DB", "records") is None
        assert provider.get_last_status("STUDY_DB", "records") is None
        assert provider.read_state().studies == {}

        t1 = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
        provider.set_last_timestamp(
            "STUDY_DB",
            "records",
            t1,
            records_processed=25,
            status=StreamExecutionStatus.SUCCESS,
            metadata={"source": "cli"},
        )
        provider.set_last_timestamp(
            "STUDY_DB",
            "queries",
            t1,
            records_processed=5,
            status="success",
        )

        assert provider.get_last_timestamp("STUDY_DB", "records") == t1
        assert provider.get_last_status("STUDY_DB", "records") == StreamExecutionStatus.SUCCESS

        ledger = provider.read_state()
        assert "STUDY_DB" in ledger.studies
        assert ledger.studies["STUDY_DB"].streams["records"].records_processed == 25
        assert ledger.studies["STUDY_DB"].streams["queries"].records_processed == 5

        # Delete single stream, then whole study, then non-existent study
        assert provider.delete_entry("STUDY_DB", "queries") is True
        assert provider.delete_entry("STUDY_DB", "queries") is False
        assert provider.delete_entry("STUDY_DB") is True
        assert provider.delete_entry("STUDY_DB") is False
    finally:
        settings.dispose_orm()
        if prev_conf_conn is not None:
            conf.set("database", "sql_alchemy_conn", prev_conf_conn)
        if prev_pool_enabled is not None:
            conf.set("database", "sql_alchemy_pool_enabled", prev_pool_enabled)
        settings.configure_vars()
        settings.SQL_ALCHEMY_CONN = prev_settings_conn
        if prev_settings_conn_async is not None and hasattr(settings, "SQL_ALCHEMY_CONN_ASYNC"):
            settings.SQL_ALCHEMY_CONN_ASYNC = prev_settings_conn_async
        settings.configure_orm()
        root_logger.handlers = saved_handlers
        root_logger.setLevel(saved_level)


def test_initial_transaction_failure_keeps_watermark_none_for_retry(tmp_path) -> None:
    """Failed initial transaction keeps last_timestamp as None so retry reads historical data."""
    import socket

    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    with pytest.raises(RuntimeError, match="Initial API failure"):
        with ledger.transaction("STUDY-INIT", "records") as tx:
            assert tx["last_timestamp"] is None
            running_stream = ledger.read_state().studies["STUDY-INIT"].streams["records"]
            assert running_stream.last_run_status == StreamExecutionStatus.RUNNING
            assert running_stream.last_timestamp is None
            assert running_stream.owner_pid == os.getpid()
            assert running_stream.owner_host == socket.gethostname()
            assert running_stream.lease_expires_at is not None
            raise RuntimeError("Initial API failure")

    assert ledger.get_last_timestamp("STUDY-INIT", "records") is None
    failed_stream = ledger.read_state().studies["STUDY-INIT"].streams["records"]
    assert failed_stream.last_run_status == StreamExecutionStatus.FAILED
    assert failed_stream.last_timestamp is None
    assert failed_stream.owner_pid is None
    assert failed_stream.owner_host is None
    assert failed_stream.lease_expires_at is None

    # Retry must still see last_timestamp=None and commit cleanly on success
    committed_ts = datetime(2026, 2, 10, 8, 30, 0, tzinfo=timezone.utc)
    with ledger.transaction("STUDY-INIT", "records") as retry_tx:
        assert retry_tx["last_timestamp"] is None
        retry_tx["new_timestamp"] = committed_ts
        retry_tx["records_processed"] = 12

    assert ledger.get_last_timestamp("STUDY-INIT", "records") == committed_ts


def test_empty_initial_and_subsequent_transactions_preserve_committed_watermark(tmp_path) -> None:
    """Zero-record initial sync keeps None; zero-record subsequent sync preserves prior watermark."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    # Initial sync reading zero records must keep last_timestamp as None
    with ledger.transaction("STUDY-EMPTY", "records") as tx:
        assert tx["last_timestamp"] is None
        tx["records_processed"] = 0

    assert ledger.get_last_timestamp("STUDY-EMPTY", "records") is None
    initial_stream = ledger.read_state().studies["STUDY-EMPTY"].streams["records"]
    assert initial_stream.last_run_status == StreamExecutionStatus.SUCCESS
    assert initial_stream.last_timestamp is None

    # Set a committed watermark via a non-empty run
    wm = datetime(2026, 3, 5, 14, 0, 0, tzinfo=timezone.utc)
    with ledger.transaction("STUDY-EMPTY", "records") as tx2:
        assert tx2["last_timestamp"] is None
        tx2["new_timestamp"] = wm
        tx2["records_processed"] = 5
    assert ledger.get_last_timestamp("STUDY-EMPTY", "records") == wm

    # Subsequent empty run must preserve wm
    with ledger.transaction("STUDY-EMPTY", "records") as tx3:
        assert tx3["last_timestamp"] == wm
        tx3["records_processed"] = 0
    assert ledger.get_last_timestamp("STUDY-EMPTY", "records") == wm


def test_set_last_timestamp_accepts_none(tmp_path) -> None:
    """set_last_timestamp accepts None for uncommitted stream checkpoints."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    ledger.set_last_timestamp("STUDY-NONE", "records", None, status=StreamExecutionStatus.PENDING)
    assert ledger.get_last_timestamp("STUDY-NONE", "records") is None
    stream = ledger.read_state().studies["STUDY-NONE"].streams["records"]
    assert stream.last_timestamp is None
    assert stream.last_run_status == StreamExecutionStatus.PENDING


def test_sigkill_crashed_worker_recovers_through_failed_to_running(tmp_path) -> None:
    """Retry after SIGKILL crash recovers dead-PID RUNNING stream through FAILED to RUNNING."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))
    prior_ts = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp("STUDY-CRASH", "records", prior_ts, records_processed=20)

    worker_code = (
        "import sys, time\n"
        f"from imednet_workflows.state_ledger import ExtractionStateLedger\n"
        f"ledger = ExtractionStateLedger({str(ledger_file)!r})\n"
        "with ledger.transaction('STUDY-CRASH', 'records'):\n"
        "    sys.stdout.write('READY\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = proc.stdout.readline().strip() if proc.stdout else ""
        assert ready_line == "READY"
        proc.kill()
        proc.wait(timeout=5)
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    crashed_stream = ledger.read_state().studies["STUDY-CRASH"].streams["records"]
    assert crashed_stream.last_run_status == StreamExecutionStatus.RUNNING
    assert crashed_stream.owner_pid == proc.pid
    assert crashed_stream.last_timestamp == prior_ts

    observed_statuses: list[StreamExecutionStatus] = []
    orig_write = ledger.write_state

    def tracking_write(state: LedgerState) -> None:
        observed_statuses.append(state.studies["STUDY-CRASH"].streams["records"].last_run_status)
        orig_write(state)

    new_ts = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)
    with patch.object(ledger, "write_state", side_effect=tracking_write):
        with ledger.transaction("STUDY-CRASH", "records") as retry_tx:
            assert retry_tx["last_timestamp"] == prior_ts
            retry_tx["new_timestamp"] = new_ts
            retry_tx["records_processed"] = 8

    assert observed_statuses == [
        StreamExecutionStatus.FAILED,
        StreamExecutionStatus.RUNNING,
        StreamExecutionStatus.SUCCESS,
    ]
    assert ledger.get_last_timestamp("STUDY-CRASH", "records") == new_ts


def test_active_concurrent_run_and_remote_lease_expiry_guards(tmp_path) -> None:
    """Active local/remote leases raise InvalidStateTransitionError; expired leases recover."""
    import json
    from datetime import timedelta

    ledger_file = tmp_path / "ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    # 1. Active local transaction blocks re-entrant / concurrent transaction on same stream
    with ledger.transaction("STUDY-GUARD", "records"):
        with pytest.raises(
            InvalidStateTransitionError,
            match="Cannot transition stream state from 'running' to 'running'",
        ):
            with ledger.transaction("STUDY-GUARD", "records"):
                pass

    # 2. Remote container worker with unexpired lease blocks concurrent transaction
    future_exp = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    remote_active = {
        "studies": {
            "STUDY-REMOTE": {
                "streams": {
                    "records": {
                        "last_timestamp": None,
                        "records_processed": 0,
                        "last_run_status": "running",
                        "owner_pid": 99999999,
                        "owner_host": "remote-container-worker-a",
                        "lease_expires_at": future_exp,
                    }
                }
            }
        }
    }
    ledger_file.write_text(json.dumps(remote_active), encoding="utf-8")
    with pytest.raises(
        InvalidStateTransitionError,
        match="Cannot transition stream state from 'running' to 'running'",
    ):
        with ledger.transaction("STUDY-REMOTE", "records"):
            pass

    # 3. Remote container worker (even with PID 1) recovers once lease expires
    past_exp = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    remote_expired = {
        "studies": {
            "STUDY-REMOTE": {
                "streams": {
                    "records": {
                        "last_timestamp": None,
                        "records_processed": 0,
                        "last_run_status": "running",
                        "owner_pid": 1,
                        "owner_host": "remote-container-worker-a",
                        "lease_expires_at": past_exp,
                    }
                }
            }
        }
    }
    ledger_file.write_text(json.dumps(remote_expired), encoding="utf-8")
    recovered_ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with ledger.transaction("STUDY-REMOTE", "records") as tx:
        assert tx["last_timestamp"] is None
        tx["new_timestamp"] = recovered_ts
        tx["records_processed"] = 3
    assert ledger.get_last_timestamp("STUDY-REMOTE", "records") == recovered_ts
