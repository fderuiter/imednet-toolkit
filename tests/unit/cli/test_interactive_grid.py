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
    grid = InteractiveDataGrid(
        win, items=items, fields=["ID", "Subject", "Form", "Status", "Created"]
    )
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
    items = [{"ID": i, "Subject": f"S{i}", "Status": "Active"} for i in range(10)]
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
    grid = InteractiveDataGrid(win, items=items, fields=["ID", "Subject", "Form"], details=details)

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
    grid_normal = InteractiveDataGrid(
        win, items=items, fields=["ID", "Status"], is_high_contrast=False
    )
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
    from imednet import cli

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
    from imednet import cli

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


def test_grid_unavailable_when_curses_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When curses is None, is_interactive_terminal and run_interactive_grid return False."""
    import imednet.cli.utils.grid as grid_mod

    monkeypatch.setattr(grid_mod, "curses", None)
    assert grid_mod.is_interactive_terminal() is False
    assert grid_mod.run_interactive_grid([], []) is False
    # safe_addstr is a no-op
    grid_mod.safe_addstr(MockWindow(), 0, 0, "test")
    # InteractiveDataGrid raises RuntimeError
    with pytest.raises(RuntimeError, match="curses module, which is unavailable"):
        grid_mod.InteractiveDataGrid(MockWindow(), items=[], fields=[])


def test_cli_help_without_curses(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --help succeeds without error even when curses is unavailable."""
    import imednet.cli.utils.grid as grid_mod
    from imednet import cli

    monkeypatch.setattr(grid_mod, "curses", None)
    with pytest.raises(SystemExit) as exc_info:
        cli.app(["--help"])
    assert exc_info.value.code == 0


