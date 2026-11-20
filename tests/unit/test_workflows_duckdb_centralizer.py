"""Unit tests for workflows duckdb centralizer."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from imednet.models.record_revisions import RecordRevision
from imednet.models.records import RecordStatus
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
            record_revision_id=201,
            record_id=2,
            record_oid="OID-2",
            form_key="VITALS",
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
            == 2
        )
        assert workflow.ingest_revisions("STUDY", mode="append") == 1

    assert extract_mock.call_args_list == [
        call("STUDY", start_date="2024-01-01", end_date="2024-01-31"),
        call("STUDY", start_date=None, end_date=None),
    ]

    expected_columns = [
        "study_key",
        "record_revision_id",
        "record_id",
        "record_oid",
        "form_key",
        "subject_key",
        "site_id",
        "interval_id",
        "record_status",
        "record_revision",
        "data_revision",
        "user",
        "role",
        "reason_for_change",
        "deleted",
        "date_created",
        "last_updated",
    ]
    bronze_cols = [
        desc[0]
        for desc in workflow._connection.execute(
            "SELECT * FROM bronze_revisions LIMIT 0"
        ).description
    ]
    assert bronze_cols == expected_columns
    assert "variable_name" not in bronze_cols
    assert "value" not in bronze_cols

    bronze_count = workflow._connection.execute("SELECT COUNT(*) FROM bronze_revisions").fetchone()[
        0
    ]
    assert bronze_count == 3
    rev2_reason = workflow._connection.execute("""
        SELECT reason_for_change
        FROM bronze_revisions
        WHERE record_id = 1 AND record_revision = 2
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
        SELECT record_id, form_key, record_revision, "user", reason_for_change, date_created
        FROM silver_current_state
        ORDER BY record_id
        """).fetchall()
    assert silver_rows == [
        (
            1,
            "DEMO",
            2,
            "bob",
            "Corrected demographics",
            datetime(2024, 1, 2, 10, 0, 0),
        ),
        (
            2,
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


def test_ingest_revisions_uses_bulk_register_without_executemany() -> None:
    """Test bulk staging calls register and unregister once per non-empty batch."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    real_conn = workflow._connection
    spy_conn = MagicMock(wraps=real_conn)
    workflow._connection = spy_conn

    batch_one = [
        RecordRevision(
            record_revision_id=10,
            record_id=1,
            form_key="DEMO",
            record_revision=1,
            user="alice",
            date_created="2024-01-01T10:00:00Z",
        ),
        RecordRevision(
            record_revision_id=11,
            record_id=1,
            form_key="DEMO",
            record_revision=2,
            user="bob",
            date_created="2024-01-02T10:00:00Z",
        ),
    ]
    batch_two = [
        RecordRevision(
            record_revision_id=20,
            record_id=2,
            form_key="VITALS",
            record_revision=1,
            user="carol",
            date_created="2024-01-03T10:00:00Z",
        ),
    ]

    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        side_effect=[batch_one, batch_two],
    ):
        assert workflow.ingest_revisions("STUDY", mode="replace") == 2
        assert workflow.ingest_revisions("STUDY", mode="append") == 1

    spy_conn.executemany.assert_not_called()
    assert spy_conn.register.call_count == 2
    assert spy_conn.unregister.call_count == 2
    registered_views = [c.args[0] for c in spy_conn.register.call_args_list]
    unregistered_views = [c.args[0] for c in spy_conn.unregister.call_args_list]
    assert registered_views == unregistered_views
    assert len(set(registered_views)) == 2


