"""Exercise versioned machine responses through the actual argparse entry point."""

import json
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet.models import Coding, Record, Study, Subject, User
from imednet.models.jobs import JobStatus


@pytest.fixture
def sdk(monkeypatch):
    sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    return sdk


@pytest.mark.parametrize(
    ("command", "attr", "model"),
    [
        (["studies", "list"], "studies", Study(studyKey="S", studyName="Synthetic")),
        (
            ["records", "list", "S"],
            "records",
            Record(recordId=1, recordData={"value": 0, "flag": False, "empty": None}),
        ),
        (["subjects", "list", "S"], "subjects", Subject(subjectKey="SUB")),
        (["users", "list", "S"], "users", User(userId="U1", login="jdoe", userActiveInStudy=True)),
        (["codings", "list", "S"], "codings", Coding(codingId=1, dictionaryName="MedDRA")),
    ],
)
def test_single_json_result_preserves_values(sdk, capsys, command, attr, model):
    getattr(sdk, attr).list.return_value = [model]
    cli.app(command + ["--format", "json"])
    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert response["schemaVersion"] == "1.0"
    assert response["status"] == "complete"
    assert response["resource"] == attr
    assert response["count"] == 1
    assert response["items"] == [model.model_dump(mode="json", by_alias=True)]
    assert "Fetching" not in captured.out
    assert captured.err == ""
    sdk.close.assert_called_once()


@pytest.mark.parametrize(
    "resource",
    [
        "studies",
        "queries",
        "variables",
        "record-revisions",
        "sites",
        "records",
        "subjects",
        "intervals",
        "visits",
        "users",
        "codings",
    ],
)
def test_empty_core_list_json(sdk, capsys, resource):
    getattr(sdk, resource.replace("-", "_")).list.return_value = []
    command = [resource, "list"] + ([] if resource == "studies" else ["S"])
    cli.app(command + ["--format", "json"])
    response = json.loads(capsys.readouterr().out)
    assert response["items"] == [] and response["count"] == 0


def test_partial_iteration_failure_has_no_success_stdout(sdk, capsys):
    def partial():
        yield Study(studyKey="S")
        raise RuntimeError("synthetic fetch failure")

    sdk.studies.list.return_value = partial()
    with pytest.raises(SystemExit) as exc:
        cli.app(["studies", "list", "--format", "json"])
    captured = capsys.readouterr()
    assert exc.value.code == 1 and captured.out == ""
    assert "synthetic fetch failure" in captured.err
    sdk.close.assert_called_once()


def test_subject_filters_reach_sdk_in_json_mode(sdk, capsys):
    sdk.subjects.list.return_value = []
    cli.app(["subjects", "list", "S", "--filter", "deleted=false", "--format", "json"])
    sdk.subjects.list.assert_called_once_with("S", deleted=False)
    assert json.loads(capsys.readouterr().out)["items"] == []


@pytest.mark.parametrize("command", ["status", "wait"])
def test_jobs_json_uses_same_envelope(sdk, capsys, command):
    job = JobStatus(batchId="B", state="COMPLETED")
    sdk.get_job.return_value = job
    sdk.poll_job.return_value = job
    cli.app(["jobs", command, "S", "B", "--format", "json"])
    response = json.loads(capsys.readouterr().out)
    assert response["resource"] == "jobs" and response["items"][0]["state"] == "COMPLETED"


