"""CLI output formatting and display utilities."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .terminal import is_json_output_mode, supports_ansi_color, supports_live_status

if TYPE_CHECKING:
    from imednet.utils.job_poller import JobStatusEvent

_ANSI_COLORS = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "dim": "\033[2m",
    "bold_magenta": "\033[1;35m",
    "hc_success": "\033[1;38;2;0;255;102;48;2;0;0;0m",
    "hc_warning": "\033[1;38;2;255;255;0;48;2;0;0;0m",
    "hc_danger": "\033[1;38;2;255;153;255;48;2;0;0;0m",
    "hc_16_success": "\033[1;97;40m",
    "hc_16_warning": "\033[30;107m",
    "hc_16_danger": "\033[1;4;97;40m",
    "reset": "\033[0m",
}

_16_COLOR_TERMS: frozenset[str] = frozenset(
    {
        "16color",
        "ansi",
        "conhost",
        "cons25",
        "cygwin",
        "linux",
        "rxvt",
        "screen",
        "vt100",
        "vt220",
        "xterm",
        "xterm-16color",
        "xterm-color",
    }
)

_HC_16_FALLBACK_MAP: dict[str, str] = {
    "hc_success": "hc_16_success",
    "hc_warning": "hc_16_warning",
    "hc_danger": "hc_16_danger",
}


def _is_16_color_terminal() -> bool:
    """Return True when the active terminal environment only supports 16 ANSI colors."""
    colorterm = os.environ.get("COLORTERM", "").strip().lower()
    if colorterm in {"truecolor", "24bit"}:
        return False
    if colorterm in {"16", "16color", "ansi"}:
        return True
    term = os.environ.get("TERM", "").strip().lower()
    if not term:
        return False
    if any(token in term for token in ("256color", "truecolor", "24bit", "-direct")):
        return False
    return term in _16_COLOR_TERMS or term.endswith(("-16color", "-color"))


_STATUS_COLOR_MAP = {
    "active": "green",
    "success": "green",
    "ok": "green",
    "completed": "green",
    "complete": "green",
    "open": "green",
    "approved": "green",
    "verified": "green",
    "pending": "yellow",
    "processing": "yellow",
    "running": "yellow",
    "queued": "yellow",
    "suspended": "yellow",
    "hold": "yellow",
    "incomplete": "yellow",
    "initiated": "yellow",
    "answered": "yellow",
    "inactive": "red",
    "closed": "red",
    "error": "red",
    "fail": "red",
    "failed": "red",
    "cancelled": "red",
    "timeout": "red",
    "rejected": "red",
    "terminated": "red",
    "withdrawn": "red",
}

_HC_STATUS_COLOR_MAP = {
    "active": "hc_success",
    "success": "hc_success",
    "ok": "hc_success",
    "completed": "hc_success",
    "complete": "hc_success",
    "open": "hc_success",
    "approved": "hc_success",
    "verified": "hc_success",
    "pending": "hc_warning",
    "processing": "hc_warning",
    "running": "hc_warning",
    "queued": "hc_warning",
    "suspended": "hc_warning",
    "hold": "hc_warning",
    "incomplete": "hc_warning",
    "initiated": "hc_warning",
    "answered": "hc_warning",
    "inactive": "hc_danger",
    "closed": "hc_danger",
    "error": "hc_danger",
    "fail": "hc_danger",
    "failed": "hc_danger",
    "cancelled": "hc_danger",
    "timeout": "hc_danger",
    "rejected": "hc_danger",
    "terminated": "hc_danger",
    "withdrawn": "hc_danger",
}

_ANSI_REGEX = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_SGR_REGEX = re.compile(r"\x1b\[[0-9;]*m")

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ASCII_SPINNER_FRAMES = ("-", "\\", "|", "/")

_CURSOR_HIDE = "\033[?25l"
_CURSOR_SHOW = "\033[?25h"
_CLEAR_LINE = "\r\033[K"


def _char_display_width(ch: str) -> int:
    """Return terminal column display width (0, 1, or 2) for a single code point."""
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me") or cat.startswith("C"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _iter_graphemes(text: str) -> Iterator[tuple[str, int]]:
    """Yield (grapheme_cluster, display_width) keeping Mn/Me combining marks attached."""
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        w = _char_display_width(ch)
        j = i + 1
        while j < n and unicodedata.category(text[j]) in ("Mn", "Me"):
            j += 1
        yield text[i:j], w
        i = j


def _display_width(text: str) -> int:
    """Return terminal display width of text in columns."""
    return sum(w for _, w in _iter_graphemes(text))


def _slice_display_width(text: str, max_width: int) -> str:
    """Slice text so its terminal display width does not exceed max_width."""
    if max_width <= 0 or not text:
        return ""
    out: list[str] = []
    used = 0
    for cluster, w in _iter_graphemes(text):
        if used + w > max_width:
            break
        out.append(cluster)
        used += w
    return "".join(out)


def _pad_display_width(text: str, width: int, fillchar: str = " ") -> str:
    """Right-pad text to target terminal display width."""
    pad = max(0, width - _display_width(text))
    return f"{text}{fillchar * pad}"


def _fit_display_width(text: str, width: int) -> str:
    """Slice and right-pad text to exact display width, padding overflowed wide-glyph cells."""
    if width <= 0:
        return ""
    sliced = _slice_display_width(text, width)
    return _pad_display_width(sliced, width)


def _center_display_width(text: str, width: int, fillchar: str = " ") -> str:
    """Slice and center text within target display width."""
    if width <= 0:
        return ""
    sliced = _slice_display_width(text, width)
    pad = max(0, width - _display_width(sliced))
    left = pad // 2
    right = pad - left
    return f"{fillchar * left}{sliced}{fillchar * right}"


def _sanitize_cell_text(text: str) -> str:
    """Replace line breaks, tabs, and C* control characters with single spaces."""
    normalized = text.replace("\r\n", " ")
    out: list[str] = []
    for ch in normalized:
        if ch in ("\r", "\n", "\t", "\u2028", "\u2029") or unicodedata.category(ch).startswith("C"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _truncate_visible(text: str, max_width: int) -> str:
    """Truncate text to a maximum visible display width, preserving ANSI sequences.

    Args:
        text: String possibly containing ANSI escape sequences.
        max_width: Maximum number of visible terminal columns to permit.

    Returns:
        Truncated string whose visible display width does not exceed max_width.
    """
    if max_width <= 0:
        return ""
    stripped = _ANSI_REGEX.sub("", text)
    if _display_width(stripped) <= max_width:
        return text

    result: list[str] = []
    visible_width = 0
    pos = 0
    has_ansi = False
    has_base = False
    n = len(text)
    while pos < n:
        match = _ANSI_REGEX.match(text, pos)
        if match:
            if visible_width >= max_width:
                break
            has_ansi = True
            result.append(match.group(0))
            pos = match.end()
            continue

        ch = text[pos]
        if unicodedata.category(ch) in ("Mn", "Me") and has_base:
            result.append(ch)
            pos += 1
            continue

        ch_w = _char_display_width(ch)
        if visible_width + ch_w > max_width:
            break
        result.append(ch)
        visible_width += ch_w
        has_base = True
        pos += 1

    if visible_width == 0 and not has_base:
        return ""
    if has_ansi:
        result.append("\033[0m")
    return "".join(result)


def clear_line(stream: Any = None, lines: int = 1) -> None:
    """Clear the current line(s) and restore terminal cursor on interactive streams.

    Args:
        stream: Target output stream (defaults to sys.stderr).
        lines: Number of terminal rows to clear.
    """
    target = stream if stream is not None else sys.stderr
    try:
        if supports_live_status(target):
            clear_seq = "\r\033[K" + ("\033[A\r\033[K" * max(0, lines - 1))
            target.write(f"{clear_seq}{_CURSOR_SHOW}")
            target.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def format_job_progress(
    event: JobStatusEvent | Any,
    status: Any = None,
    elapsed: float | None = None,
    bar_width: int = 20,
) -> str:
    """Format a job polling event into a progress bar, state label, and elapsed timer.

    Args:
        event: A JobStatusEvent or batch_id string.
        status: Optional JobStatus if event is a batch_id string.
        elapsed: Optional elapsed seconds if event is a batch_id string.
        bar_width: Width of the progress bar in characters.

    Returns:
        Formatted string containing progress bar, state label, and elapsed timer.
    """
    if hasattr(event, "status") and hasattr(event, "batch_id"):
        batch_id = str(event.batch_id)
        status_obj = event.status
        elapsed_val = float(getattr(event, "elapsed", 0.0))
    else:
        batch_id = str(event)
        status_obj = status
        elapsed_val = float(elapsed) if elapsed is not None else 0.0

    raw_progress = getattr(status_obj, "progress", None) if status_obj is not None else None
    parsed_progress: int | None = None
    if isinstance(raw_progress, (int, float, str)) and not isinstance(raw_progress, bool):
        try:
            parsed_progress = max(0, min(100, int(raw_progress)))
        except (TypeError, ValueError):
            parsed_progress = None

    state_val = getattr(status_obj, "state", "UNKNOWN") if status_obj is not None else "UNKNOWN"
    state_label = state_val.value if hasattr(state_val, "value") else str(state_val)

    if parsed_progress is not None:
        filled = round(bar_width * parsed_progress / 100.0)
        empty = max(0, bar_width - filled)
        bar = "=" * filled + "-" * empty
        return f"Job {batch_id}: [{bar}] {parsed_progress}% - {state_label} ({elapsed_val:.1f}s)"

    state_upper = state_label.strip().upper()
    if state_upper in {"COMPLETED", "COMPLETE", "SUCCESS"}:
        filled = bar_width
    elif state_upper in {"RUNNING", "PROCESSING"}:
        filled = bar_width // 2
    else:
        filled = 0
    empty = max(0, bar_width - filled)
    bar = "=" * filled + "-" * empty
    return f"Job {batch_id}: [{bar}] {state_label} ({elapsed_val:.1f}s)"


class Status:
    """Status controller managing live ANSI spinner rendering or static fallback."""

    def __init__(
        self,
        msg: str,
        console: Console,
        spinner: str | None = None,
        refresh_per_second: float = 12.5,
        *,
        announce_non_interactive: bool = True,
    ) -> None:
        """Initialize a status controller.

        Args:
            msg: Initial status message.
            console: Console instance managing stream output.
            spinner: Optional spinner style identifier.
            refresh_per_second: Target frame refresh rate.
            announce_non_interactive: Whether to emit the initial message on
                stderr when entering non-interactive mode.
        """
        self._msg = str(msg)
        self.console = console
        self.spinner = spinner
        self._interval = 1.0 / refresh_per_second if refresh_per_second > 0 else 0.08
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time: float = 0.0
        self._lock = threading.Lock()
        self._interactive = False
        self._entered = False
        self._frames: Sequence[str] = _SPINNER_FRAMES
        self._lines_rendered: int = 1
        self._announce_non_interactive = announce_non_interactive

    @property
    def lines_rendered(self) -> int:
        """Get the number of terminal rows occupied by the most recent frame."""
        return self._lines_rendered

    @property
    def msg(self) -> str:
        """Get the current status message."""
        with self._lock:
            return self._msg

    @msg.setter
    def msg(self, value: str) -> None:
        self.update(value)

    def update(self, msg: str) -> None:
        """Update the displayed status message."""
        with self._lock:
            self._msg = str(msg)
        if self._interactive and self._entered:
            self._render_frame()

    def step(self, msg: str) -> None:
        """Emit a mode-aware step update for long-running loops.

        Updates the live spinner on interactive TTYs and writes a structured
        plain-text line to ``stderr`` when non-interactive (unless JSON output
        mode is active).
        """
        with self._lock:
            self._msg = str(msg)
        if self._interactive and self._entered:
            self._render_frame()
            return
        if is_json_output_mode():
            return
        stream = self.console.stream
        try:
            clean_msg = _ANSI_REGEX.sub("", str(msg))
            print(clean_msg, file=stream, flush=True)
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _render_frame(self, frame_idx: int = 0) -> None:
        stream = self.console.stream
        try:
            with self._lock:
                current_msg = self._msg
            if not supports_ansi_color(stream):
                current_msg = _SGR_REGEX.sub("", current_msg)
            elapsed = time.monotonic() - self._start_time
            has_timer = bool(re.search(r"\(\d+\.?\d*s\)", current_msg))
            timer_suffix = "" if has_timer else f" ({elapsed:.1f}s)"

            frame = self._frames[frame_idx % len(self._frames)]
            content = f"{frame} {current_msg}{timer_suffix}"

            cols = self.console.width
            if cols > 0:
                max_width = max(1, cols - 1)
                content = _truncate_visible(content, max_width)

            clear_seq = "\r\033[K" + ("\033[A\r\033[K" * max(0, self._lines_rendered - 1))
            line = f"{clear_seq}{content}"

            vis_len = _display_width(_ANSI_REGEX.sub("", content))
            lines_by_width = max(1, (vis_len + cols - 1) // cols) if cols > 0 else 1
            lines_by_newlines = content.count("\n") + 1
            self._lines_rendered = max(lines_by_width, lines_by_newlines)

            try:
                stream.write(line)
                stream.flush()
            except UnicodeEncodeError:
                self._frames = _ASCII_SPINNER_FRAMES
                frame = self._frames[frame_idx % len(self._frames)]
                content = f"{frame} {current_msg}{timer_suffix}"
                if cols > 0:
                    max_width = max(1, cols - 1)
                    content = _truncate_visible(content, max_width)
                clear_seq = "\r\033[K" + ("\033[A\r\033[K" * max(0, self._lines_rendered - 1))
                line = f"{clear_seq}{content}"
                stream.write(line)
                stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _run_spinner(self) -> None:
        idx = 0
        while not self._stop_event.is_set():
            self._render_frame(idx)
            idx += 1
            if self._stop_event.wait(self._interval):
                break

    def __enter__(self) -> Status:
        """Enter the status context and begin live rendering or static output."""
        self._entered = True
        self._start_time = time.monotonic()
        self._interactive = self.console.is_interactive()
        stream = self.console.stream

        if self._interactive:
            try:
                stream.write(_CURSOR_HIDE)
                stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_spinner, daemon=True)
            self._thread.start()
        else:
            is_json_mode = is_json_output_mode()

            is_string_io = (
                isinstance(stream, io.StringIO) or stream.__class__.__name__ == "StringIO"
            )
            is_test_captured = stream is sys.stderr and is_string_io

            if self._announce_non_interactive and not is_json_mode and not is_test_captured:
                try:
                    clean_msg = _ANSI_REGEX.sub("", self._msg)
                    print(clean_msg, file=stream, flush=True)
                except (BrokenPipeError, OSError, ValueError):
                    pass

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the status context, stop spinner rendering, and restore cursor."""
        self._entered = False
        if self._interactive:
            self._stop_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.0)
            stream = self.console.stream
            try:
                clear_seq = "\r\033[K" + ("\033[A\r\033[K" * max(0, self._lines_rendered - 1))
                stream.write(f"{clear_seq}{_CURSOR_SHOW}")
                stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass


