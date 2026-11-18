"""Terminal user interface application for iMednet."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

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
    Label,
    ListItem,
    ListView,
    Log,
    Static,
    TabbedContent,
    TabPane,
)

from .terminal import is_interactive_terminal

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK

__all__ = [
    "DEFAULT_FETCH_LIMIT",
    "DashboardScreen",
    "ImednetTuiApp",
    "JobMonitor",
    "SiteList",
    "StudyList",
    "SubjectTable",
    "TextualLogHandler",
    "is_interactive_terminal",
    "run_tui",
]

DEFAULT_FETCH_LIMIT = 100


def _consume_bounded(source: Any, limit: int | None) -> tuple[list[Any], bool]:
    """Consume up to ``limit`` items from ``source`` and report whether items were truncated."""
    if limit is None:
        return list(source), False
    if isinstance(source, Sequence):
        return list(source[:limit]), len(source) > limit
    if hasattr(source, "has_more"):
        items = list(itertools.islice(source, limit))
        return items, bool(getattr(source, "has_more", False))
    iterator = iter(source)
    batch = list(itertools.islice(iterator, limit + 1))
    return batch[:limit], len(batch) > limit


def _call_sync_list(
    list_fn: Any, *args: Any, limit: int | None, **kwargs: Any
) -> tuple[list[Any], bool]:
    """Invoke a synchronous SDK ``list()`` method with bounded ``size`` when supported."""
    if limit is not None:
        try:
            stream = list_fn(*args, size=limit, **kwargs)
        except TypeError:
            stream = list_fn(*args, **kwargs)
        return _consume_bounded(stream, limit)
    return list(list_fn(*args, **kwargs)), False


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

    class Selected(Message):
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

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the StudyList widget.

        Args:
            sdk: Active iMednet SDK client instance.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.loaded = False
        self.truncated = False
        self.status_notice = ""

    async def on_mount(self) -> None:
        """Load studies automatically upon mounting the widget."""
        await self.load_studies()

    async def load_studies(self, *, load_all: bool = False) -> None:
        """Fetch and populate study items from the SDK asynchronously.

        Args:
            load_all: When True, fetch all pages instead of the initial bounded batch.
        """
        if self.is_mounted:
            await self.clear()
        self.loading = True
        limit = None if load_all else DEFAULT_FETCH_LIMIT
        try:
            studies_ep: Any = self.sdk.studies
            if hasattr(studies_ep, "async_list"):
                raw_studies = await studies_ep.async_list()
                studies, truncated = _consume_bounded(raw_studies, limit)
            else:
                studies, truncated = await asyncio.to_thread(
                    _call_sync_list, self.sdk.studies.list, limit=limit
                )

            items = []
            for study in studies:
                name = getattr(study, "study_name", None) or getattr(study, "name", "Unknown Study")
                key = getattr(study, "study_key", "no-key")
                label = f"{name} ({key})"
                items.append(ListItem(Label(label), id=f"study-{key}"))
            self.extend(items)
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
            study_key = event.item.id.replace("study-", "")
            try:
                label_widget = event.item.query_one(Label)
                study_name = str(getattr(label_widget, "renderable", study_key))
            except Exception:
                study_name = study_key
            self.post_message(self.Selected(study_key, study_name))


class SiteList(ListView):
    """List of sites for a selected study."""

    class Selected(Message):
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

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the SiteList widget.

        Args:
            sdk: Active iMednet SDK client instance.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.current_study_key: str | None = None
        self.truncated = False
        self.status_notice = ""

    async def load_sites(self, study_key: str, *, load_all: bool = False) -> None:
        """Fetch and populate site items for a given study key.

        Args:
            study_key: Study key to load sites for.
            load_all: When True, fetch all pages instead of the initial bounded batch.
        """
        self.current_study_key = study_key
        if self.is_mounted:
            await self.clear()
        self.loading = True
        limit = None if load_all else DEFAULT_FETCH_LIMIT
        try:
            sites_ep: Any = self.sdk.sites
            if hasattr(sites_ep, "async_list"):
                raw_sites = await sites_ep.async_list(study_key)
                sites, truncated = _consume_bounded(raw_sites, limit)
            else:
                sites, truncated = await asyncio.to_thread(
                    _call_sync_list, self.sdk.sites.list, study_key, limit=limit
                )

            items = []
            for site in sites:
                site_id = getattr(site, "site_id", "no-id")
                name = getattr(site, "site_name", "Unknown Site")
                label = f"{name} ({site_id})"
                items.append(ListItem(Label(label), id=f"site-{site_id}"))
            self.extend(items)
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
            site_id = event.item.id.replace("site-", "")
            try:
                label_widget = event.item.query_one(Label)
                site_name = str(getattr(label_widget, "renderable", site_id))
            except Exception:
                site_name = site_id
            self.post_message(self.Selected(site_id, site_name))


class SubjectTable(DataTable[str]):
    """Interactive table displaying subjects."""

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the SubjectTable widget.

        Args:
            sdk: Active iMednet SDK client instance.
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
            site_id: Optional site identifier to filter results.
            load_all: When True, fetch all pages instead of the initial bounded batch.
        """
        self.current_study_key = study_key
        self.current_site_id = site_id
        if self.is_mounted:
            self.clear()
        self.loading = True
        limit = None if load_all else DEFAULT_FETCH_LIMIT
        try:
            subjects_ep: Any = self.sdk.subjects
            if hasattr(subjects_ep, "async_list"):
                raw_subjects = await subjects_ep.async_list(study_key)
                if site_id is not None:
                    raw_subjects = [
                        sub
                        for sub in raw_subjects
                        if str(getattr(sub, "site_id", "") or "") == str(site_id)
                    ]
                subjects, truncated = _consume_bounded(raw_subjects, limit)
            elif site_id is not None and load_all and hasattr(subjects_ep, "list_by_site"):
                subjects = await asyncio.to_thread(
                    lambda: list(subjects_ep.list_by_site(study_key, site_id))
                )
                truncated = False
            elif site_id is not None:
                subjects, truncated = await asyncio.to_thread(
                    _call_sync_list,
                    self.sdk.subjects.list,
                    study_key,
                    limit=limit,
                    site_id=site_id,
                )
            else:
                subjects, truncated = await asyncio.to_thread(
                    _call_sync_list, self.sdk.subjects.list, study_key, limit=limit
                )

            rows = []
            for sub in subjects:
                s_site_id = str(getattr(sub, "site_id", "") or "")
                if site_id is None or s_site_id == str(site_id):
                    sub_id = getattr(sub, "subject_id", "-")
                    status = (
                        getattr(sub, "subject_status", None) or getattr(sub, "status", None) or "-"
                    )
                    created = str(getattr(sub, "date_created", "-"))
                    rows.append((str(sub_id), s_site_id, str(status), created))
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


