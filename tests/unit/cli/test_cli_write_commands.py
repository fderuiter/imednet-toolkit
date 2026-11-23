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
    assert res.stderr.strip() != ""
    payload = json.loads(res.stdout)
    assert payload["status"] == "failed"
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


def _install_reusable_sim_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SimulationState, list[httpx.Request]]:
    """Patch get_sdk to construct a fresh SimulationTransport-backed ImednetSDK per CLI call."""
    state = SimulationState()
    base_transport = SimulationTransport(state=state)
    requests: list[httpx.Request] = []

    class SpyTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return base_transport.handle_request(request)

    spy = SpyTransport()

    def _make(**_kwargs: object) -> ImednetSDK:
        return ImednetSDK(
            client=Client(
                api_key="sim-key",  # pragma: allowlist secret
                security_key="sim-secret",  # pragma: allowlist secret
                base_url="https://edc.sim.imednet.com",
                transport=spy,
            )
        )

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make)
    return state, requests


def _write_schema_fixture(
    tmp_path: Path,
    fmt: str,
    state: SimulationState,
) -> str:
    """Create a schema source for F_DM with ZIP_CODE (text), CODE (string), and NOTE (memo)."""
    from imednet.datadict.emit import to_odm_file
    from imednet.datadict.models import DataDictionary, Form, Variable

    if fmt == "api":
        state.forms.append(
            {
                "studyKey": "STUDY",
                "formId": 99,
                "formKey": "F_TEXT",
                "formName": "Text Coercion Form",
                "formType": "Enrollment",
            }
        )
        state.variables.extend(
            [
                {
                    "studyKey": "STUDY",
                    "formId": 99,
                    "formKey": "F_TEXT",
                    "formName": "Text Coercion Form",
                    "variableId": 901,
                    "variableName": "ZIP_CODE",
                    "variableType": "text",
                },
                {
                    "studyKey": "STUDY",
                    "formId": 99,
                    "formKey": "F_TEXT",
                    "formName": "Text Coercion Form",
                    "variableId": 902,
                    "variableName": "CODE",
                    "variableType": "string",
                },
                {
                    "studyKey": "STUDY",
                    "formId": 99,
                    "formKey": "F_TEXT",
                    "formName": "Text Coercion Form",
                    "variableId": 903,
                    "variableName": "NOTE",
                    "variableType": "memo",
                },
            ]
        )
        return "api"

    dd = DataDictionary(
        forms=[
            Form(
                formId=99,
                formKey="F_TEXT",
                formName="Text Coercion Form",
                variables=[
                    Variable(variableName="ZIP_CODE", variableType="text"),
                    Variable(variableName="CODE", variableType="string"),
                    Variable(variableName="NOTE", variableType="memo"),
                ],
            )
        ]
    )
    if fmt == "json":
        p = tmp_path / "schema.json"
        p.write_text(dd.model_dump_json(by_alias=True), encoding="utf-8")
        return str(p)
    if fmt == "odm_xml":
        p = tmp_path / "schema.xml"
        odm_dd = DataDictionary(
            forms=[
                Form(
                    formId=99,
                    formKey="F_TEXT",
                    formName="Text Coercion Form",
                    variables=[
                        Variable(variableName="ZIP_CODE", variableType="Text"),
                        Variable(variableName="CODE", variableType="Text"),
                        Variable(variableName="NOTE", variableType="Memo"),
                    ],
                )
            ]
        )
        to_odm_file(odm_dd, p)
        return str(p)
    if fmt == "redcap_csv":
        p = tmp_path / "redcap.csv"
        p.write_text(
            '"Variable / Field Name","Form Name","Field Type","Field Label"\n'
            '"ZIP_CODE","F_TEXT","text","Zip Code"\n'
            '"CODE","F_TEXT","text","Code"\n'
            '"NOTE","F_TEXT","notes","Note"\n',
            encoding="utf-8",
        )
        return str(p)
    if fmt in {"csv_dir", "csv_zip"}:
        import zipfile

        d = tmp_path / "dd_csv"
        d.mkdir()
        (d / "FORMS.csv").write_text(
            "formId,formKey,formType,formName\n99,F_TEXT,Enrollment,Text Coercion Form\n",
            encoding="utf-8",
        )
        (d / "QUESTIONS.csv").write_text(
            "formKey,variableName,variableType,label,sequence\n"
            "F_TEXT,ZIP_CODE,Text,Zip Code,1\n"
            "F_TEXT,CODE,String,Code,2\n"
            "F_TEXT,NOTE,Memo,Note,3\n",
            encoding="utf-8",
        )
        (d / "CHOICES.csv").write_text(
            "formKey,variableName,choiceValue,choiceText,position\n",
            encoding="utf-8",
        )
        (d / "BUSINESS_LOGIC.csv").write_text(
            "id,name,formKey,sequence,logic\n",
            encoding="utf-8",
        )
        if fmt == "csv_zip":
            zp = tmp_path / "dd_csv.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                for csv_file in d.iterdir():
                    zf.write(csv_file, arcname=csv_file.name)
            return str(zp)
        return str(d)
    raise ValueError(f"Unknown format {fmt}")


