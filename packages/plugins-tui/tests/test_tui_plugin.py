"""Unit tests for the imednet-tui plugin."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from textual.app import App
from textual.containers import Container
from textual.widgets import ListItem, ListView

from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet_tui.app import (
    DashboardScreen,
    ImednetTuiApp,
    JobMonitor,
    SiteList,
    StudyList,
    SubjectTable,
    TextualLogHandler,
    is_interactive_terminal,
    run_tui,
)
from imednet_tui.cli import setup_parser


@pytest.fixture(autouse=True)
def reset_high_contrast_env(monkeypatch):
    """Prevent IMEDNET_HIGH_CONTRAST state leaks across tests."""
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)


@pytest.fixture
def mock_sdk():
    """Return a mocked ImednetSDK with async methods."""
    sdk = MagicMock()
    sdk.config = MagicMock()
    sdk.config.base_url = "https://edc.test.imednetapi.com"

    # Mock studies
    mock_study = MagicMock()
    mock_study.study_name = "Oncology Phase III"
    mock_study.study_key = "STUDY-ONC-01"
    sdk.studies.async_list = AsyncMock(return_value=[mock_study])

    # Mock sites
    mock_site = MagicMock()
    mock_site.site_id = "SITE-101"
    mock_site.site_name = "Memorial Hospital"
    sdk.sites.async_list = AsyncMock(return_value=[mock_site])

    # Mock subjects
    mock_subj1 = MagicMock()
    mock_subj1.subject_id = "SUBJ-001"
    mock_subj1.site_id = "SITE-101"
    mock_subj1.subject_status = "Enrolled"
    mock_subj1.date_created = "2026-01-10T08:00:00Z"

    mock_subj2 = MagicMock()
    mock_subj2.subject_id = "SUBJ-002"
    mock_subj2.site_id = "SITE-102"
    mock_subj2.subject_status = "Screened"
    mock_subj2.date_created = "2026-01-11T09:00:00Z"

    sdk.subjects.async_list = AsyncMock(return_value=[mock_subj1, mock_subj2])

    # Mock jobs
    mock_job = MagicMock()
    mock_job.job_type = "ExportData"
    mock_job.state = "Completed"
    mock_job.date_created = "2026-01-12T10:00:00Z"
    sdk.jobs.async_list = AsyncMock(return_value=[mock_job])

    return sdk


def test_app_instantiation(mock_sdk):
    """Test that ImednetTuiApp instantiates and inherits from Textual App."""
    app = ImednetTuiApp(mock_sdk)
    assert app.sdk == mock_sdk
    assert isinstance(app, App)


def test_high_contrast_mode_detection(mock_sdk, monkeypatch):
    """Test that app honors IMEDNET_HIGH_CONTRAST environment variable."""
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    app = ImednetTuiApp(mock_sdk)
    assert app.high_contrast_enabled is True

    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    app2 = ImednetTuiApp(mock_sdk)
    assert app2.high_contrast_enabled is False


def test_toggle_high_contrast_action(mock_sdk, monkeypatch):
    """Test toggling high contrast dynamically."""
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    app = ImednetTuiApp(mock_sdk)
    assert app.high_contrast_enabled is False

    app.action_toggle_high_contrast()
    assert app.high_contrast_enabled is True
    assert os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"

    app.action_toggle_high_contrast()
    assert app.high_contrast_enabled is False
    assert "IMEDNET_HIGH_CONTRAST" not in os.environ


@pytest.mark.asyncio
async def test_app_study_and_subject_flow(mock_sdk):
    """Test interactive dashboard study selection, site loading, and subject display."""
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        assert len(study_list.children) == 1

        # Simulate selecting a study
        study_list.post_message(StudyList.StudySelected("STUDY-ONC-01", "Oncology Phase III"))
        await pilot.pause()

        site_list = app.screen.query_one(SiteList)
        assert len(site_list.children) == 1

        subject_table = app.screen.query_one(SubjectTable)
        assert subject_table.row_count == 2

        job_monitor = app.screen.query_one(JobMonitor)
        assert job_monitor.current_study_key == "STUDY-ONC-01"

        # Simulate selecting a site to filter subjects
        site_list.post_message(SiteList.SiteSelected("SITE-101", "Memorial Hospital"))
        await pilot.pause()

        assert subject_table.row_count == 1

        # Trigger refresh action
        app.action_refresh_data()
        await pilot.pause()
        assert mock_sdk.studies.async_list.await_count >= 2
        assert mock_sdk.sites.async_list.await_count >= 2
        assert mock_sdk.subjects.async_list.await_count >= 3
        assert mock_sdk.jobs.async_list.await_count >= 2


@pytest.mark.asyncio
async def test_app_list_view_selection_events(mock_sdk):
    """Test on_list_view_selected event handlers for StudyList and SiteList."""
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        item = study_list.children[0]
        assert isinstance(item, ListItem)

        # Trigger on_list_view_selected
        event = ListView.Selected(study_list, item, 0)
        study_list.on_list_view_selected(event)
        await pilot.pause()

        site_list = app.screen.query_one(SiteList)
        site_item = site_list.children[0]
        site_event = ListView.Selected(site_list, site_item, 0)
        site_list.on_list_view_selected(site_event)
        await pilot.pause()


@pytest.mark.asyncio
async def test_app_sync_sdk_fallback():
    """Test that app executes successfully with synchronous SDK methods."""
    sync_sdk = MagicMock()
    del sync_sdk.studies.async_list
    del sync_sdk.sites.async_list
    del sync_sdk.subjects.async_list
    del sync_sdk.jobs.async_list
    del sync_sdk.jobs.async_get

    mock_study = MagicMock()
    mock_study.study_name = None
    mock_study.name = "Sync Study"
    mock_study.study_key = "SYNC-01"
    sync_sdk.studies.list = MagicMock(return_value=[mock_study])

    mock_site = MagicMock()
    mock_site.site_id = "SITE-SYNC"
    mock_site.site_name = "Sync Site"
    sync_sdk.sites.list = MagicMock(return_value=[mock_site])

    mock_subj = MagicMock()
    mock_subj.subject_id = "SUB-SYNC"
    mock_subj.site_id = "SITE-SYNC"
    mock_subj.subject_status = "Active"
    mock_subj.date_created = "2026-02-01"
    sync_sdk.subjects.list = MagicMock(return_value=[mock_subj])
    sync_sdk.subjects.list_by_site = MagicMock(return_value=[mock_subj])

    del sync_sdk.jobs.list
    mock_job = MagicMock()
    mock_job.job_id = "SYNC-JOB-1"
    mock_job.job_type = "SyncJob"
    mock_job.state = "Running"
    mock_job.date_created = "2026-02-01"
    sync_sdk.jobs.get = MagicMock(return_value=mock_job)

    app = ImednetTuiApp(sync_sdk)
    async with app.run_test() as pilot:
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        assert len(study_list.children) == 1

        study_list.post_message(StudyList.StudySelected("SYNC-01", "Sync Study"))
        await pilot.pause()

        site_list = app.screen.query_one(SiteList)
        assert len(site_list.children) == 1

        subject_table = app.screen.query_one(SubjectTable)
        assert subject_table.row_count == 1

        job_monitor = app.screen.query_one(JobMonitor)
        assert "enumeration is not supported" in str(job_monitor.render())

        # Track job ID using supported get API
        job_monitor.track_job("SYNC-JOB-1")
        await pilot.pause()
        sync_sdk.jobs.get.assert_called_with("SYNC-01", "SYNC-JOB-1")
        assert "SYNC-JOB-1" in str(job_monitor.render())

        site_list.post_message(SiteList.SiteSelected("SITE-SYNC", "Sync Site"))
        await pilot.pause()
        assert subject_table.row_count == 1


@pytest.mark.asyncio
async def test_app_error_notifications(mock_sdk):
    """Test error handling in sites, subjects, and jobs."""
    mock_sdk.sites.async_list = AsyncMock(side_effect=RuntimeError("Site failure"))
    mock_sdk.subjects.async_list = AsyncMock(side_effect=RuntimeError("Subject failure"))
    mock_sdk.jobs.async_list = AsyncMock(side_effect=RuntimeError("Job failure"))

    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        study_list.post_message(StudyList.StudySelected("STUDY-ONC-01", "Oncology Phase III"))
        await pilot.pause()

        site_list = app.screen.query_one(SiteList)
        assert site_list.loading is False
        subject_table = app.screen.query_one(SubjectTable)
        assert subject_table.loading is False


@pytest.mark.asyncio
async def test_app_handles_study_load_error(mock_sdk):
    """Test that app gracefully handles study listing errors on mount."""
    mock_sdk.studies.async_list = AsyncMock(side_effect=RuntimeError("Connection refused"))
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        assert study_list.loading is False
        assert len(study_list.children) == 0


@pytest.mark.asyncio
async def test_app_high_contrast_class_applied(mock_sdk, monkeypatch):
    """Test that high contrast CSS class and heavy white borders apply to DashboardScreen."""
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "high-contrast" in app.classes
        assert isinstance(app.screen, DashboardScreen)
        assert "high-contrast" in app.screen.classes

        studies_box = app.screen.query_one("#studies-box", Container)
        sites_box = app.screen.query_one("#sites-box", Container)
        subjects_box = app.screen.query_one("#subjects-box", Container)
        assert studies_box.styles.border.top[0] == "heavy"
        assert sites_box.styles.border.top[0] == "heavy"
        assert subjects_box.styles.border.top[0] == "heavy"

        await pilot.press("h")
        await pilot.pause()
        assert "high-contrast" not in app.classes
        assert "high-contrast" not in app.screen.classes
        assert studies_box.styles.border.top[0] == "solid"
        assert sites_box.styles.border.top[0] == "solid"
        assert subjects_box.styles.border.top[0] == "solid"


@pytest.mark.asyncio
async def test_job_monitor_without_study_key(mock_sdk):
    """Test JobMonitor refresh when no study is selected."""
    monitor = JobMonitor(mock_sdk)
    monitor.current_study_key = None
    await monitor.refresh_jobs()


def test_textual_log_handler():
    """Test TextualLogHandler emits formatted log to Log widget and handles errors."""
    mock_log = MagicMock()
    handler = TextualLogHandler(mock_log)
    formatter = logging.Formatter("%(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    record = logging.LogRecord("test", logging.INFO, "test.py", 10, "Test log message", (), None)
    handler.emit(record)

    mock_log.write_line.assert_called_once_with("INFO - Test log message")

    # Test error handling
    mock_log.write_line.side_effect = RuntimeError("Write error")
    with patch.object(handler, "handleError") as mock_handle_error:
        handler.emit(record)
        mock_handle_error.assert_called_once()


def test_run_tui_helper(mock_sdk):
    """Test that run_tui starts the app when terminal is interactive."""
    with (
        patch("imednet_tui.app.is_interactive_terminal", return_value=True),
        patch.object(ImednetTuiApp, "run") as mock_run,
    ):
        run_tui(mock_sdk)
        mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_subject_table_with_real_sdk_subject_instance():
    """Test that SubjectTable correctly renders real SDK Subject model subject_status."""
    from imednet.models.subjects import Subject

    real_subj = Subject(
        subject_id=42,
        site_id=10,
        subject_status="Enrolled",
        date_created="2026-03-01T12:00:00Z",
    )
    sdk = MagicMock()
    sdk.subjects.async_list = AsyncMock(return_value=[real_subj])

    app = ImednetTuiApp(sdk)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one(SubjectTable)
        await table.load_subjects("STUDY-01")
        assert table.row_count == 1
        # Verify row cell contains "Enrolled"
        row_data = [str(cell) for cell in table.get_row_at(0)]
        assert "Enrolled" in row_data
        assert "42" in row_data


@pytest.mark.asyncio
async def test_job_monitor_with_real_jobs_endpoint_contract():
    """Test JobMonitor with real JobsEndpoint contract (no list, get supported)."""
    from imednet.models.jobs import JobState, JobStatus

    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    real_job = JobStatus(
        job_id="BATCH-999",
        state=JobState.COMPLETED,
        date_created="2026-03-01T12:00:00Z",
    )
    sdk.jobs.get = MagicMock(return_value=real_job)

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-REAL"

    # Before tracking any jobs, enumeration is explained as unsupported
    await monitor.refresh_jobs()
    assert "enumeration is not supported" in str(monitor.render())

    # Track specific job ID
    monitor.track_job("BATCH-999")
    await monitor.refresh_jobs()
    sdk.jobs.get.assert_called_with("STUDY-REAL", "BATCH-999")
    assert "BATCH-999" in str(monitor.render())
    assert "COMPLETED" in str(monitor.render())


@pytest.mark.asyncio
async def test_job_monitor_tracked_job_failure_retains_failed_state():
    """Test that a tracked job whose get() raises is retained as FAILED rather than dropped."""
    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = MagicMock(side_effect=RuntimeError("Internal job lookup error"))

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-ERR"
    monitor.track_job("BATCH-ERR-101")
    await monitor.refresh_jobs()

    rendered = str(monitor.render())
    # Crucial assertion: failure is not dropped, so active jobs is (1), not (0)
    assert "Active & Recent Jobs (1)" in rendered
    assert "Active & Recent Jobs (0)" not in rendered
    assert "BATCH-ERR-101" in rendered
    assert "FAILED" in rendered


@pytest.mark.asyncio
async def test_job_monitor_tracked_job_unknown_state():
    """Test that tracked job with unretrievable/None result is displayed as UNKNOWN."""
    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = MagicMock(return_value=None)

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-UNK"
    monitor.track_job("BATCH-UNK-202")
    await monitor.refresh_jobs()

    rendered = str(monitor.render())
    assert "Active & Recent Jobs (1)" in rendered
    assert "BATCH-UNK-202" in rendered
    assert "UNKNOWN" in rendered


@pytest.mark.asyncio
async def test_job_monitor_sync_get_offloaded_to_thread():
    """Test that synchronous JobsEndpoint.get is offloaded to a worker thread."""
    import threading

    from imednet.models.jobs import JobState, JobStatus

    main_thread = threading.current_thread()
    called_in_worker_thread = False

    def sync_get(study_key: str, job_id: str) -> JobStatus:
        nonlocal called_in_worker_thread
        if threading.current_thread() != main_thread:
            called_in_worker_thread = True
        return JobStatus(job_id=job_id, state=JobState.RUNNING)

    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = sync_get

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-ASYNC"
    monitor.track_job("BATCH-THREAD-1")
    await monitor.refresh_jobs()

    assert called_in_worker_thread is True
    rendered = str(monitor.render())
    assert "Active & Recent Jobs (1)" in rendered
    assert "BATCH-THREAD-1" in rendered
    assert "RUNNING" in rendered


@pytest.mark.asyncio
async def test_job_monitor_with_real_jobs_endpoint_instance():
    """Test JobMonitor with an actual JobsEndpoint class instance."""
    from imednet.endpoints.jobs import JobsEndpoint

    mock_client = MagicMock()
    mock_client.get.side_effect = RuntimeError("Connection timed out")
    real_endpoint = JobsEndpoint(mock_client)

    sdk = MagicMock()
    sdk.jobs = real_endpoint

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-REAL-EP"
    monitor.track_job("BATCH-REAL-ERR")
    await monitor.refresh_jobs()

    rendered = str(monitor.render())
    assert "Active & Recent Jobs (1)" in rendered
    assert "FAILED" in rendered
    assert "BATCH-REAL-ERR" in rendered


@pytest.mark.asyncio
async def test_job_monitor_sync_get_responsiveness_regression_heartbeat():
    """Responsiveness regression: synchronous get() does not block event loop heartbeat."""
    import time

    from imednet.models.jobs import JobState, JobStatus

    heartbeat_ticks = 0
    stop_heartbeat = False

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not stop_heartbeat:
            await asyncio.sleep(0.02)
            heartbeat_ticks += 1

    def slow_sync_get(study_key: str, job_id: str) -> JobStatus:
        time.sleep(0.18)  # 180ms synchronous blocking stub (150-200ms)
        return JobStatus(job_id=job_id, state=JobState.COMPLETED)

    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = slow_sync_get

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-HEARTBEAT"
    monitor.track_job("BATCH-HEARTBEAT-1")

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        await monitor.refresh_jobs()
    finally:
        stop_heartbeat = True
        await heartbeat_task

    # During the 180ms synchronous stub, the asyncio event loop must not freeze,
    # allowing the heartbeat task to record multiple ticks.
    assert heartbeat_ticks >= 4
    rendered = str(monitor.render())
    assert "Active & Recent Jobs (1)" in rendered
    assert "COMPLETED" in rendered
    assert "BATCH-HEARTBEAT-1" in rendered


def test_is_interactive_terminal_true():
    """Verify is_interactive_terminal returns True when streams are TTY and TERM is valid."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is True


