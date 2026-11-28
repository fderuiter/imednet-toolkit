"""Unit tests for `imednet workflows extract-audit-trail` and `ingest-revisions` CLI commands."""

from __future__ import annotations

import csv
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet.models.record_revisions import RecordRevision
from imednet.models.study_config import StudyConfiguration


class _Result:
    """CLI execution result."""

    def __init__(self, exit_code: int, stdout: str, stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _CliRunner:
    """Simple test runner for CLI invocations."""

    def invoke(self, app: Any, args: list[str]) -> _Result:
        out = io.StringIO()
        err = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                app(args)
        except SystemExit as exc:
            exit_code = (
                int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            )
        return _Result(exit_code, out.getvalue(), err.getvalue())


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMEDNET_API_KEY", "test-api-key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-security-key")  # pragma: allowlist secret


@pytest.fixture
def runner() -> _CliRunner:
    return _CliRunner()


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))
    return mock_sdk


def test_workflows_extract_audit_trail_cli_bounds_chain_and_export(
    runner: _CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify `imednet workflows extract-audit-trail` date bounds, chain validation, PHI masking, and export."""
    study_cfg = StudyConfiguration(studyKey="STUDY_WF", phiFields=["customIdentifier"])
    monkeypatch.setattr(
        "imednet_workflows.cli.resolve_study_configuration",
        lambda sk: study_cfg if sk == "STUDY_WF" else None,
    )

    rev2 = RecordRevision(
        studyKey="STUDY_WF",
        recordRevisionId=701,
        recordId=44,
        recordOid="REC-44",
        formKey="VS",
        subjectKey="SUBJ-44",
        recordRevision=2,
        dataRevision=1,
        recordStatus="Verified",
        user="pi_user",
        reasonForChange=(
            'Sign-off patientName="Hidden Person" '
            'payload={"variable": "customIdentifier", "value": "MRN-999"} '
            "https://edc.example.org/records?ssn=123-45-6789"
        ),
        deleted=False,
        dateCreated="2026-01-15T10:00:00Z",
    )
    rev3_deleted = RecordRevision(
        studyKey="STUDY_WF",
        recordRevisionId=702,
        recordId=44,
        recordOid="REC-44",
        formKey="VS",
        subjectKey="SUBJ-44",
        recordRevision=3,
        dataRevision=1,
        recordStatus="Verified",
        user="pi_user",
        reasonForChange="Voided duplicate entry",
        deleted=True,
        dateCreated="2026-01-16T10:00:00Z",
    )
    rev4_resurrected = RecordRevision(
        studyKey="STUDY_WF",
        recordRevisionId=703,
        recordId=44,
        recordOid="REC-44",
        formKey="VS",
        subjectKey="SUBJ-44",
        recordRevision=4,
        dataRevision=1,
        recordStatus="Verified",
        user="pi_user",
        reasonForChange="Invalid restore",
        deleted=False,
        dateCreated="2026-01-17T10:00:00Z",
    )

    extraction_wf = MagicMock()
    extraction_wf.extract_audit_trail.return_value = [rev2, rev3_deleted]
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow",
        MagicMock(return_value=extraction_wf),
    )

    # 1. Valid date-bounded partial slice with --validate-chain and JSON envelope
    res_json = runner.invoke(
        cli.app,
        [
            "workflows",
            "extract-audit-trail",
            "STUDY_WF",
            "--start-date",
            "2026-01-10",
            "--end-date",
            "2026-01-20",
            "-f",
            "user==pi_user",
            "--validate-chain",
            "--format",
            "json",
        ],
    )
    assert res_json.exit_code == 0, res_json.stderr
    payload = json.loads(res_json.stdout)
    assert payload["resource"] == "record_revisions"
    assert payload["count"] == 2
    assert "Hidden Person" not in res_json.stdout
    assert "MRN-999" not in res_json.stdout
    assert "123-45-6789" not in res_json.stdout

    # 2. Export to JSON file with --no-mask-phi
    export_file = tmp_path / "audit_slice.json"
    res_export = runner.invoke(
        cli.app,
        [
            "workflows",
            "extract-audit-trail",
            "STUDY_WF",
            "--start-date",
            "2026-01-10",
            "--no-mask-phi",
            "-o",
            str(export_file),
        ],
    )
    assert res_export.exit_code == 0, res_export.stderr
    exported_items = json.loads(export_file.read_text(encoding="utf-8"))
    assert len(exported_items) == 2
    assert "Hidden Person" in exported_items[0]["reasonForChange"]
    assert "MRN-999" in exported_items[0]["reasonForChange"]

    # 3. Corrupted chain (deleted=True -> deleted=False) fails --validate-chain with exit code 1
    extraction_wf.extract_audit_trail.return_value = [rev2, rev3_deleted, rev4_resurrected]
    res_bad = runner.invoke(
        cli.app,
        [
            "workflows",
            "extract-audit-trail",
            "STUDY_WF",
            "--start-date",
            "2026-01-10",
            "--validate-chain",
        ],
    )
    assert res_bad.exit_code == 1
    assert "Chain validation failed" in res_bad.stderr


def test_workflows_ingest_revisions_cli_duckdb_and_chain_guard(
    runner: _CliRunner,
    sdk: MagicMock,
    tmp_path: Path,
) -> None:
    """Verify `imednet workflows ingest-revisions` DuckDB persistence, replace isolation, chain guard, and --output."""
    duckdb = pytest.importorskip("duckdb")

    db_file = tmp_path / "cli_ingest.duckdb"
    study_a_revs = [
        RecordRevision(
            studyKey="STUDY_A",
            recordRevisionId=11,
            recordId=1,
            recordOid="REC-1",
            formKey="DM",
            subjectKey="S1",
            recordRevision=1,
            dataRevision=1,
            recordStatus="Record Incomplete",
            user="crc",
            reasonForChange='=CMD|Created {"patientName": "Secret Doe"}',
            deleted=False,
            dateCreated="2026-01-01T10:00:00Z",
        ),
        RecordRevision(
            studyKey="STUDY_A",
            recordRevisionId=12,
            recordId=1,
            recordOid="REC-1",
            formKey="DM",
            subjectKey="S1",
            recordRevision=2,
            dataRevision=1,
            recordStatus="Verified",
            user="pi",
            reasonForChange="PI sign-off",
            deleted=False,
            dateCreated="2026-01-02T10:00:00Z",
        ),
    ]
    study_b_revs = [
        RecordRevision(
            studyKey="STUDY_B",
            recordRevisionId=21,
            recordId=9,
            recordOid="REC-9",
            formKey="VS",
            subjectKey="S9",
            recordRevision=1,
            dataRevision=1,
            recordStatus="Record Complete",
            user="crc_b",
            reasonForChange="Initial",
            deleted=False,
            dateCreated="2026-01-03T10:00:00Z",
        )
    ]

    sdk.get_record_revisions.side_effect = [
        study_b_revs,
        study_a_revs,
        study_a_revs,
        study_a_revs,
    ]

    # Seed STUDY_B first
    res_b = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY_B",
            "--db-path",
            str(db_file),
            "--mode",
            "append",
            "--format",
            "json",
        ],
    )
    assert res_b.exit_code == 0, res_b.stderr

    # Ingest STUDY_A with --mode replace, --validate-chain, and --output JSON
    out_history = tmp_path / "study_a_history.json"
    res_a = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY_A",
            "--db-path",
            str(db_file),
            "--mode",
            "replace",
            "--validate-chain",
            "-o",
            str(out_history),
        ],
    )
    assert res_a.exit_code == 0, res_a.stderr
    exported = json.loads(out_history.read_text(encoding="utf-8"))
    assert len(exported) == 2
    assert exported[0]["change_type"] == "created"
    assert exported[1]["change_type"] == "status_changed"
    assert exported[1]["data_changed"] is False
    assert "Secret Doe" not in json.dumps(exported)

    # Also verify CSV export sanitizes spreadsheet formula prefixes
    out_csv = tmp_path / "study_a_history.csv"
    res_csv = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY_A",
            "--db-path",
            str(db_file),
            "-o",
            str(out_csv),
        ],
    )
    assert res_csv.exit_code == 0, res_csv.stderr
    with out_csv.open("r", encoding="utf-8") as fh:
        csv_rows = list(csv.DictReader(fh))
    assert len(csv_rows) == 2
    assert csv_rows[0]["reason_for_change"].startswith("'=CMD")

    # Confirm STUDY_B remained untouched by STUDY_A's --mode replace
    conn = duckdb.connect(str(db_file), read_only=True)
    try:
        studies = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT study_key FROM bronze_revisions ORDER BY study_key"
            ).fetchall()
        ]
        assert studies == ["STUDY_A", "STUDY_B"]
    finally:
        conn.close()

    # Invalid strict chain (starts at revision 2 without date bounds) aborts before mutating DuckDB
    sdk.get_record_revisions.side_effect = [[study_a_revs[1]]]
    res_invalid = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY_A",
            "--db-path",
            str(db_file),
            "--mode",
            "replace",
            "--validate-chain",
        ],
    )
    assert res_invalid.exit_code == 1
    assert "Chain validation failed" in res_invalid.stderr


def _make_rev(
    *,
    revision_id: int,
    record_id: int,
    record_revision: int,
    data_revision: int,
    record_status: str = "Complete",
    deleted: bool = False,
    date_created: str = "2026-01-10T10:00:00Z",
) -> RecordRevision:
    return RecordRevision(
        studyKey="STUDY",
        recordRevisionId=revision_id,
        recordId=record_id,
        recordOid=f"REC-{record_id}",
        formKey="F_VS",
        subjectKey=f"SUBJ-{record_id}",
        siteId=201,
        intervalId=401,
        recordRevision=record_revision,
        dataRevision=data_revision,
        recordStatus=record_status,
        user="crc_user",
        role="CRC",
        reasonForChange="Routine update",
        deleted=deleted,
        dateCreated=date_created,
        lastUpdated=date_created,
    )


def test_ingest_revisions_simulation_persists_default_lakehouse_and_skips_curses(
    runner: _CliRunner,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`ingest-revisions STUDY --simulation` writes ./audit_lakehouse.duckdb and skips curses."""
    duckdb = pytest.importorskip("duckdb")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    def _forbid_curses(**_kwargs: object) -> bool:
        raise AssertionError("ingest-revisions summary must not open interactive curses grid")

    monkeypatch.setattr("imednet.cli.utils.grid.run_interactive_grid", _forbid_curses)

    res = runner.invoke(cli.app, ["workflows", "ingest-revisions", "STUDY", "--simulation"])
    captured = capsys.readouterr()
    combined_out = res.stdout + captured.out
    assert res.exit_code == 0, res.stderr + captured.err
    assert "Found 1 ingested revisions:" in combined_out
    assert "./audit_lakehouse.duckdb" in combined_out

    default_db = tmp_path / "audit_lakehouse.duckdb"
    assert default_db.is_file()

    conn = duckdb.connect(str(default_db), read_only=True)
    try:
        for obj_name in (
            "bronze_revisions",
            "record_revisions",
            "silver_current_state",
            "silver_current_state_STUDY",
            "silver_records_history",
            "silver_records_history_STUDY",
        ):
            count_row = conn.execute(f'SELECT COUNT(*) FROM "{obj_name}"').fetchone()
            assert count_row is not None and count_row[0] > 0
    finally:
        conn.close()


def test_ingest_revisions_memory_db_requires_output_file(
    runner: _CliRunner,
    sdk: MagicMock,
    tmp_path: Path,
) -> None:
    """`ingest-revisions --db-path :memory:` exits 1 without -o and succeeds with -o."""
    pytest.importorskip("duckdb")

    res_missing_out = runner.invoke(
        cli.app,
        ["workflows", "ingest-revisions", "STUDY", "--db-path", ":memory:"],
    )
    assert res_missing_out.exit_code == 1
    assert res_missing_out.stdout == ""
    assert "--db-path" in res_missing_out.stderr
    assert "./audit_lakehouse.duckdb" in res_missing_out.stderr
    assert "-o" in res_missing_out.stderr or "--output" in res_missing_out.stderr
    sdk.get_record_revisions.assert_not_called()

    out_json = tmp_path / "ephemeral_history.json"
    sdk.get_record_revisions.return_value = [
        _make_rev(
            revision_id=101,
            record_id=1,
            record_revision=1,
            data_revision=1,
            date_created="2026-01-10T10:00:00Z",
        )
    ]
    res_with_out = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY",
            "--db-path",
            ":memory:",
            "-o",
            str(out_json),
        ],
    )
    assert res_with_out.exit_code == 0, res_with_out.stderr
    exported = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(exported) == 1
    assert exported[0]["change_type"] == "created"