def test_display_list_fallback_when_curses_none(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """display_list falls back to plain table when curses is unavailable, even in a TTY."""
    import imednet.cli.utils.grid as grid_mod

    monkeypatch.setattr(grid_mod, "curses", None)
    with (
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
        patch.dict("os.environ", {"TERM": "xterm-256color"}),
    ):
        items = [{"ID": 10, "Status": "Active"}]
        display_list(items, "records")
        captured = capfd.readouterr()
        assert "Found 1 records:" in captured.out
        assert "Active" in captured.out


def test_records_list_cli_fallback_when_curses_none(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """records list falls back to plain table when curses is unavailable."""
    import imednet.cli.utils.grid as grid_mod
    from imednet import cli

    monkeypatch.setattr(grid_mod, "curses", None)
    with (
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
        patch.dict("os.environ", {"TERM": "xterm-256color"}),
    ):
        rec = Record.model_construct(
            record_id=101,
            subject_key="S-101",
            form_key="VS",
            record_status="Active",
            record_data={"SYSBP": 120},
        )
        mock_sdk = MagicMock()
        mock_sdk.records.list.return_value = [rec]
        monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: mock_sdk)

        cli.app(["records", "list", "STUDY"])
        captured = capfd.readouterr()
        assert "Found 1 records:" in captured.out
        assert "S-101" in captured.out


def test_records_list_cli_json_when_curses_none(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """records list with --format json succeeds without curses."""
    import json

    import imednet.cli.utils.grid as grid_mod
    from imednet import cli

    monkeypatch.setattr(grid_mod, "curses", None)
    rec = Record.model_construct(
        record_id=101,
        subject_key="S-101",
        form_key="VS",
        record_status="Active",
        record_data={"SYSBP": 120},
    )
    mock_sdk = MagicMock()
    mock_sdk.records.list.return_value = [rec]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: mock_sdk)

    cli.app(["records", "list", "STUDY", "--format", "json"])
    captured = capfd.readouterr()
    payload = json.loads(captured.out)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "records"
    assert payload["count"] == 1


def test_subjects_list_cli_fallback_when_curses_none(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """subjects list falls back to plain table when curses is unavailable."""
    import imednet.cli.utils.grid as grid_mod
    from imednet import cli

    monkeypatch.setattr(grid_mod, "curses", None)
    with (
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
        patch.dict("os.environ", {"TERM": "xterm-256color"}),
    ):
        subj = Subject.model_construct(
            subject_key="S-202",
            subject_status="Active",
            site_name="Site A",
            keywords=[],
        )
        mock_sdk = MagicMock()
        mock_sdk.subjects.list.return_value = [subj]
        monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: mock_sdk)

        cli.app(["subjects", "list", "STUDY"])
        captured = capfd.readouterr()
        assert "Found 1 subjects:" in captured.out
        assert "S-202" in captured.out


def test_subjects_list_cli_json_when_curses_none(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """subjects list with --format json succeeds without curses."""
    import json

    import imednet.cli.utils.grid as grid_mod
    from imednet import cli

    monkeypatch.setattr(grid_mod, "curses", None)
    subj = Subject.model_construct(
        subject_key="S-202",
        subject_status="Active",
        site_name="Site A",
        keywords=[],
    )
    mock_sdk = MagicMock()
    mock_sdk.subjects.list.return_value = [subj]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: mock_sdk)

    cli.app(["subjects", "list", "STUDY", "--format", "json"])
    captured = capfd.readouterr()
    payload = json.loads(captured.out)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "subjects"
    assert payload["count"] == 1


def test_display_list_defaults_details_to_full_items() -> None:
    """display_list forwards original items as details when details=None so non-summary attributes survive."""
    from imednet.models.forms import Form

    form = Form.model_construct(
        study_key="ST-1",
        form_id=10,
        form_key="DM",
        form_name="Demographics",
        form_type="Standard",
        disabled=False,
        epro_form=False,
        revision=0,
    )
    with patch("imednet.cli.utils.grid.run_interactive_grid", return_value=True) as mock_run:
        display_list(
            [form],
            "forms",
            fields=["form_id", "form_key"],
            interactive=True,
        )
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["items"] == [{"form_id": 10, "form_key": "DM"}]
        assert kwargs["details"] == [form]


@pytest.mark.parametrize(
    ("argv", "sdk_setup", "expected_label"),
    [
        (["studies", "list"], "studies", "studies"),
        (["sites", "list", "STUDY"], "sites", "sites"),
        (["forms", "list", "STUDY"], "forms", "forms"),
        (["intervals", "list", "STUDY"], "intervals", "intervals"),
        (["variables", "list", "STUDY"], "variables", "variables"),
        (["visits", "list", "STUDY"], "visits", "visits"),
        (["queries", "list", "STUDY"], "queries", "queries"),
        (["record-revisions", "list", "STUDY"], "record_revisions", "record revisions"),
        (["codings", "list", "STUDY"], "codings", "codings"),
        (["users", "list", "STUDY"], "users", "users"),
    ],
)
def test_all_cli_list_commands_forward_full_details(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    sdk_setup: str,
    expected_label: str,
) -> None:
    """Every CLI list command forwards full SDK domain models via details to run_interactive_grid."""
    from imednet import cli
    from imednet.models.studies import Study

    sentinel_model = Study.model_construct(
        study_key="STUDY",
        study_id=99,
        study_name=" Oncology Trial ",
        study_description="Hidden non-summary attribute",
        study_type="Phase II",
        sponsor_key="SPONSOR",
    )
    mock_sdk = MagicMock()
    getattr(mock_sdk, sdk_setup).list.return_value = [sentinel_model]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: mock_sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    with patch("imednet.cli.utils.grid.run_interactive_grid", return_value=True) as mock_grid:
        cli.app(argv)
        mock_grid.assert_called_once()
        _, kwargs = mock_grid.call_args
        assert kwargs["label"] == expected_label
        assert kwargs["details"] == [sentinel_model]


@pytest.mark.parametrize("subcmd", ["status", "wait"])
def test_jobs_cli_commands_forward_full_details(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    subcmd: str,
) -> None:
    """jobs status and jobs wait forward details=[job] with interactive=False static output."""
    from imednet import cli
    from imednet.models.jobs import JobState, JobStatus

    job = JobStatus(
        batch_id="BATCH-1",
        job_id="JOB-1",
        state=JobState.COMPLETED,
        progress=0,
        result_url="https://example.test/results/1",
        results={"processed": 0, "errors": []},
    )
    mock_sdk = MagicMock()
    mock_sdk.get_job.return_value = job
    mock_sdk.poll_job.return_value = job
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: mock_sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    with (
        patch("imednet.cli.jobs.display_list", wraps=display_list) as spy_display,
        patch("imednet.cli.utils.grid.run_interactive_grid", return_value=True) as mock_grid,
    ):
        cli.app(["jobs", subcmd, "STUDY", "BATCH-1"])
        spy_display.assert_called_once()
        args, kwargs = spy_display.call_args
        assert args[1] == "jobs"
        assert kwargs["details"] == [job]
        assert kwargs["interactive"] is False
        mock_grid.assert_not_called()
        captured = capfd.readouterr()
        assert "BATCH-1" in captured.out


def test_enum_unwrapping_and_nested_search_indexing() -> None:
    """_format_cell_value unwraps Enum.value and filter_entries indexes nested collections and enums."""
    from imednet.cli.utils.output import _format_cell_value
    from imednet.models.queries import Query, QueryComment, QueryStatus
    from imednet.models.users import Role, User

    assert _format_cell_value(QueryStatus.OPEN) == "OPEN"
    assert _format_cell_value([QueryStatus.OPEN, QueryStatus.CLOSED]) == "OPEN, CLOSED"

    win = MockWindow(height=20, width=100)
    q1 = Query.model_construct(
        annotation_id=1,
        description="Systolic check",
        subject_key="S-1",
        status=QueryStatus.OPEN,
        query_comments=[
            QueryComment.model_construct(
                sequence=0,
                closed=False,
                user="monitor_jane",
                comment="Verify cuff size",
            )
        ],
    )
    q2 = Query.model_construct(
        annotation_id=2,
        description="Lab date",
        subject_key="S-2",
        status=QueryStatus.ANSWERED,
        query_comments=[],
    )
    items = [
        {"description": "Systolic check", "subject_key": "S-1"},
        {"description": "Lab date", "subject_key": "S-2"},
    ]
    grid = InteractiveDataGrid(
        win,
        items=items,
        fields=["description", "subject_key"],
        label="queries",
        details=[q1, q2],
    )

    # Search nested QueryComment text
    grid.search_query = "cuff size"
    grid.filter_entries()
    assert len(grid.filtered_entries) == 1
    assert grid.filtered_entries[0][0]["subject_key"] == "S-1"

    # Search unwrapped Enum status value
    grid.search_query = "answered"
    grid.filter_entries()
    assert len(grid.filtered_entries) == 1
    assert grid.filtered_entries[0][0]["subject_key"] == "S-2"

    # Search nested Role inside User
    user = User.model_construct(
        user_id="U-1",
        login="asmith",
        first_name="Alice",
        last_name="Smith",
        email="alice@example.com",
        user_active_in_study=False,
        roles=[
            Role.model_construct(role_id="R-9", name="Biostatistician", level=0, inactive=False)
        ],
    )
    user_grid = InteractiveDataGrid(
        win,
        items=[{"user_id": "U-1", "login": "asmith"}],
        fields=["user_id", "login"],
        label="users",
        details=[user],
    )
    user_grid.search_query = "biostatistician"
    user_grid.filter_entries()
    assert len(user_grid.filtered_entries) == 1


def test_side_panel_renders_falsy_and_nested_collections_with_entity_headers() -> None:
    """_draw_side_panel preserves False, 0, list, and dict values and shows entity-specific headers."""
    from imednet.models.jobs import JobState, JobStatus
    from imednet.models.users import Role, User

    win = MockWindow(height=28, width=100)
    user = User.model_construct(
        user_id="U-7",
        login="inactive_reviewer",
        first_name="Bob",
        last_name="Jones",
        email="bob@example.com",
        user_active_in_study=False,
        roles=[Role.model_construct(role_id="R-0", name="Data Manager", level=0, inactive=False)],
    )
    grid = InteractiveDataGrid(
        win,
        items=[{"user_id": "U-7", "login": "inactive_reviewer"}],
        fields=["user_id", "login"],
        label="users",
        details=[user],
    )
    grid._handle_normal_input(ord(" "))
    grid.draw()

    panel_text = " ".join(s[2] for s in win.strings)
    assert "USERS DETAILS" in panel_text
    assert "USERS METADATA" in panel_text
    assert "FORM VARIABLES" not in panel_text
    assert "User Active In Study: No" in panel_text
    assert "Data Manager" in panel_text
    assert "level: 0" in panel_text
    assert "inactive: No" in panel_text

    # Verify dict and 0 progress on JobStatus
    job = JobStatus(
        batch_id="B-0",
        job_id="J-0",
        state=JobState.PENDING,
        progress=0,
        results={"updated_count": 0, "retryable": False, "warnings": []},
    )
    job_grid = InteractiveDataGrid(
        win,
        items=[{"batch_id": "B-0", "state": JobState.PENDING, "progress": 0}],
        fields=["batch_id", "state", "progress"],
        label="jobs",
        details=[job],
    )
    job_grid._handle_normal_input(ord(" "))
    job_grid.draw()
    job_panel_text = " ".join(s[2] for s in win.strings)
    assert "JOBS DETAILS" in job_panel_text
    assert "JOBS METADATA" in job_panel_text
    assert "State: PENDING" in job_panel_text
    assert "Progress: 0" in job_panel_text
    assert "updated_count: 0" in job_panel_text
    assert "retryable: No" in job_panel_text
    assert "warnings: []" in job_panel_text


def test_horizontal_scrolling_and_literal_search_keys() -> None:
    """KEY_LEFT, KEY_RIGHT, h, and l scroll columns in normal mode while / search keeps h and l literal."""
    from imednet.models.record_revisions import RecordRevision

    rev = RecordRevision.model_construct(
        study_key="STUDY-A",
        record_revision_id=5001,
        record_id=101,
        record_revision=0,
        data_revision=0,
        record_status="Active",
        subject_id=11,
        subject_key="SUBJ-001",
        site_id=2,
        form_key="VITALS",
        interval_id=3,
        deleted=False,
        date_created="2025-01-01",
        reason_for_change="Initial entry",
        record_oid="OID-101",
        role="Investigator",
        subject_oid="SOID-1",
        user="dr_house",
        last_updated="2025-01-02",
    )
    row = rev.model_dump()
    fields = list(row.keys())
    assert len(fields) == 19

    win = MockWindow(height=24, width=80)
    grid = InteractiveDataGrid(
        win,
        items=[row],
        fields=fields,
        label="record revisions",
        details=[rev],
    )
    grid.draw()

    # Initial offset shows full readable headers, never 4-char crushed stubs
    header_row = "".join(s[2] for s in win.strings if s[0] == 1)
    assert "Study Key" in header_row
    assert "Record Revision Id" in header_row
    assert "Last Updated" not in header_row

    # Scroll right with KEY_RIGHT and 'l'
    grid._handle_normal_input(curses.KEY_RIGHT)
    assert grid.col_offset == 1
    grid._handle_normal_input(ord("l"))
    assert grid.col_offset == 2

    # Scroll left with 'h' and KEY_LEFT
    grid._handle_normal_input(ord("h"))
    assert grid.col_offset == 1
    grid._handle_normal_input(curses.KEY_LEFT)
    assert grid.col_offset == 0
    grid._handle_normal_input(curses.KEY_LEFT)
    assert grid.col_offset == 0

    # Entering '/' search keeps 'h' and 'l' literal without scrolling columns
    grid._handle_normal_input(curses.KEY_RIGHT)
    assert grid.col_offset == 1
    grid._handle_normal_input(ord("/"))
    for ch in "hl":
        grid._handle_search_input(ord(ch))
    assert grid.search_query == "hl"
    assert grid.col_offset == 1
    grid._handle_search_input(10)  # Exit search
    grid._handle_normal_input(27)  # Clear filter

    # Scroll all the way to the rightmost attributes
    for _ in range(30):
        grid._handle_normal_input(curses.KEY_RIGHT)
    grid.draw()
    right_headers = "".join(s[2] for s in win.strings if s[0] == 1)
    assert "Last Updated" in right_headers
    assert "User" in right_headers


def test_record_revision_80_col_side_panel_strict_clipping_and_clamping() -> None:
    """19-attribute RecordRevision on 80-col terminal clips strictly before panel_x and clamps col_offset."""
    from imednet.models.record_revisions import RecordRevision

    rev = RecordRevision.model_construct(
        study_key="STUDY-LONG-KEY",
        record_revision_id=9001,
        record_id=202,
        record_revision=0,
        data_revision=0,
        record_status="Completed",
        subject_id=44,
        subject_key="SUBJ-044",
        site_id=5,
        form_key="AE_LOG",
        interval_id=0,
        deleted=False,
        date_created="2025-02-01",
        reason_for_change="Verified source",
        record_oid="REC-OID-202",
        role="Coordinator",
        subject_oid="SUBJ-OID-044",
        user="coord_lee",
        last_updated="2025-02-02",
    )
    row = rev.model_dump()
    fields = list(row.keys())

    win = MockWindow(height=24, width=80)
    grid = InteractiveDataGrid(
        win,
        items=[row],
        fields=fields,
        label="record revisions",
        details=[rev],
    )

    # Open side panel with Space on 80-col terminal (panel_x = int(80 * 0.55) = 44)
    grid.col_offset = 99  # Out-of-bounds offset should clamp when Space opens
    grid._handle_normal_input(ord(" "))
    assert grid.side_panel_open is True
    assert 0 <= grid.col_offset < len(fields)

    # Reset to col_offset 0 and draw to verify strict clipping before panel_x=44
    grid.col_offset = 0
    grid.draw()

    panel_x = int(80 * 0.55)
    assert panel_x == 44

    # Every write starting in the grid region (x < panel_x, rows 0..height-2) must end strictly before panel_x
    for y, x, text, _ in win.strings:
        assert x + len(text) <= 80
        if y < win.height - 1 and x < panel_x:
            assert x + len(text) < panel_x, (
                f"Grid write on row {y} bled into panel_x={panel_x}: x={x}, text={text!r}"
            )

    # Divider column is intact across all viewport rows
    dividers = [s for s in win.strings if 1 <= s[0] < win.height - 1 and s[1] == panel_x]
    assert len(dividers) == win.height - 2
    assert all(s[2].startswith("│") for s in dividers)

    # Side panel displays RecordRevision details including falsy 0 and False attributes
    panel_strings = " ".join(s[2] for s in win.strings if s[1] >= panel_x)
    assert "RECORD REVISIONS DETAILS" in panel_strings
    assert "Record Revision: 0" in panel_strings
    assert "Data Revision: 0" in panel_strings
    assert "Deleted: No" in panel_strings

    # Scroll to max offset with side panel open, then widen terminal and verify clamping on resize
    for _ in range(30):
        grid._handle_normal_input(ord("l"))
    narrow_offset = grid.col_offset
    assert narrow_offset > 0

    win.width = 400  # Partially wider viewport clamps col_offset down
    grid.draw()
    assert 0 < grid.col_offset < narrow_offset

    win.width = 500  # Wide enough for all 19 columns to fit in 55% grid pane
    grid.draw()
    assert grid.col_offset == 0


def test_narrow_24_char_viewport_clips_wide_single_attribute_before_panel_x() -> None:
    """Narrow ~24-char grid viewports clip single wide attributes cleanly without bleeding into panel_x."""
    # At width=50 with side panel open, grid_width = max(24, int(50 * 0.55)) = 27, grid_limit = 26
    win_split = MockWindow(height=15, width=50)
    wide_val = "VERY_WIDE_CLINICAL_ATTRIBUTE_VALUE_EXCEEDING_FIFTY_CHARS"
    items = [{"very_long_attribute_header_name": wide_val}]
    grid_split = InteractiveDataGrid(
        win_split,
        items=items,
        fields=["very_long_attribute_header_name"],
        label="records",
    )
    grid_split._handle_normal_input(ord(" "))
    grid_split.draw()

    panel_x = max(24, int(50 * 0.55))
    for y, x, text, _ in win_split.strings:
        if y < win_split.height - 1 and x < panel_x:
            assert x + len(text) < panel_x

    # At exact minimum terminal width=24 without side panel, single wide attribute clips cleanly to 24 chars
    win_24 = MockWindow(height=12, width=24)
    grid_24 = InteractiveDataGrid(
        win_24,
        items=items,
        fields=["very_long_attribute_header_name"],
        label="records",
    )
    grid_24.draw()
    for _y, x, text, _ in win_24.strings:
        assert x + len(text) <= 24
