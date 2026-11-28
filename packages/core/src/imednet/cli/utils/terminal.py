"""Centralized terminal capability policy for CLI and SPI extensions."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import sys
from pathlib import Path
from typing import Any

NON_INTERACTIVE_ENV = "IMEDNET_NON_INTERACTIVE"
OUTPUT_FORMAT_ENV = "IMEDNET_CLI_OUTPUT_FORMAT"
SIMULATION_MODE_ENV = "IMEDNET_SIMULATION_MODE"
NO_COLOR_ENV = "NO_COLOR"

_TRUTHY_NON_INTERACTIVE = frozenset({"1", "true", "yes"})
_TERMINFO_CACHE: dict[tuple[str, str, str], bool] = {}
_CURSES_LIB_RESOLVED = False
_CURSES_LIB: Any = None


def _load_ncurses_lib() -> Any:
    """Lazily load libtinfo/libncurses for terminfo capability probing."""
    global _CURSES_LIB_RESOLVED, _CURSES_LIB
    if _CURSES_LIB_RESOLVED:
        return _CURSES_LIB
    _CURSES_LIB_RESOLVED = True
    for lib_name in ("tinfo", "ncursesw", "ncurses", "curses"):
        try:
            resolved = ctypes.util.find_library(lib_name)
            if not resolved:
                continue
            lib = ctypes.CDLL(resolved)
            if hasattr(lib, "setupterm"):
                _CURSES_LIB = lib
                return lib
        except OSError:
            continue
    return None


def _iter_terminfo_dirs(terminfo_env: str, terminfo_dirs_env: str) -> list[Path]:
    """Return candidate terminfo database directories in standard lookup order."""
    candidates: list[Path] = []
    if terminfo_env:
        candidates.append(Path(terminfo_env))
    with contextlib.suppress(RuntimeError, OSError):
        candidates.append(Path.home() / ".terminfo")
    if terminfo_dirs_env:
        for entry in terminfo_dirs_env.split(os.pathsep):
            cleaned = entry.strip()
            candidates.append(Path(cleaned) if cleaned else Path("/usr/share/terminfo"))
    for sys_dir in (
        "/etc/terminfo",
        "/lib/terminfo",
        "/usr/share/terminfo",
        "/usr/lib/terminfo",
        "/usr/share/lib/terminfo",
        "/usr/local/share/terminfo",
        "/usr/local/lib/terminfo",
        "/opt/homebrew/share/terminfo",
        f"{sys.prefix}/share/terminfo",
        f"{sys.base_prefix}/share/terminfo",
    ):
        candidates.append(Path(sys_dir))
    return candidates


def _has_terminfo_support(term: str) -> bool:
    """Return True when ``term`` resolves to a valid terminfo entry."""
    if not term or term.lower() == "dumb" or "/" in term or "\\" in term or "\x00" in term:
        return False

    terminfo_env = os.environ.get("TERMINFO", "").strip()
    terminfo_dirs_env = os.environ.get("TERMINFO_DIRS", "").strip()
    cache_key = (term, terminfo_env, terminfo_dirs_env)
    cached = _TERMINFO_CACHE.get(cache_key)
    if cached is not None:
        return cached

    first_char = term[0]
    hex_lower = f"{ord(first_char):02x}"
    hex_upper = f"{ord(first_char):02X}"
    saw_terminfo_dir = False

    for base_dir in _iter_terminfo_dirs(terminfo_env, terminfo_dirs_env):
        try:
            if not base_dir.is_dir():
                continue
            saw_terminfo_dir = True
            for subdir in (first_char, hex_lower, hex_upper):
                if (base_dir / subdir / term).is_file():
                    _TERMINFO_CACHE[cache_key] = True
                    return True
        except OSError:
            continue

    lib = _load_ncurses_lib()
    if lib is not None:
        with contextlib.suppress(
            AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError
        ):
            setupterm = lib.setupterm
            setupterm.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            setupterm.restype = ctypes.c_int
            prev_term: Any = None
            has_cur_term = False
            with contextlib.suppress(AttributeError, ValueError, OSError):
                cur_term_ptr = ctypes.c_void_p.in_dll(lib, "cur_term")
                prev_term = cur_term_ptr.value
                has_cur_term = True

            err = ctypes.c_int(0)
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            try:
                rc = setupterm(
                    term.encode("utf-8", errors="surrogateescape"), devnull_fd, ctypes.byref(err)
                )
            finally:
                os.close(devnull_fd)

            supported = rc == 0 and err.value == 1
            if has_cur_term:
                with contextlib.suppress(AttributeError, ValueError, OSError):
                    new_term = ctypes.c_void_p.in_dll(lib, "cur_term").value
                    if new_term and new_term != prev_term and hasattr(lib, "del_curterm"):
                        del_curterm = lib.del_curterm
                        del_curterm.argtypes = [ctypes.c_void_p]
                        del_curterm.restype = ctypes.c_int
                        del_curterm(ctypes.c_void_p(new_term))
                    if hasattr(lib, "set_curterm"):
                        set_curterm = lib.set_curterm
                        set_curterm.argtypes = [ctypes.c_void_p]
                        set_curterm.restype = ctypes.c_void_p
                        set_curterm(ctypes.c_void_p(prev_term))
            _TERMINFO_CACHE[cache_key] = supported
            return supported

    supported_fallback = not saw_terminfo_dir
    _TERMINFO_CACHE[cache_key] = supported_fallback
    return supported_fallback


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
    if os.environ.get(OUTPUT_FORMAT_ENV, "").strip().lower() == "json":
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
    ``TERM`` setting backed by terminfo, and neither non-interactive mode nor
    JSON output mode.
    """
    if is_non_interactive() or is_json_output_mode():
        return False
    term = os.environ.get("TERM", "").strip()
    if not term or term.lower() == "dumb":
        return False
    inp = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    if not (is_tty(out) and is_tty(inp)):
        return False
    return _has_terminfo_support(term)


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
