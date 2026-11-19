"""Interactive keyboard data grid with side panel inspection using curses."""

from __future__ import annotations

import contextlib

try:
    import curses
except ImportError:
    curses = None  # type: ignore[assignment]

_CURSES_ERROR: tuple[type[BaseException], ...] = (curses.error,) if curses is not None else ()
import os
import textwrap
from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from .output import _HC_STATUS_COLOR_MAP, _STATUS_COLOR_MAP, _format_cell_value
from .terminal import supports_fullscreen_ui

_MIN_READABLE_CELL_WIDTH = 12
_MAX_SCROLL_COL_WIDTH = 36


def _normalize_nested_value(val: Any) -> Any:
    """Recursively normalize Pydantic models and tuples into plain dicts and lists."""
    if isinstance(val, BaseModel):
        return _normalize_nested_value(val.model_dump(by_alias=False))
    if isinstance(val, dict):
        return {k: _normalize_nested_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_normalize_nested_value(x) for x in val]
    return val


def _flatten_variables(prefix: str, source: dict[str, Any], out: dict[str, Any]) -> None:
    """Recursively flatten nested variable dictionaries using dot notation."""
    for k, v in source.items():
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            _flatten_variables(full_key, v, out)
        else:
            out[full_key] = v


def _collect_search_tokens(val: Any) -> list[str]:
    """Recursively extract searchable strings from scalars, enums, models, and collections."""
    if val is None:
        return []
    if isinstance(val, Enum):
        return [str(val.value)]
    if isinstance(val, BaseModel):
        return _collect_search_tokens(val.model_dump(by_alias=False))
    if isinstance(val, dict):
        tokens: list[str] = []
        for k, v in val.items():
            if v is None or v == "":
                continue
            tokens.append(str(k))
            tokens.extend(_collect_search_tokens(v))
        return tokens
    if isinstance(val, (list, tuple, set)):
        seq_tokens: list[str] = []
        for item in val:
            seq_tokens.extend(_collect_search_tokens(item))
        return seq_tokens
    if isinstance(val, datetime):
        return [val.strftime("%Y-%m-%d %H:%M"), val.isoformat()]
    return [str(val)]


def is_interactive_terminal() -> bool:
    """Determine if stdout and stdin are connected to an active interactive TTY.

    Returns:
        True if interactive curses mode should be used, False if output should
        be emitted as plain tabular text.
    """
    if curses is None:
        return False
    return supports_fullscreen_ui()


