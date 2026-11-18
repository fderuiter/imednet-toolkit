"""Modular terminal user interface plugin for iMednet."""

from __future__ import annotations

from .app import (
    DashboardScreen,
    ImednetTuiApp,
    JobMonitor,
    LogViewer,
    SiteList,
    StudyList,
    SubjectTable,
    TextualLogHandler,
    is_interactive_terminal,
    run_tui,
)
from .theme import (
    DARK_PALETTE,
    HIGH_CONTRAST_PALETTE,
    LIGHT_PALETTE,
    ThemePalette,
    contrast_ratio,
    format_progress_bar,
    format_status_badge,
    get_active_palette,
    relative_luminance,
)
from .triage_pane import TriagePane, get_default_triage_db_path
from .views import DataDictionaryPane, EntityTablePane
from .workflows_pane import WorkflowsPane, discover_workflow_plugins

__all__ = [
    "DARK_PALETTE",
    "HIGH_CONTRAST_PALETTE",
    "LIGHT_PALETTE",
    "DashboardScreen",
    "DataDictionaryPane",
    "EntityTablePane",
    "ImednetTuiApp",
    "JobMonitor",
    "LogViewer",
    "SiteList",
    "StudyList",
    "SubjectTable",
    "TextualLogHandler",
    "ThemePalette",
    "TriagePane",
    "WorkflowsPane",
    "contrast_ratio",
    "discover_workflow_plugins",
    "format_progress_bar",
    "format_status_badge",
    "get_active_palette",
    "get_default_triage_db_path",
    "is_interactive_terminal",
    "relative_luminance",
    "run_tui",
]
