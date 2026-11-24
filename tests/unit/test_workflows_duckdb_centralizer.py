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


def test_ingest_revisions_normalizes_record_revision_cst_and_offset_strings() -> None:
    """Test RecordRevision ISO-8601 -06:00, +02:00, Z, fractional, and naive string timestamps."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    revisions = [
        RecordRevision(
            studyKey="STUDY",
            recordRevisionId=1001,
            recordId=50,
            recordOid="REC-50",
            formKey="VITALS",
            subjectKey="SUBJ-01",
            siteId=10,
            intervalId=1,
            recordStatus="Record Complete",
            recordRevision=1,
            dataRevision=1,
            user="crc_cst",
            role="CRC",
            reasonForChange="Captured in CST -06:00",
            deleted=False,
            dateCreated="2024-01-15T08:30:00-06:00",
            lastUpdated="2024-01-15T09:45:15-06:00",
        ),
        RecordRevision(
            studyKey="STUDY",
            recordRevisionId=1002,
            recordId=50,
            recordOid="REC-50",
            formKey="VITALS",
            subjectKey="SUBJ-01",
            siteId=10,
            intervalId=1,
            recordStatus="Verified",
            recordRevision=1,
            dataRevision=1,
            user="pi_utc",
            role="PI",
            reasonForChange="Updated 15 minutes later in UTC",
            deleted=False,
            dateCreated="2024-01-15T14:45:00Z",
            lastUpdated="2024-01-15T14:45:00.250000-06:00",
        ),
        RecordRevision(
            studyKey="STUDY",
            recordRevisionId=1003,
            recordId=51,
            recordOid="REC-51",
            formKey="DEMO",
            subjectKey="SUBJ-02",
            siteId=10,
            intervalId=1,
            recordStatus="Signed",
            recordRevision=1,
            dataRevision=1,
            user="pi_naive",
            role="PI",
            reasonForChange="Space-separated naive UTC and blank lastUpdated",
            deleted=False,
            dateCreated="2024-01-15 18:10:00",
            lastUpdated="   ",
        ),
    ]

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=revisions):
        assert workflow.ingest_revisions("STUDY", mode="replace") == 3

    stored = workflow._connection.execute("""
        SELECT record_revision_id, date_created, last_updated
        FROM bronze_revisions
        ORDER BY record_revision_id
        """).fetchall()
    assert stored == [
        (
            1001,
            datetime(2024, 1, 15, 14, 30, 0),
            datetime(2024, 1, 15, 15, 45, 15),
        ),
        (
            1002,
            datetime(2024, 1, 15, 14, 45, 0),
            datetime(2024, 1, 15, 20, 45, 0, 250000),
        ),
        (
            1003,
            datetime(2024, 1, 15, 18, 10, 0),
            None,
        ),
    ]

    workflow.build_silver_view("STUDY")
    winner = workflow._connection.execute("""
        SELECT record_revision_id, "user", date_created
        FROM silver_current_state
        WHERE record_id = 50
        """).fetchone()
    assert winner == (1002, "pi_utc", datetime(2024, 1, 15, 14, 45, 0))
    workflow.close()


@pytest.mark.parametrize(
    "invalid_key",
    [
        "STUDY 01",
        "STUDY'; DROP TABLE bronze_revisions; --",
        'STUDY" OR 1=1',
        "",
        "   ",
    ],
)
def test_ingest_revisions_rejects_invalid_study_key_and_revision_study_key(
    invalid_key: str,
) -> None:
    """Test ingest_revisions validates study_key and non-empty revision.study_key."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    with (
        patch.object(DataExtractionWorkflow, "extract_audit_trail") as extract_mock,
        pytest.raises(ValueError, match="Invalid study key"),
    ):
        workflow.ingest_revisions(invalid_key)
    extract_mock.assert_not_called()

    seed = [
        RecordRevision(
            study_key="STUDY",
            record_revision_id=1,
            record_id=1,
            form_key="DEMO",
            record_revision=1,
            date_created="2024-01-01T00:00:00Z",
        )
    ]
    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=seed):
        assert workflow.ingest_revisions("STUDY", mode="replace") == 1

    if invalid_key != "":
        bad_rev = [
            SimpleNamespace(
                study_key=invalid_key,
                record_revision_id=2,
                record_id=2,
                form_key="DEMO",
                record_revision=1,
                date_created="2024-01-02T00:00:00Z",
            )
        ]
        with (
            patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=bad_rev),
            pytest.raises(ValueError, match="Invalid study key"),
        ):
            workflow.ingest_revisions("STUDY", mode="replace")

        # Existing study rows remain untouched because validation failed before transaction
        remaining = workflow._connection.execute(
            "SELECT study_key, record_revision_id FROM bronze_revisions"
        ).fetchall()
        assert remaining == [("STUDY", 1)]
    else:
        # Empty string revision.study_key falls back to validated study_key
        empty_study_rev = [
            RecordRevision(
                study_key="",
                record_revision_id=3,
                record_id=3,
                form_key="DEMO",
                record_revision=1,
                date_created="2024-01-03T00:00:00Z",
            )
        ]
        with patch.object(
            DataExtractionWorkflow, "extract_audit_trail", return_value=empty_study_rev
        ):
            assert workflow.ingest_revisions("STUDY", mode="append") == 1
        stored_keys = workflow._connection.execute(
            "SELECT study_key, record_revision_id FROM bronze_revisions ORDER BY record_revision_id"
        ).fetchall()
        assert stored_keys == [("STUDY", 1), ("STUDY", 3)]

    workflow.close()


