# pylint: disable=duplicate-code
"""Integrated safety reporting dashboard.

Consolidates adverse events, protocol deviations, device deficiencies,
and site performance metrics into a single multi-tab view with advanced
filtering and saved view support.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import pandas as pd
import streamlit as st

from imednet.spi.models import (
    AdverseEvent,
    DeviceDeficiency,
    ProtocolDeviation,
    Record,
    StudyConfiguration,
)
from imednet_streamlit import components
from imednet_streamlit.auth import get_sdk, get_study_key
from imednet_streamlit.components.charts import render_accessible_chart
from imednet_streamlit.utils import models_to_frame

_HIGH_QUERY_RATE_THRESHOLD = 20.0
_HIGH_RATE_COLOR = "#ffe0e0"
_PERCENT_SCALE = 100.0
_MAX_HEATMAP_SUBJECTS = 50
_MAX_HEATMAP_FORMS = 20
_VIEW_STORE_KEY = "_reporting_saved_views"
_DEFAULT_VIEW_KEY = "_reporting_default_template"
_ACTIVE_VIEW_KEY = "_reporting_active_view_name"
_DEFAULT_TEMPLATE = "Safety Overview"
_COMPLETE_RECORD_STATUSES = {"Complete", "Pending SDV", "Verified"}

_TEMPLATES: dict[str, list[str]] = {
    "Safety Overview": [
        "Adverse Events",
        "Protocol Deviations",
        "Device Deficiencies",
        "Site Performance",
        "Data Completeness",
    ],
    "Adverse Events": ["Adverse Events"],
    "Protocol Deviations": ["Protocol Deviations"],
    "Device Deficiencies": ["Device Deficiencies"],
}


@dataclass(frozen=True)
class _AppliedFilters:
    """Container for active dashboard filter criteria."""

    site_filter: list[str]
    subject_filter: list[str]
    severity_filter: list[str]
    date_range: tuple[date, date]


def _get_date_range_defaults(frames: Sequence[pd.DataFrame]) -> tuple[date, date]:
    """Calculate the overall min/max date range across multiple DataFrames."""
    candidates: list[pd.Series] = []
    for frame in frames:
        for col in ("ae_start_date", "dv_date", "dd_date", "date_created"):
            if col in frame and not frame.empty:
                dt_col = pd.to_datetime(frame[col], utc=True, errors="coerce").dropna()
                if not dt_col.empty:
                    candidates.append(dt_col)
    if not candidates:
        today = date.today()
        return today, today
    combined = pd.concat(candidates, ignore_index=True)
    return combined.min().date(), combined.max().date()


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_subjects_df(_sdk: object, study_key: str, limit: int = 1000) -> pd.DataFrame:
    """Fetch subject metadata and return as a DataFrame."""
    try:
        rows = [
            {
                "subject_key": str(subject.subject_key),
                "site_name": str(subject.site_name or ""),
                "deleted": bool(subject.deleted),
            }
            for subject in _sdk.get_subjects(study_key=study_key, limit=limit)  # type: ignore
        ]
        if not rows:
            return pd.DataFrame(columns=["subject_key", "site_name", "deleted"])
        df = pd.DataFrame(rows)
        return df.loc[~df["deleted"]].reset_index(drop=True)
    except Exception as e:
        st.error(f"Failed to load subjects. The server-side chunked data request failed: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_records(_sdk: object, study_key: str, limit: int = 1000) -> list[Record]:
    """Fetch all records for a study."""
    try:
        return list(_sdk.get_records(study_key=study_key, limit=limit))  # type: ignore
    except Exception as e:
        st.error(f"Failed to load records. The server-side chunked data request failed: {e}")
        return []


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_forms_df(_sdk: object, study_key: str) -> pd.DataFrame:
    """Fetch form metadata and return as a DataFrame."""
    forms = _sdk.get_forms(study_key=study_key)  # type: ignore
    return pd.DataFrame(
        [
            {
                "form_key": str(form.form_key),
                "form_name": str(getattr(form, "form_name", "") or form.form_key),
            }
            for form in forms
        ],
        columns=["form_key", "form_name"],
    )


def _fallback_direct_models(
    records: list[Record],
) -> tuple[list[AdverseEvent], list[ProtocolDeviation], list[DeviceDeficiency]]:
    """Attempt to parse records into models without a formal study configuration."""
    aes: list[AdverseEvent] = []
    pds: list[ProtocolDeviation] = []
    dds: list[DeviceDeficiency] = []
    for record in records:
        payload = getattr(record, "record_data", {}) or {}
        if not isinstance(payload, dict):
            continue
        payload = dict(payload)
        payload.setdefault("subjectKey", getattr(record, "subject_key", ""))
        try:
            if {"aeTerm", "aeSeverity"} <= payload.keys():
                aes.append(AdverseEvent.model_validate(payload))
            elif {"dvTerm", "dvCategory", "dvSeverity", "dvDate"} <= payload.keys():
                pds.append(ProtocolDeviation.model_validate(payload))
            elif {"ddTerm", "ddCategory", "ddDate"} <= payload.keys():
                dds.append(DeviceDeficiency.model_validate(payload))
        except Exception:  # noqa: S112
            continue
    return aes, pds, dds


def _extract_domain_frames(
    records: list[Record], configuration: StudyConfiguration
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run extraction and return DataFrames for AE, PD, and DD domains."""
    from imednet_workflows.extraction_engine import extract_canonical_records

    extracted = extract_canonical_records(records, configuration)
    adverse_events = extracted.adverse_events
    protocol_deviations = extracted.protocol_deviations
    device_deficiencies = extracted.device_deficiencies
    if not adverse_events and not protocol_deviations and not device_deficiencies:
        adverse_events, protocol_deviations, device_deficiencies = _fallback_direct_models(records)
    return (
        models_to_frame(adverse_events, date_column="ae_start_date"),
        models_to_frame(protocol_deviations, date_column="dv_date"),
        models_to_frame(device_deficiencies, date_column="dd_date"),
    )


