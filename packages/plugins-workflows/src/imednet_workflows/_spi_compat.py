"""Compatibility shims for core SPI symbols across supported ``imednet>=1.1.0`` releases.

Delegates directly to ``imednet.spi`` when running alongside a core release that
exports the extended CLI and utility helpers, while supplying self-contained
fallbacks when paired with ``imednet==1.1.0``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from imednet.spi import cli as _spi_cli
from imednet.spi import errors as _spi_errors
from imednet.spi import utils as _spi_utils
from imednet.spi.cli import STUDY_KEY_ARG, with_sdk

_ANSI_STRIP_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_TWO_CHAR_OPS = ("<=", ">=", "!=", "==", "=~")
_ONE_CHAR_OPS = ("<", ">", "=")
_OP_CHARS = frozenset("<>=!~")


def _fallback_add_output_format(parser: argparse.ArgumentParser) -> None:
    """Register ``--format {table,json}`` on a CLI leaf command parser."""
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("table", "json"),
        default="table",
        help="Response format: human table (default) or versioned JSON on stdout.",
    )


def _encode_json_value(value: Any) -> Any:
    """Encode Pydantic models, dates, and enums for JSON envelope output."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def _fallback_emit_json(items: Sequence[Any], resource: str) -> None:
    """Emit a ``schemaVersion: '1.0'`` JSON envelope to stdout."""
    envelope = {
        "schemaVersion": "1.0",
        "resource": resource,
        "status": "complete",
        "count": len(items),
        "items": list(items),
    }
    print(json.dumps(envelope, default=_encode_json_value, ensure_ascii=False, allow_nan=False))


def _render_cell(val: Any) -> str:
    """Format a single scalar or collection cell for ASCII table output."""
    if val is None or val == "":
        return "-"
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M")
    if isinstance(val, list) and all(isinstance(x, (str, int, float, bool)) for x in val):
        if not val:
            return "-"
        joined = ", ".join(str(x) for x in val)
        return f"{joined[:60]}..." if len(joined) > 60 else joined
    rendered = str(val)
    if isinstance(val, (list, dict)) and len(rendered) > 60:
        return f"{rendered[:60]}..."
    return rendered


def _fallback_display_list(
    items: Sequence[Any],
    label: str,
    empty_msg: str | None = None,
    fields: list[str] | None = None,
    *,
    output_format: str = "table",
    details: Sequence[Any] | None = None,
    interactive: bool | None = None,
) -> None:
    """Render items as a versioned JSON envelope or an aligned ASCII table."""
    del details, interactive
    if output_format == "json":
        _fallback_emit_json(items, label.replace(" ", "_"))
        return
    if output_format != "table":
        raise ValueError("Unsupported output format")
    if not items:
        print(empty_msg or f"No {label} found.")
        return

    if fields:
        col_keys = list(fields)
        source_rows: Sequence[Any] = items
    else:
        sample = items[0]
        if hasattr(sample, "model_dump"):
            dumped = [row.model_dump() for row in items]
        elif isinstance(sample, dict):
            dumped = list(items)
        else:
            print(f"Found {len(items)} {label}:")
            for entry in items:
                print(entry)
            return
        col_keys = list(dumped[0].keys())
        source_rows = dumped

    headers = [key.replace("_", " ").title() for key in col_keys]
    matrix = [
        [
            _render_cell(row.get(k) if isinstance(row, dict) else getattr(row, k, None))
            for k in col_keys
        ]
        for row in source_rows
    ]
    widths = [
        max(len(headers[idx]), *(len(r[idx]) for r in matrix)) for idx in range(len(col_keys))
    ]
    header_line = " | ".join(headers[idx].ljust(widths[idx]) for idx in range(len(col_keys)))
    print(f"Found {len(items)} {label}:")
    print(header_line)
    print("-" * len(header_line))
    for row in matrix:
        print(" | ".join(row[idx].ljust(widths[idx]) for idx in range(len(col_keys))))


