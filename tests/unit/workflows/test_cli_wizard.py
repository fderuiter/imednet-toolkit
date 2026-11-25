"""Tests for interactive CLI wizard and parameter validation."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from imednet.cli import app as cli_app
from imednet.spi.models import Record, Visit
from imednet_workflows.cli import state_app
from imednet_workflows.state_ledger import LedgerState, StreamState, StudyState
from imednet_workflows.uat.models import UATWorkflowState
from imednet_workflows.uat.orchestrator import UATRunResult
from imednet_workflows.wizard import (
    display_ledger_streams,
    print_progress,
    prompt_confirm,
    prompt_filters,
    prompt_input,
    prompt_iso8601_timestamp,
    prompt_non_negative_int,
    prompt_positive_int,
    prompt_stream_name,
    prompt_study_key,
    validate_filter_syntax,
    validate_iso8601_timestamp,
)


class _TTYStringIO(io.StringIO):
    """In-memory StringIO stream that reports isatty() == True."""

    def isatty(self) -> bool:
        return True


class CliResult:
    """Captured result of CLI invocation."""

    def __init__(self, exit_code: int, stdout: str, stderr: str):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.output = stdout + stderr


def run_cli_command(
    app_fn,
    args: list[str],
    stdin_input: str = "",
    *,
    tty_stdin: bool = True,
    capsys: pytest.CaptureFixture[str] | None = None,
) -> CliResult:
    """Invoke CLI entry point with redirected stdin, stdout, and stderr."""
    from contextlib import redirect_stderr, redirect_stdout

    if capsys is not None:
        capsys.readouterr()
    out = io.StringIO()
    err = io.StringIO()
    stdin_stream = _TTYStringIO(stdin_input) if tty_stdin else io.StringIO(stdin_input)
    exit_code = 0
    with redirect_stdout(out), redirect_stderr(err), patch("sys.stdin", stdin_stream):
        try:
            app_fn(args)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)

    extra_out = ""
    extra_err = ""
    if capsys is not None:
        captured = capsys.readouterr()
        extra_out = captured.out
        extra_err = captured.err
    return CliResult(exit_code, out.getvalue() + extra_out, err.getvalue() + extra_err)


# ---------------------------------------------------------------------------
# Unit tests for wizard utility functions
# ---------------------------------------------------------------------------


def test_print_progress() -> None:
    """Test print_progress writes to stderr and flushes."""
    err = io.StringIO()
    with patch("sys.stderr", err):
        print_progress("hello progress")
    assert "hello progress\n" in err.getvalue()


def test_validate_iso8601_timestamp_valid() -> None:
    """Test valid ISO-8601 UTC timestamps."""
    valid_ts = "2026-05-22T12:00:00Z"
    is_valid, dt, err = validate_iso8601_timestamp(valid_ts)
    assert is_valid is True
    assert err is None
    assert dt == datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)

    offset_ts = "2026-05-22T12:00:00+00:00"
    is_valid, dt, err = validate_iso8601_timestamp(offset_ts)
    assert is_valid is True
    assert dt == datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_validate_iso8601_timestamp_invalid() -> None:
    """Test invalid ISO-8601 timestamps return descriptive errors."""
    for invalid in ["", "   ", "not-a-date", "2026-99-99", "tomorrow"]:
        is_valid, dt, err = validate_iso8601_timestamp(invalid)
        assert is_valid is False
        assert dt is None
        assert err is not None
        assert "Invalid" in err or "empty" in err


def test_validate_filter_syntax_valid() -> None:
    """Test valid key=value filter strings."""
    is_valid, key, val, err = validate_filter_syntax("form_key=DEMO")
    assert is_valid is True
    assert key == "form_key"
    assert val == "DEMO"
    assert err is None

    # Boolean & int parsing
    _, k, v, _ = validate_filter_syntax("is_active=true")
    assert k == "is_active" and v is True

    _, k, v, _ = validate_filter_syntax("is_active=false")
    assert k == "is_active" and v is False

    _, k, v, _ = validate_filter_syntax("count=42")
    assert k == "count" and v == 42

    for expr, expected_key, expected_tuple in [
        ("dictionaryName==MedDRA", "dictionaryName", ("==", "MedDRA")),
        ("revision>=2", "revision", (">=", 2)),
        ("subjectStatus!=Enrolled", "subjectStatus", ("!=", "Enrolled")),
        ("revision>1", "revision", (">", 1)),
    ]:
        ok, parsed_key, parsed_val, parse_err = validate_filter_syntax(expr)
        assert ok is True
        assert parsed_key == expected_key
        assert parsed_val == expected_tuple
        assert parse_err is None

    ok_visit, visit_k, visit_v, visit_err = validate_filter_syntax(
        "interval_name=Screening", model=Visit
    )
    assert ok_visit is True
    assert visit_k == "interval_name"
    assert visit_v == "Screening"
    assert visit_err is None

    bad_visit, bad_k, bad_v, bad_err = validate_filter_syntax("visit_key=SCREENING", model=Visit)
    assert bad_visit is False
    assert bad_k is None
    assert bad_v is None
    assert bad_err is not None
    assert "Unknown filter attribute 'visit_key' for Visit" in bad_err


def test_validate_filter_syntax_invalid() -> None:
    """Test malformed filter strings show example syntax."""
    for invalid in ["", "no_equals", "=missing_key", "missing_val=", "  =  "]:
        is_valid, key, val, err = validate_filter_syntax(invalid)
        assert is_valid is False
        assert key is None
        assert val is None
        assert err is not None
        assert "key=value" in err


def test_prompt_input_and_confirm() -> None:
    """Test prompt_input and prompt_confirm reading from stdin."""
    with patch("sys.stdin", _TTYStringIO("custom_val\n")):
        val = prompt_input("Enter value", default="def")
        assert val == "custom_val"

    with patch("sys.stdin", _TTYStringIO("\n")):
        val = prompt_input("Enter value", default="def")
        assert val == "def"

    with patch("sys.stdin", _TTYStringIO("y\n")):
        assert prompt_confirm("Proceed?") is True

    with patch("sys.stdin", _TTYStringIO("n\n")):
        assert prompt_confirm("Proceed?") is False

    with patch("sys.stdin", _TTYStringIO("\n")):
        assert prompt_confirm("Proceed?", default=True) is True
    with patch("sys.stdin", _TTYStringIO("\n")):
        assert prompt_confirm("Proceed?", default=False) is False


def test_prompt_eof_handling() -> None:
    """Test that all interactive prompt functions exit with code 1 upon encountering EOF."""
    # prompt_input
    with patch("sys.stdin", _TTYStringIO("")), pytest.raises(SystemExit) as exc:
        prompt_input("Test prompt")
    assert exc.value.code == 1

    # prompt_confirm
    with patch("sys.stdin", _TTYStringIO("")), pytest.raises(SystemExit) as exc:
        prompt_confirm("Confirm?")
    assert exc.value.code == 1

    # prompt_study_key
    with patch("sys.stdin", _TTYStringIO("")), pytest.raises(SystemExit) as exc:
        prompt_study_key(None)
    assert exc.value.code == 1

    # prompt_stream_name
    with patch("sys.stdin", _TTYStringIO("")), pytest.raises(SystemExit) as exc:
        prompt_stream_name(None)
    assert exc.value.code == 1

    # prompt_iso8601_timestamp
    with patch("sys.stdin", _TTYStringIO("")), pytest.raises(SystemExit) as exc:
        prompt_iso8601_timestamp()
    assert exc.value.code == 1

    # prompt_positive_int
    with patch("sys.stdin", _TTYStringIO("")), pytest.raises(SystemExit) as exc:
        prompt_positive_int("Enter count")
    assert exc.value.code == 1

    # prompt_non_negative_int
    with patch("sys.stdin", _TTYStringIO("")), pytest.raises(SystemExit) as exc:
        prompt_non_negative_int("Enter count")
    assert exc.value.code == 1

    # prompt_filters
    with patch("sys.stdin", _TTYStringIO("")), pytest.raises(SystemExit) as exc:
        prompt_filters("record filter", "form_key=DEMO")
    assert exc.value.code == 1


def test_prompt_stream_name() -> None:
    """Test prompt_stream_name accepts prefilled stream or prompts interactively."""
    assert prompt_stream_name("records") == "records"
    with patch("sys.stdin", _TTYStringIO("\n\nevents\n")):
        assert prompt_stream_name(None) == "events"


def test_prompt_filters_malformed_supplied_filter_rejected_on_enter() -> None:
    """Test that malformed supplied filters are not dropped on Enter and abort with exit code 1."""
    with patch("sys.stdin", _TTYStringIO("\n")), pytest.raises(SystemExit) as exc:
        prompt_filters("record filter", "form_key=DEMO", existing_filters=["bad_format"])
    assert exc.value.code == 1


def test_prompt_filters_malformed_supplied_filter_corrected() -> None:
    """Test that malformed supplied filters can be corrected interactively."""
    user_inputs = "form_key=DEMO\n\n"
    with patch("sys.stdin", _TTYStringIO(user_inputs)):
        filters = prompt_filters("record filter", "form_key=DEMO", existing_filters=["bad_format"])
        assert filters == ["form_key=DEMO"]


def test_prompt_filters_malformed_supplied_filter_retry_then_valid() -> None:
    """Test that correcting a malformed supplied filter handles multiple retries before valid."""
    user_inputs = "still_bad\nform_key=DEMO\n\n"
    with patch("sys.stdin", _TTYStringIO(user_inputs)):
        filters = prompt_filters("record filter", "form_key=DEMO", existing_filters=["bad_format"])
        assert filters == ["form_key=DEMO"]


def test_prompt_filters_malformed_supplied_filter_retry_then_abort() -> None:
    """Test that correcting a malformed supplied filter aborts if empty string given on retry."""
    user_inputs = "still_bad\n\n"
    with patch("sys.stdin", _TTYStringIO(user_inputs)), pytest.raises(SystemExit) as exc:
        prompt_filters("record filter", "form_key=DEMO", existing_filters=["bad_format"])
    assert exc.value.code == 1


def test_prompt_iso8601_timestamp_retry() -> None:
    """Test prompt_iso8601_timestamp retries on invalid input before succeeding."""
    user_inputs = "bad_date\n2026-05-22T12:00:00Z\n"
    with patch("sys.stdin", _TTYStringIO(user_inputs)):
        raw, dt = prompt_iso8601_timestamp()
        assert raw == "2026-05-22T12:00:00Z"
        assert dt == datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_prompt_filters_interactive() -> None:
    """Test prompt_filters handles retries on invalid format and collects multiple filters."""
    # 1st invalid filter: "bad_format"
    # 2nd valid filter: "form_key=DEMO"
    # 3rd valid filter: "subject_status=Screened"
    # 4th empty line to finish
    user_inputs = "bad_format\nform_key=DEMO\nsubject_status=Screened\n\n"
    with patch("sys.stdin", _TTYStringIO(user_inputs)):
        filters = prompt_filters("record filter", "form_key=DEMO")
        assert filters == ["form_key=DEMO", "subject_status=Screened"]

    # Test with existing valid filter
    with patch("sys.stdin", _TTYStringIO("extra=1\n\n")):
        filters = prompt_filters(
            "record filter", "form_key=DEMO", existing_filters=["form_key=DEMO"]
        )
        assert filters == ["form_key=DEMO", "extra=1"]


def test_prompt_study_key() -> None:
    """Test prompt_study_key prompts when None or empty."""
    assert prompt_study_key("STUDY-01") == "STUDY-01"

    with patch("sys.stdin", _TTYStringIO("\n\nPROMPT_STUDY\n")):
        assert prompt_study_key(None) == "PROMPT_STUDY"


def test_prompt_positive_int() -> None:
    """Test prompt_positive_int validates positive numbers."""
    with patch("sys.stdin", _TTYStringIO("not_a_num\n0\n-5\n4\n")):
        val = prompt_positive_int("Enter count", default=1)
        assert val == 4


def test_prompt_non_negative_int() -> None:
    """Test prompt_non_negative_int validates non-negative integers and handles invalid inputs."""
    with patch("sys.stdin", _TTYStringIO("not_a_num\n-1\n0\n")):
        val = prompt_non_negative_int("Enter count", default=10)
        assert val == 0


def test_display_ledger_streams(tmp_path) -> None:
    """Test display_ledger_streams outputs stream metadata to stderr and filters by study_key."""
    ledger_path = str(tmp_path / "ledger.json")
    state = LedgerState(
        studies={
            "STUDY_A": StudyState(
                streams={
                    "records": StreamState(
                        last_timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
                        records_processed=10,
                        last_run_status="success",
                    )
                }
            ),
            "STUDY_B": StudyState(
                streams={
                    "records": StreamState(
                        last_timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
                        records_processed=5,
                        last_run_status="success",
                    )
                }
            ),
        }
    )
    with patch("imednet_workflows.wizard.get_state_provider") as mock_provider:
        mock_provider.return_value.read_state.return_value = state
        err = io.StringIO()
        with patch("sys.stderr", err):
            display_ledger_streams(ledger_path, study_key="STUDY_A")
        output = err.getvalue()
        assert "STUDY_A" in output
        assert "STUDY_B" not in output

        # Test read error handling
        mock_provider.return_value.read_state.side_effect = RuntimeError("disk error")
        err2 = io.StringIO()
        with patch("sys.stderr", err2):
            display_ledger_streams(ledger_path)
        assert "Could not read existing ledger" in err2.getvalue()


# ---------------------------------------------------------------------------
# CLI wizard tests: extract-records --interactive
# ---------------------------------------------------------------------------


def test_extract_records_interactive_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test extract-records in interactive mode prompts and routes progress to stderr."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")

    mock_workflow = MagicMock()
    mock_workflow.extract_records_by_criteria.return_value = [
        MagicMock(spec=Record, record_id=101, subject_key="SUBJ-1", form_key="DEMO")
    ]
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow",
        MagicMock(return_value=mock_workflow),
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    # User inputs:
    # 1. study key: STUDY_INTERACTIVE
    # 2. record filter: form_key=DEMO
    # 3. empty line to finish record filters
    # 4. subject filter: subject_status=Screened
    # 5. empty line to finish subject filters
    # 6. visit filter: empty line to skip
    stdin_input = (
        "\n".join(
            [
                "STUDY_INTERACTIVE",
                "form_key=DEMO",
                "",
                "subject_status=Screened",
                "",
                "",
            ]
        )
        + "\n"
    )

    result = run_cli_command(
        cli_app,
        ["workflows", "extract-records", "--interactive"],
        stdin_input=stdin_input,
    )

    assert result.exit_code == 0
    # Data results should go to stdout
    assert "Found 1 matching records:" in result.stdout
    # Progress and prompts should go to stderr
    assert "Extracting records for study 'STUDY_INTERACTIVE'..." in result.stderr
    assert "Enter study key" in result.stderr

    # Verify workflow was called with parsed filters
    mock_workflow.extract_records_by_criteria.assert_called_once()
    call_kwargs = mock_workflow.extract_records_by_criteria.call_args.kwargs
    assert call_kwargs["study_key"] == "STUDY_INTERACTIVE"
    assert call_kwargs["record_filter"] == {"form_key": "DEMO"}
    assert call_kwargs["subject_filter"] == {"subject_status": "Screened"}
    assert call_kwargs["visit_filter"] is None
    assert call_kwargs["on_progress"] is not None


def test_extract_records_interactive_with_prefilled_study_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test extract-records with positional study_key and --interactive."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")

    mock_workflow = MagicMock()
    mock_workflow.extract_records_by_criteria.return_value = []
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow",
        MagicMock(return_value=mock_workflow),
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    # Study key is provided on CLI; filters are skipped
    stdin_input = "\n\n\n"

    result = run_cli_command(
        cli_app,
        ["workflows", "extract-records", "STUDY_PRESET", "--interactive"],
        stdin_input=stdin_input,
    )

    assert result.exit_code == 0
    assert "No records found" in result.stdout
    assert "Extracting records for study 'STUDY_PRESET'..." in result.stderr


def test_extract_records_headless_missing_study_key_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test extract-records in headless mode without study_key produces required argument error."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")

    result = run_cli_command(cli_app, ["workflows", "extract-records"])
    assert result.exit_code == 2
    assert "the following arguments are required: study_key" in result.stderr


# ---------------------------------------------------------------------------
# CLI wizard tests: state set --interactive
# ---------------------------------------------------------------------------


def test_state_set_interactive_confirm_writes_ledger(tmp_path) -> None:
    """Test state set --interactive prompts, validates timestamp, confirms transition and writes."""
    ledger_path = str(tmp_path / "ledger.json")

    # Stdin inputs:
    # 1. Study key: TEST_STUDY
    # 2. Stream name: records
    # 3. Timestamp: 2026-05-22T14:30:00Z
    # 4. Records processed: 25
    # 5. Confirm transition: y
    stdin_input = "TEST_STUDY\nrecords\n2026-05-22T14:30:00Z\n25\ny\n"

    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        result = run_cli_command(
            state_app,
            ["set", "--ledger-path", ledger_path, "--interactive"],
            stdin_input=stdin_input,
        )

        assert result.exit_code == 0
        assert "Successfully set high-water mark" in result.stdout
        # Prompts and state summary should be in stderr
        assert "Proposed State Transition:" in result.stderr
        assert "Confirm state transition" in result.stderr

        # Check ledger was called with force=False by default
        mock_ledger.return_value.set_last_timestamp.assert_called_once()
        call_kwargs = mock_ledger.return_value.set_last_timestamp.call_args.kwargs
        assert call_kwargs["study_key"] == "TEST_STUDY"
        assert call_kwargs["stream_name"] == "records"
        assert call_kwargs["records_processed"] == 25
        assert call_kwargs["force"] is False


def test_state_set_interactive_abort_on_rejection(tmp_path) -> None:
    """Test state set --interactive does not modify ledger when rejected."""
    ledger_path = str(tmp_path / "ledger.json")

    # User declines confirmation
    stdin_input = "TEST_STUDY\nrecords\n2026-05-22T14:30:00Z\n0\nn\n"

    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        result = run_cli_command(
            state_app,
            ["set", "--ledger-path", ledger_path, "--interactive"],
            stdin_input=stdin_input,
        )

        assert result.exit_code == 0
        assert "State update cancelled" in result.stderr
        mock_ledger.return_value.set_last_timestamp.assert_not_called()


def test_state_set_interactive_retry_invalid_timestamp(tmp_path) -> None:
    """Test state set --interactive prompts again on invalid timestamp syntax."""
    ledger_path = str(tmp_path / "ledger.json")

    # 1. Study key: TEST_STUDY
    # 2. Stream name: records
    # 3. Invalid timestamp: invalid_date
    # 4. Valid timestamp: 2026-05-22T12:00:00Z
    # 5. Records processed: 0
    # 6. Confirm transition: y
    stdin_input = "TEST_STUDY\nrecords\ninvalid_date\n2026-05-22T12:00:00Z\n0\ny\n"

    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        result = run_cli_command(
            state_app,
            ["set", "--ledger-path", ledger_path, "--interactive"],
            stdin_input=stdin_input,
        )

        assert result.exit_code == 0
        assert "Validation Error" in result.stderr
        assert "Successfully set high-water mark" in result.stdout
        mock_ledger.return_value.set_last_timestamp.assert_called_once()


# ---------------------------------------------------------------------------
# CLI wizard tests: workflows uat run
# ---------------------------------------------------------------------------


def test_workflows_uat_run_interactive_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test workflows uat run --interactive prompts and runs pipeline with live phase updates."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    mock_run_result = MagicMock(spec=UATRunResult)
    mock_run_result.overall_success = True
    mock_run_result.summary.return_value = (
        "=== UAT RUN SUMMARY ===\nStatus: PASS\nTotal Records: 10"
    )

    mock_uat_workflow = MagicMock()
    mock_uat_workflow.run.return_value = mock_run_result

    monkeypatch.setattr(
        "imednet_workflows.uat.UATWorkflow",
        MagicMock(return_value=mock_uat_workflow),
    )

    # Stdin inputs:
    # 1. Study key: UAT_STUDY
    # 2. Test subjects count: 5
    # 3. Site name: Site Alpha
    # 4. Target forms: DEMO, VISITS
    # 5. Email notify: (blank -> None)
    stdin_input = "UAT_STUDY\n5\nSite Alpha\nDEMO, VISITS\n\n"

    result = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "--interactive"],
        stdin_input=stdin_input,
    )

    assert result.exit_code == 0
    # Pure summary in stdout
    assert "=== UAT RUN SUMMARY ===" in result.stdout
    assert "Status: PASS" in result.stdout
    # Phase progress and prompts in stderr
    assert "Starting UAT pipeline for study 'UAT_STUDY'..." in result.stderr
    assert "Enter study key" in result.stderr

    # Verify parameters passed to workflow.run
    mock_uat_workflow.run.assert_called_once_with(
        study_key="UAT_STUDY",
        subject_count=5,
        site_name="Site Alpha",
        enabled_form_keys=["DEMO", "VISITS"],
        email_notify=None,
    )


def test_workflows_uat_run_headless_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test workflows uat run headlessly with CLI flags."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    mock_run_result = MagicMock(spec=UATRunResult)
    mock_run_result.overall_success = True
    mock_run_result.summary.return_value = "=== UAT RUN SUMMARY ===\nStatus: PASS"

    mock_uat_workflow = MagicMock()
    mock_uat_workflow.run.return_value = mock_run_result

    monkeypatch.setattr(
        "imednet_workflows.uat.UATWorkflow",
        MagicMock(return_value=mock_uat_workflow),
    )

    result = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "CLI_STUDY",
            "--subject-count",
            "3",
            "--site-name",
            "Site Beta",
            "--form-key",
            "F1",
            "--form-key",
            "F2",
        ],
    )

    assert result.exit_code == 0
    assert "=== UAT RUN SUMMARY ===" in result.stdout
    assert "Starting UAT pipeline for study 'CLI_STUDY'..." in result.stderr

    mock_uat_workflow.run.assert_called_once_with(
        study_key="CLI_STUDY",
        subject_count=3,
        site_name="Site Beta",
        enabled_form_keys=["F1", "F2"],
        email_notify=None,
    )


def test_workflows_uat_run_headless_failure_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test workflows uat run exits with 1 and prints [Failed] (never [Complete]) on failure."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    mock_run_result = MagicMock(spec=UATRunResult)
    mock_run_result.overall_success = False
    mock_run_result.summary.return_value = "=== UAT RUN SUMMARY ===\nOverall: FAIL"

    mock_uat_workflow = MagicMock()
    mock_uat_workflow.run.return_value = mock_run_result

    monkeypatch.setattr(
        "imednet_workflows.uat.UATWorkflow",
        MagicMock(return_value=mock_uat_workflow),
    )

    result = run_cli_command(cli_app, ["workflows", "uat", "run", "FAIL_STUDY"])

    assert result.exit_code == 1
    assert "Overall: FAIL" in result.stdout
    assert "[Failed] UAT pipeline execution encountered an error." in result.stderr
    assert "[Complete]" not in result.stderr


def test_workflows_uat_run_end_to_end_monitoring_failure_cli_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end uat_run with failed monitored job emits [Failed] before Overall: FAIL."""
    import builtins

    from imednet import ImednetSDK
    from imednet.spi.models import JobStatus
    from imednet.spi.utils import JobFailedError, JobPollSummary

    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    sim_sdk = ImednetSDK(simulation_mode=True)
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=sim_sdk))

    failed_summary = JobPollSummary(
        study_key="STUDY",
        results={},
        failures={
            "batch-mon-fail-99": JobFailedError(
                "batch-mon-fail-99", JobStatus(batch_id="batch-mon-fail-99", state="FAILED")
            )
        },
        elapsed_total=0.2,
    )
    monkeypatch.setattr(
        "imednet_workflows.uat.orchestrator.UATWorkflow.monitor",
        MagicMock(return_value=failed_summary),
    )

    events: list[str] = []
    orig_print = builtins.print

    def tracking_print(*args: object, **kwargs: object) -> None:
        events.append(" ".join(str(a) for a in args))
        orig_print(*args, **kwargs)

    def tracking_progress(message: str) -> None:
        events.append(message)
        print_progress(message)

    monkeypatch.setattr(builtins, "print", tracking_print)
    monkeypatch.setattr("imednet_workflows.wizard.print_progress", tracking_progress)

    with pytest.raises(SystemExit) as exc_info:
        cli_app(["workflows", "uat", "run", "STUDY", "--subject-count", "1"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "[Phase 1/5] Inspecting study schema and metadata..." in captured.err
    assert "[Phase 5/5] Monitoring asynchronous job completion..." in captured.err
    assert "[Failed] UAT pipeline execution encountered an error." in captured.err
    assert "[Complete]" not in captured.err
    assert "Overall: FAIL" in captured.out
    assert "Failed phase: MONITORING" in captured.out
    assert "batch-mon-fail-99" in captured.out
    combined_events = "\n".join(events)
    assert combined_events.index("[Failed]") < combined_events.index("Overall: FAIL")


def test_long_extraction_piped_stdout_cleanliness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that long multi-query extractions output progress to stderr and leave stdout clean for piping."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    records_data = [
        MagicMock(record_id=1, subject_key="S1", form_key="DEMO"),
        MagicMock(record_id=2, subject_key="S2", form_key="DEMO"),
    ]

    def fake_extract_records_by_criteria(*args, **kwargs):
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress("Querying subjects matching filter criteria...")
            on_progress("Retrieved 2 matching subjects.")
            on_progress("Querying records for study 'STUDY_LONG'...")
            on_progress("Filtering records by matched subject keys...")
            on_progress("Extraction complete: found 2 matching records.")
        return records_data

    mock_wf = MagicMock()
    mock_wf.extract_records_by_criteria.side_effect = fake_extract_records_by_criteria
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow", MagicMock(return_value=mock_wf)
    )

    # Run with empty filters to proceed
    stdin_input = "\n\n\n"
    result = run_cli_command(
        cli_app,
        ["workflows", "extract-records", "STUDY_LONG", "--interactive"],
        stdin_input=stdin_input,
    )

    assert result.exit_code == 0
    # Ensure stdout does NOT contain any progress statements
    assert "[Progress]" not in result.stdout
    assert "Extracting records for study" not in result.stdout
    assert "Found 2 matching records:" in result.stdout

    # Ensure stderr DOES contain progress indicators
    assert "[Progress] Querying subjects matching filter criteria..." in result.stderr
    assert "[Progress] Extraction complete: found 2 matching records." in result.stderr
    assert "Extracting records for study 'STUDY_LONG'..." in result.stderr