def test_ingest_revisions_output_omits_deleted_by_default_and_keeps_with_flag(
    runner: _CliRunner,
    sdk: MagicMock,
    tmp_path: Path,
) -> None:
    """`-o` exports omit `deleted = True` rows by default and keep them with `--include-deleted`."""
    pytest.importorskip("duckdb")
    db_file = tmp_path / "deleted_filter.duckdb"
    revs = [
        _make_rev(
            revision_id=501,
            record_id=10,
            record_revision=1,
            data_revision=1,
            deleted=False,
            date_created="2026-01-10T08:00:00Z",
        ),
        _make_rev(
            revision_id=502,
            record_id=10,
            record_revision=2,
            data_revision=1,
            deleted=True,
            date_created="2026-01-11T08:00:00Z",
        ),
    ]
    sdk.get_record_revisions.side_effect = [revs, revs]

    out_default = tmp_path / "history_default.json"
    res_default = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY",
            "--db-path",
            str(db_file),
            "-o",
            str(out_default),
        ],
    )
    assert res_default.exit_code == 0, res_default.stderr
    rows_default = json.loads(out_default.read_text(encoding="utf-8"))
    assert [r["record_revision"] for r in rows_default] == [1]
    assert all(not r["deleted"] for r in rows_default)

    out_incl = tmp_path / "history_include_deleted.json"
    res_incl = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY",
            "--db-path",
            str(db_file),
            "--include-deleted",
            "-o",
            str(out_incl),
        ],
    )
    assert res_incl.exit_code == 0, res_incl.stderr
    rows_incl = json.loads(out_incl.read_text(encoding="utf-8"))
    assert [r["record_revision"] for r in rows_incl] == [1, 2]
    assert [r["deleted"] for r in rows_incl] == [False, True]


