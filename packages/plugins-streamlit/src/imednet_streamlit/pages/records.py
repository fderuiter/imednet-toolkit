# pylint: disable=duplicate-code
"""Resolve the human-readable name for a form object."""

from __future__ import annotations

from typing import cast

import altair as alt
import pandas as pd
import streamlit as st

from imednet_streamlit import components
from imednet_streamlit.auth import get_sdk, get_study_key
from imednet_streamlit.components.charts import render_accessible_chart

RECORD_COLUMNS = [
    "record_id",
    "form_key",
    "subject_key",
    "site_id",
    "record_status",
    "record_type",
    "deleted",
    "date_created",
    "date_modified",
]
FORM_COLUMNS = ["form_key", "form_name"]
DISPLAY_COLUMNS = [
    "record_id",
    "form_name",
    "form_key",
    "subject_key",
    "site_id",
    "record_status",
    "record_type",
    "date_created",
    "date_modified",
]
STATUS_ORDER = ["Complete", "Incomplete", "Pending SDV", "Verified"]
COMPLETE_RECORD_STATUSES = {"Complete", "Pending SDV", "Verified"}
MAX_HEATMAP_SUBJECTS = 50
MAX_HEATMAP_FORMS = 20
LARGE_DATASET_WARNING_THRESHOLD = 10_000