def test_is_interactive_terminal_false_when_non_interactive_env():
    """Verify is_interactive_terminal returns False when IMEDNET_NON_INTERACTIVE=1."""
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


@pytest.mark.parametrize("term_val", ["dumb", "", "   "])
def test_is_interactive_terminal_false_when_term_dumb_or_empty(term_val):
    """Verify is_interactive_terminal returns False when TERM is dumb, empty, or whitespace."""
    with (
        patch.dict("os.environ", {"TERM": term_val}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_is_interactive_terminal_false_when_non_tty_stdout():
    """Verify is_interactive_terminal returns False when stdout is redirected / non-tty."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", return_value=False),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_is_interactive_terminal_false_when_non_tty_stdin():
    """Verify is_interactive_terminal returns False when stdin is piped / non-tty."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=False),
    ):
        assert is_interactive_terminal() is False


@pytest.mark.parametrize(
    "exc_type", [OSError("Closed"), ValueError("Invalid"), AttributeError("No isatty")]
)
def test_is_interactive_terminal_false_on_stream_exception(exc_type):
    """Verify is_interactive_terminal safely returns False on stream inspection exceptions."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", side_effect=exc_type),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_is_interactive_terminal_false_when_stream_is_none():
    """Verify is_interactive_terminal safely handles None streams."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout", None),
    ):
        assert is_interactive_terminal() is False


