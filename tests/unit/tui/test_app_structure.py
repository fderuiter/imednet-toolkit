"""Unit tests for TUI app structure and widgets."""

from unittest.mock import MagicMock

import pytest
from textual.app import App

from imednet.sdk import ImednetSDK
from imednet_tui.app import (
    DashboardScreen,
    ImednetTuiApp,
    JobMonitor,
    SiteList,
    StudyList,
    SubjectTable,
)


@pytest.fixture
def mock_sdk():
    """Fixture providing a mocked SDK."""
    return MagicMock(spec=ImednetSDK)


def test_app_instantiation(mock_sdk):
    """Test that the app can be instantiated with an SDK."""
    app = ImednetTuiApp(sdk=mock_sdk)
    assert app.sdk == mock_sdk
    assert isinstance(app, App)


def test_dashboard_screen_structure(mock_sdk):
    """Test that the dashboard screen has the expected widgets."""
    sl = StudyList(mock_sdk)
    assert sl.sdk == mock_sdk

    sitel = SiteList(mock_sdk)
    assert sitel.sdk == mock_sdk

    sub_table = SubjectTable(mock_sdk)
    assert sub_table.sdk == mock_sdk

    job_mon = JobMonitor(mock_sdk)
    assert job_mon.sdk == mock_sdk

    screen = DashboardScreen(mock_sdk)
    assert screen.sdk == mock_sdk
    assert screen.log_viewer is not None