def _records_frame(
    records: list[Record], forms_df: pd.DataFrame, subjects_df: pd.DataFrame
) -> pd.DataFrame:
    """Prepare a consolidated records DataFrame with form and subject metadata."""
    rows = [
        {
            "record_id": record.record_id,
            "subject_key": str(record.subject_key),
            "site_id": getattr(record, "site_id", None),
            "form_key": record.form_key,
            "record_status": getattr(record, "record_status", None),
            "record_type": getattr(record, "record_type", None),
            "deleted": bool(getattr(record, "deleted", False)),
            "date_created": getattr(record, "date_created", None),
            "date_modified": getattr(record, "date_modified", None),
        }
        for record in records
    ]
    if not rows:
        return pd.DataFrame(
            columns=[
                "record_id",
                "subject_key",
                "site_name",
                "form_key",
                "form_name",
                "record_status",
                "record_type",
                "date_created",
                "date_modified",
            ]
        )

    df = pd.DataFrame(rows)
    df = df.loc[~df["deleted"]].copy()
    df["subject_key"] = df["subject_key"].astype(str)
    df["date_created"] = pd.to_datetime(df["date_created"], utc=True, errors="coerce")
    form_lookup = (
        forms_df.drop_duplicates("form_key")
        if not forms_df.empty
        else pd.DataFrame(columns=["form_key", "form_name"])
    )
    df = df.merge(form_lookup, on="form_key", how="left")
    df["form_name"] = df["form_name"].fillna(df["form_key"])
    if not subjects_df.empty:
        df = df.merge(subjects_df[["subject_key", "site_name"]], on="subject_key", how="left")
    else:
        df["site_name"] = ""
    return df


def _attach_site_name(df: pd.DataFrame, subjects_df: pd.DataFrame) -> pd.DataFrame:
    """Join a domain DataFrame with subject metadata to include site names."""
    if df.empty:
        return df
    out = df.copy()
    out["subject_key"] = out["subject_key"].astype(str)
    if subjects_df.empty:
        out["site_name"] = ""
        return out
    return out.merge(subjects_df[["subject_key", "site_name"]], on="subject_key", how="left")


