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
    assert "--no-interactive" in result.stdout
    assert "hybrid" in result.stdout.lower()


def _make_sample_subject_data() -> Any:
    """Build a populated SubjectComprehensiveData instance with real domain models."""
    from datetime import datetime, timezone

    from imednet.models.queries import Query, QueryComment, QueryStatus
    from imednet.models.records import Record
    from imednet.models.subjects import Subject, SubjectKeyword
    from imednet.models.visits import Visit
    from imednet_workflows.subject_data import SubjectComprehensiveData

    subject = Subject(
        study_key="STUDY-1",
        subject_key="SUBJ-1",
        subject_status="Screened",
        site_name="Site Alpha",
        enrollment_start_date="2026-03-01",
        date_created=datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc),
        keywords=[SubjectKeyword(keyword_name="Enrolled", keyword_key="ENR")],
    )
    visit = Visit(
        study_key="STUDY-1",
        visit_id=10,
        subject_key="SUBJ-1",
        interval_name="Screening",
    )
    record = Record(
        study_key="STUDY-1",
        record_id=101,
        subject_key="SUBJ-1",
        form_key="DEMO",
        record_status="Record Complete",
        record_data={"AGE": 44, "SEX": "F"},
        date_created=datetime(2026, 3, 2, 14, 15, tzinfo=timezone.utc),
    )
    query = Query(
        study_key="STUDY-1",
        subject_key="SUBJ-1",
        description="Confirm birth year",
        status=QueryStatus.OPEN,
        query_comments=[QueryComment(sequence=1, comment="Please verify DOB")],
    )
    return SubjectComprehensiveData(
        subject_details=subject,
        visits=[visit],
        records=[record],
        queries=[query],
    )


@pytest.mark.parametrize(
    ("cli_args", "use_env"),
    [
        (["subject-data", "STUDY-1", "SUBJ-1", "--format", "json"], False),
        (["subject-data", "-s", "STUDY-1", "-k", "SUBJ-1", "--format", "json"], False),
        (["subject-data", "STUDY-1", "-k", "SUBJ-1", "--format", "json"], False),
        (["subject-data", "STUDY-1", "SUBJ-1"], True),
        (["subject-data", "--study-key", "STUDY-1", "--subject", "SUBJ-1"], True),
    ],
)
def test_subject_data_json_format_flag_and_env(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    cli_args: list[str],
    use_env: bool,
) -> None:
    """Verify subject-data emits valid schemaVersion 1.0 JSON via --format json and env var."""
    import json

    sample = _make_sample_subject_data()
    mock_workflow.get_all_subject_data.return_value = sample
    if use_env:
        monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    else:
        monkeypatch.delenv("IMEDNET_CLI_OUTPUT_FORMAT", raising=False)

    result = runner.invoke(cli.app, cli_args)
    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "subject_data"
    assert payload["status"] == "complete"
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["subject_details"]["subjectKey"] == "SUBJ-1"
    assert item["subject_details"]["siteName"] == "Site Alpha"
    assert item["records"][0]["recordId"] == 101
    assert item["visits"][0]["intervalName"] == "Screening"
    assert item["queries"][0]["description"] == "Confirm birth year"
    assert item["queries"][0]["status"] == "OPEN"
    assert item["queries"][0]["queryComments"][0]["comment"] == "Please verify DOB"


@pytest.mark.parametrize(
    "extra_args",
    [
        [],
        ["--format", "json"],
    ],
)
def test_subject_data_missing_subject_exits_one_on_stderr(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    extra_args: list[str],
) -> None:
    """Verify unknown subject writes diagnostic to stderr, leaves stdout empty, and exits 1."""
    from imednet_workflows.subject_data import SubjectComprehensiveData

    mock_workflow.get_all_subject_data.return_value = SubjectComprehensiveData(subject_details=None)
    result = runner.invoke(cli.app, ["subject-data", "STUDY", "UNKNOWN", *extra_args])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "UNKNOWN" in result.stderr
    assert "STUDY" in result.stderr