def test_run_tui_headless_raises_runtime_error(mock_sdk):
    """Verify run_tui raises RuntimeError when terminal is non-interactive and force is False."""
    with (
        patch("imednet_tui.app.is_interactive_terminal", return_value=False),
        patch.object(ImednetTuiApp, "run") as mock_run,
    ):
        with pytest.raises(RuntimeError, match="Interactive terminal required to run the TUI"):
            run_tui(mock_sdk, force=False)
        mock_run.assert_not_called()


def test_run_tui_force_flag_bypasses_check(mock_sdk):
    """Verify run_tui runs app when force=True even in non-interactive environment."""
    with (
        patch("imednet_tui.app.is_interactive_terminal", return_value=False),
        patch.object(ImednetTuiApp, "run") as mock_run,
    ):
        run_tui(mock_sdk, force=True)
        mock_run.assert_called_once()


def test_cli_run_tui_command_interactive_success(mock_sdk):
    """Verify CLI run_tui_command launches run_tui when terminal is interactive."""
    subparsers = MagicMock()
    mock_parser = MagicMock()
    subparsers.add_parser.return_value = mock_parser

    setup_parser(subparsers)
    command_func = mock_parser.set_defaults.call_args[1]["func"]

    args = argparse.Namespace(high_contrast=False, force=False)
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk),
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
    ):
        command_func(args)
        mock_run_tui.assert_called_once_with(mock_sdk, force=False)


