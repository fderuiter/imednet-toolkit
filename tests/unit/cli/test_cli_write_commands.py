"""Unit tests for CLI write commands: records create, records import, and subjects register."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest

from imednet import ImednetSDK, cli
from imednet.core.client import Client
from imednet.core.context import reset_strict_mode_context, set_strict_mode_context
from imednet.simulation import SimulationState, SimulationTransport
from imednet.utils.pandas import MASKED_SENTINEL


class CliResult:
    """Captured CLI invocation result."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _invoke_cli(capsys: pytest.CaptureFixture[str], args: list[str]) -> CliResult:
    capsys.readouterr()
    exit_code = 0
    try:
        cli.app(args)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return CliResult(exit_code, captured.out, captured.err)


@pytest.fixture(autouse=True)
def reset_env_and_cli(monkeypatch: pytest.MonkeyPatch):
    """Provide clean CLI parser and credentials per test."""
    token = set_strict_mode_context(False)
    monkeypatch.setenv("IMEDNET_API_KEY", "sim-key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "sim-secret")  # pragma: allowlist secret
    importlib.reload(cli)
    try:
        yield
    finally:
        importlib.reload(cli)
        reset_strict_mode_context(token)


@pytest.fixture
def sim_sdk_and_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ImednetSDK, SimulationState, list[httpx.Request]]:
    """Patch get_sdk to return a SimulationTransport-backed ImednetSDK with request spy."""
    state = SimulationState()
    base_transport = SimulationTransport(state=state)
    requests: list[httpx.Request] = []

    class SpyTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return base_transport.handle_request(request)

    def _build_sdk() -> ImednetSDK:
        return ImednetSDK(
            client=Client(
                api_key="sim-key",  # pragma: allowlist secret
                security_key="sim-secret",  # pragma: allowlist secret
                base_url="https://edc.sim.imednet.com",
                transport=SpyTransport(),
            )
        )

    sdk = _build_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda **_kwargs: _build_sdk())
    return sdk, state, requests


def test_cli_write_commands_parse_cleanly_in_root_parser() -> None:
    """AC 4: records create, records import, and subjects register parse cleanly in cli/__init__.py."""
    parser = cli.get_parser()

    ns_create = parser.parse_args(
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "F_DM",
            "--subject-key",
            "SUB1",
            "-d",
            "V_AGE=45",
            "--dry-run",
            "--wait",
            "--no-interactive",
            "--format",
            "json",
        ]
    )
    assert ns_create.dry_run is True
    assert ns_create.wait is True
    assert ns_create.no_interactive is True
    assert ns_create.output_format == "json"

    ns_import = parser.parse_args(
        [
            "records",
            "import",
            "STUDY",
            "data.csv",
            "--schema",
            "api",
            "--dry-run",
            "--wait",
            "--timeout",
            "120",
            "--interval",
            "2",
            "--no-interactive",
            "--format",
            "json",
        ]
    )
    assert ns_import.file == "data.csv"
    assert ns_import.schema_source == "api"
    assert ns_import.timeout == 120.0
    assert ns_import.interval == 2.0

    ns_register = parser.parse_args(
        [
            "subjects",
            "register",
            "STUDY",
            "--site-name",
            "SITE",
            "--form-key",
            "F_DM",
            "--subject-key",
            "SUB-NEW",
            "--skip-existing",
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ]
    )
    assert ns_register.site_name == "SITE"
    assert ns_register.skip_existing is True
    assert ns_register.output_format == "json"


