"""Unit tests for duckdb ingestion workflow."""

from __future__ import annotations

import sys
from builtins import __import__ as builtin_import
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from imednet.models.record_revisions import RecordRevision
from imednet.models.records import RecordStatus
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow


def test_ingest_revisions_append_mode() -> None:
    """Test that ingest revisions append mode."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    first = [
        RecordRevision(
            record_revision_id=1,
            record_id=1,
            form_key="DEMO",
            record_revision=1,
            user="alice",
            reason_for_change="Initial",
            date_created="2024-01-01T10:00:00Z",
        )
    ]
    second = [
        RecordRevision(
            record_revision_id=2,
            record_id=2,
            form_key="DEMO",
            record_revision=1,
            user="bob",
            reason_for_change="Initial",
            date_created="2024-01-02T10:00:00Z",
        )
    ]

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", side_effect=[first, second]):
        workflow.ingest_revisions("STUDY", mode="append")
        workflow.ingest_revisions("STUDY", mode="append")

    count = workflow._connection.execute("SELECT COUNT(*) FROM bronze_revisions").fetchone()[0]
    assert count == 2


def test_ingest_revisions_replace_mode() -> None:
    """Test that ingest revisions replace mode."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    old_row = [
        RecordRevision(
            record_revision_id=1,
            record_id=1,
            form_key="DEMO",
            record_revision=1,
            user="alice",
            date_created="2024-01-01T10:00:00Z",
        )
    ]
    new_row = [
        RecordRevision(
            record_revision_id=9,
            record_id=9,
            form_key="VITALS",
            record_revision=1,
            user="bob",
            date_created="2024-01-02T10:00:00Z",
        )
    ]

    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        side_effect=[old_row, new_row],
    ):
        workflow.ingest_revisions("STUDY", mode="append")
        workflow.ingest_revisions("STUDY", mode="replace")

    rows = workflow._connection.execute(
        "SELECT record_id, form_key FROM bronze_revisions ORDER BY record_id"
    ).fetchall()
    assert rows == [(9, "VITALS")]


def test_build_silver_view_deduplication() -> None:
    """Test that build_silver_view deduplicates by record_id keeping highest record_revision."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    revisions = [
        RecordRevision(
            record_revision_id=701,
            record_id=7,
            form_key="VITALS",
            record_revision=1,
            user="alice",
            reason_for_change="Initial",
            date_created="2024-01-01T10:00:00Z",
        ),
        RecordRevision(
            record_revision_id=702,
            record_id=7,
            form_key="VITALS",
            record_revision=2,
            user="bob",
            reason_for_change="Updated height",
            date_created="2024-01-02T10:00:00Z",
        ),
    ]

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions):
        workflow.ingest_revisions("STUDY", mode="append")

    workflow.build_silver_view("STUDY")
    silver_rows = workflow._connection.execute(
        'SELECT record_id, form_key, record_revision, "user", reason_for_change, date_created '
        "FROM silver_current_state"
    ).fetchall()

    assert silver_rows == [
        (7, "VITALS", 2, "bob", "Updated height", datetime(2024, 1, 2, 10, 0, 0))
    ]


def test_ingest_revisions_returns_row_count() -> None:
    """Test that ingest revisions returns row count."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    revisions = [
        RecordRevision(
            record_revision_id=1,
            record_id=1,
            form_key="F1",
            record_revision=1,
            user="alice",
            date_created="2024-01-01T10:00:00Z",
        ),
        RecordRevision(
            record_revision_id=2,
            record_id=2,
            form_key="F1",
            record_revision=1,
            user="bob",
            date_created="2024-01-01T11:00:00Z",
        ),
        RecordRevision(
            record_revision_id=3,
            record_id=3,
            form_key="F1",
            record_revision=1,
            user="carol",
            date_created="2024-01-01T12:00:00Z",
        ),
    ]

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions):
        assert workflow.ingest_revisions("STUDY", mode="append") == 3


def test_duckdb_workflow_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that duckdb workflow import error."""

    def fake_import(
        name: str,
        globals_arg: Any = None,
        locals_arg: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        """Helper function to fake import."""
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals_arg, locals_arg, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(ImportError, match=r"pip install"):
        DuckDBIngestionWorkflow(MagicMock(), ":memory:")


def test_ingest_revisions_bulk_staging_calls_and_empty_batch() -> None:
    """Test bulk ingestion registers/unregisters once per non-empty batch and skips empty."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    real_conn = workflow._connection
    spy_conn = MagicMock(wraps=real_conn)
    workflow._connection = spy_conn

    revisions = [
        RecordRevision(
            record_revision_id=1,
            record_id=1,
            form_key="F1",
            record_revision=1,
            user="alice",
            date_created="2024-01-01T10:00:00Z",
        ),
        RecordRevision(
            record_revision_id=2,
            record_id=2,
            form_key="F1",
            record_revision=1,
            user="bob",
            date_created="2024-01-01T11:00:00Z",
        ),
    ]

    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        side_effect=[[], revisions],
    ):
        assert workflow.ingest_revisions("STUDY", mode="replace") == 0
        spy_conn.register.assert_not_called()
        spy_conn.unregister.assert_not_called()

        assert workflow.ingest_revisions("STUDY", mode="append") == 2
        spy_conn.register.assert_called_once()
        spy_conn.unregister.assert_called_once()
        spy_conn.executemany.assert_not_called()


