"""Unit tests for state provider resolution and FileStateProvider contracts."""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from imednet_workflows.state_ledger import (
    DEFAULT_LEDGER_PATH,
    BaseStateProvider,
    ExtractionStateLedger,
    FileStateProvider,
    InvalidStateTransitionError,
    LedgerState,
    StreamExecutionStatus,
    get_state_provider,
)


class _MinimalProvider(BaseStateProvider):
    """Minimal concrete BaseStateProvider exercising default base hook behavior."""

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        _ = (study_key, stream_name)
        return None

    def set_last_timestamp(
        self,
        study_key: str,
        stream_name: str,
        timestamp: datetime,
        records_processed: int = 0,
        status: StreamExecutionStatus | str = StreamExecutionStatus.SUCCESS,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        _ = (
            study_key,
            stream_name,
            timestamp,
            records_processed,
            status,
            error_message,
            metadata,
            force,
        )

    @contextlib.contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        _ = (study_key, stream_name)
        yield {"last_timestamp": fallback_timestamp}

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        _ = (study_key, stream_name)
        return False

    def read_state(self) -> LedgerState:
        return LedgerState()


def test_get_state_provider_ignores_airflow_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_state_provider always returns FileStateProvider regardless of Airflow env vars."""
    env_ledger = tmp_path / "env_ledger.json"
    explicit_ledger = tmp_path / "explicit_ledger.json"

    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "task_1")
    monkeypatch.setenv("AIRFLOW_CTX_DAG_ID", "dag_1")
    monkeypatch.setenv("USE_AIRFLOW_STATE_PROVIDER", "1")
    monkeypatch.delenv("IMEDNET_WORKFLOW_LEDGER_PATH", raising=False)

    default_provider = get_state_provider()
    assert isinstance(default_provider, FileStateProvider)
    assert default_provider.ledger_path == Path(DEFAULT_LEDGER_PATH)

    monkeypatch.setenv("IMEDNET_WORKFLOW_LEDGER_PATH", str(env_ledger))
    env_provider = get_state_provider()
    assert isinstance(env_provider, FileStateProvider)
    assert env_provider.ledger_path == env_ledger

    explicit_provider = get_state_provider(explicit_ledger)
    assert isinstance(explicit_provider, FileStateProvider)
    assert explicit_provider.ledger_path == explicit_ledger


def test_base_state_provider_default_last_status_is_none() -> None:
    """BaseStateProvider.get_last_status defaults to None when not overridden."""
    provider = _MinimalProvider()
    assert provider.get_last_status("ST1", "records") is None


def test_file_state_provider_status_timestamp_and_delete_contracts(tmp_path: Path) -> None:
    """FileStateProvider persists UTC timestamps, statuses, deletes, and state snapshots."""
    ledger = ExtractionStateLedger(tmp_path / "provider_ledger.json")
    assert ledger.get_last_timestamp("ST1", "recs") is None
    assert ledger.get_last_status("ST1", "recs") is None
    assert ledger.delete_entry("ST1") is False

    naive_ts = datetime(2026, 5, 22, 12, 0, 0)
    ledger.set_last_timestamp("ST1", "recs", naive_ts, records_processed=5, metadata={"run": 1})

    ts = ledger.get_last_timestamp("ST1", "recs")
    assert ts == datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert ledger.get_last_status("ST1", "recs") == StreamExecutionStatus.SUCCESS

    state = ledger.read_state()
    assert state.studies["ST1"].streams["recs"].records_processed == 5
    assert state.studies["ST1"].streams["recs"].metadata == {"run": 1}

    assert ledger.delete_entry("ST1", "missing_stream") is False
    assert ledger.delete_entry("ST1", "recs") is True
    assert ledger.get_last_timestamp("ST1", "recs") is None
    assert ledger.delete_entry("ST1") is True


def test_file_state_provider_transaction_guard_and_rollback(tmp_path: Path) -> None:
    """FileStateProvider enforces RUNNING guards and preserves prior watermarks on failure."""
    ledger = FileStateProvider(tmp_path / "tx_ledger.json")
    initial_ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    next_ts = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)

    ledger.set_last_timestamp("ST1", "recs", initial_ts, status=StreamExecutionStatus.RUNNING)
    with (
        pytest.raises(
            InvalidStateTransitionError,
            match="Cannot transition stream state from 'running' to 'running'",
        ),
        ledger.transaction("ST1", "recs"),
    ):
        pass

    ledger.set_last_timestamp("ST1", "recs", initial_ts, status=StreamExecutionStatus.FAILED)
    with ledger.transaction("ST1", "recs") as tx:
        assert tx["last_timestamp"] == initial_ts
        tx["new_timestamp"] = next_ts
        tx["records_processed"] = 12

    assert ledger.get_last_timestamp("ST1", "recs") == next_ts
    assert ledger.get_last_status("ST1", "recs") == StreamExecutionStatus.SUCCESS

    partial_ts = datetime(2026, 5, 24, 8, 0, 0, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="worker crash"):
        with ledger.transaction("ST1", "recs") as tx:
            tx["new_timestamp"] = partial_ts
            tx["records_processed"] = 99
            raise RuntimeError("worker crash")

    assert ledger.get_last_timestamp("ST1", "recs") == next_ts
    assert ledger.get_last_status("ST1", "recs") == StreamExecutionStatus.FAILED
    stream = ledger.read_state().studies["ST1"].streams["recs"]
    assert stream.records_processed == 99
    assert stream.error_message == "worker crash"

    with pytest.raises(ValueError, match="Invalid stream execution status"):
        ledger.set_last_timestamp("ST1", "recs", next_ts, status="unsupported_status")
