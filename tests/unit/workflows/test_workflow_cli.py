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


@pytest.mark.parametrize(
    ("args", "expected_study", "expected_subj"),
    [
        (["--study-key", "S1", "K1"], "S1", "K1"),
        (["K1", "--study-key", "S1"], "S1", "K1"),
        (["-s", "S1", "K1"], "S1", "K1"),
        (["K1", "-s", "S1"], "S1", "K1"),
        (["--subject-key", "K1", "S1"], "S1", "K1"),
        (["S1", "--subject-key", "K1"], "S1", "K1"),
        (["-k", "K1", "S1"], "S1", "K1"),
        (["S1", "-k", "K1"], "S1", "K1"),
        (["--subject", "K1", "S1"], "S1", "K1"),
        (["S1", "--subject", "K1"], "S1", "K1"),
    ],
)
def test_subject_data_hybrid_single_flag_single_positional_orderings(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    args: list[str],
    expected_study: str,
    expected_subj: str,
) -> None:
    """Test all single-flag and single-positional ordering combinations and aliases."""
    result = runner.invoke(cli.app, ["subject-data", *args])
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with(expected_study, expected_subj)


@pytest.mark.parametrize(
    ("args", "expected_study", "expected_subj"),
    [
        (["--subject-key", "SUBJ-REV", "--study-key", "STUDY-REV"], "STUDY-REV", "SUBJ-REV"),
        (["-k", "SUBJ-SREV", "-s", "STUDY-SREV"], "STUDY-SREV", "SUBJ-SREV"),
        (["--subject", "SUBJ-AREV", "--study-key", "STUDY-AREV"], "STUDY-AREV", "SUBJ-AREV"),
        (["--subject", "SUBJ-AREV2", "-s", "STUDY-AREV2"], "STUDY-AREV2", "SUBJ-AREV2"),
    ],
)
def test_subject_data_both_flags_reversed_order(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    args: list[str],
    expected_study: str,
    expected_subj: str,
) -> None:
    """Test both flags provided in reversed argument order."""
    result = runner.invoke(cli.app, ["subject-data", *args])
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with(expected_study, expected_subj)


@pytest.mark.parametrize(
    "args",
    [
        ["-s", "S1", "--study-key", "S1", "-k", "K1"],
        ["--study-key", "S1", "-s", "S1", "--subject-key", "K1"],
        ["-s", "S1", "--subject-key", "K1", "--subject", "K1"],
        ["-s", "S1", "-k", "K1", "--subject-key", "K1"],
        ["-s", "S1", "-s", "S1", "-k", "K1", "-k", "K1"],
    ],
)
def test_subject_data_duplicate_flags_non_conflicting(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    args: list[str],
) -> None:
    """Test duplicate flags with identical values succeed."""
    result = runner.invoke(cli.app, ["subject-data", *args])
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with("S1", "K1")


@pytest.mark.parametrize(
    "args",
    [
        ["--study-key", "S1", "--study-key", "S2", "--subject-key", "K1"],
        ["-s", "S1", "--study-key", "S2", "-k", "K1"],
        ["--study-key", "S1", "-s", "S2", "-k", "K1"],
    ],
)
def test_subject_data_duplicate_flags_conflicting_study_key(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    args: list[str],
) -> None:
    """Test duplicate study-key flags with conflicting values fail with code 2."""
    result = runner.invoke(cli.app, ["subject-data", *args])
    assert result.exit_code == 2
    assert "study_key" in result.stderr
    assert "conflicting" in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["-s", "S1", "--subject-key", "K1", "--subject-key", "K2"],
        ["-s", "S1", "-k", "K1", "--subject-key", "K2"],
        ["-s", "S1", "--subject-key", "K1", "--subject", "K2"],
        ["-s", "S1", "-k", "K1", "--subject", "K2"],
    ],
)
def test_subject_data_duplicate_flags_conflicting_subject_key(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    args: list[str],
) -> None:
    """Test duplicate subject-key flags with conflicting values fail with code 2."""
    result = runner.invoke(cli.app, ["subject-data", *args])
    assert result.exit_code == 2
    assert "subject_key" in result.stderr
    assert "conflicting" in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