def _fallback_print_error(
    *objects: Any,
    sep: str = " ",
    end: str = "\n",
    file: Any = None,
    flush: bool = False,
) -> None:
    """Write an error line safely to stderr, stripping ANSI escapes on pipes."""
    target = sys.stderr if file is None else file
    with contextlib.suppress(BrokenPipeError, OSError):
        is_tty = False
        with contextlib.suppress(AttributeError, OSError, ValueError):
            is_tty = bool(target and hasattr(target, "isatty") and target.isatty())
        text = sep.join(str(obj) for obj in objects)
        if not is_tty:
            text = _ANSI_STRIP_RE.sub("", text)
        print(text, end=end, file=target, flush=flush)


def _fallback_clear_line(stream: Any = None, lines: int = 1) -> None:
    """Clear active terminal line(s) when connected to a TTY stream."""
    target = sys.stderr if stream is None else stream
    with contextlib.suppress(AttributeError, BrokenPipeError, OSError, ValueError):
        if target and hasattr(target, "isatty") and target.isatty():
            seq = "\r\033[K" + ("\033[A\r\033[K" * max(0, lines - 1))
            target.write(f"{seq}\033[?25h")
            target.flush()


def _fallback_format_job_progress(
    event: Any,
    status: Any = None,
    elapsed: float | None = None,
    bar_width: int = 20,
) -> str:
    """Format job polling status into a deterministic progress bar line."""
    if hasattr(event, "status") and hasattr(event, "batch_id"):
        batch_id = str(event.batch_id)
        status_obj = event.status
        elapsed_sec = float(getattr(event, "elapsed", 0.0))
    else:
        batch_id = str(event)
        status_obj = status
        elapsed_sec = float(elapsed) if elapsed is not None else 0.0

    raw_pct = getattr(status_obj, "progress", 0) if status_obj is not None else 0
    try:
        pct = max(0, min(100, int(raw_pct)))
    except (TypeError, ValueError):
        pct = 0

    raw_state = getattr(status_obj, "state", "UNKNOWN") if status_obj is not None else "UNKNOWN"
    state_str = raw_state.value if hasattr(raw_state, "value") else str(raw_state)
    filled = round(bar_width * pct / 100.0)
    bar = "=" * filled + "-" * max(0, bar_width - filled)
    return f"Job {batch_id}: [{bar}] {pct}% - {state_str} ({elapsed_sec:.1f}s)"


class _FallbackStatus:
    """Minimal status context manager when core ``Status`` is unavailable."""

    def __init__(self, msg: str, stream: Any = None) -> None:
        self._msg = str(msg)
        self._stream = stream

    @property
    def stream(self) -> Any:
        return sys.stderr if self._stream is None else self._stream

    def update(self, msg: str) -> None:
        """Update current status message in place on TTY streams."""
        self._msg = str(msg)
        target = self.stream
        with contextlib.suppress(AttributeError, BrokenPipeError, OSError, ValueError):
            if target and hasattr(target, "isatty") and target.isatty():
                target.write(f"\r\033[K{self._msg}")
                target.flush()

    def __enter__(self) -> _FallbackStatus:
        target = self.stream
        with contextlib.suppress(AttributeError, BrokenPipeError, OSError, ValueError):
            if target and hasattr(target, "isatty") and target.isatty():
                target.write(f"\r\033[K{self._msg}")
                target.flush()
            elif not (
                os.environ.get("IMEDNET_CLI_OUTPUT_FORMAT") == "json"
                or (target is sys.stderr and isinstance(target, io.StringIO))
            ):
                print(_ANSI_STRIP_RE.sub("", self._msg), file=target, flush=True)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        del exc_type, exc_val, exc_tb
        _fallback_clear_line(self.stream)