def test_ingest_revisions_boundary_deduplication_and_legacy_table(tmp_path) -> None:
    """Test append deduplicates overlapping keys, intra-batch duplicates, and legacy tables."""
    duckdb = pytest.importorskip("duckdb")

    # 1. Pre-create a legacy DuckDB file WITHOUT a UNIQUE constraint and WITH duplicate rows
    db_file = str(tmp_path / "legacy_audit.duckdb")
    legacy_conn = duckdb.connect(db_file)
    legacy_conn.execute("""
        CREATE TABLE bronze_revisions (
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
    for dup_reason in ("Legacy duplicate 1", "Legacy duplicate 2"):
        legacy_conn.execute(
            """
            INSERT INTO bronze_revisions (
                study_key, record_revision_id, record_id, form_key,
                record_revision, data_revision, "user", reason_for_change,
                deleted, date_created
            ) VALUES ('STUDY', 501, 10, 'VITALS', 1, 1, 'alice', ?, FALSE, '2024-01-31 23:59:59')
            """,
            [dup_reason],
        )
    legacy_conn.close()

    with DuckDBIngestionWorkflow(MagicMock(), db_file) as workflow:
        overlapping_batch = [
            RecordRevision(
                study_key="STUDY",
                record_revision_id=501,
                record_id=10,
                form_key="VITALS",
                record_revision=1,
                data_revision=1,
                user="alice",
                reason_for_change="Stale intra-batch copy",
                deleted=False,
                date_created="2024-01-31T17:59:59-06:00",
            ),
            RecordRevision(
                study_key="STUDY",
                record_revision_id=501,
                record_id=10,
                form_key="VITALS",
                record_revision=1,
                data_revision=1,
                user="alice",
                reason_for_change="Canonical boundary revision",
                deleted=False,
                date_created="2024-01-31T17:59:59-06:00",
            ),
            RecordRevision(
                study_key=None,
                record_revision_id=None,
                record_id=None,
                record_oid="UNKEYED-1",
                form_key="NOTES",
                record_revision=1,
                data_revision=1,
                user="bob",
                reason_for_change="Null revision id 1",
                deleted=False,
                date_created="2024-02-01T00:00:00Z",
            ),
        ]
        next_batch = [
            RecordRevision(
                study_key=None,
                record_revision_id=None,
                record_id=None,
                record_oid="UNKEYED-2",
                form_key="NOTES",
                record_revision=1,
                data_revision=1,
                user="carol",
                reason_for_change="Null revision id 2",
                deleted=False,
                date_created="2024-02-02T00:00:00Z",
            ),
        ]
        with patch.object(
            DataExtractionWorkflow,
            "extract_audit_trail",
            side_effect=[overlapping_batch, next_batch],
        ):
            assert workflow.ingest_revisions("STUDY", mode="append") == 1
            assert workflow.ingest_revisions("STUDY", mode="append") == 1

        rows = workflow._connection.execute("""
            SELECT record_revision_id, record_oid, reason_for_change, date_created
            FROM bronze_revisions
            ORDER BY date_created
            """).fetchall()
        assert rows == [
            (501, None, "Canonical boundary revision", datetime(2024, 1, 31, 23, 59, 59)),
            (None, "UNKEYED-1", "Null revision id 1", datetime(2024, 2, 1, 0, 0, 0)),
            (None, "UNKEYED-2", "Null revision id 2", datetime(2024, 2, 2, 0, 0, 0)),
        ]


def test_ingest_revisions_transactional_rollback_and_study_isolated_replace() -> None:
    """Test replace mode isolates target study and rolls back DELETE on INSERT failure."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    study_a_initial = [
        RecordRevision(
            study_key="STUDY_A",
            record_revision_id=101,
            record_id=1,
            form_key="DEMO",
            record_revision=1,
            user="alice",
            date_created="2024-01-01T00:00:00Z",
        )
    ]
    study_b_initial = [
        RecordRevision(
            study_key="STUDY_B",
            record_revision_id=201,
            record_id=2,
            form_key="VITALS",
            record_revision=1,
            user="bob",
            date_created="2024-01-02T00:00:00Z",
        )
    ]
    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        side_effect=[study_a_initial, study_b_initial],
    ):
        assert workflow.ingest_revisions("STUDY_A", mode="append") == 1
        assert workflow.ingest_revisions("STUDY_B", mode="append") == 1

    # Simulate INSERT failure during mode="replace" for STUDY_A; DELETE must roll back
    real_conn = workflow._connection
    spy_conn = MagicMock(wraps=real_conn)

    def fail_on_insert(query: str, *args: object, **kwargs: object) -> object:
        if "INSERT INTO bronze_revisions" in query:
            raise RuntimeError("Disk full on insert")
        return real_conn.execute(query, *args, **kwargs)

    spy_conn.execute.side_effect = fail_on_insert
    workflow._connection = spy_conn

    replacement_attempt = [
        RecordRevision(
            study_key="STUDY_A",
            record_revision_id=199,
            record_id=9,
            form_key="AE",
            record_revision=1,
            user="carol",
            date_created="2024-01-05T00:00:00Z",
        )
    ]
    with (
        patch.object(
            DataExtractionWorkflow, "extract_audit_trail", return_value=replacement_attempt
        ),
        pytest.raises(RuntimeError, match="Disk full on insert"),
    ):
        workflow.ingest_revisions("STUDY_A", mode="replace")

    workflow._connection = real_conn
    preserved = real_conn.execute(
        "SELECT study_key, record_revision_id FROM bronze_revisions ORDER BY study_key"
    ).fetchall()
    assert preserved == [("STUDY_A", 101), ("STUDY_B", 201)]

    # Successful non-empty replace on STUDY_A preserves STUDY_B
    with patch.object(
        DataExtractionWorkflow, "extract_audit_trail", return_value=replacement_attempt
    ):
        assert workflow.ingest_revisions("STUDY_A", mode="replace") == 1

    after_replace = real_conn.execute(
        "SELECT study_key, record_revision_id FROM bronze_revisions ORDER BY study_key"
    ).fetchall()
    assert after_replace == [("STUDY_A", 199), ("STUDY_B", 201)]

    # Empty replace on STUDY_A clears ONLY STUDY_A while keeping STUDY_B intact
    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=[]):
        assert workflow.ingest_revisions("STUDY_A", mode="replace") == 0

    after_empty_replace = real_conn.execute(
        "SELECT study_key, record_revision_id FROM bronze_revisions ORDER BY study_key"
    ).fetchall()
    assert after_empty_replace == [("STUDY_B", 201)]
    workflow.close()


