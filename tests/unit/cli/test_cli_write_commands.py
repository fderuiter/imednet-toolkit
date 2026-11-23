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


def test_cli_records_pipe_list_envelope_into_create_fails_exit_1(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 2 & Req 3: Piping `records list --format json` into `records create --file - --dry-run` exits 1."""
    import io

    list_res = _invoke_cli(
        capsys,
        ["records", "list", "STUDY", "--format", "json"],
    )
    assert list_res.exit_code == 0
    envelope = json.loads(list_res.stdout)
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["resource"] == "records"

    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kwargs: ImednetSDK(
            client=Client(
                api_key="sim-key",  # pragma: allowlist secret
                security_key="sim-secret",  # pragma: allowlist secret
                base_url="https://edc.sim.imednet.com",
                transport=SimulationTransport(state=sim_sdk_and_requests[1]),
            )
        ),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(list_res.stdout))
    create_res = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--file",
            "-",
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert create_res.exit_code == 1
    err_json = json.loads(create_res.stdout)
    assert err_json["schemaVersion"] == "1.0"
    assert err_json["status"] == "complete"
    assert err_json["items"][0]["allSuccessful"] is False
    assert "envelope" in err_json["items"][0]["error"]
    assert "Error:" in create_res.stderr


def test_cli_records_create_preserves_string_data_and_applies_form_key_before_coercion(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1 & Req 3/4: Preserve '1.0' with/without --schema, apply --form-key first, reject missing formKey."""
    state = SimulationState()
    base_transport = SimulationTransport(state=state)
    requests: list[httpx.Request] = []

    class _MultiCallSpyTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return base_transport.handle_request(request)

    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kwargs: ImednetSDK(
            client=Client(
                api_key="sim-key",  # pragma: allowlist secret
                security_key="sim-secret",  # pragma: allowlist secret
                base_url="https://edc.sim.imednet.com",
                transport=_MultiCallSpyTransport(),
            )
        ),
    )

    dd_path = tmp_path / "schema.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {"variableName": "V_NOTES", "variableType": "text"},
                            {"variableName": "V_AGE", "variableType": "integer"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # 1. Without --schema: -d V_NOTES=1.0 stays exact string "1.0"
    requests.clear()
    res_no_schema = _invoke_cli(
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
            "V_NOTES=1.0",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_no_schema.exit_code == 0, res_no_schema.stderr
    post_req_1 = next(r for r in requests if r.method == "POST")
    sent_1 = json.loads(post_req_1.content.decode("utf-8"))
    assert sent_1[0]["data"]["V_NOTES"] == "1.0"

    # 2. With --schema and CSV lacking formKey column: --form-key applied BEFORE coercion
    csv_no_fk = tmp_path / "no_form_key.csv"
    csv_no_fk.write_text(
        "subjectKey,siteName,V_NOTES,V_AGE\nSUB1,SITE,1.0,33.0\n", encoding="utf-8"
    )
    requests.clear()
    res_with_schema = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--file",
            str(csv_no_fk),
            "--form-key",
            "F_DM",
            "--schema",
            str(dd_path),
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_with_schema.exit_code == 0, res_with_schema.stderr
    post_req_2 = next(r for r in requests if r.method == "POST")
    sent_2 = json.loads(post_req_2.content.decode("utf-8"))
    assert sent_2[0]["formKey"] == "F_DM"
    assert sent_2[0]["data"]["V_NOTES"] == "1.0"
    assert sent_2[0]["data"]["V_AGE"] == 33

    # 3. Missing both formKey and formId fails validation with exit 1
    res_missing_form = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--json",
            '[{"subjectKey": "SUB1", "data": {"V_NOTES": "1.0"}}]',
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_missing_form.exit_code == 1
    missing_payload = json.loads(res_missing_form.stdout)
    assert missing_payload["schemaVersion"] == "1.0"
    assert missing_payload["items"][0]["allSuccessful"] is False


def test_cli_subjects_register_rerun_with_explicit_subject_key_creates_zero_duplicates(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4 & Req 5: Rerunning `imednet subjects register --skip-existing` skips explicit subjectKey rows."""
    state = SimulationState()
    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kwargs: ImednetSDK(
            client=Client(
                api_key="sim-key",  # pragma: allowlist secret
                security_key="sim-secret",  # pragma: allowlist secret
                base_url="https://edc.sim.imednet.com",
                transport=SimulationTransport(state=state),
            )
        ),
    )
    csv_path = tmp_path / "explicit_subjects.csv"
    csv_path.write_text(
        "formKey,siteName,subjectKey,V_AGE\nF_DM,SITE,SUB-IDEMP-1,31\nF_DM,SITE,SUB-IDEMP-2,42\n",
        encoding="utf-8",
    )

    before_count = len(state.get_subjects("STUDY"))
    first_run = _invoke_cli(
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
    assert first_run.exit_code == 0, first_run.stderr
    first_item = json.loads(first_run.stdout)["items"][0]
    assert first_item["totalRecords"] == 2
    assert first_item["skippedCount"] == 0
    assert len(state.get_subjects("STUDY")) == before_count + 2

    second_run = _invoke_cli(
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
    assert second_run.exit_code == 0, second_run.stderr
    second_item = json.loads(second_run.stdout)["items"][0]
    assert second_item["totalRecords"] == 0
    assert second_item["skippedCount"] == 2
    assert len(state.get_subjects("STUDY")) == before_count + 2


@pytest.mark.parametrize(
    ("result_content_type", "result_body"),
    [
        ("application/json", json.dumps({"errors": [{"message": "Invalid visit window"}]})),
        ("text/html", "<html><body>502 Bad Gateway</body></html>"),
    ],
)
def test_cli_write_wait_exits_1_and_emits_json_counts_on_result_url_errors_or_html(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    result_content_type: str,
    result_body: str,
) -> None:
    """AC 6 & Req 8: CLI write --wait exits 1 and emits JSON counts when resultUrl returns errors or HTML."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/records"):
            return httpx.Response(
                200,
                json={"jobId": "J-FAIL-1", "batchId": "B-FAIL-1", "state": "PROCESSING"},
            )
        if request.method == "GET" and request.url.path.endswith("/jobs/B-FAIL-1"):
            return httpx.Response(
                200,
                json={
                    "jobId": "J-FAIL-1",
                    "batchId": "B-FAIL-1",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": "https://results.synthetic.example/B-FAIL-1",
                },
            )
        if str(request.url) == "https://results.synthetic.example/B-FAIL-1":
            return httpx.Response(
                200,
                content=result_body.encode("utf-8"),
                headers={"Content-Type": result_content_type},
            )
        return httpx.Response(404)

    custom_sdk = ImednetSDK(
        client=Client(
            api_key="sim-key",  # pragma: allowlist secret
            security_key="sim-secret",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=httpx.MockTransport(handler),
        )
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda **_kwargs: custom_sdk)

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
            "-d",
            "V_NOTES=1.0",
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
    assert res.exit_code == 1
    out_payload = json.loads(res.stdout)
    assert out_payload["schemaVersion"] == "1.0"
    assert out_payload["resource"] == "records_create"
    summary_item = out_payload["items"][0]
    assert summary_item["allSuccessful"] is False
    assert len(summary_item["jobs"]) == 1
    job_entry = summary_item["jobs"][0]
    assert job_entry["batchId"] == "B-FAIL-1"
    assert job_entry["failedRecords"] >= 1 or bool(job_entry.get("resultError"))
    assert "Error:" in res.stderr