def test_ingest_revisions_validate_chain_fetches_once_and_rolls_back_tombstone_violation(
    runner: _CliRunner,
    sdk: MagicMock,
    tmp_path: Path,
) -> None:
    """Appending rev 5 deleted=False onto rev 2 deleted=True with --validate-chain fetches once and rolls back."""
    duckdb = pytest.importorskip("duckdb")
    db_file = tmp_path / "rollback_tombstone.duckdb"

    sdk.get_record_revisions.return_value = [
        _make_rev(
            revision_id=701,
            record_id=20,
            record_revision=2,
            data_revision=1,
            record_status="Verified",
            deleted=True,
            date_created="2026-01-10T12:00:00Z",
        )
    ]
    res_seed = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY",
            "--db-path",
            str(db_file),
            "--start-date",
            "2026-01-10",
            "--format",
            "json",
        ],
    )
    assert res_seed.exit_code == 0, res_seed.stderr

    sdk.get_record_revisions.reset_mock()
    sdk.get_record_revisions.return_value = [
        _make_rev(
            revision_id=705,
            record_id=20,
            record_revision=5,
            data_revision=1,
            record_status="Verified",
            deleted=False,
            date_created="2026-01-15T12:00:00Z",
        )
    ]
    res_append = runner.invoke(
        cli.app,
        [
            "workflows",
            "ingest-revisions",
            "STUDY",
            "--db-path",
            str(db_file),
            "--start-date",
            "2026-01-15",
            "--validate-chain",
        ],
    )
    assert res_append.exit_code == 1
    assert "Chain validation failed" in res_append.stderr
    assert sdk.get_record_revisions.call_count == 1

    conn = duckdb.connect(str(db_file), read_only=True)
    try:
        rows = conn.execute(
            "SELECT record_revision, deleted FROM bronze_revisions WHERE study_key = 'STUDY'"
        ).fetchall()
        assert rows == [(2, True)]
    finally:
        conn.close()


