"""Study metadata inspector for UAT specification generation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict, cast

from pydantic import Field

from imednet.spi.models import (
    Form,
    ImednetBaseModel,
    Interval,
    Site,
    Variable,
    is_site_enrollment_eligible,
)

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade


class FormVariableMap(TypedDict):
    """Mapping of a form to its associated variables."""

    form: Form
    variables: list[Variable]


class StudySnapshot(ImednetBaseModel):
    """Point-in-time snapshot of a study's structural metadata."""

    study_key: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    forms: list[Form]
    variables: list[Variable]
    intervals: list[Interval]
    sites: list[Site]
    forms_by_key: dict[str, Form] = Field(default_factory=dict)
    variables_by_form: dict[str, list[Variable]] = Field(default_factory=dict)
    intervals_by_name: dict[str, Interval] = Field(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        """Initialize derived lookup dictionaries after model initialization."""
        self.forms_by_key = {
            form.form_key: form for form in self.forms if form.form_key is not None
        }
        self.variables_by_form = {}
        for variable in self.variables:
            if variable.form_key is not None:
                self.variables_by_form.setdefault(variable.form_key, []).append(variable)
        self.intervals_by_name = {
            interval.interval_name: interval
            for interval in self.intervals
            if interval.interval_name is not None
        }

    def enrollment_forms(self) -> list[Form]:
        """Return forms with form_type indicating subject registration."""
        return [form for form in self.forms if form.form_type in ("Enrollment", "Registration")]

    def scheduled_forms(self) -> list[Form]:
        """Return scheduled (non-enrollment, non-unscheduled) forms."""
        return [
            form
            for form in self.forms
            if not getattr(form, 'unscheduled_visit', False)
            and form.form_type not in ("Enrollment", "Registration")
        ]

    def unscheduled_forms(self) -> list[Form]:
        """Return unscheduled forms."""
        return [form for form in self.forms if getattr(form, 'unscheduled_visit', False)]

    def active_sites(self) -> list[Site]:
        """Return sites that are actively enrolling."""
        return [
            site for site in self.sites if is_site_enrollment_eligible(site.site_enrollment_status)
        ]


class StudySchemaInspector:
    """Fetch and index all structural metadata for an iMednet study.

    Supports both sync and async SDK clients. When an async client is used,
    metadata calls are executed concurrently via ``asyncio.gather``.
    The in-memory cache is per inspector instance. It can be shared by concurrent
    tasks in a single event loop, but is not thread-safe across threads/processes;
    use external synchronization or separate inspector instances for that access pattern.
    """

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade) -> None:
        """Initialize the study schema inspector.

        Args:
            sdk: An instance of the synchronous or asynchronous iMednet SDK facade.
        """
        self._sdk = sdk
        self._cache: dict[str, StudySnapshot] = {}

    def inspect(self, study_key: str, *, force_refresh: bool = False) -> StudySnapshot:
        """Synchronously fetch and return a study snapshot."""
        if not force_refresh and study_key in self._cache:
            return self._cache[study_key]

        sdk = cast("ImednetFacade", self._sdk)
        snapshot = StudySnapshot(
            study_key=study_key,
            forms=sdk.get_forms(study_key),
            variables=sdk.get_variables(study_key),
            intervals=sdk.get_intervals(study_key),
            sites=sdk.get_sites(study_key),
        )
        self._cache[study_key] = snapshot
        return snapshot

    async def async_inspect(self, study_key: str, *, force_refresh: bool = False) -> StudySnapshot:
        """Asynchronously fetch metadata concurrently and return a study snapshot."""
        if not force_refresh and study_key in self._cache:
            return self._cache[study_key]

        sdk = cast("AsyncImednetFacade", self._sdk)

        async def fetch_forms() -> list[Any]:
            """Asynchronous fetch for forms."""
            return await sdk.async_get_forms(study_key)

        async def fetch_variables() -> list[Any]:
            """Asynchronous fetch for variables."""
            return await sdk.async_get_variables(study_key)

        async def fetch_intervals() -> list[Any]:
            """Asynchronous fetch for intervals."""
            return await sdk.async_get_intervals(study_key)

        async def fetch_sites() -> list[Any]:
            """Asynchronous fetch for sites."""
            return await sdk.async_get_sites(study_key)

        forms, variables, intervals, sites = await asyncio.gather(
            fetch_forms(),
            fetch_variables(),
            fetch_intervals(),
            fetch_sites(),
        )

        snapshot = StudySnapshot(
            study_key=study_key,
            forms=forms,
            variables=variables,
            intervals=intervals,
            sites=sites,
        )
        self._cache[study_key] = snapshot
        return snapshot

    def clear_cache(self, study_key: str | None = None) -> None:
        """Clear snapshot cache for one study key or all keys."""
        if study_key is None:
            self._cache.clear()
            return
        self._cache.pop(study_key, None)
