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
            subject_id=big_id + 4,
            subject_oid="SUBJ-OID-9",
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
        "SELECT record_revision_id, record_id, subject_id, subject_oid, site_id, interval_id, "
        "record_status, date_created, last_updated FROM bronze_revisions"
    ).fetchone()
    assert result == (
        big_id,
        big_id + 1,
        big_id + 4,
        "SUBJ-OID-9",
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


def test_ingest_revisions_fractional_end_date_and_unkeyed_append_dedup() -> None:
    """Verify date-only end_date retains subsecond revisions and unkeyed revisions deduplicate across appends."""
    pytest.importorskip("duckdb")

    sdk = MagicMock()
    fractional_keyed = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=7001,
        recordId=71,
        recordOid="REC-71",
        formKey="F_VS",
        subjectId=8801,
        subjectOid="SUBJ-OID-8801",
        subjectKey="SUBJ-88",
        siteId=10,
        intervalId=20,
        recordStatus="Record Complete",
        recordRevision=1,
        dataRevision=1,
        user="crc_user",
        role="CRC",
        reasonForChange="Late-night fractional-second save",
        deleted=False,
        dateCreated="2026-01-31T23:59:59.750000Z",
        lastUpdated="2026-01-31T23:59:59.750000Z",
    )
    unkeyed_rev = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=None,
        recordId=72,
        recordOid="REC-72",
        formKey="F_DM",
        subjectId=8802,
        subjectOid="SUBJ-OID-8802",
        subjectKey="SUBJ-89",
        recordStatus="Verified",
        recordRevision=1,
        dataRevision=1,
        user="pi_user",
        role="PI",
        reasonForChange="Unkeyed sign-off",
        deleted=False,
        dateCreated="2026-01-31T23:59:59.250000Z",
        lastUpdated="2026-01-31T23:59:59.250000Z",
    )
    missing_date_rev = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=7002,
        recordId=73,
        recordOid="REC-73",
        formKey="F_AE",
        recordRevision=1,
        dataRevision=1,
        dateCreated=None,
    )
    out_of_range_rev = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=7003,
        recordId=74,
        recordOid="REC-74",
        formKey="F_AE",
        recordRevision=1,
        dataRevision=1,
        dateCreated="2026-02-01T00:00:00Z",
    )

    sdk.get_record_revisions.side_effect = [
        [fractional_keyed, unkeyed_rev, missing_date_rev, out_of_range_rev],
        [fractional_keyed, unkeyed_rev],
    ]

    with DuckDBIngestionWorkflow(sdk, ":memory:") as workflow:
        assert (
            workflow.ingest_revisions(
                "STUDY",
                start_date="2026-01-01",
                end_date="2026-01-31",
                mode="append",
            )
            == 2
        )
        assert (
            workflow.ingest_revisions(
                "STUDY",
                end_date="2026-01-31",
                mode="append",
            )
            == 0
        )
        workflow.build_silver_view("STUDY")

        rows = workflow._connection.execute(
            "SELECT record_id, subject_id, subject_oid, subject_key, date_created "
            "FROM silver_current_state ORDER BY record_id"
        ).fetchall()
        assert rows == [
            (
                71,
                8801,
                "SUBJ-OID-8801",
                "SUBJ-88",
                datetime(2026, 1, 31, 23, 59, 59, 750000),
            ),
            (
                72,
                8802,
                "SUBJ-OID-8802",
                "SUBJ-89",
                datetime(2026, 1, 31, 23, 59, 59, 250000),
            ),
        ]
