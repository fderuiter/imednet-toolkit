"""Unit and integration tests for the imednet-tui plugin."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from textual.app import App
from textual.containers import Container
from textual.widgets import Input, ListItem, ListView, Static

import imednet.spi.cli as spi_cli
import imednet_tui.app as tui_app_mod
import imednet_tui.cli as tui_cli_mod
import imednet_tui.terminal as tui_terminal_mod
from imednet.endpoints.jobs import JobsEndpoint
from imednet.models.jobs import JobState, JobStatus
from imednet.models.subjects import Subject
from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet.spi.constants import DEFAULT_BASE_URL
from imednet.spi.errors import NotFoundError
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet_tui.app import (
    DashboardScreen,
    ImednetTuiApp,
    JobMonitor,
    JobTable,
    LogViewer,
    SiteList,
    StudyList,
    SubjectTable,
    TextualLogHandler,
    is_interactive_terminal,
    run_tui,
)
from imednet_tui.cli import setup_parser


@pytest.fixture(autouse=True)
def reset_high_contrast_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent IMEDNET_HIGH_CONTRAST state leaks across tests."""
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)


async def _settle(pilot: Any) -> None:
    """Drain queued Textual messages and await all active background workers."""
    await pilot.pause()
    for _ in range(5):
        active = [worker for worker in pilot.app.workers if not worker.is_finished]
        if not active:
            break
        await pilot.app.workers.wait_for_complete(active)
        await pilot.pause()


@pytest.fixture
def mock_sdk() -> MagicMock:
    """Return a contract-verified AsyncImednetFacade mock."""
    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock()
    sdk.config.base_url = "https://edc.test.imednetapi.com"

    mock_study = MagicMock()
    mock_study.study_name = "Oncology Phase III"
    mock_study.study_key = "STUDY-ONC-01"
    sdk.async_get_studies = AsyncMock(return_value=[mock_study])

    mock_site = MagicMock()
    mock_site.site_id = "SITE-101"
    mock_site.site_name = "Memorial Hospital"
    sdk.async_get_sites = AsyncMock(return_value=[mock_site])

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

    async def _get_subjects(study_key: str, **filters: Any) -> list[Any]:
        assert study_key
        items = [mock_subj1, mock_subj2]
        site_filter = filters.get("site_id")
        if site_filter is not None:
            items = [item for item in items if str(item.site_id) == str(site_filter)]
        limit = filters.get("limit")
        return items[:limit] if limit is not None else items

    sdk.async_get_subjects = AsyncMock(side_effect=_get_subjects)

    mock_job = JobStatus(
        batch_id="BATCH-DEFAULT-1",
        state=JobState.COMPLETED,
        date_created="2026-01-12T10:00:00Z",
    )
    sdk.async_get_job = AsyncMock(return_value=mock_job)

    return sdk


def test_public_exports_preserved() -> None:
    """Preserve all public symbols in imednet_tui.app.__all__."""
    expected = {
        "ImednetTuiApp",
        "DashboardScreen",
        "StudyList",
        "SiteList",
        "SubjectTable",
        "JobMonitor",
        "TextualLogHandler",
        "is_interactive_terminal",
        "run_tui",
    }
    assert expected.issubset(set(tui_app_mod.__all__))


def test_terminal_helper_imported_from_spi_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify imednet_tui.terminal delegates is_interactive_terminal to imednet.spi.cli.supports_fullscreen_ui."""
    assert tui_terminal_mod.supports_fullscreen_ui is spi_cli.supports_fullscreen_ui
    assert tui_app_mod.is_interactive_terminal is tui_terminal_mod.is_interactive_terminal
    assert tui_cli_mod.is_interactive_terminal is tui_terminal_mod.is_interactive_terminal

    class _TTYStream:
        def isatty(self) -> bool:
            return True

    tty_in = _TTYStream()
    tty_out = _TTYStream()

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)
    monkeypatch.delenv("IMEDNET_CLI_OUTPUT_FORMAT", raising=False)
    monkeypatch.setattr("sys.argv", ["imednet", "tui"])
    assert tui_terminal_mod.is_interactive_terminal(stdin=tty_in, stdout=tty_out) is True

    # Truthy IMEDNET_NON_INTERACTIVE values ("1", "true", "yes", case-insensitive) block TUI
    for truthy_val in ("1", "true", "TRUE", "yes", "YES"):
        monkeypatch.setenv("IMEDNET_NON_INTERACTIVE", truthy_val)
        assert tui_terminal_mod.is_interactive_terminal(stdin=tty_in, stdout=tty_out) is False
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)

    # Case-insensitive TERM=DUMB and empty TERM block TUI
    for bad_term in ("dumb", "DUMB", "  DUMB  ", ""):
        monkeypatch.setenv("TERM", bad_term)
        assert tui_terminal_mod.is_interactive_terminal(stdin=tty_in, stdout=tty_out) is False
    monkeypatch.setenv("TERM", "xterm-256color")

    # JSON output mode blocks TUI
    monkeypatch.setenv("IMEDNET_CLI_OUTPUT_FORMAT", "json")
    assert tui_terminal_mod.is_interactive_terminal(stdin=tty_in, stdout=tty_out) is False


def test_app_instantiation_and_base_url_reads_sdk_config(mock_sdk: MagicMock) -> None:
    """Test that ImednetTuiApp instantiates and reads sdk.config.base_url without _base_url."""

    class _StrictFacade:
        def __init__(self) -> None:
            self.config = MagicMock(base_url="https://strict.imednetapi.com")

        @property
        def _base_url(self) -> str:
            raise AssertionError("ImednetTuiApp must not access private sdk._base_url")

        def get_studies(self, **filters: Any) -> list[Any]:
            return []

    strict_sdk: Any = _StrictFacade()
    app = ImednetTuiApp(strict_sdk)
    assert app.sdk is strict_sdk
    assert isinstance(app, App)
    assert app.high_contrast_enabled is False
    assert mock_sdk.config.base_url == "https://edc.test.imednetapi.com"


@pytest.mark.asyncio
async def test_app_subtitle_uses_sdk_config_base_url() -> None:
    """Verify mounted app subtitle displays sdk.config.base_url and never touches _base_url."""

    class _StrictAsyncFacade:
        def __init__(self) -> None:
            self.config = MagicMock(base_url="https://strict-async.imednetapi.com")

        @property
        def _base_url(self) -> str:
            raise AssertionError("Private _base_url accessed")

        async def async_get_studies(self, **filters: Any) -> list[Any]:
            return []

    sdk: Any = _StrictAsyncFacade()
    app = ImednetTuiApp(sdk)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.sub_title == "Env: https://strict-async.imednetapi.com"


def test_high_contrast_mode_detection(mock_sdk: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that app honors IMEDNET_HIGH_CONTRAST environment variable and explicit flag."""
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    app = ImednetTuiApp(mock_sdk)
    assert app.high_contrast_enabled is True

    app_override = ImednetTuiApp(mock_sdk, high_contrast=False)
    assert app_override.high_contrast_enabled is False

    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    app2 = ImednetTuiApp(mock_sdk)
    assert app2.high_contrast_enabled is False

    app3 = ImednetTuiApp(mock_sdk, high_contrast=True)
    assert app3.high_contrast_enabled is True
    assert "IMEDNET_HIGH_CONTRAST" not in os.environ


