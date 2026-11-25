"""Study metadata inspector for UAT specification generation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

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
    from imednet.spi.validation import DataDictionary

_NON_SUBJECT_FORM_TYPES = frozenset({"site", "study", "admin"})
_LEGACY_ENROLLMENT_FORM_TYPES = frozenset({"enrollment", "registration"})
_SUBJECT_ENROLLMENT_FORM_TYPES = frozenset({"subject", "crf", ""})


def _coerce_optional_bool(value: Any) -> bool | None:
    """Parse boolean or truthy/falsy string tokens into ``bool | None``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "yes", "y", "1"}:
            return True
        if token in {"false", "no", "n", "0"}:
            return False
    return None


def _is_true_flag(obj: Any, *attrs: str) -> bool:
    """Return True only when an attribute or mapping key on ``obj`` is literally ``True``."""
    for attr in attrs:
        val = obj.get(attr) if isinstance(obj, Mapping) else getattr(obj, attr, None)
        if val is True:
            return True
    return False


def _normalized_form_type(form: Any) -> str:
    """Extract lowercase ``form_type`` / ``formType`` string when available."""
    raw = (
        (form.get("form_type") or form.get("formType"))
        if isinstance(form, Mapping)
        else (getattr(form, "form_type", None) or getattr(form, "formType", None))
    )
    return raw.strip().lower() if isinstance(raw, str) else ""


def is_disabled_form(form: Any) -> bool:
    """Return True when ``form`` has ``disabled=True``."""
    return _is_true_flag(form, "disabled")


def is_non_subject_form(form: Any) -> bool:
    """Return True when ``form`` is an ``otherForms`` or Site/Study/Admin form."""
    return (
        _is_true_flag(form, "other_forms", "otherForms")
        or _normalized_form_type(form) in _NON_SUBJECT_FORM_TYPES
    )


def is_enrollment_form(form: Any) -> bool:
    """Return True when ``form`` is an active Subject registration/enrollment form."""
    if is_disabled_form(form) or is_non_subject_form(form):
        return False
    ft = _normalized_form_type(form)
    if ft in _LEGACY_ENROLLMENT_FORM_TYPES:
        return True
    return (
        _is_true_flag(form, "subject_record_report", "subjectRecordReport")
        and ft in _SUBJECT_ENROLLMENT_FORM_TYPES
    )


