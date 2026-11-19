"""Terminal user interface application for iMednet."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Container
from textual.message import Message
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Log,
    Static,
    TabbedContent,
    TabPane,
)

from imednet.spi.cli import is_interactive_terminal
from imednet.spi.constants import MAX_PAGE_SIZE
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet.spi.models import Job, JobState, JobStatus
from imednet.spi.utils import (
    InvalidJobStateError,
    InvalidJobTransitionError,
    JobFailedError,
    evaluate_job_state,
)

__all__ = [
    "DEFAULT_FETCH_LIMIT",
    "DashboardScreen",
    "ImednetTuiApp",
    "JobMonitor",
    "JobTable",
    "SiteList",
    "StudyList",
    "SubjectTable",
    "TextualLogHandler",
    "is_interactive_terminal",
    "run_tui",
]

DEFAULT_FETCH_LIMIT = 100

_INVALID_ID_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]")
_VALID_ID_START_RE = re.compile(r"^[A-Za-z_]")
_BATCH_TOKEN_SPLIT_RE = re.compile(r"[\s,]+")
_SUCCESS_STATE_NAMES: frozenset[str] = frozenset({"COMPLETED", "COMPLETE", "SUCCESS"})
_ACTIVE_STATE_NAMES: frozenset[str] = frozenset(
    {"PROCESSING", "PENDING", "RUNNING", "QUEUED", "OPEN", "ACTIVE"}
)


def _normalize_batch_ids(raw_values: Sequence[str] | str | None) -> list[str]:
    """Parse one or more batch ID strings into a deduplicated ordered list."""
    if raw_values is None:
        return []
    entries = [raw_values] if isinstance(raw_values, str) else list(raw_values)
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for token in _BATCH_TOKEN_SPLIT_RE.split(str(entry)):
            cleaned = token.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                normalized.append(cleaned)
    return normalized


def _sanitize_dom_id(prefix: str, raw_value: object, used_ids: set[str]) -> str:
    """Build a unique Textual DOM identifier matching ``[A-Za-z_][A-Za-z0-9_-]*``.

    Args:
        prefix: Leading namespace prefix such as ``study`` or ``site``.
        raw_value: Raw SDK identifier value that may contain dots or symbols.
        used_ids: Set of already-allocated DOM IDs within the parent widget.

    Returns:
        Sanitized, collision-free DOM identifier string.
    """
    cleaned_prefix = _INVALID_ID_CHARS_RE.sub("_", prefix) or "item"
    if not _VALID_ID_START_RE.match(cleaned_prefix):
        cleaned_prefix = f"_{cleaned_prefix}"
    cleaned_value = _INVALID_ID_CHARS_RE.sub("_", str(raw_value)) or "item"
    base_id = f"{cleaned_prefix}-{cleaned_value}"
    candidate = base_id
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base_id}-{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _escape_markup(text: str) -> str:
    """Escape square brackets so dynamic text is rendered literally by Rich markup."""
    return text.replace("[", r"\[")


async def _fetch_bounded_facade_items(
    sdk: ImednetFacade | AsyncImednetFacade,
    async_method_name: str,
    sync_method_name: str,
    *args: Any,
    limit: int | None = None,
    **filters: Any,
) -> tuple[list[Any], bool]:
    """Fetch items through the typed async or sync SPI facade with bounded pagination."""
    req_filters: dict[str, Any] = dict(filters)
    if limit is not None:
        req_filters["limit"] = min(limit + 1, MAX_PAGE_SIZE)

    async_fn: Callable[..., Any] | None = getattr(sdk, async_method_name, None)
    if inspect.iscoroutinefunction(async_fn):
        raw_items = await async_fn(*args, **req_filters)
    else:
        sync_fn: Callable[..., Any] = getattr(sdk, sync_method_name)
        raw_items = await asyncio.to_thread(sync_fn, *args, **req_filters)

    items = list(raw_items)
    if limit is None:
        return items, False
    truncated = len(items) > limit
    return items[:limit], truncated


def _sync_widget_truncation(widget: Any, count: int, truncated: bool) -> None:
    """Update parent container subtitle and dashboard truncation banner."""
    with contextlib.suppress(Exception):
        if isinstance(widget.parent, Container):
            widget.parent.border_subtitle = (
                f"First {count} shown (press 'a' for all)" if truncated else ""
            )
        screen: Any = widget.screen
        if isinstance(screen, DashboardScreen):
            screen.refresh_truncation_notice()


def _fallback_item_label(item: ListItem, default: str) -> str:
    """Extract human-readable text from a ``ListItem`` when unmapped."""
    try:
        label_widget = item.query_one(Label)
        rendered = getattr(label_widget, "renderable", None) or label_widget.render()
        return str(rendered)
    except Exception:
        return default


class TextualLogHandler(logging.Handler):
    """Logging handler routing records to a Textual Log widget."""

    def __init__(self, log_widget: Log) -> None:
        """Initialize handler with target Log widget.

        Args:
            log_widget: The Textual Log widget to write to.
        """
        super().__init__()
        self.log_widget = log_widget

    def emit(self, record: logging.LogRecord) -> None:
        """Format and write log record to widget.

        Args:
            record: The logging LogRecord to emit.
        """
        try:
            msg = self.format(record)
            self.log_widget.write_line(msg)
        except Exception:
            self.handleError(record)


class StudyList(ListView):
    """List of available clinical studies."""

    class StudySelected(Message):
        """Event emitted when a study is selected."""

        def __init__(self, study_key: str, study_name: str) -> None:
            """Initialize the study selected event.

            Args:
                study_key: Unique identifier of the study.
                study_name: Human-readable study name.
            """
            self.study_key = study_key
            self.study_name = study_name
            super().__init__()

    def __init__(
        self,
        sdk: ImednetFacade | AsyncImednetFacade,
        *,
        preferred_study_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the StudyList widget.

        Args:
            sdk: Active iMednet SDK facade instance.
            preferred_study_key: Optional study key to highlight when initially loaded.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.preferred_study_key = preferred_study_key
        self.loaded = False
        self.truncated = False
        self.status_notice = ""
        self._study_items: dict[str, tuple[str, str]] = {}

    async def on_mount(self) -> None:
        """Load studies automatically upon mounting the widget."""
        await self.load_studies()

    async def load_studies(self, *, load_all: bool = False) -> None:
        """Fetch and populate study items from the SDK facade asynchronously.

        Args:
            load_all: When True, fetch all pages instead of the initial bounded batch.
        """
        was_loaded = self.loaded
        prev_index = self.index if was_loaded else None
        if self.children:
            await self.clear()
        self._study_items.clear()
        self.loading = True
        limit = None if load_all else DEFAULT_FETCH_LIMIT
        try:
            studies, truncated = await _fetch_bounded_facade_items(
                self.sdk,
                "async_get_studies",
                "get_studies",
                limit=limit,
            )

            used_ids: set[str] = set()
            items: list[ListItem] = []
            study_map: dict[str, tuple[str, str]] = {}
            for study in studies:
                name = str(
                    getattr(study, "study_name", None)
                    or getattr(study, "name", None)
                    or "Unknown Study"
                )
                key = str(getattr(study, "study_key", "no-key"))
                item_id = _sanitize_dom_id("study", key, used_ids)
                study_map[item_id] = (key, name)
                items.append(ListItem(Label(f"{name} ({key})"), id=item_id))

            self._study_items = study_map
            if self.is_attached:
                if self.children:
                    await self.clear()
                await self.extend(items)
                if items:
                    preferred_index: int | None = None
                    if not was_loaded and self.preferred_study_key:
                        for idx, (_, (key, _)) in enumerate(study_map.items()):
                            if key == self.preferred_study_key:
                                preferred_index = idx
                                break
                    if preferred_index is not None:
                        target_index = preferred_index
                    elif prev_index is not None:
                        target_index = min(prev_index, len(items) - 1)
                    else:
                        target_index = 0
                    self._initial_index = target_index
                    self.index = target_index
            self.loaded = True
            self.truncated = truncated
            self.status_notice = (
                f"Showing first {len(items)} studies (truncated — press 'a' to load all)"
                if truncated
                else ""
            )
            _sync_widget_truncation(self, len(items), truncated)
        except Exception as e:
            if hasattr(self.app, "notify"):
                self.app.notify(f"Error loading studies: {e}", severity="error")
        finally:
            self.loading = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle selection event in the list view.

        Args:
            event: The ListView selection event.
        """
        if event.item and event.item.id:
            mapped = self._study_items.get(event.item.id)
            if mapped is not None:
                study_key, study_name = mapped
            else:
                study_key = event.item.id.removeprefix("study-")
                study_name = _fallback_item_label(event.item, study_key)
            self.post_message(self.StudySelected(study_key, study_name))