def test_toggle_high_contrast_action_leaves_os_environ_unchanged(
    mock_sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test toggling high contrast dynamically without mutating os.environ."""
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    env_snapshot = dict(os.environ)

    app = ImednetTuiApp(mock_sdk)
    assert app.high_contrast_enabled is False

    app.action_toggle_high_contrast()
    assert app.high_contrast_enabled is True
    assert "IMEDNET_HIGH_CONTRAST" not in os.environ
    assert dict(os.environ) == env_snapshot

    app.action_toggle_high_contrast()
    assert app.high_contrast_enabled is False
    assert "IMEDNET_HIGH_CONTRAST" not in os.environ
    assert dict(os.environ) == env_snapshot

    # Also verify preset environment variables are never deleted or overwritten
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    preset_app = ImednetTuiApp(mock_sdk)
    assert preset_app.high_contrast_enabled is True
    preset_app.action_toggle_high_contrast()
    assert preset_app.high_contrast_enabled is False
    assert os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"


@pytest.mark.asyncio
async def test_app_study_and_subject_flow(mock_sdk: MagicMock) -> None:
    """Test interactive dashboard study selection, site loading, and site-filtered subjects."""
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await _settle(pilot)

        study_list = app.screen.query_one(StudyList)
        assert len(study_list.children) == 1

        study_list.post_message(StudyList.StudySelected("STUDY-ONC-01", "Oncology Phase III"))
        await _settle(pilot)

        site_list = app.screen.query_one(SiteList)
        assert len(site_list.children) == 1

        subject_table = app.screen.query_one(SubjectTable)
        assert subject_table.row_count == 2

        job_monitor = app.screen.query_one(JobMonitor)
        assert job_monitor.current_study_key == "STUDY-ONC-01"
        mock_sdk.async_get_job.return_value = JobStatus(
            batch_id="BATCH-Active-1",
            state=JobState.RUNNING,
        )
        job_monitor.track_job("BATCH-Active-1")
        await _settle(pilot)

        # Simulate selecting a site to filter subjects via facade site_id parameter
        site_list.post_message(SiteList.SiteSelected("SITE-101", "Memorial Hospital"))
        await _settle(pilot)

        assert subject_table.row_count == 1
        assert mock_sdk.async_get_subjects.await_args_list[-1].kwargs.get("site_id") == "SITE-101"

        # Trigger refresh action
        app.action_refresh_data()
        await _settle(pilot)
        assert mock_sdk.async_get_studies.await_count >= 2
        assert mock_sdk.async_get_sites.await_count >= 2
        assert mock_sdk.async_get_subjects.await_count >= 3
        assert mock_sdk.async_get_job.await_count >= 2


@pytest.mark.asyncio
async def test_app_list_view_selection_events(mock_sdk: MagicMock) -> None:
    """Test on_list_view_selected event handlers for StudyList and SiteList."""
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await _settle(pilot)

        study_list = app.screen.query_one(StudyList)
        item = study_list.children[0]
        assert isinstance(item, ListItem)

        event = ListView.Selected(study_list, item, 0)
        study_list.on_list_view_selected(event)
        await _settle(pilot)

        site_list = app.screen.query_one(SiteList)
        site_item = site_list.children[0]
        assert isinstance(site_item, ListItem)
        site_event = ListView.Selected(site_list, site_item, 0)
        site_list.on_list_view_selected(site_event)
        await _settle(pilot)


@pytest.mark.asyncio
async def test_app_sync_sdk_facade_offloads_to_thread() -> None:
    """Test that synchronous ImednetFacade methods execute via asyncio.to_thread."""
    sync_sdk = MagicMock(spec=ImednetFacade)
    sync_sdk.config = MagicMock(base_url="https://sync.imednetapi.com")

    mock_study = MagicMock()
    mock_study.study_name = None
    mock_study.name = "Sync Study"
    mock_study.study_key = "SYNC-01"
    sync_sdk.get_studies = MagicMock(return_value=[mock_study])

    mock_site = MagicMock()
    mock_site.site_id = "SITE-SYNC"
    mock_site.site_name = "Sync Site"
    sync_sdk.get_sites = MagicMock(return_value=[mock_site])

    mock_subj = MagicMock()
    mock_subj.subject_id = "SUB-SYNC"
    mock_subj.site_id = "SITE-SYNC"
    mock_subj.subject_status = "Active"
    mock_subj.date_created = "2026-02-01"
    sync_sdk.get_subjects = MagicMock(return_value=[mock_subj])

    mock_job = JobStatus(
        batch_id="SYNC-JOB-1",
        state=JobState.RUNNING,
        date_created="2026-02-01T00:00:00Z",
    )
    sync_sdk.get_job = MagicMock(return_value=mock_job)

    with patch("imednet_tui.app.asyncio.to_thread", wraps=asyncio.to_thread) as spy_to_thread:
        app = ImednetTuiApp(sync_sdk)
        async with app.run_test() as pilot:
            await _settle(pilot)

            study_list = app.screen.query_one(StudyList)
            assert len(study_list.children) == 1

            study_list.post_message(StudyList.StudySelected("SYNC-01", "Sync Study"))
            await _settle(pilot)

            site_list = app.screen.query_one(SiteList)
            assert len(site_list.children) == 1

            subject_table = app.screen.query_one(SubjectTable)
            assert subject_table.row_count == 1

            job_monitor = app.screen.query_one(JobMonitor)
            assert "enumeration is not supported" in str(job_monitor.render())

            job_monitor.track_job("SYNC-JOB-1")
            await _settle(pilot)
            sync_sdk.get_job.assert_called_with("SYNC-01", "SYNC-JOB-1")
            assert "SYNC-JOB-1" in str(job_monitor.render())

            site_list.post_message(SiteList.SiteSelected("SITE-SYNC", "Sync Site"))
            await _settle(pilot)
            assert subject_table.row_count == 1
            assert sync_sdk.get_subjects.call_args.kwargs.get("site_id") == "SITE-SYNC"

        assert spy_to_thread.await_count >= 4


@pytest.mark.asyncio
async def test_app_error_notifications(mock_sdk: MagicMock) -> None:
    """Test error handling in sites, subjects, and jobs."""
    mock_sdk.async_get_sites = AsyncMock(side_effect=RuntimeError("Site failure"))
    mock_sdk.async_get_subjects = AsyncMock(side_effect=RuntimeError("Subject failure"))
    mock_sdk.async_get_job = AsyncMock(side_effect=RuntimeError("Job failure"))

    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await _settle(pilot)

        study_list = app.screen.query_one(StudyList)
        study_list.post_message(StudyList.StudySelected("STUDY-ONC-01", "Oncology Phase III"))
        await _settle(pilot)

        site_list = app.screen.query_one(SiteList)
        assert site_list.loading is False
        subject_table = app.screen.query_one(SubjectTable)
        assert subject_table.loading is False


@pytest.mark.asyncio
async def test_app_handles_study_load_error(mock_sdk: MagicMock) -> None:
    """Test that app gracefully handles study listing errors on mount."""
    mock_sdk.async_get_studies = AsyncMock(side_effect=RuntimeError("Connection refused"))
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await _settle(pilot)

        study_list = app.screen.query_one(StudyList)
        assert study_list.loading is False
        assert len(study_list.children) == 0


@pytest.mark.asyncio
async def test_app_high_contrast_class_applied(
    mock_sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        assert os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_level",
    [logging.WARNING, logging.ERROR, logging.NOTSET, logging.DEBUG],
)
async def test_dashboard_unmount_restores_imednet_logger_level(
    mock_sdk: MagicMock,
    initial_level: int,
) -> None:
    """Unmounting DashboardScreen restores logging.getLogger('imednet').level and handlers."""
    imednet_logger = logging.getLogger("imednet")
    saved_level = imednet_logger.level
    baseline_handlers = list(imednet_logger.handlers)
    imednet_logger.setLevel(initial_level)
    try:
        app = ImednetTuiApp(mock_sdk)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert imednet_logger.level <= logging.INFO
            imednet_logger.info("Verification message during mounted session")
            await pilot.pause()
            log_text = "\n".join(str(line) for line in app.screen.log_viewer.lines)
            assert "Verification message during mounted session" in log_text

        assert imednet_logger.level == initial_level
        leaked = [
            h
            for h in imednet_logger.handlers
            if isinstance(h, TextualLogHandler) and h not in baseline_handlers
        ]
        assert leaked == []
    finally:
        imednet_logger.setLevel(saved_level)


@pytest.mark.asyncio
async def test_job_monitor_without_study_key(mock_sdk: MagicMock) -> None:
    """Test JobMonitor refresh when no study is selected."""
    monitor = JobMonitor(mock_sdk)
    monitor.current_study_key = None
    await monitor.refresh_jobs()


def test_textual_log_handler() -> None:
    """Test TextualLogHandler emits formatted log to Log widget and handles errors."""
    mock_log = MagicMock()
    handler = TextualLogHandler(mock_log)
    formatter = logging.Formatter("%(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    record = logging.LogRecord("test", logging.INFO, "test.py", 10, "Test log message", (), None)
    handler.emit(record)

    mock_log.write_line.assert_called_once_with("INFO - Test log message")

    mock_log.write_line.side_effect = RuntimeError("Write error")
    with patch.object(handler, "handleError") as mock_handle_error:
        handler.emit(record)
        mock_handle_error.assert_called_once()


def test_run_tui_helper(mock_sdk: MagicMock) -> None:
    """Test that run_tui starts the app when terminal is interactive."""
    with (
        patch("imednet_tui.app.is_interactive_terminal", return_value=True),
        patch.object(ImednetTuiApp, "run") as mock_run,
    ):
        run_tui(mock_sdk)
        mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_subject_table_with_real_sdk_subject_instance() -> None:
    """Test that SubjectTable correctly renders real SDK Subject model subject_status."""
    real_subj = Subject(
        subject_id=42,
        site_id=10,
        subject_status="Enrolled",
        date_created="2026-03-01T12:00:00Z",
    )
    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[real_subj])

    app = ImednetTuiApp(sdk)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one(SubjectTable)
        await table.load_subjects("STUDY-01")
        assert table.row_count == 1
        row_data = [str(cell) for cell in table.get_row_at(0)]
        assert "Enrolled" in row_data
        assert "42" in row_data


@pytest.mark.asyncio
async def test_job_monitor_with_real_facade_job_contract() -> None:
    """Test JobMonitor with real ImednetFacade get_job contract."""
    sdk = MagicMock(spec=ImednetFacade)
    real_job = JobStatus(
        batch_id="BATCH-999",
        state=JobState.COMPLETED,
        date_created="2026-03-01T12:00:00Z",
    )
    sdk.get_job = MagicMock(return_value=real_job)

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-REAL"

    await monitor.refresh_jobs()
    assert "enumeration is not supported" in str(monitor.render())

    monitor.track_job("BATCH-999")
    await monitor.refresh_jobs()
    sdk.get_job.assert_called_once_with("STUDY-REAL", "BATCH-999")
    rendered = str(monitor.render())
    assert "BATCH-999" in rendered
    assert "COMPLETED" in rendered


@pytest.mark.asyncio
async def test_job_monitor_terminal_states_stop_polling_and_ignore_later_updates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Terminal jobs (success or JobFailedError) stop polling and ignore stale/transient updates."""
    sdk = MagicMock(spec=AsyncImednetFacade)
    call_counts: dict[str, int] = {"JOB-DONE": 0, "JOB-FAILED": 0, "JOB-CANCEL": 0, "JOB-PROG": 0}

    async def _get_job(study_key: str, batch_id: str) -> JobStatus:
        assert study_key == "STUDY-TERM"
        call_counts[batch_id] += 1
        if batch_id == "JOB-DONE":
            if call_counts[batch_id] > 1:
                raise RuntimeError("Transient failure after completion")
            return JobStatus(batch_id=batch_id, state=JobState.COMPLETED)
        if batch_id == "JOB-FAILED":
            if call_counts[batch_id] > 1:
                return JobStatus(batch_id=batch_id, state=JobState.RUNNING)
            return JobStatus(batch_id=batch_id, state=JobState.FAILED)
        if batch_id == "JOB-CANCEL":
            return JobStatus(batch_id=batch_id, state=JobState.CANCELLED)
        # JOB-PROG transitions RUNNING -> COMPLETED on second poll
        if call_counts[batch_id] == 1:
            return JobStatus(batch_id=batch_id, state=JobState.RUNNING)
        return JobStatus(batch_id=batch_id, state=JobState.COMPLETED)

    sdk.async_get_job = AsyncMock(side_effect=_get_job)

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-TERM"
    for jid in ("JOB-DONE", "JOB-FAILED", "JOB-CANCEL", "JOB-PROG"):
        monitor.track_job(jid)

    # First tick polls all 4 jobs; 3 become terminal immediately, JOB-PROG stays RUNNING
    await monitor.refresh_jobs()
    assert call_counts == {"JOB-DONE": 1, "JOB-FAILED": 1, "JOB-CANCEL": 1, "JOB-PROG": 1}
    rendered_tick1 = str(monitor.render())
    assert "COMPLETED - Job (JOB-DONE)" in rendered_tick1
    assert "FAILED - Job (JOB-FAILED)" in rendered_tick1
    assert "CANCELLED - Job (JOB-CANCEL)" in rendered_tick1
    assert "RUNNING - Job (JOB-PROG)" in rendered_tick1

    # Second tick polls ONLY non-terminal JOB-PROG; terminal jobs are never re-polled
    await monitor.refresh_jobs()
    assert call_counts == {"JOB-DONE": 1, "JOB-FAILED": 1, "JOB-CANCEL": 1, "JOB-PROG": 2}
    rendered_tick2 = str(monitor.render())
    assert "COMPLETED - Job (JOB-PROG)" in rendered_tick2

    # Third tick issues zero network calls because all jobs are now terminal
    await monitor.refresh_jobs()
    assert call_counts == {"JOB-DONE": 1, "JOB-FAILED": 1, "JOB-CANCEL": 1, "JOB-PROG": 2}

    # Verify overlapping in-flight stale active response and transient error after completion
    race_sdk = MagicMock(spec=AsyncImednetFacade)
    both_in_flight = asyncio.Event()
    first_completed = asyncio.Event()
    race_counts: dict[str, int] = {"JOB-STALE-RACE": 0, "JOB-ERR-RACE": 0}

    async def _race_get_job(study_key: str, batch_id: str) -> JobStatus:
        assert study_key == "STUDY-RACE"
        race_counts[batch_id] += 1
        attempt = race_counts[batch_id]
        if sum(race_counts.values()) >= 4:
            both_in_flight.set()
        await both_in_flight.wait()
        if attempt == 1:
            return JobStatus(batch_id=batch_id, state=JobState.COMPLETED)
        await first_completed.wait()
        if batch_id == "JOB-STALE-RACE":
            return JobStatus(batch_id=batch_id, state=JobState.RUNNING)
        raise RuntimeError("Transient gateway glitch after terminal state")

    race_sdk.async_get_job = AsyncMock(side_effect=_race_get_job)
    race_monitor = JobMonitor(race_sdk)
    race_monitor.current_study_key = "STUDY-RACE"
    race_monitor.track_job("JOB-STALE-RACE")
    race_monitor.track_job("JOB-ERR-RACE")

    with caplog.at_level(logging.WARNING, logger="imednet_tui"):
        first_refresh = asyncio.create_task(race_monitor.refresh_jobs())
        second_refresh = asyncio.create_task(race_monitor.refresh_jobs())
        await first_refresh
        first_completed.set()
        await second_refresh

    rendered_race = str(race_monitor.render())
    assert "COMPLETED - Job (JOB-STALE-RACE)" in rendered_race
    assert "COMPLETED - Job (JOB-ERR-RACE)" in rendered_race
    assert "RUNNING" not in rendered_race
    assert any(
        "Ignoring stale response for job JOB-STALE-RACE" in rec.message for rec in caplog.records
    )
    assert any(
        "Ignoring transient error for terminal job JOB-ERR-RACE" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_job_monitor_set_study_isolates_jobs_and_retains_cached_states() -> None:
    """JobMonitor.set_study polls and displays only the active Study's Jobs and retains cache."""
    sdk = MagicMock(spec=AsyncImednetFacade)
    polled: list[tuple[str, str]] = []

    async def _get_job(study_key: str, batch_id: str) -> JobStatus:
        polled.append((study_key, batch_id))
        if study_key == "STUDY-A" and batch_id == "JOB-A-DONE":
            return JobStatus(batch_id=batch_id, state=JobState.COMPLETED)
        if study_key == "STUDY-A" and batch_id == "JOB-A-RUN":
            return JobStatus(batch_id=batch_id, state=JobState.RUNNING)
        if study_key == "STUDY-B" and batch_id == "JOB-B-ONLY":
            return JobStatus(batch_id=batch_id, state=JobState.QUEUED)
        raise AssertionError(f"Unexpected poll {(study_key, batch_id)}")

    sdk.async_get_job = AsyncMock(side_effect=_get_job)

    monitor = JobMonitor(sdk)
    monitor.track_job("JOB-A-DONE", study_key="STUDY-A")
    monitor.track_job("JOB-A-RUN", study_key="STUDY-A")
    monitor.track_job("JOB-B-ONLY", study_key="STUDY-B")

    monitor.set_study("STUDY-A")
    assert monitor.tracked_job_ids == ["JOB-A-DONE", "JOB-A-RUN"]
    await monitor.refresh_jobs()

    rendered_a = str(monitor.render())
    assert "JOB-A-DONE" in rendered_a
    assert "JOB-A-RUN" in rendered_a
    assert "JOB-B-ONLY" not in rendered_a
    assert polled == [("STUDY-A", "JOB-A-DONE"), ("STUDY-A", "JOB-A-RUN")]

    # Switch to STUDY-B: only STUDY-B's job is displayed and polled
    polled.clear()
    monitor.set_study("STUDY-B")
    assert monitor.tracked_job_ids == ["JOB-B-ONLY"]
    await monitor.refresh_jobs()

    rendered_b = str(monitor.render())
    assert "JOB-B-ONLY" in rendered_b
    assert "QUEUED" in rendered_b
    assert "JOB-A-DONE" not in rendered_b
    assert "JOB-A-RUN" not in rendered_b
    assert polled == [("STUDY-B", "JOB-B-ONLY")]

    # Switch back to STUDY-A: cached states render immediately before refresh_jobs() runs
    polled.clear()
    monitor.set_study("STUDY-A")
    cached_render_a = str(monitor.render())
    assert "COMPLETED - Job (JOB-A-DONE)" in cached_render_a
    assert "RUNNING - Job (JOB-A-RUN)" in cached_render_a
    assert "JOB-B-ONLY" not in cached_render_a

    # Refreshing STUDY-A polls only active JOB-A-RUN; terminal JOB-A-DONE stays cached
    await monitor.refresh_jobs()
    assert polled == [("STUDY-A", "JOB-A-RUN")]


@pytest.mark.asyncio
async def test_job_monitor_missing_invalid_and_backward_transition_states(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing state renders ACTIVE; invalid states and backward transitions warn safely."""
    sdk = MagicMock(spec=AsyncImednetFacade)

    # Step 1: Missing state (state=None) renders as [yellow]ACTIVE[/yellow]
    missing_state_job = JobStatus(batch_id="JOB-MISSING", state=None)
    # Step 2: Valid active state RUNNING
    running_job = JobStatus(batch_id="JOB-TRANSITION", state=JobState.RUNNING)
    # Step 3: Invalid wire state on an unvalidated object triggers InvalidJobStateError
    bogus_job = MagicMock()
    bogus_job.batch_id = "JOB-TRANSITION"
    bogus_job.job_id = None
    bogus_job.job_type = "Job"
    bogus_job.date_created = ""
    bogus_job.state = "MYSTERY_WIRE_STATE"
    bogus_job.is_terminal = False
    bogus_job.is_failed = False
    # Step 4: Backward transition RUNNING -> PENDING triggers InvalidJobTransitionError
    regressed_job = JobStatus(batch_id="JOB-TRANSITION", state=JobState.PENDING)
    # Step 5: Valid forward completion RUNNING -> COMPLETED clears warning and freezes
    completed_job = JobStatus(batch_id="JOB-TRANSITION", state=JobState.COMPLETED)

    responses: dict[str, list[Any]] = {
        "JOB-MISSING": [
            missing_state_job,
            JobStatus(batch_id="JOB-MISSING", state=JobState.COMPLETED),
        ],
        "JOB-TRANSITION": [running_job, bogus_job, regressed_job, completed_job],
    }

    async def _get_job(study_key: str, batch_id: str) -> Any:
        assert study_key == "STUDY-STATES"
        return responses[batch_id].pop(0)

    sdk.async_get_job = AsyncMock(side_effect=_get_job)

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-STATES"
    monitor.track_job("JOB-MISSING")
    monitor.track_job("JOB-TRANSITION")

    # Poll 1: JOB-MISSING -> ACTIVE (yellow), JOB-TRANSITION -> RUNNING
    with patch.object(monitor, "update", wraps=monitor.update) as spy_update:
        await monitor.refresh_jobs()
        markup_1 = str(spy_update.call_args.args[0])
    assert "[yellow]ACTIVE[/yellow] - Job (JOB-MISSING)" in markup_1
    rendered_1 = str(monitor.render())
    assert "ACTIVE - Job (JOB-MISSING)" in rendered_1
    assert "RUNNING - Job (JOB-TRANSITION)" in rendered_1

    # Poll 2: JOB-MISSING completes; JOB-TRANSITION sees bogus state -> warning, keeps RUNNING
    with caplog.at_level(logging.WARNING, logger="imednet_tui"):
        await monitor.refresh_jobs()
    rendered_2 = str(monitor.render())
    assert "COMPLETED - Job (JOB-MISSING)" in rendered_2
    assert "RUNNING - Job (JOB-TRANSITION)" in rendered_2
    assert "Warning:" in rendered_2
    assert "MYSTERY_WIRE_STATE" in rendered_2

    # Poll 3: JOB-TRANSITION attempts backward RUNNING -> PENDING -> warning, keeps RUNNING
    with caplog.at_level(logging.WARNING, logger="imednet_tui"):
        await monitor.refresh_jobs()
    rendered_3 = str(monitor.render())
    assert "RUNNING - Job (JOB-TRANSITION)" in rendered_3
    assert "Invalid job state transition from RUNNING to PENDING" in rendered_3

    # Poll 4: JOB-TRANSITION cleanly advances from preserved RUNNING -> COMPLETED
    await monitor.refresh_jobs()
    rendered_4 = str(monitor.render())
    assert "COMPLETED - Job (JOB-TRANSITION)" in rendered_4
    assert "Warning:" not in rendered_4


@pytest.mark.asyncio
async def test_job_monitor_wire_payload_missing_and_invalid_state_via_async_sdk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify real AsyncImednetSDK HTTP wire responses with omitted and unrecognized state."""
    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.jobs.test/api/v1/edc/studies/ST-WIRE/jobs/BATCH-OMIT").mock(
            return_value=httpx.Response(200, json={"batchId": "BATCH-OMIT", "progress": 25})
        )
        router.get("https://api.jobs.test/api/v1/edc/studies/ST-WIRE/jobs/BATCH-BAD").mock(
            return_value=httpx.Response(
                200,
                json={"batchId": "BATCH-BAD", "state": "BOGUS_WIRE_ENUM"},
            )
        )

        async with AsyncImednetSDK(
            api_key="wire-key",  # pragma: allowlist secret
            security_key="wire-secret",  # pragma: allowlist secret
            base_url="https://api.jobs.test",
        ) as async_sdk:
            monitor = JobMonitor(async_sdk)
            monitor.current_study_key = "ST-WIRE"
            monitor.track_job("BATCH-OMIT")
            monitor.track_job("BATCH-BAD")

            with (
                caplog.at_level(logging.WARNING, logger="imednet_tui"),
                patch.object(monitor, "update", wraps=monitor.update) as spy_update,
            ):
                await monitor.refresh_jobs()
                wire_markup = str(spy_update.call_args.args[0])

            assert "[yellow]ACTIVE[/yellow] - Job (BATCH-OMIT)" in wire_markup
            assert "[yellow]WARNING[/yellow] - Job (BATCH-BAD)" in wire_markup

            rendered = str(monitor.render())
            assert "ACTIVE - Job (BATCH-OMIT)" in rendered
            assert "WARNING - Job (BATCH-BAD)" in rendered
            assert "BOGUS_WIRE_ENUM" in rendered
            assert any(
                "Invalid job state update for BATCH-BAD" in rec.message for rec in caplog.records
            )


@pytest.mark.asyncio
async def test_job_monitor_tracked_job_failure_retains_failed_state() -> None:
    """Test that a tracked job whose get_job() raises is retained as FAILED rather than dropped."""
    sdk = MagicMock(spec=ImednetFacade)
    sdk.get_job = MagicMock(side_effect=RuntimeError("Internal job lookup error"))

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-ERR"
    monitor.track_job("BATCH-ERR-101")
    await monitor.refresh_jobs()

    rendered = str(monitor.render())
    assert "Active & Recent Jobs (1)" in rendered
    assert "Active & Recent Jobs (0)" not in rendered
    assert "BATCH-ERR-101" in rendered
    assert "FAILED" in rendered


@pytest.mark.asyncio
async def test_job_monitor_tracked_job_unknown_state() -> None:
    """Test that tracked job with unretrievable/None result is displayed as UNKNOWN."""
    sdk = MagicMock(spec=ImednetFacade)
    sdk.get_job = MagicMock(return_value=None)

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-UNK"
    monitor.track_job("BATCH-UNK-202")
    await monitor.refresh_jobs()

    rendered = str(monitor.render())
    assert "Active & Recent Jobs (1)" in rendered
    assert "BATCH-UNK-202" in rendered
    assert "UNKNOWN" in rendered


@pytest.mark.asyncio
async def test_job_monitor_sync_get_offloaded_to_thread() -> None:
    """Test that synchronous ImednetFacade.get_job is offloaded to a worker thread."""
    main_thread = threading.current_thread()
    called_in_worker_thread = False

    def sync_get_job(study_key: str, batch_id: str) -> JobStatus:
        nonlocal called_in_worker_thread
        assert study_key == "STUDY-ASYNC"
        if threading.current_thread() != main_thread:
            called_in_worker_thread = True
        return JobStatus(batch_id=batch_id, state=JobState.RUNNING)

    sdk = MagicMock(spec=ImednetFacade)
    sdk.get_job = sync_get_job

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
async def test_job_monitor_with_real_jobs_endpoint_via_facade() -> None:
    """Test JobMonitor when ImednetFacade.get_job delegates to a real JobsEndpoint."""
    mock_client = MagicMock()
    mock_client.get.side_effect = RuntimeError("Connection timed out")
    real_endpoint = JobsEndpoint(mock_client)

    sdk = MagicMock(spec=ImednetFacade)
    sdk.get_job = real_endpoint.get

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-REAL-EP"
    monitor.track_job("BATCH-REAL-ERR")
    await monitor.refresh_jobs()

    rendered = str(monitor.render())
    assert "Active & Recent Jobs (1)" in rendered
    assert "FAILED" in rendered
    assert "BATCH-REAL-ERR" in rendered


@pytest.mark.asyncio
async def test_job_monitor_sync_get_responsiveness_regression_heartbeat() -> None:
    """Responsiveness regression: synchronous get_job() does not block event loop heartbeat."""
    heartbeat_ticks = 0
    stop_heartbeat = False

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not stop_heartbeat:
            await asyncio.sleep(0.01)
            heartbeat_ticks += 1

    def slow_sync_get_job(study_key: str, batch_id: str) -> JobStatus:
        assert study_key == "STUDY-HEARTBEAT"
        time.sleep(0.20)
        return JobStatus(batch_id=batch_id, state=JobState.COMPLETED)

    sdk = MagicMock(spec=ImednetFacade)
    sdk.get_job = slow_sync_get_job

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-HEARTBEAT"
    monitor.track_job("BATCH-HEARTBEAT-1")

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        await monitor.refresh_jobs()
    finally:
        stop_heartbeat = True
        await heartbeat_task

    assert heartbeat_ticks >= 4
    rendered = str(monitor.render())
    assert "Active & Recent Jobs (1)" in rendered
    assert "COMPLETED" in rendered
    assert "BATCH-HEARTBEAT-1" in rendered


def test_is_interactive_terminal_true() -> None:
    """Verify is_interactive_terminal returns True when streams are TTY and TERM is valid."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is True


def test_is_interactive_terminal_false_when_non_interactive_env() -> None:
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
def test_is_interactive_terminal_false_when_term_dumb_or_empty(term_val: str) -> None:
    """Verify is_interactive_terminal returns False when TERM is dumb, empty, or whitespace."""
    with (
        patch.dict("os.environ", {"TERM": term_val}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_is_interactive_terminal_false_when_non_tty_stdout() -> None:
    """Verify is_interactive_terminal returns False when stdout is redirected / non-tty."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", return_value=False),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_is_interactive_terminal_false_when_non_tty_stdin() -> None:
    """Verify is_interactive_terminal returns False when stdin is piped / non-tty."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("sys.stdin.isatty", return_value=False),
    ):
        assert is_interactive_terminal() is False


@pytest.mark.parametrize(
    "exc_type",
    [OSError("Closed"), ValueError("Invalid"), AttributeError("No isatty")],
)
def test_is_interactive_terminal_false_on_stream_exception(exc_type: Exception) -> None:
    """Verify is_interactive_terminal safely returns False on stream inspection exceptions."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout.isatty", side_effect=exc_type),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert is_interactive_terminal() is False