def test_subject_data_interactive_grid_and_piped_table_views(
    runner: CliRunner,
    sdk: MagicMock,
    mock_workflow: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify subject-data launches InteractiveDataGrid on TTY and ASCII tables on pipes."""
    import imednet.cli.utils.grid as grid_mod

    sample = _make_sample_subject_data()
    mock_workflow.get_all_subject_data.return_value = sample

    # Piped output renders aligned ASCII table without Python repr text
    piped = runner.invoke(cli.app, ["subject-data", "STUDY-1", "SUBJ-1"])
    assert piped.exit_code == 0
    assert "Found 1 subject data:" in piped.stdout
    assert "SUBJ-1" in piped.stdout
    assert "Site Alpha" in piped.stdout
    assert "Enrolled" in piped.stdout
    assert "SubjectComprehensiveData(" not in piped.stdout

    # Explicit --format table overrides IMEDNET_CLI_OUTPUT_FORMAT=json
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    override = runner.invoke(cli.app, ["subject-data", "STUDY-1", "SUBJ-1", "--format", "table"])
    assert override.exit_code == 0
    assert "Found 1 subject data:" in override.stdout
    monkeypatch.delenv("IMEDNET_CLI_OUTPUT_FORMAT", raising=False)

    # Interactive TTY launches InteractiveDataGrid unless --no-interactive is passed
    grid_spy = MagicMock(return_value=True)
    monkeypatch.setattr(grid_mod, "is_interactive_terminal", lambda: True)
    monkeypatch.setattr(grid_mod, "run_interactive_grid", grid_spy)

    tty_res = runner.invoke(cli.app, ["subject-data", "STUDY-1", "SUBJ-1"])
    assert tty_res.exit_code == 0
    grid_spy.assert_called_once()
    assert grid_spy.call_args.kwargs["label"] == "subject data"
    assert grid_spy.call_args.kwargs["details"] == [sample]

    grid_spy.reset_mock()
    no_int_res = runner.invoke(cli.app, ["subject-data", "STUDY-1", "SUBJ-1", "--no-interactive"])
    assert no_int_res.exit_code == 0
    grid_spy.assert_not_called()
    assert "Found 1 subject data:" in no_int_res.stdout


def test_extract_records_json_format_and_env_suppress_progress(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify extract-records emits schemaVersion 1.0 JSON and suppresses stderr print_progress."""
    import json
    from datetime import datetime, timezone

    from imednet.models.records import Record

    rec1 = Record(
        study_key="STUDY-1",
        record_id=201,
        subject_key="SUBJ-01",
        form_key="DEMO",
        record_status="Record Complete",
        record_data={"AGE": 31},
        date_created=datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc),
    )
    rec2 = Record(
        study_key="STUDY-1",
        record_id=202,
        subject_key="SUBJ-02",
        form_key="AE",
        record_status="Data Entry Complete",
        record_data={"AETERM": "Headache"},
        date_created=datetime(2026, 4, 11, 9, 0, tzinfo=timezone.utc),
    )
    extraction_wf = MagicMock()
    extraction_wf.extract_records_by_criteria.return_value = [rec1, rec2]
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow",
        MagicMock(return_value=extraction_wf),
    )

    # 1. Explicit --format json
    res_flag = runner.invoke(
        cli.app,
        [
            "workflows",
            "extract-records",
            "STUDY-1",
            "--record-filter",
            "form_key=DEMO",
            "--format",
            "json",
        ],
    )
    assert res_flag.exit_code == 0
    assert res_flag.stderr == ""
    payload_flag = json.loads(res_flag.stdout)
    assert payload_flag["schemaVersion"] == "1.0"
    assert payload_flag["resource"] == "records"
    assert payload_flag["status"] == "complete"
    assert payload_flag["count"] == 2
    assert payload_flag["items"] == [
        rec1.model_dump(mode="json", by_alias=True),
        rec2.model_dump(mode="json", by_alias=True),
    ]

    # 2. IMEDNET_CLI_OUTPUT_FORMAT=json without --format flag
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    res_env = runner.invoke(cli.app, ["workflows", "extract-records", "STUDY-1"])
    assert res_env.exit_code == 0
    assert res_env.stderr == ""
    payload_env = json.loads(res_env.stdout)
    assert payload_env["schemaVersion"] == "1.0"
    assert payload_env["resource"] == "records"
    assert payload_env["count"] == 2


