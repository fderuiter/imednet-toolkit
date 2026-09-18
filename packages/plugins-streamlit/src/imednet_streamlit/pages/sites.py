# pylint: disable=duplicate-code
"""Site performance and metrics dashboard.

Visualizes site-level enrollment counts, query rates, and average query
resolution times across the study.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pandas as pd
import streamlit as st

from imednet_streamlit import components
from imednet_streamlit.auth import get_sdk, get_study_key
from imednet_streamlit.components.charts import render_accessible_chart

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

_HIGH_QUERY_RATE_THRESHOLD = 20.0
_HIGH_RATE_COLOR = "#ffe0e0"
_MAX_CHART_SITES = 10


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_site_metrics(
    _sdk: ImednetFacade, study_key: str, *, now_utc: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Build site metrics with site/query counts, rates, and average days open."""
    from imednet.spi.models import Query, Subject
    from imednet_workflows.query_management import QueryManagementWorkflow

    # --- Subjects ---
    subject_fields = list(Subject.model_fields.keys())
    subjects = _sdk.get_subjects(study_key=study_key)

    subject_rows = []
    for s in subjects:
        subject_rows.append({f: getattr(s, f, None) for f in subject_fields})

    df_subjects = pd.DataFrame(subject_rows, columns=subject_fields)

    if df_subjects.empty:
        df_subjects = pd.DataFrame(columns=subject_fields)
    else:
        df_subjects = df_subjects[~df_subjects["deleted"].fillna(False).astype(bool)]

    site_enrollment = df_subjects.groupby("site_name").size().reset_index(name="enrolled_count")

    # --- Open Queries ---
    workflow = QueryManagementWorkflow(sdk=_sdk)
    open_queries = workflow.get_open_queries(study_key=study_key)
    query_fields = list(Query.model_fields.keys())

    query_rows = []
    for q in open_queries:
        query_rows.append({f: getattr(q, f, None) for f in query_fields})

    df_queries = pd.DataFrame(query_rows, columns=query_fields)

    if df_queries.empty:
        df_queries = pd.DataFrame(columns=query_fields)

    # Join queries → subjects → site_name
    df_q_with_site = df_queries.merge(
        df_subjects[["subject_key", "site_name"]], on="subject_key", how="left"
    )

    if df_q_with_site.empty:
        site_queries = pd.DataFrame(columns=["site_name", "open_queries", "avg_days_open"])
    else:
        timestamp_now = now_utc or pd.Timestamp.now(tz="UTC")
        site_queries = (
            df_q_with_site.groupby("site_name")
            .agg(
                open_queries=("annotation_id", "count"),
                avg_days_open=(
                    "date_created",
                    lambda x: cast(Any, timestamp_now - pd.to_datetime(x, utc=True)).dt.days.mean(),
                ),
            )
            .reset_index()
        )

    # Merge enrollment + queries
    df_metrics = site_enrollment.merge(site_queries, on="site_name", how="left").fillna(0)
    for col in ("enrolled_count", "open_queries", "avg_days_open"):
        df_metrics[col] = pd.to_numeric(df_metrics[col], errors="coerce").fillna(0)
    df_metrics["query_rate"] = (
        df_metrics["open_queries"] / df_metrics["enrolled_count"].replace(0, 1) * 100
    ).round(1)
    return df_metrics


def _highlight_high_rate(val: Any) -> str:
    """Apply CSS highlighting for sites with query rates above the threshold."""
    return f"background-color: {_HIGH_RATE_COLOR}" if val > _HIGH_QUERY_RATE_THRESHOLD else ""


def _top_sites_with_other(
    df: pd.DataFrame, *, rank_column: str, top_n: int = _MAX_CHART_SITES
) -> pd.DataFrame:
    """Return top-N sites and aggregate others into a single 'Other' row."""
    if df.empty:
        return df

    sorted_df = df.sort_values(rank_column, ascending=False).reset_index(drop=True)
    top_df = sorted_df.head(top_n).copy()
    remainder = sorted_df.iloc[top_n:]
    if remainder.empty:
        return top_df

    enrolled_sum = remainder["enrolled_count"].sum()
    open_queries_sum = remainder["open_queries"].sum()
    weighted_days_open = (
        (remainder["avg_days_open"] * remainder["open_queries"]).sum() / open_queries_sum
        if open_queries_sum > 0
        else remainder["avg_days_open"].mean()
    )
    other_row = pd.DataFrame(
        [
            {
                "site_name": "Other",
                "enrolled_count": enrolled_sum,
                "open_queries": open_queries_sum,
                "avg_days_open": weighted_days_open,
                "query_rate": round(open_queries_sum / max(enrolled_sum, 1) * 100, 1),
            }
        ]
    )
    return pd.concat([top_df, other_row], ignore_index=True)


st.title("🏥 Site Performance")

if st.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

sdk = get_sdk()
study_key = get_study_key()
df_metrics = _fetch_site_metrics(sdk, study_key)

# ── KPI Row ───────────────────────────────────────────────────────────────
total_sites = int(df_metrics["site_name"].nunique()) if not df_metrics.empty else 0
total_enrolled = int(df_metrics["enrolled_count"].sum()) if not df_metrics.empty else 0
total_open_queries = int(df_metrics["open_queries"].sum()) if not df_metrics.empty else 0
avg_query_rate = (
    round(float(total_open_queries / total_enrolled * 100), 1) if total_enrolled else 0.0
)

components.kpi_row(
    [
        {"label": "Total Sites", "value": total_sites},
        {"label": "Total Enrolled", "value": total_enrolled},
        {"label": "Total Open Queries", "value": total_open_queries},
        {"label": "Avg Query Rate %", "value": f"{avg_query_rate}%"},
    ]
)

# ── Two-column charts ─────────────────────────────────────────────────────
col_left, col_right = st.columns([3, 2])

with col_left:
    st.subheader("Enrollment & Open Queries by Site")
    if not df_metrics.empty:
        chart_source = _top_sites_with_other(df_metrics, rank_column="open_queries")
        chart_df = chart_source[["site_name", "enrolled_count", "open_queries"]].melt(
            id_vars="site_name",
            value_vars=["enrolled_count", "open_queries"],
            var_name="metric",
            value_name="count",
        )
        render_accessible_chart(
            components.bar_chart(
                chart_df,
                x="count",
                y="site_name",
                color="metric",
                title="Enrolled Subjects & Open Queries per Site",
            ),
            use_container_width=True,
        )

with col_right:
    st.subheader("Avg Days Open by Site")
    if not df_metrics.empty:
        days_source = _top_sites_with_other(df_metrics, rank_column="avg_days_open")
        days_df = days_source[["site_name", "avg_days_open"]].copy()
        render_accessible_chart(
            components.bar_chart(
                days_df,
                x="avg_days_open",
                y="site_name",
                title="Average Days Open per Site",
            ),
            use_container_width=True,
        )

# ── Per-site metrics table ────────────────────────────────────────────────
st.subheader("Per-Site Metrics")
display_cols = ["site_name", "enrolled_count", "open_queries", "query_rate", "avg_days_open"]
if not df_metrics.empty:
    df_display = df_metrics[display_cols].copy()
else:
    df_display = pd.DataFrame(columns=display_cols)

page_df = components.paginated_slice(df_display, key="sites_metrics_table")
styled = page_df.style.map(_highlight_high_rate, subset=["query_rate"])
st.dataframe(styled, use_container_width=True)

components.csv_download_button(df_display, filename="site_metrics.csv")