def test_build_silver_view_excludes_deleted_partitions_oid_and_preserves_multi_study() -> None:
    """Test silver views exclude voided records, partition null record_id by record_oid, and isolate studies."""
    pytest.importorskip("duckdb")
    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")

    study_a_revisions = [
        # Record 10: active in rev 1, voided (deleted=True) in rev 2 -> must NOT resurface rev 1
        RecordRevision(
            study_key="STUDY-A",
            record_revision_id=1,
            record_id=10,
            record_oid="REC-10",
            form_key="DEMO",
            record_revision=1,
            data_revision=1,
            deleted=False,
            date_created="2024-01-01T10:00:00Z",
        ),
        RecordRevision(
            study_key="STUDY-A",
            record_revision_id=2,
            record_id=10,
            record_oid="REC-10",
            form_key="DEMO",
            record_revision=2,
            data_revision=1,
            deleted=True,
            reason_for_change="Voided duplicate subject",
            date_created="2024-01-02T10:00:00Z",
        ),
        # Record with record_id=None partitioned by record_oid="OID-ONLY-1"
        RecordRevision(
            study_key="STUDY-A",
            record_revision_id=3,
            record_id=None,
            record_oid="OID-ONLY-1",
            form_key="VITALS",
            record_revision=1,
            data_revision=1,
            user="crc1",
            deleted=False,
            date_created="2024-01-03T10:00:00Z",
        ),
        RecordRevision(
            study_key="STUDY-A",
            record_revision_id=4,
            record_id=None,
            record_oid="OID-ONLY-1",
            form_key="VITALS",
            record_revision=2,
            data_revision=2,
            user="pi1",
            deleted=False,
            date_created="2024-01-04T10:00:00Z",
        ),
        # Distinct record with record_id=None and different record_oid="OID-ONLY-2"
        RecordRevision(
            study_key="STUDY-A",
            record_revision_id=5,
            record_id=None,
            record_oid="OID-ONLY-2",
            form_key="LABS",
            record_revision=1,
            data_revision=1,
            user="crc2",
            deleted=False,
            date_created="2024-01-05T10:00:00Z",
        ),
    ]
    study_b_revisions = [
        RecordRevision(
            study_key="PROT:202",
            record_revision_id=10,
            record_id=10,
            record_oid="REC-10",
            form_key="AE",
            record_revision=1,
            data_revision=1,
            user="site_b",
            deleted=False,
            date_created="2024-01-06T08:00:00-06:00",
        )
    ]

    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        side_effect=[study_a_revisions, study_b_revisions],
    ):
        assert workflow.ingest_revisions("STUDY-A", mode="append") == 5
        assert workflow.ingest_revisions("PROT:202", mode="append") == 1

    workflow.build_silver_view("STUDY-A")
    workflow.build_silver_view("PROT:202")

    study_a_silver = workflow._connection.execute("""
        SELECT record_revision_id, record_id, record_oid, record_revision, "user"
        FROM "silver_current_state_STUDY-A"
        ORDER BY record_revision_id
        """).fetchall()
    assert study_a_silver == [
        (4, None, "OID-ONLY-1", 2, "pi1"),
        (5, None, "OID-ONLY-2", 1, "crc2"),
    ]

    study_b_silver = workflow._connection.execute("""
        SELECT study_key, record_revision_id, record_id, date_created
        FROM "silver_current_state_PROT:202"
        """).fetchall()
    assert study_b_silver == [("PROT:202", 10, 10, datetime(2024, 1, 6, 14, 0, 0))]

    combined_silver = workflow._connection.execute("""
        SELECT study_key, record_revision_id, deleted
        FROM silver_current_state
        ORDER BY study_key, record_revision_id
        """).fetchall()
    assert combined_silver == [
        ("PROT:202", 10, False),
        ("STUDY-A", 4, False),
        ("STUDY-A", 5, False),
    ]

    # Rebuilding STUDY-A with include_deleted=True retains voided rev 2 without overwriting PROT:202
    workflow.build_silver_view("STUDY-A", include_deleted=True)
    combined_with_deleted = workflow._connection.execute("""
        SELECT study_key, record_revision_id, deleted
        FROM silver_current_state
        ORDER BY study_key, record_revision_id
        """).fetchall()
    assert combined_with_deleted == [
        ("PROT:202", 10, False),
        ("STUDY-A", 2, True),
        ("STUDY-A", 4, False),
        ("STUDY-A", 5, False),
    ]

    combined_history = workflow._connection.execute("""
        SELECT study_key, record_revision_id, valid_from, valid_to, is_current
        FROM silver_records_history
        WHERE study_key = 'PROT:202' OR record_id = 10
        ORDER BY study_key, record_revision_id
        """).fetchall()
    assert combined_history == [
        ("PROT:202", 10, datetime(2024, 1, 6, 14, 0, 0), None, True),
        (
            "STUDY-A",
            1,
            datetime(2024, 1, 1, 10, 0, 0),
            datetime(2024, 1, 2, 10, 0, 0),
            False,
        ),
        ("STUDY-A", 2, datetime(2024, 1, 2, 10, 0, 0), None, True),
    ]
    workflow.close()


