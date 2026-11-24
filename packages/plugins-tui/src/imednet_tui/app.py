"""Terminal user interface application for iMednet."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import logging
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar, Self, cast
from unittest.mock import NonCallableMock

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Container, VerticalScroll
from textual.events import Resize
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

from imednet.spi.constants import DEFAULT_BASE_URL, MAX_PAGE_SIZE
from imednet.spi.errors import NotFoundError
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet.spi.models import JobState, Subject, is_valid_job_transition
from imednet.spi.utils import (
    InvalidJobStateError,
    InvalidJobTransitionError,
    JobFailedError,
    PathTraversalValidationError,
    SensitivityRegistry,
    evaluate_job_state,
    global_sensitivity_registry,
    resolve_study_configuration,
    sanitize_diagnostic_payload,
    sanitize_diagnostic_text,
    sdk_governance_context,
    validate_path_segment,
)

from .terminal import is_interactive_terminal

__all__ = [
    "DEFAULT_FETCH_LIMIT",
    "DashboardScreen",
    "ImednetTuiApp",
    "JobDetailPanel",
    "JobMonitor",
    "JobTable",
    "LogViewer",
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


class _StudyPhiRegistryCache:
    """Session-scoped cache resolving ``StudyConfiguration.phi_fields`` per study."""

    def __init__(self, sdk: object | None = None) -> None:
        """Initialize the per-study sensitivity registry cache."""
        self.sdk = sdk
        self._phi_fields_by_study: dict[str, tuple[str, ...]] = {}
        self._registry_cache: dict[
            tuple[str, frozenset[str], frozenset[str]],
            SensitivityRegistry,
        ] = {}

    def clear(self) -> None:
        """Invalidate cached per-study ``phi_fields`` and isolated registries."""
        self._phi_fields_by_study.clear()
        self._registry_cache.clear()

    def _resolve_phi_fields(self, study_key: str) -> tuple[str, ...]:
        """Resolve committed ``StudyConfiguration.phi_fields`` once per study key."""
        cached = self._phi_fields_by_study.get(study_key)
        if cached is not None:
            return cached
        fields: tuple[str, ...] = ()
        try:
            if self.sdk is not None:
                with sdk_governance_context(self.sdk):
                    cfg = resolve_study_configuration(study_key)
            else:
                cfg = resolve_study_configuration(study_key)
            raw_fields = getattr(cfg, "phi_fields", None) if cfg is not None else None
            if raw_fields:
                fields = tuple(str(field_name) for field_name in raw_fields if field_name)
        except Exception:
            fields = ()
        self._phi_fields_by_study[study_key] = fields
        return fields

    def registry_for(self, study_key: str | None) -> SensitivityRegistry:
        """Return an isolated ``SensitivityRegistry`` for ``study_key`` without global mutation."""
        cleaned_study = (
            study_key.strip() if isinstance(study_key, str) and study_key.strip() else None
        )
        if cleaned_study is None:
            return global_sensitivity_registry
        phi_fields = self._resolve_phi_fields(cleaned_study)
        if not phi_fields:
            return global_sensitivity_registry
        sens_snap, exempt_snap = global_sensitivity_registry.snapshot()
        cache_key = (cleaned_study, sens_snap, exempt_snap)
        existing = self._registry_cache.get(cache_key)
        if existing is not None:
            return existing
        reg = SensitivityRegistry()
        for key in sens_snap:
            reg.add_sensitive_key(key)
        for key in exempt_snap:
            reg.add_exempt_key(key)
        for custom_key in phi_fields:
            if not reg.is_exempt(custom_key):
                reg.add_sensitive_key(custom_key)
        self._registry_cache[cache_key] = reg
        return reg


def _sanitize_structured_payload(
    value: Any,
    *,
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Apply clinical PHI masking and credential redaction via core diagnostic sanitizer."""
    return sanitize_diagnostic_payload(value, registry=registry)


def _sanitize_text(
    value: Any,
    *,
    registry: SensitivityRegistry | None = None,
) -> str:
    """Redact credentials, URL query filters, JSON, and clinical PHI via core sanitizer."""
    return sanitize_diagnostic_text(value, registry=registry)


def _get_configured_attr(obj: object, name: str) -> Any:
    """Read ``name`` from ``obj`` without auto-creating unconfigured mock attributes."""
    if obj is None:
        return None
    if isinstance(obj, NonCallableMock):
        return vars(obj).get(name)
    return getattr(obj, name, None)


def _resolve_env_subtitle(sdk: object) -> str:
    """Resolve the active environment subtitle from simulation mode, base_url, or default."""
    config = _get_configured_attr(sdk, "config")
    clients = [_get_configured_attr(sdk, attr) for attr in ("client", "async_client")]
    if (
        _get_configured_attr(config, "simulation_mode") is True
        or _get_configured_attr(sdk, "simulation_mode") is True
        or any(_get_configured_attr(client, "simulation_mode") is True for client in clients)
    ):
        return "Env: Simulation"

    config_url = _get_configured_attr(config, "base_url")
    if isinstance(config_url, str) and config_url.strip():
        return f"Env: {_sanitize_text(config_url.strip())}"

    for client in clients:
        client_url = _get_configured_attr(client, "base_url")
        if isinstance(client_url, str) and client_url.strip():
            return f"Env: {_sanitize_text(client_url.strip())}"

    return f"Env: {DEFAULT_BASE_URL}"


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


