"""Unit tests for duckdb ingestion workflow."""

from __future__ import annotations

import sys
from builtins import __import__ as builtin_import
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from imednet.models.record_revisions import RecordRevision
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


def test_duckdb_workflow_uses_single_worker_thread() -> None:
    """Verify DuckDBIngestionWorkflow pins threads=1 to avoid cgroup v2 WaitForTask cast errors."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    threads_setting = workflow._connection.execute("SELECT current_setting('threads')").fetchone()[
        0
    ]
    assert int(threads_setting) == 1