def test_cli_run_tui_command_headless_exits(mock_sdk, capsys):
    """Verify CLI run_tui_command prints error and exits with code 1 in headless/non-tty runs."""
    subparsers = MagicMock()
    mock_parser = MagicMock()
    subparsers.add_parser.return_value = mock_parser

    setup_parser(subparsers)
    command_func = mock_parser.set_defaults.call_args[1]["func"]

    args = argparse.Namespace(high_contrast=False, force=False)
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk) as mock_get_sdk,
        patch("imednet_tui.cli.is_interactive_terminal", return_value=False),
        pytest.raises(SystemExit) as exc_info,
    ):
        command_func(args)

    assert exc_info.value.code == 1
    _, err = capsys.readouterr()
    assert "Error: Interactive terminal required to run the TUI interface." in err
    mock_get_sdk.assert_not_called()


def test_cli_run_tui_command_headless_skips_sdk_initialization(capsys):
    """Verify headless execution emits terminal diagnostic and skips SDK initialization entirely."""
    subparsers = MagicMock()
    mock_parser = MagicMock()
    subparsers.add_parser.return_value = mock_parser

    setup_parser(subparsers)
    command_func = mock_parser.set_defaults.call_args[1]["func"]

    args = argparse.Namespace(high_contrast=False, force=False)
    with (
        patch(
            "imednet.cli.utils.context.get_sdk",
            side_effect=RuntimeError("SDK credentials missing in headless environment"),
        ) as mock_get_sdk,
        patch("imednet_tui.cli.is_interactive_terminal", return_value=False),
        pytest.raises(SystemExit) as exc_info,
    ):
        command_func(args)

    assert exc_info.value.code == 1
    _, err = capsys.readouterr()
    assert "Error: Interactive terminal required to run the TUI interface." in err
    mock_get_sdk.assert_not_called()


