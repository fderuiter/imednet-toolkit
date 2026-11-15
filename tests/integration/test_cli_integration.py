"""Unit tests for cli integration."""

from pathlib import Path
from unittest.mock import MagicMock

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


from imednet import cli


def test_cli_rejects_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that cli rejects missing credentials."""
    runner = CliRunner()
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)

    result = runner.invoke(cli.app, ["studies", "list"])

    assert result.exit_code == 1
    assert "IMEDNET_API_KEY" in result.stderr


def test_studies_list_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that studies list success."""
    runner = CliRunner()
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")

    sdk = MagicMock()
    obj = MagicMock()
    obj.study_key = "study1"
    obj.study_name = "Study One"
    obj.study_type = "Type"
    obj.sponsor_key = "Sponsor"
    sdk.studies.list.return_value = [obj]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=sdk))

    result = runner.invoke(cli.app, ["studies", "list"])

    assert result.exit_code == 0
    sdk.studies.list.assert_called_once_with()
    assert "study1" in result.stdout


def test_records_list_output_csv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that records list output csv."""
    runner = CliRunner()
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")

    record = MagicMock()
    record.model_dump.return_value = {"id": 1}
    sdk = MagicMock()
    sdk.records.list.return_value = [record]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=sdk))

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["records", "list", "ST", "--output", "csv"])
    assert result.exit_code == 0
    assert Path("records.csv").exists()
    sdk.records.list.assert_called_once_with("ST")


def test_extract_records_cli_parses_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that extract records cli parses filters."""
    runner = CliRunner()
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")

    workflow = MagicMock()
    workflow.extract_records_by_criteria.return_value = [1]
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow", MagicMock(return_value=workflow)
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    result = runner.invoke(
        cli.app,
        [
            "workflows",
            "extract-records",
            "ST",
            "--record-filter",
            "form_key=DEMOG",
            "--subject-filter",
            "subject_status=Screened",
            "--visit-filter",
            "visit_key=BASE",
        ],
    )

    assert result.exit_code == 0
    workflow.extract_records_by_criteria.assert_called_once_with(
        study_key="ST",
        record_filter={"form_key": "DEMOG"},
        subject_filter={"subject_status": "Screened"},
        visit_filter={"visit_key": "BASE"},
    )


def test_invalid_filter_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that invalid filter string."""
    runner = CliRunner()
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    result = runner.invoke(cli.app, ["subjects", "list", "ST", "--filter", "badfilter"])

    assert result.exit_code == 1
    assert "Invalid filter format" in result.stderr
