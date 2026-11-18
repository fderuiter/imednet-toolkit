"""Terminal environment and stream interactivity utilities for TUI."""

from __future__ import annotations

import os
import sys


def is_interactive_terminal() -> bool:
    """Determine if stdout and stdin are connected to an active interactive TTY.

    Returns:
        True if stdout and stdin are interactive TTYs and TERM is valid, False otherwise.
    """
    if os.environ.get("IMEDNET_NON_INTERACTIVE") == "1":
        return False
    term = os.environ.get("TERM", "").strip()
    if not term or term == "dumb":
        return False
    try:
        return bool(
            sys.stdout
            and hasattr(sys.stdout, "isatty")
            and sys.stdout.isatty()
            and sys.stdin
            and hasattr(sys.stdin, "isatty")
            and sys.stdin.isatty()
        )
    except (AttributeError, ValueError, OSError):
        return False
