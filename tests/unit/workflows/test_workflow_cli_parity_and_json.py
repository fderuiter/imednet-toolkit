"""Contract and simulation-mode JSON tests for all imednet_workflows CLI subcommands."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
from pathlib import Path

import pytest

from imednet import cli
from imednet.simulation.state import get_default_simulation_state
from imednet_workflows.cli import WORKFLOW_CLI_SUBCOMMANDS


@pytest.fixture(autouse=True)
def reset_simulation_state():
    """Reset the shared in-memory simulation state before and after each test."""
    get_default_simulation_state().reset()
    yield
    get_default_simulation_state().reset()


class CliResult:
    """Captured CLI invocation result with separated stdout and stderr."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def invoke_cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> CliResult:
    """Invoke imednet.cli.app using pytest capsys so log_cli never swallows stdout."""
    capsys.readouterr()
    exit_code = 0
    try:
        cli.app(args)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return CliResult(exit_code, captured.out, captured.err)


def test_workflows_cli_parity_contract_covers_all_9_registered_plugins(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify imednet workflows --help lists subcommands covering all 9 registered plugins."""
    eps = importlib.metadata.entry_points(group="imednet.workflows")
    registered_names = {ep.name for ep in eps}
    assert len(registered_names) == 9
    assert set(WORKFLOW_CLI_SUBCOMMANDS.keys()) == registered_names

    wf_help = invoke_cli(["workflows", "--help"], capsys)
    assert wf_help.exit_code == 0
    for ep_name, subcommand in WORKFLOW_CLI_SUBCOMMANDS.items():
        assert subcommand in wf_help.stdout, (
            f"Workflow entry point '{ep_name}' missing CLI subcommand '{subcommand}' in workflows --help"
        )

    top_help = invoke_cli(["--help"], capsys)
    assert top_help.exit_code == 0
    assert "subject-data" in top_help.stdout
    assert "workflows" in top_help.stdout


def test_extract_records_simulation_json_and_stderr_separation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify extract-records --simulate --format json emits valid JSON on stdout and progress on stderr."""
    res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "extract-records",
            "STUDY",
            "--record-filter",
            "form_key=F_DM",
            "--subject-filter",
            "subject_status=Enrolled",
            "--format",
            "json",
        ],
        capsys,
    )
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["resource"] == "records"
    assert parsed["status"] == "complete"
    assert parsed["count"] == 1
    assert parsed["items"][0]["formKey"] == "F_DM"
    assert "Extracting records for study 'STUDY'" in res.stderr


