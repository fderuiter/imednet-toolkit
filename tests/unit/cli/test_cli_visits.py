"""Unit tests for the visits CLI subparser."""

from __future__ import annotations

import importlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet.errors import ApiError
from imednet.models.visits import Visit


class Result:
    """CLI execution result wrapper."""

    def __init__(self, exit_code: int, stdout: str, stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.output = stdout + stderr


class CliRunner:
    """Helper to run CLI commands and capture output and exit codes."""

    def invoke(self, app: object, args: list[str]) -> Result:
        out = io.StringIO()
        err = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                app(args)
        except SystemExit as e:
            exit_code = e.code or 0
        except Exception:
            import traceback

            err.write(traceback.format_exc())
            exit_code = 1

        return Result(exit_code, out.getvalue(), err.getvalue())


@pytest.fixture(autouse=True)
def reload_cli() -> None:
    """Ensure cli module is fresh for tests."""
    import imednet.cli

    importlib.reload(imednet.cli)
    yield
    importlib.reload(imednet.cli)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required environment variables for CLI invocation."""
    monkeypatch.setenv("IMEDNET_API_KEY", "key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "secret")


@pytest.fixture
def runner() -> CliRunner:
    """Provide a CliRunner instance."""
    return CliRunner()


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Provide a mocked SDK instance."""
    mock_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))
    return mock_sdk


def test_visits_list_table_output(runner: CliRunner, sdk: MagicMock) -> None:
    """Test successful listing of study visits with table output."""
    visit = MagicMock()
    visit.visit_id = 501
    visit.interval_name = "Baseline"
    visit.subject_key = "SUBJ-001"
    visit.visit_date = "2023-05-10"
    sdk.visits.list.return_value = [visit]

    result = runner.invoke(cli.app, ["visits", "list", "STUDY1"])

    assert result.exit_code == 0
    sdk.visits.list.assert_called_once_with("STUDY1")
    assert "Found 1 visits:" in result.stdout
    assert "501" in result.stdout
    assert "Baseline" in result.stdout
    assert "SUBJ-001" in result.stdout
    assert "2023-05-10" in result.stdout


def test_visits_list_output_json(runner: CliRunner, sdk: MagicMock) -> None:
    """Test visits list with JSON output formatting."""
    visit = Visit(
        visitId=501,
        intervalName="Baseline",
        subjectKey="SUBJ-001",
        visitDate="2023-05-10",
        studyKey="STUDY1",
    )
    sdk.visits.list.return_value = [visit]

    result = runner.invoke(cli.app, ["visits", "list", "STUDY1", "--format", "json"])

    assert result.exit_code == 0
    sdk.visits.list.assert_called_once_with("STUDY1")
    data = json.loads(result.stdout)
    assert data["schemaVersion"] == "1.0"
    assert data["status"] == "complete"
    assert data["resource"] == "visits"
    assert data["count"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["visitId"] == 501
    assert data["items"][0]["intervalName"] == "Baseline"
    assert data["items"][0]["subjectKey"] == "SUBJ-001"
    assert data["items"][0]["visitDate"] == "2023-05-10"


def test_visits_list_empty_table(runner: CliRunner, sdk: MagicMock) -> None:
    """Test visits list when no visits are found (table output)."""
    sdk.visits.list.return_value = []

    result = runner.invoke(cli.app, ["visits", "list", "STUDY1"])

    assert result.exit_code == 0
    sdk.visits.list.assert_called_once_with("STUDY1")
    assert "No visits found." in result.stdout


def test_visits_list_empty_json(runner: CliRunner, sdk: MagicMock) -> None:
    """Test visits list when no visits are found (JSON output)."""
    sdk.visits.list.return_value = []

    result = runner.invoke(cli.app, ["visits", "list", "STUDY1", "--format", "json"])

    assert result.exit_code == 0
    sdk.visits.list.assert_called_once_with("STUDY1")
    data = json.loads(result.stdout)
    assert data["schemaVersion"] == "1.0"
    assert data["status"] == "complete"
    assert data["resource"] == "visits"
    assert data["count"] == 0
    assert data["items"] == []


def test_visits_list_missing_study_key(runner: CliRunner) -> None:
    """Test that visits list requires study_key argument."""
    result = runner.invoke(cli.app, ["visits", "list"])

    assert result.exit_code != 0
    assert "the following arguments are required: study_key" in result.stderr


def test_visits_list_api_error(runner: CliRunner, sdk: MagicMock) -> None:
    """Test API error handling during visits list command."""
    sdk.visits.list.side_effect = ApiError("Visits service unavailable")

    result = runner.invoke(cli.app, ["visits", "list", "STUDY1"])

    assert result.exit_code == 1
    assert "API Error" in result.stderr
    assert "API Error" not in result.stdout


def test_visits_list_closes_sdk(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that sdk is closed after visits list command."""
    sdk.visits.list.return_value = []

    result = runner.invoke(cli.app, ["visits", "list", "STUDY1"])

    assert result.exit_code == 0
    sdk.close.assert_called_once()