class _FallbackConsole:
    """Minimal console wrapper providing ``status(...)`` on ``imednet==1.1.0``."""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream

    def status(
        self,
        msg: str,
        spinner: str | None = None,
        refresh_per_second: float = 12.5,
    ) -> _FallbackStatus:
        """Create a status context manager."""
        del spinner, refresh_per_second
        return _FallbackStatus(msg, stream=self._stream)


def _camel_alias(snake_name: str) -> str:
    parts = snake_name.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def _allowed_model_keys(model: type[Any]) -> set[str]:
    fields_map = getattr(model, "model_fields", None)
    if not isinstance(fields_map, dict):
        return set()
    names: set[str] = set()
    for fname, finfo in fields_map.items():
        names.add(fname)
        names.add(_camel_alias(fname))
        for attr in ("alias", "validation_alias"):
            alias_val = getattr(finfo, attr, None)
            if isinstance(alias_val, str) and alias_val:
                names.add(alias_val)
    return names


def _fallback_parse_filter_expression(
    filter_str: str,
    *,
    model: type[Any] | None = None,
) -> tuple[str, Any]:
    """Parse ``key<op>value`` filter syntax when core ``1.1.0`` lacks SPI filter helpers."""
    if not isinstance(filter_str, str) or not filter_str.strip():
        raise ValueError(
            f"Invalid filter format: '{filter_str}'. "
            "Use 'key=value' with non-empty key and value (e.g., 'form_key=DEMO')."
        )

    operator_sym: str | None = None
    op_pos = -1
    for idx in range(len(filter_str)):
        pair = filter_str[idx : idx + 2]
        if len(pair) == 2 and pair in _TWO_CHAR_OPS:
            operator_sym, op_pos = pair, idx
            break
        if filter_str[idx] in _ONE_CHAR_OPS:
            operator_sym, op_pos = filter_str[idx], idx
            break

    if operator_sym is None:
        raise ValueError(
            f"Invalid filter format: '{filter_str}'. "
            "Use 'key=value' with non-empty key and value (e.g., 'form_key=DEMO')."
        )

    key = filter_str[:op_pos].strip()
    raw_val = filter_str[op_pos + len(operator_sym) :].strip()
    if not key or not raw_val:
        raise ValueError(
            f"Invalid filter format: '{filter_str}'. "
            "Key and value cannot be empty; use 'key=value' (e.g., 'form_key=DEMO')."
        )
    if (operator_sym == "==" and raw_val.startswith("=")) or (
        operator_sym == "<" and raw_val.startswith(">")
    ):
        raise ValueError(f"Invalid filter format: '{filter_str}'. Unsupported comparison operator.")
    if any(ch.isspace() or ch in _OP_CHARS for ch in key):
        raise ValueError(f"Invalid filter key '{key}'.")
    if model is not None:
        allowed = _allowed_model_keys(model)
        if allowed and key not in allowed:
            raise ValueError(
                f"Unknown filter attribute '{key}' for {model.__name__}. "
                f"Valid attributes: {', '.join(sorted(allowed))}"
            )

    low = raw_val.lower()
    scalar: str | bool | int
    if low == "true":
        scalar = True
    elif low == "false":
        scalar = False
    elif raw_val.isdigit():
        scalar = int(raw_val)
    else:
        scalar = raw_val
    return (key, scalar) if operator_sym == "=" else (key, (operator_sym, scalar))


def _fallback_parse_filter_args(
    filter_args: list[str] | None,
    *,
    model: type[Any] | None = None,
) -> dict[str, Any] | None:
    """Parse CLI filter arguments with optional model key validation."""
    if not filter_args:
        return None
    parsed: dict[str, Any] = {}
    for expr in filter_args:
        try:
            key, val = _fallback_parse_filter_expression(expr, model=model)
        except ValueError as exc:
            print_error(f"Error: {exc}")
            sys.exit(1)
        if key not in parsed:
            parsed[key] = val
        else:
            existing = parsed[key]
            items = (
                list(existing)
                if isinstance(existing, list)
                else [existing if isinstance(existing, tuple) else ("==", existing)]
            )
            items.append(val if isinstance(val, tuple) else ("==", val))
            parsed[key] = items
    return parsed


