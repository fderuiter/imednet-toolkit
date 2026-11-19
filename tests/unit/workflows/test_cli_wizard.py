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
    from contextlib import redirect_stderr, redirect_stdout

    out = io.StringIO()
    err = io.StringIO()
    stdin_stream = _TTYStringIO(stdin_input) if tty_stdin else io.StringIO(stdin_input)
    exit_code = 0
    with redirect_stdout(out), redirect_stderr(err), patch("sys.stdin", stdin_stream):
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
    """Test workflows uat run exits with 1 when pipeline fails."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=MagicMock()))

    mock_run_result = MagicMock(spec=UATRunResult)
    mock_run_result.overall_success = False
    mock_run_result.summary.return_value = "=== UAT RUN SUMMARY ===\nStatus: FAIL"

    mock_uat_workflow = MagicMock()
    mock_uat_workflow.run.return_value = mock_run_result

    monkeypatch.setattr(
        "imednet_workflows.uat.UATWorkflow",
        MagicMock(return_value=mock_uat_workflow),
    )

    result = run_cli_command(cli_app, ["workflows", "uat", "run", "FAIL_STUDY"])

    assert result.exit_code == 1
    assert "Status: FAIL" in result.stdout


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
