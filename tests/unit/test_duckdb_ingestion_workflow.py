"""Unit tests for duckdb ingestion workflow."""

from __future__ import annotations

import sys
from builtins import __import__ as builtin_import
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from imednet.models.record_revisions import RecordRevision
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow


def test_ingest_revisions_append_mode() -> None:
    """Test that ingest_revisions append mode deduplicates matching (study_key, record_revision_id)."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    first = [
        RecordRevision(
            study_key="STUDY",
            record_revision_id=1,
            record_id=1,
            subject_id=100,
            subject_oid="SUBJ-OID-1",
            subject_key="SUBJ-1",
            form_key="DEMO",
            record_revision=1,
            user="alice",
            reason_for_change="Initial",
            date_created="2024-01-01T10:00:00Z",
        )
    ]
    second = [
        RecordRevision(
            study_key="STUDY",
            record_revision_id=1,
            record_id=1,
            subject_id=100,
            subject_oid="SUBJ-OID-1",
            subject_key="SUBJ-1",
            form_key="DEMO",
            record_revision=1,
            user="alice",
            reason_for_change="Initial re-synced",
            date_created="2024-01-01T10:00:00Z",
        ),
        RecordRevision(
            study_key="STUDY",
            record_revision_id=2,
            record_id=2,
            subject_id=101,
            subject_oid="SUBJ-OID-2",
            subject_key="SUBJ-2",
            form_key="DEMO",
            record_revision=1,
            user="bob",
            reason_for_change="Initial",
            date_created="2024-01-02T10:00:00Z",
        ),
    ]

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", side_effect=[first, second]):
        workflow.ingest_revisions("STUDY", mode="append")
        workflow.ingest_revisions("STUDY", mode="append")

    rows = workflow._connection.execute(
        "SELECT record_revision_id, reason_for_change FROM bronze_revisions ORDER BY record_revision_id"
    ).fetchall()
    assert rows == [(1, "Initial re-synced"), (2, "Initial")]


def test_ingest_revisions_missing_record_revision_id_raises() -> None:
    """Test that ingest_revisions raises ValueError when record_revision_id is missing."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    invalid = [RecordRevision(record_id=1, form_key="DEMO", record_revision=1)]

    with (
        patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=invalid),
        pytest.raises(ValueError, match="record_revision_id"),
    ):
        workflow.ingest_revisions("STUDY", mode="append")


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


