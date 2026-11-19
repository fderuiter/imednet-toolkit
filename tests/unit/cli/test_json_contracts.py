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


def test_console_entrypoint_with_offline_simulation():
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env.update(
        IMEDNET_API_KEY="offline",
        IMEDNET_SECURITY_KEY="offline",
        IMEDNET_OIDC_TOKEN="",
        IMEDNET_SIMULATION_MODE="true",
    )
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from imednet.cli import app; app()",
            "studies",
            "list",
            "--format",
            "json",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    response = json.loads(process.stdout)
    assert response["schemaVersion"] == "1.0" and response["resource"] == "studies"
    assert response["count"] == len(response["items"])
    assert "Fetching" not in process.stdout


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