def test_uat_workflow_phase_lifecycle_callback() -> None:
    """Test UATWorkflow on_phase_change callback receives all 5 phases in lifecycle order."""
    from imednet_workflows.uat.generator import GeneratedRecordSet
    from imednet_workflows.uat.models import UATSpecification
    from imednet_workflows.uat.orchestrator import UATWorkflow
    from imednet_workflows.uat.submission import SubmissionResult

    observed_phases: list[UATWorkflowState] = []
    mock_sdk = MagicMock()
    mock_sdk.get_job.return_value = MagicMock(state="COMPLETED")

    workflow = UATWorkflow(mock_sdk, on_phase_change=observed_phases.append)

    workflow.inspect = MagicMock(return_value=MagicMock())
    mock_spec = MagicMock(spec=UATSpecification)
    workflow.build_spec = MagicMock(return_value=mock_spec)
    workflow.generate = MagicMock(return_value=[MagicMock(spec=GeneratedRecordSet)])
    mock_submission = MagicMock(spec=SubmissionResult)
    mock_submission.all_batch_ids = ["B1"]
    workflow.submit = MagicMock(return_value=mock_submission)
    mock_poll = MagicMock()
    mock_poll.all_successful = True
    workflow.monitor = MagicMock(return_value=mock_poll)

    result = workflow.run("PHASE_TEST_STUDY")

    assert result.overall_success is True
    assert observed_phases == [
        UATWorkflowState.INSPECTING,
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.GENERATING,
        UATWorkflowState.SUBMITTING,
        UATWorkflowState.MONITORING,
        UATWorkflowState.COMPLETED,
    ]


