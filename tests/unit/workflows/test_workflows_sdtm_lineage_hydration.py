"""Unit tests for _lineage_hydrated transaction markers in FileStateProvider and _airflow_cas."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    FileStateProvider,
    StreamExecutionStatus,
)


def test_file_state_provider_transaction_and_batch_transaction_hydration_marker(
    tmp_path: Path,
) -> None:
    """FileStateProvider sets top-level _lineage_hydrated=True without persisting transient keys."""
    ledger_path = tmp_path / "wf_hydration.json"
    provider = FileStateProvider(ledger_path)
    t1 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

    with provider.transaction("STUDY_WF", "csv") as tx:
        assert tx.get("_lineage_hydrated") is True
        assert "_lineage_hydrated" not in tx["metadata"]
        tx["_sdtm_lineage_session"] = {"transient": object()}
        tx["new_timestamp"] = t1
        tx["records_processed"] = 3
        tx["metadata"] = {
            "sdtm_lineage": {
                "1": {
                    "study_id": "STUDY_WF",
                    "domain": "AE",
                    "usubjid": "STUDY_WF-01",
                    "seq_col": "AESEQ",
                    "seq_val": 1,
                }
            },
            "sdtm_seq_watermarks": {"AE:STUDY_WF-01": 1},
        }

    disk_payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    csv_entry = disk_payload["studies"]["STUDY_WF"]["streams"]["csv"]
    assert "_lineage_hydrated" not in csv_entry
    assert "_sdtm_lineage_session" not in csv_entry
    assert "_lineage_hydrated" not in csv_entry["metadata"]
    assert csv_entry["metadata"]["sdtm_seq_watermarks"] == {"AE:STUDY_WF-01": 1}

    # Re-open via batch_transaction and verify prior metadata is hydrated on top-level tx_data
    with provider.batch_transaction("STUDY_WF", ["csv", "sql_F01"]) as btx:
        assert btx["csv"].get("_lineage_hydrated") is True
        assert btx["sql_F01"].get("_lineage_hydrated") is True
        assert "_lineage_hydrated" not in btx["csv"]["metadata"]
        assert btx["csv"]["metadata"]["sdtm_seq_watermarks"] == {"AE:STUDY_WF-01": 1}
        btx["csv"]["_sdtm_lineage_session"] = {"transient": object()}
        btx["sql_F01"]["new_timestamp"] = t2
        btx["sql_F01"]["records_processed"] = 2
        btx["sql_F01"]["metadata"] = {"sdtm_seq_watermarks": {"VS:STUDY_WF-01": 2}}

    disk_payload_after = json.loads(ledger_path.read_text(encoding="utf-8"))
    for s_name in ("csv", "sql_F01"):
        s_entry = disk_payload_after["studies"]["STUDY_WF"]["streams"][s_name]
        assert "_lineage_hydrated" not in s_entry
        assert "_sdtm_lineage_session" not in s_entry
        assert "_lineage_hydrated" not in s_entry["metadata"]

    # Failed batch_transaction rolls back both watermarks and metadata
    with pytest.raises(RuntimeError, match="abort batch"):
        with provider.batch_transaction("STUDY_WF", ["csv", "sql_F01"]) as btx_fail:
            btx_fail["csv"]["new_timestamp"] = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
            btx_fail["csv"]["metadata"]["sdtm_seq_watermarks"] = {"AE:STUDY_WF-01": 999}
            raise RuntimeError("abort batch")

    assert provider.get_last_timestamp("STUDY_WF", "csv") == t1
    assert provider.get_sdtm_lineage("STUDY_WF", "csv")["sdtm_seq_watermarks"] == {
        "AE:STUDY_WF-01": 1
    }
    assert provider.get_last_status("STUDY_WF", "csv") == StreamExecutionStatus.FAILED


def test_airflow_cas_prepare_running_streams_sets_top_level_lineage_hydrated() -> None:
    """AirflowStateProvider._transact_streams sets _lineage_hydrated=True outside tx_data['metadata']."""
    store: dict[str, dict[str, Any]] = {
        "imednet_cas_test:STUDY_CAS": {
            "study_key": "STUDY_CAS",
            "streams": {
                "airflow_csv": {
                    "study_key": "STUDY_CAS",
                    "stream_name": "airflow_csv",
                    "last_timestamp": "2026-10-01T00:00:00+00:00",
                    "status": "success",
                    "records_processed": 10,
                    "metadata": {
                        "sdtm_lineage": {
                            "42": {
                                "study_id": "STUDY_CAS",
                                "domain": "AE",
                                "usubjid": "STUDY_CAS-01",
                                "seq_col": "AESEQ",
                                "seq_val": 4,
                            }
                        },
                        "sdtm_seq_watermarks": {"AE:STUDY_CAS-01": 4},
                    },
                }
            },
        }
    }

    def _read_var(key: str) -> dict[str, Any] | None:
        raw = store.get(key)
        return json.loads(json.dumps(raw)) if raw is not None else None

    def _write_var(key: str, payload: dict[str, Any]) -> None:
        store[key] = json.loads(json.dumps(payload))

    provider = AirflowStateProvider(variable_prefix="imednet_cas_test")
    with (
        patch.object(provider, "_airflow_available", return_value=True),
        patch.object(provider, "_has_active_task_sdk_comms", return_value=True),
        patch.object(provider, "_read_variable", side_effect=_read_var),
        patch.object(provider, "_write_variable", side_effect=_write_var),
    ):
        with provider.batch_transaction("STUDY_CAS", ["airflow_csv", "new_stream"]) as btx:
            assert btx["airflow_csv"]["_lineage_hydrated"] is True
            assert btx["new_stream"]["_lineage_hydrated"] is True
            assert "_lineage_hydrated" not in btx["airflow_csv"]["metadata"]
            assert "_lineage_hydrated" not in btx["new_stream"]["metadata"]
            assert btx["airflow_csv"]["metadata"]["sdtm_seq_watermarks"] == {"AE:STUDY_CAS-01": 4}
            btx["airflow_csv"]["_sdtm_lineage_session"] = {"transient": object()}
            btx["new_stream"]["new_timestamp"] = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)

    persisted = store["imednet_cas_test:STUDY_CAS"]
    for s_name in ("airflow_csv", "new_stream"):
        s_entry = persisted["streams"][s_name]
        assert "_lineage_hydrated" not in s_entry
        assert "_sdtm_lineage_session" not in s_entry
        assert "_lineage_hydrated" not in s_entry.get("metadata", {})
