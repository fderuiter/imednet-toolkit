"""Unit tests for Airflow Variable state provider and provider factory."""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any

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
def sdk_variable_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install an in-memory ``airflow.sdk.Variable`` stub without task_runner internals."""
    store: dict[str, str] = {}

    class FakeVariable:
        @staticmethod
        def get(key: str, default: Any = None, deserialize_json: bool = False) -> Any:
            if key not in store:
                return default
            raw = store[key]
            return json.loads(raw) if deserialize_json else raw

        @staticmethod
        def set(
            key: str,
            value: Any,
            description: str | None = None,
            serialize_json: bool = False,
        ) -> None:
            _ = description
            store[key] = json.dumps(value) if serialize_json else str(value)

        @staticmethod
        def delete(key: str) -> None:
            store.pop(key, None)

        @staticmethod
        def keys(prefix: str | None = None) -> list[str]:
            if prefix is None:
                return sorted(store.keys())
            return sorted(k for k in store if k.startswith(prefix))

    airflow_mod = ModuleType("airflow")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.Variable = FakeVariable  # type: ignore[attr-defined]
    airflow_mod.sdk = sdk_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
    monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time", raising=False)
    monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time.task_runner", raising=False)
    return store


def test_get_state_provider(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_airflow_variable_roundtrip_and_read_state(sdk_variable_store: dict[str, Any]) -> None:
    provider = AirflowStateProvider()
    ts_naive = datetime(2026, 5, 22, 12, 0, 0)

    provider.set_last_timestamp(
        "STUDY:01_A",
        "records_main",
        ts_naive,
        records_processed=12,
        metadata={"batch": 1},
    )
    provider.set_last_timestamp(
        "STUDY:01_A",
        "queries",
        datetime(2026, 5, 23, 8, 30, 0, tzinfo=timezone.utc),
        records_processed=3,
    )

    resolved_ts = provider.get_last_timestamp("STUDY:01_A", "records_main")
    assert resolved_ts == datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    assert provider.get_last_status("STUDY:01_A", "records_main") == StreamExecutionStatus.SUCCESS

    state = provider.read_state()
    assert "STUDY:01_A" in state.studies
    assert state.studies["STUDY:01_A"].streams["records_main"].records_processed == 12
    assert state.studies["STUDY:01_A"].streams["records_main"].metadata == {"batch": 1}
    assert state.studies["STUDY:01_A"].streams["queries"].records_processed == 3
    assert len(sdk_variable_store) == 2


def test_airflow_delete_entry_stream_and_study(sdk_variable_store: dict[str, Any]) -> None:
    _ = sdk_variable_store
    provider = AirflowStateProvider()
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)

    provider.set_last_timestamp("ST1", "recs", ts)
    provider.set_last_timestamp("ST1", "queries", ts)
    provider.set_last_timestamp("ST1_OTHER", "recs", ts)

    assert provider.delete_entry("ST1", "missing") is False
    assert provider.delete_entry("ST1", "queries") is True
    assert provider.get_last_timestamp("ST1", "queries") is None
    assert provider.get_last_timestamp("ST1", "recs") == ts

    assert provider.delete_entry("ST1") is True
    assert provider.get_last_timestamp("ST1", "recs") is None
    assert provider.delete_entry("ST1") is False
    # Prefix isolation: ST1_OTHER must remain untouched when ST1 is deleted
    assert provider.get_last_timestamp("ST1_OTHER", "recs") == ts


def test_airflow_transaction_guard_and_force_override(sdk_variable_store: dict[str, Any]) -> None:
    _ = sdk_variable_store
    provider = AirflowStateProvider()
    ts = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)

    provider.set_last_timestamp("ST1", "recs", ts, status=StreamExecutionStatus.RUNNING)

    with pytest.raises(
        InvalidStateTransitionError,
        match="Cannot transition stream state from 'running' to 'running'",
    ):
        provider.set_last_timestamp("ST1", "recs", ts, status=StreamExecutionStatus.RUNNING)

    with pytest.raises(
        InvalidStateTransitionError,
        match="Cannot transition stream state from 'running' to 'running'",
    ):
        with provider.transaction("ST1", "recs"):
            pass

    # Explicit force=True permits administrative recovery reset
    provider.set_last_timestamp(
        "ST1",
        "recs",
        ts,
        status=StreamExecutionStatus.FAILED,
        force=True,
    )
    assert provider.get_last_status("ST1", "recs") == StreamExecutionStatus.FAILED


def test_airflow_transaction_base_exception_rollback_and_retry(
    sdk_variable_store: dict[str, Any],
) -> None:
    """AirflowStateProvider.transaction catches BaseException, preserves watermark, and retries."""
    _ = sdk_variable_store

    class SimulatedAirflowTimeout(BaseException):
        """Synthetic BaseException matching AirflowTaskTimeout."""

    provider = AirflowStateProvider()
    prior_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    partial_ts = datetime(2026, 5, 22, 11, 0, 0, tzinfo=timezone.utc)
    provider.set_last_timestamp("ST1", "recs", prior_ts)

    with pytest.raises(SimulatedAirflowTimeout, match="Task timed out"):
        with provider.transaction("ST1", "recs") as tx:
            tx["new_timestamp"] = partial_ts
            tx["records_processed"] = 99
            raise SimulatedAirflowTimeout("Task timed out")

    assert provider.get_last_status("ST1", "recs") == StreamExecutionStatus.FAILED
    assert provider.get_last_timestamp("ST1", "recs") == prior_ts
    stream_state = provider.read_state().studies["ST1"].streams["recs"]
    assert stream_state.records_processed == 99
    assert stream_state.error_message == "Task timed out"

    commit_ts = datetime(2026, 5, 22, 12, 30, 0, tzinfo=timezone.utc)
    with provider.transaction("ST1", "recs") as retry_tx:
        assert retry_tx["last_timestamp"] == prior_ts
        retry_tx["new_timestamp"] = commit_ts
        retry_tx["records_processed"] = 120

    assert provider.get_last_status("ST1", "recs") == StreamExecutionStatus.SUCCESS
    assert provider.get_last_timestamp("ST1", "recs") == commit_ts


def test_airflow_corrupted_payloads_and_uninstalled_fallback(
    sdk_variable_store: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AirflowStateProvider()
    bad_key = provider._get_variable_key("ST_BAD", "recs")
    sdk_variable_store[bad_key] = json.dumps(
        {"last_timestamp": "2026-05-22T00:00:00Z", "status": "bogus"}
    )
    assert provider.get_last_status("ST_BAD", "recs") == StreamExecutionStatus.FAILED

    sdk_variable_store[f"{provider.variable_prefix}:malformed_key"] = "{not-json"
    state = provider.read_state()
    assert "ST_BAD" in state.studies

    monkeypatch.setitem(sys.modules, "airflow", None)
    assert provider.read_state() == LedgerState()
    assert provider.delete_entry("ST1") is False
    assert provider.get_last_timestamp("ST1", "rec") is None
    assert provider.get_last_status("ST1", "rec") is None
    provider.set_last_timestamp("ST1", "rec", datetime.now(timezone.utc))


def test_airflow_set_last_timestamp_invalid_status(sdk_variable_store: dict[str, Any]) -> None:
    _ = sdk_variable_store
    provider = AirflowStateProvider()
    with pytest.raises(ValueError, match="Invalid stream execution status"):
        provider.set_last_timestamp(
            "ST1", "recs", datetime.now(timezone.utc), status="unsupported_status"
        )


def test_airflow_initial_failure_and_empty_run_keep_none_watermark(
    sdk_variable_store: dict[str, Any],
) -> None:
    """Airflow provider keeps last_timestamp as None on failed or zero-record initial syncs."""
    provider = AirflowStateProvider()
    var_key = provider._get_variable_key("ST-INIT", "recs")

    with pytest.raises(RuntimeError, match="initial airflow failure"):
        with provider.transaction("ST-INIT", "recs") as tx:
            assert tx["last_timestamp"] is None
            running_payload = json.loads(sdk_variable_store[var_key])
            assert running_payload["status"] == "running"
            assert running_payload["last_timestamp"] is None
            raise RuntimeError("initial airflow failure")

    failed_payload = json.loads(sdk_variable_store[var_key])
    assert failed_payload["status"] == "failed"
    assert failed_payload["last_timestamp"] is None
    assert provider.get_last_timestamp("ST-INIT", "recs") is None

    # Subsequent retry that reads zero records commits status=success while keeping None watermark
    with provider.transaction("ST-INIT", "recs") as retry_tx:
        assert retry_tx["last_timestamp"] is None
        retry_tx["records_processed"] = 0

    ok_payload = json.loads(sdk_variable_store[var_key])
    assert ok_payload["status"] == "success"
    assert ok_payload["last_timestamp"] is None
    assert provider.get_last_timestamp("ST-INIT", "recs") is None


def test_airflow_recovers_stale_running_variable_and_blocks_active_lease(
    sdk_variable_store: dict[str, Any],
) -> None:
    """Airflow provider recovers expired/dead-PID RUNNING Variable leases via FAILED -> RUNNING."""
    provider = AirflowStateProvider()
    var_key = provider._get_variable_key("ST1", "recs")
    pushed_statuses: list[str] = []

    orig_write = provider._write_variable

    def _tracking_write(key: str, payload: dict[str, Any]) -> None:
        pushed_statuses.append(str(payload["status"]))
        orig_write(key, payload)

    provider._write_variable = _tracking_write  # type: ignore[method-assign]

    # Active lease on current PID blocks concurrent transaction
    sdk_variable_store[var_key] = json.dumps(
        {
            "last_timestamp": None,
            "status": "running",
            "owner_pid": os.getpid(),
            "owner_host": socket.gethostname(),
            "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        }
    )
    with pytest.raises(InvalidStateTransitionError):
        with provider.transaction("ST1", "recs"):
            pass

    # Expired remote lease transitions RUNNING -> FAILED -> RUNNING -> SUCCESS
    sdk_variable_store[var_key] = json.dumps(
        {
            "last_timestamp": None,
            "status": "running",
            "owner_pid": 1,
            "owner_host": "airflow-worker-dead-pod",
            "lease_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        }
    )
    committed = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    with provider.transaction("ST1", "recs") as tx:
        assert tx["last_timestamp"] is None
        tx["new_timestamp"] = committed
        tx["records_processed"] = 4

    assert pushed_statuses == ["failed", "running", "success"]
    assert provider.get_last_timestamp("ST1", "recs") == committed


def test_file_and_airflow_transaction_hydrate_persist_rollback_and_redact_sdtm_metadata(
    tmp_path: Path,
    sdk_variable_store: dict[str, Any],
) -> None:
    """Both state providers hydrate, redact, persist, and roll back SDTM metadata in transactions."""
    _ = sdk_variable_store
    file_provider = FileStateProvider(ledger_path=tmp_path / "sdtm_meta_ledger.json")
    airflow_provider = AirflowStateProvider()

    for label, provider in (("file", file_provider), ("airflow", airflow_provider)):
        study = f"STUDY_{label.upper()}"
        t1 = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

        # Run 1: populate SDTM sequence counters and verify PHI redaction in metadata
        with provider.transaction(study, "ae_sdtm") as tx1:
            assert tx1["metadata"] == {}
            tx1["metadata"]["sdtm_seq_counters"] = {"AE:SUBJ-01": 2, "AE:SUBJ-02": 1}
            tx1["metadata"]["sdtm_record_seqs"] = {"AE:1": 1, "AE:2": 2, "AE:3": 1}
            tx1["metadata"]["patientName"] = "Jane Secret"
            tx1["new_timestamp"] = t1
            tx1["records_processed"] = 3

        committed_1 = provider.read_state().studies[study].streams["ae_sdtm"]
        assert committed_1.last_timestamp == t1
        assert committed_1.metadata["sdtm_seq_counters"] == {"AE:SUBJ-01": 2, "AE:SUBJ-02": 1}
        assert committed_1.metadata["sdtm_record_seqs"] == {"AE:1": 1, "AE:2": 2, "AE:3": 1}
        assert committed_1.metadata["patientName"] == "***MASKED***"

        # Run 2 (failed mid-stream): mutate metadata in place, then raise -> rolls back metadata & timestamp
        with pytest.raises(RuntimeError, match="reconcile boom"):
            with provider.transaction(study, "ae_sdtm") as tx_fail:
                assert tx_fail["metadata"]["sdtm_seq_counters"]["AE:SUBJ-01"] == 2
                tx_fail["metadata"]["sdtm_seq_counters"]["AE:SUBJ-01"] = 99
                tx_fail["metadata"]["sdtm_record_seqs"]["AE:999"] = 99
                tx_fail["new_timestamp"] = t2
                raise RuntimeError("reconcile boom")

        rolled_back = provider.read_state().studies[study].streams["ae_sdtm"]
        assert rolled_back.last_run_status == StreamExecutionStatus.FAILED
        assert rolled_back.last_timestamp == t1
        assert rolled_back.metadata["sdtm_seq_counters"] == {"AE:SUBJ-01": 2, "AE:SUBJ-02": 1}
        assert "AE:999" not in rolled_back.metadata["sdtm_record_seqs"]

        # Run 3 (retry succeeds): hydrates Run 1 counters and advances to 3
        with provider.transaction(study, "ae_sdtm") as tx3:
            assert tx3["last_timestamp"] == t1
            assert tx3["metadata"]["sdtm_seq_counters"] == {"AE:SUBJ-01": 2, "AE:SUBJ-02": 1}
            tx3["metadata"]["sdtm_seq_counters"]["AE:SUBJ-01"] = 3
            tx3["metadata"]["sdtm_record_seqs"]["AE:4"] = 3
            tx3["new_timestamp"] = t3
            tx3["records_processed"] = 1

        committed_3 = provider.read_state().studies[study].streams["ae_sdtm"]
        assert committed_3.last_run_status == StreamExecutionStatus.SUCCESS
        assert committed_3.last_timestamp == t3
        assert committed_3.metadata["sdtm_seq_counters"] == {"AE:SUBJ-01": 3, "AE:SUBJ-02": 1}
        assert committed_3.metadata["sdtm_record_seqs"]["AE:4"] == 3
