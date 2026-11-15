"""Unit tests for workflows duckdb centralizer."""

from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

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


def test_ingest_revisions_and_build_silver_view() -> None:
    """Test that ingest revisions and build silver view."""
    pytest.importorskip("duckdb")
    sdk = MagicMock()
    workflow = DuckDBIngestionWorkflow(sdk, ":memory:")

    initial_revisions = [
        SimpleNamespace(
            record_id=1,
            form_id=10,
            variable_name="AGE",
            value=30,
            revision_number=1,
            date_modified=datetime(2024, 1, 1, 10, 0, 0),
            modified_by="alice",
        ),
        SimpleNamespace(
            record_id=1,
            form_id=10,
            variable_name="AGE",
            value=31,
            revision_number=2,
            date_modified=datetime(2024, 1, 2, 10, 0, 0),
            modified_by="bob",
        ),
        SimpleNamespace(
            record_id=1,
            form_id=10,
            variable_name="WEIGHT",
            value=70,
            revision_number=1,
            date_modified=datetime(2024, 1, 1, 11, 0, 0),
            modified_by="alice",
        ),
    ]
    appended_revision = [
        SimpleNamespace(
            record_id=2,
            form_id=11,
            variable_name="HEIGHT",
            value=180,
            revision_number=1,
            date_modified=datetime(2024, 1, 3, 12, 0, 0),
            modified_by="carol",
        )
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
            == 3
        )
        assert workflow.ingest_revisions("STUDY", mode="append") == 1

    assert extract_mock.call_args_list == [
        call("STUDY", start_date="2024-01-01", end_date="2024-01-31"),
        call("STUDY", start_date=None, end_date=None),
    ]

    bronze_count = workflow._connection.execute("SELECT COUNT(*) FROM bronze_revisions").fetchone()[
        0
    ]
    assert bronze_count == 4
    age_value = workflow._connection.execute("""
        SELECT value
        FROM bronze_revisions
        WHERE record_id = 1 AND variable_name = 'AGE' AND revision_number = 2
        """).fetchone()[0]
    assert age_value == "31"

    workflow.build_silver_view("STUDY")
    silver_rows = workflow._connection.execute("""
        SELECT record_id, variable_name, value, revision_number
        FROM silver_current_state
        ORDER BY record_id, variable_name
        """).fetchall()
    assert silver_rows == [
        (1, "AGE", "31", 2),
        (1, "WEIGHT", "70", 1),
        (2, "HEIGHT", "180", 1),
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