def _clean_scalar(value: object) -> str | None:
    """Convert scalar SDK/mock values to non-empty strings while preserving ``0``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, Enum):
        text = str(value.value)
        return text if text.strip() else None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return str(value)
    return None


def _extract_subject_status(sub: object) -> str:
    """Resolve effective subject status across SDK ``Subject`` instances and test mocks."""
    if isinstance(sub, Subject):
        raw_effective = sub.effective_status
        if isinstance(raw_effective, Enum):
            text = str(raw_effective.value)
            return text if text.strip() else "-"
        if isinstance(raw_effective, str) and raw_effective.strip():
            return raw_effective
        return "-"
    if getattr(sub, "deleted", None) is True:
        return "DELETED"
    for attr in ("effective_status", "subject_status", "status"):
        candidate = getattr(sub, attr, None)
        if isinstance(candidate, Enum):
            text = str(candidate.value)
            if text.strip():
                return text
        elif isinstance(candidate, str) and candidate.strip():
            return candidate
    return "-"


def _format_subject_row(sub: object) -> tuple[Text, Text, Text, Text]:
    """Format a subject record into literal Rich ``Text`` cells for ``SubjectTable``."""
    sub_id = (
        _clean_scalar(getattr(sub, "subject_id", None))
        or _clean_scalar(getattr(sub, "subject_key", None))
        or "-"
    )
    raw_site_id = getattr(sub, "site_id", None)
    cleaned_site_id = _clean_scalar(raw_site_id) if raw_site_id is not None else None
    site_id_str = cleaned_site_id if cleaned_site_id is not None else "-"
    status_str = _extract_subject_status(sub)
    created_str = _clean_scalar(getattr(sub, "date_created", None)) or "-"
    return (Text(sub_id), Text(site_id_str), Text(status_str), Text(created_str))


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

    def __init__(
        self,
        log_widget: Log,
        *,
        registry_resolver: Callable[[], SensitivityRegistry | None] | None = None,
    ) -> None:
        """Initialize handler with target Log widget.

        Args:
            log_widget: The Textual Log widget to write to.
            registry_resolver: Optional callable returning the active study registry.
        """
        super().__init__()
        self.log_widget = log_widget
        self._registry_resolver = registry_resolver

    def _active_registry(self) -> SensitivityRegistry | None:
        """Resolve the active SensitivityRegistry for the target log widget."""
        if self._registry_resolver is not None:
            return self._registry_resolver()
        resolver = getattr(self.log_widget, "active_registry", None)
        if callable(resolver):
            return cast("SensitivityRegistry | None", resolver())
        return None

    def emit(self, record: logging.LogRecord) -> None:
        """Format, redact, mask clinical PHI, and write log record to widget.

        Args:
            record: The logging LogRecord to emit.
        """
        try:
            reg = self._active_registry()
            sanitized_record = copy.copy(record)
            if isinstance(record.msg, (dict, list, tuple)):
                sanitized_record.msg = _sanitize_structured_payload(record.msg, registry=reg)
            if isinstance(record.args, dict):
                sanitized_record.args = _sanitize_structured_payload(record.args, registry=reg)
            elif isinstance(record.args, tuple):
                sanitized_record.args = tuple(
                    _sanitize_structured_payload(arg, registry=reg)
                    if isinstance(arg, (dict, list, tuple))
                    else arg
                    for arg in record.args
                )
            msg = _sanitize_text(self.format(sanitized_record), registry=reg)
            if isinstance(self.log_widget, LogViewer):
                Log.write_lines(self.log_widget, [msg])
            else:
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
        self._request_id = 0

    def sync_selected_study(self, study_key: str | None) -> None:
        """Synchronize highlighted study index with ``study_key`` when loaded."""
        if not self.loaded or not self.is_attached or not study_key:
            return
        target_index: int | None = None
        for idx, (_, (key, _)) in enumerate(self._study_items.items()):
            if key == study_key:
                target_index = idx
                break
        self._initial_index = target_index
        self.index = target_index

    async def on_mount(self) -> None:
        """Load studies automatically upon mounting the widget."""
        await self.load_studies()

    async def load_studies(self, *, load_all: bool = False) -> None:
        """Fetch and populate study items from the SDK facade asynchronously.

        Args:
            load_all: When True, fetch all pages instead of the initial bounded batch.
        """
        self._request_id += 1
        request_id = self._request_id
        was_loaded = self.loaded
        prev_index = self.index if was_loaded else None
        self.loading = True
        limit = None if load_all else DEFAULT_FETCH_LIMIT
        try:
            studies, truncated = await _fetch_bounded_facade_items(
                self.sdk,
                "async_get_studies",
                "get_studies",
                limit=limit,
            )
            if request_id != self._request_id:
                return

            used_ids: set[str] = set()
            items: list[ListItem] = []
            study_map: dict[str, tuple[str, str]] = {}
            for study in studies:
                name = (
                    _clean_scalar(getattr(study, "study_name", None))
                    or _clean_scalar(getattr(study, "name", None))
                    or "Unknown Study"
                )
                key = _clean_scalar(getattr(study, "study_key", None)) or "no-key"
                item_id = _sanitize_dom_id("study", key, used_ids)
                study_map[item_id] = (key, name)
                items.append(ListItem(Label(f"{name} ({key})", markup=False), id=item_id))

            self._study_items = study_map
            if self.is_attached:
                if self.children:
                    await self.clear()
                if request_id != self._request_id:
                    return
                target_index: int | None = None
                if items:
                    active_study_key: str | None = None
                    with contextlib.suppress(Exception):
                        screen_obj: Any = self.screen
                        if isinstance(screen_obj, DashboardScreen):
                            active_study_key = screen_obj.selected_study_key
                    match_key = active_study_key or self.preferred_study_key
                    if match_key:
                        for idx, (_, (key, _)) in enumerate(study_map.items()):
                            if key == match_key:
                                target_index = idx
                                break
                    elif prev_index is not None:
                        target_index = min(prev_index, len(items) - 1)
                    else:
                        target_index = 0
                self._initial_index = target_index
                await self.extend(items)
                if request_id != self._request_id:
                    return
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
            if request_id == self._request_id and hasattr(self.app, "notify"):
                self.app.notify(
                    f"Error loading studies: {_sanitize_text(e)}",
                    severity="error",
                    markup=False,
                )
        finally:
            if request_id == self._request_id:
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
        self._request_id = 0

    async def load_sites(self, study_key: str, *, load_all: bool = False) -> None:
        """Fetch and populate site items for a given study key.

        Args:
            study_key: Study key to load sites for.
            load_all: When True, fetch all pages instead of the initial bounded batch.
        """
        self._request_id += 1
        request_id = self._request_id
        same_study = self.current_study_key == study_key
        prev_index = self.index if same_study else None
        self.current_study_key = study_key
        self.loading = True
        limit = None if load_all else DEFAULT_FETCH_LIMIT
        try:
            if not same_study:
                if self.children:
                    await self.clear()
                if request_id != self._request_id or self.current_study_key != study_key:
                    return
                self._site_items.clear()

            sites, truncated = await _fetch_bounded_facade_items(
                self.sdk,
                "async_get_sites",
                "get_sites",
                study_key,
                limit=limit,
            )
            if request_id != self._request_id or self.current_study_key != study_key:
                return

            used_ids: set[str] = set()
            items: list[ListItem] = []
            site_map: dict[str, tuple[str, str]] = {}
            for site in sites:
                raw_site_id = getattr(site, "site_id", None)
                cleaned_site_id = _clean_scalar(raw_site_id) if raw_site_id is not None else None
                site_id = cleaned_site_id if cleaned_site_id is not None else "no-id"
                name = _clean_scalar(getattr(site, "site_name", None)) or "Unknown Site"
                item_id = _sanitize_dom_id("site", site_id, used_ids)
                site_map[item_id] = (site_id, name)
                items.append(ListItem(Label(f"{name} ({site_id})", markup=False), id=item_id))

            self._site_items = site_map
            if self.is_attached:
                if self.children:
                    await self.clear()
                if request_id != self._request_id or self.current_study_key != study_key:
                    return
                target_index = (
                    min(prev_index, len(items) - 1)
                    if items and prev_index is not None
                    else (0 if items else None)
                )
                self._initial_index = target_index
                await self.extend(items)
                if request_id != self._request_id or self.current_study_key != study_key:
                    return
                if items:
                    self.index = target_index
            self.truncated = truncated
            self.status_notice = (
                f"Showing first {len(items)} sites (truncated — press 'a' to load all)"
                if truncated
                else ""
            )
            _sync_widget_truncation(self, len(items), truncated)
        except Exception as e:
            if (
                request_id == self._request_id
                and self.current_study_key == study_key
                and hasattr(self.app, "notify")
            ):
                self.app.notify(
                    f"Error loading sites: {_sanitize_text(e)}",
                    severity="error",
                    markup=False,
                )
        finally:
            if request_id == self._request_id:
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


class SubjectTable(DataTable[Text]):
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
        self._request_id = 0

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
        self._request_id += 1
        request_id = self._request_id
        same_scope = self.current_study_key == study_key and self.current_site_id == site_id
        self.current_study_key = study_key
        self.current_site_id = site_id
        if not same_scope and self.is_mounted:
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
            if (
                request_id != self._request_id
                or self.current_study_key != study_key
                or self.current_site_id != site_id
            ):
                return

            rows = [_format_subject_row(sub) for sub in subjects]
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
            if (
                request_id == self._request_id
                and self.current_study_key == study_key
                and self.current_site_id == site_id
                and hasattr(self.app, "notify")
            ):
                self.app.notify(
                    f"Error loading subjects: {_sanitize_text(e)}",
                    severity="error",
                    markup=False,
                )
        finally:
            if request_id == self._request_id:
                self.loading = False


class _TrackedJobPlaceholder:
    """Representation for tracked jobs whose status could not be retrieved."""

    def __init__(
        self,
        job_id: str,
        status: str = "FAILED",
        *,
        is_terminal: bool = False,
        non_retryable: bool = False,
    ) -> None:
        """Initialize placeholder with job ID, status, and terminal flag.

        Args:
            job_id: Identifier of the tracked job.
            status: Status display string, such as FAILED, NOT_FOUND, INVALID, or UNKNOWN.
            is_terminal: Whether background polling should treat this placeholder as terminal.
            non_retryable: Whether manual refreshes should skip re-polling this placeholder.
        """
        self.job_id = job_id
        self.batch_id = job_id
        self.job_type = "Job"
        self.state = status
        self.status = status
        self.job_status = status
        self.date_created = ""
        self.is_terminal = is_terminal
        self.non_retryable = non_retryable


@dataclass
class _TrackedJobEntry:
    """Per-study tracked job state for monotonic evaluation and terminal freezing."""

    job_id: str
    last_job: Any = None
    last_state: JobState | str | None = None
    is_terminal: bool = False
    warning: str | None = None
    record_errors: list[dict[str, Any]] | None = None


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


def _resolve_entry_state_color(entry: _TrackedJobEntry, state_str: str) -> str:
    """Resolve Rich color name for a tracked job entry's state."""
    job = entry.last_job
    if job is None:
        return "yellow"
    upper_status = state_str.upper()
    raw_failed = getattr(job, "failed_records", getattr(job, "record_failure_count", 0))
    has_failed_records = (
        not isinstance(job, _TrackedJobPlaceholder)
        and isinstance(raw_failed, int)
        and not isinstance(raw_failed, bool)
        and raw_failed > 0
    )
    if (
        getattr(job, "is_failed", False)
        or has_failed_records
        or bool(getattr(job, "result_error", None))
    ):
        return "red"
    if upper_status in _SUCCESS_STATE_NAMES:
        return "green"
    if upper_status in _ACTIVE_STATE_NAMES:
        return "yellow"
    return "red"


