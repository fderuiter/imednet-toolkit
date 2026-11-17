"""Unit tests for workflow CLI commands."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet_workflows.cli import setup_subject_parser


class Result:
    """CLI execution result."""

    def __init__(self, exit_code: int, stdout: str, stderr: str = "") -> None:
        """Initialize the result object."""
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.output = stdout + stderr


class CliRunner:
    """Simple test runner for CLI applications."""

    def invoke(self, app: Any, args: list[str]) -> Result:
        """Invoke the CLI application with given arguments."""
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
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required environment variables for each test."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test-api-key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-security-key")


@pytest.fixture
def runner() -> CliRunner:
    """Fixture providing a CliRunner instance."""
    return CliRunner()


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Provide a mocked SDK and patch get_sdk."""
    mock_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))
    return mock_sdk


@pytest.fixture
def mock_workflow(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Provide a mocked SubjectDataWorkflow."""
    workflow = MagicMock()
    monkeypatch.setattr(
        "imednet_workflows.cli.SubjectDataWorkflow", MagicMock(return_value=workflow)
    )
    workflow.get_all_subject_data.return_value = MagicMock()
    return workflow


def test_subject_data_documented_flags(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test retrieving subject data using documented --study-key and --subject-key flags."""
    result = runner.invoke(
        cli.app, ["subject-data", "--study-key", "STUDY-DOC", "--subject-key", "SUBJ-DOC"]
    )
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with("STUDY-DOC", "SUBJ-DOC")


def test_subject_data_short_flags(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test retrieving subject data using short -s and -k flags."""
    result = runner.invoke(cli.app, ["subject-data", "-s", "STUDY-SHORT", "-k", "SUBJ-SHORT"])
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with("STUDY-SHORT", "SUBJ-SHORT")


def test_subject_data_alias_flags(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test retrieving subject data using the --subject alias flag."""
    result = runner.invoke(
        cli.app, ["subject-data", "--study-key", "STUDY-ALIAS", "--subject", "SUBJ-ALIAS"]
    )
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with("STUDY-ALIAS", "SUBJ-ALIAS")

    mock_workflow.reset_mock()
    result_short = runner.invoke(
        cli.app, ["subject-data", "-s", "STUDY-ALIAS2", "--subject", "SUBJ-ALIAS2"]
    )
    assert result_short.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with("STUDY-ALIAS2", "SUBJ-ALIAS2")


def test_subject_data_positional_arguments(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test retrieving subject data using positional study_key and subject_key."""
    result = runner.invoke(cli.app, ["subject-data", "STUDY-POS", "SUBJ-POS"])
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with("STUDY-POS", "SUBJ-POS")


def test_subject_data_flags_precedence_over_positional(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test that flags take precedence over positional arguments when both are supplied."""
    result = runner.invoke(
        cli.app,
        [
            "subject-data",
            "STUDY-POS",
            "SUBJ-POS",
            "--study-key",
            "STUDY-FLAG",
            "--subject-key",
            "SUBJ-FLAG",
        ],
    )
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with("STUDY-FLAG", "SUBJ-FLAG")


def test_subject_data_missing_both_arguments_error(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test that omitting both study_key and subject_key fails with exit code 2."""
    result = runner.invoke(cli.app, ["subject-data"])
    assert result.exit_code == 2
    assert "study_key" in result.stderr
    assert "subject_key" in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


def test_subject_data_missing_subject_key_flag_error(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test that omitting subject_key when providing --study-key fails with exit code 2."""
    result = runner.invoke(cli.app, ["subject-data", "--study-key", "STUDY-1"])
    assert result.exit_code == 2
    assert "subject_key" in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


def test_subject_data_missing_study_key_flag_error(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test that omitting study_key when providing --subject-key fails with exit code 2."""
    result = runner.invoke(cli.app, ["subject-data", "--subject-key", "SUBJ-1"])
    assert result.exit_code == 2
    assert "study_key" in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


def test_subject_data_missing_study_key_alias_error(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test that omitting study_key when providing --subject fails with exit code 2."""
    result = runner.invoke(cli.app, ["subject-data", "--subject", "SUBJ-1"])
    assert result.exit_code == 2
    assert "study_key" in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


def test_subject_data_missing_subject_key_positional_error(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Test that omitting subject_key in positional syntax fails with exit code 2."""
    result = runner.invoke(cli.app, ["subject-data", "STUDY-1"])
    assert result.exit_code == 2
    assert "subject_key" in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


def test_setup_subject_parser_standalone() -> None:
    """Test standalone setup_subject_parser setup and error dispatch."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    setup_subject_parser(subparsers)

    parsed = parser.parse_args(["subject-data", "-s", "S1", "-k", "K1"])
    assert parsed.study_key_flag == "S1"
    assert parsed.subject_key_flag == "K1"
    assert parsed.study_key is None
    assert parsed.subject_key is None