def test_is_interactive_terminal_false_when_stream_is_none() -> None:
    """Verify is_interactive_terminal safely handles None streams."""
    with (
        patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True),
        patch("sys.stdout", None),
    ):
        assert is_interactive_terminal() is False


def test_run_tui_headless_raises_runtime_error(mock_sdk: MagicMock) -> None:
    """Verify run_tui raises RuntimeError when terminal is non-interactive and force is False."""
    with (
        patch("imednet_tui.app.is_interactive_terminal", return_value=False),
        patch.object(ImednetTuiApp, "run") as mock_run,
    ):
        with pytest.raises(RuntimeError, match="Interactive terminal required to run the TUI"):
            run_tui(mock_sdk, force=False)
        mock_run.assert_not_called()


def test_run_tui_force_flag_bypasses_check(mock_sdk: MagicMock) -> None:
    """Verify run_tui runs app when force=True even in non-interactive environment."""
    with (
        patch("imednet_tui.app.is_interactive_terminal", return_value=False),
        patch.object(ImednetTuiApp, "run") as mock_run,
    ):
        run_tui(mock_sdk, force=True, high_contrast=True)
        mock_run.assert_called_once()
        assert "IMEDNET_HIGH_CONTRAST" not in os.environ


def test_cli_run_tui_command_interactive_success(mock_sdk: MagicMock) -> None:
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


def test_cli_run_tui_command_headless_exits(
    mock_sdk: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_cli_run_tui_command_headless_skips_sdk_initialization(
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_cli_run_tui_command_non_interactive_env_exits(
    mock_sdk: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_cli_run_tui_command_term_dumb_or_missing_exits(
    mock_sdk: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_cli_run_tui_command_force_flag_bypasses(mock_sdk: MagicMock) -> None:
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


def test_cli_run_tui_command_high_contrast_leaves_environ_unchanged(
    mock_sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify CLI run_tui_command passes high_contrast=True without mutating os.environ."""
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    env_before = dict(os.environ)

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
        mock_run_tui.assert_called_once_with(mock_sdk, force=False, high_contrast=True)
        assert "IMEDNET_HIGH_CONTRAST" not in os.environ
        assert dict(os.environ) == env_before


@pytest.mark.asyncio
async def test_job_monitor_bounded_concurrent_polling_duration_and_limit() -> None:
    """Verify tracked jobs execute concurrently within one round-trip window bounded by semaphore."""
    current_concurrent = 0
    max_concurrent_seen = 0
    lock = asyncio.Lock()
    all_in_flight = asyncio.Event()

    async def _get_job_async(study_key: str, batch_id: str) -> JobStatus:
        nonlocal current_concurrent, max_concurrent_seen
        assert study_key == "STUDY-CONCURRENT"
        async with lock:
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
            if current_concurrent == 5:
                all_in_flight.set()
        await asyncio.wait_for(all_in_flight.wait(), timeout=2.0)
        await asyncio.sleep(0.02)
        async with lock:
            current_concurrent -= 1
        return JobStatus(batch_id=batch_id, state=JobState.COMPLETED)

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.async_get_job = _get_job_async

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-CONCURRENT"
    job_ids = [f"BATCH-CONC-{i}" for i in range(5)]
    for jid in job_ids:
        monitor.track_job(jid)

    start = time.monotonic()
    await monitor.refresh_jobs()
    elapsed = time.monotonic() - start

    assert elapsed < 1.5
    assert max_concurrent_seen == JobMonitor.MAX_CONCURRENT_POLLS

    rendered = str(monitor.render())
    assert "Active & Recent Jobs (5)" in rendered
    for jid in job_ids:
        assert jid in rendered


@pytest.mark.asyncio
async def test_job_monitor_semaphore_bounds_maximum_concurrency() -> None:
    """Verify that tracking more than MAX_CONCURRENT_POLLS jobs never exceeds concurrency limit."""
    current_concurrent = 0
    max_concurrent_seen = 0
    lock = asyncio.Lock()

    async def _get_job_async(study_key: str, batch_id: str) -> JobStatus:
        nonlocal current_concurrent, max_concurrent_seen
        assert study_key == "STUDY-BOUND"
        async with lock:
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
        await asyncio.sleep(0.05)
        async with lock:
            current_concurrent -= 1
        return JobStatus(batch_id=batch_id, state=JobState.RUNNING)

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.async_get_job = _get_job_async

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-BOUND"
    job_ids = [f"BATCH-BOUND-{i}" for i in range(12)]
    for jid in job_ids:
        monitor.track_job(jid)

    await monitor.refresh_jobs()

    assert max_concurrent_seen == JobMonitor.MAX_CONCURRENT_POLLS


@pytest.mark.asyncio
async def test_job_monitor_partial_network_timeout_does_not_abort_batch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Network timeout on one tracked job displays FAILED while sibling jobs succeed."""

    async def _get_job_async(study_key: str, batch_id: str) -> JobStatus:
        assert study_key == "STUDY-PARTIAL-FAIL"
        if batch_id == "BATCH-TIMEOUT":
            raise TimeoutError("Gateway timeout connecting to iMednet Jobs API")
        return JobStatus(batch_id=batch_id, state=JobState.COMPLETED)

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.async_get_job = _get_job_async

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
async def test_job_monitor_preserves_registration_order() -> None:
    """Verify refresh_jobs preserves exact registration order despite varying completion times."""
    delays = {
        "BATCH-FIRST": 0.08,
        "BATCH-SECOND": 0.01,
        "BATCH-THIRD": 0.05,
    }

    async def _get_job_async(study_key: str, batch_id: str) -> JobStatus:
        assert study_key == "STUDY-ORDER"
        await asyncio.sleep(delays[batch_id])
        return JobStatus(batch_id=batch_id, state=JobState.COMPLETED)

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.async_get_job = _get_job_async

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
async def test_job_monitor_sync_facade_bounded_concurrency() -> None:
    """Verify synchronous get_job() facade calls execute concurrently across worker threads."""
    barrier = threading.Barrier(5, timeout=2.0)

    def sync_get_job(study_key: str, batch_id: str) -> JobStatus:
        assert study_key == "STUDY-SYNC-CONC"
        barrier.wait()
        return JobStatus(batch_id=batch_id, state=JobState.RUNNING)

    sdk = MagicMock(spec=ImednetFacade)
    sdk.get_job = sync_get_job

    monitor = JobMonitor(sdk)
    monitor.current_study_key = "STUDY-SYNC-CONC"
    job_ids = [f"BATCH-SYNC-{i}" for i in range(5)]
    for jid in job_ids:
        monitor.track_job(jid)

    start = time.monotonic()
    await monitor.refresh_jobs()
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    rendered = str(monitor.render())
    assert "Active & Recent Jobs (5)" in rendered
    for jid in job_ids:
        assert jid in rendered


@pytest.mark.asyncio
async def test_subject_table_2500_study_issues_single_http_request_and_loads_all() -> None:
    """SubjectTable issues 1 HTTP request via ImednetSDK facade on a 2,500-item Study."""

    def _sites_initial_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 0))
        size = int(request.url.params.get("size", 100))
        total = 150
        start = page * size
        count = max(0, min(size, total - start))
        total_pages = (total + size - 1) // size
        return httpx.Response(
            200,
            json={
                "data": [
                    {"siteId": start + i + 1, "siteName": f"Site {start + i + 1}"}
                    for i in range(count)
                ],
                "pagination": {
                    "page": page,
                    "size": size,
                    "totalPages": total_pages,
                    "totalItems": total,
                },
            },
        )

    def _subjects_initial_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 0))
        size = int(request.url.params.get("size", 100))
        total = 2500
        start = page * size
        count = max(0, min(size, total - start))
        total_pages = (total + size - 1) // size
        data = [
            {
                "subjectId": start + idx + 1,
                "subjectKey": f"SUB-{start + idx + 1:04d}",
                "siteId": 1,
                "subjectStatus": "Enrolled",
                "dateCreated": "2026-02-01T00:00:00Z",
            }
            for idx in range(count)
        ]
        return httpx.Response(
            200,
            json={
                "data": data,
                "pagination": {
                    "page": page,
                    "size": size,
                    "totalPages": total_pages,
                    "totalItems": total,
                },
            },
        )

    with respx.mock(assert_all_called=False) as router:
        studies_route = router.get("https://api.test/api/v1/edc/studies").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"studyKey": "LARGE-01", "studyName": "Large Trial"}],
                    "pagination": {"page": 0, "size": 101, "totalPages": 1, "totalItems": 1},
                },
            )
        )
        sites_route = router.get("https://api.test/api/v1/edc/studies/LARGE-01/sites").mock(
            side_effect=_sites_initial_handler
        )
        subjects_route = router.get("https://api.test/api/v1/edc/studies/LARGE-01/subjects").mock(
            side_effect=_subjects_initial_handler
        )

        sdk = ImednetSDK(
            api_key="test-key",  # pragma: allowlist secret
            security_key="test-secret",  # pragma: allowlist secret
            base_url="https://api.test",
        )

        app = ImednetTuiApp(sdk)
        async with app.run_test() as pilot:
            await _settle(pilot)
            assert studies_route.call_count == 1

            study_list = app.screen.query_one(StudyList)
            study_list.post_message(StudyList.StudySelected("LARGE-01", "Large Trial"))
            await _settle(pilot)

            assert subjects_route.call_count == 1
            assert sites_route.call_count == 1

            subject_table = app.screen.query_one(SubjectTable)
            assert subject_table.row_count == 100
            assert subject_table.truncated is True

            site_list = app.screen.query_one(SiteList)
            assert len(site_list.children) == 100
            assert site_list.truncated is True

            banner = app.screen.query_one("#truncation-notice", Static)
            assert "visible" in banner.classes
            assert "Showing first 100 subjects" in str(banner.render())
            subjects_box = app.screen.query_one("#subjects-box", Container)
            assert "100" in str(subjects_box.border_subtitle)

            # Replace routes with smaller 2-page total so Load All ('a') finishes fast
            sites_route.mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    json={
                        "data": [
                            {"siteId": int(req.url.params["page"]) * 100 + i, "siteName": f"S{i}"}
                            for i in range(1, 51 if int(req.url.params["page"]) == 1 else 101)
                        ],
                        "pagination": {
                            "page": int(req.url.params["page"]),
                            "size": 100,
                            "totalPages": 2,
                        },
                    },
                )
            )
            subjects_route.mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "subjectId": int(req.url.params["page"]) * 100 + i,
                                "siteId": 1,
                                "subjectStatus": "Enrolled",
                            }
                            for i in range(1, 21 if int(req.url.params["page"]) == 1 else 101)
                        ],
                        "pagination": {
                            "page": int(req.url.params["page"]),
                            "size": 100,
                            "totalPages": 2,
                        },
                    },
                )
            )

            await pilot.press("a")
            await _settle(pilot)

            assert subject_table.row_count == 120
            assert subject_table.truncated is False
            assert len(site_list.children) == 150
            assert site_list.truncated is False
            assert "visible" not in banner.classes
            assert subjects_box.border_subtitle == ""


@pytest.mark.asyncio
async def test_study_list_bounded_truncation_and_load_all() -> None:
    """Verify StudyList bounds initial studies to 100 items and action_load_all loads remaining."""
    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    many_studies = [
        MagicMock(study_name=f"Study {i}", study_key=f"ST-{i:03d}") for i in range(1, 136)
    ]

    async def _get_studies(**filters: Any) -> list[Any]:
        limit = filters.get("limit")
        return many_studies[:limit] if limit is not None else list(many_studies)

    sdk.async_get_studies = AsyncMock(side_effect=_get_studies)

    app = ImednetTuiApp(sdk)
    async with app.run_test() as pilot:
        await _settle(pilot)

        study_list = app.screen.query_one(StudyList)
        assert len(study_list.children) == 100
        assert study_list.truncated is True
        assert "Showing first 100 studies" in study_list.status_notice

        app.action_load_all()
        await _settle(pilot)

        assert len(study_list.children) == 135
        assert study_list.truncated is False
        assert study_list.status_notice == ""


@pytest.mark.asyncio
async def test_pilot_sync_simulation_sdk_selection_logs_refresh_and_contrast() -> None:
    """ImednetSDK(simulation_mode=True) loads Studies, Sites, and Site-filtered Subjects cleanly."""
    imednet_logger = logging.getLogger("imednet")
    saved_level = imednet_logger.level
    imednet_logger.setLevel(logging.WARNING)
    baseline_handlers = list(imednet_logger.handlers)

    try:
        with (
            ImednetSDK(simulation_mode=True) as sdk,
            patch("imednet_tui.app.asyncio.to_thread", wraps=asyncio.to_thread) as spy_to_thread,
        ):
            app = ImednetTuiApp(sdk)
            async with app.run_test() as pilot:
                await _settle(pilot)
                assert isinstance(app.screen, DashboardScreen)
                dashboard = app.screen

                study_list = dashboard.query_one(StudyList)
                assert len(study_list.children) >= 1
                study_list.focus()
                await pilot.pause()

                # Select first study via Enter
                await pilot.press("enter")
                await _settle(pilot)

                site_list = dashboard.query_one(SiteList)
                subject_table = dashboard.query_one(SubjectTable)
                job_monitor = dashboard.query_one(JobMonitor)
                assert dashboard.selected_study_key == "DEMO-STUDY"
                assert len(site_list.children) == 1
                assert subject_table.row_count == 1
                assert job_monitor.current_study_key == "DEMO-STUDY"

                # Select site via Enter -> filters subjects by site_id via ImednetFacade
                site_list.focus()
                await pilot.pause()
                await pilot.press("enter")
                await _settle(pilot)
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
                    await _settle(pilot)
                    spy_studies.assert_called_once()
                    spy_sites.assert_called_once_with("DEMO-STUDY")
                    spy_subjects.assert_called_once_with("DEMO-STUDY", "102")
                    spy_jobs.assert_called_once()

                # Verify 'h' toggles high-contrast styling without mutating os.environ
                studies_box = dashboard.query_one("#studies-box", Container)
                assert studies_box.styles.border.top[0] == "solid"
                await pilot.press("h")
                await pilot.pause()
                assert "high-contrast" in app.classes
                assert "high-contrast" in dashboard.classes
                assert studies_box.styles.border.top[0] == "heavy"
                assert "IMEDNET_HIGH_CONTRAST" not in os.environ

            # Sync facade calls were offloaded via asyncio.to_thread
            assert spy_to_thread.await_count >= 4

        # Verify TextualLogHandler detached and logger level restored on unmount
        remaining_textual_handlers = [
            h
            for h in imednet_logger.handlers
            if isinstance(h, TextualLogHandler) and h not in baseline_handlers
        ]
        assert remaining_textual_handlers == []
        assert imednet_logger.level == logging.WARNING
    finally:
        imednet_logger.setLevel(saved_level)


@pytest.mark.asyncio
async def test_pilot_async_simulation_sdk_selection_logs_refresh_and_zero_to_thread() -> None:
    """AsyncImednetSDK(simulation_mode=True) executes with zero asyncio.to_thread invocations."""
    imednet_logger = logging.getLogger("imednet")
    saved_level = imednet_logger.level
    imednet_logger.setLevel(logging.WARNING)
    baseline_handlers = list(imednet_logger.handlers)

    try:
        async with AsyncImednetSDK(simulation_mode=True) as sdk:
            created_job = await sdk.async_create_record(
                "DEMO-STUDY",
                [{"formKey": "DEMO_FORM", "siteId": 999, "data": {"AGE": 45}}],
            )
            with patch(
                "imednet_tui.app.asyncio.to_thread",
                wraps=asyncio.to_thread,
            ) as spy_to_thread:
                app = ImednetTuiApp(sdk)
                async with app.run_test() as pilot:
                    await _settle(pilot)
                    assert isinstance(app.screen, DashboardScreen)
                    dashboard = app.screen

                    study_list = dashboard.query_one(StudyList)
                    assert len(study_list.children) >= 1
                    study_list.focus()
                    await pilot.pause()

                    # Select study via Enter -> awaits AsyncImednetFacade coroutines directly
                    await pilot.press("enter")
                    await _settle(pilot)

                    site_list = dashboard.query_one(SiteList)
                    subject_table = dashboard.query_one(SubjectTable)
                    job_monitor = dashboard.query_one(JobMonitor)
                    assert dashboard.selected_study_key == "DEMO-STUDY"
                    assert len(site_list.children) == 1
                    # Study has 2 subjects (initial subject on site 102 + synthesized record subject)
                    assert subject_table.row_count == 2
                    assert job_monitor.current_study_key == "DEMO-STUDY"

                    # Track simulated job on AsyncImednetSDK
                    if created_job.batch_id:
                        job_monitor.track_job(created_job.batch_id)
                        await _settle(pilot)
                        assert created_job.batch_id in str(job_monitor.render())

                    # Select site via Enter -> filters down to the 1 subject on site 102
                    site_list.focus()
                    await pilot.pause()
                    await pilot.press("enter")
                    await _settle(pilot)
                    assert dashboard.selected_site_id == "102"
                    assert subject_table.row_count == 1

                    imednet_logger.info("Async simulation audit log")
                    await pilot.pause()
                    log_text = "\n".join(str(line) for line in dashboard.log_viewer.lines)
                    assert "Async simulation audit log" in log_text

                    # Refresh via 'r', Load All via 'a', and toggle contrast via 'h'
                    await pilot.press("r")
                    await _settle(pilot)
                    assert subject_table.row_count == 1

                    await pilot.press("a")
                    await _settle(pilot)
                    assert subject_table.row_count == 1

                    await pilot.press("h")
                    await pilot.pause()
                    studies_box = dashboard.query_one("#studies-box", Container)
                    assert "high-contrast" in dashboard.classes
                    assert studies_box.styles.border.top[0] == "heavy"
                    assert "IMEDNET_HIGH_CONTRAST" not in os.environ

                # Crucial Acceptance Criterion 1: ZERO asyncio.to_thread invocations!
                assert spy_to_thread.call_count == 0
                assert spy_to_thread.await_count == 0

        remaining_textual_handlers = [
            h
            for h in imednet_logger.handlers
            if isinstance(h, TextualLogHandler) and h not in baseline_handlers
        ]
        assert remaining_textual_handlers == []
        assert imednet_logger.level == logging.WARNING
    finally:
        imednet_logger.setLevel(saved_level)


