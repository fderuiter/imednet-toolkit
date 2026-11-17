"""Unit tests for interactive keyboard data grid with side panel inspection."""

from __future__ import annotations

import curses
from unittest.mock import MagicMock, patch

import pytest

from imednet.cli.utils.grid import (
    InteractiveDataGrid,
    extract_item_details,
    is_interactive_terminal,
    run_interactive_grid,
    safe_addstr,
)
from imednet.cli.utils.output import display_list
from imednet.models.records import Record
from imednet.models.subjects import Subject, SubjectKeyword


class MockWindow:
    """Mock curses window for terminal UI testing."""

    def __init__(self, height: int = 24, width: int = 80) -> None:
        self.height = height
        self.width = width
        self.strings: list[tuple[int, int, str, int]] = []
        self.cursor_pos: tuple[int, int] = (0, 0)
        self.keypad_enabled: bool = False

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.strings.clear()

    def refresh(self) -> None:
        pass

    def keypad(self, flag: bool) -> None:
        self.keypad_enabled = flag

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("Out of bounds")
        if x + len(text) > self.width:
            raise curses.error("String exceeds line width")
        if y == self.height - 1 and x + len(text) == self.width:
            raise curses.error("Bottom-right corner write")
        self.strings.append((y, x, text, attr))

    def move(self, y: int, x: int) -> None:
        self.cursor_pos = (y, x)


def test_is_interactive_terminal_true() -> None:
    """is_interactive_terminal returns True when both stdout and stdin are tty and TERM is valid."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is True


def test_is_interactive_terminal_false_when_non_interactive_env() -> None:
    """is_interactive_terminal returns False when IMEDNET_NON_INTERACTIVE=1."""
    with (
        patch.dict(
            "os.environ",
            {"IMEDNET_NON_INTERACTIVE": "1", "TERM": "xterm-256color"},
            clear=True,
        ),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_is_interactive_terminal_false_when_term_dumb() -> None:
    """is_interactive_terminal returns False when TERM is dumb or missing."""
    with (
        patch.dict("os.environ", {"TERM": "dumb"}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_is_interactive_terminal_false_when_piped_stdout() -> None:
    """is_interactive_terminal returns False when stdout is redirected / piped."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", return_value=False),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_safe_addstr_bounds() -> None:
    """safe_addstr clips out-of-bound strings safely without error."""
    win = MockWindow(height=10, width=20)
    # Beyond vertical bound
    safe_addstr(win, 15, 0, "Test")
    # Beyond horizontal bound
    safe_addstr(win, 0, 25, "Test")
    # Truncated string at edge
    safe_addstr(win, 2, 15, "0123456789")
    rendered = [s for s in win.strings if s[0] == 2]
    assert len(rendered) == 1
    assert rendered[0][2] == "01234"


def test_extract_item_details_dict() -> None:
    """extract_item_details extracts metadata and form variables from dictionary."""
    item = {
        "record_id": 101,
        "subject_key": "SUBJ-1",
        "form_key": "DM",
        "record_status": "Completed",
        "record_data": {"age": 42, "gender": "F", "vitals": {"bp": "120/80"}},
    }
    meta, variables = extract_item_details(item)
    assert meta["record_id"] == 101
    assert meta["subject_key"] == "SUBJ-1"
    assert variables["age"] == 42
    assert variables["gender"] == "F"
    assert variables["vitals.bp"] == "120/80"


def test_extract_item_details_record_model() -> None:
    """extract_item_details handles Record pydantic models."""
    record = Record.model_construct(
        record_id=202,
        subject_key="SUBJ-2",
        form_key="AE",
        record_status="Active",
        record_data={"aeTerm": "Headache", "severity": "Mild"},
    )
    meta, variables = extract_item_details(record)
    assert meta["record_id"] == 202
    assert meta["subject_key"] == "SUBJ-2"
    assert variables["aeTerm"] == "Headache"
    assert variables["severity"] == "Mild"


def test_extract_item_details_subject_model() -> None:
    """extract_item_details handles Subject pydantic models."""
    subject = Subject.model_construct(
        subject_key="SUBJ-99",
        subject_status="Screened",
        site_name="Site 101",
        keywords=[SubjectKeyword.model_construct(keyword_name="Cohort A")],
    )
    meta, variables = extract_item_details(subject)
    assert meta["subject_key"] == "SUBJ-99"
    assert meta["subject_status"] == "Screened"
    assert meta["site_name"] == "Site 101"
    assert variables == {}


