# pylint: disable=duplicate-code
"""Modular terminal user interface plugin for iMednet."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import (
        DashboardScreen,
        ImednetTuiApp,
        JobDetailPanel,
        JobMonitor,
        JobTable,
        SiteList,
        StudyList,
        SubjectTable,
        is_interactive_terminal,
        run_tui,
    )

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "DashboardScreen": ("imednet_tui.app", "DashboardScreen"),
    "ImednetTuiApp": ("imednet_tui.app", "ImednetTuiApp"),
    "JobDetailPanel": ("imednet_tui.app", "JobDetailPanel"),
    "JobMonitor": ("imednet_tui.app", "JobMonitor"),
    "JobTable": ("imednet_tui.app", "JobTable"),
    "SiteList": ("imednet_tui.app", "SiteList"),
    "StudyList": ("imednet_tui.app", "StudyList"),
    "SubjectTable": ("imednet_tui.app", "SubjectTable"),
    "is_interactive_terminal": ("imednet_tui.terminal", "is_interactive_terminal"),
    "run_tui": ("imednet_tui.app", "run_tui"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve TUI components so CLI discovery avoids importing textual."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy exports."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "DashboardScreen",
    "ImednetTuiApp",
    "JobDetailPanel",
    "JobMonitor",
    "JobTable",
    "SiteList",
    "StudyList",
    "SubjectTable",
    "is_interactive_terminal",
    "run_tui",
]