def test_extract_records_interactive_malformed_filter_aborts_without_broadening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test interactive extract-records aborts if supplied filter is malformed and not corrected."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    mock_workflow = MagicMock()
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow",
        MagicMock(return_value=mock_workflow),
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    # User enters study key, then presses Enter on malformed filter prompt
    stdin_input = "STUDY1\n\n"
    result = run_cli_command(
        cli_app,
        ["workflows", "extract-records", "--record-filter", "invalidfilter", "--interactive"],
        stdin_input=stdin_input,
    )

    assert result.exit_code == 1
    assert "Malformed filter 'invalidfilter' was not corrected" in result.stderr
    # Workflow must NOT be called with an unconstrained/broadened query
    mock_workflow.extract_records_by_criteria.assert_not_called()


def test_extract_records_interactive_eof_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test interactive extract-records aborts immediately on EOF."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    mock_workflow = MagicMock()
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow",
        MagicMock(return_value=mock_workflow),
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    result = run_cli_command(
        cli_app,
        ["workflows", "extract-records", "--interactive"],
        stdin_input="",
    )

    assert result.exit_code == 1
    assert "Input stream closed (EOF)" in result.stderr
    mock_workflow.extract_records_by_criteria.assert_not_called()


def test_state_set_interactive_eof_aborts(tmp_path) -> None:
    """Test state set --interactive aborts cleanly on EOF."""
    ledger_path = str(tmp_path / "ledger.json")
    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        result = run_cli_command(
            state_app,
            ["set", "--ledger-path", ledger_path, "--interactive"],
            stdin_input="",
        )
        assert result.exit_code == 1
        assert "Input stream closed (EOF)" in result.stderr
        mock_ledger.return_value.set_last_timestamp.assert_not_called()