def test_grid_initialization_and_drawing() -> None:
    """InteractiveDataGrid renders table headers, status indicators, and rows."""
    win = MockWindow(height=20, width=80)
    items = [
        {"ID": 1, "Subject": "S1", "Form": "DM", "Status": "Completed", "Created": "2024-01-01"},
        {"ID": 2, "Subject": "S2", "Form": "AE", "Status": "Pending", "Created": "2024-01-02"},
    ]
    grid = InteractiveDataGrid(win, items=items, fields=["ID", "Subject", "Form", "Status", "Created"])
    grid.draw()

    text_content = " ".join(s[2] for s in win.strings)
    assert "iMednet Records Review" in text_content
    assert "Subject" in text_content
    assert "S1" in text_content
    assert "S2" in text_content
    assert "Row 1/2" in text_content


def test_grid_navigation_keys() -> None:
    """InteractiveDataGrid navigates selection with arrows, page keys, and home/end."""
    win = MockWindow(height=10, width=80)
    items = [
        {"ID": i, "Subject": f"S{i}", "Status": "Active"} for i in range(10)
    ]
    grid = InteractiveDataGrid(win, items=items, fields=["ID", "Subject", "Status"])
    assert grid.selected_idx == 0

    # Down arrow
    grid._handle_normal_input(curses.KEY_DOWN)
    assert grid.selected_idx == 1

    # 'j' key
    grid._handle_normal_input(ord("j"))
    assert grid.selected_idx == 2

    # Up arrow
    grid._handle_normal_input(curses.KEY_UP)
    assert grid.selected_idx == 1

    # 'k' key
    grid._handle_normal_input(ord("k"))
    assert grid.selected_idx == 0

    # Cannot go above 0
    grid._handle_normal_input(curses.KEY_UP)
    assert grid.selected_idx == 0

    # Page Down
    grid._handle_normal_input(curses.KEY_NPAGE)
    assert grid.selected_idx > 0

    # End key
    grid._handle_normal_input(curses.KEY_END)
    assert grid.selected_idx == 9

    # Home key
    grid._handle_normal_input(curses.KEY_HOME)
    assert grid.selected_idx == 0


def test_grid_side_panel_toggle_and_inspection() -> None:
    """Pressing space opens the side panel displaying form variables."""
    win = MockWindow(height=20, width=100)
    items = [
        {"ID": 100, "Subject": "SUBJ-A", "Form": "VS", "Status": "Completed"},
    ]
    details = [
        Record.model_construct(
            record_id=100,
            subject_key="SUBJ-A",
            form_key="VS",
            record_status="Completed",
            record_data={"PULSE": 72, "TEMP": 98.6},
        )
    ]
    grid = InteractiveDataGrid(
        win, items=items, fields=["ID", "Subject", "Form", "Status"], details=details
    )

    # Initially closed
    assert grid.side_panel_open is False
    grid.draw()
    text_content = " ".join(s[2] for s in win.strings)
    assert "RECORD INSPECTION" not in text_content

    # Toggle open with Space
    grid._handle_normal_input(ord(" "))
    assert grid.side_panel_open is True
    grid.draw()
    text_open = " ".join(s[2] for s in win.strings)
    assert "RECORD INSPECTION" in text_open
    assert "PULSE" in text_open
    assert "72" in text_open
    assert "TEMP" in text_open

    # Toggle closed with Space
    grid._handle_normal_input(ord(" "))
    assert grid.side_panel_open is False


def test_grid_instant_search_filtering() -> None:
    """Pressing '/' activates real-time filter prompt and updates visible rows immediately."""
    win = MockWindow(height=20, width=80)
    items = [
        {"ID": 1, "Subject": "SUBJ-ALPHA", "Form": "DM"},
        {"ID": 2, "Subject": "SUBJ-BETA", "Form": "VS"},
        {"ID": 3, "Subject": "SUBJ-GAMMA", "Form": "AE"},
    ]
    details = [
        {"record_data": {"note": "baseline"}},
        {"record_data": {"note": "followup"}},
        {"record_data": {"note": "adverse event observed"}},
    ]
    grid = InteractiveDataGrid(
        win, items=items, fields=["ID", "Subject", "Form"], details=details
    )

    # Press '/' to open search
    grid._handle_normal_input(ord("/"))
    assert grid.is_searching is True

    # Type 'beta'
    for char in "beta":
        grid._handle_search_input(ord(char))
    assert grid.search_query == "beta"
    assert len(grid.filtered_entries) == 1
    assert grid.filtered_entries[0][0]["Subject"] == "SUBJ-BETA"

    # Backspace twice -> 'be'
    grid._handle_search_input(curses.KEY_BACKSPACE)
    grid._handle_search_input(curses.KEY_BACKSPACE)
    assert grid.search_query == "be"
    assert len(grid.filtered_entries) == 1

    # Enter commits search
    grid._handle_search_input(10)
    assert grid.is_searching is False
    assert grid.search_query == "be"

    # Search by variable content in details: press '/' and type 'adverse'
    grid._handle_normal_input(ord("/"))
    grid.search_query = ""
    for char in "adverse":
        grid._handle_search_input(ord(char))
    assert len(grid.filtered_entries) == 1
    assert grid.filtered_entries[0][0]["Subject"] == "SUBJ-GAMMA"

    # Escape in normal mode clears filter
    grid._handle_search_input(10)  # exit search mode
    grid._handle_normal_input(27)  # clear filter
    assert grid.search_query == ""
    assert len(grid.filtered_entries) == 3