def test_extract_records_interactive_grid_and_piped_table_and_env_override(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify extract-records opens InteractiveDataGrid on TTY and prints ASCII tables on pipes."""
    from datetime import datetime, timezone

    import imednet.cli.utils.grid as grid_mod
    from imednet.models.records import Record

    rec = Record(
        study_key="STUDY-1",
        record_id=301,
        subject_key="SUBJ-09",
        form_key="VS",
        record_status="Record Complete",
        date_created=datetime(2026, 4, 12, 10, 30, tzinfo=timezone.utc),
    )
    extraction_wf = MagicMock()
    extraction_wf.extract_records_by_criteria.return_value = [rec]
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow",
        MagicMock(return_value=extraction_wf),
    )

    # Piped run prints ASCII table (no raw Record(...) repr)
    piped = runner.invoke(cli.app, ["workflows", "extract-records", "STUDY-1"])
    assert piped.exit_code == 0
    assert "Found 1 matching records:" in piped.stdout
    assert "SUBJ-09" in piped.stdout
    assert "Record(" not in piped.stdout
    assert "Extracting records for study 'STUDY-1'..." in piped.stderr

    # Explicit --format table overrides IMEDNET_CLI_OUTPUT_FORMAT=json
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    override = runner.invoke(
        cli.app, ["workflows", "extract-records", "STUDY-1", "--format", "table"]
    )
    assert override.exit_code == 0
    assert "Found 1 matching records:" in override.stdout
    monkeypatch.delenv("IMEDNET_CLI_OUTPUT_FORMAT", raising=False)

    # Interactive TTY launches InteractiveDataGrid
    grid_spy = MagicMock(return_value=True)
    monkeypatch.setattr(grid_mod, "is_interactive_terminal", lambda: True)
    monkeypatch.setattr(grid_mod, "run_interactive_grid", grid_spy)

    tty_res = runner.invoke(cli.app, ["workflows", "extract-records", "STUDY-1"])
    assert tty_res.exit_code == 0
    grid_spy.assert_called_once()
    assert grid_spy.call_args.kwargs["details"] == [rec]


def test_workflow_cli_masks_phi_in_subject_data_and_extract_records(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subject-data and workflows extract-records mask PHI by default across JSON envelopes and tables."""
    import json

    from imednet.models.records import Record
    from imednet.models.subjects import Subject
    from imednet_workflows.subject_data import SubjectComprehensiveData

    subj = Subject(
        study_key="STUDY1",
        subject_key="SUBJ-001",
        subject_status="Screened",
    )
    rec_subj = Record(
        record_id=11,
        study_key="STUDY1",
        form_key="AE",
        subject_key="SUBJ-001",
        record_data={
            "birthDate": "1980-05-12",
            "patientName": "Alice Example",
            "SSN": "999-00-1111",
            "aeTerm": "Mild rash",
            "email": "alice@example.com",
        },
    )
    workflow_subj = MagicMock()
    workflow_subj.get_all_subject_data.return_value = SubjectComprehensiveData(
        subject_details=subj,
        visits=[],
        records=[rec_subj],
        queries=[],
    )
    monkeypatch.setattr(
        "imednet_workflows.cli.SubjectDataWorkflow", MagicMock(return_value=workflow_subj)
    )

    res_subj = runner.invoke(cli.app, ["subject-data", "STUDY1", "SUBJ-001", "--format", "json"])
    assert res_subj.exit_code == 0
    parsed_subj = json.loads(res_subj.stdout)
    assert parsed_subj["schemaVersion"] == "1.0"
    assert parsed_subj["resource"] == "subject_data"
    subj_rec_data = parsed_subj["items"][0]["records"][0]["recordData"]
    assert subj_rec_data["birthDate"] == "***MASKED***"
    assert subj_rec_data["patientName"] == "***MASKED***"
    assert subj_rec_data["SSN"] == "***MASKED***"
    assert subj_rec_data["email"] == "***MASKED***"
    assert subj_rec_data["aeTerm"] == "Mild rash"
    assert "1980-05-12" not in res_subj.stdout
    assert "Alice Example" not in res_subj.stdout

    # Opt-out --no-mask-phi preserves enclave values
    res_unmasked = runner.invoke(
        cli.app, ["subject-data", "STUDY1", "SUBJ-001", "--format", "json", "--no-mask-phi"]
    )
    assert res_unmasked.exit_code == 0
    unmasked_rec_data = json.loads(res_unmasked.stdout)["items"][0]["records"][0]["recordData"]
    assert unmasked_rec_data["birthDate"] == "1980-05-12"
    assert unmasked_rec_data["email"] == "alice@example.com"

    workflow_ext = MagicMock()
    workflow_ext.extract_records_by_criteria.return_value = [
        Record(
            record_id=10,
            study_key="STUDY1",
            form_key="DM",
            subject_key="SUBJ-001",
            record_status="Complete",
            record_data={"DOB": "1980-05-12", "patientName": "Alice Example", "age": 45},
        )
    ]
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow", MagicMock(return_value=workflow_ext)
    )

    res_ext = runner.invoke(cli.app, ["workflows", "extract-records", "STUDY1", "--format", "json"])
    assert res_ext.exit_code == 0
    parsed_ext = json.loads(res_ext.stdout)
    assert parsed_ext["schemaVersion"] == "1.0"
    assert parsed_ext["resource"] == "records"
    ext_item = parsed_ext["items"][0]
    assert ext_item["recordData"]["DOB"] == "***MASKED***"
    assert ext_item["recordData"]["patientName"] == "***MASKED***"
    assert ext_item["recordData"]["age"] == 45
    assert "1980-05-12" not in res_ext.stdout


