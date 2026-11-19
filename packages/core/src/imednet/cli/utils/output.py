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
from collections.abc import Sequence
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
    "reset": "\033[0m",
}

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
    "active": "blue",
    "success": "blue",
    "ok": "blue",
    "completed": "blue",
    "complete": "blue",
    "open": "blue",
    "approved": "blue",
    "verified": "blue",
    "pending": "yellow",
    "processing": "yellow",
    "running": "yellow",
    "queued": "yellow",
    "suspended": "yellow",
    "hold": "yellow",
    "incomplete": "yellow",
    "initiated": "yellow",
    "inactive": "magenta",
    "closed": "magenta",
    "error": "magenta",
    "fail": "magenta",
    "failed": "magenta",
    "cancelled": "magenta",
    "timeout": "magenta",
    "rejected": "magenta",
    "terminated": "magenta",
    "withdrawn": "magenta",
}

_ANSI_REGEX = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_SGR_REGEX = re.compile(r"\x1b\[[0-9;]*m")

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ASCII_SPINNER_FRAMES = ("-", "\\", "|", "/")

_CURSOR_HIDE = "\033[?25l"
_CURSOR_SHOW = "\033[?25h"
_CLEAR_LINE = "\r\033[K"


def _truncate_visible(text: str, max_width: int) -> str:
    """Truncate text to a maximum visible width, preserving ANSI sequences.

    Args:
        text: String possibly containing ANSI escape sequences.
        max_width: Maximum number of visible characters to permit.

    Returns:
        Truncated string whose visible character count does not exceed max_width.
    """
    if max_width <= 0:
        return ""
    stripped = _ANSI_REGEX.sub("", text)
    if len(stripped) <= max_width:
        return text

    result: list[str] = []
    visible_count = 0
    pos = 0
    has_ansi = False
    while pos < len(text) and visible_count < max_width:
        match = _ANSI_REGEX.match(text, pos)
        if match:
            has_ansi = True
            result.append(match.group(0))
            pos = match.end()
        else:
            result.append(text[pos])
            visible_count += 1
            pos += 1

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

    raw_progress = getattr(status_obj, "progress", 0) if status_obj is not None else 0
    try:
        progress = max(0, min(100, int(raw_progress)))
    except (TypeError, ValueError):
        progress = 0

    state_val = getattr(status_obj, "state", "UNKNOWN") if status_obj is not None else "UNKNOWN"
    state_label = state_val.value if hasattr(state_val, "value") else str(state_val)

    filled = round(bar_width * progress / 100.0)
    empty = max(0, bar_width - filled)
    bar = "=" * filled + "-" * empty

    return f"Job {batch_id}: [{bar}] {progress}% - {state_label} ({elapsed_val:.1f}s)"


class Status:
    """Status controller managing live ANSI spinner rendering or static fallback."""

    def __init__(
        self,
        msg: str,
        console: Console,
        spinner: str | None = None,
        refresh_per_second: float = 12.5,
    ) -> None:
        """Initialize a status controller.

        Args:
            msg: Initial status message.
            console: Console instance managing stream output.
            spinner: Optional spinner style identifier.
            refresh_per_second: Target frame refresh rate.
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

            vis_len = len(_ANSI_REGEX.sub("", content))
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

            if not is_json_mode and not is_test_captured:
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
    ) -> Status:
        """Create a live status context manager."""
        return Status(msg, console=self, spinner=spinner, refresh_per_second=refresh_per_second)


_DummyConsole = Console
console = Console()


def _truncate(text: str, length: int = 60) -> str:
    text = str(text)
    return f"{text[:length]}..." if len(text) > length else text


def _format_cell_value(value: Any, key: str | None = None) -> str:
    if isinstance(value, Enum):
        value = value.value
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")

    str_val = str(value)
    if isinstance(value, list) and all(isinstance(x, (str, int, float, bool, Enum)) for x in value):
        if not value:
            return "-"
        s = ", ".join(str(x.value if isinstance(x, Enum) else x) for x in value)
        return _truncate(s)
    if isinstance(value, (list, dict)):
        return _truncate(str(value))
    return str_val


def _colorize(text: str, color: str) -> str:
    if not supports_ansi_color(sys.stdout):
        return text
    return f"{_ANSI_COLORS.get(color, '')}{text}{_ANSI_COLORS['reset']}"


def _print_table(items: Sequence[Any], fields: list[str]) -> None:
    if not items:
        return

    headers = [str(header).replace("_", " ").title() for header in fields]

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

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    header_str = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))  # noqa: B905
    print(_colorize(header_str, "bold_magenta"))
    print("-" * len(header_str))

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
            colored_row.append(c + " " * (col_widths[i] - len(cell)))
        print(" | ".join(colored_row))


def display_list(
    items: Sequence[Any],
    label: str,
    empty_msg: str | None = None,
    fields: list[str] | None = None,
    *,
    output_format: str = "table",
    details: Sequence[Any] | None = None,
    interactive: bool | None = None,
) -> None:
    """Display a human table or one versioned JSON response without progress text."""
    if output_format == "json":
        emit_json(items, label.replace(" ", "_"))
        return
    if output_format != "table":
        raise ValueError("Unsupported output format")
    if not items:
        print(empty_msg or f"No {label} found.")
        return

    field_keys: list[str] = []
    data_list: list[dict[str, Any]] = []

    if fields:
        field_keys = list(fields)
        for item in items:
            if isinstance(item, dict):
                data_list.append({k: item.get(k) for k in field_keys})
            else:
                data_list.append({k: getattr(item, k, None) for k in field_keys})
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

        def is_interactive_terminal() -> bool:  # type: ignore[misc]
            return False

        def run_interactive_grid(*args: Any, **kwargs: Any) -> bool:  # type: ignore[misc]
            return False

    should_run_interactive = interactive if interactive is not None else is_interactive_terminal()
    if should_run_interactive:
        effective_details = details if details is not None else items
        grid_ran = run_interactive_grid(
            items=data_list,
            fields=field_keys,
            label=label,
            details=effective_details,
        )
        if grid_ran:
            return

    # Non-interactive / piped fallback: emit standard ASCII table
    print(f"Found {len(items)} {label}:")
    _print_table(items if fields else data_list, field_keys)


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


def emit_json(items: Sequence[Any], resource: str) -> None:
    """Write one complete v1 response after successful retrieval and serialization."""
    envelope = {
        "schemaVersion": "1.0",
        "resource": resource,
        "status": "complete",
        "count": len(items),
        "items": list(items),
    }
    # Serialize before the first byte is emitted: failures cannot create partial JSON.
    rendered = json.dumps(envelope, default=_json_default, ensure_ascii=False, allow_nan=False)
    print(rendered)
