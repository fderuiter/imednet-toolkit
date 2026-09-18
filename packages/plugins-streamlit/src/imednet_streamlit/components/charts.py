"""Standard Altair chart wrappers for iMednet.

Provides accessible, brand-compliant bar, line, and pie charts with automatic
tabular data fallback for accessibility.
"""

from __future__ import annotations

from typing import Any, Literal, cast

import altair as alt
import pandas as pd

# iMednet brand palette — used across all charts
PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
HIGH_CONTRAST_PALETTE = ["#000000", "#E69F00", "#56B4E9", "#009E73", "#D55E00", "#0072B2"]


def _get_palette() -> list[str]:
    """Get the current palette based on high contrast mode setting."""
    import streamlit as st

    try:
        if st.session_state.get("high_contrast", False):
            return HIGH_CONTRAST_PALETTE
    except Exception:  # noqa: S110
        pass
    return PALETTE


def _color_encoding(field: str) -> alt.Color:
    """Return an Altair color encoding using the current palette."""
    return alt.Color(f"{field}:N", scale=alt.Scale(range=_get_palette()))


def bar_chart(
    df: pd.DataFrame,
    x: str,
    y: str,
    color: str | None = None,
    title: str = "",
    x_title: str | None = None,
    y_title: str | None = None,
) -> alt.Chart:
    """Build a horizontal Altair bar chart.

    Args:
        df: Source DataFrame for the chart.
        x: Quantitative column for the x-axis.
        y: Categorical column for the y-axis.
        color: Optional categorical column for grouped colors.
        title: Optional chart title.
        x_title: Optional x-axis label override.
        y_title: Optional y-axis label override.

    Returns:
        Configured Altair chart object.
    """
    chart = alt.Chart(df).mark_bar()
    if color:
        chart = chart.encode(
            x=alt.X(f"{x}:Q", title=x_title),
            y=alt.Y(f"{y}:N", title=y_title, sort="-x"),
            color=_color_encoding(color),
            tooltip=[alt.Tooltip(f"{y}:N"), alt.Tooltip(f"{x}:Q"), alt.Tooltip(f"{color}:N")],
        )
    else:
        chart = chart.encode(
            x=alt.X(f"{x}:Q", title=x_title),
            y=alt.Y(f"{y}:N", title=y_title, sort="-x"),
            color=alt.value(_get_palette()[0]),
            tooltip=[alt.Tooltip(f"{y}:N"), alt.Tooltip(f"{x}:Q")],
        )

    return cast(alt.Chart, chart.properties(width="container", title=title))


def line_chart(
    df: pd.DataFrame,
    x: str,
    y: str,
    color: str | None = None,
    title: str = "",
) -> alt.Chart:
    """Build an Altair temporal line chart.

    Args:
        df: Source DataFrame for the chart.
        x: Temporal column for the x-axis.
        y: Quantitative column for the y-axis.
        color: Optional categorical column for grouped lines.
        title: Optional chart title.

    Returns:
        Configured Altair chart object.
    """
    chart = alt.Chart(df).mark_line(point=True)
    if color:
        chart = chart.encode(
            x=alt.X(f"{x}:T"),
            y=alt.Y(f"{y}:Q"),
            color=_color_encoding(color),
            tooltip=[alt.Tooltip(f"{x}:T"), alt.Tooltip(f"{y}:Q"), alt.Tooltip(f"{color}:N")],
        )
    else:
        chart = chart.encode(
            x=alt.X(f"{x}:T"),
            y=alt.Y(f"{y}:Q"),
            color=alt.value(_get_palette()[0]),
            tooltip=[alt.Tooltip(f"{x}:T"), alt.Tooltip(f"{y}:Q")],
        )

    return cast(alt.Chart, chart.properties(width="container", title=title))


def pie_chart(
    df: pd.DataFrame,
    theta: str,
    color: str,
    title: str = "",
) -> alt.Chart:
    """Build an Altair pie chart.

    Args:
        df: Source DataFrame for the chart.
        theta: Quantitative column used for slice size.
        color: Categorical column used for slice color.
        title: Optional chart title.

    Returns:
        Configured Altair chart object.
    """
    return cast(
        alt.Chart,
        (
            alt.Chart(df)
            .mark_arc()
            .encode(
                theta=alt.Theta(f"{theta}:Q"),
                color=_color_encoding(color),
                tooltip=[alt.Tooltip(f"{color}:N"), alt.Tooltip(f"{theta}:Q")],
            )
            .properties(width="container", title=title, description=f"Pie chart for {title}")
        ),
    )


import streamlit as st


def render_accessible_chart(
    chart: alt.Chart,
    use_container_width: bool = True,
    theme: Literal["streamlit"] | None = "streamlit",
    **kwargs: Any,
) -> None:
    """Render an Altair chart with an accessible tabular data view."""
    title = getattr(chart, "title", "Chart")
    if isinstance(title, dict) and "text" in title:
        title = title["text"]
    elif not isinstance(title, str):
        title = "Chart"

    df = getattr(chart, "data", pd.DataFrame())

    # Ensure chart has a description for screen readers (ARIA label)
    if not hasattr(chart, "description") or not getattr(chart, "description", None):
        chart = chart.properties(description=f"Data visualization for {title}")

    st.altair_chart(chart, use_container_width=use_container_width, theme=theme, **kwargs)

    if isinstance(df, pd.DataFrame) and not df.empty:
        with st.expander(f"Tabular Data View: {title}", expanded=False):
            st.dataframe(df, use_container_width=use_container_width)
