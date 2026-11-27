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


def test_ingest_revisions_cst_timestamps_and_inclusive_boundary_deduplication() -> None:
    """Test RecordRevision -06:00 timestamps and deduplication across inclusive extract_audit_trail bounds."""
    pytest.importorskip("duckdb")

    sdk = MagicMock()
    boundary_rev = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=901,
        recordId=42,
        recordOid="REC-42",
        formKey="VITALS",
        recordRevision=1,
        dataRevision=1,
        user="crc_user",
        reasonForChange="Initial entry in CST",
        deleted=False,
        dateCreated="2026-01-31T18:00:00-06:00",
        lastUpdated="2026-01-31T18:05:00-06:00",
    )
    feb_rev = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=902,
        recordId=42,
        recordOid="REC-42",
        formKey="VITALS",
        recordRevision=2,
        dataRevision=1,
        user="pi_user",
        reasonForChange="Investigator sign-off",
        deleted=False,
        dateCreated="2026-02-01T09:30:00-06:00",
        lastUpdated="2026-02-01T09:30:00-06:00",
    )
    sdk.get_record_revisions.side_effect = [
        [boundary_rev],
        [boundary_rev, feb_rev],
    ]

    with DuckDBIngestionWorkflow(sdk, ":memory:") as workflow:
        assert (
            workflow.ingest_revisions(
                "STUDY",
                start_date="2026-01-01",
                end_date="2026-02-01",
                mode="append",
            )
            == 1
        )
        assert (
            workflow.ingest_revisions(
                "STUDY",
                start_date="2026-02-01",
                end_date="2026-02-28",
                mode="append",
            )
            == 1
        )

        bronze_rows = workflow._connection.execute(
            "SELECT record_revision_id, date_created, last_updated "
            "FROM bronze_revisions ORDER BY record_revision_id"
        ).fetchall()
        assert bronze_rows == [
            (901, datetime(2026, 2, 1, 0, 0, 0), datetime(2026, 2, 1, 0, 5, 0)),
            (902, datetime(2026, 2, 1, 15, 30, 0), datetime(2026, 2, 1, 15, 30, 0)),
        ]


def test_silver_view_deleted_oid_multi_study_and_close_on_exception(tmp_path) -> None:
    """Test voided record exclusion, record_oid fallback, multi-Study views, and context manager exception exit."""
    duckdb = pytest.importorskip("duckdb")
    db_path = str(tmp_path / "workflow_test.duckdb")

    revisions_s1 = [
        RecordRevision(
            study_key="S1",
            record_revision_id=10,
            record_id=1,
            record_oid="OID-1",
            form_key="F1",
            record_revision=1,
            deleted=False,
            date_created="2026-01-01T00:00:00Z",
        ),
        RecordRevision(
            study_key="S1",
            record_revision_id=11,
            record_id=1,
            record_oid="OID-1",
            form_key="F1",
            record_revision=2,
            deleted=True,
            date_created="2026-01-02T00:00:00Z",
        ),
        RecordRevision(
            study_key="S1",
            record_revision_id=12,
            record_id=None,
            record_oid="OID-NULL-ID",
            form_key="F2",
            record_revision=1,
            deleted=False,
            date_created="2026-01-03T00:00:00Z",
        ),
        RecordRevision(
            study_key="S1",
            record_revision_id=13,
            record_id=None,
            record_oid="OID-NULL-ID",
            form_key="F2",
            record_revision=2,
            deleted=False,
            date_created="2026-01-04T00:00:00Z",
        ),
    ]
    revisions_s2 = [
        RecordRevision(
            study_key="S2",
            record_revision_id=20,
            record_id=1,
            record_oid="OID-1",
            form_key="F1",
            record_revision=1,
            deleted=False,
            date_created="2026-01-05T00:00:00Z",
        )
    ]

    with (
        patch.object(
            DataExtractionWorkflow,
            "extract_audit_trail",
            side_effect=[revisions_s1, revisions_s2],
        ),
        pytest.raises(RuntimeError, match="caller failure"),
    ):
        with DuckDBIngestionWorkflow(MagicMock(), db_path) as workflow:
            workflow.ingest_revisions("S1", mode="replace")
            workflow.ingest_revisions("S2", mode="append")
            workflow.build_silver_view("S1")
            workflow.build_silver_view("S2")
            raise RuntimeError("caller failure")

    # Connection must be closed despite exception inside with-block
    conn = duckdb.connect(db_path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT study_key, record_revision_id, record_oid "
            "FROM silver_current_state ORDER BY study_key, record_revision_id"
        ).fetchall()
        assert rows == [
            ("S1", 13, "OID-NULL-ID"),
            ("S2", 20, "OID-1"),
        ]
    finally:
        conn.close()


