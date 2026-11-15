"""Unit tests for utils output."""

from unittest.mock import MagicMock, patch

import pytest

from imednet.cli.utils import (
    _format_cell_value,
    display_list,
    fetching_status,
    print_error,
)


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
