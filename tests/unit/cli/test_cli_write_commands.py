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
    import sys

    token = set_strict_mode_context(False)
    monkeypatch.setenv("IMEDNET_API_KEY", "sim-key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "sim-secret")  # pragma: allowlist secret
    sys.modules["imednet.cli"] = cli
    importlib.reload(cli)
    try:
        yield
    finally:
        sys.modules["imednet.cli"] = cli
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


def test_cli_export_and_reimport_roundtrip_csv_json_parquet_excel_and_create_only(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 1 & Req 1-4: Re-importing exported CSV, JSON, Parquet, and Excel updates in place without duplicates."""
    import pandas as pd

    from imednet.integrations.export import (
        export_to_excel,
        export_to_parquet,
    )

    sdk, state, _requests = sim_sdk_and_requests
    # Seed an unscheduled record alongside scheduled records so re-imports prove both stay idempotent
    sdk.records.create_batch(
        "STUDY",
        [
            {
                "recordOid": "REC-OID-UNSCHED-EXP",
                "formKey": "F_VS",
                "subjectKey": "SUB1",
                "siteName": "SITE",
                "data": {"V_WEIGHT": 70.2},
            }
        ],
    )
    baseline_count = len(state.get_records("STUDY"))
    assert baseline_count >= 3

    csv_file = tmp_path / "export.csv"
    json_file = tmp_path / "export.json"
    parquet_file = tmp_path / "export.parquet"
    xlsx_file = tmp_path / "export.xlsx"

    res_exp_csv = _invoke_cli(
        capsys, ["export", "csv", "STUDY", str(csv_file), "--no-mask-phi", "--no-quality-gate"]
    )
    assert res_exp_csv.exit_code == 0, res_exp_csv.stderr
    res_exp_json = _invoke_cli(
        capsys, ["export", "json", "STUDY", str(json_file), "--no-mask-phi", "--no-quality-gate"]
    )
    assert res_exp_json.exit_code == 0, res_exp_json.stderr
    export_to_parquet(sdk, "STUDY", str(parquet_file), mask_phi=False)
    export_to_excel(sdk, "STUDY", str(xlsx_file), mask_phi=False)

    exported_csv_df = pd.read_csv(csv_file)
    assert {"recordId", "formKey", "intervalId"} <= set(exported_csv_df.columns)

    for exported_path in (csv_file, json_file, parquet_file, xlsx_file):
        res = _invoke_cli(
            capsys,
            [
                "records",
                "import",
                "STUDY",
                str(exported_path),
                "--no-interactive",
                "--format",
                "json",
            ],
        )
        assert res.exit_code == 0, f"{exported_path.name}: {res.stderr}"
        body = json.loads(res.stdout)
        assert body["resource"] == "records_import"
        assert body["items"][0]["allSuccessful"] is True
        assert len(state.get_records("STUDY")) == baseline_count

    # --create-only strips recordId and recordOid and creates new records
    res_create_only = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(csv_file),
            "--create-only",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_create_only.exit_code == 0, res_create_only.stderr
    assert len(state.get_records("STUDY")) == baseline_count + 1


def test_cli_records_import_unwraps_v1_list_envelope_rest_data_envelope_and_jsonl(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 3 & Req 5: Importing records list --format json, REST data envelope, and .jsonl/.ndjson creates clean data dicts."""
    import io

    _sdk, state, requests = sim_sdk_and_requests
    baseline_count = len(state.get_records("STUDY"))

    list_res = _invoke_cli(
        capsys,
        ["records", "list", "STUDY", "--format", "json", "--no-interactive"],
    )
    assert list_res.exit_code == 0, list_res.stderr
    list_json = json.loads(list_res.stdout)
    assert "items" in list_json

    monkeypatch.setattr("sys.stdin", io.StringIO(list_res.stdout))
    pipe_res = _invoke_cli(
        capsys,
        ["records", "import", "STUDY", "-", "--no-interactive", "--format", "json"],
    )
    assert pipe_res.exit_code == 0, pipe_res.stderr
    assert len(state.get_records("STUDY")) == baseline_count
    post_body = json.loads([r for r in requests if r.method == "POST"][-1].content)
    for sent_rec in post_body:
        assert "data" in sent_rec
        assert "items" not in sent_rec["data"]
        assert "schemaVersion" not in sent_rec["data"]
        assert "recordData" not in sent_rec

    # REST API {"data": [...]} envelope
    rest_env_path = tmp_path / "rest_envelope.json"
    rest_env_path.write_text(json.dumps({"data": list_json["items"]}), encoding="utf-8")
    rest_res = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(rest_env_path),
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert rest_res.exit_code == 0, rest_res.stderr
    assert len(state.get_records("STUDY")) == baseline_count

    # .jsonl and .ndjson files
    for ext in (".jsonl", ".ndjson"):
        jl_path = tmp_path / f"batch{ext}"
        jl_path.write_text(
            "\n".join(json.dumps(item) for item in list_json["items"]) + "\n",
            encoding="utf-8",
        )
        jl_res = _invoke_cli(
            capsys,
            ["records", "import", "STUDY", str(jl_path), "--no-interactive", "--format", "json"],
        )
        assert jl_res.exit_code == 0, jl_res.stderr
        assert len(state.get_records("STUDY")) == baseline_count


def test_cli_records_import_optional_dependency_hints_and_stdlib_formats(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 4 & Req 4: Missing Parquet/Excel readers exit 1 with install hint; CSV/JSON/JSONL work with stdlib; unsupported suffixes exit 1."""
    import sys

    import imednet.utils.pandas as pandas_mod

    _sdk, _state, _requests = sim_sdk_and_requests

    bad_ext = tmp_path / "records.tsv"
    bad_ext.write_text("formKey\tF_DM\n", encoding="utf-8")
    bad_res = _invoke_cli(capsys, ["records", "import", "STUDY", str(bad_ext), "--no-interactive"])
    assert bad_res.exit_code == 1
    assert "Unsupported file format" in bad_res.stderr

    pq_dummy = tmp_path / "dummy.parquet"
    pq_dummy.write_bytes(b"PAR1")
    xlsx_dummy = tmp_path / "dummy.xlsx"
    xlsx_dummy.write_bytes(b"PK\x03\x04")

    monkeypatch.setitem(sys.modules, "pandas", None)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    monkeypatch.setattr(pandas_mod, "pd", None)

    pq_res = _invoke_cli(capsys, ["records", "import", "STUDY", str(pq_dummy), "--no-interactive"])
    assert pq_res.exit_code == 1
    assert "imednet[export]" in pq_res.stderr

    xlsx_res = _invoke_cli(
        capsys, ["records", "import", "STUDY", str(xlsx_dummy), "--no-interactive"]
    )
    assert xlsx_res.exit_code == 1
    assert "imednet[export]" in xlsx_res.stderr

    # Meanwhile .csv, .json, .jsonl, and .ndjson succeed with stdlib only
    csv_f = tmp_path / "stdlib.csv"
    csv_f.write_text("formKey,subjectKey,siteName,V_AGE\nF_DM,SUB1,SITE,41\n", encoding="utf-8")
    json_f = tmp_path / "stdlib.json"
    json_f.write_text(
        json.dumps([{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "V_AGE": 42}]),
        encoding="utf-8",
    )
    jsonl_f = tmp_path / "stdlib.jsonl"
    jsonl_f.write_text(
        '{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "V_AGE": 43}\n',
        encoding="utf-8",
    )
    ndjson_f = tmp_path / "stdlib.ndjson"
    ndjson_f.write_text(
        '{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "V_AGE": 44}\n',
        encoding="utf-8",
    )
    for fpath in (csv_f, json_f, jsonl_f, ndjson_f):
        ok_res = _invoke_cli(
            capsys,
            ["records", "import", "STUDY", str(fpath), "--no-interactive", "--format", "json"],
        )
        assert ok_res.exit_code == 0, f"{fpath.name}: {ok_res.stderr}"


def test_cli_dry_run_rejects_invalid_flat_json_and_nan_with_is_valid_false(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 5 & Req 6: --dry-run --schema on flattened JSON with invalid Variable types or NaN exits 1 with isValid: false."""
    _sdk, _state, requests = sim_sdk_and_requests

    dd_path = tmp_path / "dd.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {"variableName": "V_CODE", "variableType": "text"},
                            {"variableName": "V_WEIGHT", "variableType": "float"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    flat_invalid = tmp_path / "flat_invalid.json"
    flat_invalid.write_text(
        json.dumps([{"formKey": "F_DM", "subjectKey": "SUB1", "V_CODE": 12345}]),
        encoding="utf-8",
    )
    res_flat = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(flat_invalid),
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_flat.exit_code == 1
    assert not any(r.method == "POST" for r in requests)
    flat_report = json.loads(res_flat.stdout)["items"][0]["validationReport"]
    assert flat_report["isValid"] is False

    nan_json = tmp_path / "nan_payload.json"
    nan_json.write_text(
        '[{"formKey": "F_DM", "subjectKey": "SUB1", "V_WEIGHT": NaN}]',
        encoding="utf-8",
    )
    res_nan = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(nan_json),
            "--schema",
            str(dd_path),
            "--dry-run",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_nan.exit_code == 1
    assert not any(r.method == "POST" for r in requests)
    nan_report = json.loads(res_nan.stdout)["items"][0]["validationReport"]
    assert nan_report["isValid"] is False


def test_cli_records_create_and_update_schema_d_coercion_and_record_identifiers(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 7 & Req 3, 7: -d coerces Text, Boolean, and true/false with --schema; records update targets recordId/recordOid."""
    _sdk, state, requests = sim_sdk_and_requests

    dd_path = tmp_path / "dd_coercion.json"
    dd_path.write_text(
        json.dumps(
            {
                "forms": [
                    {
                        "formKey": "F_DM",
                        "variables": [
                            {"variableName": "V_CODE", "variableType": "text"},
                            {"variableName": "V_BOOL", "variableType": "checkbox"},
                            {"variableName": "V_TF", "variableType": "true/false"},
                            {"variableName": "V_AGE", "variableType": "integer"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    res_create = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--schema",
            str(dd_path),
            "--form-key",
            "F_DM",
            "--subject-key",
            "SUB1",
            "--site-name",
            "SITE",
            "-d",
            "V_CODE=123",
            "-d",
            "V_BOOL=true",
            "-d",
            "V_TF=true",
            "--create-only",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_create.exit_code == 0, res_create.stderr
    sent_create = json.loads([r for r in requests if r.method == "POST"][-1].content)
    assert sent_create[0]["data"] == {"V_CODE": "123", "V_BOOL": True, "V_TF": True}

    count_after_create = len(state.get_records("STUDY"))

    # records update by --record-id
    res_upd_id = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--record-id",
            "601",
            "--form-key",
            "F_DM",
            "--schema",
            str(dd_path),
            "-d",
            "V_CODE=009",
            "-d",
            "V_AGE=54",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_upd_id.exit_code == 0, res_upd_id.stderr
    assert json.loads(res_upd_id.stdout)["resource"] == "records_update"
    assert len(state.get_records("STUDY")) == count_after_create
    rec_601 = next(r for r in state.get_records("STUDY") if r["recordId"] == 601)
    assert rec_601["recordData"]["V_CODE"] == "009"
    assert rec_601["recordData"]["V_AGE"] == 54

    # records update by --record-oid
    res_upd_oid = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY",
            "--record-oid",
            "REC-602",
            "-d",
            "V_WEIGHT=83.5",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_upd_oid.exit_code == 0, res_upd_oid.stderr
    assert len(state.get_records("STUDY")) == count_after_create
    rec_602 = next(r for r in state.get_records("STUDY") if r["recordId"] == 602)
    assert rec_602["recordData"]["V_WEIGHT"] == 83.5


def test_cli_failed_jobs_exit_1_with_json_summary_and_readable_table_headers(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """AC 8 & Req 8: Failed Jobs return exit code 1 with readable table labels or complete JSON summaries, with and without --wait."""
    from imednet.endpoints.records import RecordsEndpoint
    from imednet.models.jobs import Job
    from imednet.utils.batch_coordinator import BatchSubmissionSummary
    from imednet.utils.job_poller import JobFailedError

    _sdk, _state, _requests = sim_sdk_and_requests
    failed_job = Job(batchId="BATCH-FAIL-1", jobId="JOB-FAIL-1", state="FAILED")
    failed_summary = BatchSubmissionSummary(
        study_key="STUDY",
        jobs=[failed_job],
        chunk_sizes=[1],
        total_records=1,
    )

    # 1. Without --wait: summary has failed job
    monkeypatch.setattr(RecordsEndpoint, "create_batch", lambda *_a, **_kw: failed_summary)
    res_json_nowait = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "F_DM",
            "-d",
            "V_AGE=40",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_json_nowait.exit_code == 1
    parsed_nowait = json.loads(res_json_nowait.stdout)
    assert parsed_nowait["items"][0]["allSuccessful"] is False

    res_table_nowait = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY",
            "--form-key",
            "F_DM",
            "-d",
            "V_AGE=40",
            "--no-interactive",
        ],
    )
    assert res_table_nowait.exit_code == 1
    assert "Found 1 record creation jobs:" in res_table_nowait.stdout
    assert "records_create" not in res_table_nowait.stdout

    # 2. With --wait: JobFailedError raised during polling
    def _raise_job_failed(*_a: object, **_kw: object) -> BatchSubmissionSummary:
        raise JobFailedError("Job BATCH-FAIL-1 ended in state FAILED", status=failed_job)

    monkeypatch.setattr(RecordsEndpoint, "create_batch", _raise_job_failed)
    res_json_wait = _invoke_cli(
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
            "--format",
            "json",
        ],
    )
    assert res_json_wait.exit_code == 1
    parsed_wait = json.loads(res_json_wait.stdout)
    assert parsed_wait["resource"] == "records_update"
    assert parsed_wait["items"][0]["allSuccessful"] is False
    assert parsed_wait["items"][0]["jobs"][0]["batchId"] == "BATCH-FAIL-1"

    csv_file = tmp_path / "fail_wait.csv"
    csv_file.write_text("formKey,subjectKey,V_AGE\nF_DM,SUB1,40\n", encoding="utf-8")
    res_table_wait = _invoke_cli(
        capsys,
        ["records", "import", "STUDY", str(csv_file), "--wait", "--no-interactive"],
    )
    assert res_table_wait.exit_code == 1
    assert "Found 1 record import jobs:" in res_table_wait.stdout
    assert "records_import" not in res_table_wait.stdout

    # 3. With --wait when JobFailedError has status=None: still exits 1 in JSON and table modes
    def _raise_job_failed_no_status(*_a: object, **_kw: object) -> BatchSubmissionSummary:
        raise JobFailedError("Job BATCH-NONE failed before status attached", status=None)

    monkeypatch.setattr(RecordsEndpoint, "create_batch", _raise_job_failed_no_status)
    res_nostatus_json = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY",
            str(csv_file),
            "--wait",
            "--no-interactive",
            "--format",
            "json",
        ],
    )
    assert res_nostatus_json.exit_code == 1
    assert json.loads(res_nostatus_json.stdout)["items"][0]["allSuccessful"] is False

    res_nostatus_table = _invoke_cli(
        capsys,
        ["records", "import", "STUDY", str(csv_file), "--wait", "--no-interactive"],
    )
    assert res_nostatus_table.exit_code == 1
    assert "BATCH-NONE" in res_nostatus_table.stderr


def test_cli_blocks_masked_sentinel_across_parquet_excel_jsonl_and_inline_data(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sim_sdk_and_requests: tuple[ImednetSDK, SimulationState, list[httpx.Request]],
) -> None:
    """Guardrail: Block ***MASKED*** across Parquet, Excel, JSONL, and inline -d before HTTP writes."""
    import pandas as pd

    _sdk, _state, requests = sim_sdk_and_requests
    masked_df = pd.DataFrame(
        [{"formKey": "F_DM", "subjectKey": "SUB1", "V_NOTES": MASKED_SENTINEL}]
    )
    pq_path = tmp_path / "masked.parquet"
    xlsx_path = tmp_path / "masked.xlsx"
    jsonl_path = tmp_path / "masked.jsonl"
    masked_df.to_parquet(pq_path, index=False)
    masked_df.to_excel(xlsx_path, index=False, engine="openpyxl")
    jsonl_path.write_text(
        json.dumps({"formKey": "F_DM", "subjectKey": "SUB1", "V_NOTES": MASKED_SENTINEL}) + "\n",
        encoding="utf-8",
    )

    for masked_file in (pq_path, xlsx_path, jsonl_path):
        res = _invoke_cli(
            capsys,
            ["records", "import", "STUDY", str(masked_file), "--no-interactive"],
        )
        assert res.exit_code == 1
        assert "MASKED" in res.stderr

    res_inline = _invoke_cli(
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
        ],
    )
    assert res_inline.exit_code == 1
    assert "MASKED" in res_inline.stderr
    assert requests == []


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
