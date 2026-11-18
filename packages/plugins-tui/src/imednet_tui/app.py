"""Terminal user interface application for iMednet."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
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

from imednet.spi.jobs import (
    ACTIVE_JOB_STATES,
    JobLedger,
    JobLedgerEntry,
    normalize_job_state,
)
from imednet.spi.utils import mask_clinical_phi

from .terminal import is_interactive_terminal
from .theme import WORKBENCH_CSS, format_progress_bar, format_status_badge
from .triage_pane import TriagePane
from .views import ENTITY_VIEW_SPECS, DataDictionaryPane, EntityTablePane
from .workflows_pane import WorkflowsPane

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK

__all__ = [
    "DashboardScreen",
    "ImednetTuiApp",
    "JobMonitor",
    "LogViewer",
    "SiteList",
    "StudyList",
    "SubjectTable",
    "TextualLogHandler",
    "is_interactive_terminal",
    "run_tui",
]


class TextualLogHandler(logging.Handler):
    """Thread-safe logging handler routing records to a Textual Log widget."""

    def __init__(self, log_widget: Log, *, owner_thread_id: int | None = None) -> None:
        """Initialize handler with target Log widget.

        Args:
            log_widget: The Textual Log widget to write to.
            owner_thread_id: Thread identifier owning the Textual event loop.
        """
        super().__init__()
        self.log_widget = log_widget
        self._owner_thread_id = (
            owner_thread_id if owner_thread_id is not None else threading.get_ident()
        )

    def emit(self, record: logging.LogRecord) -> None:
        """Format and write log record to widget across UI and worker threads.

        Args:
            record: The logging LogRecord to emit.
        """
        try:
            msg = self.format(record)
            if threading.get_ident() == self._owner_thread_id:
                self.log_widget.write_line(msg)
            else:
                self.log_widget.app.call_from_thread(self.log_widget.write_line, msg)
        except Exception:
            self.handleError(record)


class StudyList(ListView):
    """List of available clinical studies."""

    class Selected(Message):
        """Event emitted when a study is selected."""

        def __init__(self, study_key: str, study_name: str) -> None:
            """Initialize the study selected event."""
            self.study_key = study_key
            self.study_name = study_name
            super().__init__()

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the StudyList widget."""
        super().__init__(**kwargs)
        self.sdk = sdk
        self.loaded = False

    async def on_mount(self) -> None:
        """Load studies automatically upon mounting the widget."""
        await self.load_studies()

    async def load_studies(self) -> None:
        """Fetch and populate study items from the SDK asynchronously."""
        if self.is_mounted:
            self.clear()
        self.loading = True
        try:
            studies_ep: Any = self.sdk.studies
            if hasattr(studies_ep, "async_list"):
                studies = await studies_ep.async_list()
            else:
                studies = await asyncio.to_thread(lambda: list(self.sdk.studies.list()))

            items = []
            for study in studies:
                name = getattr(study, "study_name", None) or getattr(study, "name", "Unknown Study")
                key = getattr(study, "study_key", "no-key")
                label = f"{name} ({key})"
                items.append(ListItem(Label(label), id=f"study-{key}"))
            self.extend(items)
            self.loaded = True
        except Exception as e:
            if hasattr(self.app, "notify"):
                self.app.notify(f"Error loading studies: {e}", severity="error")
        finally:
            self.loading = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle selection event in the list view."""
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
            """Initialize the site selected event."""
            self.site_id = site_id
            self.site_name = site_name
            super().__init__()

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the SiteList widget."""
        super().__init__(**kwargs)
        self.sdk = sdk
        self.current_study_key: str | None = None

    async def load_sites(self, study_key: str) -> None:
        """Fetch and populate site items for a given study key."""
        self.current_study_key = study_key
        if self.is_mounted:
            self.clear()
        self.loading = True
        try:
            sites_ep: Any = self.sdk.sites
            if hasattr(sites_ep, "async_list"):
                sites = await sites_ep.async_list(study_key)
            else:
                sites = await asyncio.to_thread(lambda: list(self.sdk.sites.list(study_key)))

            items = []
            for site in sites:
                site_id = getattr(site, "site_id", "no-id")
                name = getattr(site, "site_name", "Unknown Site")
                label = f"{name} ({site_id})"
                items.append(ListItem(Label(label), id=f"site-{site_id}"))
            self.extend(items)
        except Exception as e:
            if hasattr(self.app, "notify"):
                self.app.notify(f"Error loading sites: {e}", severity="error")
        finally:
            self.loading = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle selection event in the site list view."""
        if event.item and event.item.id:
            site_id = event.item.id.replace("site-", "")
            try:
                label_widget = event.item.query_one(Label)
                site_name = str(getattr(label_widget, "renderable", site_id))
            except Exception:
                site_name = site_id
            self.post_message(self.Selected(site_id, site_name))


class SubjectTable(DataTable[str]):
    """Interactive table displaying clinical subjects and emitting selection events."""

    class Selected(Message):
        """Event emitted when a clinical subject row is selected."""

        def __init__(self, subject_key: str, subject_id: str, site_id: str) -> None:
            """Initialize the subject selected event."""
            self.subject_key = subject_key
            self.subject_id = subject_id
            self.site_id = site_id
            super().__init__()

    def __init__(self, sdk: ImednetSDK, *, phi_masked: bool = False, **kwargs: Any) -> None:
        """Initialize the SubjectTable widget."""
        super().__init__(**kwargs)
        self.sdk = sdk
        self.phi_masked = phi_masked
        self.cursor_type = "row"
        self.current_study_key: str | None = None
        self.current_site_id: str | None = None
        self._loaded_subjects: list[tuple[str, str, str, str, str]] = []

    def on_mount(self) -> None:
        """Initialize table columns when widget is mounted."""
        if not self.columns:
            self.add_columns("Subject Key", "Subject ID", "Site ID", "Status", "Created")

    def set_phi_masked(self, enabled: bool) -> None:
        """Toggle PHI masking for subject keys and re-render existing rows."""
        self.phi_masked = enabled
        if self.is_mounted:
            self._render_subject_rows()

    def _render_subject_rows(self) -> None:
        """Populate table rows from cached subject tuples with optional PHI masking."""
        if not self.columns:
            self.add_columns("Subject Key", "Subject ID", "Site ID", "Status", "Created")
        self.clear()
        rendered_rows: list[tuple[str, str, str, str, str]] = []
        for subj_key, subj_id, site_id, status, created in self._loaded_subjects:
            if self.phi_masked:
                masked = mask_clinical_phi({"subject_key": subj_key, "status": status})
                display_key = str(masked.get("subject_key", subj_key))
            else:
                display_key = subj_key
            rendered_rows.append((display_key, subj_id, site_id, status, created))
        if rendered_rows:
            self.add_rows(rendered_rows)

    async def load_subjects(self, study_key: str, site_id: str | None = None) -> None:
        """Fetch and populate subject rows for a study with optional site filtering."""
        self.current_study_key = study_key
        self.current_site_id = site_id
        if self.is_mounted:
            self.clear()
        self.loading = True
        try:
            subjects_ep: Any = self.sdk.subjects
            if hasattr(subjects_ep, "async_list"):
                subjects = await subjects_ep.async_list(study_key)
            elif site_id is not None and hasattr(subjects_ep, "list_by_site"):
                subjects = await asyncio.to_thread(
                    lambda: list(subjects_ep.list_by_site(study_key, site_id))
                )
            else:
                subjects = await asyncio.to_thread(lambda: list(self.sdk.subjects.list(study_key)))

            rows: list[tuple[str, str, str, str, str]] = []
            for sub in subjects:
                s_site_id = str(getattr(sub, "site_id", "") or "")
                if site_id is None or s_site_id == str(site_id):
                    sub_id = str(getattr(sub, "subject_id", "-") or "-")
                    raw_key = getattr(sub, "subject_key", None)
                    sub_key = (
                        str(raw_key).strip()
                        if isinstance(raw_key, str) and raw_key.strip()
                        else sub_id
                    )
                    status = (
                        getattr(sub, "subject_status", None) or getattr(sub, "status", None) or "-"
                    )
                    created = str(getattr(sub, "date_created", "-"))
                    rows.append((sub_key, sub_id, s_site_id, str(status), created))
            self._loaded_subjects = rows
            self._render_subject_rows()
        except Exception as e:
            if hasattr(self.app, "notify"):
                self.app.notify(f"Error loading subjects: {e}", severity="error")
        finally:
            self.loading = False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Emit SubjectTable.Selected with the unmasked clinical subject_key."""
        idx = event.cursor_row
        if 0 <= idx < len(self._loaded_subjects):
            subj_key, subj_id, site_id, _, _ = self._loaded_subjects[idx]
            self.post_message(self.Selected(subj_key, subj_id, site_id))