class SiteList(ListView):
    """List of sites for a selected study."""

    class SiteSelected(Message):
        """Event emitted when a site is selected."""

        def __init__(self, site_id: str, site_name: str) -> None:
            """Initialize the site selected event.

            Args:
                site_id: Identifier of the site.
                site_name: Human-readable site name.
            """
            self.site_id = site_id
            self.site_name = site_name
            super().__init__()

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade, **kwargs: Any) -> None:
        """Initialize the SiteList widget.

        Args:
            sdk: Active iMednet SDK facade instance.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.current_study_key: str | None = None
        self.truncated = False
        self.status_notice = ""
        self._site_items: dict[str, tuple[str, str]] = {}

    async def load_sites(self, study_key: str, *, load_all: bool = False) -> None:
        """Fetch and populate site items for a given study key.

        Args:
            study_key: Study key to load sites for.
            load_all: When True, fetch all pages instead of the initial bounded batch.
        """
        prev_index = self.index if self.current_study_key == study_key else None
        self.current_study_key = study_key
        if self.children:
            await self.clear()
        self._site_items.clear()
        self.loading = True
        limit = None if load_all else DEFAULT_FETCH_LIMIT
        try:
            sites, truncated = await _fetch_bounded_facade_items(
                self.sdk,
                "async_get_sites",
                "get_sites",
                study_key,
                limit=limit,
            )

            used_ids: set[str] = set()
            items: list[ListItem] = []
            site_map: dict[str, tuple[str, str]] = {}
            for site in sites:
                site_id = str(getattr(site, "site_id", "no-id"))
                name = str(getattr(site, "site_name", None) or "Unknown Site")
                item_id = _sanitize_dom_id("site", site_id, used_ids)
                site_map[item_id] = (site_id, name)
                items.append(ListItem(Label(f"{name} ({site_id})"), id=item_id))

            self._site_items = site_map
            if self.is_attached:
                if self.children:
                    await self.clear()
                self.extend(items)
                if items:
                    self.index = min(prev_index, len(items) - 1) if prev_index is not None else 0
            self.truncated = truncated
            self.status_notice = (
                f"Showing first {len(items)} sites (truncated — press 'a' to load all)"
                if truncated
                else ""
            )
            _sync_widget_truncation(self, len(items), truncated)
        except Exception as e:
            if hasattr(self.app, "notify"):
                self.app.notify(f"Error loading sites: {e}", severity="error")
        finally:
            self.loading = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle selection event in the site list view.

        Args:
            event: The ListView selection event.
        """
        if event.item and event.item.id:
            mapped = self._site_items.get(event.item.id)
            if mapped is not None:
                site_id, site_name = mapped
            else:
                site_id = event.item.id.removeprefix("site-")
                site_name = _fallback_item_label(event.item, site_id)
            self.post_message(self.SiteSelected(site_id, site_name))