def safe_addstr(win: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    """Safely write a string to a curses window without boundary exceptions.

    Args:
        win: The curses window.
        y: Target line row.
        x: Target line column.
        text: String to render.
        attr: Optional curses text attribute.
    """
    if curses is None:
        return

    try:
        max_y, max_x = win.getmaxyx()
    except _CURSES_ERROR:
        return

    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return

    avail = max_x - x
    if len(text) > avail:
        text = text[:avail]

    # Prevent curses error when writing to bottom-right cell
    if y == max_y - 1 and x + len(text) >= max_x:
        text = text[: max_x - x - 1]

    if not text:
        return

    with contextlib.suppress(*_CURSES_ERROR):
        win.addstr(y, x, text, attr)


def extract_item_details(detail: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract metadata and form variables from any domain model or dictionary.

    Args:
        detail: A Pydantic model, dictionary, or data object.

    Returns:
        A tuple of (metadata_dict, form_variables_dict).
    """
    if detail is None:
        return {}, {}

    data_dict: dict[str, Any] = {}
    if isinstance(detail, BaseModel):
        data_dict = detail.model_dump(by_alias=False)
    elif isinstance(detail, dict):
        data_dict = dict(detail)
    elif hasattr(detail, "model_dump") and callable(detail.model_dump):
        try:
            dumped = detail.model_dump(by_alias=False)
        except TypeError:
            dumped = detail.model_dump()
        if isinstance(dumped, dict):
            data_dict = dumped
        else:
            data_dict = {k: v for k, v in vars(detail).items() if not k.startswith("_")}
    elif hasattr(detail, "dict") and callable(detail.dict):
        dumped_legacy = detail.dict()
        if isinstance(dumped_legacy, dict):
            data_dict = dumped_legacy
        else:
            data_dict = {k: v for k, v in vars(detail).items() if not k.startswith("_")}
    else:
        data_dict = {k: v for k, v in vars(detail).items() if not k.startswith("_")}

    data_dict = {k: _normalize_nested_value(v) for k, v in data_dict.items()}

    raw_vars = data_dict.pop("record_data", None)
    if raw_vars is None:
        raw_vars = data_dict.pop("data", None)

    variables: dict[str, Any] = {}
    if isinstance(raw_vars, dict):
        variables = dict(raw_vars)
    elif hasattr(raw_vars, "root") and isinstance(raw_vars.root, dict):
        variables = _normalize_nested_value(dict(raw_vars.root))
    elif hasattr(raw_vars, "model_dump"):
        variables = _normalize_nested_value(raw_vars.model_dump(by_alias=False))

    flattened_vars: dict[str, Any] = {}
    _flatten_variables("", variables, flattened_vars)

    return data_dict, flattened_vars


class InteractiveDataGrid:
    """Curses-driven scrollable data grid with real-time search and side panel inspection."""

    def __init__(
        self,
        stdscr: Any,
        items: Sequence[dict[str, Any]],
        fields: list[str],
        label: str = "records",
        details: Sequence[Any] | None = None,
        is_high_contrast: bool | None = None,
        status_notice: str | None = None,
    ) -> None:
        """Initialize the interactive data grid.

        Args:
            stdscr: Curses screen window.
            items: List of dictionary rows for table rendering.
            fields: Column headers to display.
            label: Name of the resource being reviewed (e.g. 'records', 'subjects').
            details: Full domain objects corresponding to each row.
            is_high_contrast: Whether high-contrast clinical status mode is active.
            status_notice: Optional notice displayed when results are truncated/bounded.
        """
        if curses is None:
            raise RuntimeError(
                "Interactive data grid requires the curses module, which is unavailable."
            )
        self.stdscr = stdscr
        self.fields = fields
        self.label = label
        self.status_notice = status_notice
        self.is_high_contrast = (
            is_high_contrast
            if is_high_contrast is not None
            else (os.environ.get("IMEDNET_HIGH_CONTRAST") == "1")
        )

        detail_list = list(details) if details is not None else list(items)
        self.all_entries: list[tuple[dict[str, Any], Any]] = list(
            zip(items, detail_list, strict=False)
        )
        self.filtered_entries: list[tuple[dict[str, Any], Any]] = list(self.all_entries)

        self.selected_idx: int = 0
        self.top_idx: int = 0
        self.col_offset: int = 0
        self.side_panel_open: bool = False
        self.side_panel_scroll: int = 0

        self.is_searching: bool = False
        self.search_query: str = ""

        self.color_attrs: dict[str, int] = {}
        self._setup_curses()

    def _setup_curses(self) -> None:
        """Configure curses environment and colors."""
        with contextlib.suppress(curses.error):
            curses.curs_set(0)

        with contextlib.suppress(AttributeError, curses.error):
            self.stdscr.keypad(True)

        with contextlib.suppress(AttributeError, curses.error):
            curses.set_escdelay(25)

        with contextlib.suppress(curses.error):
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_RED, -1)
                curses.init_pair(4, curses.COLOR_BLUE, -1)
                curses.init_pair(5, curses.COLOR_MAGENTA, -1)
                curses.init_pair(6, curses.COLOR_CYAN, -1)

                self.color_attrs["green"] = curses.color_pair(1)
                self.color_attrs["yellow"] = curses.color_pair(2)
                self.color_attrs["red"] = curses.color_pair(3)
                self.color_attrs["blue"] = curses.color_pair(4)
                self.color_attrs["magenta"] = curses.color_pair(5)
                self.color_attrs["cyan"] = curses.color_pair(6)

    def _get_status_attr(self, status_val: str) -> int:
        """Return the curses color attribute for a clinical status value."""
        val = str(status_val).lower().strip()
        color_name = (
            _HC_STATUS_COLOR_MAP.get(val) if self.is_high_contrast else _STATUS_COLOR_MAP.get(val)
        )
        if color_name and color_name in self.color_attrs:
            return self.color_attrs[color_name]
        return 0

    def filter_entries(self) -> None:
        """Filter in-memory entries immediately based on current search query."""
        if not self.search_query.strip():
            self.filtered_entries = list(self.all_entries)
        else:
            query = self.search_query.strip().lower()
            matches = []
            for row_dict, detail_obj in self.all_entries:
                row_tokens: list[str] = []
                for k, v in row_dict.items():
                    row_tokens.append(self._format_cell(v, key=str(k)))
                    row_tokens.extend(_collect_search_tokens(v))
                if query in " ".join(row_tokens).lower():
                    matches.append((row_dict, detail_obj))
                    continue

                meta, variables = extract_item_details(detail_obj)
                detail_tokens = _collect_search_tokens(meta) + _collect_search_tokens(variables)
                if query in " ".join(detail_tokens).lower():
                    matches.append((row_dict, detail_obj))

            self.filtered_entries = matches

        total = len(self.filtered_entries)
        if total == 0:
            self.selected_idx = 0
            self.top_idx = 0
        else:
            self.selected_idx = max(0, min(self.selected_idx, total - 1))
            self.top_idx = max(0, min(self.top_idx, total - 1))
        self.side_panel_scroll = 0
        self.col_offset = max(0, min(self.col_offset, self._max_col_offset()))

    def _format_cell(self, value: Any, key: str | None = None) -> str:
        """Format a single cell value for table display."""
        return _format_cell_value(value, key=key)

    def _layout_bounds(self, max_x: int) -> tuple[int, int, int, int]:
        """Compute (grid_width, grid_limit, panel_x, panel_width) for terminal width max_x."""
        if self.side_panel_open and max_x >= 50:
            grid_width = max(24, int(max_x * 0.55))
            panel_x = grid_width
            panel_width = max_x - panel_x
            grid_limit = max(1, panel_x - 1)
        else:
            grid_width = max_x
            panel_x = max_x
            panel_width = 0
            grid_limit = max(1, max_x)
        return grid_width, grid_limit, panel_x, panel_width

    def _compute_col_widths(self, grid_limit: int) -> tuple[list[str], list[int]]:
        """Compute formatted headers and readable column widths without crushing headers."""
        headers = [str(f).replace("_", " ").title() for f in self.fields]
        num_cols = len(self.fields)
        if num_cols == 0:
            return headers, []

        natural_widths = [len(h) for h in headers]
        sample_entries = self.filtered_entries[:100]
        for row_dict, _ in sample_entries:
            for idx, col in enumerate(self.fields):
                val_str = self._format_cell(row_dict.get(col), key=str(col))
                natural_widths[idx] = max(natural_widths[idx], len(val_str))

        min_widths = [
            max(len(headers[idx]), min(natural_widths[idx], _MIN_READABLE_CELL_WIDTH))
            for idx in range(num_cols)
        ]
        sep_len = 3 * (num_cols - 1)
        total_natural = sum(natural_widths) + sep_len

        if total_natural <= grid_limit:
            return headers, natural_widths

        total_min = sum(min_widths) + sep_len
        if total_min <= grid_limit:
            extra_budget = grid_limit - total_min
            slack = [natural_widths[i] - min_widths[i] for i in range(num_cols)]
            total_slack = sum(slack)
            if total_slack > 0:
                col_widths = [
                    min_widths[i] + int(extra_budget * slack[i] / total_slack)
                    for i in range(num_cols)
                ]
                return headers, col_widths
            return headers, min_widths

        # Wide table: preserve full header readability and allow horizontal scrolling
        col_widths = [
            max(min_widths[i], min(natural_widths[i], _MAX_SCROLL_COL_WIDTH))
            for i in range(num_cols)
        ]
        return headers, col_widths

    @staticmethod
    def _max_offset_for_widths(col_widths: list[int], grid_limit: int) -> int:
        """Return the maximum column offset where remaining columns fill or exceed grid_limit."""
        num_cols = len(col_widths)
        if num_cols <= 1:
            return 0
        total_all = sum(col_widths) + 3 * (num_cols - 1)
        if total_all <= grid_limit:
            return 0
        for start in range(num_cols):
            rem = sum(col_widths[start:]) + 3 * (num_cols - 1 - start)
            if rem <= grid_limit:
                return start
        return num_cols - 1

    def _max_col_offset(self) -> int:
        """Compute current maximum valid horizontal column offset."""
        if not self.fields:
            return 0
        try:
            _, max_x = self.stdscr.getmaxyx()
        except _CURSES_ERROR:
            return 0
        _, grid_limit, _, _ = self._layout_bounds(max_x)
        _, col_widths = self._compute_col_widths(grid_limit)
        return self._max_offset_for_widths(col_widths, grid_limit)

    def draw(self) -> None:
        """Render the complete data grid and inspection panel."""
        self.stdscr.erase()
        max_y, max_x = self.stdscr.getmaxyx()

        if max_y < 5 or max_x < 24:
            safe_addstr(self.stdscr, 0, 0, "Terminal window too small.")
            self.stdscr.refresh()
            return

        total_rows = len(self.filtered_entries)
        viewport_height = max(1, max_y - 4)

        # Keep selected row visible within viewport
        if self.selected_idx < self.top_idx:
            self.top_idx = self.selected_idx
        elif self.selected_idx >= self.top_idx + viewport_height:
            self.top_idx = self.selected_idx - viewport_height + 1

        _, grid_limit, panel_x, panel_width = self._layout_bounds(max_x)

        # Title / header bar at line 0
        mode_str = " [SEARCH MODE]" if self.is_searching else ""
        hc_str = " [HIGH-CONTRAST]" if self.is_high_contrast else ""
        panel_str = " [SIDE PANEL]" if (self.side_panel_open and panel_width > 0) else ""
        filter_status = f' [Filter: "{self.search_query}"]' if self.search_query else ""
        notice_str = f" [{self.status_notice}]" if self.status_notice else ""
        title = (
            f"iMednet {self.label.title()} Review ({total_rows} items)"
            f"{notice_str}{filter_status}{panel_str}{mode_str}{hc_str}"
        )
        safe_addstr(self.stdscr, 0, 0, title[:grid_limit], curses.A_BOLD)

        num_cols = len(self.fields)
        max_col_offset = 0

        if num_cols > 0:
            headers, col_widths = self._compute_col_widths(grid_limit)
            max_col_offset = self._max_offset_for_widths(col_widths, grid_limit)
            self.col_offset = max(0, min(self.col_offset, max_col_offset))

            # Render Table Headers (line 1) and Separator (line 2), strictly clipped to grid_limit
            header_parts: list[str] = []
            sep_parts: list[str] = []
            used_x = 0
            for col_idx in range(self.col_offset, num_cols):
                if used_x >= grid_limit:
                    break
                if col_idx > self.col_offset:
                    sep_avail = grid_limit - used_x
                    if sep_avail <= 0:
                        break
                    header_parts.append(" | "[:sep_avail])
                    sep_parts.append("-+-"[:sep_avail])
                    used_x += min(3, sep_avail)
                if used_x >= grid_limit:
                    break
                w = col_widths[col_idx]
                cell_avail = min(w, grid_limit - used_x)
                header_parts.append(headers[col_idx][:w].ljust(w)[:cell_avail])
                sep_parts.append("-" * cell_avail)
                used_x += cell_avail

            safe_addstr(self.stdscr, 1, 0, "".join(header_parts), curses.A_BOLD)
            safe_addstr(self.stdscr, 2, 0, "".join(sep_parts))

            # Render table rows in viewport, strictly clipped before panel_x (at grid_limit)
            for v_idx in range(viewport_height):
                row_idx = self.top_idx + v_idx
                y = 3 + v_idx
                if row_idx >= total_rows:
                    if total_rows == 0 and v_idx == 0:
                        empty_note = (
                            "No matching records found."
                            if self.search_query
                            else f"No {self.label} found."
                        )
                        note_avail = max(0, grid_limit - 2)
                        if note_avail > 0:
                            safe_addstr(self.stdscr, y, 2, empty_note[:note_avail], curses.A_DIM)
                    break

                row_dict, _ = self.filtered_entries[row_idx]
                is_selected = row_idx == self.selected_idx
                base_attr = (curses.A_REVERSE | curses.A_BOLD) if is_selected else 0

                curr_x = 0
                for col_idx in range(self.col_offset, num_cols):
                    if curr_x >= grid_limit:
                        break
                    if col_idx > self.col_offset:
                        sep_avail = grid_limit - curr_x
                        if sep_avail <= 0:
                            break
                        sep_str = " | "[:sep_avail]
                        safe_addstr(self.stdscr, y, curr_x, sep_str, base_attr)
                        curr_x += len(sep_str)
                    if curr_x >= grid_limit:
                        break

                    col = self.fields[col_idx]
                    w = col_widths[col_idx]
                    raw_val = row_dict.get(col)
                    cell_text = self._format_cell(raw_val, key=str(col))
                    cell_avail = min(w, grid_limit - curr_x)
                    formatted_cell = cell_text[:w].ljust(w)[:cell_avail]

                    col_attr = base_attr
                    if any(s in col.lower() for s in ("status", "state")):
                        status_attr = self._get_status_attr(cell_text)
                        if status_attr:
                            col_attr = base_attr | status_attr

                    safe_addstr(self.stdscr, y, curr_x, formatted_cell, col_attr)
                    curr_x += len(formatted_cell)

        # Draw Side Inspection Panel if open
        if self.side_panel_open and panel_width > 0:
            self._draw_side_panel(panel_x, panel_width, max_y)

        # Draw Bottom Status / Filter Bar at line max_y - 1
        self._draw_status_bar(max_y - 1, max_x, total_rows, max_col_offset)

        self.stdscr.refresh()

    def _wrap_panel_line(
        self, text: str, content_w: int, attr: int = 0, cont_indent: str = "    "
    ) -> list[tuple[str, int]]:
        """Wrap a panel line to content_w without dropping characters."""
        if not text or len(text) <= content_w:
            return [(text, attr)]
        wrapped = textwrap.wrap(
            text,
            width=max(8, content_w),
            subsequent_indent=cont_indent if content_w > len(cont_indent) + 4 else "",
            break_long_words=True,
            break_on_hyphens=False,
        )
        return [(line, attr) for line in wrapped] if wrapped else [(text[:content_w], attr)]

    def _format_panel_scalar(self, val: Any, key: str | None = None) -> str:
        """Format scalar, boolean, datetime, or enum value without mid-string truncation."""
        if isinstance(val, Enum):
            val = val.value
        if val is None or val == "":
            return "-"
        if isinstance(val, bool):
            return "Yes" if val else "No"
        if isinstance(val, datetime):
            return val.strftime("%Y-%m-%d %H:%M")
        return str(val)

    def _append_panel_value(
        self,
        lines: list[tuple[str, int]],
        display_key: str,
        value: Any,
        raw_key: str,
        content_w: int,
        indent: str = "  ",
    ) -> None:
        """Append formatted scalar or nested list/dict lines to the side panel."""
        if isinstance(value, dict):
            non_empty = {k: v for k, v in value.items() if v is not None and v != ""}
            if not non_empty:
                lines.extend(self._wrap_panel_line(f"{indent}{display_key}: {{}}", content_w))
                return
            lines.extend(self._wrap_panel_line(f"{indent}{display_key}:", content_w))
            for sub_k, sub_v in non_empty.items():
                self._append_panel_value(
                    lines,
                    str(sub_k),
                    sub_v,
                    str(sub_k),
                    content_w,
                    indent=indent + "  ",
                )
            return

        if isinstance(value, list):
            if not value:
                lines.extend(self._wrap_panel_line(f"{indent}{display_key}: []", content_w))
                return
            if all(isinstance(x, (str, int, float, bool, Enum, datetime)) for x in value):
                joined = ", ".join(self._format_panel_scalar(x) for x in value)
                lines.extend(
                    self._wrap_panel_line(
                        f"{indent}{display_key}: {joined}",
                        content_w,
                        cont_indent=indent + "  ",
                    )
                )
                return
            lines.extend(self._wrap_panel_line(f"{indent}{display_key}:", content_w))
            for entry in value:
                if isinstance(entry, dict):
                    entry_pairs = [
                        f"{ek}: {self._format_panel_scalar(ev, key=str(ek))}"
                        for ek, ev in entry.items()
                        if ev is not None and ev != ""
                    ]
                    item_str = ", ".join(entry_pairs) if entry_pairs else "{}"
                    lines.extend(
                        self._wrap_panel_line(
                            f"{indent}  - {item_str}",
                            content_w,
                            cont_indent=indent + "    ",
                        )
                    )
                else:
                    lines.extend(
                        self._wrap_panel_line(
                            f"{indent}  - {self._format_panel_scalar(entry)}",
                            content_w,
                            cont_indent=indent + "    ",
                        )
                    )
            return

        v_str = self._format_panel_scalar(value, key=raw_key)
        lines.extend(
            self._wrap_panel_line(
                f"{indent}{display_key}: {v_str}",
                content_w,
                cont_indent=indent + "  ",
            )
        )

    def _draw_side_panel(self, panel_x: int, panel_width: int, max_y: int) -> None:
        """Render the side inspection panel displaying variables and metadata."""
        viewport_lines = max_y - 3  # Lines from 1 to max_y - 2

        # Blank the entire panel area across title and viewport rows and draw divider
        safe_addstr(self.stdscr, 0, panel_x, " " * panel_width)
        blank_row = "│" + (" " * max(0, panel_width - 1))
        for y in range(1, max_y - 1):
            safe_addstr(self.stdscr, y, panel_x, blank_row)

        content_x = panel_x + 2
        content_w = max(10, panel_width - 3)

        is_records = self.label == "records"
        panel_title = " RECORD INSPECTION " if is_records else f" {self.label.upper()} DETAILS "
        safe_addstr(
            self.stdscr,
            1,
            content_x,
            panel_title.center(content_w, "─")[:content_w],
            curses.A_BOLD,
        )

        if not self.filtered_entries:
            empty_sel = "No record selected." if is_records else f"No {self.label} selected."
            safe_addstr(self.stdscr, 3, content_x, empty_sel[:content_w])
            return

        _, detail_obj = self.filtered_entries[self.selected_idx]
        meta, variables = extract_item_details(detail_obj)

        lines: list[tuple[str, int]] = []
        primary_header = "SUBMISSION METADATA" if is_records else f"{self.label.upper()} METADATA"
        lines.append((primary_header, curses.A_BOLD))

        record_primary_keys = [
            "record_id",
            "subject_key",
            "form_key",
            "record_status",
            "subject_status",
            "site_name",
            "date_created",
            "date_modified",
            "enrollment_start_date",
        ]
        if is_records:
            priority_keys = record_primary_keys
        else:
            seen_keys: set[str] = set()
            priority_keys = []
            for candidate in (*self.fields, *record_primary_keys):
                if candidate not in seen_keys:
                    seen_keys.add(candidate)
                    priority_keys.append(candidate)

        rendered_meta: set[str] = set()
        for k in priority_keys:
            if k in meta and meta[k] is not None and meta[k] != "":
                self._append_panel_value(
                    lines,
                    k.replace("_", " ").title(),
                    meta[k],
                    k,
                    content_w,
                )
                rendered_meta.add(k)

        other_meta = [
            (k, v) for k, v in meta.items() if k not in rendered_meta and v is not None and v != ""
        ]

        # If no priority keys matched for a non-record entity, show attributes directly under main header
        if not rendered_meta and not is_records and other_meta:
            for k, v in other_meta:
                self._append_panel_value(
                    lines,
                    k.replace("_", " ").title(),
                    v,
                    k,
                    content_w,
                )
                rendered_meta.add(k)
            other_meta = []

        if is_records or variables:
            lines.append(("", 0))
            lines.append(("FORM VARIABLES", curses.A_BOLD))
            if not variables:
                lines.append(("  (No submitted variables recorded)", curses.A_DIM))
            else:
                for var_key, var_val in sorted(variables.items()):
                    if var_val is None or var_val == "":
                        lines.extend(self._wrap_panel_line(f"  {var_key}: -", content_w))
                    else:
                        self._append_panel_value(lines, var_key, var_val, var_key, content_w)

        if other_meta:
            lines.append(("", 0))
            lines.append(("ADDITIONAL INFO", curses.A_BOLD))
            for k, v in other_meta:
                self._append_panel_value(
                    lines,
                    k.replace("_", " ").title(),
                    v,
                    k,
                    content_w,
                )

        # Render lines with scroll offset
        visible_slots = max(1, viewport_lines - 2)
        max_scroll = max(0, len(lines) - visible_slots)
        self.side_panel_scroll = max(0, min(self.side_panel_scroll, max_scroll))

        for idx in range(visible_slots):
            line_idx = self.side_panel_scroll + idx
            y = 2 + idx
            if line_idx >= len(lines):
                break
            text, attr = lines[line_idx]
            safe_addstr(self.stdscr, y, content_x, text[:content_w].ljust(content_w), attr)

    def _draw_status_bar(
        self, y: int, max_x: int, total_rows: int, max_col_offset: int = 0
    ) -> None:
        """Render the status or filter input bar at the bottom of the screen."""
        if self.is_searching:
            prompt = f"Filter (type to search): {self.search_query}_"
            safe_addstr(self.stdscr, y, 0, prompt[:max_x].ljust(max_x), curses.A_REVERSE)
            try:
                cursor_col = min(max_x - 1, len(f"Filter (type to search): {self.search_query}"))
                curses.curs_set(1)
                self.stdscr.move(y, cursor_col)
            except curses.error:
                pass
        else:
            with contextlib.suppress(curses.error):
                curses.curs_set(0)
            pos_info = f"Row {self.selected_idx + 1}/{total_rows}" if total_rows > 0 else "0/0"
            num_cols = len(self.fields)
            col_info = (
                f"  Col {self.col_offset + 1}/{num_cols}"
                if num_cols > 0 and (max_col_offset > 0 or self.col_offset > 0)
                else ""
            )
            notice_suffix = f"  [{self.status_notice}]" if self.status_notice else ""
            help_text = (
                f"[↑/↓/←/→] Navigate  [/] Search  [Space] Panel  [q] Quit  "
                f"{pos_info}{col_info}{notice_suffix}"
            )
            safe_addstr(self.stdscr, y, 0, help_text[:max_x].ljust(max_x), curses.A_REVERSE)

    def run(self) -> None:
        """Execute the interactive event loop until the user exits with 'q'."""
        while True:
            self.draw()
            try:
                ch = self.stdscr.getch()
            except KeyboardInterrupt:
                break
            except curses.error:
                continue

            if ch == curses.KEY_RESIZE:
                self.col_offset = max(0, min(self.col_offset, self._max_col_offset()))
                continue

            if self.is_searching:
                self._handle_search_input(ch)
            else:
                should_exit = self._handle_normal_input(ch)
                if should_exit:
                    break

    def _handle_search_input(self, ch: int) -> None:
        """Handle keystrokes while search input mode is active."""
        if ch in (10, 13, curses.KEY_ENTER) or ch == 27:  # Enter or Escape exits search input
            self.is_searching = False
            with contextlib.suppress(curses.error):
                curses.curs_set(0)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):  # Backspace
            if self.search_query:
                self.search_query = self.search_query[:-1]
                self.filter_entries()
        elif 32 <= ch <= 126:  # Printable character
            self.search_query += chr(ch)
            self.filter_entries()

    def _handle_normal_input(self, ch: int) -> bool:
        """Handle keystrokes in normal navigation mode.

        Returns:
            True if loop should exit, False otherwise.
        """
        max_y, _ = self.stdscr.getmaxyx()
        viewport_height = max(1, max_y - 4)
        total = len(self.filtered_entries)

        if ch in (ord("q"), ord("Q")):
            return True

        if ch == ord(" "):
            self.side_panel_open = not self.side_panel_open
            self.side_panel_scroll = 0
            self.col_offset = max(0, min(self.col_offset, self._max_col_offset()))
            return False

        if ch == ord("/"):
            self.is_searching = True
            return False

        if ch == 27:  # Escape in normal mode clears active filter
            if self.search_query:
                self.search_query = ""
                self.filter_entries()
            return False

        if ch in (curses.KEY_UP, ord("k")):
            if total > 0 and self.selected_idx > 0:
                self.selected_idx -= 1
                self.side_panel_scroll = 0

        elif ch in (curses.KEY_DOWN, ord("j")):
            if total > 0 and self.selected_idx < total - 1:
                self.selected_idx += 1
                self.side_panel_scroll = 0

        elif ch in (curses.KEY_LEFT, ord("h")):
            if self.col_offset > 0:
                self.col_offset = min(self.col_offset - 1, self._max_col_offset())

        elif ch in (curses.KEY_RIGHT, ord("l")):
            max_col = self._max_col_offset()
            if self.col_offset < max_col:
                self.col_offset += 1
            else:
                self.col_offset = max_col

        elif ch == curses.KEY_PPAGE:  # Page Up
            if total > 0:
                self.selected_idx = max(0, self.selected_idx - viewport_height)
                self.side_panel_scroll = 0

        elif ch == curses.KEY_NPAGE:  # Page Down
            if total > 0:
                self.selected_idx = min(total - 1, self.selected_idx + viewport_height)
                self.side_panel_scroll = 0

        elif ch in (curses.KEY_HOME, 362):  # Home
            if total > 0:
                self.selected_idx = 0
                self.side_panel_scroll = 0

        elif ch in (curses.KEY_END, 385):  # End
            if total > 0:
                self.selected_idx = total - 1
                self.side_panel_scroll = 0

        elif ch in (ord("["), ord("{")):  # Scroll side panel up
            if self.side_panel_open and self.side_panel_scroll > 0:
                self.side_panel_scroll -= 1

        elif ch in (ord("]"), ord("}")) and self.side_panel_open:  # Scroll side panel down
            self.side_panel_scroll += 1

        return False


def run_interactive_grid(
    items: Sequence[dict[str, Any]],
    fields: list[str],
    label: str = "records",
    details: Sequence[Any] | None = None,
    is_high_contrast: bool | None = None,
    status_notice: str | None = None,
) -> bool:
    """Launch the curses interactive data grid.

    Args:
        items: List of dictionary rows for table columns.
        fields: Column field names.
        label: Entity label (e.g. 'records', 'subjects').
        details: Optional list of full domain models for variable inspection.
        is_high_contrast: Optional high-contrast mode override.
        status_notice: Optional truncation/status notice to render in the grid bars.

    Returns:
        True if the interactive grid ran and exited normally, False if curses
        could not be initialized.
    """
    if curses is None:
        return False

    try:

        def _curses_main(stdscr: Any) -> None:
            grid = InteractiveDataGrid(
                stdscr,
                items=items,
                fields=fields,
                label=label,
                details=details,
                is_high_contrast=is_high_contrast,
                status_notice=status_notice,
            )
            grid.run()

        curses.wrapper(_curses_main)
        return True
    except _CURSES_ERROR:
        return False
    except Exception:
        # Guarantee terminal restoration if any unexpected error occurs
        if curses is not None:
            with contextlib.suppress(*_CURSES_ERROR):
                curses.endwin()
        return False