def test_duckdb_workflow_close_context_manager_and_persistent_reopen(tmp_path) -> None:
    """Test idempotent close(), context manager cleanup, and persistent multi-Study silver views."""
    duckdb = pytest.importorskip("duckdb")
    db_file = str(tmp_path / "centralized.duckdb")

    rev_a = [
        RecordRevision(
            study_key="STUDY_A",
            record_revision_id=1,
            record_id=100,
            form_key="DEMO",
            record_revision=1,
            date_created="2024-03-01T06:00:00-06:00",
        )
    ]
    rev_b = [
        RecordRevision(
            study_key="STUDY_B",
            record_revision_id=2,
            record_id=200,
            form_key="VITALS",
            record_revision=1,
            date_created="2024-03-02T07:00:00-06:00",
        )
    ]

    assert DuckDBIngestionWorkflow._optional_timestamp(20240115) == "20240115"

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=rev_a):
        with DuckDBIngestionWorkflow(MagicMock(), db_file) as wf1:
            assert wf1.ingest_revisions("STUDY_A", mode="append") == 1
            wf1.build_silver_view("STUDY_A")
            real_conn = wf1._connection
            spy_conn = MagicMock(wraps=real_conn)
            wf1._connection = spy_conn
        # Exiting context manager called close() once; subsequent close() is a no-op
        wf1.close()
        spy_conn.close.assert_called_once()

    # Verify rollback when DELETE fails during an empty mode="replace" run
    wf_err = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    real_mem = wf_err._connection
    spy_mem = MagicMock(wraps=real_mem)

    def fail_delete(query: str, *args: object, **kwargs: object) -> object:
        if "DELETE FROM bronze_revisions" in query:
            raise RuntimeError("Simulated delete failure")
        return real_mem.execute(query, *args, **kwargs)

    spy_mem.execute.side_effect = fail_delete
    wf_err._connection = spy_mem
    with (
        patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=[]),
        pytest.raises(RuntimeError, match="Simulated delete failure"),
    ):
        wf_err.ingest_revisions("STUDY_A", mode="replace")
    wf_err._connection = real_mem
    wf_err.close()

    with patch.object(DataExtractionWorkflow, "extract_audit_trail", return_value=rev_b):
        with DuckDBIngestionWorkflow(MagicMock(), db_file) as wf2:
            assert wf2.ingest_revisions("STUDY_B", mode="append") == 1
            wf2.build_silver_view("STUDY_B")

    # Reopen persistent DuckDB file in read_only mode after with-block closed connection
    conn = duckdb.connect(db_file, read_only=True)
    try:
        rows = conn.execute("""
            SELECT study_key, record_id, date_created
            FROM silver_current_state
            ORDER BY study_key
            """).fetchall()
        assert rows == [
            ("STUDY_A", 100, datetime(2024, 3, 1, 12, 0, 0)),
            ("STUDY_B", 200, datetime(2024, 3, 2, 13, 0, 0)),
        ]
    finally:
        conn.close()