def test_cli_run_tui_command_non_interactive_env_exits(mock_sdk, capsys):
    """Verify CLI run_tui_command exits cleanly with code 1 when IMEDNET_NON_INTERACTIVE=1."""
    subparsers = MagicMock()
    mock_parser = MagicMock()
    subparsers.add_parser.return_value = mock_parser

    setup_parser(subparsers)
    command_func = mock_parser.set_defaults.call_args[1]["func"]

    args = argparse.Namespace(high_contrast=False, force=False)
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk) as mock_get_sdk,
        patch.dict(
            "os.environ", {"IMEDNET_NON_INTERACTIVE": "1", "TERM": "xterm-256color"}, clear=True
        ),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
        pytest.raises(SystemExit) as exc_info,
    ):
        command_func(args)

    assert exc_info.value.code == 1
    _, err = capsys.readouterr()
    assert "Interactive terminal required" in err
    mock_get_sdk.assert_not_called()


def test_cli_run_tui_command_term_dumb_or_missing_exits(mock_sdk, capsys):
    """Verify CLI run_tui_command exits cleanly with code 1 when TERM is dumb or missing."""
    subparsers = MagicMock()
    mock_parser = MagicMock()
    subparsers.add_parser.return_value = mock_parser

    setup_parser(subparsers)
    command_func = mock_parser.set_defaults.call_args[1]["func"]

    args = argparse.Namespace(high_contrast=False, force=False)
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk) as mock_get_sdk,
        patch.dict("os.environ", {"TERM": "dumb"}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
        pytest.raises(SystemExit) as exc_info,
    ):
        command_func(args)

    assert exc_info.value.code == 1
    _, err = capsys.readouterr()
    assert "Interactive terminal required" in err
    mock_get_sdk.assert_not_called()


def test_cli_run_tui_command_force_flag_bypasses(mock_sdk):
    """Verify CLI run_tui_command bypasses interactivity guard when --force is set."""
    subparsers = MagicMock()
    mock_parser = MagicMock()
    subparsers.add_parser.return_value = mock_parser

    setup_parser(subparsers)
    command_func = mock_parser.set_defaults.call_args[1]["func"]

    args = argparse.Namespace(high_contrast=False, force=True)
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk),
        patch("imednet_tui.cli.is_interactive_terminal", return_value=False),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
    ):
        command_func(args)
        mock_run_tui.assert_called_once_with(mock_sdk, force=True)


