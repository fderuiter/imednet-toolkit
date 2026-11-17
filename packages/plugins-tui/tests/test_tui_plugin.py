"""Unit tests for the imednet-tui plugin."""

from __future__ import annotations

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
    mock_subj1.status = "Enrolled"
    mock_subj1.date_created = "2026-01-10T08:00:00Z"

    mock_subj2 = MagicMock()
    mock_subj2.subject_id = "SUBJ-002"
    mock_subj2.site_id = "SITE-102"
    mock_subj2.status = "Screened"
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
    mock_subj.status = "Active"
    mock_subj.date_created = "2026-02-01"
    sync_sdk.subjects.list = MagicMock(return_value=[mock_subj])
    sync_sdk.subjects.list_by_site = MagicMock(return_value=[mock_subj])

    mock_job = MagicMock()
    mock_job.job_type = "SyncJob"
    mock_job.state = "Running"
    mock_job.date_created = "2026-02-01"
    sync_sdk.jobs.list = MagicMock(return_value=[mock_job])

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