def test_json_file_flag_conflict_does_not_fetch_or_write(sdk, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.app(["records", "list", "S", "--format", "json", "--output", "json"])
    assert exc.value.code == 1
    assert not (tmp_path / "records.json").exists()
    sdk.records.list.assert_not_called()
    assert capsys.readouterr().out == ""


def test_json_serialization_failure_is_atomic(sdk, capsys):
    sdk.studies.list.return_value = [{"value": float("nan")}]
    with pytest.raises(SystemExit) as exc:
        cli.app(["studies", "list", "--format", "json"])
    assert exc.value.code == 1 and capsys.readouterr().out == ""


def test_usage_error_stays_exit_two_and_stderr(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.app(["studies", "list", "--format", "invalid"])
    captured = capsys.readouterr()
    assert exc.value.code == 2 and captured.out == ""
    assert "invalid choice" in captured.err


def test_console_entrypoint_with_offline_simulation(monkeypatch, capsys):
    monkeypatch.setenv("IMEDNET_API_KEY", "offline")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "offline")
    monkeypatch.setenv("IMEDNET_OIDC_TOKEN", "")
    monkeypatch.setenv("IMEDNET_SIMULATION_MODE", "true")
    cli.app(["studies", "list", "--format", "json"])
    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert response["schemaVersion"] == "1.0" and response["resource"] == "studies"
    assert response["count"] == len(response["items"])
    assert "Fetching" not in captured.out


def test_users_and_codings_json_complete_aliases_and_no_contamination(sdk, capsys):
    """Verify complete JSON aliases, values, and zero output contamination for users and codings."""
    from imednet.models.codings import Coding
    from imednet.models.users import Role, User

    user = User.model_validate(
        {
            "userId": "U-JSON-1",
            "login": "jdoe_json",
            "firstName": "John",
            "lastName": "Doe",
            "email": "jdoe@example.com",
            "userActiveInStudy": True,
            "roles": [Role.model_validate({"roleId": "R-1", "name": "Auditor"})],
        }
    )
    sdk.users.list.return_value = [user]

    cli.app(["users", "list", "STUDY-JSON", "--include-inactive", "--format", "json"])
    captured_users = capsys.readouterr()
    assert captured_users.err == ""
    # Ensure raw output is valid JSON without extraneous text prefix or suffix
    user_payload = json.loads(captured_users.out)
    assert user_payload["schemaVersion"] == "1.0"
    assert user_payload["status"] == "complete"
    assert user_payload["resource"] == "users"
    assert user_payload["count"] == 1
    assert user_payload["items"][0]["userId"] == "U-JSON-1"
    assert user_payload["items"][0]["login"] == "jdoe_json"
    assert user_payload["items"][0]["userActiveInStudy"] is True
    assert user_payload["items"][0]["name"] == "John Doe"

    coding = Coding.model_validate(
        {
            "codingId": 9001,
            "dictionaryName": "MedDRA",
            "dictionaryVersion": "26.1",
            "variable": "AETERM",
            "value": "Pyrexia",
            "code": "10037660",
        }
    )
    sdk.codings.list.return_value = [coding]

    cli.app(["codings", "list", "STUDY-JSON", "-f", "dictionaryName=MedDRA", "--format", "json"])
    captured_codings = capsys.readouterr()
    assert captured_codings.err == ""
    coding_payload = json.loads(captured_codings.out)
    assert coding_payload["schemaVersion"] == "1.0"
    assert coding_payload["status"] == "complete"
    assert coding_payload["resource"] == "codings"
    assert coding_payload["count"] == 1
    assert coding_payload["items"][0]["codingId"] == 9001
    assert coding_payload["items"][0]["dictionaryName"] == "MedDRA"
    assert coding_payload["items"][0]["dictionaryVersion"] == "26.1"
    assert coding_payload["items"][0]["variable"] == "AETERM"
    assert coding_payload["items"][0]["value"] == "Pyrexia"
    assert coding_payload["items"][0]["code"] == "10037660"


@pytest.mark.parametrize("command", ["status", "wait"])
def test_jobs_json_preserves_nested_results_and_full_model_equality(sdk, capsys, command):
    """Verify JSON mode retains nested results, non-summary fields, and full alias equality."""
    job = JobStatus(
        batchId="B_FULL",
        jobId="J_FULL",
        state="COMPLETED",
        progress=100,
        dateCreated="2026-11-17T10:00:00Z",
        dateStarted="2026-11-17T10:01:00Z",
        dateFinished="2026-11-17T10:05:00Z",
        resultUrl="https://example.com/api/v1/jobs/B_FULL/result",
        results={
            "total": 3,
            "success": 2,
            "failed": 1,
            "details": [{"id": "rec-1", "status": "PASS"}, {"id": "rec-2", "status": "FAIL"}],
        },
    )
    sdk.get_job.return_value = job
    sdk.poll_job.return_value = job
    cli.app(["jobs", command, "S", "B_FULL", "--format", "json"])
    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert response["schemaVersion"] == "1.0"
    assert response["status"] == "complete"
    assert response["resource"] == "jobs"
    assert response["count"] == 1
    expected_item = job.model_dump(mode="json", by_alias=True)
    assert response["items"] == [expected_item]
    assert response["items"][0] == expected_item
    assert response["items"][0]["results"] == job.results
    assert response["items"][0]["resultUrl"] == job.result_url
    assert response["items"][0]["dateStarted"] == job.date_started
    assert "Fetching" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("argv", "attr", "expected_args", "expected_kwargs"),
    [
        (
            ["studies", "list", "-f", "study_key=S1", "-f", "study_type=Clinical"],
            "studies",
            (),
            {"study_key": "S1", "study_type": "Clinical"},
        ),
        (
            ["sites", "list", "S", "-f", "site_name=SiteA"],
            "sites",
            ("S",),
            {"site_name": "SiteA"},
        ),
        (
            ["forms", "list", "S", "-f", "form_key=DM", "-f", "disabled=false"],
            "forms",
            ("S",),
            {"form_key": "DM", "disabled": False},
        ),
        (
            ["variables", "list", "S", "-f", "variable_name=AGE"],
            "variables",
            ("S",),
            {"variable_name": "AGE"},
        ),
        (
            ["intervals", "list", "S", "-f", "interval_name=Screening"],
            "intervals",
            ("S",),
            {"interval_name": "Screening"},
        ),
        (
            ["visits", "list", "S", "-f", "subject_key=001"],
            "visits",
            ("S",),
            {"subject_key": "001"},
        ),
        (
            ["queries", "list", "S", "-f", "subject_key=001"],
            "queries",
            ("S",),
            {"subject_key": "001"},
        ),
        (
            ["record-revisions", "list", "S", "-f", "record_revision>=2"],
            "record_revisions",
            ("S",),
            {"record_revision": (">=", 2)},
        ),
        (
            [
                "records",
                "list",
                "S",
                "-f",
                "form_key=AE",
                "--record-data-filter",
                "AESER==Serious",
            ],
            "records",
            ("S",),
            {"form_key": "AE", "record_data_filter": "AESER==Serious"},
        ),
        (
            ["records", "list", "S", "--record-data-filter", "none"],
            "records",
            ("S",),
            {"record_data_filter": "none"},
        ),
    ],
)
def test_list_filters_reach_sdk_in_json_mode(
    sdk,
    capsys,
    argv,
    attr,
    expected_args,
    expected_kwargs,
):
    getattr(sdk, attr).list.return_value = []
    cli.app([*argv, "--format", "json"])
    getattr(sdk, attr).list.assert_called_once_with(*expected_args, **expected_kwargs)
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schemaVersion"] == "1.0"
    assert payload["status"] == "complete"
    assert payload["resource"] == attr
    assert payload["count"] == 0
    assert payload["items"] == []


