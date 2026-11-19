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
from textual.widgets import ListItem, ListView, Static

import imednet.spi.cli as spi_cli
import imednet_tui.app as tui_app_mod
import imednet_tui.cli as tui_cli_mod
import imednet_tui.terminal as tui_terminal_mod
from imednet.endpoints.jobs import JobsEndpoint
from imednet.models.jobs import JobState, JobStatus
from imednet.models.subjects import Subject
from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
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
def reset_high_contrast_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent IMEDNET_HIGH_CONTRAST state leaks across tests."""
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)


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


def test_terminal_helper_imported_from_spi_cli() -> None:
    """Verify imednet_tui shares is_interactive_terminal from imednet.spi.cli."""
    assert tui_app_mod.is_interactive_terminal is spi_cli.is_interactive_terminal
    assert tui_cli_mod.is_interactive_terminal is spi_cli.is_interactive_terminal
    assert tui_terminal_mod.is_interactive_terminal is spi_cli.is_interactive_terminal


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
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        assert len(study_list.children) == 1

        study_list.post_message(StudyList.StudySelected("STUDY-ONC-01", "Oncology Phase III"))
        await pilot.pause()

        site_list = app.screen.query_one(SiteList)
        assert len(site_list.children) == 1

        subject_table = app.screen.query_one(SubjectTable)
        assert subject_table.row_count == 2

        job_monitor = app.screen.query_one(JobMonitor)
        assert job_monitor.current_study_key == "STUDY-ONC-01"
        job_monitor.track_job("BATCH-Active-1")
        mock_sdk.async_get_job.return_value = JobStatus(
            batch_id="BATCH-Active-1",
            state=JobState.RUNNING,
        )
        await pilot.pause()

        # Simulate selecting a site to filter subjects via facade site_id parameter
        site_list.post_message(SiteList.SiteSelected("SITE-101", "Memorial Hospital"))
        await pilot.pause()

        assert subject_table.row_count == 1
        assert mock_sdk.async_get_subjects.await_args_list[-1].kwargs.get("site_id") == "SITE-101"

        # Trigger refresh action
        app.action_refresh_data()
        await pilot.pause()
        assert mock_sdk.async_get_studies.await_count >= 2
        assert mock_sdk.async_get_sites.await_count >= 2
        assert mock_sdk.async_get_subjects.await_count >= 3
        assert mock_sdk.async_get_job.await_count >= 2


@pytest.mark.asyncio
async def test_app_list_view_selection_events(mock_sdk: MagicMock) -> None:
    """Test on_list_view_selected event handlers for StudyList and SiteList."""
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        item = study_list.children[0]
        assert isinstance(item, ListItem)

        event = ListView.Selected(study_list, item, 0)
        study_list.on_list_view_selected(event)
        await pilot.pause()

        site_list = app.screen.query_one(SiteList)
        site_item = site_list.children[0]
        assert isinstance(site_item, ListItem)
        site_event = ListView.Selected(site_list, site_item, 0)
        site_list.on_list_view_selected(site_event)
        await pilot.pause()


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

            job_monitor.track_job("SYNC-JOB-1")
            await pilot.pause()
            sync_sdk.get_job.assert_called_with("SYNC-01", "SYNC-JOB-1")
            assert "SYNC-JOB-1" in str(job_monitor.render())

            site_list.post_message(SiteList.SiteSelected("SITE-SYNC", "Sync Site"))
            await pilot.pause()
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
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        study_list.post_message(StudyList.StudySelected("STUDY-ONC-01", "Oncology Phase III"))
        await pilot.pause()

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
        await pilot.pause()

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
            await asyncio.sleep(0.02)
            heartbeat_ticks += 1

    def slow_sync_get_job(study_key: str, batch_id: str) -> JobStatus:
        assert study_key == "STUDY-HEARTBEAT"
        time.sleep(0.18)
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

    async def _get_job_async(study_key: str, batch_id: str) -> JobStatus:
        nonlocal current_concurrent, max_concurrent_seen
        assert study_key == "STUDY-CONCURRENT"
        async with lock:
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
        await asyncio.sleep(0.12)
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

    assert elapsed < 0.35
    assert max_concurrent_seen <= JobMonitor.MAX_CONCURRENT_POLLS
    assert max_concurrent_seen >= 2

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

    def sync_get_job(study_key: str, batch_id: str) -> JobStatus:
        assert study_key == "STUDY-SYNC-CONC"
        time.sleep(0.08)
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

    assert elapsed < 0.25
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
            await pilot.pause()
            assert studies_route.call_count == 1

            study_list = app.screen.query_one(StudyList)
            study_list.post_message(StudyList.StudySelected("LARGE-01", "Large Trial"))
            await pilot.pause()

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
            await pilot.pause()
            await pilot.pause()

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
        await pilot.pause()

        study_list = app.screen.query_one(StudyList)
        assert len(study_list.children) == 100
        assert study_list.truncated is True
        assert "Showing first 100 studies" in study_list.status_notice

        app.action_load_all()
        await pilot.pause()
        await pilot.pause()

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

                # Select site via Enter -> filters subjects by site_id via ImednetFacade
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
                    await pilot.pause()
                    assert isinstance(app.screen, DashboardScreen)
                    dashboard = app.screen

                    study_list = dashboard.query_one(StudyList)
                    assert len(study_list.children) >= 1
                    study_list.focus()
                    await pilot.pause()

                    # Select study via Enter -> awaits AsyncImednetFacade coroutines directly
                    await pilot.press("enter")
                    await pilot.pause()

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
                        await pilot.pause()
                        assert created_job.batch_id in str(job_monitor.render())

                    # Select site via Enter -> filters down to the 1 subject on site 102
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

                    # Refresh via 'r', Load All via 'a', and toggle contrast via 'h'
                    await pilot.press("r")
                    await pilot.pause()
                    assert subject_table.row_count == 1

                    await pilot.press("a")
                    await pilot.pause()
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

        assert sdk.get_sites.call_args.args == ("STUDY.01",)
        assert sdk.get_subjects.call_args.args == ("STUDY.01",)

        site_list = dashboard.query_one(SiteList)
        site_dom_ids = [child.id for child in site_list.children]
        assert site_dom_ids == ["site-SITE_01", "site-SITE_01-2"]

        # Select SITE.01 via Enter
        site_list.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert sdk.get_subjects.call_args.args == ("STUDY.01",)
        assert sdk.get_subjects.call_args.kwargs.get("site_id") == "SITE.01"

        subject_table = dashboard.query_one(SubjectTable)
        assert subject_table.row_count == 1
        assert str(subject_table.get_row_at(0)[0]) == "SUB-01"

        # Move down to colliding SITE_01 and select via Enter
        await pilot.press("down", "enter")
        await pilot.pause()
        assert sdk.get_subjects.call_args.args == ("STUDY.01",)
        assert sdk.get_subjects.call_args.kwargs.get("site_id") == "SITE_01"
        assert str(subject_table.get_row_at(0)[0]) == "SUB-02"

        # Move down in StudyList to colliding STUDY_01 and select via Enter
        study_list.focus()
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
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
                    await pilot.pause()
                    study_view = app.screen.query_one(StudyList)
                    study_view.post_message(StudyList.StudySelected("ASYNC-01", "Async Study"))
                    await pilot.pause()

                    assert subj_route.call_count == 1
                    assert dict(subj_route.calls[0].request.url.params) == {
                        "page": "0",
                        "size": "101",
                    }
                    subj_table = app.screen.query_one(SubjectTable)
                    assert subj_table.row_count == 100
                    assert subj_table.truncated is True

                    await pilot.press("a")
                    await pilot.pause()
                    await pilot.pause()

                    assert subj_table.row_count == 135
                    assert subj_table.truncated is False

                    # Selecting a site filters directly via site_id -> filter=siteId==10
                    site_view = app.screen.query_one(SiteList)
                    site_view.post_message(SiteList.SiteSelected("10", "Async Site"))
                    await pilot.pause()
                    assert subj_table.row_count == 1
                    assert "siteId==10" in subj_route.calls[-1].request.url.params.get("filter", "")

                assert spy_to_thread.call_count == 0