def test_cli_records_create_inline_json_and_wait(
    capsys: pytest.CaptureFixture[str],
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """records create submits record, polls when --wait is passed, and emits v1 JSON."""
    _sdk, state, requests = sim_sdk_and_requests
    before_count = len(state.get_records("STUDY"))

    res = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "F_DM",
            "--subject-key",
            "SUB1",
            "--site-name",
            "SITE",
            "-d",
            "V_AGE=48",
            "-d",
            "V_NOTES=baseline",
            "--wait",
            "--timeout",
            "5",
            "--interval",
            "0",
            "--no-interactive",
            "--format",
            "json",
        ],
    )

    assert res.exit_code == 0, res.stderr
    assert res.stderr == ""
    payload = json.loads(res.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "records_create"
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["dryRun"] is False
    assert item["totalRecords"] == 1
    assert item["allSuccessful"] is True
    assert len(item["jobs"]) == 1
    assert any(r.method == "POST" for r in requests)
    assert len(state.get_records("STUDY")) == before_count + 1


def test_cli_records_create_non_interactive_table_mode_has_no_ansi_spinner(
    capsys: pytest.CaptureFixture[str],
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """records create with --no-interactive in table mode outputs plain text without ANSI spinners."""
    res = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--json",
            '{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "data": {"V_AGE": 49}}',
            "--schema",
            "api",
            "--no-interactive",
        ],
    )

    assert res.exit_code == 0, res.stderr
    assert "\033[" not in res.stderr
    assert "BATCH-" in res.stdout


def test_cli_records_import_dry_run_with_schema_sends_no_post_request(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """records import --dry-run --schema validates locally without sending POST /records."""
    _sdk, _state, requests = sim_sdk_and_requests

    dd_path = tmp_path / "dd.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {
                                "variableName": "V_AGE",
                                "variableType": "integer",
                                "required": "Yes",
                                "extraAttributes": {"minValue": 18, "maxValue": 100},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    csv_path = tmp_path / "import.csv"
    csv_path.write_text(
        "form_key,subject_key,site_name,V_AGE\nF_DM,SUB1,SITE,55.0\n",
        encoding="utf-8",
    )

    res = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(csv_path),
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )

    assert res.exit_code == 0, res.stderr
    assert res.stderr == ""
    assert not any(r.method == "POST" for r in requests)
    payload = json.loads(res.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "records_import"
    assert payload["items"][0]["dryRun"] is True
    assert payload["items"][0]["totalRecords"] == 1
    assert payload["items"][0]["validationReport"]["isValid"] is True


def test_cli_records_import_dry_run_invalid_records_reports_all_errors_in_json(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """records import --dry-run --format json exits 1 with structured BatchValidationReport."""
    _sdk, _state, requests = sim_sdk_and_requests

    dd_path = tmp_path / "dd.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {
                                "variableName": "V_AGE",
                                "variableType": "integer",
                                "required": "Yes",
                                "extraAttributes": {"minValue": 18, "maxValue": 100},
                            },
                            {
                                "variableName": "V_CODE",
                                "variableType": "text",
                                "required": "Yes",
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    csv_path = tmp_path / "invalid.csv"
    csv_path.write_text(
        "form_key,subject_key,site_name,V_AGE\nF_DM,SUB1,SITE,10\n",
        encoding="utf-8",
    )

    res = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(csv_path),
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )

    assert res.exit_code == 1
    assert not any(r.method == "POST" for r in requests)
    payload = json.loads(res.stdout)
    report = payload["items"][0]["validationReport"]
    assert report["isValid"] is False
    assert report["errorCount"] == 2


def test_cli_records_import_rejects_masked_sentinel_with_nonzero_exit(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """records import fails cleanly before HTTP POST when CSV contains ***MASKED***."""
    _sdk, _state, requests = sim_sdk_and_requests
    csv_path = tmp_path / "masked.csv"
    csv_path.write_text(
        f"form_key,subject_key,site_name,V_NAME\nF_DM,SUB1,SITE,{MASKED_SENTINEL}\n",
        encoding="utf-8",
    )

    res = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(csv_path),
            "--no-interactive",
            "--format",
            "json",
        ],
    )

    assert res.exit_code != 0
    assert requests == []
    assert "MASKED" in res.stderr


