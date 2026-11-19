"""Unit tests for workflows duckdb centralizer."""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from imednet.models.record_revisions import RecordRevision
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow
from imednet_workflows.namespace import Workflows


def test_duckdb_ingestion_import_error(monkeypatch) -> None:
    """Test that duckdb ingestion import error."""
    builtin_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        """Helper function to fake import."""
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(
        ImportError,
        match=(
            r"DuckDB ingestion requires the optional 'duckdb' dependency\. "
            r"Install with `pip install 'imednet\[duckdb\]'`\."
        ),
    ):
        DuckDBIngestionWorkflow(MagicMock(), ":memory:")


def test_workflows_namespace_lazy_duckdb_centralizer(monkeypatch) -> None:
    """Test that Workflows instantiates without duckdb and defers ImportError."""
    builtin_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        """Helper function to fake import."""
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    sdk = MagicMock()
    workflows = Workflows(sdk)
    assert workflows.data_extraction is not None

    with pytest.raises(
        ImportError,
        match=(
            r"DuckDB ingestion requires the optional 'duckdb' dependency\. "
            r"Install with `pip install 'imednet\[duckdb\]'`\."
        ),
    ):
        _ = workflows.duckdb_centralizer


def test_workflows_namespace_duckdb_centralizer_caching() -> None:
    """Test that duckdb_centralizer returns a cached DuckDBIngestionWorkflow instance."""
    pytest.importorskip("duckdb")
    sdk = MagicMock()
    workflows = Workflows(sdk)

    centralizer1 = workflows.duckdb_centralizer
    centralizer2 = workflows.duckdb_centralizer

    assert isinstance(centralizer1, DuckDBIngestionWorkflow)
    assert centralizer1 is centralizer2


def test_duckdb_centralizer_default_memory() -> None:
    """Test that DuckDBIngestionWorkflow defaults to in-memory mode."""
    pytest.importorskip("duckdb")
    sdk = MagicMock()
    with patch("duckdb.connect") as mock_connect:
        DuckDBIngestionWorkflow(sdk)
        mock_connect.assert_called_once_with(":memory:")


def test_ingest_revisions_and_build_silver_view() -> None:
    """Test that ingest revisions and build silver view."""
    pytest.importorskip("duckdb")
    sdk = MagicMock()
    workflow = DuckDBIngestionWorkflow(sdk, ":memory:")

    initial_revisions = [
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
            reason_for_change="Initial entry",
            deleted=False,
            date_created="2024-01-01T10:00:00Z",
            last_updated="2024-01-01T10:00:00Z",
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
            reason_for_change="Corrected demographics",
            deleted=False,
            date_created="2024-01-02T10:00:00Z",
            last_updated="2024-01-02T10:05:00Z",
        ),
    ]
    appended_revision = [
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
            reason_for_change="Corrected demographics",
            deleted=False,
            date_created="2024-01-02T10:00:00Z",
            last_updated="2024-01-02T10:05:00Z",
        ),
        RecordRevision(
            study_key="STUDY",
            record_revision_id=103,
            record_id=1,
            record_oid="OID-1",
            form_key="DEMO",
            subject_id=501,
            subject_oid="SUBJ-OID-01",
            subject_key="SUBJ-01",
            site_id=10,
            interval_id=5,
            record_status="Record Verified",
            record_revision=2,
            data_revision=1,
            user="dave",
            role="Monitor",
            reason_for_change="Status verified",
            deleted=False,
            date_created="2024-01-02T12:00:00+02:00",
            last_updated="2024-01-02T12:05:00+02:00",
        ),
        RecordRevision(
            study_key="STUDY",
            record_revision_id=201,
            record_id=2,
            record_oid="OID-2",
            form_key="VITALS",
            subject_id=502,
            subject_oid="SUBJ-OID-02",
            subject_key="SUBJ-02",
            site_id=11,
            interval_id=6,
            record_status="Record Complete",
            record_revision=1,
            data_revision=1,
            user="carol",
            role="CRC",
            reason_for_change="Baseline vitals",
            deleted=False,
            date_created="2024-01-03T12:00:00Z",
            last_updated="2024-01-03T12:00:00Z",
        ),
    ]

    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        side_effect=[initial_revisions, appended_revision],
    ) as extract_mock:
        assert (
            workflow.ingest_revisions(
                "STUDY",
                start_date="2024-01-01",
                end_date="2024-01-31",
                mode="replace",
            )
            == 2
        )
        assert workflow.ingest_revisions("STUDY", mode="append") == 3

    assert extract_mock.call_args_list == [
        call("STUDY", start_date="2024-01-01", end_date="2024-01-31"),
        call("STUDY", start_date=None, end_date=None),
    ]

    expected_columns = list(RecordRevision.model_fields.keys())
    assert len(expected_columns) == 19
    bronze_cols = [
        desc[0]
        for desc in workflow._connection.execute(
            "SELECT * FROM bronze_revisions LIMIT 0"
        ).description
    ]
    assert bronze_cols == expected_columns
    assert "form_id" not in bronze_cols
    assert "variable_name" not in bronze_cols
    assert "value" not in bronze_cols

    bronze_count = workflow._connection.execute("SELECT COUNT(*) FROM bronze_revisions").fetchone()[
        0
    ]
    assert bronze_count == 4
    rev2_reason = workflow._connection.execute("""
        SELECT reason_for_change
        FROM bronze_revisions
        WHERE record_revision_id = 102
        """).fetchone()[0]
    assert rev2_reason == "Corrected demographics"

    workflow.build_silver_view("STUDY")
    silver_cols = [
        desc[0]
        for desc in workflow._connection.execute(
            "SELECT * FROM silver_current_state LIMIT 0"
        ).description
    ]
    assert silver_cols == expected_columns

    silver_rows = workflow._connection.execute("""
        SELECT
            record_revision_id,
            record_id,
            subject_id,
            subject_oid,
            form_key,
            record_revision,
            "user",
            reason_for_change,
            date_created
        FROM silver_current_state
        ORDER BY record_id
        """).fetchall()
    assert silver_rows == [
        (
            103,
            1,
            501,
            "SUBJ-OID-01",
            "DEMO",
            2,
            "dave",
            "Status verified",
            datetime(2024, 1, 2, 10, 0, 0),
        ),
        (
            201,
            2,
            502,
            "SUBJ-OID-02",
            "VITALS",
            1,
            "carol",
            "Baseline vitals",
            datetime(2024, 1, 3, 12, 0, 0),
        ),
    ]