@pytest.mark.asyncio
async def test_pilot_dotted_and_colliding_keys_preserve_raw_sdk_keys() -> None:
    """Verify STUDY.01/STUDY_01 and SITE.01/SITE_01 deduplicate DOM IDs and preserve raw keys."""
    sdk = MagicMock(spec=ImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")

    study_dot = MagicMock(study_key="STUDY.01", study_name="Dotted Study")
    study_under = MagicMock(study_key="STUDY_01", study_name="Underscore Study")
    sdk.get_studies = MagicMock(side_effect=lambda **_kw: [study_dot, study_under])

    site_dot = MagicMock(site_id="SITE.01", site_name="Dotted Site")
    site_under = MagicMock(site_id="SITE_01", site_name="Underscore Site")
    sdk.get_sites = MagicMock(side_effect=lambda _key, **_kw: [site_dot, site_under])

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

    def _get_subjects(_key: str, **filters: Any) -> list[Any]:
        sid = filters.get("site_id")
        if sid == "SITE.01":
            return [subj_dot]
        if sid == "SITE_01":
            return [subj_under]
        return [subj_dot, subj_under]

    sdk.get_subjects = MagicMock(side_effect=_get_subjects)

    app = ImednetTuiApp(sdk)
    async with app.run_test() as pilot:
        await _settle(pilot)
        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen

        study_list = dashboard.query_one(StudyList)
        study_dom_ids = [child.id for child in study_list.children]
        assert study_dom_ids == ["study-STUDY_01", "study-STUDY_01-2"]

        # Select STUDY.01 via Enter
        study_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await _settle(pilot)

        assert sdk.get_sites.call_args.args == ("STUDY.01",)
        assert sdk.get_subjects.call_args.args == ("STUDY.01",)

        site_list = dashboard.query_one(SiteList)
        site_dom_ids = [child.id for child in site_list.children]
        assert site_dom_ids == ["site-SITE_01", "site-SITE_01-2"]

        # Select SITE.01 via Enter
        site_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await _settle(pilot)
        assert sdk.get_subjects.call_args.args == ("STUDY.01",)
        assert sdk.get_subjects.call_args.kwargs.get("site_id") == "SITE.01"

        subject_table = dashboard.query_one(SubjectTable)
        assert subject_table.row_count == 1
        assert str(subject_table.get_row_at(0)[0]) == "SUB-01"

        # Move down to colliding SITE_01 and select via Enter
        await pilot.press("down", "enter")
        await _settle(pilot)
        assert sdk.get_subjects.call_args.args == ("STUDY.01",)
        assert sdk.get_subjects.call_args.kwargs.get("site_id") == "SITE_01"
        assert str(subject_table.get_row_at(0)[0]) == "SUB-02"

        # Move down in StudyList to colliding STUDY_01 and select via Enter
        study_list.focus()
        await pilot.pause()
        await pilot.press("down", "enter")
        await _settle(pilot)
        assert sdk.get_sites.call_args.args == ("STUDY_01",)


@pytest.mark.asyncio
async def test_async_sdk_bounded_initial_fetch_site_filter_and_load_all() -> None:
    """AsyncImednetSDK TUI bounds initial fetch to 1 request, filters by site_id, and loads all."""

    def _paged_subjects(req: httpx.Request) -> httpx.Response:
        page_idx = int(req.url.params.get("page", 0))
        req_size = int(req.url.params.get("size", 100))
        filter_param = req.url.params.get("filter", "")
        if "siteId==10" in filter_param:
            rows = [
                {
                    "subjectId": 1,
                    "subjectKey": "ASUB-1",
                    "siteId": 10,
                    "subjectStatus": "Screened",
                    "dateCreated": "2026-03-01T00:00:00Z",
                }
            ]
            return httpx.Response(
                200,
                json={
                    "data": rows,
                    "pagination": {"page": 0, "size": req_size, "totalPages": 1, "totalItems": 1},
                },
            )

        total = 135
        offset = page_idx * req_size
        batch_len = max(0, min(req_size, total - offset))
        total_pages = (total + req_size - 1) // req_size
        rows = [
            {
                "subjectId": offset + n,
                "subjectKey": f"ASUB-{offset + n}",
                "siteId": 10,
                "subjectStatus": "Screened",
                "dateCreated": "2026-03-01T00:00:00Z",
            }
            for n in range(1, batch_len + 1)
        ]
        return httpx.Response(
            200,
            json={
                "data": rows,
                "pagination": {
                    "page": page_idx,
                    "size": req_size,
                    "totalPages": total_pages,
                    "totalItems": total,
                },
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.async-tui.test/api/v1/edc/studies").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"studyKey": "ASYNC-01", "studyName": "Async Study"}],
                    "pagination": {"page": 0, "size": 101, "totalPages": 1},
                },
            )
        )
        router.get("https://api.async-tui.test/api/v1/edc/studies/ASYNC-01/sites").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"siteId": 10, "siteName": "Async Site"}],
                    "pagination": {"page": 0, "size": 101, "totalPages": 1},
                },
            )
        )
        subj_route = router.get(
            "https://api.async-tui.test/api/v1/edc/studies/ASYNC-01/subjects"
        ).mock(side_effect=_paged_subjects)

        async with AsyncImednetSDK(
            api_key="async-key",  # pragma: allowlist secret
            security_key="async-secret",  # pragma: allowlist secret
            base_url="https://api.async-tui.test",
        ) as async_sdk:
            with patch(
                "imednet_tui.app.asyncio.to_thread",
                wraps=asyncio.to_thread,
            ) as spy_to_thread:
                app = ImednetTuiApp(async_sdk)
                async with app.run_test() as pilot:
                    await _settle(pilot)
                    study_view = app.screen.query_one(StudyList)
                    study_view.post_message(StudyList.StudySelected("ASYNC-01", "Async Study"))
                    await _settle(pilot)

                    assert subj_route.call_count == 1
                    assert dict(subj_route.calls[0].request.url.params) == {
                        "page": "0",
                        "size": "101",
                    }
                    subj_table = app.screen.query_one(SubjectTable)
                    assert subj_table.row_count == 100
                    assert subj_table.truncated is True

                    await pilot.press("a")
                    await _settle(pilot)

                    assert subj_table.row_count == 135
                    assert subj_table.truncated is False

                    # Selecting a site filters directly via site_id -> filter=siteId==10
                    site_view = app.screen.query_one(SiteList)
                    site_view.post_message(SiteList.SiteSelected("10", "Async Site"))
                    await _settle(pilot)
                    assert subj_table.row_count == 1
                    assert "siteId==10" in subj_route.calls[-1].request.url.params.get("filter", "")

                assert spy_to_thread.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport", [(80, 24), (100, 30), (120, 40)])
async def test_jobs_tab_positive_content_height_across_viewports(viewport: tuple[int, int]) -> None:
    """Acceptance Criterion 1 & Req 1/8: Jobs tab and table have positive visible height across viewports."""
    from textual.widgets import Input, Static, TabbedContent, TabPane

    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    study = MagicMock(study_key="VP-STUDY", study_name="Viewport Study")
    sdk.async_get_studies = AsyncMock(return_value=[study])
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=viewport) as pilot:
        await _settle(pilot)

        bottom_pane = app.screen.query_one("#bottom-pane", TabbedContent)
        jobs_tab = app.screen.query_one("#jobs-tab", TabPane)
        job_monitor = app.screen.query_one(JobMonitor)
        batch_input = app.screen.query_one("#batch_id_input", Input)
        notice = app.screen.query_one("#job_monitor_notice", Static)
        job_table = app.screen.query_one("#job_table", JobTable)

        assert bottom_pane.size.height > 0
        assert jobs_tab.content_size.height > 0
        assert job_monitor.content_size.height > 0
        assert batch_input.size.height > 0
        assert job_table.content_size.height > 0

        # Select study with zero tracked jobs and verify empty-view prompt & positive heights
        study_list = app.screen.query_one(StudyList)
        study_list.post_message(StudyList.StudySelected("VP-STUDY", "Viewport Study"))
        await _settle(pilot)

        notice_rendered = str(notice.render())
        assert "upstream APIs require batch IDs" in notice_rendered
        assert "Enter one or more batch IDs above" in notice_rendered
        assert job_monitor.content_size.height > 0
        assert job_table.content_size.height > 0


@pytest.mark.asyncio
async def test_launch_tui_with_initial_study_and_batches_polls_and_renders_table() -> None:
    """Acceptance Criterion 2 & Req 2/5: Launching with study and batch options selects study and polls."""
    from textual.widgets import TabbedContent

    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    s1 = MagicMock(study_key="STUDY-ALPHA", study_name="Alpha Study")
    s2 = MagicMock(study_key="STUDY-BETA", study_name="Beta Study")
    sdk.async_get_studies = AsyncMock(return_value=[s1, s2])
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    jobs_by_id = {
        "BATCH-01": JobStatus(
            batch_id="BATCH-01",
            state=JobState.RUNNING,
            progress=45,
            total_records=120,
            failed_records=2,
            date_created="2026-05-01T10:00:00Z",
        ),
        "BATCH-02": JobStatus(
            batch_id="BATCH-02",
            state=JobState.COMPLETED,
            progress=100,
            total_records=80,
            failed_records=0,
            date_created="2026-05-01T09:30:00Z",
        ),
    }
    sdk.async_get_job = AsyncMock(side_effect=lambda sk, bid: jobs_by_id[bid])

    app = ImednetTuiApp(
        sdk,
        study_key="STUDY-BETA",
        batch_ids=["BATCH-01", "BATCH-02"],
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)

        assert isinstance(app.screen, DashboardScreen)
        assert app.screen.selected_study_key == "STUDY-BETA"

        study_list = app.screen.query_one(StudyList)
        assert study_list.index == 1

        bottom_pane = app.screen.query_one("#bottom-pane", TabbedContent)
        assert bottom_pane.active == "jobs-tab"

        job_table = app.screen.query_one("#job_table", JobTable)
        assert job_table.row_count == 2
        row0 = [str(c) for c in job_table.get_row_at(0)]
        row1 = [str(c) for c in job_table.get_row_at(1)]
        assert row0 == ["BATCH-01", "RUNNING", "45%", "120", "2", "2026-05-01T10:00:00Z"]
        assert row1 == ["BATCH-02", "COMPLETED", "100%", "80", "0", "2026-05-01T09:30:00Z"]


def test_run_tui_forwards_study_key_and_batch_ids(mock_sdk: MagicMock) -> None:
    """Verify run_tui passes study_key, batch_ids, and high_contrast into ImednetTuiApp."""
    with (
        patch("imednet_tui.app.is_interactive_terminal", return_value=True),
        patch.object(ImednetTuiApp, "run") as mock_run,
    ):
        captured: list[ImednetTuiApp] = []
        orig_init = ImednetTuiApp.__init__

        def spy_init(self: ImednetTuiApp, *args: Any, **kwargs: Any) -> None:
            orig_init(self, *args, **kwargs)
            captured.append(self)

        with patch.object(ImednetTuiApp, "__init__", spy_init):
            run_tui(
                mock_sdk,
                high_contrast=True,
                study_key="STUDY-CLI",
                batch_ids=["B-1", "B-2"],
            )

        mock_run.assert_called_once()
        assert len(captured) == 1
        assert captured[0].initial_study_key == "STUDY-CLI"
        assert captured[0].initial_batch_ids == ["B-1", "B-2"]
        assert captured[0].high_contrast_enabled is True


@pytest.mark.asyncio
async def test_inline_batch_input_suppresses_shortcuts_and_polls_immediately() -> None:
    """Acceptance Criteria 3 & 4: Focused batch input types q/d/h/r/a and polls immediately on Enter."""
    from textual.widgets import Input

    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key="STUDY-INP", study_name="Input Study")]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])
    sdk.async_get_job = AsyncMock(
        side_effect=lambda sk, bid: JobStatus(
            batch_id=bid,
            state=JobState.COMPLETED,
            progress=100,
            total_records=25,
            failed_records=3,
            date_created="2026-06-01T12:00:00Z",
        )
    )

    app = ImednetTuiApp(sdk, study_key="STUDY-INP")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)

        batch_input = app.screen.query_one("#batch_id_input", Input)
        job_table = app.screen.query_one("#job_table", JobTable)

        # Submit while no text entered does nothing
        batch_input.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert job_table.row_count == 0

        initial_dark = app.theme
        initial_hc = app.high_contrast_enabled

        # Verify check_action suppresses single-key shortcuts while Input is focused
        assert app.check_action("quit", ()) is False
        assert app.check_action("toggle_dark", ()) is False
        assert app.check_action("toggle_high_contrast", ()) is False
        assert app.check_action("refresh_data", ()) is False
        assert app.check_action("load_all", ()) is False

        # Press q, d, h, r, a inside the focused batch input
        for ch in "qdhra-batch-1, qdrh-batch-2":
            await pilot.press(ch)
        await pilot.pause()

        assert app.is_running is True
        assert app.theme == initial_dark
        assert app.high_contrast_enabled == initial_hc
        assert batch_input.value == "qdhra-batch-1, qdrh-batch-2"

        # Submit batch IDs via Enter
        await pilot.press("enter")
        await _settle(pilot)

        assert batch_input.value == ""
        assert job_table.row_count == 2
        row0 = [str(c) for c in job_table.get_row_at(0)]
        row1 = [str(c) for c in job_table.get_row_at(1)]
        assert row0 == ["qdhra-batch-1", "COMPLETED", "100%", "25", "3", "2026-06-01T12:00:00Z"]
        assert row1 == ["qdrh-batch-2", "COMPLETED", "100%", "25", "3", "2026-06-01T12:00:00Z"]

        # Move focus to JobTable via Tab and verify single-key shortcuts work again
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is job_table
        assert app.check_action("toggle_high_contrast", ()) is True
        await pilot.press("h")
        await pilot.pause()
        assert app.high_contrast_enabled is True


@pytest.mark.asyncio
async def test_study_switching_isolates_tracked_batches_and_retains_prior_list() -> None:
    """Acceptance Criterion 5 & Req 6: Study switching isolates batch lists and never polls foreign IDs."""
    from textual.widgets import Input

    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    s1 = MagicMock(study_key="STUDY-ONE", study_name="Study One")
    s2 = MagicMock(study_key="STUDY-TWO", study_name="Study Two")
    sdk.async_get_studies = AsyncMock(return_value=[s1, s2])
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    polled_pairs: list[tuple[str, str]] = []

    async def mock_get(study_key: str, batch_id: str) -> JobStatus:
        polled_pairs.append((study_key, batch_id))
        return JobStatus(
            batch_id=batch_id,
            state=JobState.RUNNING,
            progress=30,
            total_records=10,
            failed_records=0,
            date_created="2026-06-02T08:00:00Z",
        )

    sdk.async_get_job = mock_get

    app = ImednetTuiApp(sdk, study_key="STUDY-ONE")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)

        batch_input = app.screen.query_one("#batch_id_input", Input)
        job_table = app.screen.query_one("#job_table", JobTable)
        study_list = app.screen.query_one(StudyList)

        # Register two jobs in STUDY-ONE
        batch_input.focus()
        await pilot.pause()
        batch_input.value = "S1-BATCH-A, S1-BATCH-B"
        await pilot.press("enter")
        await _settle(pilot)

        assert job_table.row_count == 2
        assert [str(job_table.get_row_at(0)[0]), str(job_table.get_row_at(1)[0])] == [
            "S1-BATCH-A",
            "S1-BATCH-B",
        ]

        # Switch to STUDY-TWO: must show empty table and must NOT poll S1 batches under STUDY-TWO
        study_list.post_message(StudyList.StudySelected("STUDY-TWO", "Study Two"))
        await _settle(pilot)

        assert job_table.row_count == 0
        assert ("STUDY-TWO", "S1-BATCH-A") not in polled_pairs
        assert ("STUDY-TWO", "S1-BATCH-B") not in polled_pairs

        # Add a job in STUDY-TWO
        batch_input.value = "S2-BATCH-ONLY"
        await pilot.press("enter")
        await _settle(pilot)

        assert job_table.row_count == 1
        assert str(job_table.get_row_at(0)[0]) == "S2-BATCH-ONLY"
        assert ("STUDY-ONE", "S2-BATCH-ONLY") not in polled_pairs

        # Return to STUDY-ONE: prior tracked jobs are preserved and displayed
        study_list.post_message(StudyList.StudySelected("STUDY-ONE", "Study One"))
        await _settle(pilot)

        assert job_table.row_count == 2
        assert [str(job_table.get_row_at(0)[0]), str(job_table.get_row_at(1)[0])] == [
            "S1-BATCH-A",
            "S1-BATCH-B",
        ]


