"""Unit tests for all 9 workflow CLI subcommands and TriageStore CLI commands."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from imednet import cli
from imednet.models.jobs import Job
from imednet.models.queries import Query
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.spi.jobs import JobLedger


class CliResult:
    """Captured CLI execution result."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def invoke_cli(args: list[str]) -> CliResult:
    """Execute imednet.cli.app with captured stdout/stderr and exit code."""
    out = io.StringIO()
    err = io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli.app(args)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return CliResult(code, out.getvalue(), err.getvalue())


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate credentials, JobLedger DB, and TriageStore DB per test."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test-api-key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-sec-key")
    monkeypatch.setenv("IMEDNET_JOB_LEDGER_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IMEDNET_TRIAGE_DB_PATH", str(tmp_path / "triage.db"))


@pytest.fixture
def mock_sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch CLI get_sdk to return a deterministic MagicMock."""
    sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=sdk))
    return sdk


def test_data_extraction_records_and_audit_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MagicMock,
) -> None:
    """Verify extract-records (and data-extraction alias) plus extract-audit v1.0 JSON output."""
    wf = MagicMock()
    wf.extract_records_by_criteria.return_value = [
        Record(studyKey="S1", recordId=10, subjectKey="SUB-1", formKey="DM")
    ]
    wf.extract_audit_trail.return_value = [
        RecordRevision(
            studyKey="S1",
            recordRevisionId=99,
            recordId=10,
            subjectKey="SUB-1",
            recordStatus="Record Complete",
            user="auditor",
        )
    ]
    monkeypatch.setattr("imednet_workflows.cli.DataExtractionWorkflow", MagicMock(return_value=wf))

    res_rec = invoke_cli(
        ["workflows", "data-extraction", "S1", "--record-filter", "form_key=DM", "--format", "json"]
    )
    assert res_rec.exit_code == 0
    envelope = json.loads(res_rec.stdout)
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["count"] == 1
    assert envelope["items"][0]["subjectKey"] == "SUB-1"

    res_aud = invoke_cli(
        ["workflows", "extract-audit", "S1", "--start-date", "2026-01-01", "--format", "json"]
    )
    assert res_aud.exit_code == 0
    aud_env = json.loads(res_aud.stdout)
    assert aud_env["schemaVersion"] == "1.0"
    assert aud_env["items"][0]["recordRevisionId"] == 99


def test_duckdb_centralizer_cli_success_and_missing_dep(
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MagicMock,
) -> None:
    """Verify duckdb-centralize JSON output and exit code 1 when duckdb is missing."""
    mock_wf = MagicMock()
    mock_wf.ingest_revisions.return_value = 7
    monkeypatch.setattr(
        "imednet_workflows.duckdb_centralizer.DuckDBIngestionWorkflow",
        MagicMock(return_value=mock_wf),
    )

    res = invoke_cli(
        ["workflows", "duckdb-centralize", "S1", "--mode", "replace", "--format", "json"]
    )
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["items"][0]["rows_ingested"] == 7
    assert payload["items"][0]["silver_view_built"] is True
    mock_wf.build_silver_view.assert_called_once_with("S1")

    monkeypatch.setattr(
        "imednet_workflows.duckdb_centralizer.DuckDBIngestionWorkflow",
        MagicMock(side_effect=ImportError("No module named 'duckdb'")),
    )
    res_missing = invoke_cli(["workflows", "duckdb-centralize", "S1"])
    assert res_missing.exit_code == 1
    assert "duckdb" in res_missing.stderr


def test_query_management_subcommands(
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MagicMock,
) -> None:
    """Verify queries open, by-subject, by-site, and counts CLI subcommands."""
    q_item = Query(
        studyKey="S1",
        annotationId=501,
        subjectKey="SUB-1",
        variable="AGE",
        description="Verify DOB",
        queryComments=[],
    )
    wf = MagicMock()
    wf.get_open_queries.return_value = [q_item]
    wf.get_queries_for_subject.return_value = [q_item]
    wf.get_queries_by_site.return_value = [q_item]
    wf.get_query_state_counts.return_value = {"Open": 3, "Closed": 5}
    monkeypatch.setattr("imednet_workflows.cli.QueryManagementWorkflow", MagicMock(return_value=wf))

    for cmd in (
        ["workflows", "queries", "open", "S1", "--format", "json"],
        ["workflows", "queries", "by-subject", "S1", "SUB-1", "--format", "json"],
        ["workflows", "queries", "by-site", "S1", "General Hospital", "--format", "json"],
    ):
        res = invoke_cli(cmd)
        assert res.exit_code == 0
        parsed = json.loads(res.stdout)
        assert parsed["schemaVersion"] == "1.0"
        assert parsed["items"][0]["annotationId"] == 501

    res_counts = invoke_cli(["workflows", "queries", "counts", "S1", "--format", "json"])
    assert res_counts.exit_code == 0
    counts_env = json.loads(res_counts.stdout)
    assert counts_env["count"] == 2
    assert {r["status"]: r["count"] for r in counts_env["items"]} == {"Open": 3, "Closed": 5}


def test_record_mapper_dataframe_sdtm_and_hierarchy(
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MagicMock,
    tmp_path: Path,
) -> None:
    """Verify map-records tabular, --sdtm-domain, and --hierarchy modes."""
    mapper = MagicMock()
    mapper.dataframe.return_value = pd.DataFrame([{"Subject": "SUB-1", "Age": 42}])
    mapper.sdtm_dataframe.return_value = pd.DataFrame([{"STUDYID": "S1", "DOMAIN": "AE"}])
    mapper.build_hierarchy.return_value = {"SUB-1": {"Baseline": {"DM": [{"AGE": 42}]}}}
    monkeypatch.setattr("imednet_workflows.cli.RecordMapper", MagicMock(return_value=mapper))

    csv_out = tmp_path / "mapped.csv"
    res_df = invoke_cli(
        ["workflows", "map-records", "S1", "--out-file", str(csv_out), "--format", "json"]
    )
    assert res_df.exit_code == 0
    assert csv_out.is_file()
    assert json.loads(res_df.stdout)["items"][0]["Subject"] == "SUB-1"

    res_sdtm = invoke_cli(
        ["workflows", "map-records", "S1", "--sdtm-domain", "AE", "--format", "json"]
    )
    assert res_sdtm.exit_code == 0
    assert json.loads(res_sdtm.stdout)["items"][0]["DOMAIN"] == "AE"

    res_hier = invoke_cli(["workflows", "map-records", "S1", "--hierarchy", "--format", "json"])
    assert res_hier.exit_code == 0
    assert "SUB-1" in json.loads(res_hier.stdout)["items"][0]


def test_record_update_and_register_subjects_persist_jobs_and_validate_usage(
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MagicMock,
    tmp_path: Path,
) -> None:
    """Verify update-records and register-subjects persist batch_ids to JobLedger and exit 2 on bad JSON."""
    upd_wf = MagicMock()
    upd_wf.create_or_update_records.return_value = Job(
        jobId="J-100", batchId="BATCH-UPD-1", state="PROCESSING"
    )
    monkeypatch.setattr(
        "imednet_workflows.cli.RecordUpdateWorkflow", MagicMock(return_value=upd_wf)
    )

    payload_file = tmp_path / "records.json"
    payload_file.write_text(
        json.dumps([{"formKey": "DM", "siteName": "Site A", "data": {"AGE": 34}}]),
        encoding="utf-8",
    )
    res_upd = invoke_cli(
        [
            "workflows",
            "update-records",
            "S1",
            "--payload-file",
            str(payload_file),
            "--format",
            "json",
        ]
    )
    assert res_upd.exit_code == 0
    assert json.loads(res_upd.stdout)["items"][0]["batchId"] == "BATCH-UPD-1"

    reg_wf = MagicMock()
    reg_wf.register_subjects.return_value = Job(
        jobId="J-200", batchId="BATCH-REG-1", state="COMPLETED"
    )
    monkeypatch.setattr(
        "imednet_workflows.cli.RegisterSubjectsWorkflow", MagicMock(return_value=reg_wf)
    )

    res_reg = invoke_cli(
        [
            "workflows",
            "register-subjects",
            "S1",
            "--form-key",
            "ENROLL",
            "--site-name",
            "Site A",
            "--subject-key",
            "SUB-900",
            "--data",
            '{"ARM": "A"}',
            "--format",
            "json",
        ]
    )
    assert res_reg.exit_code == 0
    assert json.loads(res_reg.stdout)["items"][0]["batchId"] == "BATCH-REG-1"

    # Both batch IDs must be persisted in the shared SQLite JobLedger
    persisted_ids = {entry.batch_id for entry in JobLedger().list_jobs("S1")}
    assert {"BATCH-UPD-1", "BATCH-REG-1"}.issubset(persisted_ids)

    # Usage error (malformed JSON) must exit 2
    bad_json = invoke_cli(["workflows", "update-records", "S1", "--payload", "{not-json"])
    assert bad_json.exit_code == 2


def test_workflows_subject_data_and_uat_inspect_subcommands(
    monkeypatch: pytest.MonkeyPatch,
    mock_sdk: MagicMock,
) -> None:
    """Verify workflows subject-data and uat-inspect / uat inspect subcommands."""
    subj_wf = MagicMock()
    dossier_mock = MagicMock()
    dossier_mock.model_dump.return_value = {"subject_key": "SUB-1", "records": []}
    subj_wf.get_all_subject_data.return_value = dossier_mock
    monkeypatch.setattr(
        "imednet_workflows.cli.SubjectDataWorkflow", MagicMock(return_value=subj_wf)
    )

    res_subj = invoke_cli(["workflows", "subject-data", "S1", "SUB-1"])
    assert res_subj.exit_code == 0
    subj_wf.get_all_subject_data.assert_called_once_with("S1", "SUB-1")

    snapshot = MagicMock()
    snapshot.study_key = "S1"
    snapshot.captured_at = datetime(2026, 6, 1, tzinfo=UTC)
    snapshot.forms = [MagicMock(form_key="DM")]
    snapshot.variables = [MagicMock(), MagicMock()]
    snapshot.intervals = [MagicMock()]
    snapshot.sites = [MagicMock(site_name="Site 1")]
    snapshot.enrollment_forms.return_value = snapshot.forms
    snapshot.scheduled_forms.return_value = []
    snapshot.unscheduled_forms.return_value = []
    snapshot.active_sites.return_value = snapshot.sites

    inspector = MagicMock()
    inspector.inspect.return_value = snapshot
    monkeypatch.setattr(
        "imednet_workflows.uat.StudySchemaInspector", MagicMock(return_value=inspector)
    )

    for cmd in (
        ["workflows", "uat-inspect", "S1", "--format", "json"],
        ["workflows", "uat", "inspect", "S1", "--force-refresh", "--format", "json"],
    ):
        res_insp = invoke_cli(cmd)
        assert res_insp.exit_code == 0
        parsed = json.loads(res_insp.stdout)
        assert parsed["schemaVersion"] == "1.0"
        assert parsed["items"][0]["forms_count"] == 1
        assert parsed["items"][0]["enrollment_forms"] == ["DM"]


def test_triage_cli_full_lifecycle_and_transition_validation(tmp_path: Path) -> None:
    """Verify triage add, list, show, assign, annotate, valid transition, and invalid transition exit 1."""
    db_arg = ["--db-path", str(tmp_path / "cli_triage.db")]

    res_add = invoke_cli(
        [
            "workflows",
            "triage",
            "add",
            "STUDY_T",
            "ITEM-001",
            "--severity",
            "HIGH",
            "--comment",
            "Initial discrepancy flagged",
            *db_arg,
            "--format",
            "json",
        ]
    )
    assert res_add.exit_code == 0
    created = json.loads(res_add.stdout)["items"][0]
    assert created["item_id"] == "ITEM-001"
    assert created["status"] == "NEW"

    # Invalid self-transition NEW -> NEW must fail with exit code 1
    res_bad = invoke_cli(
        [
            "workflows",
            "triage",
            "transition",
            "ITEM-001",
            "NEW",
            "--comment",
            "Invalid self transition",
            *db_arg,
        ]
    )
    assert res_bad.exit_code == 1
    assert "transition" in res_bad.stderr.lower() or "error" in res_bad.stderr.lower()

    # Empty comment must fail with usage error exit code 2
    res_empty_comment = invoke_cli(
        [
            "workflows",
            "triage",
            "transition",
            "ITEM-001",
            "UNDER_REVIEW",
            "--comment",
            "   ",
            *db_arg,
        ]
    )
    assert res_empty_comment.exit_code == 2

    # Valid transition NEW -> UNDER_REVIEW -> RESOLVED
    res_rev = invoke_cli(
        [
            "workflows",
            "triage",
            "transition",
            "ITEM-001",
            "UNDER_REVIEW",
            "--comment",
            "Investigating source CRF",
            "--user-id",
            "cra_jane",
            *db_arg,
            "--format",
            "json",
        ]
    )
    assert res_rev.exit_code == 0
    assert json.loads(res_rev.stdout)["items"][0]["status"] == "UNDER_REVIEW"

    assert (
        invoke_cli(["workflows", "triage", "assign", "ITEM-001", "dm_lead", *db_arg]).exit_code == 0
    )
    assert (
        invoke_cli(
            ["workflows", "triage", "annotate", "ITEM-001", "--comment", "Verified lab", *db_arg]
        ).exit_code
        == 0
    )

    res_resolved = invoke_cli(
        [
            "workflows",
            "triage",
            "transition",
            "ITEM-001",
            "RESOLVED",
            "--comment",
            "Query closed with site confirmation",
            *db_arg,
            "--format",
            "json",
        ]
    )
    assert res_resolved.exit_code == 0
    assert json.loads(res_resolved.stdout)["items"][0]["status"] == "RESOLVED"

    # Invalid transition RESOLVED -> NEW must fail with exit code 1
    res_bad_resolved = invoke_cli(
        [
            "workflows",
            "triage",
            "transition",
            "ITEM-001",
            "NEW",
            "--comment",
            "Cannot jump directly to NEW",
            *db_arg,
        ]
    )
    assert res_bad_resolved.exit_code == 1

    res_list = invoke_cli(
        [
            "workflows",
            "triage",
            "list",
            "STUDY_T",
            "--status",
            "RESOLVED",
            *db_arg,
            "--format",
            "json",
        ]
    )
    assert res_list.exit_code == 0
    assert json.loads(res_list.stdout)["count"] == 1

    res_show = invoke_cli(["workflows", "triage", "show", "ITEM-001", *db_arg, "--format", "json"])
    assert res_show.exit_code == 0
    shown = json.loads(res_show.stdout)["items"][0]
    assert shown["assignee"] == "dm_lead"
    assert len(shown["annotations"]) == 2
    assert len(shown["history"]) == 2