def test_state_reset_interactive_eof_aborts(tmp_path) -> None:
    """Test state reset --interactive aborts cleanly on EOF."""
    ledger_path = str(tmp_path / "ledger.json")
    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        result = run_cli_command(
            state_app,
            ["reset", "--study-key", "STUDY1", "--ledger-path", ledger_path, "--interactive"],
            stdin_input="",
        )
        assert result.exit_code == 1
        assert "Input stream closed (EOF)" in result.stderr
        mock_ledger.return_value.delete_entry.assert_not_called()


def test_workflows_uat_run_interactive_eof_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test workflows uat run --interactive aborts cleanly on EOF."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))
    mock_uat_workflow = MagicMock()
    monkeypatch.setattr(
        "imednet_workflows.uat.UATWorkflow",
        MagicMock(return_value=mock_uat_workflow),
    )

    result = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "--interactive"],
        stdin_input="",
    )

    assert result.exit_code == 1
    assert "Input stream closed (EOF)" in result.stderr
    mock_uat_workflow.run.assert_not_called()


def test_prompts_reject_piped_stdin_and_non_interactive_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Test interactive prompts and wizards exit with code 1 on non-TTY stdin or --no-interactive."""
    err = io.StringIO()
    with patch("sys.stdin", io.StringIO("value\n")), patch("sys.stderr", err):
        with pytest.raises(SystemExit) as exc_in:
            prompt_input("Enter value")
        assert exc_in.value.code == 1
    assert "Interactive prompts require an interactive TTY on stdin" in err.getvalue()

    err_confirm = io.StringIO()
    with patch("sys.stdin", io.StringIO("y\n")), patch("sys.stderr", err_confirm):
        with pytest.raises(SystemExit) as exc_conf:
            prompt_confirm("Proceed?")
        assert exc_conf.value.code == 1
    assert "Interactive prompts require an interactive TTY on stdin" in err_confirm.getvalue()

    ledger_path = str(tmp_path / "ledger.json")
    with patch("imednet_workflows.cli.get_state_provider") as mock_ledger:
        res_piped = run_cli_command(
            state_app,
            ["set", "--ledger-path", ledger_path, "--interactive"],
            stdin_input="S1\nrecords\n2026-05-22T12:00:00Z\n0\ny\n",
            tty_stdin=False,
        )
        assert res_piped.exit_code == 1
        assert "Interactive prompts require an interactive TTY on stdin" in res_piped.stderr
        mock_ledger.return_value.set_last_timestamp.assert_not_called()

        res_flag = run_cli_command(
            cli_app,
            [
                "--no-interactive",
                "workflows",
                "state",
                "reset",
                "--study-key",
                "S1",
                "--ledger-path",
                ledger_path,
                "--interactive",
            ],
            stdin_input="y\n",
            tty_stdin=True,
        )
        assert res_flag.exit_code == 1
        assert "Interactive prompts require an interactive TTY on stdin" in res_flag.stderr
        mock_ledger.return_value.delete_entry.assert_not_called()

    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    mock_get_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", mock_get_sdk)

    res_extract = run_cli_command(
        cli_app,
        ["workflows", "extract-records", "--interactive"],
        stdin_input="STUDY1\n\n\n\n",
        tty_stdin=False,
    )
    assert res_extract.exit_code == 1
    assert "Interactive prompts require an interactive TTY on stdin" in res_extract.stderr
    mock_get_sdk.assert_not_called()

    res_uat = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "--interactive", "--no-interactive"],
        stdin_input="STUDY1\n2\n\n\n",
        tty_stdin=True,
    )
    assert res_uat.exit_code == 1
    assert "Interactive prompts require an interactive TTY on stdin" in res_uat.stderr
    mock_get_sdk.assert_not_called()


def test_wizard_filter_validation_preserves_zero_padded_and_superscripts_and_matches_cli() -> None:
    """Workflow wizard prompts and CLI parse_filter_args enforce identical validation and coercion."""
    from imednet.cli import parse_filter_args
    from imednet.models.codings import Coding
    from imednet.models.subjects import Subject

    # Unicode superscript digits succeed as text with or without a model
    ok_no_model, k1, v1, err1 = validate_filter_syntax("value=²")
    assert ok_no_model is True and k1 == "value" and v1 == "²" and err1 is None
    assert parse_filter_args(["value=²"]) == {"value": "²"}

    ok_coding, k2, v2, err2 = validate_filter_syntax("value=²", model=Coding)
    assert ok_coding is True and k2 == "value" and v2 == "²" and err2 is None
    assert parse_filter_args(["value=²"], model=Coding) == {"value": "²"}

    # Zero-padded identifiers and numeric text on string attributes stay str
    ok_subj, k3, v3, err3 = validate_filter_syntax("subject_key=001", model=Subject)
    assert ok_subj is True and k3 == "subject_key" and v3 == "001" and err3 is None
    assert parse_filter_args(["subject_key=001"], model=Subject) == {"subject_key": "001"}

    # Invalid integer or boolean values fail identically in wizard and CLI
    bad_int, _, _, int_err = validate_filter_syntax("record_id=abc", model=Record)
    assert bad_int is False and int_err is not None
    assert "Invalid integer value 'abc' for attribute 'record_id'" in int_err

    bad_bool, _, _, bool_err = validate_filter_syntax("deleted=maybe", model=Subject)
    assert bad_bool is False and bool_err is not None
    assert "Invalid boolean value 'maybe' for attribute 'deleted'" in bool_err

    # Interactive prompt_filters accepts superscripts and retries on invalid model integer
    user_inputs = "record_id=abc\nrecord_id=10\nsubject_key=001\n\n"
    with patch("sys.stdin", _TTYStringIO(user_inputs)):
        collected = prompt_filters("record filter", "form_key=DEMO", model=Record)
    assert collected == ["record_id=10", "subject_key=001"]

    with patch("sys.stdin", _TTYStringIO("value=²\n\n")):
        sup_collected = prompt_filters("coding filter", "code=0042", model=Coding)
    assert sup_collected == ["value=²"]


@pytest.mark.parametrize(
    ("extra_flags", "expected_err"),
    [
        (["--visit-filter", "visit_id=1"], "Valid attributes:"),
        (["--record-filter", "study_key=STUDY"], "'study_key' and 'studyKey' cannot be used"),
        (["--subject-filter", "keywords=vip"], "Valid attributes:"),
        (
            ["--record-filter", "form_key=~DEMO"],
            "Allowed operators: ['!=', '<', '<=', '==', '>', '>=']",
        ),
        (
            ["--record-filter", "record_id>=1", "--record-filter", "recordId<=10"],
            "Duplicate filter attribute 'recordId'",
        ),
    ],
)
def test_extract_records_headless_preflight_enforces_allowlists_before_sdk(
    extra_flags: list[str],
    expected_err: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 1 & 2: workflows extract-records validates endpoint allowlists before SDK instantiation."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    mock_get_sdk = MagicMock(side_effect=AssertionError("get_sdk must not be called in preflight"))
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", mock_get_sdk)

    result = run_cli_command(cli_app, ["workflows", "extract-records", "STUDY", *extra_flags])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert expected_err in result.stderr
    mock_get_sdk.assert_not_called()


def test_wizard_validate_filter_syntax_enforces_endpoint_allowlists() -> None:
    """Interactive wizard validate_filter_syntax rejects non-allowlisted fields and study_key."""
    ok_visit, _, _, visit_err = validate_filter_syntax("visit_id=1", model=Visit)
    assert ok_visit is False
    assert visit_err is not None
    assert "Valid attributes:" in visit_err

    ok_study, _, _, study_err = validate_filter_syntax("study_key=STUDY", model=Record)
    assert ok_study is False
    assert study_err is not None
    assert "'study_key' and 'studyKey' cannot be used in --filter" in study_err


def test_validate_filter_syntax_compound_and_or() -> None:
    """validate_filter_syntax supports compound ';' AND and ',' OR clauses with 4-tuple return."""
    from imednet.spi.models import Subject

    ok_and, key_and, val_and, err_and = validate_filter_syntax(
        "form_key=DEMO;record_id>=2", model=Record
    )
    assert (ok_and, key_and, err_and) == (True, "filter", None)
    assert val_and == {"form_key": "DEMO", "record_id": (">=", 2)}

    ok_or, key_or, val_or, err_or = validate_filter_syntax(
        "subject_status=Screened,subject_status=Enrolled", model=Subject
    )
    assert (ok_or, key_or, err_or) == (True, "filter", None)
    assert val_or == "subjectStatus==Screened,subjectStatus==Enrolled"

    ok_mixed, key_mixed, val_mixed, err_mixed = validate_filter_syntax(
        "form_key=DEMO;revision>=2,record_id=1", model=Record
    )
    assert (ok_mixed, key_mixed, val_mixed) == (False, None, None)
    assert err_mixed is not None
    assert "cannot combine both ';'" in err_mixed


def test_prompt_filters_compound_and_cross_prompt_exclusivity() -> None:
    """prompt_filters enforces connector exclusivity and duplicate AND checks across prompts."""
    from imednet.spi.models import Subject

    # Entering an OR filter first, then attempting an additional filter must re-prompt on stderr
    err_buf = io.StringIO()
    with (
        patch(
            "sys.stdin",
            _TTYStringIO("subject_status=Screened,subject_status=Enrolled\nsite_name=Alpha\n\n"),
        ),
        patch("sys.stderr", err_buf),
    ):
        collected_or = prompt_filters("subject filter", "subject_status=Screened", model=Subject)
    assert collected_or == ["subject_status=Screened,subject_status=Enrolled"]
    assert "cannot combine both ';'" in err_buf.getvalue()

    # Duplicate canonical AND attribute across two separate prompts is rejected and re-prompted
    err_dup = io.StringIO()
    with (
        patch("sys.stdin", _TTYStringIO("record_id>=1\nrecordId<=10\nform_key=DEMO\n\n")),
        patch("sys.stderr", err_dup),
    ):
        collected_and = prompt_filters("record filter", "form_key=DEMO", model=Record)
    assert collected_and == ["record_id>=1", "form_key=DEMO"]
    assert "Duplicate filter attribute 'recordId'" in err_dup.getvalue()


def test_extract_records_interactive_accepts_compound_and_or_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workflows extract-records --interactive accepts ';' AND and ',' OR filters via flags and prompts."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")  # pragma: allowlist secret

    mock_workflow = MagicMock()
    mock_workflow.extract_records_by_criteria.return_value = []
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow",
        MagicMock(return_value=mock_workflow),
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    # Flag supplies compound ';' record filter; prompt supplies compound ',' subject filter
    stdin_input = (
        "\n".join(
            [
                "STUDY_COMPOUND",
                "",  # finish record filters
                "subject_status=Screened,subject_status=Enrolled",
                "",  # finish subject filters
                "",  # skip visit filters
            ]
        )
        + "\n"
    )

    result = run_cli_command(
        cli_app,
        [
            "workflows",
            "extract-records",
            "--record-filter",
            "form_key=DEMO;record_id>=2",
            "--interactive",
        ],
        stdin_input=stdin_input,
    )

    assert result.exit_code == 0
    mock_workflow.extract_records_by_criteria.assert_called_once()
    call_kwargs = mock_workflow.extract_records_by_criteria.call_args.kwargs
    assert call_kwargs["study_key"] == "STUDY_COMPOUND"
    assert call_kwargs["record_filter"] == {"form_key": "DEMO", "record_id": (">=", 2)}
    assert call_kwargs["subject_filter"] == {
        "filter": "subjectStatus==Screened,subjectStatus==Enrolled"
    }
    assert call_kwargs["visit_filter"] is None


def test_uat_cli_and_wizard_preflight_rejects_invalid_bounds_and_email_before_inspect():
    """Preflight validation rejects invalid subject counts, emails, and --interactive --format json before get_sdk()."""
    from imednet_workflows.wizard import prompt_email_notify, prompt_subject_count

    with patch("imednet.cli.utils.context.get_sdk") as mock_get_sdk:
        res_zero = run_cli_command(
            cli_app,
            ["workflows", "uat", "run", "STUDY1", "--subject-count", "0"],
        )
        assert res_zero.exit_code == 1
        assert "between 1 and 100" in res_zero.stderr
        mock_get_sdk.assert_not_called()

        res_high = run_cli_command(
            cli_app,
            ["workflows", "uat", "build-spec", "--study-key", "STUDY1", "--subject-count", "101"],
        )
        assert res_high.exit_code == 1
        assert "between 1 and 100" in res_high.stderr
        mock_get_sdk.assert_not_called()

        res_bad_email = run_cli_command(
            cli_app,
            ["workflows", "uat", "run", "STUDY1", "--email-notify", "not-an-email"],
        )
        assert res_bad_email.exit_code == 1
        assert "email-notify" in res_bad_email.stderr
        mock_get_sdk.assert_not_called()

        res_crlf = run_cli_command(
            cli_app,
            [
                "workflows",
                "uat",
                "run",
                "STUDY1",
                "--email-notify",
                "ops@example.com\r\nBcc:evil@example.com",
            ],
        )
        assert res_crlf.exit_code == 1
        assert "newlines" in res_crlf.stderr
        mock_get_sdk.assert_not_called()

        res_interactive_json = run_cli_command(
            cli_app,
            ["workflows", "uat", "run", "--interactive", "--format", "json"],
        )
        assert res_interactive_json.exit_code == 1
        assert res_interactive_json.stdout == ""
        assert "Cannot combine --interactive with --format json" in res_interactive_json.stderr
        mock_get_sdk.assert_not_called()

    # Wizard helper validation loops
    tty_in = _TTYStringIO("0\n101\n5\n")
    tty_out = io.StringIO()
    tty_err = io.StringIO()
    with patch("sys.stdin", tty_in), patch("sys.stdout", tty_out), patch("sys.stderr", tty_err):
        assert prompt_subject_count(default=3) == 5
    assert "between 1 and 100" in tty_err.getvalue()

    email_in = _TTYStringIO("bad-email\ntrue\n")
    email_out = io.StringIO()
    email_err = io.StringIO()
    with (
        patch("sys.stdin", email_in),
        patch("sys.stdout", email_out),
        patch("sys.stderr", email_err),
    ):
        assert prompt_email_notify() is True
    assert "email-notify" in email_err.getvalue()


def test_uat_build_spec_and_run_spec_seed_dry_run_and_json_envelope(
    tmp_path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
):
    """Test uat build-spec, --spec, --seed reproducibility, --dry-run, and --format json envelope."""
    import json

    from imednet import ImednetSDK
    from imednet.datadict import DataDictionary, Metadata
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVar
    from imednet_workflows.uat.models import UATSpecification, VariableTestStrategy
    from imednet_workflows.uat.orchestrator import UATWorkflow

    spec_file = tmp_path / "uat_spec.json"

    # 1. build-spec in simulation mode writes deterministic JSON spec with seed
    build_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "build-spec",
            "--study-key",
            "SIM_STUDY",
            "--subject-count",
            "2",
            "--seed",
            "42",
            "--simulation",
            "-o",
            str(spec_file),
        ],
        capsys=capsys,
    )
    assert build_res.exit_code == 0, build_res.stderr
    assert spec_file.exists()
    loaded_spec = UATSpecification.from_json(spec_file)
    assert loaded_spec.seed == 42
    assert len(loaded_spec.subject_specs) == 1
    assert loaded_spec.subject_specs[0].subject_count == 2

    # Verify deterministic payloads from loaded_spec.seed
    with ImednetSDK(simulation_mode=True) as sim_sdk:
        res_a = UATWorkflow(sim_sdk).run("SIM_STUDY", spec=loaded_spec, dry_run=True)
        res_b = UATWorkflow(sim_sdk).run("SIM_STUDY", spec=loaded_spec, dry_run=True)
    assert [rs.payloads for rs in res_a.record_sets] == [rs.payloads for rs in res_b.record_sets]

    # 2. build-spec rejects unknown explicit site on stderr
    bad_site_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "build-spec",
            "--study-key",
            "SIM_STUDY",
            "--site-name",
            "NON_EXISTENT_SITE",
            "--simulation",
        ],
        capsys=capsys,
    )
    assert bad_site_res.exit_code == 1
    assert "NON_EXISTENT_SITE" in bad_site_res.stderr

    # 3. uat run --spec --dry-run --simulation --format json emits clean JSON envelope on stdout and silent stderr
    run_res_1 = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "--spec",
            str(spec_file),
            "--dry-run",
            "--simulation",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert run_res_1.exit_code == 0, run_res_1.stderr
    assert run_res_1.stderr == ""
    envelope_1 = json.loads(run_res_1.stdout)
    assert envelope_1["schemaVersion"] == "1.0"
    assert envelope_1["resource"] == "uat_run"
    assert envelope_1["count"] == 1
    item_1 = envelope_1["items"][0]
    assert item_1["dryRun"] is True
    assert item_1["state"] == "COMPLETED"
    assert item_1["overallSuccess"] is True
    assert item_1["recordsSubmitted"] == 0
    assert item_1["validationReport"]["isValid"] is True

    # 4. Invalid fixed-value override against offline --schema fails --dry-run with exit 1 and emits FAILED JSON envelope
    invalid_spec = loaded_spec.model_copy(deep=True)
    target_form = invalid_spec.form_specs[0]
    target_var = target_form.variables[0]
    target_var.strategy = VariableTestStrategy.FIXED
    target_var.fixed_value = "not-an-integer"
    invalid_spec_file = tmp_path / "invalid_uat_spec.json"
    invalid_spec_file.write_text(
        invalid_spec.model_dump_json(indent=2, by_alias=True), encoding="utf-8"
    )

    strict_dd = DataDictionary(
        metadata=Metadata.create(study_key="SIM_STUDY"),
        forms=[
            DDForm(
                formKey=target_form.form_key,
                formName=target_form.form_name,
                variables=[
                    DDVar(
                        variableName=target_var.variable_name,
                        variableType="Integer",
                        required="Yes",
                    )
                ],
            )
        ],
    )
    dd_file = tmp_path / "strict_dd.json"
    dd_file.write_text(strict_dd.model_dump_json(indent=2), encoding="utf-8")

    fail_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "--spec",
            str(invalid_spec_file),
            "--schema",
            str(dd_file),
            "--dry-run",
            "--simulation",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert fail_res.exit_code == 1
    assert fail_res.stderr == ""
    fail_envelope = json.loads(fail_res.stdout)
    assert fail_envelope["schemaVersion"] == "1.0"
    assert fail_envelope["resource"] == "uat_run"
    fail_item = fail_envelope["items"][0]
    assert fail_item["state"] == "FAILED"
    assert fail_item["overallSuccess"] is False
    assert fail_item["validationReport"]["isValid"] is False

    # 5. IMEDNET_CLI_OUTPUT_FORMAT=json silences print_progress directly
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    err_buf = io.StringIO()
    with patch("sys.stderr", err_buf):
        print_progress("Should be suppressed in JSON mode")
    assert err_buf.getvalue() == ""


def test_uat_run_offline_dry_run_five_formats_zero_http_calls(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 1: uat run STUDY --schema <path> --dry-run --format json exits 0 across all 5 formats without HTTP calls."""
    import json
    import zipfile
    from pathlib import Path

    from tests.unit.datadict.test_datadict_cdisc_odm_loader import SAMPLE_ODM_XML
    from tests.unit.datadict.test_datadict_redcap_loader import SAMPLE_REDCAP_CSV

    from imednet.datadict import load_from_csv

    # Provide real-looking credentials so _forbid_offline_http is enforced on any SDK transport call
    monkeypatch.setenv("IMEDNET_API_KEY", "live_key_should_not_send_http")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "live_sec_should_not_send_http")

    fixtures_dir = Path(__file__).parent.parent.parent / "fixtures" / "data_dictionary"
    json_path = tmp_path / "dd.json"
    json_path.write_text(
        load_from_csv(fixtures_dir, study_key="OTHER").model_dump_json(indent=2),
        encoding="utf-8",
    )
    odm_xml_extra = SAMPLE_ODM_XML.replace(
        '<ItemRef ItemOID="SYSBP" OrderNumber="1" Mandatory="No"/>',
        (
            '<ItemRef ItemOID="SYSBP" OrderNumber="1" Mandatory="No"/>'
            '<ItemRef ItemOID="PDAT" OrderNumber="2" Mandatory="Yes"/>'
            '<ItemRef ItemOID="DOCURI" OrderNumber="3" Mandatory="Yes"/>'
        ),
    ).replace(
        '<CodeList OID="CL_SEX"',
        (
            '<ItemDef OID="PDAT" Name="PDAT" DataType="partialDate"><Question>'
            '<TranslatedText xml:lang="en">Partial Date</TranslatedText></Question></ItemDef>'
            '<ItemDef OID="DOCURI" Name="DOCURI" DataType="URI"><Question>'
            '<TranslatedText xml:lang="en">Document Link</TranslatedText></Question></ItemDef>'
            '<CodeList OID="CL_SEX"'
        ),
    )
    xml_path = tmp_path / "dd.xml"
    xml_path.write_text(odm_xml_extra, encoding="utf-8")
    csv_path = tmp_path / "redcap.csv"
    csv_path.write_text(SAMPLE_REDCAP_CSV, encoding="utf-8")
    zip_path = tmp_path / "dd.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for fname in ("FORMS.csv", "QUESTIONS.csv", "CHOICES.csv", "BUSINESS_LOGIC.csv"):
            zf.write(fixtures_dir / fname, arcname=fname)

    for schema_path in (json_path, xml_path, csv_path, zip_path, fixtures_dir):
        res = run_cli_command(
            cli_app,
            [
                "workflows",
                "uat",
                "run",
                "STUDY",
                "--schema",
                str(schema_path),
                "--dry-run",
                "--format",
                "json",
            ],
            capsys=capsys,
        )
        assert res.exit_code == 0, f"{schema_path}: stderr={res.stderr!r} stdout={res.stdout!r}"
        assert res.stderr == ""
        envelope = json.loads(res.stdout)
        assert envelope["status"] == "complete"
        assert envelope["resource"] == "uat_run"
        assert envelope["count"] == 1
        item = envelope["items"][0]
        assert item["studyKey"] == "STUDY"
        assert item["dryRun"] is True
        assert item["state"] == "COMPLETED"
        assert item["overallSuccess"] is True
        assert item["validationReport"]["isValid"] is True