class Console:
    """Console handling terminal capabilities and live status rendering."""

    def __init__(self, stream: Any = None, width: int | None = None) -> None:
        """Initialize a Console instance.

        Args:
            stream: Optional stream destination (defaults to sys.stderr).
            width: Optional terminal column width override.
        """
        self._stream = stream
        self._width = width

    @property
    def stream(self) -> Any:
        """Get the active output stream."""
        return self._stream if self._stream is not None else sys.stderr

    @property
    def width(self) -> int:
        """Get the terminal column width."""
        if self._width is not None and self._width > 0:
            return self._width
        s = self.stream
        for attr in ("columns", "width"):
            val = getattr(s, attr, None)
            if isinstance(val, int) and val > 0:
                return val
        try:
            if s and hasattr(s, "fileno"):
                return os.get_terminal_size(s.fileno()).columns
        except (AttributeError, OSError, ValueError):
            pass
        try:
            return shutil.get_terminal_size(fallback=(80, 24)).columns
        except Exception:
            return 80

    @width.setter
    def width(self, val: int | None) -> None:
        """Set terminal column width override."""
        self._width = val

    def is_interactive(self) -> bool:
        """Check whether the output stream supports live interactive rendering."""
        return supports_live_status(self.stream)

    def status(
        self,
        msg: str,
        spinner: str | None = None,
        refresh_per_second: float = 12.5,
        *,
        announce_non_interactive: bool = True,
    ) -> Status:
        """Create a live status context manager."""
        return Status(
            msg,
            console=self,
            spinner=spinner,
            refresh_per_second=refresh_per_second,
            announce_non_interactive=announce_non_interactive,
        )