def test_ingest_revisions_invalid_mode() -> None:
    """Test that ingest revisions invalid mode."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    with pytest.raises(ValueError, match="mode must be one of"):
        workflow.ingest_revisions("STUDY", mode="invalid")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid_key",
    [
        "STUDY 01",
        "STUDY'; DROP TABLE bronze_revisions; --",
        'STUDY" OR 1=1',
        "STUDY$KEY",
        "STUDY\nKEY",
        "",
        " ",
    ],
)
def test_build_silver_view_rejects_invalid_study_key(invalid_key: str) -> None:
    """Test that build_silver_view rejects invalid study keys."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    with pytest.raises(ValueError, match="Invalid study key"):
        workflow.build_silver_view(invalid_key)


@pytest.mark.parametrize(
    "valid_key",
    [
        "STUDY",
        "STUDY-01",
        "STUDY_A",
        "PROT:101",
        "STUDY.v1",
    ],
)
def test_build_silver_view_accepts_valid_study_keys(valid_key: str) -> None:
    """Test that build_silver_view accepts valid study keys."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    workflow.build_silver_view(valid_key)


def test_duckdb_centralizer_row_conversion_edge_cases() -> None:
    """Test row conversion edge cases for timestamps, optional ints, and required keys."""
    from datetime import UTC
    from types import SimpleNamespace

    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    rev = SimpleNamespace(
        study_key=None,
        record_revision_id="42",
        record_id="  ",
        record_revision=True,
        data_revision=None,
        record_status=None,
        subject_id="77",
        subject_key="SUBJ-77",
        site_id=None,
        form_key="LABS",
        interval_id=None,
        deleted=None,
        date_created=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
        reason_for_change="",
        record_oid=None,
        role=None,
        subject_oid=None,
        user="auditor",
        last_updated="   ",
    )
    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=[rev]):
        assert workflow.ingest_revisions("STUDY", mode="append") == 1

    stored = workflow._connection.execute(
        "SELECT record_revision_id, record_id, record_revision, subject_id, deleted, date_created, last_updated "
        "FROM bronze_revisions"
    ).fetchone()
    assert stored == (42, None, None, 77, None, datetime(2026, 1, 15, 8, 30), None)

    for bad_id in (None, False, "   ", 0, -5):
        with pytest.raises(ValueError, match="record_revision_id"):
            list(workflow._to_rows("STUDY", [SimpleNamespace(record_revision_id=bad_id)]))  # type: ignore[list-item]

    with pytest.raises(ValueError, match="study_key"):
        list(workflow._to_rows("   ", [SimpleNamespace(record_revision_id=1)]))  # type: ignore[list-item]