@pytest.mark.asyncio
async def test_terminal_jobs_stop_polling_and_pause_timer() -> None:
    """Acceptance Criterion 6 & Req 7: Terminal jobs stop polling and pause background refresh timer."""
    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key="STUDY-TERM", study_name="Terminal Study")]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    call_counts = {"BATCH-ACTIVE": 0, "BATCH-FINISHED": 0}

    async def mock_get(study_key: str, batch_id: str) -> JobStatus:
        call_counts[batch_id] += 1
        if batch_id == "BATCH-FINISHED":
            return JobStatus(
                batch_id=batch_id,
                state=JobState.COMPLETED,
                progress=100,
                total_records=40,
                failed_records=0,
                date_created="2026-06-03T10:00:00Z",
            )
        # BATCH-ACTIVE is RUNNING on poll 1, then transitions to COMPLETED on poll 2
        if call_counts[batch_id] == 1:
            return JobStatus(
                batch_id=batch_id,
                state=JobState.RUNNING,
                progress=50,
                total_records=40,
                failed_records=0,
                date_created="2026-06-03T10:05:00Z",
            )
        return JobStatus(
            batch_id=batch_id,
            state=JobState.COMPLETED,
            progress=100,
            total_records=40,
            failed_records=1,
            date_created="2026-06-03T10:05:00Z",
        )

    sdk.async_get_job = mock_get

    app = ImednetTuiApp(
        sdk,
        study_key="STUDY-TERM",
        batch_ids=["BATCH-ACTIVE", "BATCH-FINISHED"],
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)

        job_monitor = app.screen.query_one(JobMonitor)
        job_table = app.screen.query_one("#job_table", JobTable)

        # Poll 1: BATCH-ACTIVE is RUNNING, BATCH-FINISHED is COMPLETED -> timer remains active
        assert call_counts == {"BATCH-ACTIVE": 1, "BATCH-FINISHED": 1}
        assert job_monitor.timer is not None
        assert job_monitor.is_polling_active is True

        # Replace interval with a fast 0.05s timer to verify real background timer ticks!
        job_monitor.timer.stop()
        job_monitor.timer = job_monitor.set_interval(0.05, job_monitor.refresh_jobs, pause=False)

        # Wait deterministically for the timer tick that transitions BATCH-ACTIVE to COMPLETED
        for _ in range(40):
            await asyncio.sleep(0.03)
            await _settle(pilot)
            if not job_monitor.is_polling_active:
                break

        # Only BATCH-ACTIVE was polled on tick 2 (BATCH-FINISHED was already terminal!)
        assert call_counts == {"BATCH-ACTIVE": 2, "BATCH-FINISHED": 1}

        # Now every tracked job is terminal -> background timer automatically paused itself!
        assert job_monitor.is_polling_active is False
        assert [str(c) for c in job_table.get_row_at(0)] == [
            "BATCH-ACTIVE",
            "COMPLETED",
            "100%",
            "40",
            "1",
            "2026-06-03T10:05:00Z",
        ]

        # Wait through multiple 0.05s timer intervals: zero additional job requests are sent!
        await asyncio.sleep(0.15)
        await _settle(pilot)
        await job_monitor.refresh_jobs()
        assert call_counts == {"BATCH-ACTIVE": 2, "BATCH-FINISHED": 1}


@pytest.mark.asyncio
async def test_more_than_15_jobs_rendered_and_keyboard_scrollable() -> None:
    """Acceptance Criterion 7 & Req 4: >15 jobs render without truncation and scroll via keyboard."""
    from textual.widgets import Input

    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key="STUDY-MANY", study_name="Many Jobs Study")]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    batch_ids = [f"BATCH-{idx:02d}" for idx in range(1, 23)]  # 22 jobs (> 15)
    sdk.async_get_job = AsyncMock(
        side_effect=lambda sk, bid: JobStatus(
            batch_id=bid,
            state=JobState.COMPLETED,
            progress=100,
            total_records=10 + int(bid.split("-")[1]),
            failed_records=int(bid.split("-")[1]) % 2,
            date_created="2026-06-04T11:00:00Z",
        )
    )

    app = ImednetTuiApp(sdk, study_key="STUDY-MANY", batch_ids=batch_ids)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)

        job_monitor = app.screen.query_one(JobMonitor)
        batch_input = app.screen.query_one("#batch_id_input", Input)
        job_table = app.screen.query_one("#job_table", JobTable)

        assert job_table.row_count == 22
        rendered_summary = str(job_monitor.render())
        for bid in batch_ids:
            assert bid in rendered_summary

        # Focus batch_input, press Tab to move into JobTable, and scroll to the 22nd row via Down arrow
        batch_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is job_table
        assert job_table.cursor_row == 0

        for _ in range(21):
            await pilot.press("down")
        await pilot.pause()

        assert job_table.cursor_row == 21
        last_row = [str(c) for c in job_table.get_row_at(21)]
        assert last_row[0] == "BATCH-22"
        assert last_row[3] == "32"
        assert last_row[4] == "0"


@pytest.mark.asyncio
async def test_batch_submission_without_study_notifies_and_pending_cli_batches_attach() -> None:
    """Verify submitting batch IDs before study selection warns, while initial CLI batches attach on first select."""
    from textual.widgets import Input

    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key="STUDY-DEFER", study_name="Deferred Study")]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])
    sdk.async_get_job = AsyncMock(
        side_effect=lambda sk, bid: JobStatus(batch_id=bid, state=JobState.COMPLETED, progress=100)
    )

    app = ImednetTuiApp(sdk, batch_ids=["PENDING-BATCH-1"])
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)

        # Before selecting a study, no poll happens and submitting in Input warns gracefully
        sdk.async_get_job.assert_not_called()
        batch_input = app.screen.query_one("#batch_id_input", Input)
        batch_input.focus()
        await pilot.pause()
        batch_input.value = "EARLY-BATCH"
        await pilot.press("enter")
        await _settle(pilot)
        sdk.async_get_job.assert_not_called()

        # Selecting a study attaches the deferred CLI batch ID
        study_list = app.screen.query_one(StudyList)
        study_list.post_message(StudyList.StudySelected("STUDY-DEFER", "Deferred Study"))
        await _settle(pilot)

        job_table = app.screen.query_one("#job_table", JobTable)
        assert job_table.row_count == 1
        assert str(job_table.get_row_at(0)[0]) == "PENDING-BATCH-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport", [(80, 24), (80, 30), (120, 40)])
async def test_pilot_viewport_geometry_tab_stability_and_unclipped_subject_table(
    viewport: tuple[int, int],
) -> None:
    """Verify JobMonitor height, #bottom-pane tab stability, LogViewer border, and SubjectTable width."""
    from textual.widgets import TabbedContent

    from imednet_tui.app import JobTable, LogViewer

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    study = MagicMock(study_key="STUDY-GEO-01", study_name="Geometry Study")
    site = MagicMock(site_id="SITE-101", site_name="Central Clinic")
    subject = MagicMock(
        subject_id="SUBJ-001",
        site_id="SITE-101",
        subject_status="Enrolled",
        date_created="2026-01-10T08:00:00Z",
    )
    sdk.async_get_studies = AsyncMock(return_value=[study])
    sdk.async_get_sites = AsyncMock(return_value=[site])
    sdk.async_get_subjects = AsyncMock(return_value=[subject])
    sdk.async_get_job = AsyncMock(
        return_value=JobStatus(
            batch_id="BATCH-GEO-1",
            state=JobState.RUNNING,
            progress=60,
            total_records=50,
            failed_records=0,
            date_created="2026-01-12T10:00:00Z",
        )
    )

    app = ImednetTuiApp(sdk, study_key="STUDY-GEO-01", batch_ids=["BATCH-GEO-1"])
    async with app.run_test(size=viewport) as pilot:
        await _settle(pilot)

        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen
        expect_compact = viewport[0] < 100
        assert ("compact" in dashboard.classes) is expect_compact

        bottom_pane = dashboard.query_one("#bottom-pane", TabbedContent)
        job_monitor = dashboard.query_one(JobMonitor)
        job_table = dashboard.query_one("#job_table", JobTable)
        subject_table = dashboard.query_one(SubjectTable)
        log_viewer = dashboard.query_one(LogViewer)

        assert job_monitor.content_region.height > 0
        assert job_table.content_region.height > 0
        assert subject_table.content_region.height > 0
        assert subject_table.content_region.width >= 53
        assert subject_table.content_region.width >= subject_table.virtual_size.width

        jobs_pane_region = bottom_pane.region
        jobs_pane_height = bottom_pane.size.height
        assert job_monitor.styles.border.top[0] in ("", "none")

        # Switch to Logs tab and verify #bottom-pane height and outer region do not shift
        bottom_pane.active = "logs-tab"
        await _settle(pilot)

        assert bottom_pane.region == jobs_pane_region
        assert bottom_pane.size.height == jobs_pane_height
        assert log_viewer.border_title == "Logs"
        assert log_viewer.styles.border.top[0] == "solid"

        # Verify high-contrast mode keeps JobMonitor borderless and applies heavy border on LogViewer
        await pilot.press("h")
        await _settle(pilot)
        assert log_viewer.styles.border.top[0] == "heavy"
        assert bottom_pane.size.height == jobs_pane_height

        bottom_pane.active = "jobs-tab"
        await _settle(pilot)
        assert job_monitor.styles.border.top[0] in ("", "none")
        assert bottom_pane.size.height == jobs_pane_height


@pytest.mark.asyncio
async def test_dynamic_resize_across_compact_threshold_preserves_state_and_scrolls_jobs() -> None:
    """Resizing across width 100 reflows #dashboard-grid while preserving Study/Site state and scrolling >15 jobs."""
    from textual.widgets import Input, TabbedContent

    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    study = MagicMock(study_key="STUDY-RESIZE", study_name="Resize Trial")
    site = MagicMock(site_id="SITE-202", site_name="West Campus")
    subject = MagicMock(
        subject_id="SUBJ-202",
        site_id="SITE-202",
        subject_status="Enrolled",
        date_created="2026-07-01T14:30:00Z",
    )
    sdk.async_get_studies = AsyncMock(return_value=[study])
    sdk.async_get_sites = AsyncMock(return_value=[site])
    sdk.async_get_subjects = AsyncMock(return_value=[subject])

    batch_ids = [f"BATCH-R-{idx:02d}" for idx in range(1, 19)]
    sdk.async_get_job = AsyncMock(
        side_effect=lambda sk, bid: JobStatus(
            batch_id=bid,
            state=JobState.COMPLETED,
            progress=100,
            total_records=20,
            failed_records=0,
            date_created="2026-07-01T15:00:00Z",
        )
    )

    app = ImednetTuiApp(sdk, study_key="STUDY-RESIZE", batch_ids=batch_ids)
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)

        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen
        site_list = dashboard.query_one(SiteList)
        subject_table = dashboard.query_one(SubjectTable)
        subjects_box = dashboard.query_one("#subjects-box", Container)
        bottom_pane = dashboard.query_one("#bottom-pane", TabbedContent)
        batch_input = dashboard.query_one("#batch_id_input", Input)
        job_table = dashboard.query_one("#job_table", JobTable)

        site_list.post_message(SiteList.SiteSelected("SITE-202", "West Campus"))
        await _settle(pilot)

        assert "compact" not in dashboard.classes
        assert dashboard.selected_study_key == "STUDY-RESIZE"
        assert dashboard.selected_site_id == "SITE-202"
        assert subject_table.row_count == 1
        assert job_table.row_count == 18

        # Narrow to 80x24 -> compact 2x3 grid activates, preserving study/site selection and scrolling >15 jobs
        await pilot.resize_terminal(80, 24)
        await _settle(pilot)

        assert "compact" in dashboard.classes
        assert dashboard.selected_study_key == "STUDY-RESIZE"
        assert dashboard.selected_site_id == "SITE-202"
        assert subject_table.row_count == 1
        assert subjects_box.region.width == 80
        assert bottom_pane.region.width == 80
        assert subject_table.content_region.width >= subject_table.virtual_size.width

        batch_input.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        for _ in range(17):
            await pilot.press("down")
        await pilot.pause()
        assert job_table.cursor_row == 17
        assert str(job_table.get_row_at(17)[0]) == "BATCH-R-18"

        # Expand to boundary width 100 -> exits compact layout cleanly
        await pilot.resize_terminal(100, 30)
        await _settle(pilot)
        assert "compact" not in dashboard.classes
        assert dashboard.selected_study_key == "STUDY-RESIZE"
        assert dashboard.selected_site_id == "SITE-202"

        # Shrink to width 99 -> re-enters compact layout without state loss
        await pilot.resize_terminal(99, 30)
        await _settle(pilot)
        assert "compact" in dashboard.classes
        assert dashboard.selected_study_key == "STUDY-RESIZE"
        assert dashboard.selected_site_id == "SITE-202"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("viewport", "min_job_table_height"),
    [
        ((80, 20), 4),  # >= 4 height (header + at least 3 visible data rows)
        ((80, 24), 6),  # >= 6 height (header + at least 5 visible data rows)
    ],
)
async def test_high_contrast_compact_truncated_job_table_rows_and_logviewer_contrast(
    viewport: tuple[int, int],
    min_job_table_height: int,
) -> None:
    """Verify JobTable height >= 4 on 80x20 and >= 6 on 80x24, inverted 1-line #truncation-notice, and 21:1 LogViewer."""
    from textual.color import Color
    from textual.widgets import Static, TabbedContent

    from imednet_tui.app import DEFAULT_FETCH_LIMIT, JobTable, LogViewer

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    # Trigger truncation across studies, sites, and subjects (DEFAULT_FETCH_LIMIT + 1 items)
    studies = [
        MagicMock(study_key=f"STUDY-{i:03d}", study_name=f"Clinical Study {i:03d}")
        for i in range(DEFAULT_FETCH_LIMIT + 5)
    ]
    sites = [
        MagicMock(site_id=f"SITE-{i:03d}", site_name=f"Clinical Site {i:03d}")
        for i in range(DEFAULT_FETCH_LIMIT + 5)
    ]
    subjects = [
        MagicMock(
            subject_id=f"SUBJ-{i:03d}",
            site_id="SITE-000",
            subject_status="Enrolled",
            date_created="2026-03-01T09:00:00Z",
        )
        for i in range(DEFAULT_FETCH_LIMIT + 5)
    ]
    sdk.async_get_studies = AsyncMock(return_value=studies)
    sdk.async_get_sites = AsyncMock(return_value=sites)
    sdk.async_get_subjects = AsyncMock(return_value=subjects)

    batch_ids = [f"BATCH-HC-{i:02d}" for i in range(1, 8)]
    sdk.async_get_job = AsyncMock(
        side_effect=lambda sk, bid: JobStatus(
            batch_id=bid,
            state=JobState.RUNNING,
            progress=45,
            total_records=100,
            failed_records=0,
            date_created="2026-03-01T10:00:00Z",
        )
    )

    app = ImednetTuiApp(
        sdk,
        high_contrast=True,
        study_key="STUDY-000",
        batch_ids=batch_ids,
    )
    async with app.run_test(size=viewport) as pilot:
        await _settle(pilot)

        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen
        assert "high-contrast" in dashboard.classes
        assert "compact" in dashboard.classes

        trunc_notice = dashboard.query_one("#truncation-notice", Static)
        assert "visible" in trunc_notice.classes
        assert trunc_notice.size.height == 1
        assert trunc_notice.region.height == 1
        assert trunc_notice.styles.border.bottom[0] in ("", "none")
        assert trunc_notice.styles.background == Color(255, 255, 255)
        assert trunc_notice.styles.color == Color(0, 0, 0)

        job_monitor = dashboard.query_one(JobMonitor)
        job_notice = dashboard.query_one("#job_monitor_notice", Static)
        job_notice.update(
            "Tracking 7 asynchronous clinical batch jobs with extended diagnostic message that must remain capped to 1 line"
        )
        await _settle(pilot)

        assert job_monitor.styles.border.top[0] in ("", "none")
        assert job_notice.size.height == 1

        job_table = dashboard.query_one("#job_table", JobTable)
        assert job_table.row_count == 7
        assert job_table.size.height >= min_job_table_height

        # Switch to Logs tab and verify LogViewer renders #000000 bg and #FFFFFF fg (21:1) unfocused & focused
        bottom_pane = dashboard.query_one("#bottom-pane", TabbedContent)
        log_viewer = dashboard.query_one(LogViewer)
        log_viewer.write_line("High-contrast audit log entry")
        bottom_pane.active = "logs-tab"
        await _settle(pilot)

        assert log_viewer.styles.background == Color(0, 0, 0)
        assert log_viewer.styles.color == Color(255, 255, 255)
        unfocused_segments = list(log_viewer.render_line(0))
        assert unfocused_segments
        for seg in unfocused_segments:
            assert seg.style is not None
            assert seg.style.bgcolor is not None
            assert seg.style.bgcolor.triplet == (0, 0, 0)
            assert seg.style.color is not None
            assert seg.style.color.triplet == (255, 255, 255)

        log_viewer.focus()
        await _settle(pilot)
        focused_segments = list(log_viewer.render_line(0))
        assert focused_segments
        for seg in focused_segments:
            assert seg.style is not None
            assert seg.style.bgcolor is not None
            assert seg.style.bgcolor.triplet == (0, 0, 0)
            assert seg.style.color is not None
            assert seg.style.color.triplet == (255, 255, 255)


