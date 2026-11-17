"""Unit tests for utils output."""

import io
import time
from unittest.mock import MagicMock, patch

import pytest

from imednet.cli.utils import (
    _STATUS_COLOR_MAP,
    Console,
    _format_cell_value,
    clear_line,
    display_list,
    fetching_status,
    format_job_progress,
    print_error,
)
from imednet.models.jobs import JobState, JobStatus
from imednet.utils.job_poller import JobStatusEvent


def test_fetching_status_records() -> None:
    """fetching_status shows status spinner with message."""
    with patch("imednet.cli.utils.context.console") as mock_console:
        mock_status = mock_console.status.return_value
        mock_status.__enter__.return_value = None

        with fetching_status("records", "ST"):
            pass

        mock_console.status.assert_called_with("Fetching records for study 'ST'...")
        mock_status.__enter__.assert_called()
        mock_status.__exit__.assert_called()


def test_fetching_status_escapes_injection() -> None:
    """fetching_status escapes Rich markup in study key."""
    with patch("imednet.cli.utils.context.console") as mock_console:
        mock_status = mock_console.status.return_value
        mock_status.__enter__.return_value = None

        with fetching_status("records", "[bold]bad[/bold]"):
            pass

        args, _kwargs = mock_console.status.call_args
        msg = args[0]
        # rich.markup.escape escapes brackets
        assert "[bold]bad[/bold]" in msg


def test_display_list_non_empty(capfd: pytest.CaptureFixture[str]) -> None:
    """display_list shows count and items for non-empty lists."""
    display_list([1, 2], "items")
    captured = capfd.readouterr()
    assert captured.out == "Found 2 items:\n[1, 2]\n"
    assert captured.err == ""


def test_display_list_empty(capfd: pytest.CaptureFixture[str]) -> None:
    """display_list prints default message for empty lists."""
    display_list([], "items")
    captured = capfd.readouterr()
    assert captured.out == "No items found.\n"
    assert captured.err == ""


def test_display_list_table(capfd: pytest.CaptureFixture[str]) -> None:
    """display_list prints a table for objects with model_dump."""
    obj = MagicMock()
    obj.model_dump.return_value = {"my_key": "my_val"}

    display_list([obj], "items")
    captured = capfd.readouterr()

    assert "Found 1 items:" in captured.out
    assert "My Key" in captured.out  # Header title-ized and underscores replaced
    assert "my_val" in captured.out


def test_display_list_formatting(capfd: pytest.CaptureFixture[str]) -> None:
    """display_list formats booleans and datetimes."""
    from datetime import datetime

    obj = MagicMock()
    obj.model_dump.return_value = {
        "active": True,
        "inactive": False,
        "date": datetime(2023, 1, 1, 12, 0, 0),
        "none": None,
    }
    display_list([obj], "items")
    captured = capfd.readouterr()

    assert "2023-01-01 12:00" in captured.out
    assert "Yes" in captured.out
    assert "No" in captured.out
    assert "-" in captured.out


def test_display_list_formatting_lists(capfd: pytest.CaptureFixture[str]) -> None:
    """display_list formats lists of primitives as comma-separated strings."""
    obj = MagicMock()
    obj.model_dump.return_value = {
        "tags": ["a", "b", "c"],
        "numbers": [1, 2],
        "empty": [],
        "complex": [{"a": 1}],
    }
    display_list([obj], "items")
    captured = capfd.readouterr()

    assert "a, b, c" in captured.out
    assert "1, 2" in captured.out
    assert "-" in captured.out
    # Complex lists fall back to truncation/repr
    assert "[{'a': 1}]" in captured.out


def test_display_list_escaping(capfd: pytest.CaptureFixture[str]) -> None:
    """display_list escapes Rich markup in user data."""
    obj = MagicMock()
    obj.model_dump.return_value = {"content": "[bold red]hack[/bold red]"}

    display_list([obj], "items")
    captured = capfd.readouterr()

    # If escaped, the markup tags are preserved in the output text
    assert "[bold red]hack[/bold red]" in captured.out


def test_display_list_with_fields(capfd: pytest.CaptureFixture[str]) -> None:
    """display_list uses optimized path when fields are provided."""
    obj = MagicMock()
    # Mock attribute access for optimized path
    obj.id = 123
    obj.name = "Test Item"
    # Ensure model_dump is NOT called
    obj.model_dump = MagicMock()

    display_list([obj], "items", fields=["id", "name"])
    captured = capfd.readouterr()

    assert "Found 1 items:" in captured.out
    assert "Id" in captured.out
    assert "Name" in captured.out
    assert "123" in captured.out
    assert "Test Item" in captured.out

    # Verify optimization: model_dump should not be called
    obj.model_dump.assert_not_called()


