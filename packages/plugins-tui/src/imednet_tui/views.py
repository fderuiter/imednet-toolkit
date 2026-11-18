"""Study entity tables and Data Dictionary inspection panes for iMednet TUI."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, DataTable, Input, Label, Static, TabbedContent, TabPane

from imednet.spi.datadict import (
    DataDictionary,
    get_schema,
    load_from_api,
    load_from_cdisc_odm,
    load_from_csv,
    load_from_redcap,
    validate,
)
from imednet.spi.utils import mask_clinical_phi

if TYPE_CHECKING:
    from imednet.sdk import AsyncImednetSDK, ImednetSDK


def _stringify_cell(raw_value: Any, *, phi_masked: bool = False, sensitive: bool = False) -> str:
    """Format a model attribute or dictionary value for display in a DataTable."""
    if raw_value is None or raw_value == "":
        return "-"
    if isinstance(raw_value, dict):
        payload = mask_clinical_phi(raw_value) if phi_masked else raw_value
        return json.dumps(payload, default=str, sort_keys=True)
    if phi_masked and sensitive:
        masked_scalar = mask_clinical_phi({"patient_name": raw_value})
        return str(masked_scalar.get("patient_name", "***MASKED***"))
    if isinstance(raw_value, list):
        if not raw_value:
            return "-"
        return f"{len(raw_value)} item(s)"
    if hasattr(raw_value, "value"):
        return str(raw_value.value)
    return str(raw_value)


def _extract_attr(item: Any, *names: str) -> Any:
    """Return the first non-None attribute or dict value found on item."""
    for name in names:
        if isinstance(item, dict):
            if name in item and item[name] is not None:
                return item[name]
        else:
            val = getattr(item, name, None)
            if val is not None and not callable(val):
                return val
    return None


@dataclass(frozen=True)
class EntityColumnSpec:
    """Declarative column definition for an EntityTablePane."""

    header: str
    attr_names: tuple[str, ...]
    sensitive: bool = False


@dataclass(frozen=True)
class EntityViewSpec:
    """Declarative specification for a Study-scoped SDK endpoint table."""

    key: str
    title: str
    endpoint_attr: str
    columns: tuple[EntityColumnSpec, ...]
    supports_subject_filter: bool = False
    read_only_notice: str | None = None


ENTITY_VIEW_SPECS: dict[str, EntityViewSpec] = {
    "records": EntityViewSpec(
        key="records",
        title="Records",
        endpoint_attr="records",
        supports_subject_filter=True,
        columns=(
            EntityColumnSpec("Record ID", ("record_id", "recordId")),
            EntityColumnSpec("Subject Key", ("subject_key", "subjectKey")),
            EntityColumnSpec("Form Key", ("form_key", "formKey", "form_id")),
            EntityColumnSpec("Visit ID", ("visit_id", "visitId")),
            EntityColumnSpec("Status", ("record_status", "recordStatus")),
            EntityColumnSpec("Data", ("record_data", "recordData"), sensitive=True),
        ),
    ),
    "visits": EntityViewSpec(
        key="visits",
        title="Visits",
        endpoint_attr="visits",
        supports_subject_filter=True,
        columns=(
            EntityColumnSpec("Visit ID", ("visit_id", "visitId")),
            EntityColumnSpec("Subject Key", ("subject_key", "subjectKey")),
            EntityColumnSpec("Interval", ("interval_name", "intervalName")),
            EntityColumnSpec("Start Date", ("start_date", "startDate")),
            EntityColumnSpec("Deleted", ("deleted",)),
        ),
    ),
    "queries": EntityViewSpec(
        key="queries",
        title="Queries",
        endpoint_attr="queries",
        supports_subject_filter=True,
        columns=(
            EntityColumnSpec("Annotation ID", ("annotation_id", "annotationId")),
            EntityColumnSpec("Subject Key", ("subject_key", "subjectKey")),
            EntityColumnSpec("Variable", ("variable", "variable_name")),
            EntityColumnSpec("Status", ("status", "type")),
            EntityColumnSpec("Description", ("description",), sensitive=True),
        ),
    ),
    "forms": EntityViewSpec(
        key="forms",
        title="Forms",
        endpoint_attr="forms",
        columns=(
            EntityColumnSpec("Form ID", ("form_id", "formId")),
            EntityColumnSpec("Form Key", ("form_key", "formKey")),
            EntityColumnSpec("Form Name", ("form_name", "formName")),
            EntityColumnSpec("Form Type", ("form_type", "formType")),
            EntityColumnSpec("Disabled", ("disabled",)),
        ),
    ),
    "variables": EntityViewSpec(
        key="variables",
        title="Variables",
        endpoint_attr="variables",
        columns=(
            EntityColumnSpec("Variable ID", ("variable_id", "variableId")),
            EntityColumnSpec("Variable Name", ("variable_name", "variableName")),
            EntityColumnSpec("Form Key", ("form_key", "formKey")),
            EntityColumnSpec("Type", ("variable_type", "variableType")),
            EntityColumnSpec("Label", ("label",)),
        ),
    ),
    "record_revisions": EntityViewSpec(
        key="record_revisions",
        title="Record Revisions",
        endpoint_attr="record_revisions",
        supports_subject_filter=True,
        read_only_notice="21 CFR Part 11 Audit Trail — Strictly Read-Only",
        columns=(
            EntityColumnSpec("Revision ID", ("record_revision_id", "recordRevisionId")),
            EntityColumnSpec("Record ID", ("record_id", "recordId")),
            EntityColumnSpec("Subject Key", ("subject_key", "subjectKey")),
            EntityColumnSpec("Status", ("record_status", "recordStatus")),
            EntityColumnSpec("Author", ("user", "modified_by"), sensitive=True),
            EntityColumnSpec("Reason", ("reason_for_change", "reasonForChange"), sensitive=True),
            EntityColumnSpec("Date Created", ("date_created", "dateCreated")),
        ),
    ),
    "intervals": EntityViewSpec(
        key="intervals",
        title="Intervals",
        endpoint_attr="intervals",
        columns=(
            EntityColumnSpec("Interval ID", ("interval_id", "intervalId")),
            EntityColumnSpec("Interval Name", ("interval_name", "intervalName")),
            EntityColumnSpec("Sequence", ("interval_sequence", "intervalSequence")),
            EntityColumnSpec("Description", ("interval_description", "description")),
            EntityColumnSpec("Disabled", ("disabled",)),
        ),
    ),
    "codings": EntityViewSpec(
        key="codings",
        title="Codings",
        endpoint_attr="codings",
        supports_subject_filter=True,
        columns=(
            EntityColumnSpec("Coding ID", ("coding_id", "codingId")),
            EntityColumnSpec("Record ID", ("record_id", "recordId")),
            EntityColumnSpec("Variable", ("variable",)),
            EntityColumnSpec("Code", ("code",)),
            EntityColumnSpec("Dictionary", ("dictionary_name", "dictionaryName")),
        ),
    ),
    "users": EntityViewSpec(
        key="users",
        title="Users",
        endpoint_attr="users",
        columns=(
            EntityColumnSpec("Account ID", ("user_id", "userId")),
            EntityColumnSpec("Login", ("login",), sensitive=True),
            EntityColumnSpec("First Name", ("first_name", "firstName"), sensitive=True),
            EntityColumnSpec("Last Name", ("last_name", "lastName"), sensitive=True),
            EntityColumnSpec("Active", ("active",)),
        ),
    ),
}


async def collect_endpoint_items(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> list[Any]:
    """Collect items from a synchronous, coroutine, or async-iterable endpoint method.

    Args:
        func: SDK endpoint callable to invoke.
        *args: Positional arguments forwarded to ``func``.
        **kwargs: Keyword arguments forwarded to ``func``.

    Returns:
        Materialized list of items yielded or returned by ``func``.
    """
    if inspect.iscoroutinefunction(func):
        result = await func(*args, **kwargs)
    elif inspect.isasyncgenfunction(func):
        return [item async for item in func(*args, **kwargs)]
    else:

        def _invoke_sync() -> Any:
            res = func(*args, **kwargs)
            if inspect.isawaitable(res) or isinstance(res, AsyncIterable):
                return res
            return list(res)

        result = await asyncio.to_thread(_invoke_sync)

    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, AsyncIterable):
        return [item async for item in result]
    return list(result)


async def _fetch_endpoint_items(endpoint: Any, study_key: str) -> list[Any]:
    """Fetch items from a sync or async SDK endpoint without blocking the UI loop."""
    if endpoint is None:
        return []
    if hasattr(endpoint, "async_list") and callable(endpoint.async_list):
        return await collect_endpoint_items(endpoint.async_list, study_key)
    if hasattr(endpoint, "list") and callable(endpoint.list):
        return await collect_endpoint_items(endpoint.list, study_key)
    return []


class _PrefetchedDataDictAdapter:
    """Sync adapter exposing pre-fetched forms and variables to ``load_from_api``."""

    class _StaticListEndpoint:
        def __init__(self, items: list[Any]) -> None:
            self._items = items

        def list(self, **_kwargs: Any) -> list[Any]:
            return self._items

    def __init__(self, forms_items: list[Any], vars_items: list[Any]) -> None:
        self.forms = self._StaticListEndpoint(forms_items)
        self.variables = self._StaticListEndpoint(vars_items)


class EntityTablePane(Container):
    """Keyboard-filterable table pane backed by an iMednet study endpoint."""

    class SubjectScopeCleared(Message):
        """Emitted when the user clears the active Subject filter."""

    def __init__(
        self,
        sdk: ImednetSDK | AsyncImednetSDK,
        spec: EntityViewSpec,
        *,
        phi_masked: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize the entity table pane."""
        super().__init__(**kwargs)
        self.sdk = sdk
        self.spec = spec
        self.phi_masked = phi_masked
        self.current_study_key: str | None = None
        self.subject_filter: str | None = None
        self.filter_query: str = ""
        self._raw_items: list[Any] = []
        self._filtered_items: list[Any] = []

    @property
    def is_read_only(self) -> bool:
        """Return True when this entity view is a read-only audit trail."""
        return self.spec.read_only_notice is not None

    @property
    def table_id(self) -> str:
        """DOM identifier of the inner DataTable."""
        return f"{self.spec.key}_table"

    @property
    def filter_input_id(self) -> str:
        """DOM identifier of the keyboard filter Input."""
        return f"{self.spec.key}_filter_input"

    def compose(self) -> ComposeResult:
        """Compose toolbar, data table, and detail preview banner."""
        with Horizontal(classes="pane-toolbar"):
            yield Input(
                placeholder=f"Filter {self.spec.title} (press / to focus)...",
                id=self.filter_input_id,
                classes="filter-input",
            )
            if self.spec.supports_subject_filter:
                yield Button(
                    "All Subjects",
                    id=f"{self.spec.key}_clear_subject_btn",
                    variant="default",
                )
            if self.spec.read_only_notice:
                yield Label(
                    self.spec.read_only_notice,
                    id=f"{self.spec.key}_readonly_label",
                    classes="readonly-badge",
                )
        table: DataTable[str] = DataTable(id=self.table_id)
        table.cursor_type = "row"
        yield table
        yield Static(
            f"{self.spec.title}: Select a study to load records.",
            id=f"{self.spec.key}_detail",
            classes="detail-banner",
        )

    def on_mount(self) -> None:
        """Initialize DataTable columns when mounted."""
        table = self.query_one(f"#{self.table_id}", DataTable)
        if not table.columns:
            table.add_columns(*(col.header for col in self.spec.columns))

    def focus_filter(self) -> None:
        """Move keyboard focus to this pane's filter input."""
        self.query_one(f"#{self.filter_input_id}", Input).focus()

    def set_phi_masked(self, enabled: bool) -> None:
        """Toggle clinical PHI masking and re-render rows immediately."""
        self.phi_masked = enabled
        if self.is_mounted:
            self._render_rows()

    def set_subject_filter(self, subject_key: str | None) -> None:
        """Filter displayed items to a single Subject key (or None for Study-wide)."""
        if not self.spec.supports_subject_filter:
            return
        self.subject_filter = subject_key
        if self.is_mounted:
            self._render_rows()

    async def load_study(
        self,
        study_key: str,
        *,
        subject_key: str | None = None,
    ) -> None:
        """Fetch items for the given study and populate the table."""
        self.current_study_key = study_key
        if self.spec.supports_subject_filter:
            self.subject_filter = subject_key
        self.loading = True
        try:
            endpoint = getattr(self.sdk, self.spec.endpoint_attr, None)
            self._raw_items = await _fetch_endpoint_items(endpoint, study_key)
            if self.is_mounted:
                self._render_rows()
        except Exception as exc:
            self._raw_items = []
            if self.is_mounted:
                detail = self.query_one(f"#{self.spec.key}_detail", Static)
                detail.update(f"Error loading {self.spec.title}: {exc}")
        finally:
            self.loading = False

    def _item_matches_subject(self, item: Any) -> bool:
        """Check whether item belongs to the currently selected Subject."""
        if not self.subject_filter or not self.spec.supports_subject_filter:
            return True
        subj_val = _extract_attr(item, "subject_key", "subjectKey", "subject_id", "subjectId")
        if subj_val is None:
            return False
        return str(subj_val) == str(self.subject_filter)

    def _row_for_item(self, item: Any) -> tuple[str, ...]:
        """Build a tuple of formatted cell strings for an item."""
        cells: list[str] = []
        for col in self.spec.columns:
            raw_val = _extract_attr(item, *col.attr_names)
            cells.append(
                _stringify_cell(raw_val, phi_masked=self.phi_masked, sensitive=col.sensitive)
            )
        return tuple(cells)

    def _render_rows(self) -> None:
        """Apply subject and keyboard text filters and populate DataTable rows."""
        table = self.query_one(f"#{self.table_id}", DataTable)
        if not table.columns:
            table.add_columns(*(col.header for col in self.spec.columns))
        table.clear()

        needle = self.filter_query.strip().lower()
        matched_items: list[Any] = []
        rows: list[tuple[str, ...]] = []

        for item in self._raw_items:
            if not self._item_matches_subject(item):
                continue
            row = self._row_for_item(item)
            if needle and not any(needle in cell.lower() for cell in row):
                continue
            matched_items.append(item)
            rows.append(row)

        self._filtered_items = matched_items
        if rows:
            table.add_rows(rows)

        scope_text = f"Subject '{self.subject_filter}'" if self.subject_filter else "All Study"
        mask_text = " (PHI Masked)" if self.phi_masked else ""
        ro_text = f" ({self.spec.read_only_notice})" if self.spec.read_only_notice else ""
        detail = self.query_one(f"#{self.spec.key}_detail", Static)
        detail.update(
            f"{self.spec.title} ({len(rows)}/{len(self._raw_items)}) — Scope: {scope_text}"
            f"{mask_text}{ro_text}"
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter table rows live as the user types in the filter input."""
        if event.input.id == self.filter_input_id:
            self.filter_query = event.value
            self._render_rows()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Clear subject scoping when 'All Subjects' button is pressed."""
        if event.button.id == f"{self.spec.key}_clear_subject_btn":
            self.set_subject_filter(None)
            self.post_message(self.SubjectScopeCleared())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Render a read-only detail preview of the selected row."""
        if event.data_table.id != self.table_id:
            return
        idx = event.cursor_row
        if 0 <= idx < len(self._filtered_items):
            selected = self._filtered_items[idx]
            payload = (
                selected.model_dump()
                if hasattr(selected, "model_dump")
                else dict(selected)
                if isinstance(selected, dict)
                else {"item": str(selected)}
            )
            if self.phi_masked:
                payload = mask_clinical_phi(payload)
            preview = json.dumps(payload, default=str, sort_keys=True)
            if len(preview) > 220:
                preview = f"{preview[:217]}..."
            ro_prefix = "(READ-ONLY) " if self.is_read_only else ""
            detail = self.query_one(f"#{self.spec.key}_detail", Static)
            detail.update(f"{ro_prefix}{self.spec.title} Detail: {preview}")


class DataDictionaryPane(Container):
    """Data Dictionary explorer and schema validator backed by ``imednet.spi.datadict``."""

    def __init__(self, sdk: ImednetSDK | AsyncImednetSDK, **kwargs: Any) -> None:
        """Initialize the Data Dictionary pane."""
        super().__init__(**kwargs)
        self.sdk = sdk
        self.current_study_key: str | None = None
        self.dictionary: DataDictionary | None = None
        self.validation_errors: list[str] = []
        self.filter_query: str = ""

    def compose(self) -> ComposeResult:
        """Compose Data Dictionary controls, validation status, variables table, and rules table."""
        with Vertical():
            with Horizontal(classes="pane-toolbar"):
                yield Input(
                    placeholder="Filter Data Dictionary forms, variables, or rules...",
                    id="datadict_filter_input",
                    classes="filter-input",
                )
                yield Input(
                    placeholder="Optional CSV dir / ODM XML / REDCap CSV path...",
                    id="datadict_source_input",
                    classes="filter-input",
                )
                yield Button("Load Source", id="datadict_load_btn", variant="primary")
                yield Button("Validate Schema", id="datadict_validate_btn", variant="default")
            yield Static(
                "Data Dictionary: Select a study or provide a file/folder source.",
                id="datadict_status",
                classes="detail-banner",
            )
            with TabbedContent(id="datadict_subtabs"):
                with TabPane("Forms & Variables", id="datadict-vars-tab"):
                    vars_table: DataTable[str] = DataTable(id="datadict_vars_table")
                    vars_table.cursor_type = "row"
                    yield vars_table
                with TabPane("Business Logic Rules", id="datadict-rules-tab"):
                    rules_table: DataTable[str] = DataTable(id="datadict_rules_table")
                    rules_table.cursor_type = "row"
                    yield rules_table

    def on_mount(self) -> None:
        """Initialize columns for Data Dictionary tables."""
        vars_table = self.query_one("#datadict_vars_table", DataTable)
        if not vars_table.columns:
            vars_table.add_columns(
                "Form Key",
                "Form Name",
                "Variable Name",
                "Type",
                "Required",
                "Choices",
                "Label",
            )
        rules_table = self.query_one("#datadict_rules_table", DataTable)
        if not rules_table.columns:
            rules_table.add_columns("Rule ID", "Name", "Type", "Status", "Form", "Sequence")

    def focus_filter(self) -> None:
        """Focus the Data Dictionary filter input."""
        self.query_one("#datadict_filter_input", Input).focus()

    async def load_study(self, study_key: str) -> None:
        """Load the study Data Dictionary from the live SDK via ``imednet.spi.datadict``."""
        self.current_study_key = study_key
        self.loading = True
        try:
            forms_ep = getattr(self.sdk, "forms", None)
            vars_ep = getattr(self.sdk, "variables", None)
            forms_list_fn = getattr(forms_ep, "list", None)
            if inspect.iscoroutinefunction(forms_list_fn) or inspect.isasyncgenfunction(
                forms_list_fn
            ):
                forms_items = await _fetch_endpoint_items(forms_ep, study_key)
                vars_items = await _fetch_endpoint_items(vars_ep, study_key)
                adapter = _PrefetchedDataDictAdapter(forms_items, vars_items)
                dd = await asyncio.to_thread(load_from_api, adapter, study_key)
            else:
                dd = await asyncio.to_thread(load_from_api, self.sdk, study_key)
            errors = await asyncio.to_thread(validate, dd, get_schema())
            self.dictionary = dd
            self.validation_errors = errors
            if self.is_mounted:
                self._render_dictionary()
        except Exception as exc:
            self.dictionary = None
            self.validation_errors = [str(exc)]
            if self.is_mounted:
                self.query_one("#datadict_status", Static).update(
                    f"Data Dictionary load error for '{study_key}': {exc}"
                )
        finally:
            self.loading = False

    async def load_from_source_path(self, raw_path: str) -> None:
        """Load Data Dictionary from CSV folder, CDISC ODM XML, or REDCap CSV path."""
        cleaned = raw_path.strip()
        if not cleaned:
            if self.current_study_key:
                await self.load_study(self.current_study_key)
            return

        target = Path(cleaned).expanduser()
        self.loading = True
        try:
            loader: Callable[[Path], DataDictionary]
            if target.is_dir():
                loader = load_from_csv
            elif target.suffix.lower() == ".xml":
                loader = load_from_cdisc_odm
            else:
                loader = load_from_redcap
            dd = await asyncio.to_thread(loader, target)
            errors = await asyncio.to_thread(validate, dd, get_schema())
            self.dictionary = dd
            self.validation_errors = errors
            if self.is_mounted:
                self._render_dictionary()
        except Exception as exc:
            self.validation_errors = [str(exc)]
            if self.is_mounted:
                self.query_one("#datadict_status", Static).update(
                    f"Failed to load Data Dictionary from '{cleaned}': {exc}"
                )
        finally:
            self.loading = False

    def _render_dictionary(self) -> None:
        """Populate variables and business logic tables with active filter applied."""
        vars_table = self.query_one("#datadict_vars_table", DataTable)
        rules_table = self.query_one("#datadict_rules_table", DataTable)
        vars_table.clear()
        rules_table.clear()

        dd = self.dictionary
        if dd is None:
            return

        needle = self.filter_query.strip().lower()
        var_rows: list[tuple[str, ...]] = []
        for form in dd.forms:
            if not form.variables:
                row = (
                    form.formKey,
                    form.formName or "-",
                    "-",
                    form.formType or "-",
                    "-",
                    "0",
                    "(No variables)",
                )
                if not needle or any(needle in cell.lower() for cell in row):
                    var_rows.append(row)
                continue
            for var in form.variables:
                row = (
                    form.formKey,
                    form.formName or "-",
                    var.variableName,
                    var.variableType or "-",
                    var.required or "-",
                    str(len(var.choices)),
                    var.label or "-",
                )
                if not needle or any(needle in cell.lower() for cell in row):
                    var_rows.append(row)

        rule_rows: list[tuple[str, ...]] = []
        for rule in dd.business_logic:
            r_row = (
                str(getattr(rule, "id", None) or "-"),
                str(getattr(rule, "name", None) or "-"),
                str(getattr(rule, "type", None) or "-"),
                str(getattr(rule, "status", None) or "-"),
                str(getattr(rule, "form", None) or "-"),
                str(getattr(rule, "sequence", None) or "-"),
            )
            if not needle or any(needle in cell.lower() for cell in r_row):
                rule_rows.append(r_row)

        if var_rows:
            vars_table.add_rows(var_rows)
        if rule_rows:
            rules_table.add_rows(rule_rows)

        warnings = dd.metadata.validationWarnings
        valid_text = (
            "VALID"
            if not self.validation_errors
            else f"INVALID ({len(self.validation_errors)} error(s))"
        )
        warn_suffix = f" | Warnings: {len(warnings)}" if warnings else ""
        err_suffix = (
            f" | First error: {self.validation_errors[0]}" if self.validation_errors else ""
        )
        self.query_one("#datadict_status", Static).update(
            f"Data Dictionary ({valid_text}) — Forms: {len(dd.forms)}, "
            f"Variables: {len(dd.questions)}, Rules: {len(dd.business_logic)}"
            f"{warn_suffix}{err_suffix}"
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter Data Dictionary tables live."""
        if event.input.id == "datadict_filter_input":
            self.filter_query = event.value
            self._render_dictionary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle source loading and explicit schema validation actions."""
        if event.button.id == "datadict_load_btn":
            source_val = self.query_one("#datadict_source_input", Input).value
            self.run_worker(self.load_from_source_path(source_val))
        elif event.button.id == "datadict_validate_btn" and self.dictionary is not None:
            self.validation_errors = validate(self.dictionary, get_schema())
            self._render_dictionary()


__all__ = [
    "ENTITY_VIEW_SPECS",
    "DataDictionaryPane",
    "EntityColumnSpec",
    "EntityTablePane",
    "EntityViewSpec",
    "collect_endpoint_items",
]