def test_uat_run_json_failure_envelopes_and_phi_secret_redaction(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 2: Headless uat run --format json emits one status='failed' JSON envelope on preflight, inspection, and validation failures."""
    import json

    from imednet.datadict import DataDictionary, Metadata
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVar
    from imednet.errors import UnauthorizedError
    from imednet_workflows.uat.models import (
        RecordTestType,
        UATFormSpec,
        UATSpecification,
        UATSubjectSpec,
        UATVariableSpec,
        VariableTestStrategy,
    )

    # 1. Preflight failure (--subject-count 0 with secret in --email-notify test)
    pre_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "STUDY_PRE",
            "--subject-count",
            "0",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert pre_res.exit_code == 1
    assert pre_res.stderr == ""
    pre_env = json.loads(pre_res.stdout)
    assert pre_env["status"] == "failed"
    assert pre_env["resource"] == "uat_run"
    assert pre_env["count"] == 1
    assert pre_env["items"][0]["overallSuccess"] is False
    assert pre_env["items"][0]["state"] == "FAILED"

    # 2. Inspection failure (live SDK raises UnauthorizedError containing credential and PHI)
    bad_sdk = MagicMock()
    bad_sdk.get_forms.side_effect = UnauthorizedError(
        "401 Unauthorized for x-api-key: super_secret_token_999 and patientName=Alice_Patient"
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=bad_sdk))
    insp_res = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "STUDY_INSP", "--format", "json"],
        capsys=capsys,
    )
    assert insp_res.exit_code == 1
    assert insp_res.stderr == ""
    insp_env = json.loads(insp_res.stdout)
    assert insp_env["status"] == "failed"
    assert insp_env["items"][0]["failurePhase"] == "INSPECTING"
    assert "super_secret_token_999" not in insp_res.stdout
    assert "Alice_Patient" not in insp_res.stdout

    # 3. Validation failure with PHI variable name (patientName) -> status="failed" and PHI masked
    phi_dd = DataDictionary(
        metadata=Metadata.create(study_key="STUDY_VAL"),
        forms=[
            DDForm(
                formKey="DM",
                formName="Demographics",
                variables=[
                    DDVar(variableName="patientName", variableType="Date", required="Yes"),
                ],
            )
        ],
    )
    phi_dd_path = tmp_path / "phi_dd.json"
    phi_dd_path.write_text(phi_dd.model_dump_json(indent=2), encoding="utf-8")

    phi_spec = UATSpecification(
        study_key="STUDY_VAL",
        study_name="STUDY_VAL",
        subject_specs=[UATSubjectSpec(site_name="Site 1", subject_count=1)],
        form_specs=[
            UATFormSpec(
                form_key="DM",
                form_name="Demographics",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="patientName",
                        variable_key="patientName",
                        variable_type="Text",
                        form_key="DM",
                        required=True,
                        strategy=VariableTestStrategy.FIXED,
                        fixed_value="Jane_Real_Patient_Name",
                    )
                ],
            )
        ],
    )
    phi_spec_path = tmp_path / "phi_spec.json"
    phi_spec_path.write_text(phi_spec.model_dump_json(indent=2, by_alias=True), encoding="utf-8")

    val_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "STUDY_VAL",
            "--spec",
            str(phi_spec_path),
            "--schema",
            str(phi_dd_path),
            "--dry-run",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert val_res.exit_code == 1
    assert val_res.stderr == ""
    val_env = json.loads(val_res.stdout)
    assert val_env["status"] == "failed"
    assert val_env["items"][0]["validationReport"]["isValid"] is False
    assert "Jane_Real_Patient_Name" not in val_res.stdout


def test_uat_run_spec_subject_count_override_and_wizard_defaults_and_clear(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 3, AC 4, AC 5: --spec subject_count preservation/override, '-' clearing, email prompt, Step 1 traversal check, and deferred schema stamping."""
    import json
    from pathlib import Path

    from imednet.datadict import load_from_csv

    fixtures_dir = Path(__file__).parent.parent.parent / "fixtures" / "data_dictionary"
    dd_obj = load_from_csv(fixtures_dir, study_key="UNSTAMPED")
    dd_json_path = tmp_path / "unstamped_dd.json"
    dd_json_path.write_text(dd_obj.model_dump_json(indent=2), encoding="utf-8")

    # Build a saved spec with subject_count=3
    spec_path = tmp_path / "saved_spec.json"
    build_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "build-spec",
            "SIM_STUDY",
            "--subject-count",
            "3",
            "--seed",
            "11",
            "--simulation",
            "-o",
            str(spec_path),
        ],
        capsys=capsys,
    )
    assert build_res.exit_code == 0, build_res.stderr
    from imednet_workflows.uat.models import UATSpecification

    saved_spec = UATSpecification.from_json(spec_path)
    num_enabled_forms = len(saved_spec.enabled_forms())
    assert num_enabled_forms >= 1

    # Headless without --subject-count preserves saved spec count (3 per enabled form)
    keep_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "--spec",
            str(spec_path),
            "--dry-run",
            "--simulation",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert keep_res.exit_code == 0
    assert json.loads(keep_res.stdout)["items"][0]["recordsGenerated"] == 3 * num_enabled_forms

    # Headless with --subject-count 5 updates both Subject and Form counts (5 per enabled form)
    override_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "--spec",
            str(spec_path),
            "--subject-count",
            "5",
            "--dry-run",
            "--simulation",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert override_res.exit_code == 0
    assert json.loads(override_res.stdout)["items"][0]["recordsGenerated"] == 5 * num_enabled_forms

    # Interactive Enter keeps saved spec count (3), while entering 5 generates 5 per enabled form
    enter_keep_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "--spec",
            str(spec_path),
            "--dry-run",
            "--simulation",
            "--interactive",
        ],
        stdin_input="\n\n\n",
        capsys=capsys,
    )
    assert enter_keep_res.exit_code == 0, enter_keep_res.stderr
    assert "[3]:" in enter_keep_res.stderr
    assert (
        f"Total Records:    {3 * num_enabled_forms}" in enter_keep_res.stdout
        or str(3 * num_enabled_forms) in enter_keep_res.stdout
    )

    inter_override_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "--spec",
            str(spec_path),
            "--dry-run",
            "--simulation",
            "--interactive",
        ],
        stdin_input="5\n\n\n",
        capsys=capsys,
    )
    assert inter_override_res.exit_code == 0, inter_override_res.stderr
    assert str(5 * num_enabled_forms) in inter_override_res.stdout

    # AC 4 & AC 5: '-' clears optional defaults (site, forms, email), Enter retains defaults,
    # and interactive uat run stamps the prompted study_key onto loaded --schema & --data-dictionary.
    captured_kwargs: dict[str, object] = {}

    class _RecordingWorkflow:
        def __init__(self, sdk, **kwargs):
            self.init_kwargs = kwargs

        def run(self, **kwargs):
            captured_kwargs.update(kwargs)
            res = MagicMock(spec=UATRunResult)
            res.overall_success = True
            res.summary.return_value = "OK"
            return res

    monkeypatch.setenv("IMEDNET_API_KEY", "dummy_api_key_12345")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "dummy_sec_key_12345")  # pragma: allowlist secret
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("imednet_workflows.uat.UATWorkflow", _RecordingWorkflow)

    clear_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "--site-name",
            "Prefilled Site",
            "--form-key",
            "AE",
            "--email-notify",
            "ops@example.com",
            "--schema",
            str(dd_json_path),
            "--data-dictionary",
            str(dd_json_path),
            "--interactive",
        ],
        # Study=PROMPTED_STUDY, count=Enter(2), site='-' (clear), forms='-' (clear), email='-' (clear)
        stdin_input="PROMPTED_STUDY\n\n-\n-\n-\n",
        capsys=capsys,
    )
    assert clear_res.exit_code == 0, clear_res.stderr
    assert captured_kwargs["study_key"] == "PROMPTED_STUDY"
    assert captured_kwargs["site_name"] is None
    assert captured_kwargs["enabled_form_keys"] is None
    assert captured_kwargs["email_notify"] is None
    assert captured_kwargs["schema"].metadata.studyKey == "PROMPTED_STUDY"  # type: ignore[attr-defined]
    assert captured_kwargs["data_dictionary"].metadata.studyKey == "PROMPTED_STUDY"  # type: ignore[attr-defined]

    # Step 1 path traversal rejection in prompt_study_key and prompt_stream_name
    bad_study_wiz = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "--interactive"],
        stdin_input="../bad\n",
        capsys=capsys,
    )
    assert bad_study_wiz.exit_code == 1
    assert bad_study_wiz.stdout == ""
    assert "study_key" in bad_study_wiz.stderr

    bad_stream_wiz = run_cli_command(
        state_app,
        ["set", "-s", "VALID_STUDY", "--interactive", "-l", str(tmp_path / "l.json")],
        stdin_input="../bad\n",
        capsys=capsys,
    )
    assert bad_stream_wiz.exit_code == 1
    assert bad_stream_wiz.stdout == ""
    assert "stream" in bad_stream_wiz.stderr