@pytest.mark.parametrize(
    "cmd_prefix",
    [
        ["--simulate", "subject-data"],
        ["--simulate", "workflows", "subject-data"],
    ],
)
def test_subject_data_simulation_json_and_table_modes(
    cmd_prefix: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify top-level and workflows subject-data emit valid JSON or tables on stdout."""
    json_res = invoke_cli([*cmd_prefix, "STUDY", "SUB1", "--format", "json"], capsys)
    assert json_res.exit_code == 0, json_res.stderr
    parsed = json.loads(json_res.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["resource"] == "subject_data"
    assert parsed["count"] == 1
    assert parsed["items"][0]["subject_details"]["subjectKey"] == "SUB1"

    table_res = invoke_cli([*cmd_prefix, "STUDY", "SUB1"], capsys)
    assert table_res.exit_code == 0, table_res.stderr
    assert "SUB1" in table_res.stdout


def test_inspect_study_simulation_json_and_table(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify workflows inspect-study emits valid JSON and human table in simulation mode."""
    json_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "inspect-study",
            "STUDY",
            "--force-refresh",
            "--format",
            "json",
        ],
        capsys,
    )
    assert json_res.exit_code == 0, json_res.stderr
    parsed = json.loads(json_res.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["resource"] == "study_schema"
    assert parsed["count"] == 1
    assert parsed["items"][0]["studyKey"] == "STUDY"
    assert parsed["items"][0]["formsCount"] >= 2

    table_res = invoke_cli(["--simulate", "workflows", "inspect-study", "STUDY"], capsys)
    assert table_res.exit_code == 0, table_res.stderr
    assert "STUDY" in table_res.stdout


def test_update_records_simulation_json_file_and_error_handling(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify workflows update-records supports flags, --data, --file, and stderr error exits."""
    flag_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "update-records",
            "STUDY",
            "--form-key",
            "F_DM",
            "--subject-key",
            "SUB1",
            "--interval-name",
            "Baseline",
            "--set",
            "V_AGE=35",
            "--wait",
            "--poll-interval",
            "1",
            "--format",
            "json",
        ],
        capsys,
    )
    assert flag_res.exit_code == 0, flag_res.stderr
    parsed = json.loads(flag_res.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["resource"] == "jobs"
    assert parsed["count"] == 1
    assert parsed["items"][0]["state"] == "COMPLETED"

    payload_file = tmp_path / "records.json"
    payload_file.write_text(
        json.dumps(
            [
                {
                    "formKey": "F_DM",
                    "subjectKey": "SUB1",
                    "intervalName": "Baseline",
                    "data": {"V_AGE": 41},
                }
            ]
        ),
        encoding="utf-8",
    )
    file_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "update-records",
            "STUDY",
            "--file",
            str(payload_file),
            "--format",
            "json",
        ],
        capsys,
    )
    assert file_res.exit_code == 0, file_res.stderr
    assert json.loads(file_res.stdout)["count"] == 1

    err_res = invoke_cli(["--simulate", "workflows", "update-records", "STUDY"], capsys)
    assert err_res.exit_code == 1
    assert err_res.stdout == ""
    assert "Error:" in err_res.stderr

    conflict_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "update-records",
            "STUDY",
            "--file",
            str(payload_file),
            "--data",
            "{}",
        ],
        capsys,
    )
    assert conflict_res.exit_code == 1
    assert conflict_res.stdout == ""
    assert "either --file or --data" in conflict_res.stderr


def test_register_subjects_simulation_json_data_and_error_handling(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify workflows register-subjects supports flags, --data JSON, and stderr error exits."""
    flag_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "register-subjects",
            "STUDY",
            "--form-key",
            "F_DM",
            "--site-name",
            "SITE",
            "--set",
            "V_AGE=42",
            "--wait",
            "--poll-interval",
            "1",
            "--format",
            "json",
        ],
        capsys,
    )
    assert flag_res.exit_code == 0, flag_res.stderr
    parsed = json.loads(flag_res.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["resource"] == "jobs"
    assert parsed["items"][0]["state"] == "COMPLETED"

    inline_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "register-subjects",
            "STUDY",
            "--data",
            json.dumps({"formKey": "F_DM", "siteName": "SITE", "data": {"V_AGE": 29}}),
            "--format",
            "json",
        ],
        capsys,
    )
    assert inline_res.exit_code == 0, inline_res.stderr
    assert json.loads(inline_res.stdout)["count"] == 1

    missing_res = invoke_cli(
        ["--simulate", "workflows", "register-subjects", "STUDY", "--form-key", "F_DM"],
        capsys,
    )
    assert missing_res.exit_code == 1
    assert missing_res.stdout == ""
    assert "--site-name" in missing_res.stderr