def test_duckdb_replace_study_isolation_phi_masking_and_history_transition_diffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Study-scoped replace, close() idempotency, default PHI masking, and silver_records_history LAG() diffs."""
    pytest.importorskip("duckdb")
    from imednet.models.study_config import StudyConfiguration

    study_cfg = StudyConfiguration(studyKey="STUDY_PHI", phiFields=["role"])
    monkeypatch.setattr(
        "imednet.integrations.enrichment.resolve_study_configuration",
        lambda sk: study_cfg if sk == "STUDY_PHI" else None,
    )

    if True:
        study_other = [
            RecordRevision(
                studyKey="STUDY_KEEP",
                recordRevisionId=100,
                recordId=1,
                recordOid="REC-KEEP",
                formKey="DM",
                subjectKey="SUBJ-KEEP",
                recordRevision=1,
                dataRevision=1,
                recordStatus="Record Complete",
                user="unconfigured_actor",
                role="CRC",
                reasonForChange="Unconfigured normal note",
                deleted=False,
                dateCreated="2026-01-01T08:00:00Z",
            )
        ]

        study_phi_revs = [
            RecordRevision(
                studyKey="STUDY_PHI",
                recordRevisionId=201,
                recordId=55,
                recordOid="REC-55",
                formKey="VS",
                subjectKey="SUBJ-55",
                recordRevision=1,
                dataRevision=1,
                recordStatus="Record Incomplete",
                user="crc_actor",
                role="Secret Coordinator Role",
                reasonForChange='Initial capture patientName="Jane Hidden"',
                deleted=False,
                dateCreated="2026-01-10T09:00:00Z",
            ),
            RecordRevision(
                studyKey="STUDY_PHI",
                recordRevisionId=202,
                recordId=55,
                recordOid="REC-55",
                formKey="VS",
                subjectKey="SUBJ-55",
                recordRevision=2,
                dataRevision=2,
                recordStatus="Record Complete",
                user="crc_actor",
                role="Secret Coordinator Role",
                reasonForChange="Updated systolic BP",
                deleted=False,
                dateCreated="2026-01-11T09:00:00Z",
            ),
            RecordRevision(
                studyKey="STUDY_PHI",
                recordRevisionId=203,
                recordId=55,
                recordOid="REC-55",
                formKey="VS",
                subjectKey="SUBJ-55",
                recordRevision=3,
                dataRevision=2,
                recordStatus="Verified",
                user="pi_actor",
                role="Secret PI Role",
                reasonForChange="21 CFR Part 11 sign-off",
                deleted=False,
                dateCreated="2026-01-12T09:00:00Z",
            ),
            RecordRevision(
                studyKey="STUDY_PHI",
                recordRevisionId=204,
                recordId=55,
                recordOid="REC-55",
                formKey="VS",
                subjectKey="SUBJ-55",
                recordRevision=4,
                dataRevision=2,
                recordStatus="Verified",
                user="dm_actor",
                role="Secret DM Role",
                reasonForChange="Administrative comment review",
                deleted=False,
                dateCreated="2026-01-13T09:00:00Z",
            ),
            RecordRevision(
                studyKey="STUDY_PHI",
                recordRevisionId=205,
                recordId=55,
                recordOid="REC-55",
                formKey="VS",
                subjectKey="SUBJ-55",
                recordRevision=5,
                dataRevision=2,
                recordStatus="Verified",
                user="dm_actor",
                role="Secret DM Role",
                reasonForChange="Duplicate visit voided",
                deleted=True,
                dateCreated="2026-01-14T09:00:00Z",
            ),
        ]

        workflow = DuckDBIngestionWorkflow(MagicMock(), ":memory:")
        try:
            with patch.object(
                DataExtractionWorkflow,
                "extract_audit_trail",
                side_effect=[study_other, study_phi_revs],
            ):
                assert workflow.ingest_revisions("STUDY_KEEP", mode="append") == 1
                assert workflow.ingest_revisions("STUDY_PHI", mode="replace") == 5

            # 1. STUDY_KEEP is preserved when STUDY_PHI runs with mode="replace"
            keep_row = workflow._connection.execute(
                'SELECT study_key, "user", role, reason_for_change '
                "FROM bronze_revisions WHERE study_key = 'STUDY_KEEP'"
            ).fetchone()
            assert keep_row == (
                "STUDY_KEEP",
                "unconfigured_actor",
                "CRC",
                "Unconfigured normal note",
            )

            # 2. Default mask_phi=True masks Study-configured `role` and inline PHI in `reason_for_change`
            #    while keeping unconfigured `user`, `subject_key`, and `deleted=True` tombstones intact
            phi_rows = workflow._connection.execute(
                'SELECT record_revision, subject_key, "user", role, reason_for_change, deleted '
                "FROM bronze_revisions WHERE study_key = 'STUDY_PHI' ORDER BY record_revision"
            ).fetchall()
            assert phi_rows[0][1] == "SUBJ-55"
            assert phi_rows[0][2] == "crc_actor"
            assert phi_rows[0][3] == "***MASKED***"
            assert "Jane Hidden" not in phi_rows[0][4]
            assert "***MASKED***" in phi_rows[0][4]
            assert phi_rows[4][5] is True

            # 3. silver_records_history computes accurate LAG() transition diff columns
            workflow.build_silver_view("STUDY_KEEP")
            workflow.build_silver_view("STUDY_PHI")
            diffs = workflow._connection.execute(
                "SELECT record_revision, prev_record_status, prev_data_revision, prev_deleted, "
                "data_changed, change_type, is_current "
                "FROM silver_records_history WHERE study_key = 'STUDY_PHI' ORDER BY record_revision"
            ).fetchall()
            assert diffs == [
                (1, None, None, None, True, "created", False),
                (2, "Record Incomplete", 1, False, True, "data_updated", False),
                (3, "Record Complete", 2, False, False, "status_changed", False),
                (4, "Verified", 2, False, False, "metadata_updated", False),
                (5, "Verified", 2, False, False, "deleted", True),
            ]

            # 4. Explicit mask_phi=False preserves raw Study-configured fields when requested
            with patch.object(
                DataExtractionWorkflow,
                "extract_audit_trail",
                return_value=[study_phi_revs[0]],
            ):
                workflow.ingest_revisions("STUDY_PHI", mode="replace", mask_phi=False)
            unmasked = workflow._connection.execute(
                "SELECT role, reason_for_change FROM bronze_revisions WHERE study_key = 'STUDY_PHI'"
            ).fetchone()
            assert unmasked is not None
            assert unmasked[0] == "Secret Coordinator Role"
            assert "Jane Hidden" in unmasked[1]
        finally:
            workflow.close()
            workflow.close()  # Idempotent close()

    def test_init_defaults_and_17_attribute_bronze_schema_preserved(self) -> None:
        """DuckDBIngestionWorkflow and AsyncDuckDBIngestionWorkflow keep db_path=':memory:' and 17 columns."""
        import inspect

        assert (
            inspect.signature(DuckDBIngestionWorkflow.__init__).parameters["db_path"].default
            == ":memory:"
        )
        assert (
            inspect.signature(AsyncDuckDBIngestionWorkflow.__init__).parameters["db_path"].default
            == ":memory:"
        )

        sdk = MagicMock()
        sdk.is_async = False
        with DuckDBIngestionWorkflow(sdk) as workflow:
            workflow._ensure_bronze_table()
            cols = [
                (row[1], row[2])
                for row in workflow._connection.execute(
                    "PRAGMA table_info('bronze_revisions')"
                ).fetchall()
            ]
            assert len(cols) == 17
            assert ("site_id", "BIGINT") in cols
            assert ("interval_id", "BIGINT") in cols

    def test_silver_records_history_window_baseline_and_initial_created_labels(self) -> None:
        """Unanchored revisions > 1 emit window_baseline with null data_changed and zero extra REST calls."""
        sdk = MagicMock()
        sdk.is_async = False
        bounded_revs = [
            RecordRevision(
                study_key="STUDY",
                record_revision_id=1002,
                record_id=10,
                record_revision=2,
                data_revision=1,
                record_status="Verified",
                deleted=False,
                date_created=datetime(2026, 1, 12, 9, 0, tzinfo=timezone.utc),
            ),
            RecordRevision(
                study_key="STUDY",
                record_revision_id=1003,
                record_id=10,
                record_revision=3,
                data_revision=2,
                record_status="Verified",
                deleted=False,
                date_created=datetime(2026, 1, 13, 9, 0, tzinfo=timezone.utc),
            ),
            RecordRevision(
                study_key="STUDY",
                record_revision_id=2001,
                record_id=20,
                record_revision=1,
                data_revision=1,
                record_status="Complete",
                deleted=False,
                date_created=datetime(2026, 1, 12, 10, 0, tzinfo=timezone.utc),
            ),
            RecordRevision.model_construct(
                study_key="STUDY",
                record_revision_id=3001,
                record_id=30,
                record_revision=None,
                data_revision=0,
                record_status="Incomplete",
                deleted=False,
                date_created=datetime(2026, 1, 12, 11, 0, tzinfo=timezone.utc),
            ),
        ]
        sdk.get_record_revisions.return_value = bounded_revs

        with DuckDBIngestionWorkflow(sdk) as workflow:
            inserted = workflow.ingest_revisions("STUDY", start_date="2026-01-12")
            assert inserted == 4
            workflow.build_silver_view("STUDY")
            assert sdk.get_record_revisions.call_count == 1

            rows = workflow._connection.execute(
                "SELECT record_id, record_revision, data_changed, change_type "
                "FROM silver_records_history WHERE study_key = 'STUDY' "
                "ORDER BY record_id ASC, record_revision ASC NULLS FIRST"
            ).fetchall()
            assert rows == [
                (10, 2, None, "window_baseline"),
                (10, 3, True, "data_updated"),
                (20, 1, True, "created"),
                (30, None, False, "created"),
            ]

    @pytest.mark.asyncio
    async def test_validate_chain_single_fetch_and_rollback_sync_and_async(self) -> None:
        """Sync and async ingest_revisions(validate_chain=True) fetch once and roll back tombstone violations."""
        rev_deleted = RecordRevision(
            study_key="STUDY",
            record_revision_id=502,
            record_id=50,
            record_revision=2,
            data_revision=1,
            record_status="Verified",
            deleted=True,
            date_created=datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc),
        )
        rev_resurrected = RecordRevision(
            study_key="STUDY",
            record_revision_id=505,
            record_id=50,
            record_revision=5,
            data_revision=1,
            record_status="Verified",
            deleted=False,
            date_created=datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        )

        # 1. Synchronous workflow
        sync_sdk = MagicMock()
        sync_sdk.is_async = False
        sync_sdk.get_record_revisions.return_value = [rev_deleted]
        with DuckDBIngestionWorkflow(sync_sdk) as sync_wf:
            assert sync_wf.ingest_revisions("STUDY", start_date="2026-01-10") == 1
            sync_sdk.get_record_revisions.reset_mock()
            sync_sdk.get_record_revisions.return_value = [rev_resurrected]
            with pytest.raises(ValueError, match="Irreversible deletion"):
                sync_wf.ingest_revisions(
                    "STUDY",
                    start_date="2026-01-15",
                    validate_chain=True,
                )
            assert sync_sdk.get_record_revisions.call_count == 1
            persisted = sync_wf._connection.execute(
                "SELECT record_revision, deleted FROM bronze_revisions WHERE study_key = 'STUDY'"
            ).fetchall()
            assert persisted == [(2, True)]

        # 2. Asynchronous workflow
        async_sdk = MagicMock()
        async_sdk.is_async = True
        async_sdk.get_record_revisions = AsyncMock(return_value=[rev_deleted])
        async with AsyncDuckDBIngestionWorkflow(async_sdk) as async_wf:
            assert await async_wf.ingest_revisions("STUDY", start_date="2026-01-10") == 1
            async_sdk.get_record_revisions.reset_mock()
            async_sdk.get_record_revisions.return_value = [rev_resurrected]
            with pytest.raises(ValueError, match="Irreversible deletion"):
                await async_wf.ingest_revisions(
                    "STUDY",
                    start_date="2026-01-15",
                    validate_chain=True,
                )
            assert async_sdk.get_record_revisions.call_count == 1
            persisted_async = async_wf._connection.execute(
                "SELECT record_revision, deleted FROM bronze_revisions WHERE study_key = 'STUDY'"
            ).fetchall()
            assert persisted_async == [(2, True)]

    @pytest.mark.asyncio
    async def test_training_origin_phi_redacts_mixed_case_and_site_id_sync_and_async(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Training base_url PHI config redacts recordOid, siteId, V_AGE, and aeTerm across sync/async DuckDB."""
        from imednet import AsyncImednetSDK, ImednetSDK
        from imednet.models.study_config import StudyConfiguration
        from imednet.simulation import SimulationState, create_simulation_transport
        from imednet_workflows.config_version_control import ConfigVersionStore

        monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
        monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
        monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(tmp_path / "origin_cfg" / "store.sqlite3"))

        state = SimulationState()
        transport = create_simulation_transport(state)
        trn_sdk = ImednetSDK(
            api_key="trn-api-key",  # pragma: allowlist secret
            security_key="trn-sec-key",  # pragma: allowlist secret
            base_url="https://edc.trn.imednetapi.com",
            transport=transport,
            simulation_mode=True,
        )
        ConfigVersionStore(sdk=trn_sdk).commit_config(
            "STUDY",
            StudyConfiguration(
                study_key="STUDY",
                phi_fields=["V_AGE", "aeTerm", "recordOid", "siteId", "subjectKey"],
            ),
            desc="Commit training PHI rules",
            sdk=trn_sdk,
        )

        rev = RecordRevision(
            study_key="STUDY",
            record_revision_id=801,
            record_id=601,
            record_oid="REC-OID-77",
            subject_key="SUBJ-77",
            site_id=201,
            interval_id=401,
            form_key="F_AE",
            record_status="Verified",
            record_revision=1,
            data_revision=1,
            user="crc_trn",
            role="CRC",
            reason_for_change="Captured V_AGE=61 aeTerm=Rash recordOid=REC-OID-77 siteId=201",
            deleted=False,
            date_created=datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc),
        )

        with trn_sdk, DuckDBIngestionWorkflow(trn_sdk) as sync_wf:
            with patch.object(
                DataExtractionWorkflow,
                "extract_audit_trail",
                return_value=[rev],
            ):
                assert sync_wf.ingest_revisions("STUDY") == 1
            row = sync_wf._connection.execute(
                "SELECT record_id, record_oid, subject_key, site_id, interval_id, reason_for_change "
                "FROM bronze_revisions WHERE study_key = 'STUDY'"
            ).fetchone()
            assert row is not None
            assert row[0] == 601
            assert row[1] == "***MASKED***"
            assert row[2] == "SUBJ-77"
            assert row[3] is None
            assert row[4] == 401
            for secret in ("61", "Rash", "REC-OID-77", "201"):
                assert secret not in row[5]

        trn_async_sdk = AsyncImednetSDK(
            api_key="trn-api-key",  # pragma: allowlist secret
            security_key="trn-sec-key",  # pragma: allowlist secret
            base_url="https://edc.trn.imednetapi.com",
            transport=transport,
            simulation_mode=True,
        )
        async with trn_async_sdk, AsyncDuckDBIngestionWorkflow(trn_async_sdk) as async_wf:
            with patch.object(
                AsyncDataExtractionWorkflow,
                "extract_audit_trail",
                AsyncMock(return_value=[rev]),
            ):
                assert await async_wf.ingest_revisions("STUDY") == 1
            async_row = async_wf._connection.execute(
                "SELECT record_oid, subject_key, site_id, reason_for_change "
                "FROM bronze_revisions WHERE study_key = 'STUDY'"
            ).fetchone()
            assert async_row is not None
            assert async_row[0] == "***MASKED***"
            assert async_row[1] == "SUBJ-77"
            assert async_row[2] is None
            for secret in ("61", "Rash", "REC-OID-77", "201"):
                assert secret not in async_row[3]

        # Unconfigured production origin must remain isolated from training PHI rules
        prod_sdk = ImednetSDK(
            api_key="prod-api-key",  # pragma: allowlist secret
            security_key="prod-sec-key",  # pragma: allowlist secret
            base_url="https://edc.prod.imednetapi.com",
            transport=transport,
            simulation_mode=True,
        )
        with prod_sdk, DuckDBIngestionWorkflow(prod_sdk) as prod_wf:
            with patch.object(
                DataExtractionWorkflow,
                "extract_audit_trail",
                return_value=[rev],
            ):
                assert prod_wf.ingest_revisions("STUDY") == 1
            prod_row = prod_wf._connection.execute(
                "SELECT record_oid, site_id FROM bronze_revisions WHERE study_key = 'STUDY'"
            ).fetchone()
            assert prod_row == ("REC-OID-77", 201)