def test_format_cell_value_status_colors() -> None:
    """Test that status columns are correctly colorized."""
    # Green statuses
    assert _format_cell_value("Active", key="status") == "Active"
    assert _format_cell_value("Success", key="status") == "Success"
    assert _format_cell_value("OK", key="status") == "OK"

    # Yellow statuses
    assert _format_cell_value("Pending", key="status") == "Pending"
    assert _format_cell_value("Processing", key="status") == "Processing"

    # Red statuses
    assert _format_cell_value("Inactive", key="status") == "Inactive"
    assert _format_cell_value("Error", key="status") == "Error"
    assert _format_cell_value("Failed", key="status") == "Failed"


def test_format_cell_value_non_status_columns() -> None:
    """Test that non-status columns are not colorized based on value."""
    # Even if value matches a status keyword, if key is not status/state, no color
    assert _format_cell_value("Active", key="name") == "Active"
    assert _format_cell_value("Pending", key="description") == "Pending"


def test_format_cell_value_unknown_status() -> None:
    """Test that unknown statuses are not colorized."""
    assert _format_cell_value("UnknownStatus", key="status") == "UnknownStatus"


def test_print_error_writes_to_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    """print_error writes to stderr and preserves clean stdout."""
    print_error("Failed to connect to study service.")
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "Failed to connect to study service.\n" == captured.err


def test_print_error_strips_ansi_on_non_tty(capfd: pytest.CaptureFixture[str]) -> None:
    """print_error strips ANSI escape sequences when stderr is non-interactive."""
    colored_msg = "\033[31mError:\033[0m something went wrong"
    print_error(colored_msg)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == "Error: something went wrong\n"


def test_print_error_preserves_ansi_on_tty() -> None:
    """print_error preserves ANSI escape sequences when stderr is a tty."""
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True
    colored_msg = "\033[31mError:\033[0m failure"
    print_error(colored_msg, file=mock_stream)
    mock_stream.write.assert_any_call("\033[31mError:\033[0m failure")


def test_print_error_handles_broken_pipe_and_ioerror() -> None:
    """print_error handles BrokenPipeError and IOError without raising."""
    broken_stream = MagicMock()
    broken_stream.write.side_effect = BrokenPipeError("Broken pipe")
    # Should not raise exception
    print_error("Error message", file=broken_stream)

    io_stream = MagicMock()
    io_stream.write.side_effect = OSError("I/O failure")
    # Should not raise exception
    print_error("Error message", file=io_stream)


def test_status_spinner_rendering_interactive() -> None:
    """Console.status writes animated spinner frames, elapsed timer, and restores cursor on interactive streams."""
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    c = Console(stream=mock_stream)
    with c.status("Processing records...", refresh_per_second=20):
        time.sleep(0.08)

    calls = [call[0][0] for call in mock_stream.write.call_args_list if call[0]]
    all_output = "".join(calls)

    # Verifies cursor hidden on enter
    assert "\033[?25l" in all_output
    # Verifies animated spinner frame with message and timer was written
    assert "Processing records..." in all_output
    assert "s)" in all_output  # elapsed timer suffix like (0.1s)
    # Verifies line cleared and cursor restored on exit
    assert "\r\033[K" in all_output
    assert "\033[?25h" in all_output


def test_status_update_interactive() -> None:
    """Status.update dynamically updates message and refreshes display."""
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    c = Console(stream=mock_stream)
    with c.status("Phase 1") as status:
        status.update("Phase 2 - downloading")

    calls = [call[0][0] for call in mock_stream.write.call_args_list if call[0]]
    all_output = "".join(calls)

    assert "Phase 1" in all_output
    assert "Phase 2 - downloading" in all_output


def test_status_interactive_exception_cleans_up() -> None:
    """Status restores terminal cursor and clears line when exception occurs."""
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    c = Console(stream=mock_stream)
    with pytest.raises(RuntimeError):
        with c.status("Failing task"):
            raise RuntimeError("Operation aborted")

    calls = [call[0][0] for call in mock_stream.write.call_args_list if call[0]]
    all_output = "".join(calls)

    assert "\033[?25h" in all_output  # Cursor restored
    assert "\r\033[K" in all_output  # Line cleared