class _TrackedJobPlaceholder:
    """Representation for tracked jobs whose status could not be retrieved."""

    def __init__(
        self,
        job_id: str,
        status: str = "FAILED",
        progress: int = 0,
        job_type: str = "Job",
        date_created: str = "",
    ) -> None:
        """Initialize placeholder with job ID, status, and fallback metadata."""
        self.job_id = job_id
        self.batch_id = job_id
        self.job_type = job_type
        self.state = status
        self.status = status
        self.job_status = status
        self.progress = progress
        self.date_created = date_created


def _clean_scalar_str(value: Any) -> str:
    """Return stripped text when value is a real string/int scalar (ignoring mocks/bools)."""
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def _is_job_state_active(state_text: str) -> bool:
    """Return True when a job state string represents an in-flight background job."""
    cleaned = state_text.strip().upper()
    if not cleaned:
        return False
    try:
        return normalize_job_state(cleaned) in ACTIVE_JOB_STATES
    except ValueError:
        return cleaned in {"PROCESSING", "PENDING", "RUNNING", "QUEUED", "OPEN"}


class JobMonitor(Static):
    """Widget polling, persisting, and displaying background study jobs with progress bars."""

    DEFAULT_CSS = """
    JobMonitor {
        height: 1fr;
        background: $surface;
        border: solid $primary;
        overflow-y: auto;
        padding: 0 1;
    }
    """

    MAX_CONCURRENT_POLLS: ClassVar[int] = 5

    def __init__(
        self,
        sdk: ImednetSDK,
        *,
        max_concurrency: int = MAX_CONCURRENT_POLLS,
        ledger: JobLedger | None = None,
        poll_interval: float = 10.0,
        **kwargs: Any,
    ) -> None:
        """Initialize the JobMonitor widget.

        Args:
            sdk: Active iMednet SDK client instance.
            max_concurrency: Maximum number of concurrent status polling requests.
            ledger: Optional SQLite JobLedger instance (defaults to shared ledger).
            poll_interval: Automatic refresh interval in seconds while active jobs exist.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.max_concurrency = max_concurrency
        self.poll_interval = poll_interval
        self.ledger = ledger if ledger is not None else JobLedger()
        self._semaphore: asyncio.Semaphore | None = None
        self.timer: Timer | None = None
        self.is_polling_paused: bool = True
        self.current_study_key: str | None = None
        self.tracked_job_ids: list[str] = []
        self._persisted_job_ids: set[str] = set()

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Asyncio semaphore bounding simultaneous job status queries."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    def on_mount(self) -> None:
        """Set up status message and interval polling timer on mount."""
        self.update("Job Monitor: Select a study to view background jobs.")
        self.timer = self.set_interval(self.poll_interval, self.refresh_jobs, pause=True)
        self.is_polling_paused = True

    def _pause_polling(self) -> None:
        """Pause background interval polling when idle."""
        self.is_polling_paused = True
        if self.timer is not None:
            self.timer.pause()

    def _resume_polling(self) -> None:
        """Resume background interval polling while active jobs remain."""
        self.is_polling_paused = False
        if self.timer is not None:
            self.timer.resume()

    def track_job(self, job_id: str, *, persist: bool = False) -> None:
        """Register a known job or batch ID to monitor.

        Args:
            job_id: Known background job or batch identifier.
            persist: When True and a study is selected, persist in SQLite JobLedger.
        """
        cleaned = job_id.strip()
        if not cleaned:
            return
        if cleaned not in self.tracked_job_ids:
            self.tracked_job_ids.append(cleaned)
        if persist and self.current_study_key:
            self._persisted_job_ids.add(cleaned)
            with contextlib.suppress(Exception):
                self.ledger.record_job(
                    self.current_study_key,
                    cleaned,
                    source="tui-manual",
                )
        if self.current_study_key and self.is_mounted:
            self._resume_polling()
            self.run_worker(self.refresh_jobs())

    def set_study(self, study_key: str) -> None:
        """Set the active study key, load persisted ledger jobs, and trigger refresh."""
        if self.current_study_key != study_key:
            self.tracked_job_ids.clear()
            self._persisted_job_ids.clear()
        self.current_study_key = study_key
        self._sync_ids_from_ledger(study_key)
        self.update(f"Job Monitor: Loading jobs for study '{study_key}'...")
        if self.tracked_job_ids:
            self._resume_polling()
        else:
            self._pause_polling()
        if self.is_mounted:
            self.run_worker(self.refresh_jobs())

    def _sync_ids_from_ledger(self, study_key: str) -> dict[str, JobLedgerEntry]:
        """Merge persisted study job IDs from SQLite JobLedger while keeping registration order."""
        ledger_map: dict[str, JobLedgerEntry] = {}
        with contextlib.suppress(Exception):
            for entry in self.ledger.list_jobs(study_key):
                ledger_map[entry.batch_id] = entry
                self._persisted_job_ids.add(entry.batch_id)
                if entry.batch_id not in self.tracked_job_ids:
                    self.tracked_job_ids.append(entry.batch_id)
        return ledger_map

    async def _poll_tracked_job(
        self,
        jobs_ep: Any,
        study_key: str,
        jid: str,
        ledger_entry: JobLedgerEntry | None = None,
    ) -> Any:
        """Poll status for a single tracked job under the concurrency semaphore."""
        async with self.semaphore:
            try:
                if hasattr(jobs_ep, "get"):
                    if asyncio.iscoroutinefunction(jobs_ep.get):
                        job = await jobs_ep.get(study_key, jid)
                    else:
                        res = await asyncio.to_thread(jobs_ep.get, study_key, jid)
                        job = await res if asyncio.iscoroutine(res) else res
                elif hasattr(jobs_ep, "async_get"):
                    res = jobs_ep.async_get(study_key, jid)
                    job = await res if asyncio.iscoroutine(res) else res
                else:
                    job = None

                if job is not None:
                    if jid in self._persisted_job_ids or ledger_entry is not None:
                        with contextlib.suppress(Exception):
                            self.ledger.record_from_model(
                                study_key,
                                job,
                                fallback_batch_id=jid,
                                job_type=ledger_entry.job_type if ledger_entry else None,
                            )
                    return job
                return _TrackedJobPlaceholder(jid, "UNKNOWN")
            except Exception as err:
                logging.getLogger("imednet_tui").warning("Failed to poll job %s: %s", jid, err)
                return _TrackedJobPlaceholder(jid, "FAILED")

    async def refresh_jobs(self) -> None:
        """Poll and update job list for the active study, pausing timer when idle."""
        if not self.current_study_key:
            self.update("Job Monitor: Select a study to view background jobs.")
            self._pause_polling()
            return

        study_key = self.current_study_key
        ledger_map = self._sync_ids_from_ledger(study_key)

        try:
            jobs_ep: Any = self.sdk.jobs
            jobs: list[Any] = []

            if hasattr(jobs_ep, "async_list"):
                jobs = list(await jobs_ep.async_list(study_key))
            elif hasattr(jobs_ep, "list"):
                jobs = await asyncio.to_thread(lambda: list(jobs_ep.list(study_key)))
            elif self.tracked_job_ids:
                tasks = [
                    self._poll_tracked_job(jobs_ep, study_key, jid, ledger_map.get(jid))
                    for jid in list(self.tracked_job_ids)
                ]
                jobs = list(await asyncio.gather(*tasks))
            else:
                self._pause_polling()
                self.update(
                    f"Job Monitor: Background job enumeration is not supported by the iMednet "
                    f"Jobs API for study '{study_key}'.\n"
                    "Submit records, run a workflow, or enter a batch_id above to track jobs."
                )
                return

            high_contrast = bool(
                getattr(self.app, "high_contrast_enabled", False) if self.is_mounted else False
            )
            dark_mode = bool(getattr(self.app, "dark", True) if self.is_mounted else True)

            active_count = 0
            lines = [f"[bold]Active & Recent Jobs ({len(jobs)})[/bold]"]
            for idx, job in enumerate(jobs[:15]):
                fallback_id = self.tracked_job_ids[idx] if idx < len(self.tracked_job_ids) else ""
                batch_id = (
                    _clean_scalar_str(getattr(job, "batch_id", None))
                    or _clean_scalar_str(getattr(job, "job_id", None))
                    or fallback_id
                )
                ledger_meta = ledger_map.get(batch_id) if batch_id else None
                job_type = (
                    _clean_scalar_str(getattr(job, "job_type", None))
                    or (ledger_meta.job_type if ledger_meta else None)
                    or "Job"
                )
                status = getattr(
                    job, "state", getattr(job, "status", getattr(job, "job_status", "Unknown"))
                )
                status_str = str(status.value if hasattr(status, "value") else status)
                if _is_job_state_active(status_str):
                    active_count += 1

                raw_progress = getattr(job, "progress", None)
                progress_int: int | None = (
                    int(raw_progress)
                    if isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool)
                    else None
                )
                if progress_int is None and ledger_meta is not None:
                    progress_int = ledger_meta.progress
                if progress_int is None and status_str.upper() in {
                    "COMPLETED",
                    "COMPLETE",
                    "SUCCESS",
                }:
                    progress_int = 100
                bar_str = format_progress_bar(
                    0 if progress_int is None else progress_int,
                    width=14,
                    high_contrast=high_contrast,
                )
                badge = format_status_badge(
                    status_str,
                    dark=dark_mode,
                    high_contrast=high_contrast,
                )
                created_raw = getattr(job, "date_created", None)
                iso_fn = (
                    getattr(created_raw, "isoformat", None)
                    if created_raw is not None and not isinstance(created_raw, type)
                    else None
                )
                iso_str = iso_fn() if callable(iso_fn) else None
                created = (
                    iso_str
                    if isinstance(iso_str, str)
                    else _clean_scalar_str(created_raw)
                    or (ledger_meta.date_created if ledger_meta else None)
                    or ""
                )
                label = f"{job_type} ({batch_id})" if batch_id else str(job_type)
                created_suffix = f" ({created})" if created else ""
                lines.append(f"{badge} {bar_str} - {label}{created_suffix}")

            if active_count == 0:
                self._pause_polling()
                lines.append("[dim]Polling paused (0 active jobs).[/dim]")
            else:
                self._resume_polling()

            self.update("\n".join(lines))
        except Exception as e:
            self._pause_polling()
            self.update(f"[red]Error fetching jobs: {e}[/red]")


class LogViewer(Log):
    """Log viewer widget for auditing actions and SDK events."""

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the LogViewer widget."""
        super().__init__(**kwargs)
        self.border_title = "Logs"