class SubjectTable(DataTable[str]):
    """Interactive table displaying subjects."""

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade, **kwargs: Any) -> None:
        """Initialize the SubjectTable widget.

        Args:
            sdk: Active iMednet SDK facade instance.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.cursor_type = "row"
        self.current_study_key: str | None = None
        self.current_site_id: str | None = None
        self.truncated = False
        self.status_notice = ""

    def on_mount(self) -> None:
        """Initialize table columns when widget is mounted."""
        if not self.columns:
            self.add_columns("Subject ID", "Site ID", "Status", "Created")

    async def load_subjects(
        self,
        study_key: str,
        site_id: str | None = None,
        *,
        load_all: bool = False,
    ) -> None:
        """Fetch and populate subject rows for a study with optional site filtering.

        Args:
            study_key: Study key to query subjects from.
            site_id: Optional site identifier to filter results directly via the SDK facade.
            load_all: When True, fetch all pages instead of the initial bounded batch.
        """
        self.current_study_key = study_key
        self.current_site_id = site_id
        if self.is_mounted:
            self.clear()
        self.loading = True
        limit = None if load_all else DEFAULT_FETCH_LIMIT
        try:
            filters: dict[str, Any] = {}
            if site_id is not None:
                filters["site_id"] = site_id
            subjects, truncated = await _fetch_bounded_facade_items(
                self.sdk,
                "async_get_subjects",
                "get_subjects",
                study_key,
                limit=limit,
                **filters,
            )

            rows = []
            for sub in subjects:
                s_site_id = str(getattr(sub, "site_id", "") or "")
                raw_sub_id = getattr(sub, "subject_id", None)
                sub_id = (
                    raw_sub_id
                    if raw_sub_id is not None
                    else (getattr(sub, "subject_key", None) or "-")
                )
                status = getattr(sub, "subject_status", None) or getattr(sub, "status", None) or "-"
                created = str(getattr(sub, "date_created", None) or "-")
                rows.append((str(sub_id), s_site_id, str(status), created))
            if self.is_mounted:
                self.clear()
            self.add_rows(rows)
            self.truncated = truncated
            self.status_notice = (
                f"Showing first {len(rows)} subjects (truncated — press 'a' to load all)"
                if truncated
                else ""
            )
            _sync_widget_truncation(self, len(rows), truncated)
        except Exception as e:
            if hasattr(self.app, "notify"):
                self.app.notify(f"Error loading subjects: {e}", severity="error")
        finally:
            self.loading = False


class _TrackedJobPlaceholder:
    """Representation for tracked jobs whose status could not be retrieved."""

    def __init__(self, job_id: str, status: str = "FAILED") -> None:
        """Initialize placeholder with job ID and status.

        Args:
            job_id: Identifier of the tracked job.
            status: Status display string, such as FAILED or UNKNOWN.
        """
        self.job_id = job_id
        self.batch_id = job_id
        self.job_type = "Job"
        self.state = status
        self.status = status
        self.job_status = status
        self.date_created = ""
        self.is_terminal = False


@dataclass
class _TrackedJobEntry:
    """Per-study tracked job state for monotonic evaluation and terminal freezing."""

    job_id: str
    last_job: Any = None
    last_state: JobState | str | None = None
    is_terminal: bool = False
    warning: str | None = None


def _extract_entry_row(entry: _TrackedJobEntry) -> tuple[str, str, str, str, str, str]:
    """Extract ``(batch_id, state, progress, total, failed, created)`` strings for an entry."""
    job = entry.last_job
    if job is None:
        batch_id = entry.job_id.strip() if entry.job_id.strip() else "-"
        state_str = "WARNING" if entry.warning else "ACTIVE"
        return (batch_id, state_str, "-", "-", "-", "-")

    batch_id = ""
    for attr in ("batch_id", "job_id"):
        raw_id = getattr(job, attr, None)
        if isinstance(raw_id, str) and raw_id.strip():
            batch_id = raw_id.strip()
            break
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            batch_id = str(raw_id)
            break
    if not batch_id:
        batch_id = entry.job_id.strip() if entry.job_id.strip() else "-"

    raw_state = getattr(job, "state", None)
    if raw_state is None and not isinstance(job, _TrackedJobPlaceholder):
        raw_state = getattr(job, "status", None) or getattr(job, "job_status", None)

    if raw_state is None:
        state_str = "ACTIVE"
    elif isinstance(raw_state, Enum):
        state_str = str(raw_state.value)
    else:
        cleaned_state = str(raw_state).strip()
        state_str = cleaned_state if cleaned_state else "ACTIVE"

    state_upper = state_str.upper()
    raw_progress = getattr(job, "progress", None)
    if isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool):
        progress_str = f"{int(raw_progress)}%"
    elif isinstance(raw_progress, str) and raw_progress.strip():
        cleaned_prog = raw_progress.strip()
        progress_str = cleaned_prog if cleaned_prog.endswith("%") else f"{cleaned_prog}%"
    elif state_upper in _SUCCESS_STATE_NAMES:
        progress_str = "100%"
    elif isinstance(job, (Job, JobStatus)) and not isinstance(job, _TrackedJobPlaceholder):
        progress_str = "0%"
    else:
        progress_str = "-"

    if isinstance(job, _TrackedJobPlaceholder):
        total_str = "-"
        failed_str = "-"
    else:
        raw_total = getattr(job, "total_records", getattr(job, "record_total_count", None))
        total_str = (
            str(raw_total)
            if isinstance(raw_total, int) and not isinstance(raw_total, bool)
            else "-"
        )
        raw_failed = getattr(job, "failed_records", getattr(job, "record_failure_count", None))
        failed_str = (
            str(raw_failed)
            if isinstance(raw_failed, int) and not isinstance(raw_failed, bool)
            else "-"
        )

    raw_created = getattr(job, "date_created", None)
    created_str = (
        raw_created.strip() if isinstance(raw_created, str) and raw_created.strip() else "-"
    )

    return (batch_id, state_str, progress_str, total_str, failed_str, created_str)


class JobTable(DataTable[str]):
    """Keyboard-scrollable table displaying tracked background study jobs."""

    JOB_COLUMNS: ClassVar[tuple[str, ...]] = (
        "Batch ID",
        "State",
        "Progress",
        "Total",
        "Failed",
        "Created",
    )

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the JobTable widget."""
        super().__init__(**kwargs)
        self.cursor_type = "row"

    def on_mount(self) -> None:
        """Initialize job table columns on mount."""
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Ensure job columns exist before populating rows."""
        if not self.columns:
            self.add_columns(*self.JOB_COLUMNS)

    def set_job_rows(
        self,
        rows: Sequence[tuple[str, str, str, str, str, str]],
        *,
        preserve_cursor: bool = True,
    ) -> None:
        """Replace displayed job rows while optionally preserving cursor position."""
        self._ensure_columns()
        prev_row = self.cursor_row if preserve_cursor else 0
        self.clear()
        if rows:
            self.add_rows(rows)
            if preserve_cursor and prev_row > 0:
                self.move_cursor(row=min(prev_row, len(rows) - 1))


class JobMonitor(Container):
    """Widget registering, polling, and displaying study background jobs."""

    DEFAULT_CSS = """
    JobMonitor {
        layout: vertical;
        height: 1fr;
        background: $surface;
        border: solid $primary;
    }

    JobMonitor #batch_id_input {
        height: 1;
        border: none;
        padding: 0 1;
        background: $boost;
        color: $text;
    }

    JobMonitor #batch_id_input:focus {
        background: $panel;
        text-style: bold;
    }

    JobMonitor #job_monitor_notice {
        height: auto;
        padding: 0 1;
        color: $text;
    }

    JobMonitor #job_table {
        height: 1fr;
    }
    """

    MAX_CONCURRENT_POLLS: ClassVar[int] = 5

    def __init__(
        self,
        sdk: ImednetFacade | AsyncImednetFacade,
        *,
        max_concurrency: int = MAX_CONCURRENT_POLLS,
        **kwargs: Any,
    ) -> None:
        """Initialize the JobMonitor widget.

        Args:
            sdk: Active iMednet SDK facade instance.
            max_concurrency: Maximum number of concurrent status polling requests.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.max_concurrency = max_concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self.timer: Timer | None = None
        self._polling_active = False
        self.current_study_key: str | None = None
        self._study_jobs: dict[str, dict[str, _TrackedJobEntry]] = {}
        self._unassigned_job_ids: list[str] = []
        self._rendered_study_key: str | None = None
        self._summary_text = "Job Monitor: Select a study to view background jobs."

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Asyncio semaphore bounding simultaneous job status queries."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    @property
    def tracked_job_ids(self) -> list[str]:
        """Return tracked job IDs in registration order for the active study."""
        if self.current_study_key is not None:
            return list(self._study_jobs.get(self.current_study_key, ()))
        return list(self._unassigned_job_ids)

    @tracked_job_ids.setter
    def tracked_job_ids(self, value: Sequence[str]) -> None:
        normalized = _normalize_batch_ids(list(value))
        if self.current_study_key is not None:
            existing = self._study_jobs.get(self.current_study_key, {})
            self._study_jobs[self.current_study_key] = {
                jid: existing.get(jid, _TrackedJobEntry(job_id=jid)) for jid in normalized
            }
        else:
            self._unassigned_job_ids = normalized

    @property
    def is_polling_active(self) -> bool:
        """Return True when the background refresh timer is unpaused."""
        return self._polling_active

    def _pause_timer(self) -> None:
        """Pause the background polling timer if initialized."""
        self._polling_active = False
        if self.timer is not None:
            self.timer.pause()

    def _resume_timer(self) -> None:
        """Resume the background polling timer if initialized."""
        if self.timer is not None:
            self.timer.resume()
            self._polling_active = True

    def _entries_for_study(self, study_key: str) -> dict[str, _TrackedJobEntry]:
        """Return the ordered tracked-job map for ``study_key``."""
        entries = self._study_jobs.setdefault(study_key, {})
        if self._unassigned_job_ids and study_key == self.current_study_key:
            for jid in self._unassigned_job_ids:
                entries.setdefault(jid, _TrackedJobEntry(job_id=jid))
            self._unassigned_job_ids.clear()
        return entries

    def _has_active_tracked_jobs(self, study_key: str) -> bool:
        """Return True if ``study_key`` has unpolled or non-terminal tracked jobs."""
        entries = self._study_jobs.get(study_key)
        if not entries:
            return False
        return any(not entry.is_terminal for entry in entries.values())

    def _sync_timer_for_study(self, study_key: str | None) -> None:
        """Resume polling timer only when active jobs exist; pause otherwise."""
        if self.timer is None:
            self._polling_active = False
            return
        if not study_key:
            self._pause_timer()
            return
        if self._has_active_tracked_jobs(study_key):
            self._resume_timer()
        else:
            self._pause_timer()

    def compose(self) -> ComposeResult:
        """Compose inline batch input, status notice, and scrollable jobs table."""
        yield Input(
            placeholder="Enter batch ID(s) to track (e.g. BATCH-001) and press Enter...",
            id="batch_id_input",
        )
        yield Static(self._summary_text, id="job_monitor_notice")
        yield JobTable(id="job_table")

    def update(self, content: str = "") -> None:
        """Store summary markup text for non-composited inspection and testing."""
        self._summary_text = content

    def render(self) -> Text:
        """Return summary text representation for non-composited inspection."""
        return Text.from_markup(self._summary_text)

    def _update_ui(
        self,
        *,
        summary_text: str,
        notice_text: str | None = None,
        rows: Sequence[tuple[str, str, str, str, str, str]] = (),
        preserve_cursor: bool = True,
    ) -> None:
        """Synchronize summary text, notice banner, and table rows when mounted."""
        self.update(summary_text)
        if not self.is_mounted:
            return
        with contextlib.suppress(Exception):
            notice = self.query_one("#job_monitor_notice", Static)
            notice.update(notice_text if notice_text is not None else summary_text)
        with contextlib.suppress(Exception):
            table = self.query_one("#job_table", JobTable)
            table.set_job_rows(rows, preserve_cursor=preserve_cursor)

    def on_mount(self) -> None:
        """Set up status message and interval polling timer on mount."""
        self.timer = self.set_interval(10.0, self.refresh_jobs, pause=True)
        self._polling_active = False
        if self.current_study_key:
            self._sync_timer_for_study(self.current_study_key)
            self.run_worker(self.refresh_jobs())
        else:
            self._update_ui(
                summary_text="Job Monitor: Select a study to view background jobs.",
                notice_text=(
                    "Select a study to monitor jobs. Upstream iMednet APIs require batch IDs; "
                    "enter batch ID(s) above once a study is active."
                ),
            )

    def track_jobs(
        self,
        job_ids: Sequence[str] | str,
        *,
        study_key: str | None = None,
        refresh: bool = True,
    ) -> list[str]:
        """Register one or more batch/job IDs for a study.

        Args:
            job_ids: Batch ID string or sequence of batch IDs to register.
            study_key: Study key to partition the IDs under; defaults to active study.
            refresh: When True, trigger an immediate poll if the study is active.

        Returns:
            Normalized batch IDs processed from ``job_ids``.
        """
        parsed_ids = _normalize_batch_ids(job_ids)
        if not parsed_ids:
            return []

        target_study = study_key if study_key is not None else self.current_study_key
        if target_study:
            entries = self._entries_for_study(target_study)
            for jid in parsed_ids:
                existing = entries.get(jid)
                if existing is None:
                    entries[jid] = _TrackedJobEntry(job_id=jid)
                elif (
                    isinstance(existing.last_job, _TrackedJobPlaceholder)
                    and not existing.is_terminal
                ):
                    existing.last_job = None
        else:
            for jid in parsed_ids:
                if jid not in self._unassigned_job_ids:
                    self._unassigned_job_ids.append(jid)

        if refresh and target_study and target_study == self.current_study_key:
            self._sync_timer_for_study(target_study)
            if self.is_mounted:
                self.run_worker(self.refresh_jobs())
        return parsed_ids

    def track_job(self, job_id: str, study_key: str | None = None) -> None:
        """Register a known job ID to monitor for the specified or active study.

        Args:
            job_id: Known background job or batch identifier.
            study_key: Optional study key override; defaults to ``current_study_key``.
        """
        self.track_jobs(job_id, study_key=study_key, refresh=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Register batch IDs submitted from the inline input control."""
        if event.input.id != "batch_id_input":
            return
        batch_ids = _normalize_batch_ids(event.value)
        event.input.value = ""
        if not batch_ids:
            return
        if not self.current_study_key:
            if hasattr(self.app, "notify"):
                self.app.notify("Select a study before adding batch IDs.", severity="warning")
            return
        self.track_jobs(batch_ids, study_key=self.current_study_key, refresh=True)

    def set_study(self, study_key: str) -> None:
        """Set the active study key, display cached study jobs, and trigger refresh.

        Args:
            study_key: Active study key to track jobs for.
        """
        self.current_study_key = study_key
        entries = self._entries_for_study(study_key)
        self._sync_timer_for_study(study_key)
        if entries and any(
            e.last_job is not None or e.warning is not None for e in entries.values()
        ):
            self._render_study_entries(study_key, entries)
        else:
            loading_msg = f"Job Monitor: Loading jobs for study '{_escape_markup(study_key)}'..."
            self._update_ui(
                summary_text=loading_msg,
                notice_text=loading_msg,
                rows=(),
                preserve_cursor=False,
            )
        if self.is_mounted:
            self.run_worker(self.refresh_jobs())

    async def _fetch_job_status(self, study_key: str, jid: str) -> Any:
        """Retrieve a single job status through the async or sync SPI facade."""
        async_fn: Callable[..., Any] | None = getattr(self.sdk, "async_get_job", None)
        if inspect.iscoroutinefunction(async_fn):
            return await async_fn(study_key, jid)
        sync_fn = cast(ImednetFacade, self.sdk).get_job
        return await asyncio.to_thread(sync_fn, study_key, jid)

    def _apply_job_evaluation(self, entry: _TrackedJobEntry, job: Any) -> None:
        """Evaluate ``job`` monotonically via ``evaluate_job_state`` and update ``entry``."""
        if entry.is_terminal:
            if job is None or not getattr(job, "is_terminal", False):
                logging.getLogger("imednet_tui").warning(
                    "Ignoring stale response for job %s (state=%r); terminal state %s preserved",
                    entry.job_id,
                    getattr(job, "state", None),
                    entry.last_state,
                )
            return

        if job is None:
            entry.last_job = _TrackedJobPlaceholder(entry.job_id, "UNKNOWN")
            entry.warning = None
            return

        try:
            is_done = evaluate_job_state(job, previous_state=entry.last_state)
        except JobFailedError:
            entry.last_job = job
            raw_state = getattr(job, "state", None)
            if raw_state is not None:
                entry.last_state = raw_state
            entry.is_terminal = True
            entry.warning = None
        except (InvalidJobStateError, InvalidJobTransitionError) as err:
            logging.getLogger("imednet_tui").warning(
                "Invalid job state update for %s: %s", entry.job_id, err
            )
            entry.warning = str(err)
        else:
            entry.last_job = job
            raw_state = getattr(job, "state", None)
            if raw_state is not None:
                entry.last_state = raw_state
            entry.warning = None
            if is_done:
                entry.is_terminal = True

    async def _poll_tracked_entry(
        self,
        study_key: str,
        entry: _TrackedJobEntry,
    ) -> None:
        """Poll status for a single non-terminal tracked job under the concurrency semaphore."""
        if entry.is_terminal:
            return
        async with self.semaphore:
            if entry.is_terminal:
                return
            try:
                job = await self._fetch_job_status(study_key, entry.job_id)
            except Exception as err:
                if entry.is_terminal:
                    logging.getLogger("imednet_tui").warning(
                        "Ignoring transient error for terminal job %s: %s",
                        entry.job_id,
                        err,
                    )
                    return
                if (
                    isinstance(err, (InvalidJobStateError, InvalidJobTransitionError))
                    or err.__class__.__name__ == "ValidationError"
                    or "Invalid job state" in str(err)
                ):
                    logging.getLogger("imednet_tui").warning(
                        "Invalid job state update for %s: %s", entry.job_id, err
                    )
                    entry.warning = str(err)
                    return
                logging.getLogger("imednet_tui").warning(
                    "Failed to poll job %s: %s", entry.job_id, err
                )
                if entry.last_job is None or isinstance(entry.last_job, _TrackedJobPlaceholder):
                    entry.last_job = _TrackedJobPlaceholder(entry.job_id, "FAILED")
                    entry.warning = None
                else:
                    entry.warning = f"Poll error: {err}"
                return

            self._apply_job_evaluation(entry, job)

    @staticmethod
    def _format_entry_line(entry: _TrackedJobEntry) -> str:
        """Format a single tracked job entry into Rich-safe markup."""
        job = entry.last_job
        if job is None:
            safe_jid = _escape_markup(entry.job_id)
            if entry.warning:
                safe_warn = _escape_markup(entry.warning)
                return f"[yellow]WARNING[/yellow] - Job ({safe_jid}) (Warning: {safe_warn})"
            return f"[yellow]ACTIVE[/yellow] - Job ({safe_jid})"

        batch_id, state_str, progress_str, total_str, failed_str, created_str = _extract_entry_row(
            entry
        )
        raw_type = getattr(job, "job_type", None)
        type_label = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else "Job"
        upper_status = state_str.upper()
        if getattr(job, "is_failed", False):
            color = "red"
        elif upper_status in _SUCCESS_STATE_NAMES:
            color = "green"
        elif upper_status in _ACTIVE_STATE_NAMES:
            color = "yellow"
        else:
            color = "red"

        safe_status = _escape_markup(state_str)
        safe_label = _escape_markup(f"{type_label} ({batch_id})" if batch_id != "-" else type_label)
        metrics_suffix = (
            ""
            if isinstance(job, _TrackedJobPlaceholder)
            else (
                f" [{_escape_markup(progress_str)}, "
                f"total={_escape_markup(total_str)}, failed={_escape_markup(failed_str)}]"
            )
        )
        created_suffix = f" ({_escape_markup(created_str)})" if created_str != "-" else ""
        warning_suffix = (
            f" [yellow](Warning: {_escape_markup(entry.warning)})[/yellow]" if entry.warning else ""
        )
        return (
            f"[{color}]{safe_status}[/{color}] - "
            f"{safe_label}{metrics_suffix}{created_suffix}{warning_suffix}"
        )

    def _render_study_entries(
        self,
        study_key: str,
        entries: dict[str, _TrackedJobEntry],
    ) -> None:
        """Render summary markup, notice banner, and table rows for ``entries``."""
        entry_list = list(entries.values())
        rows: list[tuple[str, str, str, str, str, str]] = []
        lines = [f"[bold]Active & Recent Jobs ({len(entry_list)})[/bold]"]
        active_count = 0
        warnings: list[str] = []
        for entry in entry_list:
            rows.append(_extract_entry_row(entry))
            lines.append(self._format_entry_line(entry))
            if not entry.is_terminal:
                active_count += 1
            if entry.warning:
                warnings.append(f"{entry.job_id}: {entry.warning}")

        same_study = self._rendered_study_key == study_key
        self._rendered_study_key = study_key
        polling_note = f"{active_count} active" if active_count else "polling paused (all terminal)"
        notice_text = f"Active & Recent Jobs ({len(rows)}) — {polling_note}"
        if warnings:
            notice_text = f"{notice_text} | Warning: {_escape_markup('; '.join(warnings))}"
        self._update_ui(
            summary_text="\n".join(lines),
            notice_text=notice_text,
            rows=rows,
            preserve_cursor=same_study,
        )

    async def refresh_jobs(self) -> None:
        """Poll active jobs and update the job table and summary for the active study."""
        study_key = self.current_study_key
        if not study_key:
            self._pause_timer()
            self._rendered_study_key = None
            self._update_ui(
                summary_text="Job Monitor: Select a study to view background jobs.",
                notice_text=(
                    "Select a study to monitor jobs. Upstream iMednet APIs require batch IDs; "
                    "enter batch ID(s) above once a study is active."
                ),
                rows=(),
                preserve_cursor=False,
            )
            return

        try:
            entries = self._entries_for_study(study_key)
            if not entries:
                if self.current_study_key != study_key and self.is_mounted:
                    return
                self._pause_timer()
                self._rendered_study_key = study_key
                empty_msg = (
                    f"Job Monitor: Background job enumeration is not supported by the iMednet Jobs API "
                    f"for study '{_escape_markup(study_key)}' (upstream APIs require batch IDs). "
                    "Enter one or more batch IDs above and press Enter to track jobs."
                )
                self._update_ui(
                    summary_text=empty_msg,
                    notice_text=empty_msg,
                    rows=(),
                    preserve_cursor=False,
                )
                return

            active_entries = [entry for entry in entries.values() if not entry.is_terminal]
            if active_entries:
                await asyncio.gather(
                    *(self._poll_tracked_entry(study_key, entry) for entry in active_entries)
                )

            if self.current_study_key != study_key and self.is_mounted:
                return

            current_entries = self._entries_for_study(study_key)
            if any(not entry.is_terminal for entry in current_entries.values()):
                self._resume_timer()
            else:
                self._pause_timer()

            self._render_study_entries(study_key, current_entries)
        except Exception as e:
            if self.current_study_key == study_key or not self.is_mounted:
                escaped_err = _escape_markup(str(e))
                err_msg = f"[red]Error fetching jobs: {escaped_err}[/red]"
                self._update_ui(
                    summary_text=err_msg,
                    notice_text=f"Error fetching jobs: {escaped_err}",
                    rows=(),
                    preserve_cursor=False,
                )


class LogViewer(Log):
    """Log viewer widget for auditing actions and SDK events."""

    DEFAULT_CSS = """
    LogViewer {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the LogViewer widget.

        Args:
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.border_title = "Logs"


class DashboardScreen(Screen[None]):
    """Main interactive study exploration dashboard."""

    CSS = """
    DashboardScreen {
        layout: vertical;
    }

    #dashboard-grid {
        layout: grid;
        grid-size: 3 2;
        grid-columns: 1fr 1fr 2fr;
        grid-rows: 1fr 1fr;
        height: 1fr;
    }

    #studies-box {
        border: solid green;
    }

    #sites-box {
        border: solid blue;
    }

    #subjects-box {
        border: solid magenta;
    }

    #bottom-pane {
        column-span: 3;
        height: 1fr;
        border-top: solid white;
    }

    #bottom-pane > ContentSwitcher {
        height: 1fr;
    }

    #bottom-pane TabPane {
        height: 1fr;
        padding: 0;
    }

    #truncation-notice {
        display: none;
        height: auto;
        padding: 0 1;
        background: $warning 20%;
        color: $text;
    }

    #truncation-notice.visible {
        display: block;
    }

    /* High contrast mode styles */
    DashboardScreen.high-contrast {
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast #truncation-notice {
        background: black;
        color: white;
        border-bottom: heavy white;
    }

    DashboardScreen.high-contrast #studies-box {
        border: heavy white;
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast #sites-box {
        border: heavy white;
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast #subjects-box {
        border: heavy white;
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast #bottom-pane {
        border-top: heavy white;
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast ListItem {
        color: white;
        background: black;
    }

    DashboardScreen.high-contrast ListItem:focus {
        background: white;
        color: black;
    }

    DashboardScreen.high-contrast DataTable {
        color: white;
        background: black;
    }

    DashboardScreen.high-contrast DataTable > .datatable--header {
        background: black;
        color: white;
        text-style: bold;
    }

    DashboardScreen.high-contrast DataTable > .datatable--cursor {
        background: white;
        color: black;
    }

    DashboardScreen.high-contrast JobMonitor {
        border: heavy white;
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast JobMonitor #batch_id_input {
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast JobMonitor #batch_id_input:focus {
        background: black;
        color: white;
        text-style: bold underline;
    }

    DashboardScreen.high-contrast Input > .input--placeholder {
        color: white;
        background: black;
    }

    DashboardScreen.high-contrast Input > .input--cursor {
        background: white;
        color: black;
    }

    DashboardScreen.high-contrast JobMonitor #job_monitor_notice {
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast Tabs {
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast Tab {
        background: black;
        color: white;
    }

    DashboardScreen.high-contrast Tab:focus {
        background: white;
        color: black;
    }
    """

    def __init__(
        self,
        sdk: ImednetFacade | AsyncImednetFacade,
        *,
        initial_study_key: str | None = None,
        initial_batch_ids: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the DashboardScreen.

        Args:
            sdk: Active iMednet SDK facade instance.
            initial_study_key: Optional initial study key to select upon mount.
            initial_batch_ids: Optional initial batch IDs to register upon mount.
            **kwargs: Additional screen arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.log_viewer = LogViewer()
        self.initial_study_key = (
            initial_study_key.strip() if initial_study_key and initial_study_key.strip() else None
        )
        self.initial_batch_ids = _normalize_batch_ids(initial_batch_ids)
        self._pending_batch_ids = [] if self.initial_study_key else list(self.initial_batch_ids)
        self.selected_study_key: str | None = None
        self.selected_site_id: str | None = None
        self._log_handler: TextualLogHandler | None = None
        self._previous_log_level: int | None = None

    def compose(self) -> ComposeResult:
        """Compose children widgets for the dashboard screen.

        Yields:
            Header, status banner, containers, grid panels, tab panes, and footer.
        """
        yield Header()
        yield Static("", id="truncation-notice")
        studies_box = Container(
            StudyList(self.sdk, preferred_study_key=self.initial_study_key, id="study_list"),
            id="studies-box",
        )
        studies_box.border_title = "Studies"
        sites_box = Container(SiteList(self.sdk, id="site_list"), id="sites-box")
        sites_box.border_title = "Sites"
        subjects_box = Container(SubjectTable(self.sdk, id="subject_table"), id="subjects-box")
        subjects_box.border_title = "Subjects"

        with Container(id="dashboard-grid"):
            yield studies_box
            yield sites_box
            yield subjects_box

            with TabbedContent(id="bottom-pane"):
                with TabPane("Jobs", id="jobs-tab"):
                    yield JobMonitor(self.sdk, id="job_monitor")
                with TabPane("Logs", id="logs-tab"):
                    yield self.log_viewer
        yield Footer()

    def _activate_study(self, study_key: str, study_name: str) -> None:
        """Activate ``study_key`` across sites, subjects, and job monitor panes."""
        self.selected_study_key = study_key
        self.selected_site_id = None
        self.log_viewer.write_line(f"Selected Study: {study_name} ({study_key})")
        site_list = self.query_one(SiteList)
        self.run_worker(site_list.load_sites(study_key), exclusive=True, group="load-sites")

        subject_table = self.query_one(SubjectTable)
        self.run_worker(
            subject_table.load_subjects(study_key),
            exclusive=True,
            group="load-subjects",
        )

        job_monitor = self.query_one(JobMonitor)
        if self._pending_batch_ids:
            job_monitor.track_jobs(
                self._pending_batch_ids,
                study_key=study_key,
                refresh=False,
            )
            self._pending_batch_ids.clear()
        job_monitor.set_study(study_key)

    def on_mount(self) -> None:
        """Sync high-contrast mode, attach SDK log handler, and apply initial CLI options."""
        if getattr(self.app, "high_contrast_enabled", False) or "high-contrast" in self.app.classes:
            self.add_class("high-contrast")

        handler = TextualLogHandler(self.log_viewer)
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger = logging.getLogger("imednet")
        self._previous_log_level = logger.level
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self._log_handler = handler

        if self.initial_batch_ids:
            with contextlib.suppress(Exception):
                bottom_pane = self.query_one("#bottom-pane", TabbedContent)
                bottom_pane.active = "jobs-tab"

        if self.initial_study_key:
            job_monitor = self.query_one(JobMonitor)
            if self.initial_batch_ids:
                job_monitor.track_jobs(
                    self.initial_batch_ids,
                    study_key=self.initial_study_key,
                    refresh=False,
                )
            self._activate_study(self.initial_study_key, self.initial_study_key)

    def on_unmount(self) -> None:
        """Detach the SDK log handler and restore the previous logger level on unmount."""
        logger = logging.getLogger("imednet")
        if self._log_handler is not None:
            logger.removeHandler(self._log_handler)
            self._log_handler = None
        if self._previous_log_level is not None:
            logger.setLevel(self._previous_log_level)
            self._previous_log_level = None

    def refresh_truncation_notice(self) -> None:
        """Update the dashboard truncation status banner based on widget state."""
        notices: list[str] = []
        with contextlib.suppress(Exception):
            study_list = self.query_one(StudyList)
            if study_list.truncated and study_list.status_notice:
                notices.append(study_list.status_notice)
        with contextlib.suppress(Exception):
            site_list = self.query_one(SiteList)
            if site_list.truncated and site_list.status_notice:
                notices.append(site_list.status_notice)
        with contextlib.suppress(Exception):
            subject_table = self.query_one(SubjectTable)
            if subject_table.truncated and subject_table.status_notice:
                notices.append(subject_table.status_notice)

        with contextlib.suppress(Exception):
            banner = self.query_one("#truncation-notice", Static)
            if notices:
                banner.update(" | ".join(notices))
                banner.add_class("visible")
            else:
                banner.update("")
                banner.remove_class("visible")

    async def load_all_data(self) -> None:
        """Fetch remaining pages for truncated or active Study, Site, and Subject views."""
        self.log_viewer.write_line("Loading all items for active views...")
        study_list = self.query_one(StudyList)
        site_list = self.query_one(SiteList)
        subject_table = self.query_one(SubjectTable)

        tasks = []
        if study_list.truncated or not study_list.loaded:
            tasks.append(study_list.load_studies(load_all=True))
        if site_list.current_study_key:
            tasks.append(site_list.load_sites(site_list.current_study_key, load_all=True))
        if subject_table.current_study_key:
            tasks.append(
                subject_table.load_subjects(
                    subject_table.current_study_key,
                    subject_table.current_site_id,
                    load_all=True,
                )
            )
        if tasks:
            await asyncio.gather(*tasks)
        self.refresh_truncation_notice()

    def on_study_list_study_selected(self, message: StudyList.StudySelected) -> None:
        """Handle study selection by populating sites, subjects, and jobs.

        Args:
            message: Study selection event containing study details.
        """
        self._activate_study(message.study_key, message.study_name)

    def on_site_list_site_selected(self, message: SiteList.SiteSelected) -> None:
        """Handle site selection by filtering displayed subjects.

        Args:
            message: Site selection event containing site details.
        """
        self.selected_site_id = message.site_id
        self.log_viewer.write_line(f"Selected Site: {message.site_name} (ID: {message.site_id})")
        site_list = self.query_one(SiteList)
        study_key = self.selected_study_key or site_list.current_study_key
        if study_key:
            subject_table = self.query_one(SubjectTable)
            self.run_worker(
                subject_table.load_subjects(study_key, message.site_id),
                exclusive=True,
                group="load-subjects",
            )


class ImednetTuiApp(App[None]):
    """The iMednet TUI Application."""

    CSS = """
    Screen {
        background: $surface;
    }

    .high-contrast {
        background: black;
        color: white;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("q", "quit", "Quit"),
        ("d", "toggle_dark", "Dark Mode"),
        ("h", "toggle_high_contrast", "High Contrast"),
        ("r", "refresh_data", "Refresh"),
        ("a", "load_all", "Load All"),
    ]

    _SINGLE_KEY_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"quit", "toggle_dark", "toggle_high_contrast", "refresh_data", "load_all"}
    )

    def __init__(
        self,
        sdk: ImednetFacade | AsyncImednetFacade,
        *,
        high_contrast: bool | None = None,
        study_key: str | None = None,
        batch_ids: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the iMednet TUI application.

        Args:
            sdk: Active iMednet SDK facade instance.
            high_contrast: Optional explicit high-contrast toggle; defaults to reading
                ``IMEDNET_HIGH_CONTRAST=1`` from ``os.environ`` without modifying it.
            study_key: Optional initial study key to select on launch.
            batch_ids: Optional initial batch IDs to register in the Jobs tab.
            **kwargs: Additional application arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.initial_study_key = study_key.strip() if study_key and study_key.strip() else None
        self.initial_batch_ids = _normalize_batch_ids(batch_ids)
        self.high_contrast_enabled = (
            bool(high_contrast)
            if high_contrast is not None
            else os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Suppress single-key global actions while typing inside an Input widget."""
        del parameters
        return not (action in self._SINGLE_KEY_ACTIONS and isinstance(self.focused, Input))

    def _sync_high_contrast_classes(self) -> None:
        """Synchronize the ``high-contrast`` class across the app and dashboard screens."""
        self.set_class(self.high_contrast_enabled, "high-contrast")
        for screen in self.screen_stack:
            if isinstance(screen, DashboardScreen):
                screen.set_class(self.high_contrast_enabled, "high-contrast")

    def on_mount(self) -> None:
        """Configure application title, contrast mode, and dashboard screen on mount."""
        self.title = "iMednet Mission Control"
        config = getattr(self.sdk, "config", None)
        base_url = getattr(config, "base_url", None) or "Unknown"
        self.sub_title = f"Env: {base_url}"

        dashboard = DashboardScreen(
            self.sdk,
            initial_study_key=self.initial_study_key,
            initial_batch_ids=self.initial_batch_ids,
        )
        if self.high_contrast_enabled:
            self.add_class("high-contrast")
            dashboard.add_class("high-contrast")

        self.push_screen(dashboard)

    def _get_dashboard(self) -> DashboardScreen | None:
        """Return the active DashboardScreen instance from the screen stack."""
        if isinstance(self.screen, DashboardScreen):
            return self.screen
        for screen in self.screen_stack:
            if isinstance(screen, DashboardScreen):
                return screen
        return None

    def action_toggle_high_contrast(self) -> None:
        """Toggle accessibility high contrast mode without mutating ``os.environ``."""
        self.high_contrast_enabled = not self.high_contrast_enabled
        self._sync_high_contrast_classes()

    def action_refresh_data(self) -> None:
        """Refresh studies and any active study/site panes on the dashboard screen."""
        screen = self._get_dashboard()
        if screen is None:
            return

        study_list = screen.query_one(StudyList)
        screen.run_worker(study_list.load_studies(), exclusive=True, group="load-studies")

        site_list = screen.query_one(SiteList)
        job_monitor = screen.query_one(JobMonitor)
        study_key = (
            screen.selected_study_key
            or site_list.current_study_key
            or job_monitor.current_study_key
        )
        if study_key:
            screen.run_worker(
                site_list.load_sites(study_key),
                exclusive=True,
                group="load-sites",
            )
            subject_table = screen.query_one(SubjectTable)
            screen.run_worker(
                subject_table.load_subjects(study_key, screen.selected_site_id),
                exclusive=True,
                group="load-subjects",
            )
            screen.run_worker(
                job_monitor.refresh_jobs(),
                exclusive=True,
                group="refresh-jobs",
            )

    def action_load_all(self) -> None:
        """Load all remaining items across bounded Study, Site, and Subject views."""
        dashboard = self._get_dashboard()
        if dashboard is not None:
            dashboard.run_worker(dashboard.load_all_data(), exclusive=True, group="load-all")


def run_tui(
    sdk: ImednetFacade | AsyncImednetFacade,
    force: bool = False,
    *,
    high_contrast: bool | None = None,
    study_key: str | None = None,
    batch_ids: Sequence[str] | None = None,
) -> None:
    """Run the TUI application.

    Args:
        sdk: Active iMednet SDK facade instance.
        force: If True, bypass stream interactivity checks and launch the TUI unconditionally.
        high_contrast: Optional explicit high-contrast mode flag.
        study_key: Optional initial study key to select on launch.
        batch_ids: Optional initial batch IDs to track in the Jobs tab.

    Raises:
        RuntimeError: If stdin or stdout is not connected to an interactive TTY and force is False.
    """
    if not force and not is_interactive_terminal():
        raise RuntimeError("Interactive terminal required to run the TUI interface.")
    app = ImednetTuiApp(
        sdk,
        high_contrast=high_contrast,
        study_key=study_key,
        batch_ids=batch_ids,
    )
    app.run()