def test_migrate_persistent_base_version_database_append_and_silver_view(tmp_path: Path) -> None:
    """Test opening a persistent DB created by the base 17-column version, appending, and querying silver."""
    duckdb = pytest.importorskip("duckdb")
    db_path = tmp_path / "base_version.duckdb"

    legacy_conn = duckdb.connect(str(db_path))
    legacy_conn.execute("""
        CREATE TABLE IF NOT EXISTS bronze_revisions (
            study_key VARCHAR,
            record_revision_id BIGINT,
            record_id BIGINT,
            record_oid VARCHAR,
            form_key VARCHAR,
            subject_key VARCHAR,
            site_id BIGINT,
            interval_id BIGINT,
            record_status VARCHAR,
            record_revision INTEGER,
            data_revision INTEGER,
            "user" VARCHAR,
            role VARCHAR,
            reason_for_change VARCHAR,
            deleted BOOLEAN,
            date_created TIMESTAMP,
            last_updated TIMESTAMP
        )
        """)
    legacy_conn.executemany(
        """
        INSERT INTO bronze_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "STUDY",
                101,
                1,
                "OID-1",
                "DEMO",
                "SUBJ-01",
                10,
                5,
                "Record Incomplete",
                1,
                1,
                "alice",
                "CRC",
                "Original base row",
                False,
                "2024-01-01T10:00:00Z",
                "2024-01-01T10:00:00Z",
            ),
            (
                "STUDY",
                101,
                1,
                "OID-1",
                "DEMO",
                "SUBJ-01",
                10,
                5,
                "Record Incomplete",
                1,
                1,
                "alice",
                "CRC",
                "Duplicate base append row",
                False,
                "2024-01-01T10:00:00Z",
                "2024-01-01T10:01:00Z",
            ),
            (
                "STUDY",
                201,
                2,
                "OID-2",
                "VITALS",
                "SUBJ-02",
                11,
                6,
                "Record Complete",
                1,
                1,
                "carol",
                "CRC",
                "Preserved base vitals",
                False,
                "2024-01-02T09:00:00Z",
                "2024-01-02T09:00:00Z",
            ),
        ],
    )
    legacy_conn.execute("""
        CREATE OR REPLACE VIEW silver_current_state AS
        SELECT
            study_key,
            record_revision_id,
            record_id,
            record_oid,
            form_key,
            subject_key,
            site_id,
            interval_id,
            record_status,
            record_revision,
            data_revision,
            "user",
            role,
            reason_for_change,
            deleted,
            date_created,
            last_updated
        FROM bronze_revisions
        WHERE study_key = 'STUDY'
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY record_id
            ORDER BY
                record_revision DESC NULLS LAST,
                data_revision DESC NULLS LAST,
                date_created DESC NULLS LAST,
                record_revision_id DESC NULLS LAST
        ) = 1
        """)
    legacy_conn.close()

    workflow = DuckDBIngestionWorkflow(MagicMock(), str(db_path))
    incremental_batch = [
        RecordRevision(
            study_key="STUDY",
            record_revision_id=101,
            record_id=1,
            record_oid="OID-1",
            form_key="DEMO",
            subject_id=501,
            subject_oid="SUBJ-OID-01",
            subject_key="SUBJ-01",
            site_id=10,
            interval_id=5,
            record_status="Record Incomplete",
            record_revision=1,
            data_revision=1,
            user="alice",
            role="CRC",
            reason_for_change="Backfilled with subject identifiers",
            deleted=False,
            date_created="2024-01-01T10:00:00Z",
            last_updated="2024-01-01T10:02:00Z",
        ),
        RecordRevision(
            study_key="STUDY",
            record_revision_id=102,
            record_id=1,
            record_oid="OID-1",
            form_key="DEMO",
            subject_id=501,
            subject_oid="SUBJ-OID-01",
            subject_key="SUBJ-01",
            site_id=10,
            interval_id=5,
            record_status="Record Complete",
            record_revision=2,
            data_revision=2,
            user="bob",
            role="Investigator",
            reason_for_change="Post-upgrade revision",
            deleted=False,
            date_created="2024-01-03T11:00:00Z",
            last_updated="2024-01-03T11:05:00Z",
        ),
    ]

    with patch.object(
        DataExtractionWorkflow, "extract_audit_trail", return_value=incremental_batch
    ):
        assert workflow.ingest_revisions("STUDY", mode="append") == 2

    table_info = workflow._connection.execute("PRAGMA table_info('bronze_revisions')").fetchall()
    assert [row[1] for row in table_info] == list(RecordRevision.model_fields.keys())
    assert [row[1] for row in table_info if row[5]] == ["study_key", "record_revision_id"]

    bronze_rows = workflow._connection.execute("""
        SELECT record_revision_id, record_id, subject_id, subject_oid, reason_for_change
        FROM bronze_revisions
        ORDER BY record_revision_id
        """).fetchall()
    assert bronze_rows == [
        (101, 1, 501, "SUBJ-OID-01", "Backfilled with subject identifiers"),
        (102, 1, 501, "SUBJ-OID-01", "Post-upgrade revision"),
        (201, 2, None, None, "Preserved base vitals"),
    ]

    # Pre-existing silver_current_state view is upgraded automatically on migration
    auto_silver = workflow._connection.execute("""
        SELECT record_revision_id, record_id, subject_id, subject_oid, reason_for_change
        FROM silver_current_state
        ORDER BY record_id
        """).fetchall()
    assert auto_silver == [
        (102, 1, 501, "SUBJ-OID-01", "Post-upgrade revision"),
        (201, 2, None, None, "Preserved base vitals"),
    ]

    # Explicit build_silver_view and subsequent reopen + append remain idempotent
    workflow.build_silver_view("STUDY")
    workflow._connection.close()

    reopened = DuckDBIngestionWorkflow(MagicMock(), str(db_path))
    follow_up = [
        RecordRevision(
            study_key="STUDY",
            record_revision_id=202,
            record_id=2,
            record_oid="OID-2",
            form_key="VITALS",
            subject_id=502,
            subject_oid="SUBJ-OID-02",
            subject_key="SUBJ-02",
            site_id=11,
            interval_id=6,
            record_status="Record Verified",
            record_revision=2,
            data_revision=1,
            user="dave",
            role="Monitor",
            reason_for_change="Verified vitals",
            deleted=False,
            date_created="2024-01-04T08:00:00Z",
            last_updated="2024-01-04T08:00:00Z",
        )
    ]
    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=follow_up):
        assert reopened.ingest_revisions("STUDY", mode="append") == 1

    final_silver = reopened._connection.execute("""
        SELECT record_revision_id, record_id, subject_id, subject_oid, reason_for_change
        FROM silver_current_state
        ORDER BY record_id
        """).fetchall()
    assert final_silver == [
        (102, 1, 501, "SUBJ-OID-01", "Post-upgrade revision"),
        (202, 2, 502, "SUBJ-OID-02", "Verified vitals"),
    ]
    reopened._connection.close()