def _extract_job_record_error_lines(
    entry: _TrackedJobEntry,
    *,
    registry: SensitivityRegistry | None = None,
) -> list[str]:
    """Extract sanitized per-Record error lines from ``entry.last_job.results`` or ``record_errors``."""
    job = entry.last_job
    results = getattr(job, "results", None) if job is not None else None
    raw_items: list[Any] = []
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            item_status = str(item.get("status") or "").upper()
            has_err = bool(item.get("error") or item.get("errors"))
            if item_status in {"FAIL", "FAILED", "ERROR"} or (not item_status and has_err):
                raw_items.append(item)
    elif isinstance(results, dict):
        nested = results.get("errors")
        if isinstance(nested, list):
            raw_items.extend(nested)
        elif results.get("error"):
            raw_items.append(results)
    if not raw_items and entry.record_errors:
        raw_items.extend(entry.record_errors)

    lines: list[str] = []
    for idx, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            sanitized_item = cast(
                dict[str, Any],
                _sanitize_structured_payload(item, registry=registry),
            )
            ident_parts: list[str] = []
            for id_key, label in (
                ("recordId", "recordId"),
                ("record_id", "record_id"),
                ("subjectKey", "subjectKey"),
                ("subject_key", "subject_key"),
                ("formKey", "formKey"),
                ("form_key", "form_key"),
            ):
                if id_key in sanitized_item and sanitized_item[id_key] not in (None, ""):
                    ident_parts.append(
                        f"{label}={_sanitize_text(sanitized_item[id_key], registry=registry)}"
                    )
            detail_val = (
                sanitized_item.get("error")
                or sanitized_item.get("errors")
                or sanitized_item.get("message")
                or sanitized_item.get("description")
                or sanitized_item
            )
            prefix = f"Record ({', '.join(ident_parts)})" if ident_parts else f"Record #{idx}"
            lines.append(f"- {prefix}: {_sanitize_text(detail_val, registry=registry)}")
        elif item is not None:
            lines.append(f"- Record #{idx}: {_sanitize_text(item, registry=registry)}")
    return lines


def _read_job_metadata_field(
    job: object,
    *names: str,
    registry: SensitivityRegistry | None = None,
) -> str | None:
    """Read and sanitize the first non-empty scalar field from ``job``."""
    for name in names:
        cleaned = _clean_scalar(_get_configured_attr(job, name))
        if cleaned is not None:
            return _sanitize_text(cleaned, registry=registry)
    return None


class JobTable(DataTable[Text]):
    """Keyboard-scrollable table displaying tracked background study jobs."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("enter,space", "toggle_job_detail", "Inspect Job"),
        ("delete,backspace", "untrack_selected_job", "Untrack Job"),
    ]

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

    def allow_focus(self) -> bool:
        """Allow focus only while all ancestor containers and tab panes are displayed."""
        return self.can_focus and all(ancestor.display for ancestor in self.ancestors_with_self)

    def on_mount(self) -> None:
        """Initialize job table columns on mount."""
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Ensure job columns exist before populating rows."""
        if not self.columns:
            self.add_columns(*self.JOB_COLUMNS)

    def set_job_rows(
        self,
        rows: Sequence[Sequence[str | Text]],
        *,
        preserve_cursor: bool = True,
    ) -> None:
        """Replace displayed job rows while optionally preserving cursor position."""
        self._ensure_columns()
        prev_row = self.cursor_row if preserve_cursor else 0
        self.clear()
        if rows:
            self.add_rows(
                [
                    tuple(cell.copy() if isinstance(cell, Text) else Text(cell) for cell in row)
                    for row in rows
                ]
            )
            if preserve_cursor and prev_row > 0:
                self.move_cursor(row=min(prev_row, len(rows) - 1))

    def _selected_batch_id(self) -> tuple[int, str] | None:
        """Return ``(row_idx, batch_id)`` for the currently highlighted job row, if any."""
        if not self.is_mounted or self.row_count <= 0:
            return None
        row_idx = self.cursor_row
        if row_idx < 0 or row_idx >= self.row_count:
            return None
        batch_id = str(self.get_row_at(row_idx)[0]).strip()
        return row_idx, batch_id

    def action_toggle_job_detail(self) -> None:
        """Toggle the inline ``#job_detail_panel`` for the currently highlighted job row."""
        selected = self._selected_batch_id()
        if selected is None:
            return
        row_idx, batch_id = selected
        for ancestor in self.ancestors:
            if isinstance(ancestor, JobMonitor):
                if batch_id and batch_id != "-":
                    ancestor.toggle_job_detail(batch_id)
                elif 0 <= row_idx < len(ancestor.tracked_job_ids):
                    ancestor.toggle_job_detail(ancestor.tracked_job_ids[row_idx])
                break

    def action_untrack_selected_job(self) -> None:
        """Remove the currently highlighted job row from the enclosing JobMonitor."""
        selected = self._selected_batch_id()
        if selected is None:
            return
        row_idx, batch_id = selected
        for ancestor in self.ancestors:
            if isinstance(ancestor, JobMonitor):
                if batch_id and batch_id != "-":
                    ancestor.untrack_job(batch_id)
                elif 0 <= row_idx < len(ancestor.tracked_job_ids):
                    ancestor.untrack_job(ancestor.tracked_job_ids[row_idx])
                break