@pytest.mark.asyncio
async def test_async_ingest_revisions_validate_chain_rolls_back_and_builds_window_baseline() -> (
    None
):
    """AsyncDuckDBIngestionWorkflow validates merged chains after one fetch and builds window_baseline."""
    pytest.importorskip("duckdb")
    from unittest.mock import AsyncMock

    from imednet_workflows.data_extraction import AsyncDataExtractionWorkflow
    from imednet_workflows.duckdb_centralizer import AsyncDuckDBIngestionWorkflow

    initial_window = [
        RecordRevision(
            record_revision_id=501,
            study_key="STUDY",
            record_id=55,
            form_key="DEMO",
            record_revision=3,
            deleted=False,
            user="alice",
            reason_for_change="Baseline rev 3",
            date_created="2024-03-01T10:00:00Z",
        ),
        RecordRevision(
            record_revision_id=502,
            study_key="STUDY",
            record_id=55,
            form_key="DEMO",
            record_revision=4,
            deleted=True,
            user="bob",
            reason_for_change="Voided",
            date_created="2024-03-02T10:00:00Z",
        ),
    ]
    resurrected = [
        RecordRevision(
            record_revision_id=503,
            study_key="STUDY",
            record_id=55,
            form_key="DEMO",
            record_revision=5,
            deleted=False,
            user="mallory",
            reason_for_change="Resurrected",
            date_created="2024-03-03T10:00:00Z",
        ),
    ]

    async with AsyncDuckDBIngestionWorkflow(MagicMock()) as async_wf:
        first_mock = AsyncMock(return_value=initial_window)
        with patch.object(AsyncDataExtractionWorkflow, "extract_audit_trail", first_mock):
            inserted = await async_wf.ingest_revisions(
                "STUDY",
                start_date="2024-03-01T00:00:00Z",
                validate_chain=True,
            )
        assert inserted == 2
        assert first_mock.await_count == 1

        second_mock = AsyncMock(return_value=resurrected)
        with patch.object(AsyncDataExtractionWorkflow, "extract_audit_trail", second_mock):
            with pytest.raises(ValueError, match="deleted"):
                await async_wf.ingest_revisions(
                    "STUDY",
                    start_date="2024-03-03T00:00:00Z",
                    validate_chain=True,
                )
        assert second_mock.await_count == 1
        assert (
            async_wf._connection.execute(
                "SELECT COUNT(*) FROM bronze_revisions WHERE study_key = 'STUDY'"
            ).fetchone()[0]
            == 2
        )

        await async_wf.build_silver_view("STUDY", include_deleted=True)
        rows = async_wf._connection.execute(
            "SELECT record_revision, change_type, data_changed "
            "FROM silver_records_history WHERE study_key = 'STUDY' ORDER BY record_revision"
        ).fetchall()
        assert rows == [(3, "window_baseline", None), (4, "deleted", False)]