_DummyConsole = Console
console = Console()


def _truncate(text: str, length: int = 60) -> str:
    text = str(text)
    return f"{text[:length]}..." if len(text) > length else text


def _normalize_enums(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {_normalize_enums(k): _normalize_enums(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_enums(x) for x in value]
    if isinstance(value, tuple):
        return tuple(_normalize_enums(x) for x in value)
    return value


def _format_cell_value(value: Any, key: str | None = None) -> str:
    if isinstance(value, Enum):
        value = value.value
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")

    if isinstance(value, list) and all(isinstance(x, (str, int, float, bool, Enum)) for x in value):
        if not value:
            return "-"
        s = ", ".join(
            _sanitize_cell_text(str(x.value if isinstance(x, Enum) else x)) for x in value
        )
        return _truncate(s)
    if isinstance(value, (list, dict, tuple)):
        return _truncate(_sanitize_cell_text(str(_normalize_enums(value))))
    return _sanitize_cell_text(str(value))


_HEADER_ACRONYM_RE = re.compile(r"\b(?:id|oid|url|utc)\b", re.IGNORECASE)


def _format_header_label(header: Any) -> str:
    """Format a field key into a title-cased header while preserving uppercase acronyms."""
    cleaned = _sanitize_cell_text(str(header).replace("_", " ")).title()
    return _HEADER_ACRONYM_RE.sub(lambda match: match.group(0).upper(), cleaned)


def _colorize(text: str, color: str) -> str:
    if not supports_ansi_color(sys.stdout):
        return text
    resolved_color = _HC_16_FALLBACK_MAP.get(color, color) if _is_16_color_terminal() else color
    return f"{_ANSI_COLORS.get(resolved_color, '')}{text}{_ANSI_COLORS['reset']}"


def _print_table(items: Sequence[Any], fields: list[str]) -> None:
    if not items:
        return

    headers = [_format_header_label(header) for header in fields]

    rows = []
    for item in items:
        row = []
        for k in fields:
            if isinstance(item, dict):  # noqa: SIM108
                val = item.get(k)
            else:
                val = getattr(item, k, None)
            row.append(_format_cell_value(val, key=str(k)))
        rows.append(row)

    col_widths = [_display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], _display_width(cell))

    header_str = " | ".join(_pad_display_width(h, w) for h, w in zip(headers, col_widths))  # noqa: B905
    print(_colorize(header_str, "bold_magenta"))
    print("-" * _display_width(header_str))

    is_hc = os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"

    for row in rows:
        colored_row = []
        for i, cell in enumerate(row):
            k = fields[i]
            c = cell
            if any(key in k.lower() for key in ("status", "state")):
                lower_val = cell.lower()
                color = (
                    _HC_STATUS_COLOR_MAP.get(lower_val)
                    if is_hc
                    else _STATUS_COLOR_MAP.get(lower_val)
                )
                if color:
                    c = _colorize(cell, color)
            colored_row.append(c + " " * max(0, col_widths[i] - _display_width(cell)))
        print(" | ".join(colored_row))


def _project_item_to_row(
    item: Any,
    field_keys: list[str],
    *,
    explicit_fields: bool,
) -> dict[str, Any]:
    """Convert a domain model or dict into a table row dict matching ``field_keys``."""
    if explicit_fields:
        if isinstance(item, dict):
            return {k: item.get(k) for k in field_keys}
        return {k: getattr(item, k, None) for k in field_keys}
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(item, "dict"):
        dumped_legacy = item.dict()
        if isinstance(dumped_legacy, dict):
            return dumped_legacy
    return {k: getattr(item, k, None) for k in field_keys}


def display_list(
    items: Sequence[Any],
    label: str,
    empty_msg: str | None = None,
    fields: list[str] | None = None,
    *,
    output_format: str | None = None,
    details: Sequence[Any] | None = None,
    interactive: bool | None = None,
    truncated: bool = False,
    status_notice: str | None = None,
    on_load_all: Callable[[], Any] | None = None,
) -> None:
    """Display a human table or one versioned JSON response without progress text."""
    from .args import resolve_output_format

    resolved_format = resolve_output_format(output_format)
    if resolved_format == "json":
        emit_json(items, label.replace(" ", "_"))
        return
    if resolved_format != "table":
        raise ValueError("Unsupported output format")
    if not items:
        print(empty_msg or f"No {label} found.")
        return

    effective_notice = status_notice or (
        f"Showing first {len(items)} {label} (truncated; pass --all or --limit for more)"
        if truncated
        else None
    )

    field_keys: list[str] = []
    data_list: list[dict[str, Any]] = []
    explicit_fields = bool(fields)

    if fields:
        field_keys = list(fields)
        data_list = [_project_item_to_row(item, field_keys, explicit_fields=True) for item in items]
    else:
        first = items[0]
        if hasattr(first, "model_dump"):
            data_list = [item.model_dump() for item in items]
        elif hasattr(first, "dict"):
            data_list = [item.dict() for item in items]
        elif isinstance(first, dict):
            data_list = list(items)

        if not data_list:
            print(f"Found {len(items)} {label}:")
            print(items)
            return

        field_keys = list(data_list[0].keys())

    # Launch interactive data grid if connected to an active terminal
    try:
        from .grid import is_interactive_terminal, run_interactive_grid
    except ImportError:

        def is_interactive_terminal() -> bool:
            return False

        def run_interactive_grid(*args: Any, **kwargs: Any) -> bool:  # type: ignore[misc]
            return False

    should_run_interactive = interactive if interactive is not None else is_interactive_terminal()
    if should_run_interactive:
        effective_details = details if details is not None else items
        grid_notice = status_notice or (
            f"Showing first {len(items)} {label} (truncated; press [a] or pass --all)"
            if truncated and on_load_all is not None
            else effective_notice
        )
        grid_kwargs: dict[str, Any] = {
            "items": data_list,
            "fields": field_keys,
            "label": label,
            "details": effective_details,
        }
        if truncated:
            grid_kwargs["truncated"] = True
        if grid_notice is not None:
            grid_kwargs["status_notice"] = grid_notice
        if on_load_all is not None:

            def _wrapped_load_all() -> tuple[list[dict[str, Any]], list[Any]]:
                loaded = on_load_all()
                if (
                    isinstance(loaded, tuple)
                    and len(loaded) == 2
                    and not isinstance(loaded[0], (str, bytes, dict))
                ):
                    raw_rows, raw_details = loaded
                    projected = [
                        _project_item_to_row(r, field_keys, explicit_fields=explicit_fields)
                        for r in raw_rows
                    ]
                    return projected, list(raw_details)
                raw_items = list(loaded)
                projected_items = [
                    _project_item_to_row(r, field_keys, explicit_fields=explicit_fields)
                    for r in raw_items
                ]
                return projected_items, raw_items

            grid_kwargs["on_load_all"] = _wrapped_load_all
        grid_ran = run_interactive_grid(**grid_kwargs)
        if grid_ran:
            return

    # Non-interactive / piped fallback: emit standard ASCII table
    print(f"Found {len(items)} {label}:")
    _print_table(items if fields else data_list, field_keys)
    if effective_notice:
        print(f"Notice: {effective_notice}")


def print_error(
    *objects: Any,
    sep: str = " ",
    end: str = "\n",
    file: Any = None,
    flush: bool = False,
) -> None:
    """Print an error message to sys.stderr safely.

    Handles stream safety edge cases:
    - Writes to sys.stderr by default (or custom file if provided).
    - Silently handles BrokenPipeError and IOError/OSError if the stream is closed or broken.
    - Strips ANSI escape sequences when writing to non-interactive (non-tty) streams.
    """
    target = sys.stderr if file is None else file
    try:
        formatted = sep.join(str(obj) for obj in objects)
        if not supports_ansi_color(target):
            formatted = _ANSI_REGEX.sub("", formatted)

        print(formatted, end=end, file=target, flush=flush)
    except (BrokenPipeError, OSError):
        pass


def _json_default(value: Any) -> Any:
    """Serialize model/date/enum values without lossy string fallbacks."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def emit_json(items: Sequence[Any], resource: str, *, status: str = "complete") -> None:
    """Write one complete v1 response after retrieval or execution and serialization."""
    envelope = {
        "schemaVersion": "1.0",
        "resource": resource,
        "status": status,
        "count": len(items),
        "items": list(items),
    }
    # Serialize before the first byte is emitted: failures cannot create partial JSON.
    rendered = json.dumps(envelope, default=_json_default, ensure_ascii=False, allow_nan=False)
    print(rendered)
