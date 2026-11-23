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

    sdk = ImednetSDK(
        client=Client(
            api_key="sim-key",  # pragma: allowlist secret
            security_key="sim-secret",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=SpyTransport(),
        )
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda **_kwargs: sdk)
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


def _write_text_and_int_dd(tmp_path: Path) -> Path:
    dd_path = tmp_path / "dd.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "formId": "301",
                        "variables": [
                            {"variableName": "V_AGE", "variableType": "integer", "required": "Yes"},
                            {"variableName": "V_NOTES", "variableType": "text"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return dd_path


def test_cli_records_create_dry_run_json_reports_masked_identifier_and_schema_errors(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 2: records create --dry-run --format json emits one v1 JSON envelope for identifier, masked, and schema errors."""
    _sdk, _state, requests = sim_sdk_and_requests
    dd_path = _write_text_and_int_dd(tmp_path)
    bad_records_csv = tmp_path / "bad_records.csv"
    bad_records_csv.write_text(
        f"form_id,subject_id,V_AGE,V_NOTES\nnot_int,bad_sub,not_num,{MASKED_SENTINEL}\n",
        encoding="utf-8",
    )

    res_create = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--file",
            str(bad_records_csv),
            "--form-key",
            "F_DM",
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_create.exit_code == 1
    assert res_create.stderr == ""
    env_create = json.loads(res_create.stdout)
    assert env_create["schemaVersion"] == "1.0"
    assert env_create["resource"] == "records_create"
    rep_create = env_create["items"][0]["validationReport"]
    assert rep_create["isValid"] is False
    rule_ids = {err["ruleId"] for err in rep_create["errors"]}
    assert {"invalid_identifier", "masked_phi", "type"} <= rule_ids
    assert not any(r.method == "POST" for r in requests)


def test_cli_records_import_dry_run_json_reports_masked_identifier_and_schema_errors(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 2: records import --dry-run --format json emits one v1 JSON envelope for identifier, masked, and schema errors."""
    _sdk, _state, requests = sim_sdk_and_requests
    dd_path = _write_text_and_int_dd(tmp_path)
    bad_records_csv = tmp_path / "bad_records.csv"
    bad_records_csv.write_text(
        f"form_id,subject_id,V_AGE,V_NOTES\nnot_int,bad_sub,not_num,{MASKED_SENTINEL}\n",
        encoding="utf-8",
    )

    res_import = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(bad_records_csv),
            "--form-key",
            "F_DM",
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_import.exit_code == 1
    assert res_import.stderr == ""
    env_import = json.loads(res_import.stdout)
    assert env_import["schemaVersion"] == "1.0"
    assert env_import["resource"] == "records_import"
    rep_import = env_import["items"][0]["validationReport"]
    assert rep_import["isValid"] is False
    rule_ids = {err["ruleId"] for err in rep_import["errors"]}
    assert {"invalid_identifier", "masked_phi", "type"} <= rule_ids
    assert not any(r.method == "POST" for r in requests)


def test_cli_subjects_register_dry_run_json_reports_site_masked_identifier_and_schema_errors(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 2: subjects register --dry-run --format json emits one v1 JSON envelope including Site errors."""
    _sdk, state, requests = sim_sdk_and_requests
    state.sites.append(
        {
            "studyKey": "STUDY",
            "siteId": 102,
            "siteName": "CLOSED_SITE",
            "siteEnrollmentStatus": "Enrollment Closed",
        }
    )
    dd_path = _write_text_and_int_dd(tmp_path)

    bad_subj_json = json.dumps(
        [
            {"formKey": "F_DM", "siteName": "UNKNOWN_SITE", "data": {"V_AGE": "bad"}},
            {
                "formKey": "F_DM",
                "siteName": "CLOSED_SITE",
                "subjectId": "not_int",
                "data": {"V_AGE": 30, "V_NOTES": MASKED_SENTINEL},
            },
        ]
    )
    res_reg = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--json",
            bad_subj_json,
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_reg.exit_code == 1
    assert res_reg.stderr == ""
    env_reg = json.loads(res_reg.stdout)
    assert env_reg["schemaVersion"] == "1.0"
    assert env_reg["resource"] == "subjects_register"
    rep_reg = env_reg["items"][0]["validationReport"]
    assert rep_reg["isValid"] is False
    rule_ids = {err["ruleId"] for err in rep_reg["errors"]}
    assert {
        "unknown_site",
        "ineligible_site",
        "invalid_identifier",
        "masked_phi",
        "type",
    } <= rule_ids
    assert not any(r.method == "POST" for r in requests)


def test_cli_live_records_create_keeps_stdout_empty_and_redacts_secrets_on_stderr(
    capsys: pytest.CaptureFixture[str],
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """Req 7 & Guardrail 4: Live records create failure keeps stdout empty and prints redacted diagnostics to stderr."""
    _sdk, _state, requests = sim_sdk_and_requests

    res_create = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "F_DM",
            "-d",
            f"V_NOTES={MASKED_SENTINEL}",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_create.exit_code == 1
    assert res_create.stdout == ""
    assert "Validation Error:" in res_create.stderr
    assert "sim-secret" not in res_create.stderr
    assert not any(r.method == "POST" for r in requests)


def test_cli_live_subjects_register_keeps_stdout_empty_and_writes_errors_to_stderr(
    capsys: pytest.CaptureFixture[str],
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """Req 7 & Guardrail 4: Live subjects register failure keeps stdout empty and prints diagnostics to stderr."""
    _sdk, _state, requests = sim_sdk_and_requests

    res_reg = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--form-key",
            "F_DM",
            "--site-name",
            "NO_SUCH_SITE",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_reg.exit_code == 1
    assert res_reg.stdout == ""
    assert "Validation Error:" in res_reg.stderr
    assert "sim-secret" not in res_reg.stderr
    assert not any(r.method == "POST" for r in requests)


def _write_text_only_dd(tmp_path: Path) -> Path:
    dd_path = tmp_path / "dd_text.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {"variableName": "LOT_CODE", "variableType": "text"},
                            {"variableName": "FLAG_TEXT", "variableType": "text"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return dd_path


def test_cli_records_create_preserves_text_strings_for_numeric_and_boolean_tokens(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 3 & Req 3/4: -d LOT_CODE=12345 -d FLAG_TEXT=true preserves str values on text Variables."""
    _sdk, _state, requests = sim_sdk_and_requests
    dd_path = _write_text_only_dd(tmp_path)

    res_create = _invoke_cli(
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
            "LOT_CODE=12345",
            "-d",
            "FLAG_TEXT=true",
            "--schema",
            str(dd_path),
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_create.exit_code == 0, res_create.stderr
    post_reqs = [r for r in requests if r.method == "POST"]
    assert len(post_reqs) == 1
    sent_create = json.loads(post_reqs[0].content)
    assert sent_create[0]["data"] == {"LOT_CODE": "12345", "FLAG_TEXT": "true"}
    assert isinstance(sent_create[0]["data"]["LOT_CODE"], str)
    assert isinstance(sent_create[0]["data"]["FLAG_TEXT"], str)


def test_cli_records_import_preserves_text_strings_with_form_key_default(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 3 & Req 3: records import on CSV without Form header + --form-key preserves str on text Variables."""
    _sdk, _state, requests = sim_sdk_and_requests
    dd_path = _write_text_only_dd(tmp_path)
    csv_no_form = tmp_path / "no_form_header.csv"
    csv_no_form.write_text(
        "subject_key,site_name,LOT_CODE,FLAG_TEXT\nSUB1,SITE,12345,true\n",
        encoding="utf-8",
    )

    res_import = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(csv_no_form),
            "--form-key",
            "F_DM",
            "--schema",
            str(dd_path),
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_import.exit_code == 0, res_import.stderr
    post_reqs = [r for r in requests if r.method == "POST"]
    assert len(post_reqs) == 1
    sent_import = json.loads(post_reqs[0].content)
    assert sent_import[0]["formKey"] == "F_DM"
    assert sent_import[0]["data"] == {"LOT_CODE": "12345", "FLAG_TEXT": "true"}


def test_cli_subjects_register_json_merges_data_and_subject_key_and_preserves_optional_ids(
    capsys: pytest.CaptureFixture[str],
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 6 & Req 9: subjects register with --json merges -d and --subject-key and preserves optional IDs."""
    from imednet.models.records import RegisterSubjectRequest

    # Guardrail 3: unset subjectKey is omitted when None
    minimal_dumped = RegisterSubjectRequest(formKey="F_DM", siteName="SITE").model_dump(
        by_alias=True
    )
    assert "subjectKey" not in minimal_dumped
    assert "subjectOid" not in minimal_dumped
    assert "subjectId" not in minimal_dumped
    assert "formId" not in minimal_dumped
    assert "siteId" not in minimal_dumped

    _sdk, _state, requests = sim_sdk_and_requests
    inline_json = json.dumps(
        {
            "formId": 301,
            "siteId": 101,
            "subjectOid": "OID-JSON-1",
            "subjectId": 290,
            "data": {"V_AGE": 36},
        }
    )
    res_json = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--json",
            inline_json,
            "--subject-key",
            "SUB-FROM-CLI-JSON",
            "-d",
            "V_GENDER=Female",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_json.exit_code == 0, res_json.stderr
    post_reqs = [r for r in requests if r.method == "POST"]
    assert len(post_reqs) == 1
    sent_json = json.loads(post_reqs[0].content)[0]
    assert sent_json["formId"] == 301
    assert sent_json["siteId"] == 101
    assert sent_json["subjectKey"] == "SUB-FROM-CLI-JSON"
    assert sent_json["subjectOid"] == "OID-JSON-1"
    assert sent_json["subjectId"] == 290
    assert sent_json["data"] == {"V_AGE": 36, "V_GENDER": "Female"}


def test_cli_subjects_register_file_merges_data_and_subject_key_and_preserves_optional_ids(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 6 & Req 9: subjects register with --file merges -d and --subject-key and preserves optional IDs."""
    _sdk, _state, requests = sim_sdk_and_requests
    subj_file = tmp_path / "subj_req.json"
    subj_file.write_text(
        json.dumps(
            [
                {
                    "formId": 301,
                    "siteId": 101,
                    "subjectOid": "OID-FILE-1",
                    "subjectId": 291,
                    "data": {"V_AGE": 41},
                }
            ]
        ),
        encoding="utf-8",
    )
    res_file = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--file",
            str(subj_file),
            "--subject-key",
            "SUB-FROM-CLI-FILE",
            "-d",
            "V_GENDER=Male",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_file.exit_code == 0, res_file.stderr
    post_reqs = [r for r in requests if r.method == "POST"]
    assert len(post_reqs) == 1
    sent_file = json.loads(post_reqs[0].content)[0]
    assert sent_file["formId"] == 301
    assert sent_file["siteId"] == 101
    assert sent_file["subjectKey"] == "SUB-FROM-CLI-FILE"
    assert sent_file["subjectOid"] == "OID-FILE-1"
    assert sent_file["subjectId"] == 291
    assert sent_file["data"] == {"V_AGE": 41, "V_GENDER": "Male"}


def test_cli_subjects_register_csv_file_merges_data_and_subject_key_and_preserves_optional_ids(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 6 & Req 9: subjects register with CSV --file merges -d and --subject-key and preserves optional IDs."""
    _sdk, _state, requests = sim_sdk_and_requests
    subj_csv = tmp_path / "subj_req.csv"
    subj_csv.write_text(
        "form_id,site_id,subject_oid,subject_id,V_AGE\n301,101,OID-CSV-1,292,44\n",
        encoding="utf-8",
    )
    res_csv = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--file",
            str(subj_csv),
            "--subject-key",
            "SUB-FROM-CLI-CSV",
            "-d",
            "V_GENDER=Female",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_csv.exit_code == 0, res_csv.stderr
    csv_post_reqs = [r for r in requests if r.method == "POST"]
    assert len(csv_post_reqs) == 1
    sent_csv = json.loads(csv_post_reqs[0].content)[0]
    assert sent_csv["formId"] == 301
    assert sent_csv["siteId"] == 101
    assert sent_csv["subjectKey"] == "SUB-FROM-CLI-CSV"
    assert sent_csv["subjectOid"] == "OID-CSV-1"
    assert sent_csv["subjectId"] == 292
    assert sent_csv["data"] == {"V_AGE": 44, "V_GENDER": "Female"}


def test_cli_parse_key_value_pairs_preserves_yes_no_and_slashed_datetime_strings(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """Single-choice yes/no and Date / Time variables stay strings when passed via -d."""
    _sdk, _state, requests = sim_sdk_and_requests
    dd_path = tmp_path / "dd_yn.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {"variableName": "YN_FLAG", "variableType": "yes/no"},
                            {"variableName": "STAMP", "variableType": "Date / Time"},
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
            "YN_FLAG=true",
            "-d",
            "STAMP=20260101",
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
    sent = json.loads(post_reqs[0].content)[0]
    assert sent["data"] == {"YN_FLAG": "true", "STAMP": "20260101"}
    assert isinstance(sent["data"]["YN_FLAG"], str)
    assert isinstance(sent["data"]["STAMP"], str)