def test_cli_run_tui_command_high_contrast(mock_sdk, monkeypatch):
    """Verify CLI run_tui_command sets IMEDNET_HIGH_CONTRAST when high_contrast is True."""
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    subparsers = MagicMock()
    mock_parser = MagicMock()
    subparsers.add_parser.return_value = mock_parser

    setup_parser(subparsers)
    command_func = mock_parser.set_defaults.call_args[1]["func"]

    args = argparse.Namespace(high_contrast=True, force=False)
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=mock_sdk),
        patch("imednet_tui.cli.is_interactive_terminal", return_value=True),
        patch("imednet_tui.app.run_tui") as mock_run_tui,
    ):
        command_func(args)
        mock_run_tui.assert_called_once_with(mock_sdk, force=False)
        assert os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"


@pytest.mark.asyncio
async def test_job_monitor_bounded_concurrent_polling_duration_and_limit():
    """Verify tracked jobs execute concurrently within one round-trip window bounded by semaphore."""
    import time

    from imednet.models.jobs import JobState, JobStatus

    current_concurrent = 0
    max_concurrent_seen = 0
    lock = asyncio.Lock()

    async def mock_async_get(study_key: str, job_id: str) -> JobStatus:
        nonlocal current_concurrent, max_concurrent_seen
        async with lock:
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
        await asyncio.sleep(0.12)
        async with lock:
            current_concurrent -= 1
        return JobStatus(job_id=job_id, state=JobState.COMPLETED)

    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = mock_async_get

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-CONCURRENT"
    job_ids = [f"BATCH-CONC-{i}" for i in range(5)]
    for jid in job_ids:
        monitor.track_job(jid)

    start = time.monotonic()
    await monitor.refresh_jobs()
    elapsed = time.monotonic() - start

    # 5 jobs taking 0.12s each in parallel complete in < 0.35s (sequential would take >= 0.60s)
    assert elapsed < 0.35
    assert max_concurrent_seen <= 5
    assert max_concurrent_seen >= 2

    rendered = str(monitor.render())
    assert "Active & Recent Jobs (5)" in rendered
    for jid in job_ids:
        assert jid in rendered


@pytest.mark.asyncio
async def test_job_monitor_semaphore_bounds_maximum_concurrency():
    """Verify that tracking more than 5 jobs never exceeds the concurrency limit of 5."""
    from imednet.models.jobs import JobState, JobStatus

    current_concurrent = 0
    max_concurrent_seen = 0
    lock = asyncio.Lock()

    async def mock_async_get(study_key: str, job_id: str) -> JobStatus:
        nonlocal current_concurrent, max_concurrent_seen
        async with lock:
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
        await asyncio.sleep(0.05)
        async with lock:
            current_concurrent -= 1
        return JobStatus(job_id=job_id, state=JobState.RUNNING)

    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = mock_async_get

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-BOUND"
    job_ids = [f"BATCH-BOUND-{i}" for i in range(12)]
    for jid in job_ids:
        monitor.track_job(jid)

    await monitor.refresh_jobs()

    assert max_concurrent_seen == 5