def _resolve_form_name(form: object) -> str:
    """Resolve the human-readable name for a form object."""
    form_name = getattr(form, "form_name", "")
    return str(form_name or form.form_key)  # type: ignore[attr-defined]


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_records(_sdk: object, study_key: str, limit: int = 1000) -> pd.DataFrame:
    """Fetch records with server-side pagination and error handling."""
    from imednet.spi.models import Record

    try:
        records = _sdk.get_records(study_key=study_key, limit=limit)  # type: ignore[attr-defined]
        fields = list(Record.model_fields.keys())

        rows = []
        for r in records:
            rows.append({f: getattr(r, f, None) for f in fields})

        if not rows:
            return pd.DataFrame(columns=fields)

        df = pd.DataFrame(rows, columns=fields)
        deleted_mask = (
            df.get("deleted", pd.Series(False, index=df.index)).fillna(False).astype(bool)
        )
        return df.loc[~deleted_mask].reset_index(drop=True)
    except Exception as e:
        st.error(f"Failed to load records. The server-side chunked data request failed: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_form_metadata(_sdk: object, study_key: str) -> pd.DataFrame:
    """Resolve the human-readable name for a form object."""
    forms = _sdk.get_forms(study_key=study_key)  # type: ignore[attr-defined]
    rows = [{"form_key": f.form_key, "form_name": _resolve_form_name(f)} for f in forms]
    return pd.DataFrame(rows, columns=FORM_COLUMNS)


def _prepare_records_dataframe(records_df: pd.DataFrame, forms_df: pd.DataFrame) -> pd.DataFrame:
    """Resolve the human-readable name for a form object."""
    if records_df.empty:
        return pd.DataFrame(columns=[*RECORD_COLUMNS, "form_name"])

    metadata_df = forms_df if not forms_df.empty else pd.DataFrame(columns=FORM_COLUMNS)
    merged = records_df.merge(
        metadata_df.drop_duplicates(subset=["form_key"]),
        on="form_key",
        how="left",
    )
    merged["form_name"] = merged["form_name"].fillna(merged["form_key"])
    return merged


def _status_options(df: pd.DataFrame) -> list[str]:
    """Resolve the human-readable name for a form object."""
    if df.empty:
        return []

    values = {str(value) for value in df["record_status"].dropna().tolist()}
    ordered = [status for status in STATUS_ORDER if status in values]
    return ordered + sorted(values.difference(STATUS_ORDER))


def _sorted_unique_values(series: pd.Series) -> list[object]:
    """Resolve the human-readable name for a form object."""
    return sorted(series.dropna().unique().tolist(), key=lambda value: str(value))


def _apply_filters(
    df: pd.DataFrame,
    *,
    form_filter: list[str],
    site_filter: list[str],
    status_filter: list[str],
) -> pd.DataFrame:
    """Resolve the human-readable name for a form object."""
    filtered = df.copy()
    if form_filter:
        filtered = filtered[filtered["form_name"].isin(form_filter)]
    if site_filter:
        filtered = filtered[filtered["site_id"].astype(str).isin(site_filter)]
    if status_filter:
        filtered = filtered[filtered["record_status"].isin(status_filter)]
    return filtered


def _build_status_counts(df: pd.DataFrame) -> pd.DataFrame:
    """Resolve the human-readable name for a form object."""
    if df.empty:
        return pd.DataFrame(columns=["record_status", "count"])

    status_counts = df.groupby("record_status").size().reset_index(name="count")
    sort_order = {status: index for index, status in enumerate(STATUS_ORDER)}
    status_counts["sort_order"] = (
        status_counts["record_status"].map(sort_order).fillna(len(sort_order))
    )
    status_counts = status_counts.sort_values(["sort_order", "record_status"]).drop(
        columns=["sort_order"]
    )
    return status_counts  # noqa: RET504


def _build_incomplete_form_counts(df: pd.DataFrame) -> pd.DataFrame:
    """Resolve the human-readable name for a form object."""
    if df.empty:
        return pd.DataFrame(columns=["form_name", "count"])

    incomplete = df[df["record_status"] == "Incomplete"]
    if incomplete.empty:
        return pd.DataFrame(columns=["form_name", "count"])

    grouped = incomplete.groupby("form_name").size().reset_index(name="count")
    return components.top_n_with_other(
        grouped,
        label_column="form_name",
        value_column="count",
        top_n=10,
    )


def _build_heatmap_source(df: pd.DataFrame) -> pd.DataFrame:
    """Resolve the human-readable name for a form object."""
    columns = ["subject_key", "form_name", "completion_flag", "completion_status"]
    if df.empty:
        return pd.DataFrame(columns=columns)

    df_pivot = df.pivot_table(
        index="subject_key",
        columns="form_name",
        values="record_status",
        aggfunc=lambda values: int(values.isin(COMPLETE_RECORD_STATUSES).any()),
        fill_value=0,
    )
    df_pivot = df_pivot.sort_index().sort_index(axis=1)
    df_display = df_pivot.iloc[:MAX_HEATMAP_SUBJECTS, :MAX_HEATMAP_FORMS]
    if df_display.empty:
        return pd.DataFrame(columns=columns)

    heatmap_df = df_display.reset_index().melt(
        id_vars="subject_key",
        var_name="form_name",
        value_name="completion_flag",
    )
    heatmap_df["completion_flag"] = heatmap_df["completion_flag"].astype(int)
    heatmap_df["completion_status"] = heatmap_df["completion_flag"].map(
        {1: "Complete", 0: "Incomplete"}
    )
    return heatmap_df


def _build_heatmap_chart(heatmap_df: pd.DataFrame) -> alt.Chart:
    """Resolve the human-readable name for a form object."""
    unique_subjects = heatmap_df["subject_key"].nunique()
    chart_height = max(240, min(900, unique_subjects * 16))
    return cast(
        alt.Chart,
        (
            alt.Chart(heatmap_df)
            .mark_rect()
            .encode(
                x=alt.X("form_name:N", title="Form", sort=None),
                y=alt.Y("subject_key:N", title="Subject", sort=None),
                color=alt.Color(
                    "completion_status:N",
                    title="Completion",
                    scale=alt.Scale(
                        domain=["Incomplete", "Complete"],
                        range=[components.PALETTE[3], components.PALETTE[2]],
                    ),
                ),
                tooltip=[
                    alt.Tooltip("subject_key:N", title="Subject"),
                    alt.Tooltip("form_name:N", title="Form"),
                    alt.Tooltip("completion_status:N", title="Status"),
                ],
            )
            .properties(width="container", height=chart_height, title="Subject × Form Completion")  # noqa: RUF001
        ),
    )


def render_page() -> None:
    """Resolve the human-readable name for a form object."""
    st.title("📋 Data Completeness")

    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    sdk = get_sdk()
    study_key = get_study_key()
    records_df = _fetch_records(sdk, study_key)
    forms_df = _fetch_form_metadata(sdk, study_key)
    df = _prepare_records_dataframe(records_df, forms_df)

    if len(df) > LARGE_DATASET_WARNING_THRESHOLD:
        st.warning(
            f"⚠️ This study has {len(df):,} records. Charts may be slow. "
            "Consider filtering by form or site before analyzing."
        )

    form_options = (
        [str(value) for value in _sorted_unique_values(df["form_name"])]
        if "form_name" in df
        else []
    )
    site_options = (
        [str(value) for value in _sorted_unique_values(df["site_id"])] if "site_id" in df else []
    )
    status_options = _status_options(df)

    with st.sidebar:
        st.markdown("---")
        st.header("Filters")
        form_filter: list[str] = st.multiselect("Form", form_options)
        site_filter = st.multiselect("Site", site_options)
        status_filter: list[str] = st.multiselect("Status", status_options, default=status_options)

    df_filtered = _apply_filters(
        df,
        form_filter=form_filter,
        site_filter=site_filter,
        status_filter=status_filter,
    )

    components.kpi_row(
        [
            {"label": "Total Records", "value": len(df_filtered)},
            {"label": "Complete", "value": int((df_filtered["record_status"] == "Complete").sum())},
            {
                "label": "Incomplete",
                "value": int((df_filtered["record_status"] == "Incomplete").sum()),
            },
            {
                "label": "Pending SDV",
                "value": int((df_filtered["record_status"] == "Pending SDV").sum()),
            },
            {"label": "Verified", "value": int((df_filtered["record_status"] == "Verified").sum())},
        ]
    )

    col_left, col_right = st.columns(2)

    with col_left:
        st.header("Records by Status")
        status_counts = _build_status_counts(df_filtered)
        if not status_counts.empty:
            render_accessible_chart(
                components.bar_chart(
                    status_counts,
                    x="count",
                    y="record_status",
                    title="Status Breakdown",
                    x_title="Records",
                    y_title="Status",
                ),
                use_container_width=True,
            )

    with col_right:
        st.header("Top 10 Forms by Incomplete Count")
        incomplete_forms = _build_incomplete_form_counts(df_filtered)
        if not incomplete_forms.empty:
            render_accessible_chart(
                components.bar_chart(
                    incomplete_forms,
                    x="count",
                    y="form_name",
                    title="Incomplete Forms",
                    x_title="Incomplete Records",
                    y_title="Form",
                ),
                use_container_width=True,
            )

    st.header("Subject × Form Completion")  # noqa: RUF001
    heatmap_df = _build_heatmap_source(df_filtered)
    if heatmap_df.empty:
        st.info("No records found for the selected filters.")
    else:
        render_accessible_chart(_build_heatmap_chart(heatmap_df), use_container_width=True)

    st.header("Records Overview")
    from imednet.spi.models import Record

    all_fields = list(Record.model_fields.keys())
    display_cols = [
        c for c in ["record_id", "form_name", "form_key", "subject_key"] if c in df_filtered.columns
    ]
    for c in all_fields:
        if c not in display_cols and c in df_filtered.columns and c != "deleted":
            display_cols.append(c)
    table_df = df_filtered.reindex(columns=display_cols)
    components.filterable_dataframe(table_df, key="records_table")

    col_download_csv, col_download_excel = st.columns(2)
    with col_download_csv:
        components.csv_download_button(table_df, filename="records.csv")
    with col_download_excel:
        components.excel_download_button(table_df, filename="records.xlsx")


render_page()
