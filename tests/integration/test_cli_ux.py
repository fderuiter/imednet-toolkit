"""CLI End-to-End UX Coverage tests."""

import os
from unittest.mock import MagicMock, patch

import pytest


class Result:
    def __init__(self, exit_code, stdout, stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.output = stdout + stderr


class CliRunner:
    def invoke(self, app, args):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                if hasattr(app, "parse_args"):
                    pass
                app(args)
        except SystemExit as e:
            exit_code = e.code or 0
        except Exception:
            import traceback

            err.write(traceback.format_exc())
            exit_code = 1

        # We also need to catch argparse sys.exit(2)
        return Result(exit_code, out.getvalue(), err.getvalue())


from imednet.cli import app
from imednet.errors import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    RateLimitError,
)


@pytest.fixture
def runner():
    """Create a Typer CLI runner."""
    return CliRunner()


@pytest.fixture
def mock_sdk(monkeypatch):
    """Mock the SDK for CLI tests."""
    sdk = MagicMock()
    # Patch get_sdk in both places it might be used
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=sdk))
    monkeypatch.setattr(
        "imednet.cli.decorators.get_sdk", MagicMock(return_value=sdk), raising=False
    )
    # Also set dummy env vars to avoid load_config failure if it's called anyway
    monkeypatch.setenv("IMEDNET_API_KEY", "dummy")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "dummy")
    return sdk


