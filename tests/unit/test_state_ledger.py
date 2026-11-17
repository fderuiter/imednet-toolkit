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