def test_ingest_revisions_empty_batch_ensures_table_and_skips_staging() -> None:
    """Test empty batches ensure bronze_revisions exists, skip staging, and return 0."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    real_conn = workflow._connection
    spy_conn = MagicMock(wraps=real_conn)
    workflow._connection = spy_conn

    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        return_value=iter(()),
    ):
        assert workflow.ingest_revisions("STUDY", mode="replace") == 0

    spy_conn.register.assert_not_called()
    spy_conn.unregister.assert_not_called()
    spy_conn.executemany.assert_not_called()
    count = real_conn.execute("SELECT COUNT(*) FROM bronze_revisions").fetchone()[0]
    assert count == 0


def test_ingest_revisions_preserves_64bit_bigint_and_nullable_fields() -> None:
    """Test nullable 64-bit BIGINT fields preserve precision without float64 rounding."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    large_rev_id = (1 << 53) + 1
    large_rec_id = (1 << 62) + 7
    large_site_id = (1 << 55) + 3
    large_interval_id = (1 << 58) + 9

    revisions = [
        RecordRevision(
            study_key="STUDY",
            record_revision_id=large_rev_id,
            record_id=large_rec_id,
            site_id=large_site_id,
            interval_id=large_interval_id,
            form_key="DEMO",
            record_revision=1,
            data_revision=1,
            deleted=True,
            date_created="2024-01-01T10:00:00Z",
        ),
        SimpleNamespace(
            study_key=None,
            record_revision_id=None,
            record_id=None,
            record_oid=None,
            form_key=None,
            subject_key=None,
            site_id=None,
            interval_id=None,
            record_status=None,
            record_revision=None,
            data_revision=None,
            user=None,
            role=None,
            reason_for_change=None,
            deleted=None,
            date_created=None,
            last_updated=None,
        ),
    ]

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions):
        assert workflow.ingest_revisions("FALLBACK_STUDY", mode="replace") == 2

    rows = workflow._connection.execute("""
        SELECT
            study_key,
            record_revision_id,
            record_id,
            site_id,
            interval_id,
            record_revision,
            data_revision,
            deleted
        FROM bronze_revisions
        ORDER BY study_key DESC
        """).fetchall()
    assert rows == [
        (
            "STUDY",
            large_rev_id,
            large_rec_id,
            large_site_id,
            large_interval_id,
            1,
            1,
            True,
        ),
        ("FALLBACK_STUDY", None, None, None, None, None, None, False),
    ]


