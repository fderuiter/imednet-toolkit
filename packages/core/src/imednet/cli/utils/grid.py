"""Interactive keyboard data grid with side panel inspection using curses."""

from __future__ import annotations

import contextlib

try:
    import curses
except ImportError:
    curses = None  # type: ignore[assignment]

_CURSES_ERROR: tuple[type[BaseException], ...] = (curses.error,) if curses is not None else ()
import os
import sys
from collections.abc import Sequence
from typing import Any

from .output import _HC_STATUS_COLOR_MAP, _STATUS_COLOR_MAP, _format_cell_value


def is_interactive_terminal() -> bool:
    """Determine if stdout and stdin are connected to an active interactive TTY.

    Returns:
        True if interactive curses mode should be used, False if output should
        be emitted as plain tabular text.
    """
    if curses is None:
        return False
    if os.environ.get("IMEDNET_NON_INTERACTIVE") == "1":
        return False
    term = os.environ.get("TERM", "")
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
    """Extract submission metadata and form variables from a record or subject item.

    Args:
        detail: A Pydantic model, dictionary, or data object.

    Returns:
        A tuple of (metadata_dict, form_variables_dict).
    """
    if detail is None:
        return {}, {}

    data_dict: dict[str, Any] = {}
    if hasattr(detail, "model_dump"):
        data_dict = detail.model_dump(by_alias=False)
    elif hasattr(detail, "dict"):
        data_dict = detail.dict()
    elif isinstance(detail, dict):
        data_dict = dict(detail)
    else:
        data_dict = {k: v for k, v in vars(detail).items() if not k.startswith("_")}

    raw_vars = data_dict.pop("record_data", None)
    if raw_vars is None:
        raw_vars = data_dict.pop("data", None)

    variables: dict[str, Any] = {}
    if isinstance(raw_vars, dict):
        variables = dict(raw_vars)
    elif hasattr(raw_vars, "root") and isinstance(raw_vars.root, dict):
        variables = dict(raw_vars.root)
    elif hasattr(raw_vars, "model_dump"):
        variables = raw_vars.model_dump(by_alias=False)

    flattened_vars: dict[str, Any] = {}
    for k, v in variables.items():
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                flattened_vars[f"{k}.{sub_k}"] = sub_v
        else:
            flattened_vars[k] = v

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
                # Search row display values
                row_text = " ".join(str(v) for v in row_dict.values()).lower()
                if query in row_text:
                    matches.append((row_dict, detail_obj))
                    continue

                # Search detailed metadata and form variables
                meta, variables = extract_item_details(detail_obj)
                detail_text = (
                    " ".join(f"{k} {v}" for k, v in meta.items())
                    + " "
                    + " ".join(f"{k} {v}" for k, v in variables.items())
                ).lower()
                if query in detail_text:
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

    def _format_cell(self, value: Any, key: str | None = None) -> str:
        """Format a single cell value for table display."""
        return _format_cell_value(value, key=key)

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

        # Partition width between table grid and side panel
        if self.side_panel_open and max_x >= 50:
            grid_width = max(24, int(max_x * 0.55))
            panel_x = grid_width
            panel_width = max_x - panel_x
        else:
            grid_width = max_x
            panel_width = 0

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
        safe_addstr(self.stdscr, 0, 0, title[:grid_width], curses.A_BOLD)

        # Calculate column widths for table
        headers = [str(f).replace("_", " ").title() for f in self.fields]
        num_cols = len(self.fields)

        if num_cols > 0:
            col_widths = [len(h) for h in headers]
            # Sample up to first 100 rows for sizing
            sample_entries = self.filtered_entries[:100]
            for row_dict, _ in sample_entries:
                for idx, col in enumerate(self.fields):
                    val_str = self._format_cell(row_dict.get(col), key=str(col))
                    col_widths[idx] = max(col_widths[idx], len(val_str))

            sep_len = 3 * (num_cols - 1)  # " | " separators
            avail_table_width = max(10, grid_width - sep_len - (1 if panel_width > 0 else 0))

            sum_widths = sum(col_widths)
            if sum_widths > avail_table_width:
                # Scale columns down proportionally
                scale = avail_table_width / sum_widths
                col_widths = [max(4, int(w * scale)) for w in col_widths]

            # Render Table Headers at line 1
            header_parts = []
            for idx, h in enumerate(headers):
                w = col_widths[idx]
                header_parts.append(h[:w].ljust(w))
            header_line = " | ".join(header_parts)
            safe_addstr(self.stdscr, 1, 0, header_line[:grid_width], curses.A_BOLD)

            # Separator at line 2
            sep_parts = ["-" * w for w in col_widths]
            separator_line = "-+-".join(sep_parts)
            safe_addstr(self.stdscr, 2, 0, separator_line[:grid_width])

            # Render table rows in viewport
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
                        safe_addstr(self.stdscr, y, 2, empty_note, curses.A_DIM)
                    break

                row_dict, _ = self.filtered_entries[row_idx]
                is_selected = row_idx == self.selected_idx
                base_attr = (curses.A_REVERSE | curses.A_BOLD) if is_selected else 0

                curr_x = 0
                for col_idx, col in enumerate(self.fields):
                    w = col_widths[col_idx]
                    raw_val = row_dict.get(col)
                    cell_text = self._format_cell(raw_val, key=str(col))
                    formatted_cell = cell_text[:w].ljust(w)

                    # Highlight status columns
                    col_attr = base_attr
                    if any(s in col.lower() for s in ("status", "state")):
                        status_attr = self._get_status_attr(cell_text)
                        if status_attr:
                            col_attr = base_attr | status_attr

                    safe_addstr(self.stdscr, y, curr_x, formatted_cell, col_attr)
                    curr_x += len(formatted_cell)

                    if col_idx < num_cols - 1:
                        sep_str = " | "
                        safe_addstr(self.stdscr, y, curr_x, sep_str, base_attr)
                        curr_x += len(sep_str)

        # Draw Side Inspection Panel if open
        if self.side_panel_open and panel_width > 0:
            self._draw_side_panel(panel_x, panel_width, max_y)

        # Draw Bottom Status / Filter Bar at line max_y - 1
        self._draw_status_bar(max_y - 1, max_x, total_rows)

        self.stdscr.refresh()

    def _draw_side_panel(self, panel_x: int, panel_width: int, max_y: int) -> None:
        """Render the side inspection panel displaying variables and metadata."""
        viewport_lines = max_y - 3  # Lines from 1 to max_y - 2
        for y in range(1, max_y - 1):
            safe_addstr(self.stdscr, y, panel_x, "│")

        content_x = panel_x + 2
        content_w = max(10, panel_width - 3)

        panel_title = (
            " RECORD INSPECTION " if self.label == "records" else f" {self.label.upper()} DETAILS "
        )
        safe_addstr(
            self.stdscr,
            1,
            content_x,
            panel_title.center(content_w, "─")[:content_w],
            curses.A_BOLD,
        )

        if not self.filtered_entries:
            safe_addstr(self.stdscr, 3, content_x, "No record selected.")
            return

        _, detail_obj = self.filtered_entries[self.selected_idx]
        meta, variables = extract_item_details(detail_obj)

        lines: list[tuple[str, int]] = []
        lines.append(("SUBMISSION METADATA", curses.A_BOLD))

        primary_meta_keys = [
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

        rendered_meta = set()
        for k in primary_meta_keys:
            if k in meta and meta[k] is not None and meta[k] != "":
                v_str = self._format_cell(meta[k], key=k)
                lines.append((f"  {k.replace('_', ' ').title()}: {v_str}", 0))
                rendered_meta.add(k)

        lines.append(("", 0))
        lines.append(("FORM VARIABLES", curses.A_BOLD))

        if not variables:
            lines.append(("  (No submitted variables recorded)", curses.A_DIM))
        else:
            for var_key, var_val in sorted(variables.items()):
                v_str = self._format_cell(var_val, key=var_key)
                lines.append((f"  {var_key}: {v_str}", 0))

        # Optional other non-empty metadata
        other_meta = [
            (k, v)
            for k, v in meta.items()
            if k not in rendered_meta
            and v is not None
            and v not in ("", 0)
            and not isinstance(v, (dict, list))
        ]
        if other_meta:
            lines.append(("", 0))
            lines.append(("ADDITIONAL INFO", curses.A_BOLD))
            for k, v in other_meta:
                v_str = self._format_cell(v, key=k)
                lines.append((f"  {k.replace('_', ' ').title()}: {v_str}", 0))

        # Render lines with scroll offset
        max_scroll = max(0, len(lines) - (viewport_lines - 2))
        self.side_panel_scroll = max(0, min(self.side_panel_scroll, max_scroll))

        for idx in range(viewport_lines - 2):
            line_idx = self.side_panel_scroll + idx
            y = 2 + idx
            if line_idx >= len(lines):
                break
            text, attr = lines[line_idx]
            safe_addstr(self.stdscr, y, content_x, text[:content_w].ljust(content_w), attr)

    def _draw_status_bar(self, y: int, max_x: int, total_rows: int) -> None:
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
            notice_suffix = f"  [{self.status_notice}]" if self.status_notice else ""
            help_text = (
                f"[↑/↓] Navigate  [/] Search  [Space] Panel  [q] Quit  {pos_info}{notice_suffix}"
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