def test_workflows_validate_standards_reports_scores_and_enforces_min_score(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """imednet workflows validate-standards reports domain readiness scores and exits 1 below --min-score."""
    import json

    from imednet.models.forms import Form
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    sdk.forms.list.return_value = [Form(form_id=1, form_key="AE", form_name="Adverse Events")]
    sdk.variables.list.return_value = [
        Variable(variable_id=1, form_id=1, form_key="AE", variable_name="aeTerm"),
        Variable(variable_id=2, form_id=1, form_key="AE", variable_name="unprefixedVar"),
    ]
    sdk.records.list.return_value = [
        Record(
            record_id=1,
            study_key="STUDY_STD",
            form_id=1,
            form_key="AE",
            subject_key="S01",
            record_data={
                "aeTerm": "Nausea",
                "aeSeverity": "SEVERE",
                "patientName": "Secret Patient",
            },
        )
    ]

    res_json = runner.invoke(
        cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_STD",
            "--profile",
            "drug",
            "--domain",
            "AE",
            "--format",
            "json",
        ],
    )
    assert res_json.exit_code == 1
    envelope = json.loads(res_json.stdout)
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["resource"] == "standards_readiness"
    assert envelope["status"] == "complete"
    assert envelope["count"] == 1
    report_payload = envelope["items"][0]
    assert report_payload["study_key"] == "STUDY_STD"
    assert report_payload["profile"] == "drug"
    assert report_payload["domain"] == "AE"
    assert report_payload["score"] < 100.0
    assert report_payload["is_ready"] is False
    assert report_payload["passed"] is False
    assert any("unprefixedVar" in w for w in report_payload["warnings"])
    assert "Secret Patient" not in res_json.stdout

    # Passing explicit permissive --min-score exits 0 when score >= min_score
    res_pass_threshold = runner.invoke(
        cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_STD",
            "--profile",
            "drug",
            "--domain",
            "AE",
            "--min-score",
            "0",
            "--format",
            "json",
        ],
    )
    assert res_pass_threshold.exit_code == 0
    assert json.loads(res_pass_threshold.stdout)["items"][0]["passed"] is True

    # Also honors IMEDNET_CLI_OUTPUT_FORMAT=json
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    res_env_json = runner.invoke(
        cli.app,
        ["workflows", "validate-standards", "STUDY_STD", "--profile", "drug", "--domain", "AE"],
    )
    assert res_env_json.exit_code == 1
    env_envelope = json.loads(res_env_json.stdout)
    assert env_envelope["schemaVersion"] == "1.0"
    assert env_envelope["items"][0]["study_key"] == "STUDY_STD"
    monkeypatch.delenv("IMEDNET_CLI_OUTPUT_FORMAT", raising=False)

    res_gated = runner.invoke(
        cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_STD",
            "--profile",
            "drug",
            "--domain",
            "AE",
            "--min-score",
            "95",
        ],
    )
    assert res_gated.exit_code == 1
    assert "Standards Readiness Report" in res_gated.stdout
    assert "Violations" in res_gated.stdout