@pytest.mark.asyncio
async def test_pilot_literal_bracketed_strings_and_error_notifications_across_all_panes() -> None:
    """Render bracketed clinical strings literally across all 5 panes and notifications without MarkupError."""
    from textual.widgets import Label, TabbedContent

    from imednet_tui.app import JobTable, LogViewer

    study_name = r"Oncology [Arm 1/2] [/] \[Cohort"
    study_key = "ST-[1/2][/]"
    site_name = r"Central [Unit A/B] [/] \[0]"
    subj_status = r"Cohort [A/B] [/] \[open"
    created_raw = r"2026-08-01 [UTC][/] \[t]"
    good_batch = r"BATCH-[1/2][/]\[x]"
    bad_batch = "BATCH-WARN-[/]"

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key=study_key, study_name=study_name)]
    )
    sdk.async_get_sites = AsyncMock(return_value=[MagicMock(site_id=0, site_name=site_name)])
    sdk.async_get_subjects = AsyncMock(
        return_value=[
            Subject(
                subject_id=0,
                site_id=0,
                subject_status=subj_status,
                date_created=created_raw,
            )
        ]
    )

    bogus_job = MagicMock()
    bogus_job.batch_id = bad_batch
    bogus_job.job_id = None
    bogus_job.job_type = r"Export [/] [unclosed"
    bogus_job.date_created = created_raw
    bogus_job.state = r"BAD_WIRE_[/]_[unclosed"
    bogus_job.is_terminal = False
    bogus_job.is_failed = False

    async def _get_job(_sk: str, bid: str) -> Any:
        if bid == good_batch:
            return JobStatus(
                batch_id=good_batch,
                state=JobState.COMPLETED,
                progress=100,
                total_records=5,
                failed_records=0,
                date_created=created_raw,
            )
        return bogus_job

    sdk.async_get_job = AsyncMock(side_effect=_get_job)

    app = ImednetTuiApp(sdk, study_key=study_key, batch_ids=[good_batch, bad_batch])
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen

        # Pane 1: StudyList
        study_list = dashboard.query_one(StudyList)
        study_label = str(study_list.children[0].query_one(Label).render())
        assert study_label == f"{study_name} ({study_key})"

        # Pane 2: SiteList
        site_list = dashboard.query_one(SiteList)
        site_label = str(site_list.children[0].query_one(Label).render())
        assert site_label == f"{site_name} (0)"

        # Pane 3: SubjectTable
        subject_table = dashboard.query_one(SubjectTable)
        assert [str(c) for c in subject_table.get_row_at(0)] == [
            "0",
            "0",
            subj_status,
            created_raw,
        ]

        # Pane 4: JobMonitor summary, notice, and JobTable
        job_monitor = dashboard.query_one(JobMonitor)
        job_table = dashboard.query_one("#job_table", JobTable)
        job_notice = dashboard.query_one("#job_monitor_notice", Static)
        rendered_summary = str(job_monitor.render())
        assert good_batch in rendered_summary
        assert bad_batch in rendered_summary
        assert r"BAD_WIRE_[/]_[unclosed" in rendered_summary
        assert r"BAD_WIRE_[/]_[unclosed" in str(job_notice.render())
        assert str(job_table.get_row_at(0)[0]) == good_batch

        # Pane 5: LogViewer
        bottom_pane = dashboard.query_one("#bottom-pane", TabbedContent)
        bottom_pane.active = "logs-tab"
        logging.getLogger("imednet").info(r"Audit log [/] [unclosed \[msg]")
        await _settle(pilot)
        log_viewer = dashboard.query_one(LogViewer)
        assert any(r"Audit log [/] [unclosed \[msg]" in str(line) for line in log_viewer.lines)

        # Error notify(markup=False) paths with malformed markup
        err_payload = r"Failure [/] [unclosed \[detail]"
        sdk.async_get_studies = AsyncMock(side_effect=RuntimeError(err_payload))
        sdk.async_get_sites = AsyncMock(side_effect=RuntimeError(err_payload))
        sdk.async_get_subjects = AsyncMock(side_effect=RuntimeError(err_payload))
        app.action_refresh_data()
        await _settle(pilot)


@pytest.mark.asyncio
async def test_pilot_site_zero_effective_status_and_none_fallbacks() -> None:
    """Preserve site_id=0, Subject.effective_status, mock deleted=True, and clean None fallbacks."""
    from textual.widgets import Label

    from imednet.models.sites import Site
    from imednet.models.studies import Study

    study_zero = Study(study_key="ST-ZERO", study_name="Site Zero Trial")
    study_empty = Study()
    site_zero = Site(site_id=0, site_name="Central Coordinating")
    site_empty = Site()

    subj_deleted = Subject(
        subject_id=101,
        site_id=0,
        subject_status="Enrolled",
        deleted=True,
        date_created="2026-08-02T00:00:00Z",
    )
    subj_empty = Subject()

    mock_deleted = MagicMock(
        subject_id="M-DEL",
        site_id=0,
        subject_status="Active",
        deleted=True,
        date_created="2026-08-03",
    )
    mock_unspec = MagicMock(
        subject_id="M-PLAIN",
        site_id=10,
        subject_status="Screened",
        date_created="2026-08-04",
    )
    mock_bare = MagicMock()

    all_subjects: list[Any] = [subj_deleted, subj_empty, mock_deleted, mock_unspec, mock_bare]

    async def _get_subjects(_sk: str, **filters: Any) -> list[Any]:
        site_filter = filters.get("site_id")
        if site_filter is not None:
            return [
                s
                for s in all_subjects
                if isinstance(getattr(s, "site_id", None), (int, str))
                and str(s.site_id) == str(site_filter)
            ]
        return list(all_subjects)

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(return_value=[study_zero, study_empty])
    sdk.async_get_sites = AsyncMock(return_value=[site_zero, site_empty])
    sdk.async_get_subjects = AsyncMock(side_effect=_get_subjects)

    app = ImednetTuiApp(sdk, study_key="ST-ZERO")
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen

        study_list = dashboard.query_one(StudyList)
        study_labels = [str(item.query_one(Label).render()) for item in study_list.children]
        assert study_labels == ["Site Zero Trial (ST-ZERO)", "Unknown Study (no-key)"]

        site_list = dashboard.query_one(SiteList)
        site_labels = [str(item.query_one(Label).render()) for item in site_list.children]
        assert site_labels == ["Central Coordinating (0)", "Unknown Site (no-id)"]

        subject_table = dashboard.query_one(SubjectTable)
        rows = [
            [str(c) for c in subject_table.get_row_at(i)] for i in range(subject_table.row_count)
        ]
        assert rows == [
            ["101", "0", "DELETED", "2026-08-02T00:00:00Z"],
            ["-", "-", "-", "-"],
            ["M-DEL", "0", "DELETED", "2026-08-03"],
            ["M-PLAIN", "10", "Screened", "2026-08-04"],
            ["-", "-", "-", "-"],
        ]

        # Selecting Site 0 filters subjects by site_id="0"
        site_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await _settle(pilot)
        assert dashboard.selected_site_id == "0"
        assert sdk.async_get_subjects.await_args_list[-1].kwargs.get("site_id") == "0"
        filtered_rows = [
            [str(c) for c in subject_table.get_row_at(i)] for i in range(subject_table.row_count)
        ]
        assert filtered_rows == [
            ["101", "0", "DELETED", "2026-08-02T00:00:00Z"],
            ["M-DEL", "0", "DELETED", "2026-08-03"],
        ]


@pytest.mark.asyncio
async def test_pilot_load_all_cancelled_and_discarded_on_study_switch() -> None:
    """Cancel group='load-all' and discard stale to_thread/async results when switching studies."""
    from textual.widgets import Label

    load_all_entered = threading.Event()
    release_stale_thread = threading.Event()

    studies = [
        MagicMock(study_key="ST-A", study_name="Study A"),
        MagicMock(study_key="ST-B", study_name="Study B"),
    ]

    def _get_studies(**filters: Any) -> list[Any]:
        limit = filters.get("limit")
        return studies[:limit] if limit is not None else list(studies)

    def _get_sites(study_key: str, **filters: Any) -> list[Any]:
        limit = filters.get("limit")
        if study_key == "ST-A":
            if limit is None:
                load_all_entered.set()
                release_stale_thread.wait(timeout=2.0)
                return [MagicMock(site_id=i, site_name=f"Site A-{i}") for i in range(1, 106)]
            return [MagicMock(site_id=i, site_name=f"Site A-{i}") for i in range(1, 102)]
        return [MagicMock(site_id=200, site_name="Site B-Only")]

    def _get_subjects(study_key: str, **filters: Any) -> list[Any]:
        limit = filters.get("limit")
        if study_key == "ST-A":
            if limit is None:
                load_all_entered.set()
                release_stale_thread.wait(timeout=2.0)
                return [
                    Subject(subject_id=i, site_id=1, subject_status="Active") for i in range(1, 106)
                ]
            return [
                Subject(subject_id=i, site_id=1, subject_status="Active") for i in range(1, 102)
            ]
        return [Subject(subject_id=999, site_id=200, subject_status="Completed")]

    sdk = MagicMock(spec=ImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.get_studies = MagicMock(side_effect=_get_studies)
    sdk.get_sites = MagicMock(side_effect=_get_sites)
    sdk.get_subjects = MagicMock(side_effect=_get_subjects)

    app = ImednetTuiApp(sdk, study_key="ST-A")
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot)
        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen
        study_list = dashboard.query_one(StudyList)
        site_list = dashboard.query_one(SiteList)
        subject_table = dashboard.query_one(SubjectTable)

        assert site_list.truncated is True
        assert subject_table.truncated is True

        # Start slow 'a' (load-all) on ST-A, then switch to ST-B while ST-A threads are blocked
        await pilot.press("a")
        for _ in range(50):
            if load_all_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert load_all_entered.is_set()

        study_list.post_message(StudyList.StudySelected("ST-B", "Study B"))
        release_stale_thread.set()
        await _settle(pilot)

        # Also await direct stale coroutines against superseded widgets to verify request-id guards
        stale_sites_task = asyncio.create_task(site_list.load_sites("ST-A"))
        fresh_sites_task = asyncio.create_task(site_list.load_sites("ST-B"))
        await asyncio.gather(stale_sites_task, fresh_sites_task)

        assert dashboard.selected_study_key == "ST-B"
        assert len(study_list.children) == 2
        assert len(site_list.children) == 1
        assert str(site_list.children[0].query_one(Label).render()) == "Site B-Only (200)"
        assert subject_table.row_count == 1
        assert [str(c) for c in subject_table.get_row_at(0)][:3] == ["999", "200", "Completed"]
        assert site_list.loading is False
        assert subject_table.loading is False


@pytest.mark.asyncio
async def test_pilot_study_option_past_100_and_absent_alignment() -> None:
    """Align StudyList.index=None when --study is past 100 or absent, then highlight after load-all."""
    many_studies = [
        MagicMock(study_name=f"Trial {i}", study_key=f"ST-{i:03d}") for i in range(1, 136)
    ]

    async def _get_studies(**filters: Any) -> list[Any]:
        limit = filters.get("limit")
        return many_studies[:limit] if limit is not None else list(many_studies)

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(side_effect=_get_studies)
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    # Target study ST-125 is beyond the initial 100-study page
    app = ImednetTuiApp(sdk, study_key="ST-125")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        assert isinstance(app.screen, DashboardScreen)
        study_list = app.screen.query_one(StudyList)
        assert app.screen.selected_study_key == "ST-125"
        assert len(study_list.children) == 100
        assert study_list.index is None

        await pilot.press("a")
        await _settle(pilot)
        assert len(study_list.children) == 135
        assert study_list.index == 124

    # Absent study key keeps StudyList.index=None even after load-all
    absent_app = ImednetTuiApp(sdk, study_key="ST-DOES-NOT-EXIST")
    async with absent_app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        study_list = absent_app.screen.query_one(StudyList)
        assert study_list.index is None
        await pilot.press("a")
        await _settle(pilot)
        assert study_list.index is None


@pytest.mark.asyncio
async def test_pilot_escape_exits_batch_input_and_clears_site_filter_plus_reselect() -> None:
    """Escape moves focus from #batch_id_input to #job_table, then clears site filter; reselect toggles."""
    from textual.widgets import Input

    from imednet_tui.app import JobTable

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key="ST-NAV", study_name="Navigation Study")]
    )
    sdk.async_get_sites = AsyncMock(
        return_value=[
            MagicMock(site_id=0, site_name="Central Site"),
            MagicMock(site_id=101, site_name="Regional Site"),
        ]
    )
    subjects = [
        Subject(subject_id=1, site_id=0, subject_status="Enrolled"),
        Subject(subject_id=2, site_id=101, subject_status="Screened"),
    ]

    async def _get_subjects(_sk: str, **filters: Any) -> list[Any]:
        site_filter = filters.get("site_id")
        if site_filter is not None:
            return [s for s in subjects if str(s.site_id) == str(site_filter)]
        return list(subjects)

    sdk.async_get_subjects = AsyncMock(side_effect=_get_subjects)

    app = ImednetTuiApp(sdk, study_key="ST-NAV")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        assert isinstance(app.screen, DashboardScreen)
        dashboard = app.screen
        site_list = dashboard.query_one(SiteList)
        subject_table = dashboard.query_one(SubjectTable)
        batch_input = dashboard.query_one("#batch_id_input", Input)
        job_table = dashboard.query_one("#job_table", JobTable)

        assert subject_table.row_count == 2

        # Filter by Site 0
        site_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await _settle(pilot)
        assert dashboard.selected_site_id == "0"
        assert subject_table.row_count == 1

        # Focus #batch_id_input -> Escape moves focus to #job_table without clearing site filter yet
        batch_input.focus()
        await pilot.pause()
        assert app.focused is batch_input
        assert app.check_action("quit", ()) is False
        assert app.check_action("handle_escape", ()) is True

        await pilot.press("escape")
        await _settle(pilot)
        assert app.focused is job_table
        assert app.check_action("quit", ()) is True
        assert dashboard.selected_site_id == "0"
        assert subject_table.row_count == 1

        # Pressing Escape again clears selected_site_id and reloads all study subjects
        await pilot.press("escape")
        await _settle(pilot)
        assert dashboard.selected_site_id is None
        assert subject_table.row_count == 2

        # Re-selecting the active site in SiteList also toggles off selected_site_id
        site_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await _settle(pilot)
        assert dashboard.selected_site_id == "0"
        assert subject_table.row_count == 1

        await pilot.press("enter")
        await _settle(pilot)
        assert dashboard.selected_site_id is None
        assert subject_table.row_count == 2


