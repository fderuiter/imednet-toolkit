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
