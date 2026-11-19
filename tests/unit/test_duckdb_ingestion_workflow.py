"""Unit tests for duckdb ingestion workflow."""

from __future__ import annotations

import sys
from builtins import __import__ as builtin_import
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow


def test_ingest_revisions_append_mode() -> None:
    """Test that ingest revisions append mode."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    first = [
        SimpleNamespace(
            record_id=1,
            form_id=10,
            variable_name="AGE",
            value="30",
            revision_number=1,
            date_modified=datetime(2024, 1, 1, 10, 0, 0),
            modified_by="alice",
        )
    ]
    second = [
        SimpleNamespace(
            record_id=2,
            form_id=10,
            variable_name="AGE",
            value="31",
            revision_number=1,
            date_modified=datetime(2024, 1, 2, 10, 0, 0),
            modified_by="bob",
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
        SimpleNamespace(
            record_id=1,
            form_id=10,
            variable_name="AGE",
            value="30",
            revision_number=1,
            date_modified=datetime(2024, 1, 1, 10, 0, 0),
            modified_by="alice",
        )
    ]
    new_row = [
        SimpleNamespace(
            record_id=9,
            form_id=99,
            variable_name="WEIGHT",
            value="80",
            revision_number=1,
            date_modified=datetime(2024, 1, 2, 10, 0, 0),
            modified_by="bob",
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
        "SELECT record_id, variable_name FROM bronze_revisions ORDER BY record_id"
    ).fetchall()
    assert rows == [(9, "WEIGHT")]


def test_build_silver_view_deduplication() -> None:
    """Test that build silver view deduplication."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    revisions = [
        SimpleNamespace(
            record_id=7,
            form_id=3,
            variable_name="HEIGHT",
            value="175",
            revision_number=1,
            date_modified=datetime(2024, 1, 1, 10, 0, 0),
            modified_by="alice",
        ),
        SimpleNamespace(
            record_id=7,
            form_id=3,
            variable_name="HEIGHT",
            value="176",
            revision_number=2,
            date_modified=datetime(2024, 1, 2, 10, 0, 0),
            modified_by="bob",
        ),
    ]

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions):
        workflow.ingest_revisions("STUDY", mode="append")

    workflow.build_silver_view("STUDY")
    silver_rows = workflow._connection.execute(
        "SELECT record_id, variable_name, value, revision_number FROM silver_current_state"
    ).fetchall()

    assert silver_rows == [(7, "HEIGHT", "176", 2)]


def test_ingest_revisions_returns_row_count() -> None:
    """Test that ingest revisions returns row count."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    revisions = [
        SimpleNamespace(
            record_id=1,
            form_id=1,
            variable_name="A",
            value="x",
            revision_number=1,
            date_modified=datetime(2024, 1, 1, 10, 0, 0),
            modified_by="alice",
        ),
        SimpleNamespace(
            record_id=2,
            form_id=1,
            variable_name="B",
            value="y",
            revision_number=1,
            date_modified=datetime(2024, 1, 1, 11, 0, 0),
            modified_by="bob",
        ),
        SimpleNamespace(
            record_id=3,
            form_id=1,
            variable_name="C",
            value="z",
            revision_number=1,
            date_modified=datetime(2024, 1, 1, 12, 0, 0),
            modified_by="carol",
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