def _build_site_metrics(_sdk: object, study_key: str, subjects_df: pd.DataFrame) -> pd.DataFrame:
    """Calculate site-level enrollment and query metrics."""
    from imednet_workflows.query_management import QueryManagementWorkflow

    site_enrollment = (
        subjects_df.groupby("site_name").size().reset_index(name="enrolled_count")
        if not subjects_df.empty
        else pd.DataFrame(columns=["site_name", "enrolled_count"])
    )
    workflow = QueryManagementWorkflow(sdk=_sdk)  # type: ignore
    open_queries = workflow.get_open_queries(study_key=study_key)
    query_rows = [
        {
            "subject_key": str(query.subject_key),
            "annotation_id": query.annotation_id,
            "date_created": query.date_created,
        }
        for query in open_queries
    ]
    if query_rows:
        query_df = pd.DataFrame(query_rows).merge(
            subjects_df[["subject_key", "site_name"]],
            on="subject_key",
            how="left",
        )
        now_utc = pd.Timestamp.now(tz="UTC")
        site_queries = (
            query_df.groupby("site_name")
            .agg(
                open_queries=("annotation_id", "count"),
                avg_days_open=(
                    "date_created",
                    lambda values: cast(
                        Any, now_utc - pd.to_datetime(values, utc=True)
                    ).dt.days.mean(),
                ),
            )
            .reset_index()
        )
    else:
        site_queries = pd.DataFrame(columns=["site_name", "open_queries", "avg_days_open"])

    merged = site_enrollment.merge(site_queries, on="site_name", how="left").fillna(0)
    for col in ("enrolled_count", "open_queries", "avg_days_open"):
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0)
    merged["query_rate"] = (
        merged["open_queries"] / merged["enrolled_count"].replace(0, 1) * _PERCENT_SCALE
    ).round(1)
    return merged


def _highlight_high_rate(value: Any) -> str:
    """Apply CSS highlighting for query rates above the threshold."""
    return f"background-color: {_HIGH_RATE_COLOR}" if value > _HIGH_QUERY_RATE_THRESHOLD else ""


def _apply_filters(
    df: pd.DataFrame,
    *,
    filters: _AppliedFilters,
    date_col: str | None = None,
    severity_col: str | None = None,
) -> pd.DataFrame:
    """Apply dashboard filters to a specific domain DataFrame."""
    if df.empty:
        return df
    filtered = df.copy()
    if filters.site_filter and "site_name" in filtered:
        filtered = filtered[filtered["site_name"].astype(str).isin(filters.site_filter)]
    if filters.subject_filter and "subject_key" in filtered:
        filtered = filtered[filtered["subject_key"].astype(str).isin(filters.subject_filter)]
    if filters.severity_filter and severity_col and severity_col in filtered:
        filtered = filtered[filtered[severity_col].astype(str).isin(filters.severity_filter)]
    if date_col and date_col in filtered:
        start, end = filters.date_range
        dt = pd.to_datetime(filtered[date_col], utc=True, errors="coerce")
        filtered = filtered[(dt.dt.date >= start) & (dt.dt.date <= end)]
    return filtered.reset_index(drop=True)


