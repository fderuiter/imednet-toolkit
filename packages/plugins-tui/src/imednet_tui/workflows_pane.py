"""Dynamic clinical Workflows discovery and off-UI-thread execution pane for iMednet TUI."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from imednet.spi.jobs import record_study_job
from imednet.spi.models import RegisterSubjectRequest

if TYPE_CHECKING:
    from imednet.sdk import AsyncImednetSDK, ImednetSDK

logger = logging.getLogger("imednet.tui.workflows")


@dataclass(frozen=True)
class WorkflowPluginDescriptor:
    """Metadata and input hints for a discoverable ``imednet.workflows`` plugin."""

    name: str
    title: str
    description: str
    param_placeholder: str
    payload_placeholder: str


CANONICAL_WORKFLOW_DESCRIPTORS: dict[str, WorkflowPluginDescriptor] = {
    "data_extraction": WorkflowPluginDescriptor(
        name="data_extraction",
        title="Data Extraction",
        description="Extract records by criteria or export 21 CFR Part 11 audit trail.",
        param_placeholder="mode=records (or mode=audit, start_date=YYYY-MM-DD)",
        payload_placeholder='Optional JSON record filter, e.g. {"form_key": "F_DM"}',
    ),
    "duckdb_centralizer": WorkflowPluginDescriptor(
        name="duckdb_centralizer",
        title="DuckDB Centralizer",
        description="Ingest study record revisions into bronze/silver DuckDB tables.",
        param_placeholder="db_path=:memory:, mode=append",
        payload_placeholder="Optional JSON date range: {\"start_date\": \"2025-01-01\"}",
    ),
    "query_management": WorkflowPluginDescriptor(
        name="query_management",
        title="Query Management",
        description="Inspect open clinical queries, subject/site queries, or state counts.",
        param_placeholder="action=counts (or open | subject:<KEY> | site:<NAME>)",
        payload_placeholder="Optional JSON filter dictionary",
    ),
    "record_mapper": WorkflowPluginDescriptor(
        name="record_mapper",
        title="Record Mapper",
        description="Map EDC records to tabular DataFrames, CDISC SDTM domains, or hierarchies.",
        param_placeholder="mode=dataframe (or sdtm:AE | hierarchy)",
        payload_placeholder="Optional visit_key integer",
    ),
    "record_update": WorkflowPluginDescriptor(
        name="record_update",
        title="Record Update",
        description="Validate and submit batch record creations or updates (emits Job).",
        param_placeholder="wait=false",
        payload_placeholder='JSON array: [{"formKey": "F_DM", "siteName": "Site 1", "data": {}}]',
    ),
    "register_subjects": WorkflowPluginDescriptor(
        name="register_subjects",
        title="Register Subjects",
        description="Validate sites and register one or more study subjects (emits Job).",
        param_placeholder="email_notify=",
        payload_placeholder=(
            'JSON array: [{"formKey": "F_DM", "siteName": "Site 1", '
            '"subjectKey": "SUB-101", "data": {}}]'
        ),
    ),
    "subject_data": WorkflowPluginDescriptor(
        name="subject_data",
        title="Subject Data",
        description="Aggregate subject demographics, visits, records, and queries.",
        param_placeholder="subject_key=SUB1 (required)",
        payload_placeholder="Leave blank",
    ),
    "uat": WorkflowPluginDescriptor(
        name="uat",
        title="UAT Runner",
        description="Execute end-to-end synthetic UAT generation, submission, and polling.",
        param_placeholder="subject_count=1, site_name=",
        payload_placeholder='Optional JSON form keys array, e.g. ["F_DM"]',
    ),
    "uat_inspector": WorkflowPluginDescriptor(
        name="uat_inspector",
        title="UAT Schema Inspector",
        description="Snapshot study forms, variables, intervals, and active sites for UAT.",
        param_placeholder="force_refresh=false",
        payload_placeholder="Leave blank",
    ),
}


def discover_workflow_plugins(sdk: Any) -> tuple[list[WorkflowPluginDescriptor], str | None]:
    """Discover registered ``imednet.workflows`` plugins from SDK namespace or entry points.

    Returns:
        Tuple of ``(descriptors, degradation_notice)``.
    """
    discovered_names: list[str] = []

    try:
        wf_ns = getattr(sdk, "workflows", None)
        if wf_ns is not None:
            for candidate in CANONICAL_WORKFLOW_DESCRIPTORS:
                if candidate == "duckdb_centralizer" or hasattr(wf_ns, candidate):
                    discovered_names.append(candidate)
    except ImportError as exc:
        return [], (
            f"Optional 'imednet-workflows' plugin is not installed ({exc}). "
            "Install 'imednet-workflows' to run clinical workflows from the TUI."
        )
    except Exception as exc:
        logger.debug("Workflow namespace inspection warning: %s", exc)

    if not discovered_names:
        try:
            eps = importlib.metadata.entry_points(group="imednet.workflows")
            discovered_names = sorted({ep.name for ep in eps})
        except Exception:
            discovered_names = []

    if not discovered_names:
        return [], (
            "No 'imednet.workflows' plugins detected. "
            "Install 'imednet-workflows' to enable workflow execution."
        )

    descriptors: list[WorkflowPluginDescriptor] = []
    seen: set[str] = set()
    for name in discovered_names:
        if name in seen:
            continue
        seen.add(name)
        descriptors.append(
            CANONICAL_WORKFLOW_DESCRIPTORS.get(
                name,
                WorkflowPluginDescriptor(
                    name=name,
                    title=name.replace("_", " ").title(),
                    description=f"Registered workflow plugin '{name}'.",
                    param_placeholder="key=value",
                    payload_placeholder="Optional JSON payload",
                ),
            )
        )
    return descriptors, None


def _parse_kv_params(raw_params: str) -> dict[str, str]:
    """Parse comma-separated ``key=value`` parameter pairs."""
    parsed: dict[str, str] = {}
    if not raw_params.strip():
        return parsed
    for token in raw_params.split(","):
        part = token.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Expected key=value pair in parameters, got {part!r}")
        key, val = part.split("=", 1)
        clean_key = key.strip()
        if not clean_key:
            raise ValueError(f"Empty parameter name in {part!r}")
        parsed[clean_key] = val.strip()
    return parsed


def _parse_optional_json(raw_payload: str) -> Any:
    """Parse optional JSON input or raise a descriptive ValueError."""
    cleaned = raw_payload.strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload: {exc.msg}") from exc


def _extract_batch_ids(result: Any) -> list[str]:
    """Extract all background Job batch IDs emitted by a workflow result."""
    batch_ids: list[str] = []
    direct_batch = getattr(result, "batch_id", None) or getattr(result, "batchId", None)
    if direct_batch and str(direct_batch).strip():
        batch_ids.append(str(direct_batch).strip())

    sub_result = getattr(result, "submission_result", None)
    if sub_result is not None:
        for bid in getattr(sub_result, "all_batch_ids", []):
            if bid and str(bid).strip() and str(bid).strip() not in batch_ids:
                batch_ids.append(str(bid).strip())
    return batch_ids


def _execute_workflow_sync(
    sdk: Any,
    workflow_name: str,
    study_key: str,
    params: dict[str, str],
    payload: Any,
    emit_log: Callable[[str], None],
) -> tuple[str, list[str]]:
    """Run the selected workflow synchronously (invoked inside ``asyncio.to_thread``)."""
    wf_ns = sdk.workflows
    emit_log(f"Starting workflow '{workflow_name}' for study '{study_key}'...")

    if workflow_name == "data_extraction":
        extractor = wf_ns.data_extraction
        mode = params.get("mode", "records").lower()
        if mode == "audit":
            revisions = extractor.extract_audit_trail(
                study_key,
                start_date=params.get("start_date") or None,
                end_date=params.get("end_date") or None,
            )
            summary = f"Extracted {len(revisions)} audit trail revision(s) for '{study_key}'."
            emit_log(summary)
            return summary, []
        record_filter = payload if isinstance(payload, dict) else None
        records = extractor.extract_records_by_criteria(
            study_key=study_key,
            record_filter=record_filter,
            on_progress=emit_log,
        )
        summary = f"Extracted {len(records)} matching record(s) for '{study_key}'."
        emit_log(summary)
        return summary, []

    if workflow_name == "duckdb_centralizer":
        db_path = params.get("db_path", ":memory:") or ":memory:"
        mode_arg = params.get("mode", "append")
        if mode_arg not in {"append", "replace"}:
            raise ValueError("mode must be 'append' or 'replace'")
        if db_path == ":memory:":
            centralizer = wf_ns.duckdb_centralizer
        else:
            centralizer_cls = type(wf_ns.duckdb_centralizer)
            centralizer = centralizer_cls(sdk, db_path=db_path)
        dates = payload if isinstance(payload, dict) else {}
        row_count = centralizer.ingest_revisions(
            study_key,
            start_date=dates.get("start_date") or params.get("start_date") or None,
            end_date=dates.get("end_date") or params.get("end_date") or None,
            mode=mode_arg,
        )
        centralizer.build_silver_view(study_key)
        summary = f"Centralized {row_count} revision row(s) into DuckDB ({db_path}) + silver view."
        emit_log(summary)
        return summary, []

    if workflow_name == "query_management":
        qm = wf_ns.query_management
        action = params.get("action", "counts")
        extra_filter = payload if isinstance(payload, dict) else None
        if action == "open":
            queries = qm.get_open_queries(study_key, additional_filter=extra_filter)
            summary = f"Found {len(queries)} open clinical querie(s) in '{study_key}'."
        elif action.startswith("subject:"):
            subj_key = action.split(":", 1)[1].strip()
            if not subj_key:
                raise ValueError("subject:<KEY> requires a non-empty subject key.")
            queries = qm.get_queries_for_subject(
                study_key, subj_key, additional_filter=extra_filter
            )
            summary = f"Found {len(queries)} querie(s) for subject '{subj_key}'."
        elif action.startswith("site:"):
            site_name = action.split(":", 1)[1].strip()
            if not site_name:
                raise ValueError("site:<NAME> requires a non-empty site name.")
            queries = qm.get_queries_by_site(study_key, site_name, additional_filter=extra_filter)
            summary = f"Found {len(queries)} querie(s) for site '{site_name}'."
        else:
            counts = dict(qm.get_query_state_counts(study_key))
            summary = f"Query state counts for '{study_key}': {json.dumps(counts, sort_keys=True)}"
        emit_log(summary)
        return summary, []

    if workflow_name == "record_mapper":
        mapper = wf_ns.record_mapper
        mode = params.get("mode", "dataframe")
        visit_key = str(payload).strip() if payload is not None and str(payload).strip() else None
        if mode == "hierarchy":
            tree = mapper.build_hierarchy(study_key=study_key, visit_key=visit_key)
            summary = f"Built Subject->Visit->Form hierarchy for {len(tree)} subject(s)."
        elif mode.startswith("sdtm"):
            domain = mode.split(":", 1)[1].strip() if ":" in mode else "AE"
            df = mapper.sdtm_dataframe(study_key=study_key, domain=domain, visit_key=visit_key)
            summary = f"Mapped {len(df)} SDTM '{domain.upper()}' row(s) ({len(df.columns)} cols)."
        else:
            df = mapper.dataframe(study_key=study_key, visit_key=visit_key)
            summary = f"Mapped {len(df)} study record row(s) across {len(df.columns)} column(s)."
        emit_log(summary)
        return summary, []

    if workflow_name == "record_update":
        if not isinstance(payload, list) or not payload:
            raise ValueError(
                "record_update requires a non-empty JSON array of record dictionaries."
            )
        wait_flag = params.get("wait", "false").lower() in {"1", "true", "yes"}
        updater = wf_ns.record_update
        job = updater.create_or_update_records(
            study_key=study_key,
            records_data=payload,
            wait_for_completion=wait_flag,
        )
        batches = _extract_batch_ids(job)
        summary = (
            f"Submitted {len(payload)} record(s); batch_id={batches[0] if batches else 'N/A'}."
        )
        emit_log(summary)
        return summary, batches

    if workflow_name == "register_subjects":
        if not isinstance(payload, list) or not payload:
            raise ValueError(
                "register_subjects requires a non-empty JSON array of subject registration objects."
            )
        requests = [RegisterSubjectRequest.model_validate(entry) for entry in payload]
        email_notify = params.get("email_notify") or None
        wait_flag = params.get("wait", "false").lower() in {"1", "true", "yes"}
        reg_wf = wf_ns.register_subjects
        job = reg_wf.register_subjects(
            study_key=study_key,
            subjects=requests,
            email_notify=email_notify,
            wait_for_completion=wait_flag,
        )
        batches = _extract_batch_ids(job)
        summary = (
            f"Submitted {len(requests)} subject registration(s); "
            f"batch_id={batches[0] if batches else 'N/A'}."
        )
        emit_log(summary)
        return summary, batches

    if workflow_name == "subject_data":
        subj_key = params.get("subject_key", "").strip()
        if not subj_key:
            raise ValueError("subject_data requires parameter 'subject_key=<KEY>'.")
        dossier = wf_ns.subject_data.get_all_subject_data(study_key, subj_key)
        summary = (
            f"Loaded dossier for '{subj_key}': {len(dossier.visits)} visit(s), "
            f"{len(dossier.records)} record(s), {len(dossier.queries)} querie(s)."
        )
        emit_log(summary)
        return summary, []

    if workflow_name == "uat":
        subject_count = int(params.get("subject_count", "1"))
        if subject_count < 1:
            raise ValueError("subject_count must be >= 1.")
        uat_site_name = params.get("site_name") or None
        form_keys = payload if isinstance(payload, list) else None
        uat_cls = type(wf_ns.uat)
        uat_runner = uat_cls(
            sdk,
            on_phase_change=lambda phase: emit_log(f"[UAT Phase] {getattr(phase, 'value', phase)}"),
            on_progress=lambda bid, st, el: emit_log(
                f"[UAT Job] {bid}: {getattr(st, 'state', st)} ({el:.1f}s)"
            ),
        )
        uat_res = uat_runner.run(
            study_key=study_key,
            subject_count=subject_count,
            site_name=uat_site_name,
            enabled_form_keys=form_keys,
        )
        batches = _extract_batch_ids(uat_res)
        summary = uat_res.summary().replace("\n", " | ")
        emit_log(summary)
        return summary, batches

    if workflow_name == "uat_inspector":
        force_refresh = params.get("force_refresh", "false").lower() in {"1", "true", "yes"}
        snapshot = wf_ns.uat_inspector.inspect(study_key, force_refresh=force_refresh)
        summary = (
            f"Inspected '{study_key}': {len(snapshot.forms)} form(s), "
            f"{len(snapshot.variables)} variable(s), {len(snapshot.intervals)} interval(s), "
            f"{len(snapshot.sites)} site(s)."
        )
        emit_log(summary)
        return summary, []

    plugin_obj = getattr(wf_ns, workflow_name)
    if callable(plugin_obj):
        out = plugin_obj(study_key)
        batches = _extract_batch_ids(out)
        summary = f"Executed workflow '{workflow_name}': {out}"
        emit_log(summary)
        return summary, batches
    raise ValueError(f"Unsupported workflow plugin '{workflow_name}'.")


class WorkflowsPane(Container):
    """Interactive Workflows discovery, validation, and off-thread runner pane."""

    class JobCreated(Message):
        """Emitted when a workflow creates one or more background Job batch IDs."""

        def __init__(self, study_key: str, batch_id: str, workflow_name: str) -> None:
            """Initialize the JobCreated message."""
            self.study_key = study_key
            self.batch_id = batch_id
            self.workflow_name = workflow_name
            super().__init__()

    def __init__(
        self,
        sdk: ImednetSDK | AsyncImednetSDK,
        *,
        log_callback: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Workflows pane."""
        super().__init__(**kwargs)
        self.sdk = sdk
        self.log_callback = log_callback
        self.current_study_key: str | None = None
        self.descriptors: list[WorkflowPluginDescriptor] = []
        self.degradation_notice: str | None = None
        self.selected_workflow: str | None = None

    def compose(self) -> ComposeResult:
        """Compose workflow selector list alongside input validation and execution controls."""
        with Horizontal():
            yield ListView(id="workflow_list")
            with Vertical(id="workflow_form"):
                yield Label("Select a clinical workflow to configure.", id="workflow_title")
                yield Static("", id="workflow_description", classes="detail-banner")
                yield Input(
                    placeholder="Study key (auto-populated on study selection)",
                    id="workflow_study_input",
                )
                yield Input(
                    placeholder="Workflow parameters (key=value, ...)",
                    id="workflow_params_input",
                )
                yield Input(
                    placeholder="Optional JSON payload",
                    id="workflow_payload_input",
                )
                with Horizontal(classes="pane-toolbar"):
                    yield Button("Run Workflow", id="workflow_run_btn", variant="primary")
                    yield Button("Refresh Plugins", id="workflow_refresh_btn", variant="default")
                yield Static(
                    "Workflow status: Idle.",
                    id="workflow_result",
                    classes="detail-banner",
                )

    def on_mount(self) -> None:
        """Discover installed workflow plugins upon mounting."""
        self.refresh_plugins()

    def set_study(self, study_key: str) -> None:
        """Set the active study key in the workflow input form."""
        self.current_study_key = study_key
        if self.is_mounted:
            self.query_one("#workflow_study_input", Input).value = study_key

    def refresh_plugins(self) -> None:
        """Discover ``imednet.workflows`` plugins and populate the ListView."""
        self.descriptors, self.degradation_notice = discover_workflow_plugins(self.sdk)
        if not self.is_mounted:
            return
        wf_list = self.query_one("#workflow_list", ListView)
        wf_list.clear()
        run_btn = self.query_one("#workflow_run_btn", Button)

        if not self.descriptors:
            run_btn.disabled = True
            notice = self.degradation_notice or "No workflow plugins installed."
            self.query_one("#workflow_description", Static).update(notice)
            self.query_one("#workflow_result", Static).update(notice)
            return

        run_btn.disabled = False
        items = [
            ListItem(Label(f"{desc.title} ({desc.name})"), id=f"wf-{desc.name}")
            for desc in self.descriptors
        ]
        wf_list.extend(items)
        self._select_descriptor(self.descriptors[0])

    def _select_descriptor(self, descriptor: WorkflowPluginDescriptor) -> None:
        """Populate form placeholders and description for the chosen workflow."""
        self.selected_workflow = descriptor.name
        self.query_one("#workflow_title", Label).update(
            f"Workflow: {descriptor.title} ({descriptor.name})"
        )
        self.query_one("#workflow_description", Static).update(descriptor.description)
        params_input = self.query_one("#workflow_params_input", Input)
        params_input.placeholder = descriptor.param_placeholder
        payload_input = self.query_one("#workflow_payload_input", Input)
        payload_input.placeholder = descriptor.payload_placeholder

    def _emit_threadsafe_log(self, message: str) -> None:
        """Stream a workflow log message safely from worker threads to LogViewer & logger."""
        logger.info("%s", message)
        if self.log_callback is None:
            return
        try:
            self.app.call_from_thread(self.log_callback, message)
        except RuntimeError:
            # Already running on the main Textual event loop thread
            self.log_callback(message)

    async def execute_selected_workflow(self) -> None:
        """Validate inputs and run the selected workflow off the UI thread."""
        result_banner = self.query_one("#workflow_result", Static)
        if not self.selected_workflow:
            result_banner.update("Validation Error: Select a workflow before running.")
            return

        study_key = (
            self.query_one("#workflow_study_input", Input).value.strip()
            or (self.current_study_key or "").strip()
        )
        if not study_key:
            result_banner.update("Validation Error: study_key is required.")
            return

        raw_params = self.query_one("#workflow_params_input", Input).value
        raw_payload = self.query_one("#workflow_payload_input", Input).value
        try:
            params = _parse_kv_params(raw_params)
            payload = _parse_optional_json(raw_payload)
        except ValueError as exc:
            result_banner.update(f"Validation Error: {exc}")
            return

        workflow_name = self.selected_workflow
        result_banner.update(f"Running '{workflow_name}' for study '{study_key}'...")
        run_btn = self.query_one("#workflow_run_btn", Button)
        run_btn.disabled = True
        try:
            summary, batch_ids = await asyncio.to_thread(
                _execute_workflow_sync,
                self.sdk,
                workflow_name,
                study_key,
                params,
                payload,
                self._emit_threadsafe_log,
            )
            for bid in batch_ids:
                record_study_job(
                    study_key,
                    {"batch_id": bid, "state": "PROCESSING"},
                    job_type=f"Workflow:{workflow_name}",
                    source="tui-workflow",
                )
                self.post_message(self.JobCreated(study_key, bid, workflow_name))
            result_banner.update(f"Completed '{workflow_name}': {summary}")
        except ImportError as exc:
            msg = f"Optional dependency unavailable for '{workflow_name}': {exc}"
            self._emit_threadsafe_log(msg)
            result_banner.update(msg)
        except Exception as exc:
            msg = f"Workflow '{workflow_name}' failed: {exc}"
            self._emit_threadsafe_log(msg)
            result_banner.update(msg)
        finally:
            run_btn.disabled = not bool(self.descriptors)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Update form specification when a workflow item is selected."""
        if event.list_view.id != "workflow_list" or not event.item or not event.item.id:
            return
        wf_name = event.item.id.replace("wf-", "", 1)
        for desc in self.descriptors:
            if desc.name == wf_name:
                self._select_descriptor(desc)
                break

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Trigger workflow execution or plugin re-discovery."""
        if event.button.id == "workflow_run_btn":
            self.run_worker(self.execute_selected_workflow())
        elif event.button.id == "workflow_refresh_btn":
            self.refresh_plugins()


__all__ = [
    "CANONICAL_WORKFLOW_DESCRIPTORS",
    "WorkflowPluginDescriptor",
    "WorkflowsPane",
    "discover_workflow_plugins",
]
