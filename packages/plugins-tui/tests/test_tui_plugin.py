"""Unit tests for the imednet-tui plugin."""

from __future__ import annotations

import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from textual.app import App
from textual.widgets import ListItem, ListView

from imednet_tui.app import (
    ImednetTuiApp,
    JobMonitor,
    SiteList,
    StudyList,
    SubjectTable,
    TextualLogHandler,
    run_tui,
)


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
        study_list.post_message(StudyList.Selected("STUDY-ONC-01", "Oncology Phase III"))
        await pilot.pause()

        site_list = app.screen.query_one(SiteList)
        assert len(site_list.children) == 1

        subject_table = app.screen.query_one(SubjectTable)
        assert subject_table.row_count == 2

        job_monitor = app.screen.query_one(JobMonitor)
        assert job_monitor.current_study_key == "STUDY-ONC-01"

        # Simulate selecting a site to filter subjects
        site_list.post_message(SiteList.Selected("SITE-101", "Memorial Hospital"))
        await pilot.pause()

        assert subject_table.row_count == 1

        # Trigger refresh action
        app.action_refresh_data()
        await pilot.pause()


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

        study_list.post_message(StudyList.Selected("SYNC-01", "Sync Study"))
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

        site_list.post_message(SiteList.Selected("SITE-SYNC", "Sync Site"))
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
        study_list.post_message(StudyList.Selected("STUDY-ONC-01", "Oncology Phase III"))
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
    """Test that high contrast CSS class is added to app when enabled."""
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    app = ImednetTuiApp(mock_sdk)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "high-contrast" in app.classes

        app.action_toggle_high_contrast()
        await pilot.pause()
        assert "high-contrast" not in app.classes


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
    """Test that run_tui starts the app."""
    with patch.object(ImednetTuiApp, "run") as mock_run:
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