class JobMonitor(Static):
    """Widget polling and displaying background study jobs."""

    DEFAULT_CSS = """
    JobMonitor {
        height: 100%;
        background: $surface;
        border: solid $primary;
        overflow-y: auto;
    }
    """

    MAX_CONCURRENT_POLLS: ClassVar[int] = 5

    def __init__(
        self,
        sdk: ImednetSDK,
        *,
        max_concurrency: int = MAX_CONCURRENT_POLLS,
        **kwargs: Any,
    ) -> None:
        """Initialize the JobMonitor widget.

        Args:
            sdk: Active iMednet SDK client instance.
            max_concurrency: Maximum number of concurrent status polling requests.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.max_concurrency = max_concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self.timer: Timer | None = None
        self.current_study_key: str | None = None
        self.tracked_job_ids: list[str] = []

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Asyncio semaphore bounding simultaneous job status queries."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    def on_mount(self) -> None:
        """Set up status message and interval polling timer on mount."""
        self.update("Job Monitor: Select a study to view background jobs.")
        self.timer = self.set_interval(10.0, self.refresh_jobs, pause=True)

    def track_job(self, job_id: str) -> None:
        """Register a known job ID to monitor.

        Args:
            job_id: Known background job or batch identifier.
        """
        if job_id not in self.tracked_job_ids:
            self.tracked_job_ids.append(job_id)
        if self.current_study_key and self.is_mounted:
            self.run_worker(self.refresh_jobs())

    def set_study(self, study_key: str) -> None:
        """Set the active study key and trigger background job refresh.

        Args:
            study_key: Active study key to track jobs for.
        """
        self.current_study_key = study_key
        self.update(f"Job Monitor: Loading jobs for study '{study_key}'...")
        if self.timer:
            self.timer.resume()
        if self.is_mounted:
            self.run_worker(self.refresh_jobs())

    async def _poll_tracked_job(
        self,
        jobs_ep: Any,
        study_key: str,
        jid: str,
    ) -> Any:
        """Poll status for a single tracked job under the concurrency semaphore.

        Args:
            jobs_ep: Jobs endpoint instance or mock.
            study_key: Active study key identifier.
            jid: Tracked job or batch identifier.

        Returns:
            Retrieved job model instance or _TrackedJobPlaceholder on failure/unknown.
        """
        async with self.semaphore:
            try:
                if hasattr(jobs_ep, "get"):
                    if asyncio.iscoroutinefunction(jobs_ep.get):
                        job = await jobs_ep.get(study_key, jid)
                    else:
                        res = await asyncio.to_thread(jobs_ep.get, study_key, jid)
                        if asyncio.iscoroutine(res):
                            job = await res
                        else:
                            job = res
                elif hasattr(jobs_ep, "async_get"):
                    res = jobs_ep.async_get(study_key, jid)
                    if asyncio.iscoroutine(res):
                        job = await res
                    else:
                        job = res
                else:
                    job = None

                if job is not None:
                    return job
                return _TrackedJobPlaceholder(jid, "UNKNOWN")
            except Exception as err:
                logging.getLogger("imednet_tui").warning("Failed to poll job %s: %s", jid, err)
                return _TrackedJobPlaceholder(jid, "FAILED")

    async def refresh_jobs(self) -> None:
        """Poll and update job list for the active study."""
        if not self.current_study_key:
            self.update("Job Monitor: Select a study to view background jobs.")
            if self.timer:
                self.timer.pause()
            return

        try:
            jobs_ep: Any = self.sdk.jobs
            jobs = []

            if hasattr(jobs_ep, "async_list"):
                jobs = await jobs_ep.async_list(self.current_study_key)
            elif hasattr(jobs_ep, "list"):
                jobs = await asyncio.to_thread(lambda: list(jobs_ep.list(self.current_study_key)))
            elif self.tracked_job_ids:
                tasks = [
                    self._poll_tracked_job(jobs_ep, self.current_study_key, jid)
                    for jid in list(self.tracked_job_ids)
                ]
                jobs = list(await asyncio.gather(*tasks))
            else:
                self.update(
                    f"Job Monitor: Background job enumeration is not supported by the iMednet Jobs API for study '{self.current_study_key}'.\n"
                    "Use JobsEndpoint get/poll with a specific batch/job ID to track background jobs."
                )
                return

            lines = [f"[bold]Active & Recent Jobs ({len(jobs)})[/bold]"]
            for job in jobs[:15]:
                job_id = getattr(job, "job_id", getattr(job, "batch_id", ""))
                job_type = getattr(job, "job_type", "Job")
                status = getattr(
                    job, "state", getattr(job, "status", getattr(job, "job_status", "Unknown"))
                )
                created = getattr(job, "date_created", "") or ""
                status_str = str(status.value if hasattr(status, "value") else status)
                color = (
                    "green"
                    if status_str.upper() in ("COMPLETED", "COMPLETE", "SUCCESS")
                    else "yellow"
                    if status_str.upper() in ("PROCESSING", "PENDING", "RUNNING", "QUEUED", "OPEN")
                    else "red"
                )
                label = f"{job_type} ({job_id})" if job_id else str(job_type)
                created_suffix = f" ({created})" if created else ""
                lines.append(f"[{color}]{status_str}[/{color}] - {label}{created_suffix}")

            self.update("\n".join(lines))
        except Exception as e:
            self.update(f"[red]Error fetching jobs: {e}[/red]")


class LogViewer(Log):
    """Log viewer widget for auditing actions and SDK events."""

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
        grid-rows: 3fr 1fr;
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
        border-top: solid white;
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
    .high-contrast #truncation-notice {
        background: black;
        color: white;
        border-bottom: heavy white;
    }
    .high-contrast #studies-box {
        border: heavy white;
        background: black;
        color: white;
    }

    .high-contrast #sites-box {
        border: heavy white;
        background: black;
        color: white;
    }

    .high-contrast #subjects-box {
        border: heavy white;
        background: black;
        color: white;
    }

    .high-contrast #bottom-pane {
        border-top: heavy white;
        background: black;
        color: white;
    }

    .high-contrast ListItem {
        color: white;
        background: black;
    }

    .high-contrast ListItem:focus {
        background: white;
        color: black;
    }

    .high-contrast DataTable {
        color: white;
        background: black;
    }

    .high-contrast DataTable > .datatable--cursor {
        background: white;
        color: black;
    }

    .high-contrast JobMonitor {
        border: heavy white;
        background: black;
        color: white;
    }
    """

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the DashboardScreen.

        Args:
            sdk: Active iMednet SDK client instance.
            **kwargs: Additional screen arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.log_viewer = LogViewer()

    def compose(self) -> ComposeResult:
        """Compose children widgets for the dashboard screen.

        Yields:
            Header, status banner, containers, grid panels, tab panes, and footer.
        """
        yield Header()
        yield Static("", id="truncation-notice")
        studies_box = Container(StudyList(self.sdk, id="study_list"), id="studies-box")
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

    def on_study_list_selected(self, message: StudyList.Selected) -> None:
        """Handle study selection by populating sites, subjects, and jobs.

        Args:
            message: Study selection event containing study details.
        """
        self.log_viewer.write_line(f"Selected Study: {message.study_name} ({message.study_key})")
        site_list = self.query_one(SiteList)
        self.run_worker(site_list.load_sites(message.study_key))

        subject_table = self.query_one(SubjectTable)
        self.run_worker(subject_table.load_subjects(message.study_key))

        job_monitor = self.query_one(JobMonitor)
        job_monitor.set_study(message.study_key)

    def on_site_list_selected(self, message: SiteList.Selected) -> None:
        """Handle site selection by filtering displayed subjects.

        Args:
            message: Site selection event containing site details.
        """
        self.log_viewer.write_line(f"Selected Site: {message.site_name} (ID: {message.site_id})")
        site_list = self.query_one(SiteList)
        study_key = site_list.current_study_key
        if study_key:
            subject_table = self.query_one(SubjectTable)
            self.run_worker(subject_table.load_subjects(study_key, message.site_id))


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

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the iMednet TUI application.

        Args:
            sdk: Active iMednet SDK client instance.
            **kwargs: Additional application arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.high_contrast_enabled = os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"

    def on_mount(self) -> None:
        """Configure application title, contrast mode, screen, and logger on mount."""
        self.title = "iMednet Mission Control"
        base_url = getattr(getattr(self.sdk, "config", None), "base_url", None) or getattr(
            self.sdk, "_base_url", "Unknown"
        )
        self.sub_title = f"Env: {base_url}"

        if self.high_contrast_enabled:
            self.add_class("high-contrast")

        self.push_screen(DashboardScreen(self.sdk))

        with contextlib.suppress(Exception):
            dashboard = self.query_one(DashboardScreen)
            log_widget = dashboard.log_viewer
            handler = TextualLogHandler(log_widget)
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            logger = logging.getLogger("imednet")
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)

    def _get_dashboard(self) -> DashboardScreen | None:
        """Return the active DashboardScreen instance from the screen stack."""
        if isinstance(self.screen, DashboardScreen):
            return self.screen
        for screen in self.screen_stack:
            if isinstance(screen, DashboardScreen):
                return screen
        return None

    def action_toggle_high_contrast(self) -> None:
        """Toggle accessibility high contrast mode."""
        self.high_contrast_enabled = not self.high_contrast_enabled
        if self.high_contrast_enabled:
            self.add_class("high-contrast")
            os.environ["IMEDNET_HIGH_CONTRAST"] = "1"
        else:
            self.remove_class("high-contrast")
            os.environ.pop("IMEDNET_HIGH_CONTRAST", None)

    def action_refresh_data(self) -> None:
        """Refresh current screen data."""
        dashboard = self._get_dashboard()
        if dashboard is not None:
            with contextlib.suppress(Exception):
                study_list = dashboard.query_one(StudyList)
                self.run_worker(study_list.load_studies())

    def action_load_all(self) -> None:
        """Load all remaining items across bounded Study, Site, and Subject views."""
        dashboard = self._get_dashboard()
        if dashboard is not None:
            self.run_worker(dashboard.load_all_data())


def run_tui(sdk: ImednetSDK, force: bool = False) -> None:
    """Run the TUI application.

    Args:
        sdk: Active iMednet SDK client instance.
        force: If True, bypass stream interactivity checks and launch the TUI unconditionally.

    Raises:
        RuntimeError: If stdin or stdout is not connected to an interactive TTY and force is False.
    """
    if not force and not is_interactive_terminal():
        raise RuntimeError("Interactive terminal required to run the TUI interface.")
    app = ImednetTuiApp(sdk)
    app.run()
