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
        (["codings", "list", "S"], "codings", Coding(codingId="C1", dictionaryName="MedDRA")),
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
    cli.app(["subjects", "list", "S", "--filter", "active=false", "--format", "json"])
    sdk.subjects.list.assert_called_once_with("S", active=False)
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
