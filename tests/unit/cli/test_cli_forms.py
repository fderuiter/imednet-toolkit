"""Unit tests for the forms CLI subparser."""

from __future__ import annotations

import importlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet.errors import ApiError
from imednet.models.forms import Form


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
    importlib.reload(cli)
    yield
    importlib.reload(cli)


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


def test_forms_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test successful listing of study forms."""
    form = MagicMock()
    form.form_id = 101
    form.form_key = "DEMOG"
    form.form_name = "Demographics"
    form.form_type = "Standard"
    form.disabled = False
    sdk.forms.list.return_value = [form]

    result = runner.invoke(cli.app, ["forms", "list", "STUDY1"])

    assert result.exit_code == 0
    sdk.forms.list.assert_called_once_with("STUDY1")
    assert "101" in result.stdout
    assert "DEMOG" in result.stdout
    assert "Demographics" in result.stdout
    assert "Standard" in result.stdout


def test_forms_list_empty(runner: CliRunner, sdk: MagicMock) -> None:
    """Test forms list when no forms are found."""
    sdk.forms.list.return_value = []

    result = runner.invoke(cli.app, ["forms", "list", "STUDY1"])

    assert result.exit_code == 0
    sdk.forms.list.assert_called_once_with("STUDY1")
    assert "No forms found." in result.stdout


def test_forms_list_missing_study_key(runner: CliRunner) -> None:
    """Test that forms list requires study_key argument."""
    result = runner.invoke(cli.app, ["forms", "list"])

    assert result.exit_code != 0
    assert "the following arguments are required: study_key" in result.stderr


def test_forms_list_api_error(runner: CliRunner, sdk: MagicMock) -> None:
    """Test API error handling during forms list command."""
    sdk.forms.list.side_effect = ApiError("Forms service unavailable")

    result = runner.invoke(cli.app, ["forms", "list", "STUDY1"])

    assert result.exit_code == 1
    assert "API Error" in result.stderr
    assert "API Error" not in result.stdout


def test_forms_list_output_json(runner: CliRunner, sdk: MagicMock) -> None:
    """Test forms list with JSON output formatting."""
    form = Form(
        formId=101,
        formKey="DEMOG",
        formName="Demographics",
        formType="Standard",
        disabled=False,
    )
    sdk.forms.list.return_value = [form]

    result = runner.invoke(cli.app, ["forms", "list", "STUDY1", "--format", "json"])

    assert result.exit_code == 0
    sdk.forms.list.assert_called_once_with("STUDY1")
    data = json.loads(result.stdout)
    assert data["schemaVersion"] == "1.0"
    assert data["status"] == "complete"
    assert data["resource"] == "forms"
    assert data["count"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["formId"] == 101
    assert data["items"][0]["formKey"] == "DEMOG"
    assert data["items"][0]["formName"] == "Demographics"


def test_forms_list_closes_sdk(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that sdk is closed after forms list command."""
    sdk.forms.list.return_value = []

    result = runner.invoke(cli.app, ["forms", "list", "STUDY1"])

    assert result.exit_code == 0
    sdk.close.assert_called_once()