def test_all_five_interactive_wizards_reject_json_mode_flag_and_env(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 6: All 5 --interactive workflow wizards exit 1 on stderr with empty stdout under --format json and IMEDNET_CLI_OUTPUT_FORMAT=json."""
    ledger_path = str(tmp_path / "ledger.json")
    commands_with_flag = [
        ["workflows", "extract-records", "--interactive", "--format", "json"],
        ["workflows", "uat", "run", "--interactive", "--format", "json"],
        ["workflows", "state", "show", "--interactive", "--format", "json", "-l", ledger_path],
        ["workflows", "state", "set", "--interactive", "--format", "json", "-l", ledger_path],
        ["workflows", "state", "reset", "--interactive", "--format", "json", "-l", ledger_path],
    ]
    # Test explicit --format json across all 5 wizards
    for argv in commands_with_flag:
        res = run_cli_command(cli_app, argv, stdin_input="SHOULD_NOT_READ\n", capsys=capsys)
        assert res.exit_code == 1, argv
        assert res.stdout == ""
        assert "json" in res.stderr.lower()

    # Test IMEDNET_CLI_OUTPUT_FORMAT=json across all 5 wizards
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    for argv in [
        ["workflows", "extract-records", "--interactive"],
        ["workflows", "uat", "run", "--interactive"],
        ["workflows", "state", "show", "--interactive", "-l", ledger_path],
        ["workflows", "state", "set", "--interactive", "-l", ledger_path],
        ["workflows", "state", "reset", "--interactive", "-l", ledger_path],
    ]:
        res = run_cli_command(cli_app, argv, stdin_input="SHOULD_NOT_READ\n", capsys=capsys)
        assert res.exit_code == 1, argv
        assert res.stdout == ""
        assert "IMEDNET_CLI_OUTPUT_FORMAT=json" in res.stderr


def test_uat_snapshot_metadata_and_cli_json_preflight_and_state_envelopes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cover StudySnapshot metadata intervals/sites, CLI JSON preflight branches, custom PHI masking, and headless state JSON envelopes."""
    import json
    from types import SimpleNamespace

    from imednet.datadict import DataDictionary, Metadata
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVar
    from imednet.models import Interval, Site
    from imednet_workflows.uat.inspector import StudySnapshot
    from imednet_workflows.uat.models import UATSpecification, UATSubjectSpec

    dd = DataDictionary(
        metadata=Metadata(
            studyKey="META_ST",
            extraAttributes={
                "intervals": [
                    Interval(study_key="META_ST", interval_id=1, interval_name="Screening"),
                    {"intervalName": "Week 4", "intervalId": 2, "disabled": "No"},
                ],
                "sites": [
                    Site(
                        study_key="META_ST",
                        site_name="Alpha Clinic",
                        site_enrollment_status="Enrollment Open",
                    ),
                    {"siteName": "Beta Clinic", "siteEnrollmentStatus": "Enrollment Open"},
                ],
            },
        ),
        forms=[
            DDForm(
                formKey="DM",
                formName="Demographics",
                extraAttributes={
                    "formId": 10,
                    "subjectRecordReport": "Yes",
                    "visits": ["Unscheduled", "Week 8"],
                    "intervalName": "Screening",
                },
                variables=[
                    DDVar(
                        variableName="CUSTOM_SECRET_ID",
                        variableType="Text",
                        required="Yes",
                        extraAttributes={"variableId": 77, "itemOid": "OID_77", "disabled": "No"},
                    )
                ],
            )
        ],
    )
    snap = StudySnapshot.from_data_dictionary(dd, "META_ST")
    assert [i.interval_name for i in snap.intervals] == ["Screening", "Week 4"]
    assert [s.site_name for s in snap.sites] == ["Alpha Clinic", "Beta Clinic"]
    assert snap.variables[0].variable_id == 77
    assert snap.variables[0].variable_oid == "OID_77"

    # Custom study PHI configuration masks custom_phi_fields in JSON failure envelopes and stderr
    monkeypatch.setattr(
        "imednet_workflows.cli.resolve_study_configuration",
        lambda _sk: SimpleNamespace(phi_fields=["CUSTOM_SECRET_ID"]),
    )

    bad_spec_file = tmp_path / "corrupt_spec.json"
    bad_spec_file.write_text("{not-json", encoding="utf-8")
    mismatch_spec_file = tmp_path / "mismatch_spec.json"
    mismatch_spec_file.write_text(
        UATSpecification(
            study_key="OTHER_STUDY",
            study_name="OTHER_STUDY",
            subject_specs=[UATSubjectSpec(site_name="Alpha Clinic", subject_count=1)],
            form_specs=[],
        ).model_dump_json(by_alias=True),
        encoding="utf-8",
    )

    for argv in [
        ["workflows", "uat", "run", "   ", "--format", "json"],
        ["workflows", "uat", "run", "../traversal", "--format", "json"],
        [
            "workflows",
            "uat",
            "run",
            "META_ST",
            "--spec",
            str(tmp_path / "missing.json"),
            "--format",
            "json",
        ],
        ["workflows", "uat", "run", "META_ST", "--spec", str(bad_spec_file), "--format", "json"],
        [
            "workflows",
            "uat",
            "run",
            "META_ST",
            "--spec",
            str(mismatch_spec_file),
            "--format",
            "json",
        ],
        [
            "workflows",
            "uat",
            "run",
            "META_ST",
            "--email-notify",
            "CUSTOM_SECRET_ID=Patient42\r\nbad",
            "--format",
            "json",
        ],
    ]:
        res = run_cli_command(cli_app, argv, capsys=capsys)
        assert res.exit_code == 1, argv
        assert res.stderr == ""
        env = json.loads(res.stdout)
        assert env["status"] == "failed"
        assert "Patient42" not in res.stdout

    # Headless non-JSON preflight error masks custom PHI on stderr
    err_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "META_ST",
            "--email-notify",
            "CUSTOM_SECRET_ID=Patient42\r\nbad",
        ],
        capsys=capsys,
    )
    assert err_res.exit_code == 1
    assert err_res.stdout == ""
    assert "Patient42" not in err_res.stderr

    # Headless state set and state reset --format json emit ledger_state envelopes
    ledger_file = str(tmp_path / "json_state.json")
    set_json = run_cli_command(
        cli_app,
        [
            "workflows",
            "state",
            "set",
            "-s",
            "META_ST",
            "-m",
            "records",
            "-t",
            "2026-06-01T00:00:00Z",
            "-r",
            "5",
            "-l",
            ledger_file,
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert set_json.exit_code == 0
    assert json.loads(set_json.stdout)["resource"] == "ledger_state"

    reset_stream_json = run_cli_command(
        cli_app,
        [
            "workflows",
            "state",
            "reset",
            "-s",
            "META_ST",
            "-m",
            "records",
            "-l",
            ledger_file,
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert reset_stream_json.exit_code == 0
    assert json.loads(reset_stream_json.stdout)["items"][0]["removed"] is True

    reset_study_json = run_cli_command(
        cli_app,
        ["workflows", "state", "reset", "-s", "META_ST", "-l", ledger_file, "--format", "json"],
        capsys=capsys,
    )
    assert reset_study_json.exit_code == 0
    assert json.loads(reset_study_json.stdout)["items"][0]["removed"] is True

    reset_study_again = run_cli_command(
        cli_app,
        ["workflows", "state", "reset", "-s", "META_ST", "-l", ledger_file, "--format", "json"],
        capsys=capsys,
    )
    assert reset_study_again.exit_code == 0
    assert json.loads(reset_study_again.stdout)["items"][0]["removed"] is False


def test_uat_run_interactive_live_prompts_email_notify_with_simulation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Interactive live uat_run invokes prompt_email_notify, validates input, and runs against SimulationTransport."""
    res = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "--simulation", "--interactive"],
        # study=STUDY, count=1, site=SITE, forms=F_DM, invalid email -> valid email
        stdin_input="STUDY\n1\nSITE\nF_DM\nnot-an-email\nqa@example.com\n",
        capsys=capsys,
    )
    assert res.exit_code == 0, res.stderr
    assert "Enter email notification" in res.stderr
    assert "Invalid --email-notify value" in res.stderr
    assert "Overall: PASS" in res.stdout


def test_uat_run_json_failure_envelope_across_all_workflow_stages(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Headless uat run --format json emits _serialize_uat_run_result envelope for every failed UATRunResult stage."""
    import json
    from datetime import UTC, datetime

    from imednet_workflows.uat import UATRunResult, UATWorkflowState

    expected_keys = {
        "studyKey",
        "runId",
        "state",
        "stateHistory",
        "failurePhase",
        "dryRun",
        "overallSuccess",
        "formsTested",
        "recordsGenerated",
        "recordsSubmitted",
        "jobsMonitored",
        "validationReport",
        "phaseDurations",
        "errorMessage",
    }

    # 1. Returned failed UATRunResult across INSPECTING, BUILDING_SPEC, GENERATING, SUBMITTING, MONITORING
    for stage in (
        UATWorkflowState.INSPECTING,
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.GENERATING,
        UATWorkflowState.SUBMITTING,
        UATWorkflowState.MONITORING,
    ):
        now = datetime.now(UTC)
        failed_run = UATRunResult(
            study_key="STUDY",
            run_id=f"run-{stage.value.lower()}",
            started_at=now,
            completed_at=now,
            state=UATWorkflowState.FAILED,
            state_history=[stage, UATWorkflowState.FAILED],
            failure_phase=stage,
            error_message=f"Failure during {stage.value} [variable=DOB] '1980-01-01'",
            phase_durations={stage.value: 0.12},
        )

        class _StageFailingWorkflow:
            def __init__(self, sdk, **kwargs):
                self.last_result = failed_run

            def run(self, **_kwargs):
                return self.last_result

        monkeypatch.setattr("imednet_workflows.uat.UATWorkflow", _StageFailingWorkflow)
        res = run_cli_command(
            cli_app,
            ["workflows", "uat", "run", "STUDY", "--simulation", "--format", "json"],
            capsys=capsys,
        )
        assert res.exit_code == 1, stage
        assert res.stderr == ""
        env = json.loads(res.stdout)
        assert env["resource"] == "uat_run"
        assert env["status"] == "failed"
        assert len(env["items"]) == 1
        item = env["items"][0]
        assert expected_keys.issubset(item.keys())
        assert item["studyKey"] == "STUDY"
        assert item["state"] == "FAILED"
        assert item["failurePhase"] == stage.value
        assert item["stateHistory"] == [stage.value, "FAILED"]
        assert item["overallSuccess"] is False
        assert "1980-01-01" not in res.stdout

    # 2. Raised exception with workflow.last_result populated also serializes via _serialize_uat_run_result
    now = datetime.now(UTC)
    raised_last = UATRunResult(
        study_key="STUDY",
        run_id="run-raised-submit",
        started_at=now,
        completed_at=now,
        state=UATWorkflowState.FAILED,
        state_history=[UATWorkflowState.SUBMITTING, UATWorkflowState.FAILED],
        failure_phase=UATWorkflowState.SUBMITTING,
        error_message="Batch submission failed",
    )

    class _RaisingWorkflow:
        def __init__(self, sdk, **kwargs):
            self.last_result = raised_last

        def run(self, **_kwargs):
            raise RuntimeError("Batch submission failed")

    monkeypatch.setattr("imednet_workflows.uat.UATWorkflow", _RaisingWorkflow)
    raised_res = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "STUDY", "--simulation", "--format", "json"],
        capsys=capsys,
    )
    assert raised_res.exit_code == 1
    assert raised_res.stderr == ""
    raised_env = json.loads(raised_res.stdout)
    assert raised_env["resource"] == "uat_run"
    assert raised_env["status"] == "failed"
    assert raised_env["items"][0]["runId"] == "run-raised-submit"
    assert raised_env["items"][0]["failurePhase"] == "SUBMITTING"


def test_uat_run_real_workflow_json_failures_and_spec_subject_count_override(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end CLI uat run --format json emits uat_run envelopes on real stage failures and honors --subject-count on --spec."""
    import json

    import httpx

    from imednet import ImednetSDK
    from imednet.simulation import SimulationState, create_simulation_transport
    from imednet_workflows.uat import (
        RecordTestType,
        UATFormSpec,
        UATSpecification,
        UATSubjectSpec,
        UATVariableSpec,
    )

    monkeypatch.setenv("IMEDNET_API_KEY", "dummy")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "dummy")

    # 1. INSPECTING failure (HTTP 500 on GET /forms)
    def inspect_fail_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"metadata": {"error": {"message": "Forms 500"}}})

    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kw: ImednetSDK(
            transport=httpx.MockTransport(inspect_fail_handler), simulation_mode=True
        ),
    )
    ins_res = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "STUDY", "--format", "json"],
        capsys=capsys,
    )
    assert ins_res.exit_code == 1
    assert ins_res.stderr == ""
    ins_env = json.loads(ins_res.stdout)
    assert ins_env["resource"] == "uat_run"
    assert ins_env["status"] == "failed"
    assert ins_env["items"][0]["failurePhase"] == "INSPECTING"

    # 2. GENERATING failure (spec with zero enabled forms)
    empty_spec_path = tmp_path / "empty_spec.json"
    empty_spec_path.write_text(
        UATSpecification(
            study_key="STUDY",
            study_name="STUDY",
            subject_specs=[UATSubjectSpec(site_name="SITE", subject_count=1)],
            form_specs=[
                UATFormSpec(
                    form_key="F_DM",
                    form_name="Demographics",
                    form_type="Enrollment",
                    test_type=RecordTestType.REGISTER_SUBJECT,
                    enabled=False,
                    subject_count=1,
                    variables=[],
                )
            ],
        ).model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kw: ImednetSDK(
            transport=httpx.MockTransport(
                create_simulation_transport(SimulationState()).handle_request
            ),
            simulation_mode=True,
        ),
    )
    gen_res = run_cli_command(
        cli_app,
        ["workflows", "uat", "run", "STUDY", "--spec", str(empty_spec_path), "--format", "json"],
        capsys=capsys,
    )
    assert gen_res.exit_code == 1
    gen_env = json.loads(gen_res.stdout)
    assert gen_env["resource"] == "uat_run"
    assert gen_env["items"][0]["failurePhase"] == "GENERATING"

    # 3. SUBMITTING failure (HTTP 400 on POST /records)
    sub_sim = create_simulation_transport(SimulationState())

    def submit_fail_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/records"):
            return httpx.Response(400, json={"metadata": {"error": {"message": "Bad record"}}})
        return sub_sim.handle_request(req)

    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kw: ImednetSDK(
            transport=httpx.MockTransport(submit_fail_handler), simulation_mode=True
        ),
    )
    sub_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "STUDY",
            "--form-key",
            "F_DM",
            "--subject-count",
            "1",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert sub_res.exit_code == 1
    sub_env = json.loads(sub_res.stdout)
    assert sub_env["resource"] == "uat_run"
    assert sub_env["items"][0]["failurePhase"] == "SUBMITTING"

    # 4. MONITORING failure (GET /jobs returns FAILED for non-registration batch)
    mon_state = SimulationState()
    mon_state.subjects[0]["keywords"] = [{"keywordName": "UAT", "keywordKey": "UAT"}]
    mon_sim = create_simulation_transport(mon_state)

    def monitor_fail_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/jobs/" in req.url.path:
            bid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={"batchId": bid, "state": "FAILED", "failedRecords": 1},
            )
        return mon_sim.handle_request(req)

    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kw: ImednetSDK(
            transport=httpx.MockTransport(monitor_fail_handler), simulation_mode=True
        ),
    )
    mon_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "STUDY",
            "--form-key",
            "F_AE",
            "--subject-count",
            "1",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert mon_res.exit_code == 1
    mon_env = json.loads(mon_res.stdout)
    assert mon_env["resource"] == "uat_run"
    assert mon_env["items"][0]["failurePhase"] == "MONITORING"

    # 5. Saved --spec with --subject-count override generates & submits overridden count
    saved_spec_path = tmp_path / "saved_dm_spec.json"
    saved_spec_path.write_text(
        UATSpecification(
            study_key="STUDY",
            study_name="STUDY",
            subject_specs=[UATSubjectSpec(site_name="SITE", subject_count=1)],
            form_specs=[
                UATFormSpec(
                    form_key="F_DM",
                    form_name="Demographics",
                    form_type="Enrollment",
                    test_type=RecordTestType.REGISTER_SUBJECT,
                    subject_count=1,
                    variables=[
                        UATVariableSpec(
                            variable_name="V_AGE",
                            variable_key="V_AGE",
                            variable_type="Integer",
                            form_key="F_DM",
                        )
                    ],
                )
            ],
        ).model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    override_state = SimulationState()
    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda **_kw: ImednetSDK(
            transport=httpx.MockTransport(
                create_simulation_transport(override_state).handle_request
            ),
            simulation_mode=True,
        ),
    )
    ov_res = run_cli_command(
        cli_app,
        [
            "workflows",
            "uat",
            "run",
            "STUDY",
            "--spec",
            str(saved_spec_path),
            "--subject-count",
            "3",
            "--format",
            "json",
        ],
        capsys=capsys,
    )
    assert ov_res.exit_code == 0, ov_res.stderr
    ov_env = json.loads(ov_res.stdout)
    assert ov_env["status"] == "complete"
    assert ov_env["items"][0]["recordsGenerated"] == 3
    assert ov_env["items"][0]["recordsSubmitted"] == 3