@pytest.mark.asyncio
async def test_transient_poll_errors_stay_retryable_and_recover() -> None:
    """Verify transient poll errors remain non-terminal and recover on subsequent polls."""
    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key="STUDY-RETRY", study_name="Retry Study")]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    attempts = {"BATCH-FLAKY": 0, "BATCH-ACTIVE-ERR": 0}

    async def _get_job(_study_key: str, batch_id: str) -> JobStatus:
        attempts[batch_id] += 1
        step = attempts[batch_id]
        if batch_id == "BATCH-FLAKY":
            if step == 1:
                raise RuntimeError("503 Gateway Timeout")
            if step == 2:
                return JobStatus(batch_id=batch_id, state=JobState.RUNNING, progress=60)
            return JobStatus(
                batch_id=batch_id,
                state=JobState.COMPLETED,
                progress=100,
                record_total_count=12,
                record_failure_count=0,
            )
        if step == 1:
            return JobStatus(batch_id=batch_id, state=JobState.RUNNING, progress=25)
        if step == 2:
            raise RuntimeError("Temporary socket reset")
        return JobStatus(
            batch_id=batch_id,
            state=JobState.COMPLETED,
            progress=100,
            record_total_count=8,
            record_failure_count=0,
        )

    sdk.async_get_job = AsyncMock(side_effect=_get_job)

    app = ImednetTuiApp(
        sdk,
        study_key="STUDY-RETRY",
        batch_ids=["BATCH-FLAKY", "BATCH-ACTIVE-ERR"],
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        job_monitor = app.screen.query_one(JobMonitor)
        job_table = app.screen.query_one("#job_table", JobTable)

        # Poll 1: BATCH-FLAKY failed transiently -> FAILED placeholder, timer still active
        assert attempts == {"BATCH-FLAKY": 1, "BATCH-ACTIVE-ERR": 1}
        assert job_monitor.is_polling_active is True
        assert str(job_table.get_row_at(0)[1]) == "FAILED"
        assert str(job_table.get_row_at(1)[1]) == "RUNNING"

        # Poll 2 (background refresh): BATCH-FLAKY recovers to RUNNING; BATCH-ACTIVE-ERR hits
        # a transient error while preserving RUNNING and staying non-terminal
        await job_monitor.refresh_jobs()
        await _settle(pilot)
        assert attempts == {"BATCH-FLAKY": 2, "BATCH-ACTIVE-ERR": 2}
        assert job_monitor.is_polling_active is True
        assert str(job_table.get_row_at(0)[1]) == "RUNNING"
        assert str(job_table.get_row_at(1)[1]) == "RUNNING"

        # Poll 3 (background refresh): both finish COMPLETED -> timer pauses automatically
        await job_monitor.refresh_jobs()
        await _settle(pilot)
        assert attempts == {"BATCH-FLAKY": 3, "BATCH-ACTIVE-ERR": 3}
        assert job_monitor.is_polling_active is False
        assert str(job_table.get_row_at(0)[1]) == "COMPLETED"
        assert str(job_table.get_row_at(1)[1]) == "COMPLETED"


@pytest.mark.asyncio
async def test_not_found_placeholder_pauses_timer_retries_on_r_and_untracks_via_keys() -> None:
    """Verify 404 batches pause timers, retry on manual r, and untrack only from JobTable."""
    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key="STUDY-404", study_name="Study 404")]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    calls = {"BATCH-MISTYPED": 0, "BATCH-DONE": 0}
    allow_mistyped_recovery = False

    async def _get_job(_study_key: str, batch_id: str) -> JobStatus:
        calls[batch_id] += 1
        if batch_id == "BATCH-MISTYPED":
            if not allow_mistyped_recovery:
                raise NotFoundError("Batch BATCH-MISTYPED not found", status_code=404)
            return JobStatus(
                batch_id=batch_id,
                state=JobState.COMPLETED,
                progress=100,
                record_total_count=5,
                record_failure_count=0,
            )
        return JobStatus(
            batch_id=batch_id,
            state=JobState.COMPLETED,
            progress=100,
            record_total_count=10,
            record_failure_count=0,
        )

    sdk.async_get_job = AsyncMock(side_effect=_get_job)

    # Also verify untrack_job behavior on unmounted / unassigned / foreign study entries
    standalone = JobMonitor(sdk)
    standalone.track_jobs(["UNASSIGNED-1", "UNASSIGNED-2"], refresh=False)
    assert standalone.untrack_job("   ") is False
    assert standalone.untrack_job("MISSING-ID") is False
    assert standalone.untrack_job("UNASSIGNED-1") is True
    standalone.track_jobs(["FOREIGN-1"], study_key="OTHER-STUDY", refresh=False)
    assert standalone.untrack_job("FOREIGN-1", study_key="OTHER-STUDY") is True
    assert standalone.untrack_job("FOREIGN-1", study_key="OTHER-STUDY") is False

    app = ImednetTuiApp(
        sdk,
        study_key="STUDY-404",
        batch_ids=["BATCH-MISTYPED", "BATCH-DONE"],
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        job_monitor = app.screen.query_one(JobMonitor)
        job_table = app.screen.query_one("#job_table", JobTable)
        batch_input = app.screen.query_one("#batch_id_input", Input)

        # Initial poll marks BATCH-MISTYPED as terminal NOT_FOUND and pauses background timer
        assert calls == {"BATCH-MISTYPED": 1, "BATCH-DONE": 1}
        assert job_monitor.is_polling_active is False
        assert str(job_table.get_row_at(0)[1]) == "NOT_FOUND"
        assert job_table.get_row_at(0)[1].style == "red"

        # Background timer refresh does NOT re-poll terminal NOT_FOUND placeholder
        await job_monitor.refresh_jobs()
        await _settle(pilot)
        assert calls == {"BATCH-MISTYPED": 1, "BATCH-DONE": 1}

        # Pressing manual 'r' retries placeholders without re-polling completed JobStatus entries
        await pilot.press("r")
        await _settle(pilot)
        assert calls == {"BATCH-MISTYPED": 2, "BATCH-DONE": 1}
        assert str(job_table.get_row_at(0)[1]) == "NOT_FOUND"
        assert job_monitor.is_polling_active is False

        # Focused #batch_id_input handles Backspace and Delete normally without untracking rows
        batch_input.focus()
        await pilot.pause()
        for ch in "12X34":
            await pilot.press(ch)
        await pilot.press("left", "left", "backspace", "delete")
        await pilot.pause()
        assert batch_input.value == "124"
        assert job_table.row_count == 2

        # Clear draft input and focus JobTable to untrack rows via Delete and Backspace
        batch_input.value = ""
        job_table.focus()
        await pilot.pause()
        assert app.focused is job_table

        # Cursor starts at row 0 (BATCH-MISTYPED); pressing Delete removes it
        await pilot.press("delete")
        await _settle(pilot)
        assert job_table.row_count == 1
        assert str(job_table.get_row_at(0)[0]) == "BATCH-DONE"
        assert "BATCH-MISTYPED" not in job_monitor.tracked_job_ids

        # Re-add BATCH-MISTYPED after upstream job becomes available -> recovers on track
        allow_mistyped_recovery = True
        job_monitor.track_job("BATCH-MISTYPED")
        await _settle(pilot)
        assert job_table.row_count == 2
        assert str(job_table.get_row_at(1)[1]) == "COMPLETED"

        # Remove both rows with Backspace and Delete -> empty-study prompt returns
        await pilot.press("backspace")
        await _settle(pilot)
        assert job_table.row_count == 1
        await pilot.press("delete")
        await _settle(pilot)
        assert job_table.row_count == 0
        assert job_monitor.tracked_job_ids == []
        notice = app.screen.query_one("#job_monitor_notice", Static)
        assert "Enter one or more batch IDs above" in str(notice.render())


@pytest.mark.asyncio
async def test_terminal_job_fetches_result_url_once_and_styles_partial_failures_red() -> None:
    """Verify AsyncImednetSDK and ImednetSDK fetch resultUrl once and style partial failures red."""
    base_url = "https://edc.result-test.imednetapi.com"
    marker_val = "sec-result-marker-999"  # pragma: allowlist secret

    with respx.mock(base_url=base_url, assert_all_called=False) as router:
        router.get("/api/v1/edc/studies").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"studyKey": "ST_RES", "studyName": "Result URL Study"}],
                    "pagination": {"page": 0, "size": 100, "totalPages": 1, "totalItems": 1},
                },
            )
        )
        router.get("/api/v1/edc/studies/ST_RES/sites").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [],
                    "pagination": {"page": 0, "size": 100, "totalPages": 1, "totalItems": 0},
                },
            )
        )
        router.get("/api/v1/edc/studies/ST_RES/subjects").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [],
                    "pagination": {"page": 0, "size": 100, "totalPages": 1, "totalItems": 0},
                },
            )
        )
        router.get("/api/v1/edc/studies/ST_RES/jobs/B_CLEAN").mock(
            return_value=httpx.Response(
                200,
                json={
                    "batchId": "B_CLEAN",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": f"{base_url}/api/v1/edc/studies/ST_RES/jobs/B_CLEAN/results",
                    "dateCreated": "2026-07-01T10:00:00Z",
                },
            )
        )
        clean_res_route = router.get("/api/v1/edc/studies/ST_RES/jobs/B_CLEAN/results").mock(
            return_value=httpx.Response(
                200,
                json=[{"status": "SUCCESS"}, {"status": "SUCCESS"}],
            )
        )

        router.get("/api/v1/edc/studies/ST_RES/jobs/B_PARTIAL").mock(
            return_value=httpx.Response(
                200,
                json={
                    "batchId": "B_PARTIAL",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": "/api/v1/edc/studies/ST_RES/jobs/B_PARTIAL/results",
                    "dateCreated": "2026-07-01T10:05:00Z",
                },
            )
        )
        partial_res_route = router.get("/api/v1/edc/studies/ST_RES/jobs/B_PARTIAL/results").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"status": "SUCCESS"},
                    {"status": "FAILED", "error": "Invalid visit date"},
                    {"error": "Missing subject key"},
                ],
            )
        )

        router.get("/api/v1/edc/studies/ST_RES/jobs/B_RES_ERR").mock(
            return_value=httpx.Response(
                200,
                json={
                    "batchId": "B_RES_ERR",
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": (
                        f"{base_url}/api/v1/edc/studies/ST_RES/jobs/B_RES_ERR/results"
                        f"?apiKey={marker_val}"
                    ),
                    "dateCreated": "2026-07-01T10:10:00Z",
                },
            )
        )
        err_res_route = router.get("/api/v1/edc/studies/ST_RES/jobs/B_RES_ERR/results").mock(
            return_value=httpx.Response(
                500,
                json={"message": f"Upstream error for token={marker_val}"},
            )
        )

        async with AsyncImednetSDK(
            api_key="test-key",  # pragma: allowlist secret
            security_key="test-sec",  # pragma: allowlist secret
            base_url=base_url,
            retries=0,
        ) as async_sdk:
            with patch("asyncio.to_thread", wraps=asyncio.to_thread) as spy_to_thread:
                app = ImednetTuiApp(
                    async_sdk,
                    study_key="ST_RES",
                    batch_ids=["B_CLEAN", "B_PARTIAL", "B_RES_ERR"],
                )
                async with app.run_test(size=(110, 32)) as pilot:
                    await _settle(pilot)
                    job_monitor = app.screen.query_one(JobMonitor)
                    job_table = app.screen.query_one("#job_table", JobTable)
                    log_viewer = app.screen.query_one(LogViewer)

                    assert spy_to_thread.call_count == 0
                    assert clean_res_route.call_count == 1
                    assert partial_res_route.call_count == 1
                    assert err_res_route.call_count == 1

                    # Row 0: B_CLEAN -> Total=2, Failed=0, styled green
                    row_clean = job_table.get_row_at(0)
                    assert [str(c) for c in row_clean[:5]] == [
                        "B_CLEAN",
                        "COMPLETED",
                        "100%",
                        "2",
                        "0",
                    ]
                    assert row_clean[1].style == "green"

                    # Row 1: B_PARTIAL -> Total=3, Failed=2, styled red despite COMPLETED state
                    row_partial = job_table.get_row_at(1)
                    assert [str(c) for c in row_partial[:5]] == [
                        "B_PARTIAL",
                        "COMPLETED",
                        "100%",
                        "3",
                        "2",
                    ]
                    assert row_partial[1].style == "red"

                    # Row 2: B_RES_ERR -> result_error populated & redacted, styled red
                    row_err = job_table.get_row_at(2)
                    assert str(row_err[0]) == "B_RES_ERR"
                    assert str(row_err[1]) == "COMPLETED"
                    assert row_err[1].style == "red"

                    summary_renderable = job_monitor.render()
                    spans_text = [
                        (
                            summary_renderable.plain[span.start : span.end],
                            str(span.style),
                        )
                        for span in summary_renderable.spans
                    ]
                    completed_styles = [
                        style for label, style in spans_text if label == "COMPLETED"
                    ]
                    assert completed_styles == ["green", "red", "red"]

                    log_dump = "\n".join(log_viewer.lines)
                    assert "B_RES_ERR" in log_dump
                    assert marker_val not in log_dump
                    assert "***" in log_dump

                    # Manual refresh must NOT re-fetch resultUrl for already-fetched terminal jobs
                    await pilot.press("r")
                    await _settle(pilot)
                    assert clean_res_route.call_count == 1
                    assert partial_res_route.call_count == 1
                    assert err_res_route.call_count == 1

        # Verify synchronous ImednetSDK also populates resultUrl counts off the main thread
        with ImednetSDK(
            api_key="test-key",  # pragma: allowlist secret
            security_key="test-sec",  # pragma: allowlist secret
            base_url=base_url,
            retries=0,
        ) as sync_sdk:
            sync_app = ImednetTuiApp(
                sync_sdk,
                study_key="ST_RES",
                batch_ids=["B_PARTIAL"],
            )
            async with sync_app.run_test(size=(100, 30)) as pilot:
                await _settle(pilot)
                sync_table = sync_app.screen.query_one("#job_table", JobTable)
                sync_row = sync_table.get_row_at(0)
                assert [str(c) for c in sync_row[:5]] == [
                    "B_PARTIAL",
                    "COMPLETED",
                    "100%",
                    "3",
                    "2",
                ]
                assert sync_row[1].style == "red"
                assert partial_res_route.call_count == 2


@pytest.mark.asyncio
async def test_result_url_edge_cases_skip_fetch_safely() -> None:
    """Skip resultUrl fetch when inline results exist, URL is empty/<...>, or mock lacks client."""
    # Case 1: Bare MagicMock facade without client or jobs endpoint never auto-creates mocks
    bare_sdk = MagicMock(spec=AsyncImednetFacade)
    bare_sdk.async_get_job = AsyncMock(
        side_effect=[
            JobStatus(
                batch_id="B-INLINE",
                state=JobState.COMPLETED,
                result_url="https://edc.test.imednetapi.com/results/inline",
                results=[{"status": "SUCCESS"}],
            ),
            JobStatus(
                batch_id="B-EMPTY",
                state=JobState.COMPLETED,
                result_url="   ",
            ),
            JobStatus(
                batch_id="B-PLACEHOLDER",
                state=JobState.COMPLETED,
                result_url="<resultUrl>",
            ),
            JobStatus(
                batch_id="B-NO-CLIENT",
                state=JobState.COMPLETED,
                result_url="https://edc.test.imednetapi.com/results/noclient",
            ),
        ]
    )
    monitor = JobMonitor(bare_sdk)
    monitor.current_study_key = "STUDY-EDGE"
    monitor.track_jobs(
        ["B-INLINE", "B-EMPTY", "B-PLACEHOLDER", "B-NO-CLIENT"],
        refresh=False,
    )
    await monitor.refresh_jobs()
    rendered = str(monitor.render())
    assert "Active & Recent Jobs (4)" in rendered
    assert "B-INLINE" in rendered
    assert "total=1, failed=0" in rendered

    # Case 2: Configured jobs._fetch_result_url spy is NOT called for inline, empty, or <...> URLs
    fetch_spy = AsyncMock(return_value=[{"status": "SUCCESS"}])
    configured_sdk = MagicMock(spec=AsyncImednetFacade)
    configured_sdk.jobs = MagicMock(_fetch_result_url=fetch_spy)
    configured_sdk.async_get_job = AsyncMock(
        side_effect=[
            JobStatus(
                batch_id="B-HAS-RESULTS",
                state=JobState.COMPLETED,
                result_url="https://edc.test.imednetapi.com/results/1",
                results=[{"status": "SUCCESS"}, {"status": "FAILED"}],
            ),
            JobStatus(
                batch_id="B-TEMPLATE",
                state=JobState.COMPLETED,
                result_url="<https://placeholder>",
            ),
            JobStatus(
                batch_id="B-NONE",
                state=JobState.COMPLETED,
                result_url=None,
            ),
        ]
    )
    monitor2 = JobMonitor(configured_sdk)
    monitor2.current_study_key = "STUDY-EDGE-2"
    monitor2.track_jobs(["B-HAS-RESULTS", "B-TEMPLATE", "B-NONE"], refresh=False)
    await monitor2.refresh_jobs()
    assert fetch_spy.await_count == 0


@pytest.mark.asyncio
async def test_job_transition_guards_and_exclusive_refresh_worker_group() -> None:
    """Guard job cache against state regressions and route refreshes via one exclusive group."""
    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[
            MagicMock(study_key="STUDY-GUARD-1", study_name="Guard Study 1"),
            MagicMock(study_key="STUDY-GUARD-2", study_name="Guard Study 2"),
        ]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])

    # Verify unit-level transition guards against regression and missing states
    poll_step = 0

    async def _guard_get_job(_sk: str, batch_id: str) -> Any:
        nonlocal poll_step
        poll_step += 1
        if poll_step == 1:
            return JobStatus(batch_id=batch_id, state=JobState.RUNNING, progress=50)
        if poll_step == 2:
            # Regression from RUNNING -> QUEUED must be rejected by transition guard
            return JobStatus(batch_id=batch_id, state=JobState.QUEUED, progress=10)
        if poll_step == 3:
            # Missing state after RUNNING must be rejected without clobbering RUNNING
            state_less = MagicMock()
            state_less.batch_id = batch_id
            state_less.state = None
            state_less.is_terminal = False
            return state_less
        return JobStatus(
            batch_id=batch_id,
            state=JobState.COMPLETED,
            progress=100,
            record_total_count=6,
            record_failure_count=0,
        )

    sdk.async_get_job = AsyncMock(side_effect=_guard_get_job)

    unit_monitor = JobMonitor(sdk)
    unit_monitor.current_study_key = "STUDY-GUARD-1"
    unit_monitor.track_jobs(["BATCH-GUARD"], refresh=False)

    await unit_monitor.refresh_jobs()
    assert "RUNNING" in str(unit_monitor.render())

    # Poll 2: RUNNING -> QUEUED rejected; RUNNING preserved with warning
    await unit_monitor.refresh_jobs()
    rendered_after_reg = str(unit_monitor.render())
    assert "RUNNING" in rendered_after_reg
    assert "QUEUED - Job" not in rendered_after_reg
    assert "Warning:" in rendered_after_reg

    # Poll 3: RUNNING -> state=None rejected; RUNNING preserved with warning
    await unit_monitor.refresh_jobs()
    rendered_after_missing = str(unit_monitor.render())
    assert "RUNNING" in rendered_after_missing
    assert "Ignoring missing job state update" in rendered_after_missing

    # Poll 4: RUNNING -> COMPLETED succeeds and locks terminal entry
    await unit_monitor.refresh_jobs()
    assert "COMPLETED" in str(unit_monitor.render())

    # Verify Pilot worker scheduling routes mount, track_jobs, set_study, timer, and 'r'
    # through JobMonitor's single exclusive='True', group='refresh-jobs' worker group.
    sdk.async_get_job = AsyncMock(
        return_value=JobStatus(batch_id="B-WORK", state=JobState.RUNNING, progress=20)
    )
    app = ImednetTuiApp(sdk, study_key="STUDY-GUARD-1", batch_ids=["B-WORK"])
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        job_monitor = app.screen.query_one(JobMonitor)

        recorded_calls: list[tuple[bool, str]] = []
        orig_run_worker = job_monitor.run_worker

        def _spy_run_worker(coro: Any, *args: Any, **kwargs: Any) -> Any:
            recorded_calls.append((bool(kwargs.get("exclusive")), str(kwargs.get("group", ""))))
            return orig_run_worker(coro, *args, **kwargs)

        with patch.object(job_monitor, "run_worker", side_effect=_spy_run_worker):
            job_monitor.track_job("B-WORK-2")
            job_monitor.set_study("STUDY-GUARD-2")
            if job_monitor.timer is not None:
                job_monitor.schedule_refresh()
            await pilot.press("r")
            await _settle(pilot)

        assert len(recorded_calls) >= 4
        assert all(
            exclusive is True and group == JobMonitor.REFRESH_WORKER_GROUP
            for exclusive, group in recorded_calls
        )


@pytest.mark.asyncio
async def test_log_viewer_captures_imednet_and_tui_logs_redacts_secrets_and_restores_loggers() -> (
    None
):
    """Attach TextualLogHandler to imednet & imednet_tui, redact secrets, and restore on unmount."""
    raw_api_key = "imn-live-api-key-778899"  # pragma: allowlist secret
    raw_bearer = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload"  # pragma: allowlist secret
    raw_query_pw = "Hunter2QuerySecret!"  # pragma: allowlist secret

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.config = MagicMock(base_url="https://edc.test.imednetapi.com")
    sdk.async_get_studies = AsyncMock(
        return_value=[MagicMock(study_key="STUDY-LOG", study_name="Log Study")]
    )
    sdk.async_get_sites = AsyncMock(
        side_effect=RuntimeError(
            f"Site error for Authorization: Bearer {raw_bearer} url=https://api.example/sites?password={raw_query_pw}"
        )
    )
    sdk.async_get_subjects = AsyncMock(
        side_effect=RuntimeError({"message": "Subject failure", "x-api-key": raw_api_key})
    )
    sdk.async_get_job = AsyncMock(
        side_effect=RuntimeError(
            f"Job poll failed with api_key={raw_api_key} and token={raw_query_pw}"
        )
    )

    imednet_logger = logging.getLogger("imednet")
    tui_logger = logging.getLogger("imednet_tui")
    orig_imednet_state = (
        imednet_logger.level,
        imednet_logger.propagate,
        list(imednet_logger.handlers),
    )
    orig_tui_state = (
        tui_logger.level,
        tui_logger.propagate,
        list(tui_logger.handlers),
    )
    imednet_logger.setLevel(logging.WARNING)
    imednet_logger.propagate = True
    tui_logger.setLevel(logging.ERROR)
    tui_logger.propagate = True

    notifications: list[str] = []

    try:
        app = ImednetTuiApp(
            sdk,
            study_key="STUDY-LOG",
            batch_ids=["BATCH-SECRET-ERR"],
        )
        orig_notify = app.notify

        def _capture_notify(message: Any, *args: Any, **kwargs: Any) -> None:
            notifications.append(str(message))
            orig_notify(message, *args, **kwargs)

        with patch.object(app, "notify", side_effect=_capture_notify):
            async with app.run_test(size=(100, 30)) as pilot:
                await _settle(pilot)
                log_viewer = app.screen.query_one(LogViewer)
                job_monitor = app.screen.query_one(JobMonitor)

                # While mounted, both loggers have TextualLogHandler attached and propagate=False
                assert imednet_logger.propagate is False
                assert tui_logger.propagate is False
                assert imednet_logger.level == logging.INFO
                assert tui_logger.level == logging.INFO
                assert any(isinstance(h, TextualLogHandler) for h in imednet_logger.handlers)
                assert any(isinstance(h, TextualLogHandler) for h in tui_logger.handlers)

                # Emit records directly on both loggers with raw credentials
                imednet_logger.info(
                    "SDK request headers={'x-api-key': '%s'} Authorization: Bearer %s",
                    raw_api_key,
                    raw_bearer,
                )
                imednet_logger.info(
                    "SDK structured headers=%s",
                    {"x-imn-security-key": raw_api_key},
                )
                tui_logger.warning(
                    "TUI poll https://edc.test.imednetapi.com/jobs?token=%s",
                    raw_query_pw,
                )
                await pilot.pause()

                combined_logs = "\n".join(log_viewer.lines)
                assert "imednet - INFO" in combined_logs
                assert "imednet_tui - WARNING" in combined_logs
                assert "BATCH-SECRET-ERR" in combined_logs
                assert "***" in combined_logs

                for secret_val in (raw_api_key, raw_bearer, raw_query_pw):
                    assert secret_val not in combined_logs
                    assert all(secret_val not in note for note in notifications)
                    assert secret_val not in str(job_monitor.render())

        # On unmount, handlers are detached and level + propagate are restored on both loggers
        assert imednet_logger.level == logging.WARNING
        assert imednet_logger.propagate is True
        assert tui_logger.level == logging.ERROR
        assert tui_logger.propagate is True
        assert [
            h
            for h in imednet_logger.handlers
            if isinstance(h, TextualLogHandler) and h not in orig_imednet_state[2]
        ] == []
        assert [
            h
            for h in tui_logger.handlers
            if isinstance(h, TextualLogHandler) and h not in orig_tui_state[2]
        ] == []
    finally:
        imednet_logger.setLevel(orig_imednet_state[0])
        imednet_logger.propagate = orig_imednet_state[1]
        tui_logger.setLevel(orig_tui_state[0])
        tui_logger.propagate = orig_tui_state[1]