class DashboardScreen(Screen[None]):
    """Main interactive clinical study exploration, workflow, triage, and job workbench."""

    CSS = WORKBENCH_CSS

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the DashboardScreen."""
        super().__init__(**kwargs)
        self.sdk = sdk
        self.log_viewer = LogViewer(id="log_viewer")
        self.current_study_key: str | None = None
        self.selected_subject_key: str | None = None
        self.phi_masked: bool = False

    def compose(self) -> ComposeResult:
        """Compose study/site/subject navigation alongside entity, workflow, triage, and job tabs."""
        yield Header()
        studies_box = Container(StudyList(self.sdk, id="study_list"), id="studies-box")
        studies_box.border_title = "Studies"
        sites_box = Container(SiteList(self.sdk, id="site_list"), id="sites-box")
        sites_box.border_title = "Sites"
        subjects_box = Container(SubjectTable(self.sdk, id="subject_table"), id="subjects-box")
        subjects_box.border_title = "Subjects (Enter to Drill Down)"

        with Container(id="dashboard-grid"):
            yield studies_box
            yield sites_box
            yield subjects_box

            with TabbedContent(id="bottom-pane"):
                with TabPane("Jobs", id="jobs-tab"), Vertical():
                    with Horizontal(classes="pane-toolbar"):
                        yield Input(
                            placeholder="Enter batch_id / job_id to track manually...",
                            id="manual_job_input",
                            classes="filter-input",
                        )
                        yield Button("Track Job", id="track_job_btn", variant="primary")
                        yield Button("Refresh Jobs", id="refresh_jobs_btn", variant="default")
                    yield JobMonitor(self.sdk, id="job_monitor")
                with TabPane("Records", id="records-tab"):
                    yield EntityTablePane(
                        self.sdk,
                        ENTITY_VIEW_SPECS["records"],
                        id="records_pane",
                    )
                with TabPane("Visits", id="visits-tab"):
                    yield EntityTablePane(
                        self.sdk,
                        ENTITY_VIEW_SPECS["visits"],
                        id="visits_pane",
                    )
                with TabPane("Queries", id="queries-tab"):
                    yield EntityTablePane(
                        self.sdk,
                        ENTITY_VIEW_SPECS["queries"],
                        id="queries_pane",
                    )
                with TabPane("Forms", id="forms-tab"):
                    yield EntityTablePane(
                        self.sdk,
                        ENTITY_VIEW_SPECS["forms"],
                        id="forms_pane",
                    )
                with TabPane("Variables", id="variables-tab"):
                    yield EntityTablePane(
                        self.sdk,
                        ENTITY_VIEW_SPECS["variables"],
                        id="variables_pane",
                    )
                with TabPane("Record Revisions", id="revisions-tab"):
                    yield EntityTablePane(
                        self.sdk,
                        ENTITY_VIEW_SPECS["record_revisions"],
                        id="revisions_pane",
                    )
                with TabPane("Data Dictionary", id="datadict-tab"):
                    yield DataDictionaryPane(self.sdk, id="datadict_pane")
                with (
                    TabPane("More Entities", id="more-entities-tab"),
                    TabbedContent(id="more-entities-subtabs"),
                ):
                    with TabPane("Intervals", id="intervals-subtab"):
                        yield EntityTablePane(
                            self.sdk,
                            ENTITY_VIEW_SPECS["intervals"],
                            id="intervals_pane",
                        )
                    with TabPane("Codings", id="codings-subtab"):
                        yield EntityTablePane(
                            self.sdk,
                            ENTITY_VIEW_SPECS["codings"],
                            id="codings_pane",
                        )
                    with TabPane("Users", id="users-subtab"):
                        yield EntityTablePane(
                            self.sdk,
                            ENTITY_VIEW_SPECS["users"],
                            id="users_pane",
                        )
                with TabPane("Workflows", id="workflows-tab"):
                    yield WorkflowsPane(
                        self.sdk,
                        log_callback=self._append_log_line,
                        id="workflows_pane",
                    )
                with TabPane("Triage", id="triage-tab"):
                    yield TriagePane(id="triage_pane")
                with TabPane("Logs", id="logs-tab"):
                    yield self.log_viewer
        yield Footer()

    def _append_log_line(self, line: str) -> None:
        """Append a single line to the dashboard LogViewer."""
        self.log_viewer.write_line(line)

    def load_all_study_panes(self, study_key: str) -> None:
        """Asynchronously load all study entity, Data Dictionary, workflow, and triage views."""
        for pane in self.query(EntityTablePane):
            self.run_worker(pane.load_study(study_key, subject_key=None))
        dd_pane = self.query_one(DataDictionaryPane)
        self.run_worker(dd_pane.load_study(study_key))
        wf_pane = self.query_one(WorkflowsPane)
        wf_pane.set_study(study_key)
        triage_pane = self.query_one(TriagePane)
        self.run_worker(triage_pane.load_study(study_key))

    def set_phi_masked(self, enabled: bool) -> None:
        """Apply clinical PHI masking across SubjectTable and all EntityTablePanes."""
        self.phi_masked = enabled
        self.query_one(SubjectTable).set_phi_masked(enabled)
        for pane in self.query(EntityTablePane):
            pane.set_phi_masked(enabled)
        state_word = "enabled" if enabled else "disabled"
        self.log_viewer.write_line(f"Clinical PHI masking {state_word}.")

    def clear_subject_scope(self) -> None:
        """Clear Subject-specific filtering across Records, Visits, Queries, and Revisions."""
        self.selected_subject_key = None
        for pane in self.query(EntityTablePane):
            pane.set_subject_filter(None)
        subjects_box = self.query_one("#subjects-box", Container)
        subjects_box.border_title = "Subjects (Enter to Drill Down)"

    def focus_active_filter(self) -> None:
        """Focus the keyboard filter Input of whichever bottom tab is active."""
        tabs = self.query_one("#bottom-pane", TabbedContent)
        active_id = tabs.active
        pane_map = {
            "records-tab": "#records_pane",
            "visits-tab": "#visits_pane",
            "queries-tab": "#queries_pane",
            "forms-tab": "#forms_pane",
            "variables-tab": "#variables_pane",
            "revisions-tab": "#revisions_pane",
        }
        if active_id in pane_map:
            self.query_one(pane_map[active_id], EntityTablePane).focus_filter()
        elif active_id == "datadict-tab":
            self.query_one(DataDictionaryPane).focus_filter()
        elif active_id == "triage-tab":
            self.query_one("#triage_filter_input", Input).focus()
        elif active_id == "jobs-tab":
            self.query_one("#manual_job_input", Input).focus()

    def _submit_manual_job(self) -> None:
        """Register the batch_id entered in #manual_job_input with JobMonitor and JobLedger."""
        job_input = self.query_one("#manual_job_input", Input)
        batch_id = job_input.value.strip()
        if not batch_id:
            return
        job_monitor = self.query_one(JobMonitor)
        job_monitor.track_job(batch_id, persist=True)
        self.log_viewer.write_line(f"Tracking manual background job: {batch_id}")
        job_input.value = ""

    def on_study_list_selected(self, message: StudyList.Selected) -> None:
        """Handle study selection by populating sites, subjects, entities, workflows, and jobs."""
        self.current_study_key = message.study_key
        self.selected_subject_key = None
        self.log_viewer.write_line(f"Selected Study: {message.study_name} ({message.study_key})")

        site_list = self.query_one(SiteList)
        self.run_worker(site_list.load_sites(message.study_key))

        subject_table = self.query_one(SubjectTable)
        self.run_worker(subject_table.load_subjects(message.study_key))

        job_monitor = self.query_one(JobMonitor)
        job_monitor.set_study(message.study_key)

        self.load_all_study_panes(message.study_key)

    def on_site_list_selected(self, message: SiteList.Selected) -> None:
        """Handle site selection by filtering displayed subjects."""
        self.log_viewer.write_line(f"Selected Site: {message.site_name} (ID: {message.site_id})")
        site_list = self.query_one(SiteList)
        study_key = site_list.current_study_key
        if study_key:
            subject_table = self.query_one(SubjectTable)
            self.run_worker(subject_table.load_subjects(study_key, message.site_id))

    def on_subject_table_selected(self, message: SubjectTable.Selected) -> None:
        """Filter Records, Visits, Queries, and Revisions when a Subject is selected."""
        self.selected_subject_key = message.subject_key
        self.log_viewer.write_line(
            f"Selected Subject: {message.subject_key} (ID: {message.subject_id})"
        )
        subjects_box = self.query_one("#subjects-box", Container)
        subjects_box.border_title = f"Subjects [Filtered: {message.subject_key}]"
        for pane in self.query(EntityTablePane):
            pane.set_subject_filter(message.subject_key)

    def on_entity_table_pane_subject_scope_cleared(
        self, _message: EntityTablePane.SubjectScopeCleared
    ) -> None:
        """Synchronize Subject filter reset across all entity panes."""
        self.clear_subject_scope()

    def on_workflows_pane_job_created(self, message: WorkflowsPane.JobCreated) -> None:
        """Register background jobs emitted by TUI Workflows directly in JobMonitor."""
        job_monitor = self.query_one(JobMonitor)
        if job_monitor.current_study_key == message.study_key:
            job_monitor.track_job(message.batch_id, persist=True)
        self.log_viewer.write_line(
            f"Workflow '{message.workflow_name}' registered Job batch_id={message.batch_id}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle manual job tracking and refresh buttons in the Jobs tab."""
        if event.button.id == "track_job_btn":
            self._submit_manual_job()
        elif event.button.id == "refresh_jobs_btn":
            job_monitor = self.query_one(JobMonitor)
            self.run_worker(job_monitor.refresh_jobs())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Track manual job ID when pressing Enter inside #manual_job_input."""
        if event.input.id == "manual_job_input":
            self._submit_manual_job()


class ImednetTuiApp(App[None]):
    """The iMednet TUI Application."""

    CSS = WORKBENCH_CSS

    BINDINGS: ClassVar[list[BindingType]] = [
        ("q", "quit", "Quit"),
        ("d", "toggle_dark", "Dark Mode"),
        ("h", "toggle_high_contrast", "High Contrast"),
        ("m", "toggle_phi_mask", "Mask PHI"),
        ("c", "clear_subject_filter", "All Subjects"),
        ("/", "focus_filter", "Filter"),
        ("r", "refresh_data", "Refresh"),
    ]

    def __init__(self, sdk: ImednetSDK, **kwargs: Any) -> None:
        """Initialize the iMednet TUI application."""
        super().__init__(**kwargs)
        self.sdk = sdk
        self.high_contrast_enabled = os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"
        self._log_handler: TextualLogHandler | None = None

    def on_mount(self) -> None:
        """Configure application title, contrast mode, screen, and logger on mount."""
        self.title = "iMednet Mission Control"
        base_url = getattr(getattr(self.sdk, "config", None), "base_url", None) or getattr(
            self.sdk, "_base_url", "Unknown"
        )
        self.sub_title = f"Env: {base_url}"

        if self.high_contrast_enabled:
            self.add_class("high-contrast")

        dashboard = DashboardScreen(self.sdk)
        self.push_screen(dashboard)

        with contextlib.suppress(Exception):
            handler = TextualLogHandler(
                dashboard.log_viewer,
                owner_thread_id=threading.get_ident(),
            )
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            logger = logging.getLogger("imednet")
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            self._log_handler = handler

    def on_unmount(self) -> None:
        """Detach the Textual log handler on application teardown."""
        if self._log_handler is not None:
            with contextlib.suppress(Exception):
                logging.getLogger("imednet").removeHandler(self._log_handler)
            self._log_handler = None

    def action_toggle_high_contrast(self) -> None:
        """Toggle accessibility high contrast mode."""
        self.high_contrast_enabled = not self.high_contrast_enabled
        if self.high_contrast_enabled:
            self.add_class("high-contrast")
            os.environ["IMEDNET_HIGH_CONTRAST"] = "1"
        else:
            self.remove_class("high-contrast")
            os.environ.pop("IMEDNET_HIGH_CONTRAST", None)

    def action_toggle_phi_mask(self) -> None:
        """Toggle clinical PHI masking across all workbench views."""
        active_screen: Any = self.screen
        if isinstance(active_screen, DashboardScreen):
            active_screen.set_phi_masked(not active_screen.phi_masked)

    def action_clear_subject_filter(self) -> None:
        """Clear Subject drill-down filter on the active dashboard."""
        active_screen: Any = self.screen
        if isinstance(active_screen, DashboardScreen):
            active_screen.clear_subject_scope()

    def action_focus_filter(self) -> None:
        """Focus the active pane's keyboard filter input."""
        active_screen: Any = self.screen
        if isinstance(active_screen, DashboardScreen):
            active_screen.focus_active_filter()

    def action_refresh_data(self) -> None:
        """Refresh current screen data."""
        with contextlib.suppress(Exception):
            active_screen: Any = self.screen
            if isinstance(active_screen, DashboardScreen):
                study_list = active_screen.query_one(StudyList)
                self.run_worker(study_list.load_studies())
                if active_screen.current_study_key:
                    active_screen.load_all_study_panes(active_screen.current_study_key)
                    self.run_worker(active_screen.query_one(JobMonitor).refresh_jobs())


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