@pytest.mark.parametrize(
    ("args", "expected_study", "expected_subj"),
    [
        (["S1", "K1", "--study-key", "S1"], "S1", "K1"),
        (["S1", "K1", "-s", "S1"], "S1", "K1"),
        (["--study-key", "S1", "S1", "K1"], "S1", "K1"),
        (["S1", "K1", "--subject-key", "K1"], "S1", "K1"),
        (["S1", "K1", "-k", "K1"], "S1", "K1"),
        (["S1", "K1", "--subject", "K1"], "S1", "K1"),
        (["--subject-key", "K1", "S1", "K1"], "S1", "K1"),
    ],
)
def test_subject_data_positional_and_flag_non_conflicting(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    args: list[str],
    expected_study: str,
    expected_subj: str,
) -> None:
    """Test two positionals and a matching flag succeed."""
    result = runner.invoke(cli.app, ["subject-data", *args])
    assert result.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_once_with(expected_study, expected_subj)


@pytest.mark.parametrize(
    ("args", "conflicting_param"),
    [
        (["S1", "K1", "--study-key", "S2"], "study_key"),
        (["S1", "K1", "-s", "S2"], "study_key"),
        (["--study-key", "S2", "S1", "K1"], "study_key"),
        (["S1", "K1", "--subject-key", "K2"], "subject_key"),
        (["S1", "K1", "-k", "K2"], "subject_key"),
        (["S1", "K1", "--subject", "K2"], "subject_key"),
        (["--subject-key", "K2", "S1", "K1"], "subject_key"),
    ],
)
def test_subject_data_positional_and_flag_conflicting(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    args: list[str],
    conflicting_param: str,
) -> None:
    """Test two positionals and a conflicting flag fail with exit code 2."""
    result = runner.invoke(cli.app, ["subject-data", *args])
    assert result.exit_code == 2
    assert conflicting_param in result.stderr
    assert "conflicting" in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


@pytest.mark.parametrize(
    ("args", "missing_param"),
    [
        (["--study-key", "", "--subject-key", "K1"], "study_key"),
        (["--study-key", "   ", "--subject-key", "K1"], "study_key"),
        (["--study-key", "S1", "--subject-key", ""], "subject_key"),
        (["--study-key", "S1", "--subject-key", "   "], "subject_key"),
        (["--study-key", "", "--subject-key", ""], "study_key"),
        (["-s", "S1", "--subject", ""], "subject_key"),
        (["", ""], "study_key"),
        (["   ", "   "], "study_key"),
        (["S1", ""], "subject_key"),
        (["", "K1"], "study_key"),
        (["S1", "   "], "subject_key"),
        (["   ", "K1"], "study_key"),
        (["--study-key", "S1", ""], "subject_key"),
        (["--study-key", "S1", "   "], "subject_key"),
        (["", "--study-key", "S1"], "subject_key"),
        (["   ", "--study-key", "S1"], "subject_key"),
        (["--subject-key", "K1", ""], "study_key"),
        (["--subject-key", "K1", "   "], "study_key"),
        (["", "--subject-key", "K1"], "study_key"),
        (["   ", "--subject-key", "K1"], "study_key"),
        (["-s", "S1", ""], "subject_key"),
        (["--subject", "K1", ""], "study_key"),
    ],
)
def test_subject_data_empty_values_error(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    args: list[str],
    missing_param: str,
) -> None:
    """Test that empty or whitespace-only values in flags, positionals, or hybrids fail."""
    result = runner.invoke(cli.app, ["subject-data", *args])
    assert result.exit_code == 2
    assert missing_param in result.stderr
    mock_workflow.get_all_subject_data.assert_not_called()


def test_subject_data_help_text(runner: CliRunner) -> None:
    """Test that subject-data help includes flags, positionals, and hybrid documentation."""
    result = runner.invoke(cli.app, ["subject-data", "--help"])
    assert result.exit_code == 0
    assert "--study-key" in result.stdout
    assert "--subject-key" in result.stdout
    assert "--format" in result.stdout
    assert "--interactive" in result.stdout
    assert "hybrid" in result.stdout.lower()