def is_unscheduled_form(form: Any) -> bool:
    """Return True when ``form`` has ``unscheduledVisit=True``."""
    return _is_true_flag(form, "unscheduled_visit", "unscheduledVisit")


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
        """Return active subject registration forms."""
        return [form for form in self.forms if is_enrollment_form(form)]

    def scheduled_forms(self) -> list[Form]:
        """Return active scheduled subject forms (excluding enrollment, unscheduled, and non-subject)."""
        return [
            form
            for form in self.forms
            if not is_disabled_form(form)
            and not is_non_subject_form(form)
            and not is_enrollment_form(form)
            and not is_unscheduled_form(form)
        ]

    def unscheduled_forms(self) -> list[Form]:
        """Return active unscheduled subject forms."""
        return [
            form
            for form in self.forms
            if not is_disabled_form(form)
            and not is_non_subject_form(form)
            and not is_enrollment_form(form)
            and is_unscheduled_form(form)
        ]

    def active_sites(self) -> list[Site]:
        """Return sites that are actively enrolling."""
        return [
            site for site in self.sites if is_site_enrollment_eligible(site.site_enrollment_status)
        ]

    @classmethod
    def from_data_dictionary(
        cls,
        dd: DataDictionary,
        study_key: str,
        *,
        site_name: str | None = None,
        spec_site_names: Sequence[str] | None = None,
    ) -> StudySnapshot:
        """Synthesize an offline :class:`StudySnapshot` from a canonical :class:`DataDictionary`."""
        forms: list[Form] = []
        variables: list[Variable] = []
        interval_forms: dict[str, list[dict[str, Any]]] = {}

        for form_idx, dd_form in enumerate(dd.forms, start=1):
            f_extra = (
                dd_form.extraAttributes if isinstance(dd_form.extraAttributes, Mapping) else {}
            )
            raw_fid = dd_form.formId
            form_id = (
                raw_fid if isinstance(raw_fid, int) and not isinstance(raw_fid, bool) else form_idx
            )
            form_key = dd_form.formKey
            form_name = dd_form.formName or form_key
            form_type = dd_form.formType or "Subject"

            disabled = _coerce_optional_bool(f_extra.get("disabled"))
            other_forms = _coerce_optional_bool(
                f_extra.get("otherForms", f_extra.get("other_forms"))
            )
            subject_record_report = _coerce_optional_bool(
                f_extra.get("subjectRecordReport", f_extra.get("subject_record_report"))
            )
            unscheduled_visit = _coerce_optional_bool(
                f_extra.get("unscheduledVisit", f_extra.get("unscheduled_visit"))
            )

            raw_visits = f_extra.get("visits")
            if isinstance(raw_visits, list) and raw_visits:
                scheduled_visit_names: list[str] = []
                all_unscheduled = True
                for visit_entry in raw_visits:
                    if isinstance(visit_entry, Mapping):
                        v_name = str(
                            visit_entry.get("name")
                            or visit_entry.get("intervalName")
                            or visit_entry.get("oid")
                            or ""
                        ).strip()
                        v_type = str(visit_entry.get("type") or "").strip().lower()
                        if v_type != "unscheduled":
                            all_unscheduled = False
                            if v_name:
                                scheduled_visit_names.append(v_name)
                    elif isinstance(visit_entry, str) and visit_entry.strip():
                        all_unscheduled = False
                        scheduled_visit_names.append(visit_entry.strip())
                if unscheduled_visit is None and all_unscheduled:
                    unscheduled_visit = True
                for v_name in scheduled_visit_names:
                    interval_forms.setdefault(v_name, []).append(
                        {"form_id": form_id, "form_key": form_key, "form_name": form_name}
                    )

            raw_interval_name = f_extra.get("intervalName", f_extra.get("interval_name"))
            if isinstance(raw_interval_name, str) and raw_interval_name.strip():
                interval_forms.setdefault(raw_interval_name.strip(), []).append(
                    {"form_id": form_id, "form_key": form_key, "form_name": form_name}
                )

            forms.append(
                Form(
                    study_key=study_key,
                    form_id=form_id,
                    form_key=form_key,
                    form_name=form_name,
                    form_type=form_type,
                    subject_record_report=subject_record_report,
                    unscheduled_visit=unscheduled_visit,
                    other_forms=other_forms,
                    disabled=disabled,
                )
            )

            for var_idx, dd_var in enumerate(dd_form.variables, start=1):
                v_extra = (
                    dd_var.extraAttributes if isinstance(dd_var.extraAttributes, Mapping) else {}
                )
                raw_vid = v_extra.get("variableId", v_extra.get("variable_id"))
                var_id = (
                    raw_vid
                    if isinstance(raw_vid, int) and not isinstance(raw_vid, bool)
                    else var_idx
                )
                req_bool = _coerce_optional_bool(dd_var.required)
                var_oid = v_extra.get("variableOid", v_extra.get("itemOid"))
                raw_odm_dt = v_extra.get("odmDataType")
                eff_var_type = (
                    raw_odm_dt.strip()
                    if isinstance(raw_odm_dt, str) and raw_odm_dt.strip()
                    else (dd_var.variableType or "Text")
                )
                variables.append(
                    Variable(
                        study_key=study_key,
                        form_id=form_id,
                        form_key=form_key,
                        form_name=form_name,
                        variable_id=var_id,
                        variable_name=dd_var.variableName,
                        variable_type=eff_var_type,
                        label=dd_var.label,
                        required=req_bool if req_bool is not None else dd_var.required,
                        variable_oid=str(var_oid) if isinstance(var_oid, str) else None,
                        disabled=_coerce_optional_bool(v_extra.get("disabled")),
                        deleted=_coerce_optional_bool(v_extra.get("deleted")),
                    )
                )

        meta_extra = (
            dd.metadata.extraAttributes
            if dd.metadata and isinstance(dd.metadata.extraAttributes, Mapping)
            else {}
        )
        intervals: list[Interval] = []
        raw_intervals = meta_extra.get("intervals")
        if isinstance(raw_intervals, list):
            for idx, item in enumerate(raw_intervals, start=1):
                if isinstance(item, Interval):
                    intervals.append(item)
                elif isinstance(item, Mapping):
                    i_name = item.get("intervalName", item.get("interval_name"))
                    if isinstance(i_name, str) and i_name.strip():
                        intervals.append(
                            Interval(
                                study_key=study_key,
                                interval_id=item.get("intervalId", item.get("interval_id", idx)),
                                interval_name=i_name.strip(),
                                disabled=_coerce_optional_bool(item.get("disabled")),
                                forms=item.get("forms")
                                if isinstance(item.get("forms"), list)
                                else None,
                            )
                        )
        if not intervals and interval_forms:
            for idx, (i_name, f_summaries) in enumerate(interval_forms.items(), start=1):
                intervals.append(
                    Interval.model_validate(
                        {
                            "studyKey": study_key,
                            "intervalId": idx,
                            "intervalName": i_name,
                            "forms": f_summaries,
                        }
                    )
                )

        sites: list[Site] = []
        raw_sites = meta_extra.get("sites")
        if isinstance(raw_sites, list):
            for s_item in raw_sites:
                if isinstance(s_item, Site):
                    sites.append(s_item)
                elif isinstance(s_item, Mapping):
                    s_name = s_item.get("siteName", s_item.get("site_name"))
                    if isinstance(s_name, str) and s_name.strip():
                        sites.append(
                            Site(
                                study_key=study_key,
                                site_name=s_name.strip(),
                                site_enrollment_status=s_item.get(
                                    "siteEnrollmentStatus",
                                    s_item.get("site_enrollment_status", "Enrollment Open"),
                                ),
                            )
                        )
        else:
            seen_sites: set[str] = set()
            candidate_names: list[str] = []
            if isinstance(site_name, str) and site_name.strip():
                candidate_names.append(site_name.strip())
            if spec_site_names:
                for raw_spec_site in spec_site_names:
                    if isinstance(raw_spec_site, str) and raw_spec_site.strip():
                        candidate_names.append(raw_spec_site.strip())
            for candidate in candidate_names:
                if candidate not in seen_sites:
                    seen_sites.add(candidate)
                    sites.append(
                        Site(
                            study_key=study_key,
                            site_name=candidate,
                            site_enrollment_status="Enrollment Open",
                        )
                    )

        return cls(
            study_key=study_key,
            forms=forms,
            variables=variables,
            intervals=intervals,
            sites=sites,
        )


class StudySchemaInspector:
    """Fetch and index all structural metadata for an iMednet study.

    Supports both sync and async SDK clients. When an async client is used,
    metadata calls are executed concurrently via ``asyncio.gather``.
    The in-memory cache is per inspector instance. It can be shared by concurrent
    tasks in a single event loop, but is not thread-safe across threads/processes;
    use external synchronization or separate inspector instances for that access pattern.
    """

    supports_async = True

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade) -> None:
        """Initialize the study schema inspector.

        Args:
            sdk: An instance of the synchronous or asynchronous iMednet SDK facade.
        """
        self._sdk = sdk
        self._cache: dict[str, StudySnapshot] = {}

    def inspect(self, study_key: str, *, force_refresh: bool = False) -> StudySnapshot:
        """Synchronously fetch and return a study snapshot."""
        if hasattr(self._sdk, "async_get_forms") and not callable(
            getattr(self._sdk, "get_forms", None)
        ):
            raise TypeError(
                "StudySchemaInspector.inspect() requires a synchronous ImednetSDK "
                "(ImednetFacade). With AsyncImednetSDK, await "
                "inspector.async_inspect(...) instead."
            )
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