def test_queries_workflow_simulation_json_modes(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify workflows queries open, subject, site, and counts modes in simulation mode."""
    open_res = invoke_cli(
        ["--simulate", "workflows", "queries", "STUDY", "--format", "json"], capsys
    )
    assert open_res.exit_code == 0, open_res.stderr
    open_payload = json.loads(open_res.stdout)
    assert open_payload["resource"] == "queries"
    assert open_payload["count"] >= 1

    subj_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "queries",
            "STUDY",
            "--subject-key",
            "SUB1",
            "--open-only",
            "--sort",
            "annotationId,asc",
            "--format",
            "json",
        ],
        capsys,
    )
    assert subj_res.exit_code == 0, subj_res.stderr
    assert json.loads(subj_res.stdout)["count"] >= 1

    site_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "queries",
            "STUDY",
            "--site-name",
            "SITE",
            "--open-only",
            "--format",
            "json",
        ],
        capsys,
    )
    assert site_res.exit_code == 0, site_res.stderr
    assert json.loads(site_res.stdout)["count"] >= 1

    counts_res = invoke_cli(
        ["--simulate", "workflows", "queries", "STUDY", "--counts", "--format", "json"],
        capsys,
    )
    assert counts_res.exit_code == 0, counts_res.stderr
    counts_payload = json.loads(counts_res.stdout)
    assert counts_payload["resource"] == "query_counts"
    assert counts_payload["count"] >= 1


def test_centralize_duckdb_simulation_json_and_lazy_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify workflows centralize-duckdb works in --simulate --format json and lazy-loads duckdb."""
    db_file = tmp_path / "central.duckdb"
    res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "centralize-duckdb",
            "STUDY",
            str(db_file),
            "--mode",
            "replace",
            "--format",
            "json",
        ],
        capsys,
    )
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["resource"] == "duckdb_centralization"
    assert parsed["items"][0]["revisionsIngested"] >= 1
    assert parsed["items"][0]["silverViewBuilt"] is True

    orig_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str) -> object | None:
        if name == "duckdb":
            return None
        return orig_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    help_res = invoke_cli(["workflows", "centralize-duckdb", "--help"], capsys)
    assert help_res.exit_code == 0

    missing_res = invoke_cli(["--simulate", "workflows", "centralize-duckdb", "STUDY"], capsys)
    assert missing_res.exit_code == 1
    assert missing_res.stdout == ""
    assert "imednet[duckdb]" in missing_res.stderr


def test_map_records_simulation_json_sdtm_and_hierarchical(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify workflows map-records tabular, SDTM, and hierarchical modes with --format json."""
    tabular_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "map-records",
            "STUDY",
            "--filter",
            "form_key=F_DM",
            "--record-data-filter",
            "V_AGE>=20",
            "--sort",
            "recordId,asc",
            "--format",
            "json",
        ],
        capsys,
    )
    assert tabular_res.exit_code == 0, tabular_res.stderr
    tab_payload = json.loads(tabular_res.stdout)
    assert tab_payload["resource"] == "mapped_records"
    assert tab_payload["count"] == 1
    assert tab_payload["items"][0]["V_AGE"] == 45

    sdtm_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "map-records",
            "STUDY",
            "--sdtm-domain",
            "DM",
            "--format",
            "json",
        ],
        capsys,
    )
    assert sdtm_res.exit_code == 0, sdtm_res.stderr
    sdtm_payload = json.loads(sdtm_res.stdout)
    assert sdtm_payload["resource"] == "mapped_records"
    assert sdtm_payload["items"][0]["DOMAIN"] == "DM"

    hier_res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "map-records",
            "STUDY",
            "--hierarchical",
            "--format",
            "json",
        ],
        capsys,
    )
    assert hier_res.exit_code == 0, hier_res.stderr
    hier_payload = json.loads(hier_res.stdout)
    assert hier_payload["resource"] == "mapped_records"
    assert hier_payload["count"] >= 1
    assert hier_payload["items"][0]["subject_key"] == "SUB1"


def test_uat_run_simulation_json_and_stderr_separation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify workflows uat run --simulate --format json emits valid JSON on stdout and phases on stderr."""
    res = invoke_cli(
        [
            "--simulate",
            "workflows",
            "uat",
            "run",
            "STUDY",
            "--subject-count",
            "1",
            "--format",
            "json",
        ],
        capsys,
    )
    assert res.exit_code == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["resource"] == "uat_run"
    assert parsed["count"] == 1
    assert parsed["items"][0]["state"] == "COMPLETED"
    assert "[Phase 2/5]" in res.stderr


def test_workflows_state_set_invalid_timestamp_via_main_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify workflows state set with invalid timestamp writes stderr, exits 1, and leaves stdout empty."""
    ledger_path = tmp_path / "ledger.json"
    res = invoke_cli(
        [
            "workflows",
            "state",
            "set",
            "--study-key",
            "STUDY",
            "--stream",
            "records",
            "--timestamp",
            "not-a-valid-iso-ts",
            "--ledger-path",
            str(ledger_path),
        ],
        capsys,
    )
    assert res.exit_code == 1
    assert res.stdout == ""
    assert "Invalid ISO timestamp" in res.stderr