@pytest.mark.parametrize(
    "schema_fmt", ["json", "odm_xml", "redcap_csv", "csv_dir", "csv_zip", "api"]
)
def test_cli_schema_aware_data_coercion_across_all_datadict_formats(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_fmt: str,
) -> None:
    """AC 1: records create and subjects register preserve strings on text/string/memo across all DataDictionary formats."""
    state, requests = _install_reusable_sim_sdk(monkeypatch)
    schema_arg = _write_schema_fixture(tmp_path, schema_fmt, state)

    res_create = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "F_TEXT",
            "--subject-key",
            "SUB1",
            "--site-name",
            "SITE",
            "-d",
            "ZIP_CODE=90210",
            "-d",
            "CODE=42",
            "-d",
            "NOTE=3.5",
            "--schema",
            schema_arg,
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_create.exit_code == 0, res_create.stderr
    post_reqs = [r for r in requests if r.method == "POST" and r.url.path.endswith("/records")]
    assert len(post_reqs) == 1
    posted_create = json.loads(post_reqs[0].content.decode("utf-8"))
    assert posted_create[0]["data"] == {
        "ZIP_CODE": "90210",
        "CODE": "42",
        "NOTE": "3.5",
    }

    requests.clear()
    res_reg = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--form-key",
            "F_TEXT",
            "--site-name",
            "SITE",
            "--subject-key",
            f"SUB-{schema_fmt}",
            "-d",
            "ZIP_CODE=90210",
            "-d",
            "CODE=42",
            "-d",
            "NOTE=3.5",
            "--schema",
            schema_arg,
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_reg.exit_code == 0, res_reg.stderr
    reg_posts = [r for r in requests if r.method == "POST" and r.url.path.endswith("/records")]
    assert len(reg_posts) == 1
    posted_reg = json.loads(reg_posts[0].content.decode("utf-8"))
    assert posted_reg[0]["subjectKey"] == f"SUB-{schema_fmt}"
    assert posted_reg[0]["data"] == {
        "ZIP_CODE": "90210",
        "CODE": "42",
        "NOTE": "3.5",
    }