def _fallback_normalize_utc_timestamp_bound(value: str, *, is_end: bool = False) -> str:
    """Convert date or ISO-8601 timestamp string to a UTC ``...Z`` bound."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Date filter bound must be a non-empty ISO 8601 date or timestamp string")
    raw = value.strip()
    norm = _spi_utils.normalize_iso_date(raw)
    candidate = norm if isinstance(norm, str) else raw
    if _ISO_DAY_RE.fullmatch(candidate):
        date.fromisoformat(candidate)
        return f"{candidate}{'T23:59:59Z' if is_end else 'T00:00:00Z'}"
    return _spi_utils.format_iso_datetime(_spi_utils.parse_iso_datetime(candidate))


def _fallback_validate_partition_key(key: str) -> None:
    """Reject directory traversal or path separator characters in partition keys."""
    if (
        "\x00" in key
        or key in ("..", ".")
        or "../" in key
        or "..\\" in key
        or "/" in key
        or "\\" in key
        or _WIN_DRIVE_RE.match(key)
    ):
        raise PathTraversalValidationError(f"Invalid partition key: {key!r}")


add_output_format: Callable[[argparse.ArgumentParser], None] = getattr(
    _spi_cli, "add_output_format", _fallback_add_output_format
)
clear_line: Callable[..., None] = getattr(_spi_cli, "clear_line", _fallback_clear_line)
console: Any = getattr(_spi_cli, "console", _FallbackConsole())
display_list: Callable[..., None] = getattr(_spi_cli, "display_list", _fallback_display_list)
emit_json: Callable[[Sequence[Any], str], None] = getattr(
    _spi_cli, "emit_json", _fallback_emit_json
)
format_job_progress: Callable[..., str] = getattr(
    _spi_cli, "format_job_progress", _fallback_format_job_progress
)
print_error: Callable[..., None] = getattr(_spi_cli, "print_error", _fallback_print_error)
parse_filter_expression: Callable[..., tuple[str, Any]] = getattr(
    _spi_utils, "parse_filter_expression", _fallback_parse_filter_expression
)
normalize_utc_timestamp_bound: Callable[..., str] = getattr(
    _spi_utils, "normalize_utc_timestamp_bound", _fallback_normalize_utc_timestamp_bound
)
PathTraversalValidationError: type[Exception] = getattr(
    _spi_utils, "PathTraversalValidationError", _spi_errors.PathTraversalValidationError
)
validate_partition_key: Callable[[str], None] = getattr(
    _spi_utils, "validate_partition_key", _fallback_validate_partition_key
)


def parse_filter_args(
    filter_args: list[str] | None,
    *,
    model: type[Any] | None = None,
) -> dict[str, Any] | None:
    """Delegate filter parsing to core SPI when ``model`` is supported, else fallback."""
    if hasattr(_spi_utils, "parse_filter_expressions"):
        res: dict[str, Any] | None = _spi_cli.parse_filter_args(filter_args, model=model)
        return res
    return _fallback_parse_filter_args(filter_args, model=model)


def is_interactive_terminal() -> bool:
    """Check lazily whether interactive terminal grid rendering is supported and active."""
    checker = getattr(_spi_cli, "is_interactive_terminal", None)
    return bool(checker()) if callable(checker) else False


__all__ = [
    "STUDY_KEY_ARG",
    "PathTraversalValidationError",
    "add_output_format",
    "clear_line",
    "console",
    "display_list",
    "emit_json",
    "format_job_progress",
    "is_interactive_terminal",
    "normalize_utc_timestamp_bound",
    "parse_filter_args",
    "parse_filter_expression",
    "print_error",
    "validate_partition_key",
    "with_sdk",
]