def test_workflow_cli_training_base_url_redacts_mixed_case_and_non_string_phi(
    runner: _CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Training base_url PHI config redacts V_AGE, aeTerm, recordOid, and siteId without warnings."""
    import warnings

    duckdb = pytest.importorskip("duckdb")
    from imednet import ImednetSDK
    from imednet.models.records import Record
    from imednet.models.study_config import StudyConfiguration
    from imednet.simulation import SimulationState, create_simulation_transport
    from imednet_workflows.config_version_control import ConfigVersionStore

    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(tmp_path / "cfg" / "versions.sqlite3"))

    state = SimulationState()
    transport = create_simulation_transport(state)
    trn_sdk = ImednetSDK(
        api_key="trn-api-key",  # pragma: allowlist secret
        security_key="trn-sec-key",  # pragma: allowlist secret
        base_url="https://edc.trn.imednetapi.com",
        transport=transport,
        simulation_mode=True,
    )
    trn_store = ConfigVersionStore(sdk=trn_sdk)
    trn_store.commit_config(
        "STUDY",
        StudyConfiguration(
            study_key="STUDY",
            phi_fields=["V_AGE", "aeTerm", "recordOid", "siteId", "subjectKey"],
        ),
        desc="Training PHI policy",
        sdk=trn_sdk,
    )

    sample_rec = Record.model_validate(
        {
            "studyKey": "STUDY",
            "recordId": 601,
            "recordOid": "OID-TRN-601",
            "subjectKey": "SUBJ-TRN-1",
            "siteId": 201,
            "formKey": "F_AE",
            "recordStatus": "Verified",
            "recordData": {"V_AGE": 52, "aeTerm": "Severe Vertigo", "SAFE_VAR": "ok"},
        }
    )
    sample_rev = RecordRevision.model_validate(
        {
            "recordRevisionId": 8801,
            "studyKey": "STUDY",
            "recordId": 601,
            "recordOid": "OID-TRN-601",
            "recordRevision": 2,
            "dataRevision": 1,
            "recordStatus": "Verified",
            "subjectKey": "SUBJ-TRN-1",
            "siteId": 201,
            "formKey": "F_AE",
            "intervalId": 401,
            "role": "CRC",
            "user": "crc_trn",
            "reasonForChange": "Updated V_AGE=52 aeTerm='Severe Vertigo' recordOid=OID-TRN-601 siteId=201",
            "deleted": False,
            "dateCreated": "2026-02-01T10:00:00Z",
            "lastUpdated": "2026-02-01T10:00:00Z",
        }
    )

    monkeypatch.setattr(trn_sdk, "get_records", lambda *a, **kw: [sample_rec])
    monkeypatch.setattr(
        trn_sdk,
        "get_subjects",
        lambda *a, **kw: (
            [{"studyKey": "STUDY", "subjectKey": "SUBJ-TRN-1", "siteId": 201}]
            if kw.get("subject_key") == "SUBJ-TRN-1"
            else []
        ),
    )
    monkeypatch.setattr(trn_sdk, "get_visits", lambda *a, **kw: [])
    monkeypatch.setattr(trn_sdk, "get_queries", lambda *a, **kw: [])
    monkeypatch.setattr(trn_sdk, "get_record_revisions", lambda *a, **kw: [sample_rev])
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda **_kw: trn_sdk)
    monkeypatch.setattr(trn_sdk, "close", lambda: None)

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")

        res_rec = runner.invoke(
            cli.app,
            ["workflows", "extract-records", "STUDY", "--format", "json"],
        )
        assert res_rec.exit_code == 0, res_rec.stderr
        rec_item = json.loads(res_rec.stdout)["items"][0]
        assert rec_item["recordOid"] == "***MASKED***"
        assert rec_item["siteId"] == "***MASKED***"
        assert rec_item["recordData"]["V_AGE"] == "***MASKED***"
        assert rec_item["recordData"]["aeTerm"] == "***MASKED***"
        assert rec_item["recordData"]["SAFE_VAR"] == "ok"
        assert rec_item["subjectKey"] == "SUBJ-TRN-1"
        assert rec_item["recordId"] == 601

        res_subj = runner.invoke(
            cli.app,
            ["subject-data", "STUDY", "SUBJ-TRN-1", "--format", "json"],
        )
        assert res_subj.exit_code == 0, res_subj.stderr
        subj_item = json.loads(res_subj.stdout)["items"][0]
        subj_rec = subj_item["records"][0]
        assert subj_rec["recordOid"] == "***MASKED***"
        assert subj_rec["siteId"] == "***MASKED***"
        assert subj_rec["recordData"]["V_AGE"] == "***MASKED***"
        assert subj_rec["recordData"]["aeTerm"] == "***MASKED***"
        assert subj_rec["subjectKey"] == "SUBJ-TRN-1"

        res_audit_json = runner.invoke(
            cli.app,
            ["workflows", "extract-audit-trail", "STUDY", "--format", "json"],
        )
        assert res_audit_json.exit_code == 0, res_audit_json.stderr
        audit_json_item = json.loads(res_audit_json.stdout)["items"][0]
        assert audit_json_item["recordOid"] == "***MASKED***"
        assert audit_json_item["siteId"] == "***MASKED***"
        assert audit_json_item["subjectKey"] == "SUBJ-TRN-1"
        for secret in ("52", "Severe Vertigo", "OID-TRN-601", "201"):
            assert secret not in audit_json_item["reasonForChange"]

        out_audit = tmp_path / "trn_audit.json"
        res_audit = runner.invoke(
            cli.app,
            ["workflows", "extract-audit-trail", "STUDY", "-o", str(out_audit)],
        )
        assert res_audit.exit_code == 0, res_audit.stderr
        audit_item = json.loads(out_audit.read_text(encoding="utf-8"))[0]
        assert audit_item["recordOid"] == "***MASKED***"
        assert audit_item["siteId"] == "***MASKED***"
        assert audit_item["subjectKey"] == "SUBJ-TRN-1"
        for secret in ("52", "Severe Vertigo", "OID-TRN-601", "201"):
            assert secret not in audit_item["reasonForChange"]

        db_file = tmp_path / "trn_lakehouse.duckdb"
        out_hist = tmp_path / "trn_history.json"
        res_ing = runner.invoke(
            cli.app,
            [
                "workflows",
                "ingest-revisions",
                "STUDY",
                "--db-path",
                str(db_file),
                "--start-date",
                "2026-02-01",
                "-o",
                str(out_hist),
            ],
        )
        assert res_ing.exit_code == 0, res_ing.stderr
        hist_item = json.loads(out_hist.read_text(encoding="utf-8"))[0]
        assert hist_item["change_type"] == "window_baseline"
        assert hist_item["data_changed"] is None
        assert hist_item["record_oid"] == "***MASKED***"
        assert hist_item["site_id"] == "***MASKED***"
        assert hist_item["subject_key"] == "SUBJ-TRN-1"
        for secret in ("52", "Severe Vertigo", "OID-TRN-601", "201"):
            assert secret not in hist_item["reason_for_change"]

    pydantic_warnings = [
        w
        for w in caught_warnings
        if "PydanticSerialization" in type(w.message).__name__ or "Pydantic" in str(w.message)
    ]
    assert pydantic_warnings == []

    conn = duckdb.connect(str(db_file), read_only=True)
    try:
        bronze_row = conn.execute(
            "SELECT record_oid, site_id, subject_key, reason_for_change FROM bronze_revisions"
        ).fetchone()
        assert bronze_row is not None
        assert bronze_row[0] == "***MASKED***"
        assert bronze_row[1] is None
        assert bronze_row[2] == "SUBJ-TRN-1"
        for secret in ("52", "Severe Vertigo", "OID-TRN-601", "201"):
            assert secret not in bronze_row[3]
    finally:
        conn.close()