def test_ingest_revisions_deduplicates_keyed_appends_and_preserves_unkeyed() -> None:
    """Verify repeated append calls deduplicate keyed record_revision_id items while keeping unkeyed rows."""
    pytest.importorskip("duckdb")

    workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
    batch_one = [
        RecordRevision(
            study_key="STUDY",
            record_revision_id=101,
            record_id=10,
            record_oid="REC-10",
            form_key="F_VS",
            record_revision=1,
            data_revision=1,
            user="crc1",
            date_created="2026-11-15T18:10:00Z",
        ),
        # Duplicate keyed item inside the same batch
        RecordRevision(
            study_key="STUDY",
            record_revision_id=101,
            record_id=10,
            record_oid="REC-10",
            form_key="F_VS",
            record_revision=1,
            data_revision=1,
            user="crc1",
            date_created="2026-11-15T18:10:00Z",
        ),
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=None,
            record_id=99,
            record_oid="UNKEYED-99",
            form_key="F_DM",
            record_revision=1,
            data_revision=1,
            user="system",
            deleted=False,
            date_created="2026-11-15T18:00:00Z",
            last_updated=None,
        ),
    ]
    batch_two = [
        # Previously ingested keyed item
        RecordRevision(
            study_key="STUDY",
            record_revision_id=101,
            record_id=10,
            record_oid="REC-10",
            form_key="F_VS",
            record_revision=1,
            data_revision=1,
            user="crc1",
            date_created="2026-11-15T18:10:00Z",
        ),
        # New keyed item
        RecordRevision(
            study_key="STUDY",
            record_revision_id=102,
            record_id=10,
            record_oid="REC-10",
            form_key="F_VS",
            record_revision=2,
            data_revision=1,
            user="pi1",
            date_created="2026-11-15T18:35:45Z",
        ),
        # Second unkeyed fixture should still be staged
        SimpleNamespace(
            study_key="STUDY",
            record_revision_id=None,
            record_id=99,
            record_oid="UNKEYED-99",
            form_key="F_DM",
            record_revision=2,
            data_revision=2,
            user="system",
            deleted=False,
            date_created="2026-11-15T18:45:00Z",
            last_updated=None,
        ),
    ]

    with patch.object(
        DataExtractionWorkflow,
        "extract_audit_trail",
        side_effect=[batch_one, batch_two],
    ):
        inserted_first = workflow.ingest_revisions("STUDY", mode="append")
        inserted_second = workflow.ingest_revisions("STUDY", mode="append")

    assert inserted_first == 2
    assert inserted_second == 2

    rev_rows = workflow._connection.execute(
        "SELECT record_revision_id, record_id, record_revision "
        "FROM record_revisions ORDER BY record_id, record_revision"
    ).fetchall()
    assert rev_rows == [
        (101, 10, 1),
        (102, 10, 2),
        (None, 99, 1),
        (None, 99, 2),
    ]