def test_missing_credentials(runner, monkeypatch):
    """Confirm clear error when credentials are missing."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)

    result = runner.invoke(app, ["studies", "list"])

    assert result.exit_code == 1
    # Check for core message, ignoring possible line breaks from rich wrapping
    assert "IMEDNET_API_KEY" in result.stdout
    assert "IMEDNET_SECURITY_KEY" in result.stdout
    assert "environment variables must be" in result.stdout.replace("\n", " ")
    assert "set" in result.stdout


def test_authentication_error_handling(runner, mock_sdk):
    """Confirm actionable message for 401 errors."""
    mock_sdk.studies.list.side_effect = AuthenticationError("Invalid API Key")

    result = runner.invoke(app, ["studies", "list"])

    assert result.exit_code == 1
    assert "Authentication Failed" in result.stdout
    assert "Invalid API Key" in result.stdout


def test_authorization_error_handling(runner, mock_sdk):
    """Confirm actionable message for 403 errors."""
    mock_sdk.studies.list.side_effect = AuthorizationError("Insufficient permissions")

    result = runner.invoke(app, ["studies", "list"])

    assert result.exit_code == 1
    assert "Permission Denied" in result.stdout
    assert "Insufficient permissions" in result.stdout


def test_rate_limit_error_handling(runner, mock_sdk):
    """Confirm actionable message for 429 errors."""
    mock_sdk.studies.list.side_effect = RateLimitError("Rate limit exceeded. Try again in 60s.")

    result = runner.invoke(app, ["studies", "list"])

    assert result.exit_code == 1
    assert "Rate Limit Exceeded" in result.stdout
    assert "Try again in 60s" in result.stdout


def test_not_found_error_handling(runner, mock_sdk):
    """Confirm actionable message for 404 errors."""
    mock_sdk.sites.list.side_effect = NotFoundError("Study not found: MISSING")

    result = runner.invoke(app, ["sites", "list", "MISSING"])

    assert result.exit_code == 1
    assert "Not Found" in result.stdout
    assert "Study not found: MISSING" in result.stdout


def test_malformed_filter_input(runner, mock_sdk):
    """Confirm clear error for invalid filter syntax."""
    result = runner.invoke(app, ["subjects", "list", "STUDY", "--filter", "invalid_filter"])

    assert result.exit_code == 1
    assert "Invalid filter format" in result.stdout
    assert "key=value" in result.stdout


def test_invalid_output_format(runner, mock_sdk):
    """Confirm clear error for unsupported output formats."""
    result = runner.invoke(app, ["records", "list", "STUDY", "--output", "pdf"])

    assert result.exit_code == 1
    assert "Invalid output format" in result.stdout


def test_secret_masking_in_errors(runner, mock_sdk, monkeypatch):
    """Ensure secrets are not leaked in error output."""
    secret_value = "super-secret-key-123"
    monkeypatch.setenv("IMEDNET_API_KEY", secret_value)

    # Simulate an error that might contain the secret if not careful
    mock_sdk.studies.list.side_effect = Exception(f"Failed with key {secret_value}")

    result = runner.invoke(app, ["studies", "list"])

    assert result.exit_code == 1
    assert secret_value not in result.stdout
    assert "Unexpected error" in result.stdout


def test_help_text_completeness(runner):
    """Verify help text is present for key commands."""
    commands = [
        [],
        ["studies"],
        ["studies", "list"],
        ["export"],
        ["export", "csv"],
        ["records", "list"],
    ]

    for cmd in commands:
        result = runner.invoke(app, [*cmd, "--help"])
        assert result.exit_code == 0
        assert "usage:" in result.stdout.lower()
        # Argparse uses "options:" or "positional arguments:"
        assert "options" in result.stdout.lower()


def test_active_subparser_help_without_subactions(runner, monkeypatch):
    """Verify contextual help is displayed when subcommands are invoked without arguments."""
    # Root CLI invocation displays root help banner
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "iMednet SDK Command Line Interface" in result.stdout

    # records subcommand displays records subparser help
    result = runner.invoke(app, ["records"])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert " records " in result.stdout
    assert "iMednet SDK Command Line Interface" not in result.stdout

    # studies subcommand displays studies subparser help
    result = runner.invoke(app, ["studies"])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert " studies " in result.stdout
    assert "iMednet SDK Command Line Interface" not in result.stdout

    # export subcommand displays export subparser help
    result = runner.invoke(app, ["export"])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert " export " in result.stdout
    assert "iMednet SDK Command Line Interface" not in result.stdout

    # --high-contrast records displays records help and activates high contrast
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    result = runner.invoke(app, ["--high-contrast", "records"])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert " records " in result.stdout
    assert "iMednet SDK Command Line Interface" not in result.stdout
    assert os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"

    # workflows plugin subcommand displays workflows help
    result = runner.invoke(app, ["workflows"])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert " workflows " in result.stdout
    assert "iMednet SDK Command Line Interface" not in result.stdout


def test_keyboard_interrupt_handling(runner, mock_sdk):
    """Verify clean exit on Ctrl+C."""
    mock_sdk.studies.list.side_effect = KeyboardInterrupt()

    result = runner.invoke(app, ["studies", "list"])

    assert result.exit_code == 130
    assert "Aborted by user" in result.stdout


def test_output_file_correctness_csv(runner, mock_sdk, tmp_path):
    """Validate CSV output content and format."""
    mock_record = MagicMock()
    mock_record.record_id = 123
    mock_record.subject_key = "SUBJ1"
    mock_record.form_key = "FORM1"
    mock_record.record_status = "Active"
    mock_record.date_created = "2023-01-01"
    mock_record.model_dump.return_value = {
        "record_id": 123,
        "subject_key": "SUBJ1",
        "form_key": "FORM1",
        "record_status": "Active",
        "date_created": "2023-01-01",
    }

    mock_sdk.records.list.return_value = [mock_record]

    with patch("os.getcwd", return_value=str(tmp_path)):
        os.chdir(tmp_path)
        result = runner.invoke(app, ["records", "list", "STUDY", "--output", "csv"])

    assert result.exit_code == 0
    csv_file = tmp_path / "records.csv"
    assert csv_file.exists()

    content = csv_file.read_text()
    assert "record_id,subject_key,form_key,record_status,date_created" in content
    assert "123,SUBJ1,FORM1,Active,2023-01-01" in content


def test_output_file_correctness_json(runner, mock_sdk, tmp_path):
    """Validate JSON output content and format."""
    mock_record = MagicMock()
    mock_record.model_dump.return_value = {"id": 123, "status": "Active"}
    mock_sdk.records.list.return_value = [mock_record]

    with patch("os.getcwd", return_value=str(tmp_path)):
        os.chdir(tmp_path)
        result = runner.invoke(app, ["records", "list", "STUDY", "--output", "json"])

    assert result.exit_code == 0
    json_file = tmp_path / "records.json"
    assert json_file.exists()

    import json

    with open(json_file) as f:
        data = json.load(f)

    assert len(data) == 1
    assert data[0]["id"] == 123
    assert data[0]["status"] == "Active"