def test_cli_subjects_register_skip_existing_and_json_output(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """subjects register validates sites, skips existing subjects, and outputs clean JSON."""
    _sdk, state, _requests = sim_sdk_and_requests
    before_subjects = len(state.get_subjects("STUDY"))

    csv_path = tmp_path / "subjects.csv"
    csv_path.write_text(
        "form_key,site_name,subject_key,V_AGE\nF_DM,SITE,SUB1,30\nF_DM,SITE,SUB-CLI-NEW,34\n",
        encoding="utf-8",
    )

    res = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--file",
            str(csv_path),
            "--skip-existing",
            "--wait",
            "--timeout",
            "5",
            "--interval",
            "0",
            "--no-interactive",
            "--format",
            "json",
        ],
    )

    assert res.exit_code == 0, res.stderr
    assert res.stderr == ""
    payload = json.loads(res.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "subjects_register"
    item = payload["items"][0]
    assert item["totalRecords"] == 1
    assert item["skippedCount"] == 1
    assert len(state.get_subjects("STUDY")) == before_subjects + 1


def test_cli_write_commands_exit_nonzero_and_scrub_phi_and_secrets_on_job_failures(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 6: records create/update/import and subjects register exit 1 on failed jobs and scrub PHI/secrets."""
    from imednet.errors import ServerError
    from imednet.models.jobs import JobStatus
    from imednet.utils.batch_coordinator import BatchSubmissionSummary
    from imednet.utils.job_poller import JobFailedError, JobPollSummary

    sdk, _state, _requests = sim_sdk_and_requests
    monkeypatch.setattr(sdk, "close", lambda: None)
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda **_kwargs: sdk)
    failed_status = JobStatus(
        jobId="JOB-FAIL",
        batchId="BATCH-FAIL",
        state="FAILED",
        progress=100,
        results=[
            {
                "recordId": 10,
                "status": "FAILED",
                "error": "Constraint failure api_key=sk-secret-999 ssn=123-45-6789",  # pragma: allowlist secret
                "patient_name": "John Doe",
                "token": "tok-super-secret",  # pragma: allowlist secret
            }
        ],
    )
    failed_summary = BatchSubmissionSummary(
        study_key="STUDY",
        jobs=[failed_status],
        chunk_sizes=[1],
        total_records=1,
        poll_summary=JobPollSummary(
            study_key="STUDY",
            results={},
            failures={
                "BATCH-FAIL": JobFailedError("Job BATCH-FAIL ended in state FAILED", failed_status)
            },
            elapsed_total=0.1,
        ),
        wait_for_completion=True,
    )

    monkeypatch.setattr(sdk.records, "create_batch", lambda *_a, **_kw: failed_summary)

    # 1. records create (JSON mode + table mode) exits 1 when summary.all_successful is False
    res_create_json = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--json",
            '{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "data": {"V_AGE": 40}}',
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_create_json.exit_code == 1
    parsed = json.loads(res_create_json.stdout)
    assert parsed["items"][0]["allSuccessful"] is False
    assert "sk-secret-999" not in res_create_json.stdout
    assert "tok-super-secret" not in res_create_json.stdout
    assert "123-45-6789" not in res_create_json.stdout
    assert "John Doe" not in res_create_json.stdout

    res_create_table = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--json",
            '{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "data": {"V_AGE": 40}}',
            "--no-interactive",
        ],
    )
    assert res_create_table.exit_code == 1
    assert "Batch write failed" in res_create_table.stderr
    assert "sk-secret-999" not in res_create_table.stderr
    assert "123-45-6789" not in res_create_table.stderr
    assert "John Doe" not in res_create_table.stderr

    # 1b. records update, records import, & subjects register also exit 1 against a FAILED Job summary
    res_update_json = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--record-id",
            "601",
            "-d",
            "V_AGE=40",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_update_json.exit_code == 1
    update_parsed = json.loads(res_update_json.stdout)
    assert update_parsed["items"][0]["allSuccessful"] is False
    assert "sk-secret-999" not in res_update_json.stdout
    assert "John Doe" not in res_update_json.stdout

    json_file = tmp_path / "records.json"
    json_file.write_text(
        json.dumps(
            [{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "data": {"V_AGE": 40}}]
        ),
        encoding="utf-8",
    )
    res_import_summary = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(json_file),
            "--no-interactive",
        ],
    )
    assert res_import_summary.exit_code == 1
    assert "Batch write failed" in res_import_summary.stderr
    assert "sk-secret-999" not in res_import_summary.stderr
    assert "123-45-6789" not in res_import_summary.stderr

    res_subj_summary = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--form-key",
            "F_DM",
            "--site-name",
            "SITE",
            "-d",
            "V_AGE=35",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_subj_summary.exit_code == 1
    subj_parsed = json.loads(res_subj_summary.stdout)
    assert subj_parsed["items"][0]["allSuccessful"] is False
    assert "sk-secret-999" not in res_subj_summary.stdout
    assert "John Doe" not in res_subj_summary.stdout

    # 2. records import & records update when --wait raises JobFailedError exit 1 with scrubbed stderr
    def raise_job_failed(*_a: object, **_kw: object) -> BatchSubmissionSummary:
        err = JobFailedError(
            "Job BATCH-FAIL ended in state FAILED",
            failed_status,
            poll_summary=failed_summary.poll_summary,
            partial_summary=failed_summary,
        )
        raise err

    monkeypatch.setattr(sdk.records, "create_batch", raise_job_failed)
    res_import = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(json_file),
            "--wait",
            "--no-interactive",
        ],
    )
    assert res_import.exit_code == 1
    assert "Job Error:" in res_import.stderr
    assert "accepted_batches=['BATCH-FAIL']" in res_import.stderr
    assert "sk-secret-999" not in res_import.stderr
    assert "123-45-6789" not in res_import.stderr
    assert "John Doe" not in res_import.stderr

    res_update_wait = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--record-id",
            "601",
            "-d",
            "V_AGE=40",
            "--wait",
            "--no-interactive",
        ],
    )
    assert res_update_wait.exit_code == 1
    assert "Job Error:" in res_update_wait.stderr
    assert "accepted_batches=['BATCH-FAIL']" in res_update_wait.stderr
    assert "sk-secret-999" not in res_update_wait.stderr
    assert "John Doe" not in res_update_wait.stderr

    # 3. subjects register when chunk 2 raises HTTP 503 mid-batch exits 1 with accepted batch context
    settled_b1 = JobStatus(jobId="J-1", batchId="BATCH-OK-1", state="COMPLETED", progress=100)
    mid_partial = BatchSubmissionSummary(
        study_key="STUDY",
        jobs=[settled_b1],
        chunk_sizes=[1],
        total_records=2,
        poll_summary=JobPollSummary(
            study_key="STUDY",
            results={"BATCH-OK-1": settled_b1},
            failures={},
            elapsed_total=0.05,
        ),
        unsubmitted_chunks=[
            [{"formKey": "F_DM", "siteName": "SITE", "data": {"ssn": "***MASKED***"}}]
        ],
        wait_for_completion=True,
    )

    def raise_mid_batch(*_a: object, **_kw: object) -> BatchSubmissionSummary:
        srv_err = ServerError(
            "503 Service Unavailable token=sec-tok dob=1990-01-01",
            status_code=503,
        )
        srv_err.partial_summary = mid_partial  # type: ignore[attr-defined]
        raise srv_err

    monkeypatch.setattr(sdk.records, "create_batch", raise_mid_batch)
    res_subj = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--form-key",
            "F_DM",
            "--site-name",
            "SITE",
            "-d",
            "V_AGE=35",
            "--wait",
            "--no-interactive",
        ],
    )
    assert res_subj.exit_code == 1
    assert "API Error:" in res_subj.stderr
    assert "BATCH-OK-1" in res_subj.stderr
    assert "unsubmitted_records=1" in res_subj.stderr
    assert "sec-tok" not in res_subj.stderr
    assert "1990-01-01" not in res_subj.stderr


def test_cli_records_update_targets_by_id_oid_visit_and_interval_with_reason(
    capsys: pytest.CaptureFixture[str],
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """Req 3: imednet records update updates by recordId, recordOid, visitId, or intervalName."""
    _sdk, state, requests = sim_sdk_and_requests

    # 1. Update by --record-id with --reason-for-change and --record-status
    res_id = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--record-id",
            "601",
            "-d",
            "V_AGE=51",
            "--record-status",
            "Record Complete",
            "--reason-for-change",
            "Corrected chart age",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_id.exit_code == 0, res_id.stderr
    post_body_id = json.loads([r for r in requests if r.method == "POST"][-1].content)
    assert post_body_id[0]["recordId"] == 601
    assert post_body_id[0]["recordStatus"] == "Record Complete"
    assert post_body_id[0]["reasonForChange"] == "Corrected chart age"
    assert post_body_id[0]["data"] == {"V_AGE": 51}

    # 2. Update by --record-oid
    res_oid = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--record-oid",
            "REC-601",
            "-d",
            "V_AGE=52",
            "--reason-for-change",
            "OID update",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_oid.exit_code == 0, res_oid.stderr

    # 3. Update by --visit-id + --form-key
    res_visit = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--form-key",
            "F_DM",
            "--visit-id",
            "401",
            "-d",
            "V_AGE=53",
            "--reason-for-change",
            "Visit update",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_visit.exit_code == 0, res_visit.stderr

    # 4. Update by --interval-name + --subject-key + --form-key
    res_interval = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--form-key",
            "F_DM",
            "--subject-key",
            "SUB1",
            "--interval-name",
            "Baseline",
            "-d",
            "V_AGE=54",
            "--reason-for-change",
            "Interval update",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_interval.exit_code == 0, res_interval.stderr

    rec_601 = next(r for r in state.get_records("STUDY") if r["recordId"] == 601)
    assert rec_601["recordData"]["V_AGE"] == 54
    assert rec_601["recordStatus"] == "Record Complete"
    revisions = state.get_record_revisions("STUDY")
    assert revisions[-1]["reasonForChange"] == "Interval update"

    # Missing target selector exits 1
    res_missing = _invoke_cli(
        capsys,
        ["records", "update", "STUDY", "-d", "V_AGE=55", "--no-interactive"],
    )
    assert res_missing.exit_code == 1


def test_cli_offline_dry_run_succeeds_without_credentials_or_http_requests(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 4: Offline --dry-run --schema <path> succeeds with unset credentials and zero HTTP traffic."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)
    monkeypatch.delenv("IMEDNET_SIMULATION_MODE", raising=False)
    monkeypatch.chdir(tmp_path)

    dd_path = tmp_path / "study_dd.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {"variableName": "V_AGE", "variableType": "integer"},
                            {"variableName": "V_CODE", "variableType": "text"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    csv_path = tmp_path / "records.csv"
    csv_path.write_text(
        "formKey,subjectKey,siteName,V_AGE,V_CODE\nF_DM,SUB1,SITE,42,007\n",
        encoding="utf-8",
    )

    res_import = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(csv_path),
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_import.exit_code == 0, res_import.stderr
    assert json.loads(res_import.stdout)["items"][0]["validationReport"]["isValid"] is True

    res_reg = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--form-key",
            "F_DM",
            "--site-name",
            "SITE",
            "--subject-key",
            "SUB-OFFLINE-1",
            "--skip-existing",
            "-d",
            "V_AGE=42",
            "-d",
            "V_CODE=007",
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_reg.exit_code == 0, res_reg.stderr
    assert json.loads(res_reg.stdout)["items"][0]["validationReport"]["isValid"] is True


def test_cli_dry_run_without_schema_loads_study_datadict_and_exits_one_on_unknown_keys(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """Req 5: --dry-run without --schema loads Study Data Dictionary and exits 1 on unknown Form/Variable."""
    _sdk, _state, requests = sim_sdk_and_requests

    # Unknown Form key
    res_bad_form = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "UNKNOWN_FORM",
            "--subject-key",
            "SUB1",
            "-d",
            "V_AGE=40",
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_bad_form.exit_code == 1
    assert not any(r.method == "POST" for r in requests)

    # Unknown Variable key on records import without --schema
    bad_csv = tmp_path / "bad_import.csv"
    bad_csv.write_text(
        "formKey,subjectKey,siteName,BAD_VAR\nF_DM,SUB1,SITE,123\n",
        encoding="utf-8",
    )
    res_bad_var = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(bad_csv),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_bad_var.exit_code == 1
    assert not any(r.method == "POST" for r in requests)

    # Unknown Variable key on form-less records update without --schema
    res_bad_update = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--record-id",
            "601",
            "-d",
            "NON_EXISTENT_VAR=1",
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_bad_update.exit_code == 1
    assert not any(r.method == "POST" for r in requests)


def test_cli_subjects_register_preserves_subject_key_and_text_data_strings(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """Req 6 & 7: --subject-key preserves subjectKey and -d keeps numeric Text Variable strings."""
    _sdk, state, requests = sim_sdk_and_requests

    dd_path = tmp_path / "dd_text.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {"variableName": "V_AGE", "variableType": "integer"},
                            {"variableName": "V_INITIALS", "variableType": "text"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    res = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--form-key",
            "F_DM",
            "--site-name",
            "SITE",
            "--subject-key",
            "SUB-PRESERVED-99",
            "-d",
            "V_AGE=35",
            "-d",
            "V_INITIALS=007",
            "--schema",
            str(dd_path),
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res.exit_code == 0, res.stderr
    post_reqs = [r for r in requests if r.method == "POST"]
    assert len(post_reqs) == 1
    sent = json.loads(post_reqs[0].content)
    assert sent[0]["subjectKey"] == "SUB-PRESERVED-99"
    assert sent[0]["data"]["V_AGE"] == 35
    assert sent[0]["data"]["V_INITIALS"] == "007"
    assert isinstance(sent[0]["data"]["V_INITIALS"], str)
    assert any(s["subjectKey"] == "SUB-PRESERVED-99" for s in state.get_subjects("STUDY"))


def test_cli_records_update_validation_guards_and_schema_cache_text_coercion(
    capsys: pytest.CaptureFixture[str],
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """records update enforces target/mutation guards and parse_key_value_pairs handles SchemaCache."""
    from imednet.cli.utils.write_helpers import parse_key_value_pairs
    from imednet.models.variables import Variable
    from imednet.validation.cache import SchemaCache

    cache = SchemaCache()
    cache.populate(
        [
            Variable(
                study_key="STUDY",
                form_id=301,
                form_key="F_DM",
                variable_name="V_CHART",
                variable_type="Text",
            )
        ],
        study_key="STUDY",
    )
    assert parse_key_value_pairs(
        ["V_CHART=0042"], schema=cache, form_id=301, study_key="STUDY"
    ) == {"V_CHART": "0042"}
    assert parse_key_value_pairs(["V_CHART=0042"], schema=cache, study_key="STUDY") == {
        "V_CHART": "0042"
    }

    missing_target = _invoke_cli(
        capsys,
        ["records", "update", "STUDY", "-d", "V_AGE=50", "--no-interactive"],
    )
    assert missing_target.exit_code == 1
    assert "target selector" in missing_target.stderr

    missing_mutation = _invoke_cli(
        capsys,
        ["records", "update", "STUDY", "--record-id", "601", "--no-interactive"],
    )
    assert missing_mutation.exit_code == 1
    assert "reason-for-change" in missing_mutation.stderr
