"""CLI output formatting and display utilities."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

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

_ANSI_REGEX = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class _DummyConsole:
    def status(self, msg: str, spinner: str | None = None) -> Any:
        class DummyStatus:
            def __enter__(self) -> None:
                if sys.stderr.isatty():
                    print(msg, file=sys.stderr)

            def __exit__(self, *args: Any) -> None:
                pass

        return DummyStatus()


console = _DummyConsole()


def _truncate(text: str, length: int = 60) -> str:
    text = str(text)
    return f"{text[:length]}..." if len(text) > length else text


def _format_cell_value(value: Any, key: str | None = None) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")

    str_val = str(value)
    if isinstance(value, list) and all(isinstance(x, (str, int, float, bool)) for x in value):
        if not value:
            return "-"
        s = ", ".join(str(x) for x in value)
        return _truncate(s)
    if isinstance(value, (list, dict)):
        return _truncate(str(value))
    return str_val


def _colorize(text: str, color: str) -> str:
    if not sys.stdout.isatty():
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
        grid_ran = run_interactive_grid(
            items=data_list,
            fields=field_keys,
            label=label,
            details=details,
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
        is_tty = False
        try:
            is_tty = bool(target and hasattr(target, "isatty") and target.isatty())
        except (AttributeError, ValueError, OSError):
            is_tty = False

        formatted = sep.join(str(obj) for obj in objects)
        if not is_tty:
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
