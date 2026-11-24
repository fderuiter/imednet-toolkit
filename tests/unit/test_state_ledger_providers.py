"""Unit tests for Airflow Variable state provider and provider factory."""

from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    BaseStateProvider,
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
    assert len(sdk_variable_store) == 1
    assert provider._get_variable_key("STUDY:01_A") in sdk_variable_store


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
    var_key = provider._get_variable_key("ST-INIT")

    with pytest.raises(RuntimeError, match="initial airflow failure"):
        with provider.transaction("ST-INIT", "recs") as tx:
            assert tx["last_timestamp"] is None
            running_payload = json.loads(sdk_variable_store[var_key])["streams"]["recs"]
            assert running_payload["status"] == "running"
            assert running_payload["last_timestamp"] is None
            raise RuntimeError("initial airflow failure")

    failed_payload = json.loads(sdk_variable_store[var_key])["streams"]["recs"]
    assert failed_payload["status"] == "failed"
    assert failed_payload["last_timestamp"] is None
    assert provider.get_last_timestamp("ST-INIT", "recs") is None

    # Subsequent retry that reads zero records commits status=success while keeping None watermark
    with provider.transaction("ST-INIT", "recs") as retry_tx:
        assert retry_tx["last_timestamp"] is None
        retry_tx["records_processed"] = 0

    ok_payload = json.loads(sdk_variable_store[var_key])["streams"]["recs"]
    assert ok_payload["status"] == "success"
    assert ok_payload["last_timestamp"] is None
    assert provider.get_last_timestamp("ST-INIT", "recs") is None