@pytest.mark.asyncio
async def test_job_monitor_partial_network_timeout_does_not_abort_batch(caplog):
    """Test scenario: Network timeout on one tracked job displays FAILED while others succeed."""
    from imednet.models.jobs import JobState, JobStatus

    async def mock_get(study_key: str, job_id: str) -> JobStatus:
        if job_id == "BATCH-TIMEOUT":
            raise TimeoutError("Gateway timeout connecting to iMednet Jobs API")
        return JobStatus(job_id=job_id, state=JobState.COMPLETED)

    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = mock_get

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-PARTIAL-FAIL"
    monitor.track_job("BATCH-OK-1")
    monitor.track_job("BATCH-TIMEOUT")
    monitor.track_job("BATCH-OK-2")

    with caplog.at_level(logging.WARNING, logger="imednet_tui"):
        await monitor.refresh_jobs()

    rendered = str(monitor.render())
    assert "Active & Recent Jobs (3)" in rendered
    assert "BATCH-OK-1" in rendered
    assert "BATCH-TIMEOUT" in rendered
    assert "BATCH-OK-2" in rendered
    assert "FAILED" in rendered
    assert "COMPLETED" in rendered

    assert any("Failed to poll job BATCH-TIMEOUT" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_job_monitor_preserves_registration_order():
    """Verify that refresh_jobs preserves the exact registration order despite varying completion order."""
    from imednet.models.jobs import JobState, JobStatus

    delays = {
        "BATCH-FIRST": 0.08,
        "BATCH-SECOND": 0.01,
        "BATCH-THIRD": 0.05,
    }

    async def mock_get(study_key: str, job_id: str) -> JobStatus:
        await asyncio.sleep(delays[job_id])
        return JobStatus(job_id=job_id, state=JobState.COMPLETED)

    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = mock_get

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-ORDER"
    for jid in ["BATCH-FIRST", "BATCH-SECOND", "BATCH-THIRD"]:
        monitor.track_job(jid)

    await monitor.refresh_jobs()

    rendered = str(monitor.render())
    pos1 = rendered.index("BATCH-FIRST")
    pos2 = rendered.index("BATCH-SECOND")
    pos3 = rendered.index("BATCH-THIRD")
    assert pos1 < pos2 < pos3


@pytest.mark.asyncio
async def test_job_monitor_sync_endpoint_bounded_concurrency():
    """Verify synchronous get() endpoints also execute concurrently across worker threads."""
    import time

    from imednet.models.jobs import JobState, JobStatus

    def sync_get(study_key: str, job_id: str) -> JobStatus:
        time.sleep(0.08)
        return JobStatus(job_id=job_id, state=JobState.RUNNING)

    sdk = MagicMock()
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get = sync_get

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-SYNC-CONC"
    job_ids = [f"BATCH-SYNC-{i}" for i in range(5)]
    for jid in job_ids:
        monitor.track_job(jid)

    start = time.monotonic()
    await monitor.refresh_jobs()
    elapsed = time.monotonic() - start

    # 5 synchronous jobs taking 0.08s each in parallel threads complete in < 0.25s
    # (If sequential, it would take >= 0.40s)
    assert elapsed < 0.25
    rendered = str(monitor.render())
    assert "Active & Recent Jobs (5)" in rendered
    for jid in job_ids:
        assert jid in rendered


@pytest.mark.asyncio
async def test_pilot_sync_simulation_sdk_selection_logs_refresh_and_contrast():
    """Verify Pilot keyboard controls (enter, r, h) and log routing with sync ImednetSDK."""
    imednet_logger = logging.getLogger("imednet")
    baseline_handlers = list(imednet_logger.handlers)

    with ImednetSDK(simulation_mode=True) as sdk:
        app = ImednetTuiApp(sdk)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            dashboard = app.screen

            study_list = dashboard.query_one(StudyList)
            assert len(study_list.children) >= 1
            study_list.focus()
            await pilot.pause()

            # Select first study via Enter
            await pilot.press("enter")
            await pilot.pause()

            site_list = dashboard.query_one(SiteList)
            subject_table = dashboard.query_one(SubjectTable)
            job_monitor = dashboard.query_one(JobMonitor)
            assert dashboard.selected_study_key == "DEMO-STUDY"
            assert len(site_list.children) == 1
            assert subject_table.row_count == 1
            assert job_monitor.current_study_key == "DEMO-STUDY"

            # Select site via Enter
            site_list.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert dashboard.selected_site_id == "102"
            assert subject_table.row_count == 1

            # Verify SDK logger routes into DashboardScreen.log_viewer
            imednet_logger.info("Sync simulation audit log")
            await pilot.pause()
            log_text = "\n".join(str(line) for line in dashboard.log_viewer.lines)
            assert "Selected Study: Demo Oncology Trial (DEMO-STUDY)" in log_text
            assert "Selected Site: North Site (ID: 102)" in log_text
            assert "Sync simulation audit log" in log_text

            # Verify 'r' refreshes studies and active panes
            with (
                patch.object(
                    study_list, "load_studies", wraps=study_list.load_studies
                ) as spy_studies,
                patch.object(site_list, "load_sites", wraps=site_list.load_sites) as spy_sites,
                patch.object(
                    subject_table, "load_subjects", wraps=subject_table.load_subjects
                ) as spy_subjects,
                patch.object(
                    job_monitor, "refresh_jobs", wraps=job_monitor.refresh_jobs
                ) as spy_jobs,
            ):
                await pilot.press("r")
                await pilot.pause()
                spy_studies.assert_called_once()
                spy_sites.assert_called_once_with("DEMO-STUDY")
                spy_subjects.assert_called_once_with("DEMO-STUDY", "102")
                spy_jobs.assert_called_once()

            # Verify 'h' toggles high-contrast styling on DashboardScreen
            studies_box = dashboard.query_one("#studies-box", Container)
            assert studies_box.styles.border.top[0] == "solid"
            await pilot.press("h")
            await pilot.pause()
            assert "high-contrast" in app.classes
            assert "high-contrast" in dashboard.classes
            assert studies_box.styles.border.top[0] == "heavy"

    # Verify TextualLogHandler detached on unmount
    remaining_textual_handlers = [
        h
        for h in imednet_logger.handlers
        if isinstance(h, TextualLogHandler) and h not in baseline_handlers
    ]
    assert remaining_textual_handlers == []


@pytest.mark.asyncio
async def test_pilot_async_simulation_sdk_selection_logs_refresh_and_contrast():
    """Verify Pilot keyboard controls (enter, r, h) with AsyncImednetSDK iterators and coroutines."""
    imednet_logger = logging.getLogger("imednet")
    baseline_handlers = list(imednet_logger.handlers)

    async with AsyncImednetSDK(simulation_mode=True) as sdk:
        app = ImednetTuiApp(sdk)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            dashboard = app.screen

            study_list = dashboard.query_one(StudyList)
            assert len(study_list.children) >= 1
            study_list.focus()
            await pilot.pause()

            # Select study via Enter -> iterates AsyncStudiesEndpoint, AsyncSitesEndpoint, AsyncSubjectsEndpoint
            await pilot.press("enter")
            await pilot.pause()

            site_list = dashboard.query_one(SiteList)
            subject_table = dashboard.query_one(SubjectTable)
            job_monitor = dashboard.query_one(JobMonitor)
            assert dashboard.selected_study_key == "DEMO-STUDY"
            assert len(site_list.children) == 1
            assert subject_table.row_count == 1
            assert job_monitor.current_study_key == "DEMO-STUDY"

            # Select site via Enter -> awaits AsyncSubjectsEndpoint.list_by_site coroutine
            site_list.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert dashboard.selected_site_id == "102"
            assert subject_table.row_count == 1

            imednet_logger.info("Async simulation audit log")
            await pilot.pause()
            log_text = "\n".join(str(line) for line in dashboard.log_viewer.lines)
            assert "Async simulation audit log" in log_text

            # Refresh via 'r' and toggle contrast via 'h'
            await pilot.press("r")
            await pilot.pause()
            assert subject_table.row_count == 1

            await pilot.press("h")
            await pilot.pause()
            studies_box = dashboard.query_one("#studies-box", Container)
            assert "high-contrast" in dashboard.classes
            assert studies_box.styles.border.top[0] == "heavy"

    remaining_textual_handlers = [
        h
        for h in imednet_logger.handlers
        if isinstance(h, TextualLogHandler) and h not in baseline_handlers
    ]
    assert remaining_textual_handlers == []


@pytest.mark.asyncio
async def test_pilot_dotted_and_colliding_keys_preserve_raw_sdk_keys():
    """Verify STUDY.01/STUDY_01 and SITE.01/SITE_01 deduplicate DOM IDs and preserve raw keys."""
    sdk = MagicMock()
    del sdk.studies.async_list
    del sdk.sites.async_list
    del sdk.subjects.async_list
    del sdk.jobs.async_list
    del sdk.jobs.list

    study_dot = MagicMock(study_key="STUDY.01", study_name="Dotted Study")
    study_under = MagicMock(study_key="STUDY_01", study_name="Underscore Study")
    sdk.studies.list = MagicMock(return_value=iter([study_dot, study_under]))

    site_dot = MagicMock(site_id="SITE.01", site_name="Dotted Site")
    site_under = MagicMock(site_id="SITE_01", site_name="Underscore Site")
    sdk.sites.list = MagicMock(side_effect=lambda _key: iter([site_dot, site_under]))

    subj_dot = MagicMock(
        subject_id="SUB-01",
        site_id="SITE.01",
        subject_status="Enrolled",
        date_created="2026-04-01",
    )
    subj_under = MagicMock(
        subject_id="SUB-02",
        site_id="SITE_01",
        subject_status="Screened",
        date_created="2026-04-02",
    )
    sdk.subjects.list = MagicMock(side_effect=lambda _key: iter([subj_dot, subj_under]))
    sdk.subjects.list_by_site = MagicMock(
        side_effect=lambda _key, sid: [subj_dot] if sid == "SITE.01" else [subj_under]
    )

    app = ImednetTuiApp(sdk)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen

        study_list = dashboard.query_one(StudyList)
        study_dom_ids = [child.id for child in study_list.children]
        assert study_dom_ids == ["study-STUDY_01", "study-STUDY_01-2"]

        # Select STUDY.01 via Enter
        study_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        sdk.sites.list.assert_called_with("STUDY.01")
        sdk.subjects.list.assert_called_with("STUDY.01")

        site_list = dashboard.query_one(SiteList)
        site_dom_ids = [child.id for child in site_list.children]
        assert site_dom_ids == ["site-SITE_01", "site-SITE_01-2"]

        # Select SITE.01 via Enter
        site_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        sdk.subjects.list_by_site.assert_called_with("STUDY.01", "SITE.01")

        subject_table = dashboard.query_one(SubjectTable)
        assert subject_table.row_count == 1
        assert str(subject_table.get_row_at(0)[0]) == "SUB-01"

        # Move down to colliding SITE_01 and select via Enter
        await pilot.press("down", "enter")
        await pilot.pause()
        sdk.subjects.list_by_site.assert_called_with("STUDY.01", "SITE_01")
        assert str(subject_table.get_row_at(0)[0]) == "SUB-02"

        # Move down in StudyList to colliding STUDY_01 and select via Enter
        study_list.focus()
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        sdk.sites.list.assert_called_with("STUDY_01")