def test_grid_status_colors_high_contrast() -> None:
    """Grid uses high-contrast color mappings when requested."""
    win = MockWindow()
    items = [{"ID": 1, "Status": "Completed"}]
    grid_normal = InteractiveDataGrid(win, items=items, fields=["ID", "Status"], is_high_contrast=False)
    grid_normal.color_attrs = {"green": 10, "blue": 20}
    assert grid_normal._get_status_attr("Completed") == 10

    grid_hc = InteractiveDataGrid(win, items=items, fields=["ID", "Status"], is_high_contrast=True)
    grid_hc.color_attrs = {"green": 10, "blue": 20}
    assert grid_hc._get_status_attr("Completed") == 20


def test_grid_exit_on_q() -> None:
    """Pressing 'q' or 'Q' signals exit."""
    win = MockWindow()
    items = [{"ID": 1}]
    grid = InteractiveDataGrid(win, items=items, fields=["ID"])
    assert grid._handle_normal_input(ord("q")) is True
    assert grid._handle_normal_input(ord("Q")) is True


def test_run_interactive_grid_wrapper() -> None:
    """run_interactive_grid wraps grid in curses.wrapper and handles curses.error gracefully."""
    items = [{"ID": 1, "Subject": "S1"}]
    with patch("curses.wrapper") as mock_wrapper:
        assert run_interactive_grid(items=items, fields=["ID", "Subject"]) is True
        mock_wrapper.assert_called_once()

    # When curses.wrapper raises curses.error
    with patch("curses.wrapper", side_effect=curses.error("Setup failed")):
        assert run_interactive_grid(items=items, fields=["ID", "Subject"]) is False


def test_display_list_interactive_mode() -> None:
    """display_list launches interactive grid when interactive=True."""
    items = [{"ID": 1, "Subject": "S1"}]
    with patch("imednet.cli.utils.grid.run_interactive_grid", return_value=True) as mock_run:
        display_list(items, "records", interactive=True)
        mock_run.assert_called_once()


def test_display_list_non_interactive_fallback(capfd: pytest.CaptureFixture[str]) -> None:
    """display_list emits standard table without launching curses when interactive=False."""
    items = [{"ID": 1, "Subject": "S1"}]
    with patch("imednet.cli.utils.grid.run_interactive_grid") as mock_run:
        display_list(items, "records", interactive=False)
        mock_run.assert_not_called()
        captured = capfd.readouterr()
        assert "Found 1 records:" in captured.out
        assert "S1" in captured.out


def test_records_list_cli_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoking 'records list' in interactive terminal invokes run_interactive_grid with full details."""
    import imednet.cli as cli

    rec = Record.model_construct(
        record_id=42,
        subject_key="S-42",
        form_key="DM",
        record_status="Active",
        record_data={"AGE": 55},
    )

    mock_sdk = MagicMock()
    mock_sdk.records.list.return_value = [rec]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: mock_sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    with patch("imednet.cli.utils.grid.run_interactive_grid", return_value=True) as mock_grid:
        cli.app(["records", "list", "STUDY"])
        mock_grid.assert_called_once()
        _, kwargs = mock_grid.call_args
        assert kwargs["label"] == "records"
        assert kwargs["details"] == [rec]
        assert kwargs["items"][0]["ID"] == 42


def test_subjects_list_cli_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoking 'subjects list' in interactive terminal invokes run_interactive_grid with full details."""
    import imednet.cli as cli

    subj = Subject.model_construct(
        subject_key="S-99",
        subject_status="Screened",
        site_name="Site 1",
        keywords=[],
    )

    mock_sdk = MagicMock()
    mock_sdk.subjects.list.return_value = [subj]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: mock_sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    with patch("imednet.cli.utils.grid.run_interactive_grid", return_value=True) as mock_grid:
        cli.app(["subjects", "list", "STUDY"])
        mock_grid.assert_called_once()
        _, kwargs = mock_grid.call_args
        assert kwargs["label"] == "subjects"
        assert kwargs["details"] == [subj]
        assert kwargs["items"][0]["subject_key"] == "S-99"