def test_ingest_revisions_bigint_aware_timestamps_and_pandas_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 64-bit BIGINTs, aware datetimes, RecordStatus enums, and pandas fallback."""
    pytest.importorskip("duckdb")
    import pandas as pd

    def fake_import(
        name: str,
        globals_arg: Any = None,
        locals_arg: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("No module named pyarrow")
        return builtin_import(name, globals_arg, locals_arg, fromlist, level)

    monkeypatch.delitem(sys.modules, "pyarrow", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    big_id = (1 << 54) + 99
    revisions = [
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=big_id,
            record_id=big_id + 1,
            record_oid="OID-BIG",
            form_key="VITALS",
            subject_key="SUBJ-9",
            site_id=big_id + 2,
            interval_id=big_id + 3,
            record_status=RecordStatus.LOCKED,
            record_revision=3,
            data_revision=2,
            user="alice",
            role="CRC",
            reason_for_change="Bulk insert check",
            deleted=False,
            date_created=datetime(2024, 3, 15, 15, 30, 0, tzinfo=timezone(timedelta(hours=3))),
            last_updated="   ",
        )
    ]

    rows = list(workflow._to_rows("STUDY", revisions))
    staging_df = workflow._build_staging_relation(rows)
    assert isinstance(staging_df, pd.DataFrame)

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions):
        assert workflow.ingest_revisions("STUDY", mode="replace") == 1

    result = workflow._connection.execute(
        "SELECT record_revision_id, record_id, site_id, interval_id, "
        "record_status, date_created, last_updated FROM bronze_revisions"
    ).fetchone()
    assert result == (
        big_id,
        big_id + 1,
        big_id + 2,
        big_id + 3,
        "Locked",
        datetime(2024, 3, 15, 12, 30, 0),
        None,
    )


def test_duckdb_cst_timestamps_upsert_study_scoped_replace_and_silver_views() -> None:
    """Verify CST string UTC normalization, keyed upserts, Study-scoped replace, and SCD2 views."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    study_a_batch_1 = [
        SimpleNamespace(
            record_revision_id=101,
            record_id=10,
            record_oid="OID-10",
            form_key="DEMO",
            subject_key="S1",
            site_id=1,
            interval_id=1,
            record_status="Record Incomplete",
            record_revision=1,
            data_revision=1,
            user="alice",
            role="CRC",
            reason_for_change="Initial",
            deleted=False,
            date_created="2024-05-01T22:30:00-06:00",
            last_updated="2024-05-01T22:30:00-06:00",
        ),
        # Duplicate record_revision_id within the same batch updates cleanly
        SimpleNamespace(
            record_revision_id=101,
            record_id=10,
            record_oid="OID-10",
            form_key="DEMO",
            subject_key="S1",
            site_id=1,
            interval_id=1,
            record_status="Record Complete",
            record_revision=1,
            data_revision=1,
            user="alice",
            role="CRC",
            reason_for_change="Initial corrected",
            deleted=False,
            date_created="2024-05-01T22:30:00-06:00",
            last_updated="2024-05-01T22:35:00-06:00",
        ),
        SimpleNamespace(
            record_revision_id=102,
            record_id=10,
            record_oid="OID-10",
            form_key="DEMO",
            subject_key="S1",
            site_id=1,
            interval_id=1,
            record_status="Verified",
            record_revision=2,
            data_revision=2,
            user="bob",
            role="DM",
            reason_for_change="Verified",
            deleted=False,
            date_created="2024-05-02T10:00:00Z",
            last_updated="2024-05-02T10:00:00Z",
        ),
        # Soft-deleted record (rev 1 active -> rev 2 deleted) must be omitted from current_state
        SimpleNamespace(
            record_revision_id=201,
            record_id=20,
            record_oid="OID-20",
            form_key="AE",
            subject_key="S1",
            site_id=1,
            interval_id=1,
            record_status="Record Complete",
            record_revision=1,
            data_revision=1,
            user="alice",
            role="CRC",
            reason_for_change="Created AE",
            deleted=False,
            date_created="2024-05-01T12:00:00Z",
            last_updated="2024-05-01T12:00:00Z",
        ),
        SimpleNamespace(
            record_revision_id=202,
            record_id=20,
            record_oid="OID-20",
            form_key="AE",
            subject_key="S1",
            site_id=1,
            interval_id=1,
            record_status="Deleted",
            record_revision=2,
            data_revision=1,
            user="bob",
            role="DM",
            reason_for_change="Entered in error",
            deleted=True,
            date_created="2024-05-03T12:00:00Z",
            last_updated="2024-05-03T12:00:00Z",
        ),
        # Null record_id records partitioned distinctly by record_oid
        SimpleNamespace(
            record_revision_id=301,
            record_id=None,
            record_oid="OID-NULL-1",
            form_key="CM",
            subject_key="S1",
            site_id=1,
            interval_id=1,
            record_status="Record Complete",
            record_revision=1,
            data_revision=1,
            user="alice",
            role="CRC",
            reason_for_change="CM 1",
            deleted=False,
            date_created="2024-05-01T13:00:00Z",
            last_updated="2024-05-01T13:00:00Z",
        ),
        SimpleNamespace(
            record_revision_id=302,
            record_id=None,
            record_oid="OID-NULL-2",
            form_key="CM",
            subject_key="S1",
            site_id=1,
            interval_id=1,
            record_status="Record Complete",
            record_revision=1,
            data_revision=1,
            user="alice",
            role="CRC",
            reason_for_change="CM 2",
            deleted=False,
            date_created="2024-05-01T14:00:00Z",
            last_updated="2024-05-01T14:00:00Z",
        ),
    ]

    study_b_batch = [
        SimpleNamespace(
            record_revision_id=101,
            record_id=10,
            record_oid="OID-B-10",
            form_key="DEMO",
            subject_key="SB1",
            site_id=2,
            interval_id=1,
            record_status="Locked",
            record_revision=1,
            data_revision=1,
            user="carol",
            role="PI",
            reason_for_change="Initial B",
            deleted=False,
            date_created="2024-06-01T08:00:00Z",
            last_updated="2024-06-01T08:00:00Z",
        )
    ]

    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        side_effect=[study_a_batch_1, study_a_batch_1, study_b_batch],
    ):
        assert workflow.ingest_revisions("STUDY_A", mode="append") == 6
        # Re-appending identical revisions upserts without duplicating rows
        assert workflow.ingest_revisions("STUDY_A", mode="append") == 6
        assert workflow.ingest_revisions("STUDY_B", mode="append") == 1

    # CST -06:00 string is stored as naive UTC (2024-05-02 04:30:00)
    cst_row = workflow._connection.execute(
        "SELECT date_created, reason_for_change FROM bronze_revisions "
        "WHERE study_key = 'STUDY_A' AND record_revision_id = 101"
    ).fetchone()
    assert cst_row == (datetime(2024, 5, 2, 4, 30, 0), "Initial corrected")

    total_rows = workflow._connection.execute("SELECT COUNT(*) FROM bronze_revisions").fetchone()[0]
    assert total_rows == 7

    # Unfiltered build_silver_view partitions by study_key + COALESCE(record_id, record_oid)
    workflow.build_silver_view()
    current_rows = workflow._connection.execute(
        "SELECT study_key, record_oid, record_revision FROM silver_current_state "
        "ORDER BY study_key, record_oid"
    ).fetchall()
    assert current_rows == [
        ("STUDY_A", "OID-10", 2),
        ("STUDY_A", "OID-NULL-1", 1),
        ("STUDY_A", "OID-NULL-2", 1),
        ("STUDY_B", "OID-B-10", 1),
    ]

    # SCD Type 2 history view exposes valid_from, valid_to, and is_current
    history_rows = workflow._connection.execute(
        "SELECT record_revision, valid_from, valid_to, is_current "
        "FROM silver_records_history "
        "WHERE study_key = 'STUDY_A' AND record_id = 10 "
        "ORDER BY record_revision"
    ).fetchall()
    assert history_rows == [
        (1, datetime(2024, 5, 2, 4, 30, 0), datetime(2024, 5, 2, 10, 0, 0), False),
        (2, datetime(2024, 5, 2, 10, 0, 0), None, True),
    ]

    # Replacing STUDY_A preserves STUDY_B rows in bronze_revisions
    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=[]):
        assert workflow.ingest_revisions("STUDY_A", mode="replace") == 0

    remaining = workflow._connection.execute(
        "SELECT study_key, record_oid FROM bronze_revisions"
    ).fetchall()
    assert remaining == [("STUDY_B", "OID-B-10")]