def test_status_interactive_keyboard_interrupt_cleans_up() -> None:
    """Status restores terminal cursor and clears line on KeyboardInterrupt."""
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    c = Console(stream=mock_stream)
    with pytest.raises(KeyboardInterrupt):
        with c.status("Interrupted task"):
            raise KeyboardInterrupt()

    calls = [call[0][0] for call in mock_stream.write.call_args_list if call[0]]
    all_output = "".join(calls)

    assert "\033[?25h" in all_output
    assert "\r\033[K" in all_output


def test_clear_line_interactive_and_non_interactive() -> None:
    """clear_line writes escape sequences on tty and is silent on non-tty."""
    interactive_stream = MagicMock()
    interactive_stream.isatty.return_value = True
    clear_line(stream=interactive_stream)
    interactive_stream.write.assert_called_once_with("\r\033[K\033[?25h")

    non_tty_stream = MagicMock()
    non_tty_stream.isatty.return_value = False
    clear_line(stream=non_tty_stream)
    non_tty_stream.write.assert_not_called()


def test_status_non_interactive_fallback() -> None:
    """Console.status outputs a single static text line without ANSI codes on non-interactive streams."""
    output_buffer = io.StringIO()
    # Explicitly non-interactive custom stream
    output_buffer.isatty = lambda: False  # type: ignore[assignment]

    c = Console(stream=output_buffer)
    with c.status("\033[32mFetching dataset\033[0m for study..."):
        pass

    result = output_buffer.getvalue()
    assert result == "Fetching dataset for study...\n"
    assert "\033[" not in result  # No ANSI escape sequences


def test_format_job_progress() -> None:
    """format_job_progress formats JobStatusEvent into progress bar, state label, and timer."""
    status = JobStatus(batch_id="BATCH-99", state=JobState.PROCESSING, progress=45)
    event = JobStatusEvent(
        batch_id="BATCH-99",
        status=status,
        poll_number=3,
        elapsed=12.4,
        is_terminal=False,
    )

    formatted = format_job_progress(event)

    assert "Job BATCH-99:" in formatted
    assert "[=========" in formatted  # 45% of 20 chars is 9 chars filled
    assert "45%" in formatted
    assert "PROCESSING" in formatted
    assert "(12.4s)" in formatted


def test_format_job_progress_bounds_and_arguments() -> None:
    """format_job_progress clamps progress percentage and supports alternative arguments."""
    status_over = JobStatus(batch_id="B1", state="COMPLETED", progress=150)
    event_over = JobStatusEvent(
        batch_id="B1", status=status_over, poll_number=1, elapsed=2.0, is_terminal=True
    )
    formatted_over = format_job_progress(event_over)
    assert "[====================] 100%" in formatted_over
    assert "COMPLETED" in formatted_over

    status_neg = JobStatus(batch_id="B2", state="PENDING", progress=-10)
    event_neg = JobStatusEvent(
        batch_id="B2", status=status_neg, poll_number=1, elapsed=0.5, is_terminal=False
    )
    formatted_neg = format_job_progress(event_neg)
    assert "[--------------------] 0%" in formatted_neg
    assert "PENDING" in formatted_neg

    # Direct argument invocation
    formatted_direct = format_job_progress("B3", status=status_over, elapsed=3.0)
    assert "Job B3:" in formatted_direct
    assert "(3.0s)" in formatted_direct