def test_airflow_recovers_stale_running_variable_and_blocks_active_lease(
    sdk_variable_store: dict[str, Any],
) -> None:
    """Airflow provider recovers expired/dead-PID RUNNING Variable leases via FAILED -> RUNNING."""
    provider = AirflowStateProvider()
    legacy_var_key = provider._get_variable_key("ST1", "recs")
    pushed_statuses: list[str] = []

    orig_write = provider._write_variable

    def _tracking_write(key: str, payload: dict[str, Any]) -> None:
        pushed_statuses.append(str(payload["streams"]["recs"]["status"]))
        orig_write(key, payload)

    provider._write_variable = _tracking_write  # type: ignore[method-assign]

    # Active lease on current PID blocks concurrent transaction
    sdk_variable_store[legacy_var_key] = json.dumps(
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

    # Expired remote lease transitions RUNNING -> FAILED -> RUNNING -> SUCCESS and deletes legacy key
    sdk_variable_store[legacy_var_key] = json.dumps(
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
    assert legacy_var_key not in sdk_variable_store
    assert provider._get_variable_key("ST1") in sdk_variable_store
    assert provider.get_last_timestamp("ST1", "recs") == committed


def test_batch_transaction_single_write_per_phase_and_rollback(
    tmp_path: Path,
    sdk_variable_store: dict[str, Any],
) -> None:
    """FileStateProvider and AirflowStateProvider batch_transaction write once per study phase."""
    _ = sdk_variable_store
    file_provider = FileStateProvider(tmp_path / "batch_ledger.json")
    airflow_provider = AirflowStateProvider()

    file_writes: list[int] = []
    orig_file_write = file_provider.write_state
    file_provider.write_state = lambda st: (  # type: ignore[method-assign]
        file_writes.append(1),
        orig_file_write(st),
    )[1]

    af_writes: list[str] = []
    orig_af_write = airflow_provider._write_variable
    airflow_provider._write_variable = lambda k, v: (  # type: ignore[method-assign]
        af_writes.append(k),
        orig_af_write(k, v),
    )[1]

    for prov, counter in ((file_provider, file_writes), (airflow_provider, af_writes)):
        # Empty stream list performs zero writes
        with prov.batch_transaction("ST_BATCH", []) as empty_tx:
            assert empty_tx == {}
        assert len(counter) == 0

        # Seed one SUCCESS stream before running batch_transaction
        t_seed = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        prov.set_last_timestamp("ST_BATCH", "form_a", t_seed, records_processed=3)
        counter.clear()

        # BaseException rollback writes once on lease acquisition and once on rollback
        class SimulatedCancel(BaseException):
            pass

        with pytest.raises(SimulatedCancel, match="worker interrupted"):
            with prov.batch_transaction("ST_BATCH", ["form_a", "form_b", "form_c"]) as tx_map:
                assert len(counter) == 1
                assert tx_map["form_a"]["last_timestamp"] == t_seed
                assert tx_map["form_b"]["last_timestamp"] is None
                tx_map["form_a"]["new_timestamp"] = datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc)
                tx_map["form_b"]["records_processed"] = 7
                raise SimulatedCancel("worker interrupted")

        assert len(counter) == 2
        assert prov.get_last_timestamp("ST_BATCH", "form_a") == t_seed
        assert prov.get_last_timestamp("ST_BATCH", "form_b") is None
        assert prov.get_last_status("ST_BATCH", "form_a") == StreamExecutionStatus.FAILED
        assert prov.get_last_status("ST_BATCH", "form_b") == StreamExecutionStatus.FAILED

        # Active RUNNING lease on form_c blocks batch_transaction without mutating form_a
        prov.set_last_timestamp(
            "ST_BATCH",
            "form_c",
            None,
            status=StreamExecutionStatus.RUNNING,
            force=True,
        )
        if isinstance(prov, AirflowStateProvider):
            prov.set_last_timestamp(
                "ST_BATCH",
                "form_c",
                None,
                status=StreamExecutionStatus.RUNNING,
                force=True,
                owner_pid=os.getpid(),
                owner_host=socket.gethostname(),
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        else:
            raw_state = prov.read_state()
            raw_state.studies["ST_BATCH"].streams["form_c"].owner_pid = os.getpid()
            raw_state.studies["ST_BATCH"].streams["form_c"].owner_host = socket.gethostname()
            raw_state.studies["ST_BATCH"].streams["form_c"].lease_expires_at = datetime.now(
                timezone.utc
            ) + timedelta(minutes=10)
            orig_file_write(raw_state)

        counter.clear()
        with pytest.raises(InvalidStateTransitionError):
            with prov.batch_transaction("ST_BATCH", ["form_a", "form_c"]):
                pass
        assert len(counter) == 0
        assert prov.get_last_status("ST_BATCH", "form_a") == StreamExecutionStatus.FAILED

        # Expire form_c lease -> batch_transaction recovers stale lease & commits in 2 writes total
        if isinstance(prov, AirflowStateProvider):
            prov.set_last_timestamp(
                "ST_BATCH",
                "form_c",
                None,
                status=StreamExecutionStatus.RUNNING,
                force=True,
                owner_pid=1,
                owner_host="dead-worker",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
        else:
            raw_state = prov.read_state()
            raw_state.studies["ST_BATCH"].streams["form_c"].owner_pid = 1
            raw_state.studies["ST_BATCH"].streams["form_c"].owner_host = "dead-worker"
            raw_state.studies["ST_BATCH"].streams["form_c"].lease_expires_at = datetime.now(
                timezone.utc
            ) - timedelta(seconds=10)
            orig_file_write(raw_state)

        counter.clear()
        t_commit = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
        with prov.batch_transaction("ST_BATCH", ["form_a", "form_b", "form_c"]) as tx_map:
            assert len(counter) == 1
            tx_map["form_a"]["new_timestamp"] = t_commit
            tx_map["form_a"]["records_processed"] = 10
            tx_map["form_c"]["records_processed"] = 2
        assert len(counter) == 2
        assert prov.get_last_timestamp("ST_BATCH", "form_a") == t_commit
        assert prov.get_last_timestamp("ST_BATCH", "form_c") is None
        assert prov.get_last_status("ST_BATCH", "form_c") == StreamExecutionStatus.SUCCESS


def test_base_state_provider_default_batch_transaction_delegates() -> None:
    """Custom BaseStateProvider subclasses inherit working ExitStack batch_transaction default."""
    entered: list[str] = []

    class _MinimalSubclass(BaseStateProvider):
        def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
            return None

        def set_last_timestamp(
            self, study_key: str, stream_name: str, timestamp: Any, **kw: Any
        ) -> None:
            pass

        def transaction(  # type: ignore[override]
            self,
            study_key: str,
            stream_name: str,
            fallback_timestamp: datetime | None = None,
            *,
            lease_ttl_seconds: float | None = None,
        ) -> Generator[dict[str, Any], None, None]:
            import contextlib

            @contextlib.contextmanager
            def _cm() -> Generator[dict[str, Any], None, None]:
                entered.append(f"in:{study_key}:{stream_name}")
                yield {"last_timestamp": fallback_timestamp, "records_processed": 0}
                entered.append(f"out:{study_key}:{stream_name}")

            return _cm()

        def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
            return False

        def read_state(self) -> LedgerState:
            return LedgerState()

    sub = _MinimalSubclass()
    with sub.batch_transaction("S1", ["f1", "f2", "f1"]) as tx_map:
        assert set(tx_map) == {"f1", "f2"}
    assert entered == ["in:S1:f1", "in:S1:f2", "out:S1:f2", "out:S1:f1"]


def test_airflow_legacy_keys_migrate_cleanly_and_sdk_rpc_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify legacy key migration, 6-RPC read_state for 5x50 studies, and 2-RPC delete_entry."""
    store: dict[str, str] = {}
    ops: list[tuple[str, str]] = []

    class CountingSdkVariable:
        @staticmethod
        def get(key: str, default: Any = None, deserialize_json: bool = False) -> Any:
            ops.append(("get", key))
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
            ops.append(("set", key))
            store[key] = json.dumps(value) if serialize_json else str(value)

        @staticmethod
        def delete(key: str) -> None:
            ops.append(("delete", key))
            store.pop(key, None)

        @staticmethod
        def keys(prefix: str | None = None) -> list[str]:
            ops.append(("keys", prefix or ""))
            if prefix is None:
                return sorted(store.keys())
            return sorted(k for k in store if k.startswith(prefix))

    airflow_mod = ModuleType("airflow")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.Variable = CountingSdkVariable  # type: ignore[attr-defined]
    airflow_mod.sdk = sdk_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
    monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time", raising=False)
    monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time.task_runner", raising=False)

    provider = AirflowStateProvider()
    t1 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)

    # 1. Seed two legacy per-stream keys for LEGACY_ST and one sibling study
    legacy_k1 = provider._get_variable_key("LEGACY_ST", "sql_DM")
    legacy_k2 = provider._get_variable_key("LEGACY_ST", "sql_VS")
    sibling_legacy = provider._get_variable_key("LEGACY_ST_SIBLING", "sql_DM")
    store[legacy_k1] = json.dumps(
        {
            "study_key": "LEGACY_ST",
            "stream_name": "sql_DM",
            "last_timestamp": t1.isoformat(),
            "records_processed": 11,
            "status": "success",
        }
    )
    store[legacy_k2] = json.dumps(
        {
            "study_key": "LEGACY_ST",
            "stream_name": "sql_VS",
            "last_timestamp": t2.isoformat(),
            "records_processed": 22,
            "status": "success",
        }
    )
    store[sibling_legacy] = json.dumps(
        {
            "study_key": "LEGACY_ST_SIBLING",
            "stream_name": "sql_DM",
            "last_timestamp": t1.isoformat(),
            "records_processed": 5,
            "status": "success",
        }
    )

    assert provider.get_last_timestamp("LEGACY_ST", "sql_DM") == t1
    assert provider.get_last_timestamp("LEGACY_ST", "sql_VS") == t2
    pre_state = provider.read_state()
    assert set(pre_state.studies["LEGACY_ST"].streams) == {"sql_DM", "sql_VS"}

    # Next study write consolidates BOTH streams into imednet_state:LEGACY_ST and deletes legacy keys
    t3 = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)
    provider.set_last_timestamp("LEGACY_ST", "sql_DM", t3, records_processed=30)
    assert provider._get_variable_key("LEGACY_ST") in store
    assert legacy_k1 not in store
    assert legacy_k2 not in store
    assert sibling_legacy in store
    assert provider.get_last_timestamp("LEGACY_ST", "sql_DM") == t3
    assert provider.get_last_timestamp("LEGACY_ST", "sql_VS") == t2

    # 2. Seed 5 studies x 50 form streams (= 250 streams) and verify 6 RPCs in read_state
    store.clear()
    for s_idx in range(1, 6):
        study_name = f"STUDY_{s_idx}"
        with provider.batch_transaction(
            study_name, [f"sql_FORM_{f_idx:02d}" for f_idx in range(1, 51)]
        ) as tx_map:
            for f_idx in range(1, 51):
                tx_map[f"sql_FORM_{f_idx:02d}"]["new_timestamp"] = t3
                tx_map[f"sql_FORM_{f_idx:02d}"]["records_processed"] = f_idx

    ops.clear()
    full_state = provider.read_state()
    assert len(full_state.studies) == 5
    assert sum(len(s.streams) for s in full_state.studies.values()) == 250
    assert [op for op, _ in ops] == ["keys", "get", "get", "get", "get", "get"]
    assert len(ops) == 6

    # 3. Verify unforced delete_entry(study_key) checks active leases (keys + get + delete) & preserves sibling
    provider.set_last_timestamp("STUDY_1_OTHER", "sql_FORM_01", t3)
    ops.clear()
    assert provider.delete_entry("STUDY_1") is True
    assert ops == [
        ("keys", provider._get_variable_key("STUDY_1")),
        ("get", provider._get_variable_key("STUDY_1")),
        ("delete", provider._get_variable_key("STUDY_1")),
    ]
    assert provider.get_last_timestamp("STUDY_1_OTHER", "sql_FORM_01") == t3
    assert provider.delete_entry("STUDY_1") is False

    # Forced delete_entry(study_key, force=True) skips lease read (keys + delete)
    ops.clear()
    assert provider.delete_entry("STUDY_2", force=True) is True
    assert ops == [
        ("keys", provider._get_variable_key("STUDY_2")),
        ("delete", provider._get_variable_key("STUDY_2")),
    ]

    # Single-stream delete removing one stream, then the final stream
    provider.set_last_timestamp("SINGLE_DEL", "s1", t1)
    provider.set_last_timestamp("SINGLE_DEL", "s2", t2)
    assert provider.delete_entry("SINGLE_DEL", "missing") is False
    assert provider.delete_entry("SINGLE_DEL", "s1") is True
    assert provider.get_last_timestamp("SINGLE_DEL", "s1") is None
    assert provider.get_last_timestamp("SINGLE_DEL", "s2") == t2
    assert provider.delete_entry("SINGLE_DEL", "s2") is True
    assert provider._get_variable_key("SINGLE_DEL") not in store


def test_airflow_orm_mode_consolidated_migration_and_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify AirflowStateProvider ORM mode reads, legacy migration, deletes, and fallbacks."""
    import contextlib

    from sqlalchemy import Column, String, Text, create_engine, delete, select
    from sqlalchemy.orm import DeclarativeBase, configure_mappers, sessionmaker

    engine = create_engine("sqlite:///:memory:")

    class _Base(DeclarativeBase):
        pass

    class FakeModelVariable(_Base):
        __tablename__ = "variable"
        key = Column(String(250), primary_key=True)
        val = Column(Text, nullable=True)

        @classmethod
        def get(cls, key: str, default_var: Any = None, deserialize_json: bool = False) -> Any:
            with session_factory() as sess:
                row = sess.get(cls, key)
                if row is None or row.val is None:
                    return default_var
                return json.loads(row.val) if deserialize_json else row.val

        @classmethod
        def set(
            cls,
            key: str,
            value: Any,
            serialize_json: bool = False,
            session: Any = None,
        ) -> None:
            serialized = json.dumps(value) if serialize_json else str(value)
            if session is not None:
                existing = session.get(cls, key)
                if existing is None:
                    session.add(cls(key=key, val=serialized))
                else:
                    existing.val = serialized
                return
            with session_factory() as sess, sess.begin():
                existing = sess.get(cls, key)
                if existing is None:
                    sess.add(cls(key=key, val=serialized))
                else:
                    existing.val = serialized

        @classmethod
        def delete(cls, key: str) -> int:
            with session_factory() as sess, sess.begin():
                res = sess.execute(delete(cls).where(cls.key == key))
                return int(res.rowcount or 0)

    _Base.metadata.create_all(engine)
    configure_mappers()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    invalidated: list[str] = []

    @contextlib.contextmanager
    def fake_create_session() -> Generator[Any, None, None]:
        with session_factory() as sess, sess.begin():
            yield sess

    class FakeSecretCache:
        @staticmethod
        def invalidate_variable(key: str) -> None:
            invalidated.append(key)

    airflow_mod = ModuleType("airflow")
    settings_mod = ModuleType("airflow.settings")
    settings_mod.Session = None  # type: ignore[attr-defined]

    def _configure_orm() -> None:
        settings_mod.Session = session_factory  # type: ignore[attr-defined]

    settings_mod.configure_orm = _configure_orm  # type: ignore[attr-defined]
    airflow_mod.settings = settings_mod  # type: ignore[attr-defined]

    models_mod = ModuleType("airflow.models")
    models_var_mod = ModuleType("airflow.models.variable")
    models_var_mod.Variable = FakeModelVariable  # type: ignore[attr-defined]

    utils_mod = ModuleType("airflow.utils")
    utils_sess_mod = ModuleType("airflow.utils.session")
    utils_sess_mod.create_session = fake_create_session  # type: ignore[attr-defined]

    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.Variable = object()  # type: ignore[attr-defined]
    sdk_mod.SecretCache = FakeSecretCache  # type: ignore[attr-defined]
    exec_mod = ModuleType("airflow.sdk.execution_time")
    runner_mod = ModuleType("airflow.sdk.execution_time.task_runner")
    runner_mod.SUPERVISOR_COMMS = None  # type: ignore[attr-defined]

    for mod_name, mod_obj in (
        ("airflow", airflow_mod),
        ("airflow.settings", settings_mod),
        ("airflow.models", models_mod),
        ("airflow.models.variable", models_var_mod),
        ("airflow.utils", utils_mod),
        ("airflow.utils.session", utils_sess_mod),
        ("airflow.sdk", sdk_mod),
        ("airflow.sdk.execution_time", exec_mod),
        ("airflow.sdk.execution_time.task_runner", runner_mod),
    ):
        monkeypatch.setitem(sys.modules, mod_name, mod_obj)

    provider = AirflowStateProvider()
    assert provider._has_active_task_sdk_comms() is False

    t1 = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 2, 8, 0, tzinfo=timezone.utc)

    # Seed legacy per-stream ORM rows and verify fallback read + migration on write
    leg_1 = provider._get_variable_key("ORM_ST", "sql_DM")
    leg_2 = provider._get_variable_key("ORM_ST", "sql_VS")
    FakeModelVariable.set(
        leg_1,
        {
            "study_key": "ORM_ST",
            "stream_name": "sql_DM",
            "last_timestamp": t1.isoformat(),
            "records_processed": 4,
            "status": "success",
        },
        serialize_json=True,
    )
    FakeModelVariable.set(
        leg_2,
        {
            "study_key": "ORM_ST",
            "stream_name": "sql_VS",
            "last_timestamp": t2.isoformat(),
            "records_processed": 8,
            "status": "failed",
        },
        serialize_json=True,
    )
    assert provider._list_variable_keys("imednet_state:ORM_ST:") == [leg_1, leg_2]
    assert provider.get_last_timestamp("ORM_ST", "sql_DM") == t1
    assert str(provider.get_last_status("ORM_ST", "sql_VS")).lower().endswith("failed")

    orm_initial = provider.read_state()
    assert set(orm_initial.studies["ORM_ST"].streams) == {"sql_DM", "sql_VS"}

    # Migrate legacy rows to consolidated study row via batch_transaction
    t3 = datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc)
    with provider.batch_transaction("ORM_ST", ["sql_DM", "sql_AE"]) as tx_map:
        assert tx_map["sql_DM"]["last_timestamp"] == t1
        assert tx_map["sql_AE"]["last_timestamp"] is None
        tx_map["sql_DM"]["new_timestamp"] = t3
        tx_map["sql_DM"]["records_processed"] = 15
        tx_map["sql_AE"]["new_timestamp"] = t3
        tx_map["sql_AE"]["records_processed"] = 3

    assert set(invalidated) >= {leg_1, leg_2}
    with session_factory() as check_sess:
        remaining_keys = sorted(check_sess.scalars(select(FakeModelVariable.key)).all())
    assert remaining_keys == [provider._get_variable_key("ORM_ST")]
    assert provider._read_variable(provider._get_variable_key("ORM_ST")) is not None
    assert provider.get_last_timestamp("ORM_ST", "sql_VS") == t2
    assert provider.get_last_timestamp("ORM_ST", "sql_AE") == t3

    # Also test create_session fallback when settings.Session is absent
    settings_mod.Session = None  # type: ignore[attr-defined]
    settings_mod.configure_orm = None  # type: ignore[attr-defined]
    orm_after = provider.read_state()
    assert set(orm_after.studies["ORM_ST"].streams) == {"sql_DM", "sql_VS", "sql_AE"}

    # Remove single stream then full study via ORM delete_entry
    assert provider.delete_entry("ORM_ST", "sql_AE") is True
    assert provider.get_last_timestamp("ORM_ST", "sql_AE") is None
    assert provider.delete_entry("ORM_ST") is True
    assert provider.delete_entry("ORM_ST") is False
    provider._delete_listed_variable_key("nonexistent")

    # Flat-map fallback & helper edge cases
    assert provider._parse_study_variable_key("other_prefix:ST") is None
    assert provider._parse_study_variable_key("imednet_state:ST:STREAM") is None
    flat_study, flat_streams = provider._extract_study_streams(
        {"study_key": "FLAT", "s1": {"status": "success", "last_timestamp": t1.isoformat()}},
        "FALLBACK",
    )
    assert flat_study == "FLAT"
    assert "s1" in flat_streams
    assert provider._payload_to_stream_state({"records_processed": 1}) is None
    assert provider._deserialize_json_dict("{bad-json") is None

    # Airflow unavailable guard paths
    monkeypatch.setitem(sys.modules, "airflow", None)  # type: ignore[arg-type]
    assert provider._airflow_available() is False
    assert provider._has_active_task_sdk_comms() is False
    assert provider._read_variable("k") is None
    provider._write_variable("k", {})
    assert provider._list_variable_keys("p") == []
    provider._delete_listed_variable_key("k")
    assert provider._read_stream_entry("S", "f") is None
    assert provider._load_study_state("S") == ("S", {}, [])
    provider._save_study_state("S", {})
    assert provider.delete_entry("S") is False
    assert provider.read_state().studies == {}
    engine.dispose()


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

        # Run 4 (batch_transaction): hydrates, masks PHI, rolls back on failure, and commits cleanly
        t4 = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="batch boom"):
            with provider.batch_transaction(study, ["ae_sdtm", "vs_sdtm"]) as batch_fail:
                assert batch_fail["ae_sdtm"]["metadata"]["sdtm_seq_counters"]["AE:SUBJ-01"] == 3
                assert batch_fail["vs_sdtm"]["metadata"] == {}
                batch_fail["ae_sdtm"]["metadata"]["sdtm_seq_counters"]["AE:SUBJ-01"] = 777
                batch_fail["vs_sdtm"]["metadata"]["sdtm_seq_counters"] = {"VS:SUBJ-01": 5}
                raise RuntimeError("batch boom")

        batch_rolled_back = provider.read_state().studies[study].streams
        assert batch_rolled_back["ae_sdtm"].metadata["sdtm_seq_counters"]["AE:SUBJ-01"] == 3
        assert batch_rolled_back["vs_sdtm"].metadata == {}

        with provider.batch_transaction(study, ["ae_sdtm", "vs_sdtm"]) as batch_ok:
            assert batch_ok["ae_sdtm"]["metadata"]["sdtm_seq_counters"]["AE:SUBJ-01"] == 3
            batch_ok["ae_sdtm"]["metadata"]["sdtm_seq_counters"]["AE:SUBJ-01"] = 4
            batch_ok["vs_sdtm"]["metadata"]["sdtm_seq_counters"] = {"VS:SUBJ-01": 1}
            batch_ok["vs_sdtm"]["metadata"]["patientName"] = "John Doe"
            batch_ok["ae_sdtm"]["new_timestamp"] = t4
            batch_ok["vs_sdtm"]["new_timestamp"] = t4

        batch_committed = provider.read_state().studies[study].streams
        assert batch_committed["ae_sdtm"].metadata["sdtm_seq_counters"]["AE:SUBJ-01"] == 4
        assert batch_committed["vs_sdtm"].metadata["sdtm_seq_counters"] == {"VS:SUBJ-01": 1}
        assert batch_committed["vs_sdtm"].metadata["patientName"] == "***MASKED***"


