"""Centralized terminal capability policy for CLI and SPI extensions."""

from __future__ import annotations

import os
import sys
from typing import Any

NON_INTERACTIVE_ENV = "IMEDNET_NON_INTERACTIVE"
OUTPUT_FORMAT_ENV = "IMEDNET_CLI_OUTPUT_FORMAT"
NO_COLOR_ENV = "NO_COLOR"

_TRUTHY_NON_INTERACTIVE = frozenset({"1", "true", "yes"})


def is_tty(stream: Any) -> bool:
    """Return True when ``stream`` is an open interactive TTY."""
    try:
        return bool(stream is not None and hasattr(stream, "isatty") and stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def is_dumb_terminal() -> bool:
    """Return True when ``TERM`` explicitly indicates a dumb terminal."""
    return os.environ.get("TERM", "").strip().lower() == "dumb"


def is_non_interactive() -> bool:
    """Return True when non-interactive CLI mode has been requested."""
    return os.environ.get(NON_INTERACTIVE_ENV, "").strip().lower() in _TRUTHY_NON_INTERACTIVE


def is_json_output_mode() -> bool:
    """Return True when structured JSON output mode is active on stdout."""
    if os.environ.get(OUTPUT_FORMAT_ENV) == "json":
        return True
    try:
        argv = sys.argv
    except Exception:
        return False
    if "--format=json" in argv:
        return True
    return any(arg == "--format" and argv[idx + 1] == "json" for idx, arg in enumerate(argv[:-1]))


def supports_fullscreen_ui(stdin: Any = None, stdout: Any = None) -> bool:
    """Return True when full-screen interactive UIs (such as curses grids) may run.

    Requires interactive TTYs on both ``stdin`` and ``stdout``, a valid non-dumb
    ``TERM`` setting, and neither non-interactive mode nor JSON output mode.
    """
    if is_non_interactive() or is_json_output_mode():
        return False
    term = os.environ.get("TERM", "").strip()
    if not term or term.lower() == "dumb":
        return False
    inp = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    return is_tty(out) and is_tty(inp)


def supports_live_status(stream: Any = None) -> bool:
    """Return True when VT100 cursor/line controls and live spinners may run on ``stream``.

    Defaults to ``sys.stderr``. Suppressed on non-TTY streams and under
    ``--format json``, ``--no-interactive``, ``IMEDNET_NON_INTERACTIVE=1``,
    or ``TERM=dumb``.
    """
    target = sys.stderr if stream is None else stream
    if not is_tty(target):
        return False
    return not (is_non_interactive() or is_dumb_terminal() or is_json_output_mode())


def supports_ansi_color(stream: Any = None) -> bool:
    """Return True when ANSI SGR color sequences may be written to ``stream``.

    Defaults to ``sys.stdout``. Suppressed on non-TTY streams, under
    ``TERM=dumb``, when ``NO_COLOR`` is set to a non-empty string, or on
    ``sys.stdout`` when JSON output mode is active.
    """
    target = sys.stdout if stream is None else stream
    if not is_tty(target):
        return False
    if is_dumb_terminal():
        return False
    if os.environ.get(NO_COLOR_ENV, "") != "":
        return False
    return not (target is sys.stdout and is_json_output_mode())


def supports_interactive_stdin(stdin: Any = None) -> bool:
    """Return True when interactive line prompts may read from ``stdin``."""
    if is_non_interactive():
        return False
    target = sys.stdin if stdin is None else stdin
    return is_tty(target)
