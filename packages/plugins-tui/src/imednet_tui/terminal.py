"""Terminal environment and stream interactivity utilities for TUI."""

from __future__ import annotations

from typing import Any

from imednet.spi.cli import supports_fullscreen_ui


def is_interactive_terminal(stdin: Any = None, stdout: Any = None) -> bool:
    """Return True when stdin/stdout and environment allow launching the full-screen TUI."""
    return supports_fullscreen_ui(stdin=stdin, stdout=stdout)


__all__ = ["is_interactive_terminal"]