@pytest.mark.asyncio
async def test_app_env_subtitle_resolution_simulation_client_and_default_fallback() -> None:
    """Resolve Env subtitle from simulation_mode, client base_url, or DEFAULT_BASE_URL."""
    # 1. Sync simulation SDK -> Env: Simulation
    with ImednetSDK(simulation_mode=True) as sim_sdk:
        sim_app = ImednetTuiApp(sim_sdk)
        async with sim_app.run_test() as pilot:
            await pilot.pause()
            assert sim_app.sub_title == "Env: Simulation"

    # 2. Async simulation SDK -> Env: Simulation
    async with AsyncImednetSDK(simulation_mode=True) as async_sim_sdk:
        async_sim_app = ImednetTuiApp(async_sim_sdk)
        async with async_sim_app.run_test() as pilot:
            await pilot.pause()
            assert async_sim_app.sub_title == "Env: Simulation"

    # 3. Client base_url when config.base_url is None (e.g. default ImednetSDK construction)
    class _ClientUrlFacade:
        def __init__(self) -> None:
            self.config = MagicMock(base_url=None, simulation_mode=False)
            self.client = MagicMock(
                base_url="https://staging.imednetapi.com",
                simulation_mode=False,
            )

        @property
        def _base_url(self) -> str:
            raise AssertionError("Must not read private _base_url")

        async def async_get_studies(self, **_filters: Any) -> list[Any]:
            return []

    client_sdk: Any = _ClientUrlFacade()
    client_app = ImednetTuiApp(client_sdk)
    async with client_app.run_test() as pilot:
        await pilot.pause()
        assert client_app.sub_title == "Env: https://staging.imednetapi.com"

    # 4. Bare facade without config or client -> DEFAULT_BASE_URL (never None or Unknown)
    class _BareFacade:
        @property
        def _base_url(self) -> str:
            raise AssertionError("Must not read private _base_url")

        async def async_get_studies(self, **_filters: Any) -> list[Any]:
            return []

    bare_sdk: Any = _BareFacade()
    bare_app = ImednetTuiApp(bare_sdk)
    async with bare_app.run_test() as pilot:
        await pilot.pause()
        assert bare_app.sub_title == f"Env: {DEFAULT_BASE_URL}"
        assert "None" not in bare_app.sub_title
        assert "Unknown" not in bare_app.sub_title


@pytest.mark.asyncio
async def test_job_monitor_preserves_failure_warning_and_toggles_inline_detail_panel() -> None:
    """Preserve JobFailedError warnings and inspect sanitized result/record errors inline."""
    from textual.color import Color

    from imednet.models.jobs import JobState, JobStatus
    from imednet.models.sites import Site
    from imednet.models.studies import Study

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.async_get_studies = AsyncMock(
        return_value=[Study(study_key="STUDY-DETAIL", study_name="Detail Study")]
    )
    sdk.async_get_sites = AsyncMock(
        return_value=[Site(study_key="STUDY-DETAIL", site_id=10, site_name="Site Ten")]
    )
    sdk.async_get_subjects = AsyncMock(return_value=[])

    res_err_job = JobStatus(
        job_id="BATCH-RES-ERR",
        batch_id="BATCH-RES-ERR",
        state=JobState.COMPLETED,
        progress=100,
        total_records=5,
        failed_records=0,
        result_url="https://api.example.com/results/BATCH-RES-ERR",
    )
    rec_err_job = JobStatus(
        job_id="BATCH-REC-ERR",
        batch_id="BATCH-REC-ERR",
        state=JobState.COMPLETED,
        progress=100,
        total_records=4,
        failed_records=2,
        results=[
            {
                "status": "FAILED",
                "recordId": 42,
                "subjectKey": "SUBJ-007",
                "formKey": "AE",
                "patient_name": "Jane Doe",
                "api_key": "sk-secret-inline",
                "error": "Invalid onset date for ssn=123-45-6789",
            },
            {
                "status": "ERROR",
                "record_id": 43,
                "subject_key": "SUBJ-008",
                "errors": ["Missing AE severity for email=jane@example.com"],
            },
        ],
    )
    clean_job = JobStatus(
        job_id="BATCH-OK",
        batch_id="BATCH-OK",
        state=JobState.COMPLETED,
        progress=100,
        total_records=3,
        failed_records=0,
    )

    jobs_by_id = {
        "BATCH-RES-ERR": res_err_job,
        "BATCH-REC-ERR": rec_err_job,
        "BATCH-OK": clean_job,
    }
    sdk.async_get_job = AsyncMock(side_effect=lambda _sk, jid: jobs_by_id[jid])
    sdk.async_client = MagicMock()
    sdk.async_client.fetch_external_url = AsyncMock(
        side_effect=RuntimeError(
            "502 Bad Gateway x-api-key: raw-res-secret patient_name=Alice Smith"
        )
    )

    app = ImednetTuiApp(
        sdk,
        high_contrast=True,
        study_key="STUDY-DETAIL",
        batch_ids=["BATCH-RES-ERR", "BATCH-REC-ERR", "BATCH-OK"],
    )
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()

        dashboard = app.screen
        assert isinstance(dashboard, DashboardScreen)
        job_monitor = dashboard.query_one(JobMonitor)
        notice = job_monitor.query_one("#job_monitor_notice", Static)
        job_table = job_monitor.query_one("#job_table", JobTable)
        detail_panel = job_monitor.query_one("#job_detail_panel", Static)

        rendered_summary = str(job_monitor.render())
        assert "502 Bad Gateway" in rendered_summary
        assert "raw-res-secret" not in rendered_summary
        assert "Alice Smith" not in rendered_summary
        assert "***MASKED***" in rendered_summary
        assert "failed_records=2" in rendered_summary
        assert "Jane Doe" not in rendered_summary
        assert "123-45-6789" not in rendered_summary

        notice_text = str(notice.render())
        assert "Warning:" in notice_text
        assert "BATCH-RES-ERR" in notice_text
        assert "BATCH-REC-ERR" in notice_text
        assert "raw-res-secret" not in notice_text
        assert "Alice Smith" not in notice_text
        assert "Jane Doe" not in notice_text

        # Detail panel is hidden initially (no modal overlays)
        assert not detail_panel.has_class("visible")
        assert len(app.screen_stack) <= 2

        # Activate a site filter so we can verify Escape closes #job_detail_panel first
        dashboard.on_site_list_site_selected(SiteList.SiteSelected("10", "Site Ten"))
        await pilot.pause()
        assert dashboard.selected_site_id == "10"

        # Press Enter on row 0 (BATCH-RES-ERR) -> opens inline #job_detail_panel
        job_table.focus()
        job_table.move_cursor(row=0)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert detail_panel.has_class("visible")
        assert detail_panel.styles.background == Color.parse("#000000")
        assert detail_panel.styles.color == Color.parse("#FFFFFF")
        panel_text_res = str(detail_panel.render())
        assert "BATCH-RES-ERR" in panel_text_res
        assert "Result Error:" in panel_text_res
        assert "502 Bad Gateway" in panel_text_res
        assert "raw-res-secret" not in panel_text_res
        assert "Alice Smith" not in panel_text_res
        assert "***MASKED***" in panel_text_res

        # Move to row 1 (BATCH-REC-ERR) and press Space -> switches inline panel to BATCH-REC-ERR
        job_table.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()

        assert detail_panel.has_class("visible")
        panel_text_rec = str(detail_panel.render())
        assert "BATCH-REC-ERR" in panel_text_rec
        assert "Record Errors (2):" in panel_text_rec
        assert "recordId=42" in panel_text_rec
        assert "subjectKey=SUBJ-007" in panel_text_rec
        assert "SUBJ-008" in panel_text_rec
        assert "Jane Doe" not in panel_text_rec
        assert "123-45-6789" not in panel_text_rec
        assert "jane@example.com" not in panel_text_rec
        assert "sk-secret-inline" not in panel_text_rec
        assert "***MASKED***" in panel_text_rec

        # Pressing Space again on the same row toggles #job_detail_panel closed
        await pilot.press("space")
        await pilot.pause()
        assert not detail_panel.has_class("visible")

        # Re-open on clean job (row 2) -> shows "No errors reported."
        job_table.move_cursor(row=2)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert detail_panel.has_class("visible")
        assert "No errors reported." in str(detail_panel.render())

        # First Escape closes #job_detail_panel without clearing active site filter
        await pilot.press("escape")
        await pilot.pause()
        assert not detail_panel.has_class("visible")
        assert dashboard.selected_site_id == "10"

        # Second Escape clears active site filter
        await pilot.press("escape")
        await pilot.pause()
        assert dashboard.selected_site_id is None


@pytest.mark.asyncio
async def test_batch_input_preflight_validation_and_terminal_path_traversal_error() -> None:
    """Reject invalid #batch_id_input segments before polling and mark PathTraversalValidationError terminal."""
    from imednet.models.studies import Study
    from imednet.spi.utils import PathTraversalValidationError

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.async_get_studies = AsyncMock(
        return_value=[Study(study_key="STUDY-SAFE", study_name="Safe Study")]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])
    sdk.async_get_job = AsyncMock()

    notifications: list[tuple[str, str]] = []
    app = ImednetTuiApp(sdk, study_key="STUDY-SAFE")
    orig_notify = app.notify

    def _capture_notify(message: Any, *args: Any, **kwargs: Any) -> Any:
        notifications.append((str(message), str(kwargs.get("severity", "information"))))
        return orig_notify(message, *args, **kwargs)

    app.notify = _capture_notify  # type: ignore[method-assign]

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        job_monitor = app.screen.query_one(JobMonitor)
        notice = job_monitor.query_one("#job_monitor_notice", Static)
        batch_input = job_monitor.query_one("#batch_id_input", Input)
        job_table = job_monitor.query_one("#job_table", JobTable)

        for bad_token in ("../bad-segment", "BATCH?foo=1"):
            notifications.clear()
            batch_input.focus()
            batch_input.value = bad_token
            await pilot.press("enter")
            await pilot.pause()

            assert batch_input.value == ""
            assert job_table.row_count == 0
            assert "Validation Warning:" in str(notice.render())
            assert any(sev == "warning" for _, sev in notifications)
            sdk.async_get_job.assert_not_called()

        # Also verify _poll_tracked_entry marks PathTraversalValidationError terminal without re-polling
        sdk.async_get_job = AsyncMock(
            side_effect=PathTraversalValidationError("batch_id must not contain '..'")
        )
        job_monitor.track_jobs(["../forced-bad"], study_key="STUDY-SAFE", refresh=False)
        await job_monitor.refresh_jobs()
        assert sdk.async_get_job.call_count == 1
        assert [str(c) for c in job_table.get_row_at(0)[:2]] == ["../forced-bad", "INVALID"]
        assert "batch_id" in str(notice.render())
        assert job_monitor.is_polling_active is False

        # Neither interval refresh nor manual retry_placeholders refresh re-polls it
        await job_monitor.refresh_jobs()
        await job_monitor.refresh_jobs(retry_placeholders=True)
        assert sdk.async_get_job.call_count == 1


@pytest.mark.asyncio
async def test_unassigned_cli_batches_render_queued_and_poll_on_study_selection() -> None:
    """Show CLI batch_ids as QUEUED when study_key=None and poll upon Study selection."""
    from imednet.models.jobs import JobState, JobStatus
    from imednet.models.studies import Study

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.async_get_studies = AsyncMock(
        return_value=[Study(study_key="STUDY-Q", study_name="Queued Study")]
    )
    sdk.async_get_sites = AsyncMock(return_value=[])
    sdk.async_get_subjects = AsyncMock(return_value=[])
    sdk.async_get_job = AsyncMock(
        side_effect=lambda _sk, jid: JobStatus(
            job_id=jid,
            batch_id=jid,
            state=JobState.COMPLETED,
            progress=100,
            total_records=8,
            failed_records=0,
        )
    )

    app = ImednetTuiApp(sdk, batch_ids=["BATCH-01", "BATCH-02"], study_key=None)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        dashboard = app.screen
        assert isinstance(dashboard, DashboardScreen)
        job_monitor = dashboard.query_one(JobMonitor)
        notice = job_monitor.query_one("#job_monitor_notice", Static)
        job_table = job_monitor.query_one("#job_table", JobTable)
        detail_panel = job_monitor.query_one("#job_detail_panel", Static)

        assert job_table.row_count == 2
        assert [str(c) for c in job_table.get_row_at(0)[:2]] == ["BATCH-01", "QUEUED"]
        assert [str(c) for c in job_table.get_row_at(1)[:2]] == ["BATCH-02", "QUEUED"]
        assert "Queued Jobs (2)" in str(notice.render())
        assert "BATCH-01" in str(notice.render())
        assert job_monitor.is_polling_active is False
        sdk.async_get_job.assert_not_called()

        # Inspecting a QUEUED job opens #job_detail_panel cleanly
        job_table.focus()
        job_table.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()
        assert detail_panel.has_class("visible")
        assert "BATCH-01 [QUEUED]" in str(detail_panel.render())

        # Untracking BATCH-02 while queued updates the table to 1 queued row
        job_table.move_cursor(row=1)
        await pilot.press("delete")
        await pilot.pause()
        assert job_table.row_count == 1
        assert str(job_table.get_row_at(0)[0]) == "BATCH-01"

        # Selecting a Study binds and polls BATCH-01 immediately
        study_list = dashboard.query_one(StudyList)
        study_list.focus()
        study_list.index = 0
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        sdk.async_get_job.assert_called_once_with("STUDY-Q", "BATCH-01")
        assert job_table.row_count == 1
        assert [str(c) for c in job_table.get_row_at(0)[:2]] == ["BATCH-01", "COMPLETED"]


@pytest.mark.asyncio
async def test_textual_log_handler_and_sanitize_text_mask_phi_and_preserve_relational_keys() -> (
    None
):
    """Mask Subject PHI dicts and key-value substrings to ***MASKED*** while preserving relational keys."""
    from imednet.spi.utils import global_sensitivity_registry
    from imednet_tui.app import LogViewer, _sanitize_text

    sdk = MagicMock(spec=AsyncImednetFacade)
    sdk.async_get_studies = AsyncMock(return_value=[])

    global_sensitivity_registry.add_sensitive_key("custom_mrn")
    try:
        app = ImednetTuiApp(sdk)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            imednet_logger = logging.getLogger("imednet")

            # 1. Structured dict payload via logger
            imednet_logger.info(
                {
                    "study_key": "STUDY-REL-1",
                    "subject_key": "SUBJ-REL-2",
                    "record_id": "REC-REL-3",
                    "usubjid": "USUBJ-REL-4",
                    "subjid": "SUBJID-REL-5",
                    "studyid": "STUDYID-REL-6",
                    "patient_name": "Jane Doe",
                    "ssn": "123-45-6789",
                    "dob": "1980-04-15",
                    "email": "jane.doe@example.com",
                    "brthdtc": "1980-04-15",
                    "custom_mrn": "MRN-998877",
                }
            )

            # 2. Formatted key-value text payload via logger
            imednet_logger.warning(
                "Subject audit study_key=STUDY-REL-1 subject_key=SUBJ-REL-2 "
                "patient_name='John Roe' ssn=987-65-4321, dob: 1975-12-31; "
                "email=john.roe@example.org brthdtc=1975-12-31 custom_mrn=\"MRN-112233\""
            )
            await pilot.pause()

            log_viewer = app.screen.query_one(LogViewer)
            rendered_logs = "\n".join(str(line) for line in log_viewer.lines)

            assert "***MASKED***" in rendered_logs
            for leaked_phi in (
                "Jane Doe",
                "John Roe",
                "123-45-6789",
                "987-65-4321",
                "1980-04-15",
                "1975-12-31",
                "jane.doe@example.com",
                "john.roe@example.org",
                "MRN-998877",
                "MRN-112233",
            ):
                assert leaked_phi not in rendered_logs

            for relational_id in (
                "STUDY-REL-1",
                "SUBJ-REL-2",
                "REC-REL-3",
                "USUBJ-REL-4",
                "SUBJID-REL-5",
                "STUDYID-REL-6",
            ):
                assert relational_id in rendered_logs

            direct = _sanitize_text(
                RuntimeError(
                    "Failed record_id=77 patient_name= Clara Oswald & email=clara@tardis.uk"
                )
            )
            assert "record_id=77" in direct
            assert "Clara Oswald" not in direct
            assert "clara@tardis.uk" not in direct
            assert "***MASKED***" in direct
    finally:
        global_sensitivity_registry.remove_sensitive_key("custom_mrn")
