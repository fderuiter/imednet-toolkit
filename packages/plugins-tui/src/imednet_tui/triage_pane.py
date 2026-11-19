"""Clinical Triage queue management pane backed by SQLite TriageStore."""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, Static

from imednet.spi.models import (
    ALLOWED_TRANSITIONS,
    InvalidStateTransitionError,
    TriageItem,
    TriageStatus,
    validate_transition,
)


def get_default_triage_db_path() -> Path:
    """Resolve the default SQLite path for the clinical triage store."""
    env_path = os.environ.get("IMEDNET_TRIAGE_DB_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".imednet" / "triage.db"


def _load_triage_store_class() -> tuple[type[Any] | None, str | None]:
    """Dynamically import ``TriageStore`` from optional ``imednet-workflows``."""
    try:
        mod = importlib.import_module("imednet_workflows.triage_store")
        store_cls = getattr(mod, "TriageStore", None)
        if store_cls is None:
            return None, "TriageStore class not found in imednet_workflows.triage_store."
        return store_cls, None
    except ImportError as exc:
        return None, (
            f"Optional 'imednet-workflows' package is not installed ({exc}). "
            "Install 'imednet-workflows' to manage local SQLite Triage queues."
        )


class TriagePane(Container):
    """Triage review queue screen enforcing ``ALLOWED_TRANSITIONS`` and reviewer comments."""

    def __init__(self, db_path: str | Path | None = None, **kwargs: Any) -> None:
        """Initialize the Triage queue pane."""
        super().__init__(**kwargs)
        self._explicit_db_path = Path(db_path).expanduser() if db_path is not None else None
        self.current_study_key: str | None = None
        self.status_filter: TriageStatus | None = None
        self.filter_query: str = ""
        self._items: list[TriageItem] = []
        self._displayed_items: list[TriageItem] = []
        self.selected_item_id: str | None = None

    @property
    def db_path(self) -> Path:
        """Active SQLite database file path for TriageStore."""
        if self._explicit_db_path is not None:
            return self._explicit_db_path
        return get_default_triage_db_path()

    def _open_store(self) -> tuple[Any | None, str | None]:
        """Instantiate TriageStore or return a graceful degradation message."""
        store_cls, err = _load_triage_store_class()
        if store_cls is None:
            return None, err
        return store_cls(self.db_path), None

    def compose(self) -> ComposeResult:
        """Compose triage filter bar, queue table, item detail, and transition controls."""
        with Vertical():
            with Horizontal(classes="pane-toolbar"):
                yield Input(
                    placeholder="Filter triage queue by item ID, assignee, severity...",
                    id="triage_filter_input",
                    classes="filter-input",
                )
                yield Button("All", id="triage_filter_all", variant="default")
                yield Button("NEW", id="triage_filter_new", variant="default")
                yield Button("UNDER_REVIEW", id="triage_filter_review", variant="default")
                yield Button("RESOLVED", id="triage_filter_resolved", variant="default")
            table: DataTable[str] = DataTable(id="triage_table")
            table.cursor_type = "row"
            yield table
            yield Static(
                "Triage Queue: Select a study to view review items.",
                id="triage_detail",
                classes="detail-banner",
            )
            with Horizontal(classes="pane-toolbar"):
                yield Input(
                    placeholder="Item ID (for new or selected item)",
                    id="triage_item_id_input",
                    classes="filter-input",
                )
                yield Input(
                    placeholder="Reviewer ID (e.g. reviewer1)",
                    id="triage_reviewer_input",
                    value="reviewer",
                    classes="filter-input",
                )
                yield Input(
                    placeholder="Reviewer comment (required for status transitions)",
                    id="triage_comment_input",
                    classes="filter-input",
                )
            with Horizontal(classes="pane-toolbar"):
                yield Button("Add Item", id="triage_add_btn", variant="primary")
                yield Button(
                    "-> NEW",
                    id="triage_to_new_btn",
                    variant="default",
                )
                yield Button(
                    "-> UNDER_REVIEW",
                    id="triage_to_under_review_btn",
                    variant="default",
                )
                yield Button(
                    "-> RESOLVED",
                    id="triage_to_resolved_btn",
                    variant="success",
                )
                yield Button("Add Comment", id="triage_annotate_btn", variant="default")
                yield Label("", id="triage_allowed_label")

    def on_mount(self) -> None:
        """Configure columns for the triage queue table."""
        table = self.query_one("#triage_table", DataTable)
        if not table.columns:
            table.add_columns(
                "Item ID",
                "Status",
                "Severity",
                "Assignee",
                "Comments",
                "History",
            )
        store, err = self._open_store()
        if store is None and err:
            self.query_one("#triage_detail", Static).update(err)
            self._set_controls_disabled(True)

    def _set_controls_disabled(self, disabled: bool) -> None:
        """Enable or disable triage mutation buttons."""
        for btn_id in (
            "#triage_add_btn",
            "#triage_to_new_btn",
            "#triage_to_under_review_btn",
            "#triage_to_resolved_btn",
            "#triage_annotate_btn",
        ):
            self.query_one(btn_id, Button).disabled = disabled

    async def load_study(self, study_key: str) -> None:
        """Load the study's triage items from SQLite off the UI thread."""
        self.current_study_key = study_key
        await self.refresh_queue()

    async def refresh_queue(self) -> None:
        """Fetch triage items for the active study and re-render the queue table."""
        if not self.current_study_key:
            return
        store, err = self._open_store()
        if store is None:
            if self.is_mounted and err:
                self.query_one("#triage_detail", Static).update(err)
                self._set_controls_disabled(True)
            return

        study_key = self.current_study_key
        status_filter = self.status_filter
        items = await asyncio.to_thread(store.get_queue, study_key, status_filter)
        self._items = items
        if self.is_mounted:
            self._set_controls_disabled(False)
            self._render_rows()

    def _render_rows(self) -> None:
        """Render filtered triage rows and refresh transition button states."""
        table = self.query_one("#triage_table", DataTable)
        table.clear()

        needle = self.filter_query.strip().lower()
        displayed: list[TriageItem] = []
        rows: list[tuple[str, ...]] = []

        for item in self._items:
            row = (
                item.item_id,
                item.status.value,
                item.severity,
                item.assignee or "-",
                str(len(item.annotations)),
                str(len(item.history)),
            )
            if needle and not any(needle in cell.lower() for cell in row):
                continue
            displayed.append(item)
            rows.append(row)

        self._displayed_items = displayed
        if rows:
            table.add_rows(rows)

        if self.selected_item_id:
            selected = next((it for it in displayed if it.item_id == self.selected_item_id), None)
            if selected is not None:
                self._update_selection_controls(selected)
                return

        if displayed:
            self._select_item(displayed[0])
        else:
            self.selected_item_id = None
            filter_name = self.status_filter.value if self.status_filter else "ALL"
            self.query_one("#triage_detail", Static).update(
                f"Triage Queue ({self.current_study_key or '-'}) ({filter_name}): 0 items."
            )
            self.query_one("#triage_allowed_label", Label).update("")

    def _select_item(self, item: TriageItem) -> None:
        """Select a triage item and display its history, comments, and allowed transitions."""
        self.selected_item_id = item.item_id
        self.query_one("#triage_item_id_input", Input).value = item.item_id
        self._update_selection_controls(item)

    def _update_selection_controls(self, item: TriageItem) -> None:
        """Update transition buttons and detail text according to ALLOWED_TRANSITIONS."""
        allowed = ALLOWED_TRANSITIONS.get(item.status, set())
        allowed_names = sorted(st.value for st in allowed)
        self.query_one("#triage_allowed_label", Label).update(
            f"Allowed: {', '.join(allowed_names) if allowed_names else 'None'}"
        )
        self.query_one("#triage_to_new_btn", Button).disabled = TriageStatus.NEW not in allowed
        self.query_one("#triage_to_under_review_btn", Button).disabled = (
            TriageStatus.UNDER_REVIEW not in allowed
        )
        self.query_one("#triage_to_resolved_btn", Button).disabled = (
            TriageStatus.RESOLVED not in allowed
        )

        last_comment = item.annotations[-1].comment if item.annotations else "No comments"
        last_hist = (
            f"{item.history[-1].from_status.value}->{item.history[-1].to_status.value} "
            f"by {item.history[-1].user_id} ({item.history[-1].comment or '-'})"
            if item.history
            else "Initial state"
        )
        self.query_one("#triage_detail", Static).update(
            f"Item {item.item_id} ({item.status.value}) | Severity: {item.severity} | "
            f"Latest Transition: {last_hist} | Latest Note: {last_comment}"
        )

    async def create_triage_item(
        self,
        item_id: str,
        *,
        severity: str = "MEDIUM",
        assignee: str | None = None,
        initial_comment: str | None = None,
        reviewer_id: str = "reviewer",
    ) -> None:
        """Create a new triage item in the study queue."""
        detail = self.query_one("#triage_detail", Static)
        if not self.current_study_key:
            detail.update("Validation Error: Select a study before adding triage items.")
            return
        clean_id = item_id.strip()
        if not clean_id:
            detail.update("Validation Error: Item ID must not be empty.")
            return

        store, err = self._open_store()
        if store is None:
            detail.update(err or "TriageStore unavailable.")
            return

        study_key = self.current_study_key
        try:
            new_item = TriageItem(
                item_id=clean_id,
                study_key=study_key,
                status=TriageStatus.NEW,
                assignee=assignee.strip() if assignee and assignee.strip() else None,
                severity=severity,
            )
            clean_reviewer = reviewer_id.strip() or "reviewer"
            clean_comment = initial_comment.strip() if initial_comment else None
            if hasattr(store, "create_item"):
                await asyncio.to_thread(
                    store.create_item,
                    new_item,
                    initial_comment=clean_comment,
                    user_id=clean_reviewer,
                )
            else:
                existing = await asyncio.to_thread(store.get_triage_item, clean_id)
                if existing is not None:
                    raise ValueError(f"Triage item '{clean_id}' already exists.")
                await asyncio.to_thread(store.upsert_item, new_item)
                if clean_comment:
                    await asyncio.to_thread(
                        store.add_annotation,
                        clean_id,
                        clean_reviewer,
                        clean_comment,
                    )
            if self.is_mounted:
                self.query_one("#triage_comment_input", Input).value = ""
            self.selected_item_id = clean_id
            await self.refresh_queue()
        except Exception as exc:
            detail.update(f"Failed to create triage item '{clean_id}': {exc}")

    async def transition_selected_item(self, target_status: TriageStatus) -> None:
        """Validate and execute a status transition with a required reviewer comment."""
        detail = self.query_one("#triage_detail", Static)
        item_id = (
            self.query_one("#triage_item_id_input", Input).value.strip()
            or (self.selected_item_id or "").strip()
        )
        reviewer = self.query_one("#triage_reviewer_input", Input).value.strip() or "reviewer"
        comment = self.query_one("#triage_comment_input", Input).value.strip()

        if not item_id:
            detail.update("Validation Error: Select a triage item to transition.")
            return
        if not comment:
            detail.update(
                "Validation Error: A non-empty reviewer comment is required for status transitions."
            )
            return

        store, err = self._open_store()
        if store is None:
            detail.update(err or "TriageStore unavailable.")
            return

        try:
            existing = await asyncio.to_thread(store.get_triage_item, item_id)
            if existing is None:
                detail.update(f"Validation Error: Unknown triage item '{item_id}'.")
                return
            validate_transition(existing.status, target_status)
            await asyncio.to_thread(
                store.update_status,
                item_id,
                target_status,
                reviewer,
                comment,
            )
            self.query_one("#triage_comment_input", Input).value = ""
            self.selected_item_id = item_id
            await self.refresh_queue()
        except (InvalidStateTransitionError, ValueError) as exc:
            detail.update(f"Invalid Transition: {exc}")
        except Exception as exc:
            detail.update(f"Transition error: {exc}")

    async def annotate_selected_item(self) -> None:
        """Append a reviewer annotation comment to the selected triage item."""
        detail = self.query_one("#triage_detail", Static)
        item_id = (
            self.query_one("#triage_item_id_input", Input).value.strip()
            or (self.selected_item_id or "").strip()
        )
        reviewer = self.query_one("#triage_reviewer_input", Input).value.strip() or "reviewer"
        comment = self.query_one("#triage_comment_input", Input).value.strip()

        if not item_id:
            detail.update("Validation Error: Select or enter an Item ID to comment on.")
            return
        if not comment:
            detail.update("Validation Error: Reviewer comment must not be empty.")
            return

        store, err = self._open_store()
        if store is None:
            detail.update(err or "TriageStore unavailable.")
            return

        try:
            await asyncio.to_thread(store.add_annotation, item_id, reviewer, comment)
            self.query_one("#triage_comment_input", Input).value = ""
            self.selected_item_id = item_id
            await self.refresh_queue()
        except Exception as exc:
            detail.update(f"Failed to add comment: {exc}")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter displayed triage rows live."""
        if event.input.id == "triage_filter_input":
            self.filter_query = event.value
            self._render_rows()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Populate item details and allowed transitions when a row is selected."""
        if event.data_table.id != "triage_table":
            return
        idx = event.cursor_row
        if 0 <= idx < len(self._displayed_items):
            self._select_item(self._displayed_items[idx])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch filter, add, transition, and comment button actions."""
        btn_id = event.button.id
        if btn_id == "triage_filter_all":
            self.status_filter = None
            self.run_worker(self.refresh_queue())
        elif btn_id == "triage_filter_new":
            self.status_filter = TriageStatus.NEW
            self.run_worker(self.refresh_queue())
        elif btn_id == "triage_filter_review":
            self.status_filter = TriageStatus.UNDER_REVIEW
            self.run_worker(self.refresh_queue())
        elif btn_id == "triage_filter_resolved":
            self.status_filter = TriageStatus.RESOLVED
            self.run_worker(self.refresh_queue())
        elif btn_id == "triage_add_btn":
            item_id = self.query_one("#triage_item_id_input", Input).value
            reviewer = self.query_one("#triage_reviewer_input", Input).value
            comment = self.query_one("#triage_comment_input", Input).value
            self.run_worker(
                self.create_triage_item(
                    item_id,
                    initial_comment=comment or None,
                    reviewer_id=reviewer or "reviewer",
                )
            )
        elif btn_id == "triage_to_new_btn":
            self.run_worker(self.transition_selected_item(TriageStatus.NEW))
        elif btn_id == "triage_to_under_review_btn":
            self.run_worker(self.transition_selected_item(TriageStatus.UNDER_REVIEW))
        elif btn_id == "triage_to_resolved_btn":
            self.run_worker(self.transition_selected_item(TriageStatus.RESOLVED))
        elif btn_id == "triage_annotate_btn":
            self.run_worker(self.annotate_selected_item())


__all__ = [
    "TriagePane",
    "get_default_triage_db_path",
]