def test_ingest_revisions_normalizes_enums_and_timestamps() -> None:
    """Test RecordStatus enums, aware datetimes, and blank timestamp strings."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    tz_plus2 = timezone(timedelta(hours=2))
    tz_minus5 = timezone(timedelta(hours=-5))

    revisions = [
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=1,
            record_id=10,
            record_oid=12345,
            form_key="DEMO",
            subject_key="SUBJ-1",
            site_id=1,
            interval_id=1,
            record_status=RecordStatus.RECORD_COMPLETE,
            record_revision=1,
            data_revision=1,
            user="alice",
            role="CRC",
            reason_for_change="Aware timestamps",
            deleted=False,
            date_created=datetime(2024, 5, 1, 14, 30, 0, tzinfo=tz_plus2),
            last_updated=datetime(2024, 5, 1, 8, 15, 0, tzinfo=tz_minus5),
        ),
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=2,
            record_id=11,
            record_oid="OID-11",
            form_key="DEMO",
            subject_key="SUBJ-2",
            site_id=1,
            interval_id=1,
            record_status="Custom Study Status",
            record_revision=1,
            data_revision=0,
            user="bob",
            role="PI",
            reason_for_change="Blank and naive timestamps",
            deleted=False,
            date_created=datetime(2024, 5, 2, 9, 0, 0),
            last_updated="   ",
        ),
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=3,
            record_id=12,
            record_oid="OID-12",
            form_key="DEMO",
            subject_key="SUBJ-3",
            site_id=1,
            interval_id=1,
            record_status=RecordStatus.SIGNED,
            record_revision=1,
            data_revision=1,
            user="carol",
            role="PI",
            reason_for_change="Date fallback and empty string",
            deleted=False,
            date_created=date(2024, 5, 3),
            last_updated="",
        ),
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=4,
            record_id=13,
            record_oid="OID-13",
            form_key="DEMO",
            subject_key="SUBJ-4",
            site_id=1,
            interval_id=1,
            record_status=RecordStatus.VERIFIED,
            record_revision=1,
            data_revision=1,
            user="dave",
            role="DM",
            reason_for_change="UTC aware",
            deleted=False,
            date_created=datetime(2024, 5, 4, 16, 45, 30, tzinfo=UTC),
            last_updated=None,
        ),
    ]

    row_tuples = list(workflow._to_rows("STUDY", revisions))
    assert type(row_tuples[0][8]) is str
    assert row_tuples[0][8] == "Record Complete"
    assert row_tuples[0][3] == "12345"

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions):
        assert workflow.ingest_revisions("STUDY", mode="replace") == 4

    stored = workflow._connection.execute("""
        SELECT record_revision_id, record_oid, record_status, date_created, last_updated
        FROM bronze_revisions
        ORDER BY record_revision_id
        """).fetchall()
    assert stored == [
        (
            1,
            "12345",
            "Record Complete",
            datetime(2024, 5, 1, 12, 30, 0),
            datetime(2024, 5, 1, 13, 15, 0),
        ),
        (
            2,
            "OID-11",
            "Custom Study Status",
            datetime(2024, 5, 2, 9, 0, 0),
            None,
        ),
        (
            3,
            "OID-12",
            "Signed",
            datetime(2024, 5, 3, 0, 0, 0),
            None,
        ),
        (
            4,
            "OID-13",
            "Verified",
            datetime(2024, 5, 4, 16, 45, 30),
            None,
        ),
    ]


def test_ingest_revisions_falls_back_to_pandas_without_pyarrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test staging falls back to pandas DataFrame with expected dtypes when pyarrow is absent."""
    pytest.importorskip("duckdb")
    import pandas as pd

    builtin_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("No module named pyarrow")
        return builtin_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "pyarrow", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    empty_df = workflow._build_staging_relation([])
    assert isinstance(empty_df, pd.DataFrame)
    assert len(empty_df) == 0
    assert str(empty_df["record_revision_id"].dtype) == "Int64"
    assert str(empty_df["record_revision"].dtype) == "Int32"
    assert str(empty_df["deleted"].dtype) == "bool"
    assert str(empty_df["study_key"].dtype) == "object"

    large_id = (1 << 53) + 1
    revisions = [
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=large_id,
            record_id=large_id + 2,
            record_oid="OID-1",
            form_key="DEMO",
            subject_key="SUBJ-1",
            site_id=large_id + 4,
            interval_id=large_id + 6,
            record_status=RecordStatus.RECORD_COMPLETE,
            record_revision=2,
            data_revision=1,
            user="alice",
            role="CRC",
            reason_for_change="Pandas fallback",
            deleted=False,
            date_created=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2))),
            last_updated=None,
        ),
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=None,
            record_id=None,
            record_oid=None,
            form_key="DEMO",
            subject_key=None,
            site_id=None,
            interval_id=None,
            record_status=None,
            record_revision=None,
            data_revision=None,
            user=None,
            role=None,
            reason_for_change=None,
            deleted=True,
            date_created=None,
            last_updated=None,
        ),
    ]

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions):
        assert workflow.ingest_revisions("STUDY", mode="replace") == 2

    rows = workflow._connection.execute("""
        SELECT record_revision_id, record_id, site_id, interval_id, record_status, date_created, deleted
        FROM bronze_revisions
        ORDER BY deleted ASC
        """).fetchall()
    assert rows == [
        (
            large_id,
            large_id + 2,
            large_id + 4,
            large_id + 6,
            "Record Complete",
            datetime(2024, 6, 1, 10, 0, 0),
            False,
        ),
        (None, None, None, None, None, None, True),
    ]


def test_ingest_revisions_unregisters_staging_view_on_insert_failure() -> None:
    """Test temporary staging view is unregistered even when INSERT fails."""
    pytest.importorskip("duckdb")
    import duckdb

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    real_conn = workflow._connection
    spy_conn = MagicMock(wraps=real_conn)

    def failing_execute(query: str, *args: object, **kwargs: object) -> object:
        if "INSERT INTO bronze_revisions" in query:
            raise RuntimeError("Simulated DuckDB insert failure")
        return real_conn.execute(query, *args, **kwargs)

    spy_conn.execute.side_effect = failing_execute
    workflow._connection = spy_conn

    revisions = [
        RecordRevision(
            record_revision_id=1,
            record_id=1,
            form_key="DEMO",
            record_revision=1,
            user="alice",
            date_created="2024-01-01T10:00:00Z",
        )
    ]

    with (
        patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions),
        pytest.raises(RuntimeError, match="Simulated DuckDB insert failure"),
    ):
        workflow.ingest_revisions("STUDY", mode="append")

    spy_conn.register.assert_called_once()
    spy_conn.unregister.assert_called_once()
    view_name = spy_conn.register.call_args.args[0]
    assert spy_conn.unregister.call_args.args[0] == view_name
    with pytest.raises(duckdb.CatalogException):
        real_conn.execute(f'SELECT * FROM "{view_name}"')