class JobDetailPanel(VerticalScroll):
    """Focusable, keyboard-scrollable split pane displaying sanitized job metadata and errors."""

    can_focus = True
    can_focus_children = False

    def __init__(
        self,
        content: str = "",
        *,
        markup: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize the scrollable job detail pane."""
        super().__init__(**kwargs)
        self._content_text = content
        self._markup = markup

    def allow_focus(self) -> bool:
        """Allow keyboard focus only when visible inside an active displayed tab pane."""
        return (
            self.can_focus
            and self.has_class("visible")
            and all(ancestor.display for ancestor in self.ancestors_with_self)
        )

    def compose(self) -> ComposeResult:
        """Compose inner Static body inside the scrollable viewport."""
        yield Static(self._content_text, id="job_detail_body", markup=self._markup)

    def update(self, content: str = "") -> None:
        """Update rendered detail text inside the scrollable viewport."""
        self._content_text = content
        if self.is_mounted:
            with contextlib.suppress(Exception):
                self.query_one("#job_detail_body", Static).update(content)

    def render(self) -> Text:
        """Return Rich Text snapshot of detail content for inspection."""
        return Text(self._content_text)


class JobMonitor(Container):
    """Widget registering, polling, and displaying study background jobs."""

    DEFAULT_CSS = """
    JobMonitor {
        layout: vertical;
        height: 1fr;
        background: $surface;
        border: none;
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
        height: 1;
        max-height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        text-wrap: nowrap;
        padding: 0 1;
        color: $text;
    }

    JobMonitor #job_split_pane {
        layout: horizontal;
        width: 1fr;
        height: 1fr;
    }

    JobMonitor #job_table {
        width: 1fr;
        height: 1fr;
    }

    JobMonitor #job_detail_panel {
        display: none;
        width: 1fr;
        height: 1fr;
        overflow-x: auto;
        overflow-y: auto;
        padding: 0 1;
        background: $panel;
        color: $text;
        border-left: solid $primary;
    }

    JobMonitor #job_detail_panel:focus {
        border-left: double $accent;
    }

    JobMonitor #job_detail_panel > #job_detail_body {
        width: 1fr;
        height: auto;
        min-height: 100%;
    }

    JobMonitor #job_detail_panel.visible {
        display: block;
    }
    """

    MAX_CONCURRENT_POLLS: ClassVar[int] = 5
    REFRESH_WORKER_GROUP: ClassVar[str] = "refresh-jobs"

    def __init__(
        self,
        sdk: ImednetFacade | AsyncImednetFacade,
        *,
        max_concurrency: int = MAX_CONCURRENT_POLLS,
        phi_cache: _StudyPhiRegistryCache | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the JobMonitor widget.

        Args:
            sdk: Active iMednet SDK facade instance.
            max_concurrency: Maximum number of concurrent status polling requests.
            phi_cache: Optional shared per-study ``SensitivityRegistry`` cache.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.sdk = sdk
        self.max_concurrency = max_concurrency
        self._phi_cache = phi_cache if phi_cache is not None else _StudyPhiRegistryCache(sdk)
        self._semaphore: asyncio.Semaphore | None = None
        self.timer: Timer | None = None
        self._polling_active = False
        self.current_study_key: str | None = None
        self._study_jobs: dict[str, dict[str, _TrackedJobEntry]] = {}
        self._unassigned_job_ids: list[str] = []
        self._rendered_study_key: str | None = None
        self._inspected_job_id: str | None = None
        self._detail_text: str = ""
        self._summary_renderable = Text("Job Monitor: Select a study to view background jobs.")
        self._summary_text = self._summary_renderable.markup

    def _active_registry(self, study_key: str | None = None) -> SensitivityRegistry:
        """Return the isolated ``SensitivityRegistry`` for ``study_key`` or active study."""
        target = study_key if study_key is not None else self.current_study_key
        return self._phi_cache.registry_for(target)

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
        """Compose inline batch input, status notice, and bounded horizontal split pane."""
        yield Input(
            placeholder="Enter batch ID(s) to track (e.g. BATCH-001) and press Enter...",
            id="batch_id_input",
        )
        yield Static(self._summary_renderable.plain, id="job_monitor_notice", markup=False)
        with Container(id="job_split_pane"):
            yield JobTable(id="job_table")
            yield JobDetailPanel("", id="job_detail_panel", markup=False)

    def _resolve_entry_for_detail(self, job_id: str) -> _TrackedJobEntry | None:
        """Resolve a ``_TrackedJobEntry`` for ``job_id`` in the active study."""
        if self.current_study_key is None:
            return None
        entries = self._study_jobs.get(self.current_study_key, {})
        if job_id in entries:
            return entries[job_id]
        for entry in entries.values():
            if _extract_entry_row(entry)[0] == job_id:
                return entry
        return None

    def _format_job_detail_text(self, job_id: str) -> str | None:
        """Build sanitized inline detail text for ``job_id``, or None if untracked."""
        reg = self._active_registry()
        if self.current_study_key is None:
            if job_id not in self._unassigned_job_ids:
                return None
            safe_id = _sanitize_text(job_id, registry=reg)
            return "\n".join(
                [
                    f"Job Details: {safe_id} [QUEUED]",
                    "jobId: -",
                    f"batchId: {safe_id}",
                    "state: QUEUED",
                    "dateCreated: -",
                    "dateStarted: -",
                    "dateFinished: -",
                    "Status: Queued — select a study to bind and poll this batch.",
                ]
            )

        entry = self._resolve_entry_for_detail(job_id)
        if entry is None:
            return None

        batch_id, state_str, progress_str, total_str, failed_str, _ = _extract_entry_row(entry)
        safe_batch_id = _sanitize_text(batch_id, registry=reg)
        safe_state = _sanitize_text(state_str, registry=reg)
        job = entry.last_job
        is_placeholder = job is None or isinstance(job, _TrackedJobPlaceholder)
        resolved_job_id = (
            "-"
            if is_placeholder
            else (_read_job_metadata_field(job, "job_id", "jobId", registry=reg) or "-")
        )
        resolved_batch_id = (
            safe_batch_id
            if is_placeholder
            else (
                _read_job_metadata_field(job, "batch_id", "batchId", registry=reg) or safe_batch_id
            )
        )
        resolved_created = (
            "-"
            if is_placeholder
            else (_read_job_metadata_field(job, "date_created", "dateCreated", registry=reg) or "-")
        )
        resolved_started = (
            "-"
            if is_placeholder
            else (_read_job_metadata_field(job, "date_started", "dateStarted", registry=reg) or "-")
        )
        resolved_finished = (
            "-"
            if is_placeholder
            else (
                _read_job_metadata_field(job, "date_finished", "dateFinished", registry=reg) or "-"
            )
        )
        resolved_result_url = (
            None
            if is_placeholder
            else _read_job_metadata_field(job, "result_url", "resultUrl", registry=reg)
        )

        lines = [
            f"Job Details: {safe_batch_id} [{safe_state}] "
            f"(progress={progress_str}, total={total_str}, failed={failed_str})",
            f"jobId: {resolved_job_id}",
            f"batchId: {resolved_batch_id}",
            f"state: {safe_state}",
            f"dateCreated: {resolved_created}",
            f"dateStarted: {resolved_started}",
            f"dateFinished: {resolved_finished}",
        ]
        if resolved_result_url:
            lines.append(f"resultUrl: {resolved_result_url}")

        has_diagnostic = False
        raw_res_err = getattr(job, "result_error", None) if job is not None else None
        sanitized_res_err = (
            _sanitize_text(raw_res_err.strip(), registry=reg)
            if isinstance(raw_res_err, str) and raw_res_err.strip()
            else None
        )
        if sanitized_res_err:
            lines.append(f"Result Error: {sanitized_res_err}")
            has_diagnostic = True

        record_error_lines = _extract_job_record_error_lines(entry, registry=reg)
        if record_error_lines:
            lines.append(f"Record Errors ({len(record_error_lines)}):")
            lines.extend(record_error_lines)
            has_diagnostic = True

        if entry.warning:
            sanitized_warn = _sanitize_text(entry.warning, registry=reg)
            if sanitized_warn != sanitized_res_err:
                lines.append(f"Diagnostic: {sanitized_warn}")
                has_diagnostic = True

        if not has_diagnostic:
            upper_state = state_str.upper()
            if upper_state == "NOT_FOUND" or entry.last_state == "NOT_FOUND":
                safe_study = _sanitize_text(self.current_study_key, registry=reg)
                lines.append(
                    f"Diagnostic: Batch '{resolved_batch_id}' was not found in study '{safe_study}'."
                )
            elif upper_state in {"INVALID", "FAILED", "CANCELLED", "TIMEOUT"} or bool(
                getattr(job, "is_failed", False)
            ):
                lines.append(f"Diagnostic: Job entered terminal state {safe_state}.")
            else:
                lines.append("No errors reported.")
        return "\n".join(lines)

    def _sync_job_detail_panel(self, *, reset_scroll: bool = False) -> None:
        """Synchronize ``#job_detail_panel`` content and visibility with ``_inspected_job_id``."""
        if self._inspected_job_id is None:
            self._detail_text = ""
        else:
            rendered = self._format_job_detail_text(self._inspected_job_id)
            if rendered is None:
                self._inspected_job_id = None
                self._detail_text = ""
            else:
                self._detail_text = rendered

        if not self.is_mounted:
            return
        with contextlib.suppress(Exception):
            panel = self.query_one("#job_detail_panel", JobDetailPanel)
            panel.update(self._detail_text)
            panel.set_class(bool(self._inspected_job_id), "visible")
            if reset_scroll:
                panel.scroll_home(animate=False)

    def toggle_job_detail(self, job_id: str) -> bool:
        """Toggle inline ``#job_detail_panel`` for ``job_id``; return True when open."""
        cleaned = job_id.strip()
        if not cleaned:
            return False
        if self._inspected_job_id == cleaned:
            self._inspected_job_id = None
            self._sync_job_detail_panel(reset_scroll=True)
            return False
        self._inspected_job_id = cleaned
        self._sync_job_detail_panel(reset_scroll=True)
        return self._inspected_job_id is not None

    def close_job_detail(self) -> bool:
        """Close ``#job_detail_panel`` if open; return True when a panel was closed."""
        if self._inspected_job_id is None:
            return False
        self._inspected_job_id = None
        self._sync_job_detail_panel(reset_scroll=True)
        return True

    def update(self, content: str | Text = "") -> None:
        """Store summary text or renderable for non-composited inspection and testing."""
        if isinstance(content, Text):
            self._summary_renderable = content.copy()
            self._summary_text = content.markup
        else:
            self._summary_text = content
            self._summary_renderable = Text(content)

    def render(self) -> Text:
        """Return summary text representation for non-composited inspection."""
        return self._summary_renderable.copy()

    def _update_ui(
        self,
        *,
        summary: Text | str,
        notice_text: str | None = None,
        rows: Sequence[Sequence[str | Text]] = (),
        preserve_cursor: bool = True,
    ) -> None:
        """Synchronize summary renderable, notice banner, and table rows when mounted."""
        summary_renderable = summary.copy() if isinstance(summary, Text) else Text(summary)
        self.update(summary_renderable.markup if isinstance(summary, Text) else summary)
        self._summary_renderable = summary_renderable
        if not self.is_mounted:
            return
        with contextlib.suppress(Exception):
            notice = self.query_one("#job_monitor_notice", Static)
            notice.update(notice_text if notice_text is not None else summary_renderable.plain)
        with contextlib.suppress(Exception):
            table = self.query_one("#job_table", JobTable)
            table.set_job_rows(rows, preserve_cursor=preserve_cursor)
        self._sync_job_detail_panel()

    def _render_unassigned_jobs(self) -> None:
        """Render queued CLI batch IDs prior to study selection without starting polling."""
        self._pause_timer()
        self._rendered_study_key = None
        if not self._unassigned_job_ids:
            self._update_ui(
                summary=Text("Job Monitor: Select a study to view background jobs."),
                notice_text=(
                    "Select a study to monitor jobs. Upstream iMednet APIs require batch IDs; "
                    "enter batch ID(s) above once a study is active."
                ),
                rows=(),
                preserve_cursor=False,
            )
            return

        rows: list[tuple[Text, Text, Text, Text, Text, Text]] = []
        summary_parts: list[str | Text | tuple[str, str]] = [
            (f"Queued Jobs ({len(self._unassigned_job_ids)})", "bold")
        ]
        sanitized_ids: list[str] = []
        for jid in self._unassigned_job_ids:
            safe_id = _sanitize_text(jid)
            sanitized_ids.append(safe_id)
            rows.append(
                (
                    Text(safe_id),
                    Text("QUEUED", style="yellow"),
                    Text("-"),
                    Text("-"),
                    Text("-"),
                    Text("-"),
                )
            )
            summary_parts.extend(["\n", ("QUEUED", "yellow"), f" - Job ({safe_id})"])
        notice_text = (
            f"Queued Jobs ({len(rows)}) — select a study to start polling: "
            f"{', '.join(sanitized_ids)}"
        )
        self._update_ui(
            summary=Text.assemble(*summary_parts),
            notice_text=notice_text,
            rows=rows,
            preserve_cursor=True,
        )

    def _render_empty_study_jobs(self, study_key: str) -> None:
        """Pause background polling and display the empty-study batch prompt."""
        self._pause_timer()
        self._rendered_study_key = study_key
        empty_msg = (
            f"Job Monitor: Background job enumeration is not supported by the iMednet Jobs API "
            f"for study '{study_key}' (upstream APIs require batch IDs). "
            "Enter one or more batch IDs above and press Enter to track jobs."
        )
        self._update_ui(
            summary=Text(empty_msg),
            notice_text=empty_msg,
            rows=(),
            preserve_cursor=False,
        )

    def _on_poll_timer(self) -> None:
        """Trigger an exclusive background refresh on interval timer ticks."""
        self.schedule_refresh()

    def schedule_refresh(self, *, retry_placeholders: bool = False) -> None:
        """Schedule job polling through a single exclusive worker group when mounted."""
        if not self.is_mounted:
            return

        async def _runner() -> None:
            await self.refresh_jobs(retry_placeholders=retry_placeholders)

        self.run_worker(
            cast(Any, _runner),
            exclusive=True,
            group=self.REFRESH_WORKER_GROUP,
        )

    def on_mount(self) -> None:
        """Set up status message and interval polling timer on mount."""
        self.timer = self.set_interval(10.0, self._on_poll_timer, pause=True)
        self._polling_active = False
        if self.current_study_key:
            self._sync_timer_for_study(self.current_study_key)
            self.schedule_refresh()
        else:
            self._render_unassigned_jobs()

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
                    and not existing.last_job.non_retryable
                ):
                    existing.last_job = None
                    existing.is_terminal = False
                    existing.warning = None
                    if not isinstance(existing.last_state, JobState):
                        existing.last_state = None
        else:
            for jid in parsed_ids:
                if jid not in self._unassigned_job_ids:
                    self._unassigned_job_ids.append(jid)
            if self.current_study_key is None:
                self._render_unassigned_jobs()

        if refresh and target_study and target_study == self.current_study_key:
            self._sync_timer_for_study(target_study)
            self.schedule_refresh()
        return parsed_ids

    def track_job(self, job_id: str, study_key: str | None = None) -> None:
        """Register a known job ID to monitor for the specified or active study.

        Args:
            job_id: Known background job or batch identifier.
            study_key: Optional study key override; defaults to ``current_study_key``.
        """
        self.track_jobs(job_id, study_key=study_key, refresh=True)

    def untrack_job(self, job_id: str, study_key: str | None = None) -> bool:
        """Remove a tracked batch/job ID from the specified or active study.

        Args:
            job_id: Batch or job identifier to stop tracking.
            study_key: Optional study key override; defaults to ``current_study_key``.

        Returns:
            True if a tracked job entry was removed; False otherwise.
        """
        cleaned = job_id.strip()
        if not cleaned:
            return False

        target_study = study_key if study_key is not None else self.current_study_key
        if target_study:
            entries = self._entries_for_study(target_study)
            removed_key = cleaned if cleaned in entries else None
            if removed_key is None:
                for key, entry in entries.items():
                    if _extract_entry_row(entry)[0] == cleaned:
                        removed_key = key
                        break
            if removed_key is None:
                return False
            del entries[removed_key]
            if self._inspected_job_id in {cleaned, removed_key}:
                self._inspected_job_id = None
            if target_study == self.current_study_key:
                self._sync_timer_for_study(target_study)
                if entries:
                    self._render_study_entries(target_study, entries)
                else:
                    self._render_empty_study_jobs(target_study)
            return True

        if cleaned in self._unassigned_job_ids:
            self._unassigned_job_ids.remove(cleaned)
            if self._inspected_job_id == cleaned:
                self._inspected_job_id = None
            if self.current_study_key is None:
                self._render_unassigned_jobs()
            return True
        return False

    def _show_validation_warning(self, warning_message: str) -> None:
        """Display an immediate sanitized validation warning without starting background polls."""
        reg = self._active_registry()
        sanitized = _sanitize_text(warning_message, registry=reg)
        logging.getLogger("imednet_tui").warning("Invalid batch ID input: %s", sanitized)
        notice_msg = f"Validation Warning: {sanitized}"
        self.update(Text.assemble((notice_msg, "yellow")))
        if self.is_mounted:
            with contextlib.suppress(Exception):
                self.query_one("#job_monitor_notice", Static).update(notice_msg)
            if hasattr(self.app, "notify"):
                self.app.notify(sanitized, severity="warning", markup=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Validate and stage/register batch IDs submitted from ``#batch_id_input``."""
        if event.input.id != "batch_id_input":
            return
        batch_ids = _normalize_batch_ids(event.value)
        if not batch_ids:
            event.input.value = ""
            return
        reg = self._active_registry()
        invalid_messages: list[str] = []
        for token in batch_ids:
            try:
                validate_path_segment(token, name="batch_id")
            except PathTraversalValidationError as err:
                invalid_messages.append(_sanitize_text(err, registry=reg))
        if invalid_messages:
            self._show_validation_warning("; ".join(invalid_messages))
            return
        event.input.value = ""
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
            loading_msg = f"Job Monitor: Loading jobs for study '{study_key}'..."
            self._update_ui(
                summary=Text(loading_msg),
                notice_text=loading_msg,
                rows=(),
                preserve_cursor=False,
            )
        self.schedule_refresh()

    async def _fetch_job_status(self, study_key: str, jid: str) -> Any:
        """Retrieve a single job status through the async or sync SPI facade."""
        async_fn: Callable[..., Any] | None = getattr(self.sdk, "async_get_job", None)
        if inspect.iscoroutinefunction(async_fn):
            return await async_fn(study_key, jid)
        sync_fn = cast(ImednetFacade, self.sdk).get_job
        return await asyncio.to_thread(sync_fn, study_key, jid)

    def _apply_job_evaluation(self, entry: _TrackedJobEntry, job: Any) -> None:
        """Evaluate ``job`` monotonically via ``evaluate_job_state`` and update ``entry``."""
        reg = self._active_registry()
        if entry.is_terminal:
            curr_state = getattr(job, "state", None)
            if (
                job is None
                or entry.last_state is None
                or curr_state is None
                or not getattr(job, "is_terminal", False)
                or not is_valid_job_transition(entry.last_state, curr_state)
            ):
                logging.getLogger("imednet_tui").warning(
                    "Ignoring stale response for job %s (state=%s); terminal state %s preserved",
                    entry.job_id,
                    _sanitize_text(repr(curr_state), registry=reg),
                    _sanitize_text(entry.last_state, registry=reg),
                )
            return

        if job is None:
            if entry.last_job is None or isinstance(entry.last_job, _TrackedJobPlaceholder):
                entry.last_job = _TrackedJobPlaceholder(entry.job_id, "UNKNOWN", is_terminal=False)
                entry.warning = None
            else:
                entry.warning = "Poll returned empty job status"
            return

        raw_state = getattr(job, "state", None)
        prev_state: JobState | str | None = (
            entry.last_state if entry.last_state != "NOT_FOUND" else None
        )
        if prev_state is not None and raw_state is None:
            missing_msg = f"Ignoring missing job state update after {_sanitize_text(prev_state, registry=reg)}"
            logging.getLogger("imednet_tui").warning(
                "Invalid job state update for %s: %s",
                entry.job_id,
                missing_msg,
            )
            entry.warning = missing_msg
            return

        try:
            is_done = evaluate_job_state(job, previous_state=prev_state)
        except JobFailedError as err:
            if prev_state is not None and (
                raw_state is None or not is_valid_job_transition(prev_state, raw_state)
            ):
                trans_msg = _sanitize_text(
                    f"Invalid job state transition from {prev_state} to {raw_state}",
                    registry=reg,
                )
                logging.getLogger("imednet_tui").warning(
                    "Invalid job state update for %s: %s", entry.job_id, trans_msg
                )
                entry.warning = trans_msg
                return
            entry.last_job = job
            if raw_state is not None:
                entry.last_state = raw_state
            entry.is_terminal = True
            entry.record_errors = list(err.record_errors) if err.record_errors else None
            entry.warning = _sanitize_text(err, registry=reg)
        except (InvalidJobStateError, InvalidJobTransitionError) as err:
            sanitized_err = _sanitize_text(err, registry=reg)
            logging.getLogger("imednet_tui").warning(
                "Invalid job state update for %s: %s", entry.job_id, sanitized_err
            )
            entry.warning = sanitized_err
        else:
            if (
                prev_state is not None
                and raw_state is not None
                and not is_valid_job_transition(prev_state, raw_state)
            ):
                trans_msg = _sanitize_text(
                    f"Invalid job state transition from {prev_state} to {raw_state}",
                    registry=reg,
                )
                logging.getLogger("imednet_tui").warning(
                    "Invalid job state update for %s: %s", entry.job_id, trans_msg
                )
                entry.warning = trans_msg
                return
            entry.last_job = job
            if raw_state is not None:
                entry.last_state = raw_state
            entry.record_errors = None
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
        reg = self._active_registry(study_key)
        async with self.semaphore:
            if entry.is_terminal:
                return
            try:
                job = await self._fetch_job_status(study_key, entry.job_id)
            except Exception as err:
                sanitized_err = _sanitize_text(err, registry=reg)
                if entry.is_terminal:
                    logging.getLogger("imednet_tui").warning(
                        "Ignoring transient error for terminal job %s: %s",
                        entry.job_id,
                        sanitized_err,
                    )
                    return
                if isinstance(err, PathTraversalValidationError):
                    logging.getLogger("imednet_tui").warning(
                        "Invalid batch ID segment for %s: %s",
                        _sanitize_text(entry.job_id, registry=reg),
                        sanitized_err,
                    )
                    entry.last_job = _TrackedJobPlaceholder(
                        entry.job_id,
                        "INVALID",
                        is_terminal=True,
                        non_retryable=True,
                    )
                    entry.last_state = "INVALID"
                    entry.is_terminal = True
                    entry.warning = sanitized_err
                    return
                if isinstance(err, NotFoundError):
                    not_found_msg = (
                        sanitized_err
                        if sanitized_err.strip()
                        else (
                            f"Batch '{_sanitize_text(entry.job_id, registry=reg)}' was not found "
                            f"in study '{_sanitize_text(study_key, registry=reg)}'."
                        )
                    )
                    logging.getLogger("imednet_tui").warning(
                        "Job %s not found: %s",
                        entry.job_id,
                        not_found_msg,
                    )
                    entry.last_job = _TrackedJobPlaceholder(
                        entry.job_id, "NOT_FOUND", is_terminal=True
                    )
                    entry.last_state = "NOT_FOUND"
                    entry.is_terminal = True
                    entry.warning = not_found_msg
                    return
                if (
                    isinstance(err, (InvalidJobStateError, InvalidJobTransitionError))
                    or err.__class__.__name__ == "ValidationError"
                    or "Invalid job state" in str(err)
                ):
                    logging.getLogger("imednet_tui").warning(
                        "Invalid job state update for %s: %s", entry.job_id, sanitized_err
                    )
                    entry.warning = sanitized_err
                    return
                logging.getLogger("imednet_tui").warning(
                    "Failed to poll job %s: %s", entry.job_id, sanitized_err
                )
                entry.is_terminal = False
                if entry.last_job is None or isinstance(entry.last_job, _TrackedJobPlaceholder):
                    entry.last_job = _TrackedJobPlaceholder(
                        entry.job_id, "FAILED", is_terminal=False
                    )
                    entry.warning = None
                else:
                    entry.warning = f"Poll error: {sanitized_err}"
                return

            self._apply_job_evaluation(entry, job)

    @staticmethod
    def _format_entry_line(
        entry: _TrackedJobEntry,
        *,
        registry: SensitivityRegistry | None = None,
    ) -> Text:
        """Format a single tracked job entry into a styled Rich ``Text`` line."""
        job = entry.last_job
        if job is None:
            if entry.warning:
                return Text.assemble(
                    ("WARNING", "yellow"),
                    f" - Job ({entry.job_id}) (Warning: {_sanitize_text(entry.warning, registry=registry)})",
                )
            return Text.assemble(("ACTIVE", "yellow"), f" - Job ({entry.job_id})")

        batch_id, state_str, progress_str, total_str, failed_str, created_str = _extract_entry_row(
            entry
        )
        raw_type = getattr(job, "job_type", None)
        type_label = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else "Job"
        color = _resolve_entry_state_color(entry, state_str)

        label = f"{type_label} ({batch_id})" if batch_id != "-" else type_label
        metrics_suffix = (
            ""
            if isinstance(job, _TrackedJobPlaceholder)
            else f" [{progress_str}, total={total_str}, failed={failed_str}]"
        )
        created_suffix = f" ({created_str})" if created_str != "-" else ""
        parts: list[str | tuple[str, str]] = [
            (state_str, color),
            f" - {label}{metrics_suffix}{created_suffix}",
        ]
        if entry.warning:
            parts.extend(
                [" ", (f"(Warning: {_sanitize_text(entry.warning, registry=registry)})", "yellow")]
            )
        return Text.assemble(*parts)

    def _render_study_entries(
        self,
        study_key: str,
        entries: dict[str, _TrackedJobEntry],
    ) -> None:
        """Render styled summary, notice banner, and table rows for ``entries``."""
        reg = self._active_registry(study_key)
        entry_list = list(entries.values())
        rows: list[tuple[Text, Text, Text, Text, Text, Text]] = []
        summary_parts: list[str | Text | tuple[str, str]] = [
            (f"Active & Recent Jobs ({len(entry_list)})", "bold")
        ]
        active_count = 0
        warnings: list[str] = []
        for entry in entry_list:
            batch_id, state_str, progress_str, total_str, failed_str, created_str = (
                _extract_entry_row(entry)
            )
            state_color = _resolve_entry_state_color(entry, state_str)
            rows.append(
                (
                    Text(batch_id),
                    Text(state_str, style=state_color),
                    Text(progress_str),
                    Text(total_str),
                    Text(failed_str),
                    Text(created_str),
                )
            )
            summary_parts.extend(["\n", self._format_entry_line(entry, registry=reg)])
            if not entry.is_terminal:
                active_count += 1
            if entry.warning:
                warnings.append(f"{entry.job_id}: {_sanitize_text(entry.warning, registry=reg)}")

        same_study = self._rendered_study_key == study_key
        self._rendered_study_key = study_key
        polling_note = f"{active_count} active" if active_count else "polling paused (all terminal)"
        notice_text = f"Active & Recent Jobs ({len(rows)}) — {polling_note}"
        if warnings:
            notice_text = f"{notice_text} | Warning: {'; '.join(warnings)}"
        self._update_ui(
            summary=Text.assemble(*summary_parts),
            notice_text=notice_text,
            rows=rows,
            preserve_cursor=same_study,
        )

    async def refresh_jobs(self, *, retry_placeholders: bool = False) -> None:
        """Poll active jobs and update the job table and summary for the active study.

        Args:
            retry_placeholders: When True, re-poll cached placeholder entries (such as
                ``NOT_FOUND``) during manual user-initiated refreshes.
        """
        study_key = self.current_study_key
        if not study_key:
            self._render_unassigned_jobs()
            return

        try:
            entries = self._entries_for_study(study_key)
            if not entries:
                if self.current_study_key != study_key:
                    return
                self._render_empty_study_jobs(study_key)
                return

            if retry_placeholders:
                for entry in entries.values():
                    if (
                        isinstance(entry.last_job, _TrackedJobPlaceholder)
                        and not entry.last_job.non_retryable
                    ):
                        entry.is_terminal = False
                        entry.warning = None
                        if not isinstance(entry.last_state, JobState):
                            entry.last_state = None

            active_entries = [entry for entry in entries.values() if not entry.is_terminal]
            if active_entries:
                await asyncio.gather(
                    *(self._poll_tracked_entry(study_key, entry) for entry in active_entries)
                )

            if self.current_study_key != study_key:
                return

            current_entries = self._entries_for_study(study_key)
            if not current_entries:
                self._render_empty_study_jobs(study_key)
                return

            if any(not entry.is_terminal for entry in current_entries.values()):
                self._resume_timer()
            else:
                self._pause_timer()

            self._render_study_entries(study_key, current_entries)
        except Exception as e:
            if self.current_study_key == study_key or not self.is_mounted:
                err_msg = f"Error fetching jobs: {_sanitize_text(e, registry=self._active_registry(study_key))}"
                self._update_ui(
                    summary=Text.assemble((err_msg, "red")),
                    notice_text=err_msg,
                    rows=(),
                    preserve_cursor=False,
                )


class LogViewer(Log):
    """Log viewer widget for auditing actions and SDK events."""

    DEFAULT_CSS = """
    LogViewer {
        height: 1fr;
        background: $surface;
        border: solid $primary;
    }
    """

    def __init__(
        self,
        *,
        registry_resolver: Callable[[], SensitivityRegistry | None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the LogViewer widget.

        Args:
            registry_resolver: Optional callable resolving the active study registry.
            **kwargs: Additional widget arguments.
        """
        super().__init__(**kwargs)
        self.border_title = "Logs"
        self._registry_resolver = registry_resolver

    def active_registry(self) -> SensitivityRegistry | None:
        """Resolve active study ``SensitivityRegistry`` when attached to a dashboard."""
        if self._registry_resolver is not None:
            return self._registry_resolver()
        with contextlib.suppress(Exception):
            screen_obj: Any = self.screen
            if isinstance(screen_obj, DashboardScreen):
                return screen_obj.active_phi_registry()
        return None

    def write(self, data: str, scroll_end: bool | None = None) -> Self:
        """Sanitize and write raw text to the log widget."""
        sanitized = _sanitize_text(data, registry=self.active_registry()) if data else data
        return super().write(sanitized, scroll_end=scroll_end)

    def write_lines(self, lines: Iterable[str], scroll_end: bool | None = None) -> Self:
        """Sanitize and write an iterable of lines to the log widget."""
        raw_list = list(lines)
        if not raw_list:
            return super().write_lines(raw_list, scroll_end=scroll_end)
        combined = "\n".join(str(line) for line in raw_list)
        sanitized = _sanitize_text(combined, registry=self.active_registry())
        return super().write_lines([sanitized], scroll_end=scroll_end)


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

    SubjectTable {
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
        height: 100%;
        border-top: solid white;
    }

    #bottom-pane > ContentSwitcher {
        height: 1fr;
    }

    #bottom-pane TabPane {
        height: 1fr;
        padding: 0;
    }

    #bottom-pane JobMonitor,
    #bottom-pane LogViewer {
        height: 1fr;
    }

    DashboardScreen.compact #dashboard-grid {
        grid-size: 2 3;
        grid-columns: 1fr 1fr;
        grid-rows: 1fr 1fr 2fr;
    }

    DashboardScreen.compact #subjects-box {
        column-span: 2;
    }

    DashboardScreen.compact #bottom-pane {
        column-span: 2;
    }

    #truncation-notice {
        display: none;
        height: 1;
        max-height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        text-wrap: nowrap;
        padding: 0 1;
        background: $warning 20%;
        color: $text;
    }

    #truncation-notice.visible {
        display: block;
    }

    /* High contrast mode styles */
    DashboardScreen.high-contrast {
        background: #000000;
        color: #FFFFFF;
    }

    DashboardScreen.high-contrast #truncation-notice {
        height: 1;
        max-height: 1;
        background: #FFFFFF;
        color: #000000;
        border: none;
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
        border: none;
        background: #000000;
        color: #FFFFFF;
    }

    DashboardScreen.high-contrast LogViewer,
    DashboardScreen.high-contrast LogViewer:focus {
        border: heavy white;
        background: #000000;
        color: #FFFFFF;
        text-opacity: 100%;
        background-tint: #000000 0%;
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

    DashboardScreen.high-contrast JobMonitor #job_detail_panel,
    DashboardScreen.high-contrast JobMonitor #job_detail_panel:focus,
    DashboardScreen.high-contrast JobMonitor #job_detail_panel > #job_detail_body {
        background: #000000;
        color: #FFFFFF;
    }

    DashboardScreen.high-contrast JobMonitor #job_detail_panel {
        border-left: heavy white;
    }

    DashboardScreen.high-contrast JobMonitor #job_detail_panel:focus {
        border-left: double white;
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
        self._phi_cache = _StudyPhiRegistryCache(sdk)
        self.log_viewer = LogViewer(registry_resolver=self.active_phi_registry)
        self.initial_study_key = (
            initial_study_key.strip() if initial_study_key and initial_study_key.strip() else None
        )
        self.initial_batch_ids = _normalize_batch_ids(initial_batch_ids)
        self._pending_batch_ids = [] if self.initial_study_key else list(self.initial_batch_ids)
        self.selected_study_key: str | None = None
        self.selected_site_id: str | None = None
        self._log_handler: TextualLogHandler | None = None
        self._previous_logger_settings: dict[str, tuple[int, bool]] = {}

    def active_phi_registry(self) -> SensitivityRegistry:
        """Resolve the isolated ``SensitivityRegistry`` for the active study."""
        study_key = self.selected_study_key
        if not study_key:
            with contextlib.suppress(Exception):
                study_key = self.query_one(JobMonitor).current_study_key
        if not study_key:
            study_key = self.initial_study_key
        return self._phi_cache.registry_for(study_key)

    def invalidate_phi_cache(self) -> None:
        """Clear cached per-study ``phi_fields`` registries on explicit refresh."""
        self._phi_cache.clear()

    def compose(self) -> ComposeResult:
        """Compose children widgets for the dashboard screen.

        Yields:
            Header, status banner, containers, grid panels, tab panes, and footer.
        """
        yield Header()
        yield Static("", id="truncation-notice", markup=False)
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
                    yield JobMonitor(self.sdk, phi_cache=self._phi_cache, id="job_monitor")
                with TabPane("Logs", id="logs-tab"):
                    yield self.log_viewer
        yield Footer()

    def _activate_study(self, study_key: str, study_name: str) -> None:
        """Activate ``study_key`` across sites, subjects, and job monitor panes."""
        with contextlib.suppress(Exception):
            self.workers.cancel_group(self, "load-all")
        self.selected_study_key = study_key
        self.selected_site_id = None
        self.log_viewer.write_line(f"Selected Study: {study_name} ({study_key})")
        with contextlib.suppress(Exception):
            self.query_one(StudyList).sync_selected_study(study_key)
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

    def _sync_compact_layout(self, width: int) -> None:
        """Toggle compact 2-column grid layout when terminal width drops below 100."""
        self.set_class(width < 100, "compact")

    def on_resize(self, event: Resize) -> None:
        """Reflow dashboard grid responsively when terminal dimensions change.

        Args:
            event: Textual resize event carrying updated viewport dimensions.
        """
        self._sync_compact_layout(event.size.width)

    def on_mount(self) -> None:
        """Sync high-contrast mode, attach SDK log handler, and apply initial CLI options."""
        if self.app.size.width > 0:
            self._sync_compact_layout(self.app.size.width)
        if getattr(self.app, "high_contrast_enabled", False) or "high-contrast" in self.app.classes:
            self.add_class("high-contrast")

        handler = TextualLogHandler(
            self.log_viewer,
            registry_resolver=self.active_phi_registry,
        )
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        self._previous_logger_settings.clear()
        for logger_name in ("imednet", "imednet_tui"):
            target_logger = logging.getLogger(logger_name)
            self._previous_logger_settings[logger_name] = (
                target_logger.level,
                target_logger.propagate,
            )
            if target_logger.level == logging.NOTSET or target_logger.level > logging.INFO:
                target_logger.setLevel(logging.INFO)
            target_logger.propagate = False
            target_logger.addHandler(handler)
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
        elif self._pending_batch_ids:
            job_monitor = self.query_one(JobMonitor)
            job_monitor.track_jobs(
                self._pending_batch_ids,
                study_key=None,
                refresh=False,
            )
            self._pending_batch_ids.clear()

    def on_unmount(self) -> None:
        """Detach the SDK/TUI log handler and restore original logger settings on unmount."""
        for logger_name, (prev_level, prev_propagate) in self._previous_logger_settings.items():
            target_logger = logging.getLogger(logger_name)
            if self._log_handler is not None:
                target_logger.removeHandler(self._log_handler)
            target_logger.setLevel(prev_level)
            target_logger.propagate = prev_propagate
        self._previous_logger_settings.clear()
        self._log_handler = None

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
        target_study_key = self.selected_study_key

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
        if self.selected_study_key == target_study_key:
            self.refresh_truncation_notice()

    def clear_site_filter(self) -> bool:
        """Clear any active site filter and reload unfiltered study subjects."""
        if self.selected_site_id is None:
            return False
        cleared_site = self.selected_site_id
        self.selected_site_id = None
        self.log_viewer.write_line(f"Cleared Site Filter (ID: {cleared_site})")
        with contextlib.suppress(Exception):
            self.workers.cancel_group(self, "load-all")
        site_list = self.query_one(SiteList)
        study_key = self.selected_study_key or site_list.current_study_key
        if study_key:
            subject_table = self.query_one(SubjectTable)
            self.run_worker(
                subject_table.load_subjects(study_key, None),
                exclusive=True,
                group="load-subjects",
            )
        return True

    def on_study_list_study_selected(self, message: StudyList.StudySelected) -> None:
        """Handle study selection by populating sites, subjects, and jobs.

        Args:
            message: Study selection event containing study details.
        """
        self._activate_study(message.study_key, message.study_name)

    def on_site_list_site_selected(self, message: SiteList.SiteSelected) -> None:
        """Handle site selection by filtering displayed subjects or toggling off active filter.

        Args:
            message: Site selection event containing site details.
        """
        if self.selected_site_id == message.site_id:
            self.clear_site_filter()
            return
        with contextlib.suppress(Exception):
            self.workers.cancel_group(self, "load-all")
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
        background: #000000;
        color: #FFFFFF;
    }

    .high-contrast #truncation-notice {
        height: 1;
        max-height: 1;
        background: #FFFFFF;
        color: #000000;
        border: none;
    }

    .high-contrast LogViewer,
    .high-contrast LogViewer:focus {
        background: #000000;
        color: #FFFFFF;
        text-opacity: 100%;
        background-tint: #000000 0%;
    }

    .high-contrast #job_detail_panel,
    .high-contrast #job_detail_panel:focus,
    .high-contrast #job_detail_panel > #job_detail_body {
        background: #000000;
        color: #FFFFFF;
    }

    .high-contrast #job_detail_panel {
        border-left: heavy white;
    }

    .high-contrast #job_detail_panel:focus {
        border-left: double white;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("q", "quit", "Quit"),
        ("d", "toggle_dark", "Dark Mode"),
        ("h", "toggle_high_contrast", "High Contrast"),
        ("r", "refresh_data", "Refresh"),
        ("a", "load_all", "Load All"),
        ("escape", "handle_escape", "Clear Filter"),
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
        self.sub_title = _resolve_env_subtitle(self.sdk)

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

    def action_handle_escape(self) -> None:
        """Exit batch input, close the job detail panel on the Jobs tab, or clear site filter."""
        screen = self._get_dashboard()
        if screen is None:
            return
        jobs_tab_active = False
        with contextlib.suppress(Exception):
            jobs_tab_active = screen.query_one("#bottom-pane", TabbedContent).active == "jobs-tab"
        if jobs_tab_active:
            if isinstance(self.focused, Input) and self.focused.id == "batch_id_input":
                with contextlib.suppress(Exception):
                    screen.query_one("#job_table", JobTable).focus()
                return
            with contextlib.suppress(Exception):
                detail_was_focused = isinstance(self.focused, JobDetailPanel)
                if screen.query_one(JobMonitor).close_job_detail():
                    if detail_was_focused:
                        with contextlib.suppress(Exception):
                            screen.query_one("#job_table", JobTable).focus()
                    return
        screen.clear_site_filter()

    def action_refresh_data(self) -> None:
        """Refresh studies and any active study/site panes on the dashboard screen."""
        screen = self._get_dashboard()
        if screen is None:
            return

        screen.invalidate_phi_cache()
        screen.active_phi_registry()

        with contextlib.suppress(Exception):
            screen.workers.cancel_group(screen, "load-all")

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
            job_monitor.schedule_refresh(retry_placeholders=True)

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