@pytest.mark.parametrize(
    ("argv", "expected_resource", "setup_attr"),
    [
        (["studies", "list"], "studies", "studies"),
        (["sites", "list", "S"], "sites", "sites"),
        (["forms", "list", "S"], "forms", "forms"),
        (["intervals", "list", "S"], "intervals", "intervals"),
        (["queries", "list", "S"], "queries", "queries"),
        (["record-revisions", "list", "S"], "record_revisions", "record_revisions"),
        (["variables", "list", "S"], "variables", "variables"),
        (["visits", "list", "S"], "visits", "visits"),
        (["records", "list", "S"], "records", "records"),
        (["subjects", "list", "S"], "subjects", "subjects"),
        (["codings", "list", "S"], "codings", "codings"),
        (["users", "list", "S"], "users", "users"),
        (["jobs", "status", "S", "B"], "jobs", "get_job"),
        (["jobs", "wait", "S", "B"], "jobs", "poll_job"),
    ],
)
def test_env_output_format_json_honored_across_all_14_core_commands(
    sdk,
    monkeypatch,
    capsys,
    argv,
    expected_resource,
    setup_attr,
):
    """Verify IMEDNET_CLI_OUTPUT_FORMAT=json produces JSON across all 14 core --format commands."""
    import os

    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    if setup_attr in ("get_job", "poll_job"):
        getattr(sdk, setup_attr).return_value = JobStatus(batchId="B", state="COMPLETED")
    else:
        getattr(sdk, setup_attr).list.return_value = []

    cli.app(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schemaVersion"] == "1.0"
    assert payload["status"] == "complete"
    assert payload["resource"] == expected_resource
    assert os.environ.get("IMEDNET_CLI_OUTPUT_FORMAT") == "json"


def test_datadict_diff_honors_env_output_format_json_and_explicit_overrides(
    tmp_path, monkeypatch, capsys
):
    """Verify datadict diff honors IMEDNET_CLI_OUTPUT_FORMAT=json and explicit --format overrides."""
    import os

    old_file = tmp_path / "old.json"
    new_file = tmp_path / "new.json"
    old_file.write_text(
        json.dumps(
            {
                "metadata": {"schemaVersion": "1.0.0"},
                "forms": [{"formKey": "DM", "formName": "Demographics", "variables": []}],
                "unassignedBusinessLogic": [],
            }
        ),
        encoding="utf-8",
    )
    new_file.write_text(
        json.dumps(
            {
                "metadata": {"schemaVersion": "1.0.0"},
                "forms": [{"formKey": "DM", "formName": "Demographics v2", "variables": []}],
                "unassignedBusinessLogic": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    cli.app(["datadict", "diff", str(old_file), str(new_file)])
    diff_json = json.loads(capsys.readouterr().out)
    assert diff_json["has_changes"] is True
    assert os.environ.get("IMEDNET_CLI_OUTPUT_FORMAT") == "json"

    # Explicit --format text overrides IMEDNET_CLI_OUTPUT_FORMAT=json
    cli.app(["datadict", "diff", str(old_file), str(new_file), "--format", "text"])
    text_out = capsys.readouterr().out
    assert "Demographics v2" in text_out
    with pytest.raises(json.JSONDecodeError):
        json.loads(text_out)
    assert os.environ.get("IMEDNET_CLI_OUTPUT_FORMAT") == "json"

    # Explicit --format md overrides IMEDNET_CLI_OUTPUT_FORMAT=json
    cli.app(["datadict", "diff", str(old_file), str(new_file), "--format", "md"])
    md_out = capsys.readouterr().out
    assert "#" in md_out
    with pytest.raises(json.JSONDecodeError):
        json.loads(md_out)
    assert os.environ.get("IMEDNET_CLI_OUTPUT_FORMAT") == "json"


def test_explicit_table_format_overrides_env_output_format_json(sdk, monkeypatch, capsys):
    """Verify explicit --format table overrides IMEDNET_CLI_OUTPUT_FORMAT=json."""
    import os

    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    sdk.studies.list.return_value = [Study(studyKey="S1", studyName="Override Trial")]
    cli.app(["studies", "list", "--format", "table", "--no-interactive"])
    captured = capsys.readouterr()
    assert "Found 1 studies:" in captured.out
    assert "Override Trial" in captured.out
    assert os.environ.get("IMEDNET_CLI_OUTPUT_FORMAT") == "json"


def test_env_output_format_json_skips_interactive_limit_and_grid(sdk, monkeypatch, capsys):
    """Verify IMEDNET_CLI_OUTPUT_FORMAT=json bypasses the 100-item TTY cap and InteractiveDataGrid."""
    import imednet.cli.utils.grid as grid_mod

    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    grid_spy = MagicMock(return_value=True)
    monkeypatch.setattr(grid_mod, "is_interactive_terminal", lambda: True)
    monkeypatch.setattr(grid_mod, "run_interactive_grid", grid_spy)

    sdk.studies.list.return_value = [
        Study(studyKey=f"S-{idx}", studyName=f"Study {idx}") for idx in range(150)
    ]
    cli.app(["studies", "list"])
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["count"] == 150
    assert len(payload["items"]) == 150
    grid_spy.assert_not_called()


@pytest.mark.parametrize("output_ext", ["json", "csv"])
def test_records_output_file_conflicts_with_env_output_format_json(
    sdk, tmp_path, monkeypatch, capsys, output_ext
):
    """Verify records list --output combined with IMEDNET_CLI_OUTPUT_FORMAT=json exits 1."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    with pytest.raises(SystemExit) as exc:
        cli.app(["records", "list", "S", "--output", output_ext])
    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.out == ""
    assert "Choose --format json for stdout or --output for a file." in captured.err
    assert not (tmp_path / f"records.{output_ext}").exists()
    sdk.records.list.assert_not_called()