def test_job_wait_progress_callback_and_clean_stdout_json() -> None:
    """job_wait connects on_progress to sdk.poll_job and keeps JSON stdout clean."""
    import json

    from tests.unit.cli.test_cli import CliRunner

    from imednet import cli

    runner = CliRunner()
    completed_job = JobStatus(batch_id="B-123", state="COMPLETED", progress=100)

    progress_events_captured: list[str] = []

    def mock_poll_job(study_key, batch_id, interval=5, timeout=300, on_progress=None):
        if on_progress:
            # Simulate progress callback invocations
            event1 = JobStatusEvent(
                batch_id=batch_id,
                status=JobStatus(batch_id=batch_id, state="PROCESSING", progress=50),
                poll_number=1,
                elapsed=2.5,
                is_terminal=False,
            )
            on_progress(event1)
            progress_events_captured.append(event1.batch_id)
            event2 = JobStatusEvent(
                batch_id=batch_id,
                status=completed_job,
                poll_number=2,
                elapsed=5.0,
                is_terminal=True,
            )
            on_progress(event2)
            progress_events_captured.append(event2.batch_id)
        return completed_job

    mock_sdk = MagicMock()
    mock_sdk.poll_job.side_effect = mock_poll_job

    with patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk):
        result = runner.invoke(cli.app, ["jobs", "wait", "STUDY-TEST", "B-123", "--format", "json"])

    assert result.exit_code == 0
    assert progress_events_captured == ["B-123", "B-123"]
    # stdout must be strictly valid JSON without any progress bar or spinner noise
    parsed = json.loads(result.stdout)
    assert parsed["schemaVersion"] == "1.0"
    assert parsed["resource"] == "jobs"
    assert parsed["status"] == "complete"
    assert len(parsed["items"]) == 1
    assert parsed["items"][0]["batchId"] == "B-123"
    assert parsed["items"][0]["state"] == "COMPLETED"
    assert "Job B-123:" not in result.stdout
    assert "[" not in result.stdout[:2]  # envelope starts with '{' not progress bracket '['


def test_sync_worker_visual_feedback_iterative_cycles() -> None:
    """sync_worker visual progress updates status with cycle count and sleep countdown."""
    from tests.unit.cli.test_cli import CliRunner

    from imednet import cli

    runner = CliRunner()
    cycles_seen = []
    ticks_seen = []

    def mock_run_forever(on_cycle=None, on_sleep_tick=None):
        if on_cycle:
            on_cycle(1, 42)
            cycles_seen.append((1, 42))
        if on_sleep_tick:
            on_sleep_tick(1, 10)
            on_sleep_tick(1, 9)
            ticks_seen.extend([(1, 10), (1, 9)])
        if on_cycle:
            on_cycle(2, 55)
            cycles_seen.append((2, 55))
        raise KeyboardInterrupt()

    mock_worker = MagicMock()
    mock_worker.run_forever.side_effect = mock_run_forever
    mock_loader_cls = MagicMock()
    mock_sdk = MagicMock()

    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk),
        patch("imednet_workflows.cached_loader.CachedRecordsLoader", mock_loader_cls),
        patch("imednet_workflows.cli.SyncWorker", return_value=mock_worker),
    ):
        result = runner.invoke(cli.app, ["workflows", "sync-worker", "STUDY-X", "--interval", "10"])

    assert result.exit_code == 0
    assert cycles_seen == [(1, 42), (2, 55)]
    assert ticks_seen == [(1, 10), (1, 9)]
    mock_worker.stop.assert_called_once()
    assert "Sync worker termination requested. Exiting cleanly." in result.stdout


def test_format_cell_value_empty_string_hyphen() -> None:
    """Empty strings and None format as hyphens in table cells."""
    assert _format_cell_value("") == "-"
    assert _format_cell_value(None) == "-"
    assert _format_cell_value("valid") == "valid"


def test_job_state_color_mappings() -> None:
    """Verify job states are registered in _STATUS_COLOR_MAP."""
    assert _STATUS_COLOR_MAP["running"] == "yellow"
    assert _STATUS_COLOR_MAP["queued"] == "yellow"
    assert _STATUS_COLOR_MAP["complete"] == "green"
    assert _STATUS_COLOR_MAP["cancelled"] == "red"
    assert _STATUS_COLOR_MAP["timeout"] == "red"


def test_print_table_job_state_colors(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """_print_table applies status colors to state columns."""
    from imednet.cli.utils.output import _print_table

    items = [
        {"state": "RUNNING", "batch_id": "B1"},
        {"state": "COMPLETE", "batch_id": "B2"},
        {"state": "CANCELLED", "batch_id": "B3"},
    ]
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    _print_table(items, ["batch_id", "state"])
    captured = capfd.readouterr()
    assert "\033[33mRUNNING\033[0m" in captured.out
    assert "\033[32mCOMPLETE\033[0m" in captured.out
    assert "\033[31mCANCELLED\033[0m" in captured.out

    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    _print_table(items, ["batch_id", "state"])
    captured_hc = capfd.readouterr()
    assert "\033[33mRUNNING\033[0m" in captured_hc.out
    assert "\033[34mCOMPLETE\033[0m" in captured_hc.out
    assert "\033[35mCANCELLED\033[0m" in captured_hc.out