def test_build_silver_view_creates_record_revisions_and_scd2_history_views_with_valid_bounds() -> (
    None
):
    """Verify date-bounded ingest_revisions passes validate_filter_syntax and builds SCD Type 2 views."""
    pytest.importorskip("duckdb")
    import respx

    from imednet import ImednetSDK
    from imednet.utils.filters import validate_filter_syntax

    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://api.test/api/v1/edc/studies/STUDY/recordRevisions").respond(
            200,
            json={
                "data": [
                    {
                        "studyKey": "STUDY",
                        "recordRevisionId": 9001,
                        "recordId": 601,
                        "recordOid": "REC-601",
                        "formKey": "F_VS",
                        "recordRevision": 1,
                        "dataRevision": 1,
                        "recordStatus": "Complete",
                        "user": "crc_site201",
                        "dateCreated": "2026-11-15 18:10:00",
                        "lastUpdated": "2026-11-15 18:10:00",
                    },
                    {
                        "studyKey": "STUDY",
                        "recordRevisionId": 9002,
                        "recordId": 601,
                        "recordOid": "REC-601",
                        "formKey": "F_VS",
                        "recordRevision": 2,
                        "dataRevision": 1,
                        "recordStatus": "Verified",
                        "user": "pi_site201",
                        "dateCreated": "2026-11-15 18:35:45",
                        "lastUpdated": "2026-11-15 18:35:45",
                    },
                    {
                        "studyKey": "STUDY",
                        "recordRevisionId": 9003,
                        "recordId": 601,
                        "recordOid": "REC-601",
                        "formKey": "F_VS",
                        "recordRevision": 3,
                        "dataRevision": 2,
                        "recordStatus": "Complete",
                        "user": "crc_site201",
                        "dateCreated": "2026-12-05 09:00:00",
                        "lastUpdated": "2026-12-05 09:00:00",
                    },
                ]
            },
        )
        sdk = ImednetSDK(
            api_key="audit-key",  # pragma: allowlist secret
            security_key="audit-sec",  # pragma: allowlist secret
            base_url="https://api.test",
        )
        workflow = DuckDBIngestionWorkflow(sdk, ":memory:")
        ingested = workflow.ingest_revisions(
            "STUDY",
            start_date="2026-11-01",
            end_date="2026-11-30",
            mode="append",
        )
        assert ingested == 2

        sent_filter = route.calls.last.request.url.params["filter"]
        validate_filter_syntax(sent_filter)
        assert sent_filter == "dateCreated>=2026-11-01T00:00:00Z"

    workflow.build_silver_view("STUDY")

    rr_count = workflow._connection.execute("SELECT COUNT(*) FROM record_revisions").fetchone()[0]
    assert rr_count == 2

    current = workflow._connection.execute(
        "SELECT record_id, record_revision, record_status FROM silver_current_state"
    ).fetchall()
    assert current == [(601, 2, "Verified")]

    history = workflow._connection.execute(
        "SELECT record_revision, valid_from, valid_to, is_current "
        "FROM silver_records_history ORDER BY record_revision ASC"
    ).fetchall()
    assert history == [
        (
            1,
            datetime(2026, 11, 15, 18, 10, 0),
            datetime(2026, 11, 15, 18, 35, 45),
            False,
        ),
        (
            2,
            datetime(2026, 11, 15, 18, 35, 45),
            None,
            True,
        ),
    ]
