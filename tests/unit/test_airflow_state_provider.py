"""Unit tests for Airflow incremental export checkpointing via file-backed state ledgers."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet.spi import sink_base
from imednet_sinks.document import MongoDbSinkConfig
from imednet_workflows import state_ledger
from imednet_workflows.state_ledger import (
    ExtractionStateLedger,
    FileStateProvider,
    StreamExecutionStatus,
)


class _RecordingSink(sink_base.ExportSink):
    """Minimal sink recording written batches for Airflow operator checkpoint tests."""

    def __init__(self, config: sink_base.SinkConfig, written: list[int]) -> None:
        super().__init__(config)
        self._written = written

    def write_batch(self, records: Any, *, batch_id: str) -> int:
        _ = batch_id
        items = list(records)
        self._written.extend(r.record_id for r in items)
        return len(items)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


def _make_record(record_id: int, date_modified: str) -> Record:
    return Record(
        study_key="STUDY-AF",
        record_id=record_id,
        subject_key="SUBJ-01",
        visit_id=1,
        form_id=10,
        form_key="AE",
        date_modified=date_modified,
        deleted=False,
        record_data={"AE_TERM": "Headache"},
    )


def test_airflow_state_provider_removed_from_workflows_and_entrypoints() -> None:
    """Ensure the speculative XCom state provider is removed from workflows and SPI entry points."""
    assert not hasattr(state_ledger, "AirflowStateProvider")
    provider_names = {
        ep.name for ep in entry_points(group=sink_base.STATE_PROVIDER_ENTRY_POINT_GROUP)
    }
    assert provider_names == {"default", "file"}

    with pytest.raises(ImportError, match="airflow"):
        sink_base.get_state_provider(provider_name="airflow")


def test_airflow_export_operator_incremental_uses_file_state_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ImednetExportOperator(incremental=True) persists checkpoints via FileStateProvider."""
    ledger_path = tmp_path / "shared_ledger.json"
    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "export_study")
    monkeypatch.setenv("USE_AIRFLOW_STATE_PROVIDER", "1")
    monkeypatch.setenv("IMEDNET_WORKFLOW_LEDGER_PATH", str(ledger_path))

    written_ids: list[int] = []
    seen_filters: list[dict[str, Any]] = []

    def _list_records(**kwargs: Any) -> list[Record]:
        seen_filters.append(kwargs)
        return [_make_record(101, "2026-04-01T09:30:00Z")]

    sdk = MagicMock()
    sdk.records.list.side_effect = _list_records
    sdk.sinks = SimpleNamespace(
        MongoDbExportSink=lambda config: _RecordingSink(config, written_ids),
        MongoDbSinkConfig=MongoDbSinkConfig,
    )

    op = ImednetExportOperator(
        task_id="export_study",
        study_key="STUDY-AF",
        destination="mongodb",
        incremental=True,
        stream_name="mongo_records",
        export_kwargs={
            "uri": "mongodb://localhost:27017",
            "database": "clinical",
            "collection": "records",
        },
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: sdk)
    op.execute({})

    assert written_ids == [101]
    assert "dateModified" not in seen_filters[0]

    ledger = ExtractionStateLedger(ledger_path)
    assert isinstance(ledger, FileStateProvider)
    assert ledger.get_last_timestamp("STUDY-AF", "mongo_records") == datetime(
        2026, 4, 1, 9, 30, tzinfo=UTC
    )
    assert ledger.get_last_status("STUDY-AF", "mongo_records") == StreamExecutionStatus.SUCCESS


def test_airflow_export_operator_tabular_incremental_uses_file_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tabular incremental exports in ImednetExportOperator default to the file-backed ledger."""
    ledger_path = tmp_path / "tabular_ledger.json"
    output_json = tmp_path / "records.json"
    monkeypatch.setenv("AIRFLOW_CTX_TASK_ID", "export_json_task")
    monkeypatch.setenv("IMEDNET_WORKFLOW_LEDGER_PATH", str(ledger_path))

    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(
            variable_name="AE_TERM",
            label="AE Term",
            variable_type="text",
            form_id=10,
            form_key="AE",
        )
    ]
    sdk.records.list.return_value = [_make_record(202, "2026-04-02T15:45:00Z")]

    op = ImednetExportOperator(
        task_id="export_json_task",
        study_key="STUDY-AF",
        destination="json",
        output_path=str(output_json),
        incremental=True,
        stream_name="json_stream",
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: sdk)
    result_path = op.execute({})

    assert result_path == str(output_json)
    assert output_json.exists()
    ledger = ExtractionStateLedger(ledger_path)
    assert ledger.get_last_timestamp("STUDY-AF", "json_stream") == datetime(
        2026, 4, 2, 15, 45, tzinfo=UTC
    )