def test_validate_standards_filters_multi_domain_forms_and_fails_unknown_or_empty(
    runner: CliRunner,
    sdk: MagicMock,
) -> None:
    """validate-standards resolves Record/Variable domains from Forms without cross-domain relabeling and exits 1 on unknown/empty domains."""
    import json

    from imednet.models.forms import Form
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    sdk.forms.list.return_value = [
        Form(form_id=1, form_key="F_AE", form_name="Adverse Events"),
        Form(form_id=2, form_key="F_LB", form_name="Laboratory"),
    ]
    sdk.variables.list.return_value = [
        Variable(variable_id=1, form_id=1, form_key="F_AE", variable_name="aeTerm"),
        Variable(variable_id=2, form_id=2, form_key="F_LB", variable_name="lbTest"),
        Variable(variable_id=3, form_id=2, form_key="F_LB", variable_name="lbResult"),
    ]
    sdk.records.list.return_value = [
        Record(
            record_id=1,
            study_key="STUDY_MULTI",
            form_id=1,
            form_key="F_AE",
            subject_key="S01",
            record_data={"aeTerm": "Headache"},
        ),
        Record(
            record_id=2,
            study_key="STUDY_MULTI",
            form_id=2,
            form_key="F_LB",
            subject_key="S01",
            record_data={
                "lbTest": "ALT",
                "lbTestCd": "ALT",
                "lbResult": "24",
                "lbUnit": "U/L",
                "lbDate": "2026-04-01",
            },
        ),
    ]

    # Filtering to --domain LB with biologic profile scores ONLY F_LB (100%, Ready: True, exit 0)
    # and does NOT relabel F_AE records or variables as LB.
    res_lb = runner.invoke(
        cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_MULTI",
            "--profile",
            "biologic",
            "--domain",
            "LB",
            "--format",
            "json",
        ],
    )
    assert res_lb.exit_code == 0, res_lb.stdout
    lb_item = json.loads(res_lb.stdout)["items"][0]
    assert lb_item["score"] == 100.0
    assert lb_item["is_ready"] is True
    assert lb_item["checked_records"] == 1
    assert not any("aeTerm" in w for w in lb_item["warnings"])

    # Also works when sdk.forms.list is empty by resolving Record.form_key / Variable.form_key directly
    sdk.forms.list.return_value = []
    sdk.variables.list.return_value = [
        Variable(variable_id=1, form_id=1, form_key="AE", variable_name="aeTerm"),
        Variable(variable_id=2, form_id=2, form_key="MH", variable_name="mhTerm"),
    ]
    sdk.records.list.return_value = [
        Record(
            record_id=1,
            study_key="STUDY_MULTI",
            form_id=1,
            form_key="AE",
            subject_key="S01",
            record_data={"aeTerm": "Headache"},
        ),
        Record(
            record_id=2,
            study_key="STUDY_MULTI",
            form_id=2,
            form_key="MH",
            subject_key="S01",
            record_data={
                "mhTerm": "Asthma",
                "mhDecod": "ASTHMA",
                "mhCategory": "RESPIRATORY",
                "mhStartDate": "2020-01-01",
                "mhOngoing": "YES",
            },
        ),
    ]
    res_mh = runner.invoke(
        cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_MULTI",
            "--profile",
            "vaccine",
            "--domain",
            "MH",
            "--format",
            "json",
        ],
    )
    assert res_mh.exit_code == 0, res_mh.stdout
    mh_item = json.loads(res_mh.stdout)["items"][0]
    assert mh_item["score"] == 100.0
    assert mh_item["is_ready"] is True
    assert mh_item["checked_records"] == 1
    assert not any("aeTerm" in w for w in mh_item["warnings"])

    # Unknown domain -> score 0.0, Ready: False, ERROR violation, exit 1 (even with --min-score 0)
    res_unknown = runner.invoke(
        cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_MULTI",
            "--profile",
            "drug",
            "--domain",
            "UNKNOWN_XYZ",
            "--min-score",
            "0",
            "--format",
            "json",
        ],
    )
    assert res_unknown.exit_code == 1
    unk_item = json.loads(res_unknown.stdout)["items"][0]
    assert unk_item["score"] == 0.0
    assert unk_item["is_ready"] is False
    assert any(v["severity"] == "ERROR" for v in unk_item["violations"])

    # Empty study -> score 0.0, Ready: False, ERROR violation, exit 1
    sdk.records.list.return_value = []
    sdk.variables.list.return_value = []
    res_empty = runner.invoke(
        cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_EMPTY",
            "--profile",
            "drug",
            "--format",
            "json",
        ],
    )
    assert res_empty.exit_code == 1
    empty_item = json.loads(res_empty.stdout)["items"][0]
    assert empty_item["score"] == 0.0
    assert empty_item["is_ready"] is False
    assert any(v["severity"] == "ERROR" for v in empty_item["violations"])