def _make_sample_subject_data() -> Any:
    """Create a realistic SubjectComprehensiveData payload for CLI output tests."""
    from imednet.models import Query, Record, Subject, Visit
    from imednet.models.subjects import SubjectKeyword
    from imednet_workflows.subject_data import SubjectComprehensiveData

    return SubjectComprehensiveData(
        subject_details=Subject(
            subjectKey="SUBJ-001",
            subjectStatus="Enrolled",
            siteName="General Hospital",
            enrollmentStartDate="2026-01-15",
            keywords=[SubjectKeyword(keywordName="Cohort-A", keywordKey="CA")],
        ),
        visits=[
            Visit(
                visitId=10,
                intervalName="Screening",
                subjectKey="SUBJ-001",
                startDate="2026-01-15",
            )
        ],
        records=[
            Record(
                recordId=101,
                subjectKey="SUBJ-001",
                formKey="DEMO",
                recordStatus="Record Complete",
                dateCreated="2026-01-15T10:00:00Z",
                recordData={"AGE": 44, "SEX": "F"},
            )
        ],
        queries=[
            Query(
                annotationId=501,
                description="Verify birth year",
                annotationType="Monitor Query",
                subjectKey="SUBJ-001",
                variable="BRTHYR",
                dateCreated="2026-01-16T08:30:00Z",
            )
        ],
    )


