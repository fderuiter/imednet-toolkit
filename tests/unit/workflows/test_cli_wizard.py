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
    app_fn, args: list[str], stdin_input: str = "", *, tty_stdin: bool = True
) -> CliResult:
    """Invoke CLI entry point with redirected stdin, stdout, and stderr."""
    import logging
    from contextlib import nullcontext, redirect_stderr, redirect_stdout

    out = io.StringIO()
    err = io.StringIO()
    stdin_stream = _TTYStringIO(stdin_input) if tty_stdin else io.StringIO(stdin_input)
    exit_code = 0
    capture_manager = next(
        (
            getattr(handler, "capture_manager", None)
            for handler in logging.getLogger().handlers
            if getattr(handler, "capture_manager", None) is not None
        ),
        None,
    )
    suspend_ctx = (
        capture_manager.global_and_fixture_disabled()
        if capture_manager is not None
        else nullcontext()
    )
    with (
        suspend_ctx,
        redirect_stdout(out),
        redirect_stderr(err),
        patch("sys.stdin", stdin_stream),
    ):
        try:
            app_fn(args)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)

    return CliResult(exit_code, out.getvalue(), err.getvalue())


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

        # Check ledger was called with force=True
        mock_ledger.return_value.set_last_timestamp.assert_called_once()
        call_kwargs = mock_ledger.return_value.set_last_timestamp.call_args.kwargs
        assert call_kwargs["study_key"] == "TEST_STUDY"
        assert call_kwargs["stream_name"] == "records"
        assert call_kwargs["records_processed"] == 25
        assert call_kwargs["force"] is True


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
    stdin_input = "UAT_STUDY\n5\nSite Alpha\nDEMO, VISITS\n"

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


def test_uat_build_spec_and_run_spec_seed_dry_run_and_json_envelope(tmp_path, monkeypatch):
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