def test_cli_double_quoted_literals_and_form_id_schema_resolution(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 1: Double-quoted -d strings stay str without --schema, and --form-id resolves form variables."""
    state, requests = _install_reusable_sim_sdk(monkeypatch)

    # Without --schema, double quotes preserve strings (including numeric, boolean, and empty)
    res_quoted = _invoke_cli(
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
            'ZIP_CODE="90210"',
            "-d",
            'EMPTY=""',
            "-d",
            'FLAG="true"',
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_quoted.exit_code == 0, res_quoted.stderr
    post_body = json.loads([r for r in requests if r.method == "POST"][0].content.decode("utf-8"))
    assert post_body[0]["data"] == {"ZIP_CODE": "90210", "EMPTY": "", "FLAG": "true"}

    # With --form-id (no --form-key), schema resolves formKey from formId=99
    requests.clear()
    schema_path = _write_schema_fixture(tmp_path, "json", state)
    res_fid = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--form-id",
            "99",
            "--subject-key",
            "SUB1",
            "--site-name",
            "SITE",
            "-d",
            "ZIP_CODE=90210",
            "-d",
            "CODE=42",
            "-d",
            "NOTE=3.5",
            "--schema",
            schema_path,
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_fid.exit_code == 0, res_fid.stderr
    fid_body = json.loads([r for r in requests if r.method == "POST"][0].content.decode("utf-8"))
    assert fid_body[0]["data"] == {"ZIP_CODE": "90210", "CODE": "42", "NOTE": "3.5"}


def test_cli_subjects_register_merges_file_and_json_overrides_and_rejects_multi_subject_key(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2: subjects register merges -d and --subject-key into --file/--json and rejects ambiguous multi-subject keys."""
    state, requests = _install_reusable_sim_sdk(monkeypatch)
    schema_path = _write_schema_fixture(tmp_path, "json", state)

    # 1. --json merges -d assignments and --subject-key when absent
    res_json = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--json",
            '{"formKey": "F_TEXT", "siteName": "SITE", "data": {"ZIP_CODE": "00000"}}',
            "--subject-key",
            "SUB-JSON-OVR",
            "-d",
            "ZIP_CODE=90210",
            "-d",
            "CODE=42",
            "--schema",
            schema_path,
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_json.exit_code == 0, res_json.stderr
    posted_json = json.loads([r for r in requests if r.method == "POST"][0].content.decode("utf-8"))
    assert posted_json == [
        {
            "formKey": "F_TEXT",
            "siteName": "SITE",
            "subjectKey": "SUB-JSON-OVR",
            "data": {"ZIP_CODE": "90210", "CODE": "42"},
        }
    ]

    # 2. --file merges -d assignments and applies --subject-key when absent
    requests.clear()
    subj_file = tmp_path / "single_subject.csv"
    subj_file.write_text(
        "form_key,site_name,NOTE\nF_TEXT,SITE,3.5\n",
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
            "SUB-FILE-OVR",
            "-d",
            "ZIP_CODE=90210",
            "--schema",
            schema_path,
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_file.exit_code == 0, res_file.stderr
    posted_file = json.loads([r for r in requests if r.method == "POST"][0].content.decode("utf-8"))
    assert posted_file == [
        {
            "formKey": "F_TEXT",
            "siteName": "SITE",
            "subjectKey": "SUB-FILE-OVR",
            "data": {"NOTE": "3.5", "ZIP_CODE": "90210"},
        }
    ]

    # 3. Multi-Subject input lacking subjectKey rejects --subject-key
    requests.clear()
    res_multi = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY",
            "--json",
            '[{"formKey": "F_TEXT", "siteName": "SITE"}, {"formKey": "F_TEXT", "siteName": "SITE"}]',
            "--subject-key",
            "DUP-SUB",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_multi.exit_code == 1
    assert res_multi.stdout == ""
    assert "--subject-key" in res_multi.stderr


def test_cli_partial_post_and_wait_failures_retain_committed_batches(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3: When chunk 1 commits B-1 and chunk 2 fails POST or --wait, stdout retains B-1 with status=failed."""
    secret_key = "SUPER-SECRET-KEY-999"  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_API_KEY", secret_key)

    post_count = 0
    fail_mode = "post"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        path = request.url.path
        if request.method == "POST" and path.endswith("/records"):
            post_count += 1
            if fail_mode == "post" and post_count == 2:
                return httpx.Response(
                    500,
                    json={"error": f"Database error for token {secret_key}"},
                )
            return httpx.Response(
                202,
                json={"jobId": f"J-{post_count}", "batchId": f"B-{post_count}", "state": "QUEUED"},
            )
        if request.method == "GET" and "/jobs/B-" in path and not path.endswith("/results"):
            bid = path.rsplit("/", 1)[-1]
            if bid == "B-1":
                return httpx.Response(
                    200,
                    json={
                        "jobId": "J-1",
                        "batchId": "B-1",
                        "state": "COMPLETED",
                        "progress": 100,
                        "recordTotalCount": 2,
                        "failedRecords": 0,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jobId": "J-2",
                    "batchId": "B-2",
                    "state": "FAILED",
                    "progress": 100,
                    "recordTotalCount": 1,
                    "failedRecords": 1,
                },
            )
        return httpx.Response(404)

    def _make_sdk(**_kwargs: object) -> ImednetSDK:
        return ImednetSDK(
            client=Client(
                api_key=secret_key,
                security_key="sim-secret",  # pragma: allowlist secret
                base_url="https://edc.sim.imednet.com",
                transport=httpx.MockTransport(handler),
            )
        )

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_sdk)
    three_records_json = json.dumps(
        [
            {"formKey": "F_DM", "subjectKey": "S1", "data": {"V_AGE": 30}},
            {"formKey": "F_DM", "subjectKey": "S2", "data": {"V_AGE": 31}},
            {"formKey": "F_DM", "subjectKey": "S3", "data": {"V_AGE": 32}},
        ]
    )

    # Case A1: Chunk 2 POST fails under --format json -> B-1 retained with status="failed"
    res_post_json = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--json",
            three_records_json,
            "--chunk-size",
            "2",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_post_json.exit_code == 1
    post_payload = json.loads(res_post_json.stdout)
    assert post_payload["status"] == "failed"
    assert post_payload["items"][0]["batchIds"] == ["B-1"]
    assert post_payload["items"][0]["allSuccessful"] is False
    assert secret_key not in res_post_json.stderr
    assert "API Error" in res_post_json.stderr

    # Case A2: Chunk 2 POST fails under human table mode -> committed B-1 table rendered
    post_count = 0
    res_post_table = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--json",
            three_records_json,
            "--chunk-size",
            "2",
            "--no-interactive",
        ],
    )
    assert res_post_table.exit_code == 1
    assert "B-1" in res_post_table.stdout
    assert "record submission jobs" in res_post_table.stdout
    assert "records_create" not in res_post_table.stdout
    assert secret_key not in res_post_table.stderr

    # Case B: Chunk 2 fails during --wait -> both B-1 (COMPLETED) and B-2 (FAILED) retained
    post_count = 0
    fail_mode = "wait"
    res_wait_json = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--json",
            three_records_json,
            "--chunk-size",
            "2",
            "--wait",
            "--interval",
            "0",
            "--timeout",
            "5",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_wait_json.exit_code == 1
    wait_payload = json.loads(res_wait_json.stdout)
    assert wait_payload["status"] == "failed"
    item = wait_payload["items"][0]
    assert item["batchIds"] == ["B-1", "B-2"]
    assert item["allSuccessful"] is False
    assert [j["state"] for j in item["jobs"]] == ["COMPLETED", "FAILED"]
    assert "Job Error" in res_wait_json.stderr


def test_cli_non_dry_run_and_dry_run_validation_failures_emit_failed_json_and_stderr(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 4: Non-dry-run and --dry-run validation failures both emit validationReport with status=failed and redacted stderr."""
    _sdk, _state, requests = sim_sdk_and_requests
    secret_val = "SECRET-TOKEN-ABC123"  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_API_KEY", secret_val)

    dd_path = tmp_path / "dd.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {"variableName": "V_AGE", "variableType": "integer", "required": "Yes"}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    for extra_flags in ([], ["--dry-run"]):
        requests.clear()
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
                f"V_AGE=bad-{secret_val}",
                "--schema",
                str(dd_path),
                "--no-interactive",
                "--format",
                "json",
                *extra_flags,
            ],
        )
        assert res.exit_code == 1
        assert requests == []
        parsed = json.loads(res.stdout)
        assert parsed["status"] == "failed"
        report = parsed["items"][0]["validationReport"]
        assert report["isValid"] is False
        assert report["errorCount"] == 1
        assert res.stderr.strip() != ""
        assert secret_val not in res.stderr


@pytest.mark.parametrize(
    ("env_overrides", "extra_flags"),
    [
        ({}, ["--no-interactive"]),
        ({"IMEDNET_NON_INTERACTIVE": "1"}, []),
        ({"TERM": "dumb"}, []),
        ({"NO_COLOR": "1"}, ["--no-interactive"]),
    ],
)
def test_cli_human_write_tables_headers_and_clinical_labels(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_overrides: dict[str, str],
    extra_flags: list[str],
) -> None:
    """AC 5: Human write tables show Batch Id, Job Id, State, Progress, Total Records, Failed Records under clinical labels."""
    _install_reusable_sim_sdk(monkeypatch)
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)

    csv_path = tmp_path / "records.csv"
    csv_path.write_text(
        "form_key,subject_key,site_name,V_AGE\nF_DM,SUB1,SITE,41\n",
        encoding="utf-8",
    )

    commands = [
        (
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
                "V_AGE=41",
                "--wait",
                "--interval",
                "0",
                *extra_flags,
            ],
            "record submission jobs",
            "records_create",
        ),
        (
            [
                "records",
                "update",
                "STUDY",
                "--record-id",
                "601",
                "-d",
                "V_AGE=51",
                "--wait",
                "--interval",
                "0",
                *extra_flags,
            ],
            "record update jobs",
            "records_update",
        ),
        (
            [
                "records",
                "import",
                "STUDY",
                str(csv_path),
                "--wait",
                "--interval",
                "0",
                *extra_flags,
            ],
            "record import jobs",
            "records_import",
        ),
        (
            [
                "subjects",
                "register",
                "STUDY",
                "--form-key",
                "F_DM",
                "--site-name",
                "SITE",
                "-d",
                "V_AGE=41",
                "--wait",
                "--interval",
                "0",
                *extra_flags,
            ],
            "subject registration jobs",
            "subjects_register",
        ),
    ]

    for argv, expected_label, forbidden_snake in commands:
        res = _invoke_cli(capsys, argv)
        assert res.exit_code == 0, res.stderr
        assert expected_label in res.stdout
        assert forbidden_snake not in res.stdout
        for col_header in (
            "Batch Id",
            "Job Id",
            "State",
            "Progress",
            "Total Records",
            "Failed Records",
        ):
            assert col_header in res.stdout