def test_subject_data_json_envelope(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Verify subject-data --format json emits schemaVersion 1.0 JSON with zero repr text."""
    import json

    mock_workflow.get_all_subject_data.return_value = _make_sample_subject_data()
    result = runner.invoke(cli.app, ["subject-data", "STUDY-1", "SUBJ-001", "--format", "json"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "Record(" not in result.stdout

    payload = json.loads(result.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "subject_data"
    assert payload["status"] == "complete"
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["subject_details"]["subjectKey"] == "SUBJ-001"
    assert item["records"][0]["formKey"] == "DEMO"
    assert item["visits"][0]["intervalName"] == "Screening"
    assert item["queries"][0]["variable"] == "BRTHYR"


def test_subject_data_aligned_section_tables_no_repr(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify subject-data renders aligned section tables without raw Python repr text."""
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    mock_workflow.get_all_subject_data.return_value = _make_sample_subject_data()
    result = runner.invoke(cli.app, ["subject-data", "STUDY-1", "SUBJ-001"])

    assert result.exit_code == 0
    assert "Found 1 subject details:" in result.stdout
    assert "Found 1 visits:" in result.stdout
    assert "Found 1 records:" in result.stdout
    assert "Found 1 queries:" in result.stdout
    assert "General Hospital" in result.stdout
    assert "Cohort-A" in result.stdout
    assert "Verify birth year" in result.stdout
    assert "Record(" not in result.stdout
    assert "{'subject_details'" not in result.stdout


def test_subject_data_dict_payload_rendering(
    runner: CliRunner, sdk: MagicMock, mock_workflow: MagicMock
) -> None:
    """Verify subject-data also formats dictionary-shaped payloads cleanly."""
    mock_workflow.get_all_subject_data.return_value = {
        "subject_details": {
            "subjectKey": "SUBJ-DICT",
            "subjectStatus": "Screened",
            "siteName": "Site B",
            "enrollmentStartDate": "2026-02-01",
            "keywords": [{"keywordName": "K1"}],
        },
        "visits": [],
        "records": [
            {
                "recordId": 202,
                "subjectKey": "SUBJ-DICT",
                "formKey": "VS",
                "recordStatus": "Incomplete",
                "dateCreated": "2026-02-01T12:00:00Z",
            }
        ],
        "queries": [],
    }
    result = runner.invoke(cli.app, ["subject-data", "STUDY-1", "SUBJ-DICT"])
    assert result.exit_code == 0
    assert "SUBJ-DICT" in result.stdout
    assert "No visits found." in result.stdout
    assert "No queries found." in result.stdout


def test_subject_data_interactive_section_selection_and_grid(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify subject-data --interactive prompts for missing keys, selects section, and opens grid."""
    mock_workflow.get_all_subject_data.return_value = _make_sample_subject_data()
    grid_calls: list[dict[str, Any]] = []

    def fake_grid(**kwargs: Any) -> bool:
        grid_calls.append(kwargs)
        return True

    monkeypatch.setattr("imednet.cli.utils.grid.run_interactive_grid", fake_grid)

    # 1. Active TTY + missing keys + invalid section retry ("99") then "3" (records)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("STUDY-WIZ\nSUBJ-001\n99\n3\n"))
    res_tty = runner.invoke(cli.app, ["subject-data", "--interactive"])

    assert res_tty.exit_code == 0
    mock_workflow.get_all_subject_data.assert_called_with("STUDY-WIZ", "SUBJ-001")
    assert "Invalid section '99'" in res_tty.stderr
    assert len(grid_calls) == 1
    assert grid_calls[0]["label"] == "records"
    assert len(grid_calls[0]["details"]) == 1
    assert res_tty.stdout == ""

    # 2. Piped / non-interactive fallback + section "visits" ("2") -> prints only visits table
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))
    res_pipe = runner.invoke(cli.app, ["subject-data", "STUDY-WIZ", "SUBJ-001", "--interactive"])
    assert res_pipe.exit_code == 0
    assert "Found 1 visits:" in res_pipe.stdout
    assert "Found 1 records:" not in res_pipe.stdout

    # 3. EOF during subject key prompt exits with code 1
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    res_eof = runner.invoke(cli.app, ["subject-data", "STUDY-WIZ", "--interactive"])
    assert res_eof.exit_code == 1


def test_extract_records_json_and_interactive_grid(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workflows extract-records supports --format json, aligned tables, and TTY grid."""
    import json

    from imednet.models import Record

    sample_records = [
        Record(
            recordId=77,
            subjectKey="SUBJ-77",
            formKey="AE",
            recordStatus="Record Complete",
            dateCreated="2026-03-01T09:00:00Z",
            recordData={"AETERM": "Headache"},
        )
    ]
    mock_ext = MagicMock()
    mock_ext.extract_records_by_criteria.return_value = sample_records
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow", MagicMock(return_value=mock_ext)
    )

    # 1. --format json
    res_json = runner.invoke(
        cli.app, ["workflows", "extract-records", "STUDY-1", "--format", "json"]
    )
    assert res_json.exit_code == 0
    assert res_json.stderr == ""
    payload = json.loads(res_json.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "records"
    assert payload["count"] == 1
    assert payload["items"][0]["recordId"] == 77

    # 2. Default table output -> aligned columns, zero [Record(...)] repr
    res_table = runner.invoke(cli.app, ["workflows", "extract-records", "STUDY-1"])
    assert res_table.exit_code == 0
    assert "Found 1 matching records:" in res_table.stdout
    assert "SUBJ-77" in res_table.stdout
    assert "[Record(" not in res_table.stdout

    # 3. --interactive on active TTY -> passes details=records to run_interactive_grid
    grid_calls: list[dict[str, Any]] = []

    def fake_grid(**kwargs: Any) -> bool:
        grid_calls.append(kwargs)
        return True

    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)
    monkeypatch.setattr("imednet.cli.utils.grid.run_interactive_grid", fake_grid)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n\n\n"))
    res_grid = runner.invoke(cli.app, ["workflows", "extract-records", "STUDY-1", "--interactive"])
    assert res_grid.exit_code == 0
    assert len(grid_calls) == 1
    assert grid_calls[0]["details"] == sample_records
    assert res_grid.stdout == ""


def test_uat_run_updates_console_status_in_place(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workflows uat run updates console.status in place using format_job_progress."""
    from imednet.models.jobs import JobStatus
    from imednet_workflows.uat import UATWorkflowState

    status_updates: list[str] = []

    class DummyStatus:
        def __enter__(self) -> DummyStatus:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def update(self, message: str) -> None:
            status_updates.append(message)

    monkeypatch.setattr("imednet_workflows.cli.console.status", lambda msg: DummyStatus())

    class FakeUATWorkflow:
        def __init__(
            self,
            sdk_arg: Any,
            *,
            on_progress: Any = None,
            on_phase_change: Any = None,
        ) -> None:
            self._on_progress = on_progress
            self._on_phase_change = on_phase_change

        def run(self, **kwargs: Any) -> Any:
            if self._on_phase_change:
                self._on_phase_change(UATWorkflowState.SUBMITTING)
            if self._on_progress:
                self._on_progress(
                    "reg-batch-1",
                    JobStatus(batchId="reg-batch-1", state="PROCESSING", progress=50),
                    1.2,
                )
            if self._on_phase_change:
                self._on_phase_change(UATWorkflowState.MONITORING)
            if self._on_progress:
                self._on_progress(
                    "rec-batch-1",
                    JobStatus(batchId="rec-batch-1", state="COMPLETED", progress=100),
                    2.4,
                )
            res = MagicMock()
            res.summary.return_value = "UAT Summary OK"
            res.overall_success = True
            return res

    monkeypatch.setattr("imednet_workflows.uat.UATWorkflow", FakeUATWorkflow)
    result = runner.invoke(cli.app, ["workflows", "uat", "run", "STUDY-1"])

    assert result.exit_code == 0
    assert "UAT Summary OK" in result.stdout
    assert any("reg-batch-1" in msg and "PROCESSING" in msg for msg in status_updates)
    assert any("rec-batch-1" in msg and "COMPLETED" in msg for msg in status_updates)