def test_file_and_airflow_lease_protection_fencing_and_key_validation(
    tmp_path: Path,
    sdk_variable_store: dict[str, Any],
) -> None:
    """AC 7 & AC 8: Providers reject invalid keys, block unforced RUNNING mutations/deletes, and fence displaced commits/rollbacks."""
    from imednet.spi.utils import PathTraversalValidationError

    _ = sdk_variable_store
    file_provider = FileStateProvider(ledger_path=tmp_path / "fencing_ledger.json")
    airflow_provider = AirflowStateProvider()

    for provider in (file_provider, airflow_provider):
        # 1. Reject traversal and blank keys across CRUD and transaction APIs
        for bad_key in ("../evil", "   "):
            with pytest.raises(PathTraversalValidationError):
                provider.get_last_timestamp(bad_key, "records")
            with pytest.raises(PathTraversalValidationError):
                provider.get_last_timestamp("STUDY_OK", bad_key)
            with pytest.raises(PathTraversalValidationError):
                provider.set_last_timestamp(bad_key, "records", None)
            with pytest.raises(PathTraversalValidationError):
                provider.delete_entry(bad_key)
            with pytest.raises(PathTraversalValidationError):
                provider.delete_entry("STUDY_OK", bad_key)
            with pytest.raises(PathTraversalValidationError):
                with provider.transaction(bad_key, "records"):
                    pass
            with pytest.raises(PathTraversalValidationError):
                with provider.batch_transaction("STUDY_OK", [bad_key]):
                    pass

        # 2. Active RUNNING lease blocks unforced set_last_timestamp and delete_entry; force=True displaces and fences commit
        t_forced = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        t_stale = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
        with pytest.raises(InvalidStateTransitionError, match="displaced RUNNING lease"):
            with provider.transaction("ST_FENCE", "s1") as tx:
                tx["new_timestamp"] = t_stale
                with pytest.raises(InvalidStateTransitionError):
                    provider.set_last_timestamp("ST_FENCE", "s1", t_forced)
                with pytest.raises(InvalidStateTransitionError):
                    provider.delete_entry("ST_FENCE", "s1")
                with pytest.raises(InvalidStateTransitionError):
                    provider.delete_entry("ST_FENCE")

                # Operator forces a manual checkpoint while worker is still running
                provider.set_last_timestamp(
                    "ST_FENCE",
                    "s1",
                    t_forced,
                    records_processed=42,
                    force=True,
                )

        # Forced checkpoint remains intact (not overwritten by displaced commit)
        assert provider.get_last_timestamp("ST_FENCE", "s1") == t_forced
        assert provider.get_last_status("ST_FENCE", "s1") == StreamExecutionStatus.SUCCESS
        assert provider.read_state().studies["ST_FENCE"].streams["s1"].records_processed == 42

        # 3. Displaced transaction raising an exception does NOT clobber forced checkpoint on rollback
        t_forced_2 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="worker crash after displacement"):
            with provider.transaction("ST_FENCE", "s1"):
                provider.set_last_timestamp(
                    "ST_FENCE",
                    "s1",
                    t_forced_2,
                    records_processed=99,
                    force=True,
                )
                raise RuntimeError("worker crash after displacement")

        assert provider.get_last_timestamp("ST_FENCE", "s1") == t_forced_2
        assert provider.get_last_status("ST_FENCE", "s1") == StreamExecutionStatus.SUCCESS

        # 4. Forced delete_entry inside batch_transaction fences commit without resurrecting deleted streams
        with pytest.raises(InvalidStateTransitionError, match="displaced RUNNING lease"):
            with provider.batch_transaction("ST_BATCH_FENCE", ["a", "b"]) as btx:
                btx["a"]["new_timestamp"] = t_stale
                btx["b"]["new_timestamp"] = t_stale
                with pytest.raises(InvalidStateTransitionError):
                    provider.delete_entry("ST_BATCH_FENCE")
                assert provider.delete_entry("ST_BATCH_FENCE", force=True) is True

        assert "ST_BATCH_FENCE" not in provider.read_state().studies