def test_cli_stdin_dash_tty_guard_and_piped_json_csv_autodetection(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 6: '-' on TTY stdin exits 1 before get_sdk/SchemaCache.refresh; piped JSON and CSV both succeed."""
    import io

    # 1. Interactive TTY stdin exits 1 BEFORE get_sdk() or SchemaCache.refresh()
    class TtyStdin(io.StringIO):
        def isatty(self) -> bool:
            return True

    sdk_called = False

    def _fail_if_get_sdk_called(**_kwargs: object) -> ImednetSDK:
        nonlocal sdk_called
        sdk_called = True
        raise AssertionError("get_sdk() must not be called when '-' is passed on TTY stdin")

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _fail_if_get_sdk_called)
    monkeypatch.setattr("sys.stdin", TtyStdin(""))

    for tty_argv in (
        ["records", "create", "STUDY", "-f", "-", "--schema", "api"],
        ["records", "update", "STUDY", "-f", "-", "--schema", "api"],
        ["records", "import", "STUDY", "-", "--schema", "api"],
        ["subjects", "register", "STUDY", "-f", "-", "--schema", "api"],
    ):
        res_tty = _invoke_cli(capsys, tty_argv)
        assert res_tty.exit_code == 1
        assert res_tty.stdout == ""
        assert "TTY" in res_tty.stderr or "stdin" in res_tty.stderr.lower()
        assert sdk_called is False

    # 2. Piped JSON and piped CSV succeed across all four write commands
    _state, requests = _install_reusable_sim_sdk(monkeypatch)

    class PipedStdin(io.StringIO):
        def isatty(self) -> bool:
            return False

    json_stdin = json.dumps(
        [{"formKey": "F_DM", "siteName": "SITE", "subjectKey": "SUB1", "data": {"V_AGE": 36}}]
    )
    csv_stdin = "form_key,site_name,subject_key,V_AGE\nF_DM,SITE,SUB1,37\n"

    for cmd_prefix in (
        ["records", "create", "STUDY", "-f", "-"],
        ["records", "update", "STUDY", "-f", "-"],
        ["records", "import", "STUDY", "-"],
        ["subjects", "register", "STUDY", "-f", "-"],
    ):
        for raw_pipe in (json_stdin, csv_stdin):
            requests.clear()
            monkeypatch.setattr("sys.stdin", PipedStdin(raw_pipe))
            res_pipe = _invoke_cli(capsys, [*cmd_prefix, "--no-interactive", "--format", "json"])
            assert res_pipe.exit_code == 0, res_pipe.stderr
            parsed = json.loads(res_pipe.stdout)
            assert parsed["status"] == "complete"
            assert parsed["items"][0]["totalRecords"] == 1
            assert any(r.method == "POST" for r in requests)


def test_cli_offline_dry_run_sends_zero_http_requests_and_zero_fetch_lines(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 7: Offline --dry-run on records create, records import, and subjects register --skip-existing sends 0 HTTP requests and 0 fetch lines."""
    _sdk, state, requests = sim_sdk_and_requests
    schema_path = _write_schema_fixture(tmp_path, "json", state)

    csv_path = tmp_path / "offline.csv"
    csv_path.write_text(
        "form_key,site_name,subject_key,ZIP_CODE\nF_TEXT,SITE,SUB-OFF-1,90210\nF_TEXT,SITE,SUB-OFF-1,90210\n",
        encoding="utf-8",
    )

    dry_run_commands = [
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "F_TEXT",
            "--site-name",
            "SITE",
            "--subject-key",
            "SUB1",
            "-d",
            "ZIP_CODE=90210",
            "--schema",
            schema_path,
            "--dry-run",
        ],
        [
            "records",
            "import",
            "STUDY",
            str(csv_path),
            "--schema",
            schema_path,
            "--dry-run",
        ],
        [
            "subjects",
            "register",
            "STUDY",
            "--file",
            str(csv_path),
            "--skip-existing",
            "--schema",
            schema_path,
            "--dry-run",
        ],
    ]

    for argv in dry_run_commands:
        requests.clear()
        res = _invoke_cli(capsys, argv)
        assert res.exit_code == 0, res.stderr
        assert requests == []
        assert res.stderr == ""
        assert "Fetching" not in res.stderr
        assert "Dry run succeeded" in res.stdout


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