def _build_heatmap_source(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare a pivoted DataFrame for record completeness heatmap visualization."""
    if df.empty:
        return pd.DataFrame(
            columns=["subject_key", "form_name", "completion_flag", "completion_status"]
        )
    pivot = df.pivot_table(
        index="subject_key",
        columns="form_name",
        values="record_status",
        aggfunc=lambda values: int(values.isin(_COMPLETE_RECORD_STATUSES).any()),
        fill_value=0,
    )
    pivot = pivot.sort_index().sort_index(axis=1).iloc[:_MAX_HEATMAP_SUBJECTS, :_MAX_HEATMAP_FORMS]
    if pivot.empty:
        return pd.DataFrame(
            columns=["subject_key", "form_name", "completion_flag", "completion_status"]
        )
    heatmap = pivot.reset_index().melt(
        id_vars="subject_key", var_name="form_name", value_name="completion_flag"
    )
    heatmap["completion_flag"] = heatmap["completion_flag"].astype(int)
    heatmap["completion_status"] = heatmap["completion_flag"].map({1: "Complete", 0: "Incomplete"})
    return heatmap


def _default_configuration(template_name: str) -> StudyConfiguration:
    """Return a default StudyConfiguration based on the active template."""
    profile = "device" if template_name == "Device Deficiencies" else "general"
    return StudyConfiguration.model_validate(
        {"studyKey": get_study_key(), "reportingProfile": profile}
    )


def _render_view_controls(
    *,
    site_options: list[str],
    subject_options: list[str],
    severity_options: list[str],
    default_dates: tuple[date, date],
) -> tuple[str, _AppliedFilters]:
    """Render the dashboard sidebar controls for templates and filters."""
    saved_views = st.session_state.setdefault(_VIEW_STORE_KEY, {})
    default_template = st.session_state.setdefault(_DEFAULT_VIEW_KEY, _DEFAULT_TEMPLATE)
    active_view = st.session_state.setdefault(_ACTIVE_VIEW_KEY, "Default Study View")

    with st.sidebar:
        st.markdown("---")
        st.subheader("Dashboard Views")
        view_names = ["Default Study View", *sorted(saved_views)]
        active_view_index = view_names.index(active_view) if active_view in view_names else 0
        active_view = st.selectbox("Saved View", view_names, index=active_view_index)
        loaded = saved_views.get(active_view, {}) if active_view != "Default Study View" else {}

        template_default = str(loaded.get("template", default_template))
        template_names = list(_TEMPLATES)
        selected_template = st.selectbox(
            "Template",
            template_names,
            index=(
                template_names.index(template_default) if template_default in template_names else 0
            ),
        )

        date_default = loaded.get("date_range", default_dates)
        if not isinstance(date_default, tuple) or len(date_default) != 2:
            date_default = default_dates
        date_values = st.date_input("Date Range", value=list(date_default))
        if isinstance(date_values, list | tuple) and len(date_values) == 2:
            selected_date_range = (date_values[0], date_values[1])
        else:
            selected_date_range = default_dates

        site_filter = st.multiselect("Site", site_options, default=loaded.get("site_filter", []))
        subject_filter = st.multiselect(
            "Subject", subject_options, default=loaded.get("subject_filter", [])
        )
        severity_filter = st.multiselect(
            "Severity", severity_options, default=loaded.get("severity_filter", severity_options)
        )

        view_name = st.text_input("Save Current View As")
        col_save, col_default = st.columns(2)
        with col_save:
            if st.button("💾 Save View") and view_name:
                saved_views[view_name] = {
                    "template": selected_template,
                    "date_range": selected_date_range,
                    "site_filter": site_filter,
                    "subject_filter": subject_filter,
                    "severity_filter": severity_filter,
                }
                st.success("View saved.")
        with col_default:
            if st.button("⭐ Set as Default"):
                st.session_state[_DEFAULT_VIEW_KEY] = selected_template
                st.success("Default study view updated.")
    st.session_state[_ACTIVE_VIEW_KEY] = active_view
    return selected_template, _AppliedFilters(
        site_filter=site_filter,
        subject_filter=subject_filter,
        severity_filter=severity_filter,
        date_range=selected_date_range,
    )


def _render_adverse_events_tab(df: pd.DataFrame, enrolled_subjects: int) -> None:
    """Render the Adverse Events dashboard tab."""
    total_aes = len(df)
    serious_aes = int(df["ae_serious"].astype(bool).sum()) if "ae_serious" in df else 0
    ae_rate = round(total_aes / max(enrolled_subjects, 1), 2)
    components.kpi_row(
        [
            {"label": "Total AEs", "value": total_aes},
            {"label": "Serious AEs", "value": serious_aes},
            {"label": "AE Rate", "value": ae_rate},
        ]
    )
    if df.empty:
        st.info("No adverse event data for selected filters.")
        return
    severity_counts = df.groupby("ae_severity").size().reset_index(name="count")
    render_accessible_chart(
        components.bar_chart(
            severity_counts,
            x="count",
            y="ae_severity",
            color="ae_severity",
            title="AE Severity Distribution",
        ),
        use_container_width=True,
    )
    timeline = (
        df.assign(ae_start_date=pd.to_datetime(df["ae_start_date"], utc=True, errors="coerce"))
        .dropna(subset=["ae_start_date"])
        .assign(date=lambda frame: frame["ae_start_date"].dt.normalize())
        .groupby("date")
        .size()
        .reset_index(name="count")
    )
    if not timeline.empty:
        render_accessible_chart(
            components.line_chart(timeline, x="date", y="count", title="AE Timeline"),
            use_container_width=True,
        )
    components.filterable_dataframe(
        df.reindex(
            columns=[
                "subject_key",
                "site_name",
                "ae_term",
                "ae_severity",
                "ae_serious",
                "ae_start_date",
                "ae_end_date",
                "ae_outcome",
            ]
        ),
        key="ae_table",
    )


def _render_protocol_deviations_tab(df: pd.DataFrame, enrolled_subjects: int) -> None:
    """Render the Protocol Deviations dashboard tab."""
    total = len(df)
    major = (
        int(df["dv_severity"].astype(str).str.upper().eq("MAJOR").sum())
        if "dv_severity" in df
        else 0
    )
    rate = round(total / max(enrolled_subjects, 1), 2)
    components.kpi_row(
        [
            {"label": "Total Deviations", "value": total},
            {"label": "Major Deviations", "value": major},
            {"label": "Deviation Rate", "value": rate},
        ]
    )
    if df.empty:
        st.info("No protocol deviation data for selected filters.")
        return
    by_category = df.groupby("dv_category").size().reset_index(name="count")
    render_accessible_chart(
        components.bar_chart(
            by_category, x="count", y="dv_category", title="Deviations by Category"
        ),
        use_container_width=True,
    )
    trends = (
        df.assign(dv_date=pd.to_datetime(df["dv_date"], utc=True, errors="coerce"))
        .dropna(subset=["dv_date"])
        .assign(date=lambda frame: frame["dv_date"].dt.normalize())
        .groupby("date")
        .size()
        .reset_index(name="count")
    )
    if not trends.empty:
        render_accessible_chart(
            components.line_chart(trends, x="date", y="count", title="Deviation Trends"),
            use_container_width=True,
        )
    components.filterable_dataframe(
        df.reindex(
            columns=[
                "subject_key",
                "site_name",
                "dv_term",
                "dv_category",
                "dv_severity",
                "dv_date",
                "dv_status",
            ]
        ),
        key="pd_table",
    )


def _render_device_deficiencies_tab(df: pd.DataFrame) -> None:
    """Render the Device Deficiencies dashboard tab."""
    total = len(df)
    serious = int(df["dd_serious"].astype(bool).sum()) if "dd_serious" in df else 0
    components.kpi_row(
        [
            {"label": "Total Deficiencies", "value": total},
            {"label": "Serious Deficiencies", "value": serious},
        ]
    )
    if df.empty:
        st.info("No device deficiency data for selected filters.")
        return
    by_category = df.groupby("dd_category").size().reset_index(name="count")
    render_accessible_chart(
        components.bar_chart(
            by_category, x="count", y="dd_category", title="Deficiency Category Distribution"
        ),
        use_container_width=True,
    )
    components.filterable_dataframe(
        df.reindex(
            columns=["subject_key", "site_name", "dd_term", "dd_category", "dd_serious", "dd_date"]
        ),
        key="dd_table",
    )


def _render_site_performance_tab(df_site_metrics: pd.DataFrame) -> None:
    """Render the Site Performance dashboard tab."""
    components.kpi_row(
        [
            {
                "label": "Total Sites",
                "value": (
                    int(df_site_metrics["site_name"].nunique()) if not df_site_metrics.empty else 0
                ),
            },
            {
                "label": "Total Enrolled",
                "value": (
                    int(df_site_metrics["enrolled_count"].sum()) if not df_site_metrics.empty else 0
                ),
            },
            {
                "label": "Total Open Queries",
                "value": (
                    int(df_site_metrics["open_queries"].sum()) if not df_site_metrics.empty else 0
                ),
            },
        ]
    )
    display_cols = ["site_name", "enrolled_count", "open_queries", "query_rate", "avg_days_open"]
    display = df_site_metrics.reindex(columns=display_cols)
    st.dataframe(
        display.style.map(_highlight_high_rate, subset=["query_rate"]),
        use_container_width=True,
    )


def _render_data_completeness_tab(df_records: pd.DataFrame) -> None:
    """Render the Data Completeness dashboard tab."""
    heatmap_df = _build_heatmap_source(df_records)
    if heatmap_df.empty:
        st.info("No record completeness data for selected filters.")
    else:
        render_accessible_chart(
            components.bar_chart(
                heatmap_df.groupby("completion_status").size().reset_index(name="count"),
                x="count",
                y="completion_status",
                title="Completion Status",
            ),
            use_container_width=True,
        )
    components.filterable_dataframe(
        df_records.reindex(
            columns=[
                "record_id",
                "subject_key",
                "site_name",
                "form_name",
                "record_status",
                "record_type",
                "date_created",
                "date_modified",
            ]
        ),
        key="records_table",
    )


def render_page() -> None:
    """Render the main safety reporting dashboard page."""
    st.title("📊 Safety Reporting Dashboard")
    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    sdk = get_sdk()
    study_key = get_study_key()
    subjects_df = _fetch_subjects_df(sdk, study_key)
    forms_df = _fetch_forms_df(sdk, study_key)
    records = _fetch_records(sdk, study_key)

    ae_df, pd_df, dd_df = _extract_domain_frames(records, _default_configuration(_DEFAULT_TEMPLATE))
    ae_df = _attach_site_name(ae_df, subjects_df)
    pd_df = _attach_site_name(pd_df, subjects_df)
    dd_df = _attach_site_name(dd_df, subjects_df)
    records_df = _records_frame(records, forms_df, subjects_df)

    severity_values = sorted(
        {
            str(value)
            for frame, col in ((ae_df, "ae_severity"), (pd_df, "dv_severity"))
            if col in frame
            for value in frame[col].dropna().tolist()
        }
    )
    site_options = sorted(
        {
            str(value)
            for frame in (ae_df, pd_df, dd_df, records_df)
            if "site_name" in frame
            for value in frame["site_name"].dropna().tolist()
            if str(value)
        }
    )
    subject_options = sorted(
        {
            str(value)
            for frame in (ae_df, pd_df, dd_df, records_df)
            if "subject_key" in frame
            for value in frame["subject_key"].dropna().tolist()
        }
    )

    selected_template, filters = _render_view_controls(
        site_options=site_options,
        subject_options=subject_options,
        severity_options=severity_values,
        default_dates=_get_date_range_defaults((ae_df, pd_df, dd_df, records_df)),
    )

    ae_filtered = _apply_filters(
        ae_df,
        filters=filters,
        date_col="ae_start_date",
        severity_col="ae_severity",
    )
    pd_filtered = _apply_filters(
        pd_df,
        filters=filters,
        date_col="dv_date",
        severity_col="dv_severity",
    )
    dd_filtered = _apply_filters(
        dd_df,
        filters=filters,
        date_col="dd_date",
    )
    records_filtered = _apply_filters(records_df, filters=filters, date_col="date_created")
    site_metrics_filtered = _build_site_metrics(sdk, study_key, subjects_df)
    if filters.site_filter:
        site_metrics_filtered = site_metrics_filtered[
            site_metrics_filtered["site_name"].astype(str).isin(filters.site_filter)
        ]

    selected_tabs = _TEMPLATES[selected_template]
    tab_objects = st.tabs(selected_tabs)
    enrolled_subjects = (
        records_filtered["subject_key"].nunique()
        if not records_filtered.empty and "subject_key" in records_filtered
        else 0
    )
    for tab_name, tab in zip(selected_tabs, tab_objects):  # noqa: B905
        with tab:
            if tab_name == "Adverse Events":
                _render_adverse_events_tab(ae_filtered, enrolled_subjects)
            elif tab_name == "Protocol Deviations":
                _render_protocol_deviations_tab(pd_filtered, enrolled_subjects)
            elif tab_name == "Device Deficiencies":
                if not dd_filtered.empty or selected_template in (
                    "Device Deficiencies",
                    "Safety Overview",
                ):
                    _render_device_deficiencies_tab(dd_filtered)
                else:
                    st.info("Device safety profile is not active for this study.")
            elif tab_name == "Site Performance":
                _render_site_performance_tab(site_metrics_filtered)
            elif tab_name == "Data Completeness":
                _render_data_completeness_tab(records_filtered)


render_page()
