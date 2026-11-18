"""WCAG 2.1 AA and high-contrast styling, contrast utilities, and formatters for iMednet TUI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    """Contrast-verified color palette for a TUI presentation mode."""

    name: str
    background: str
    surface: str
    text: str
    muted: str
    accent: str
    success: str
    warning: str
    error: str


DARK_PALETTE = ThemePalette(
    name="dark",
    background="#0F172A",
    surface="#111827",
    text="#F8FAFC",
    muted="#CBD5E1",
    accent="#38BDF8",
    success="#4ADE80",
    warning="#FACC15",
    error="#FCA5A5",
)

LIGHT_PALETTE = ThemePalette(
    name="light",
    background="#FFFFFF",
    surface="#F8FAFC",
    text="#0F172A",
    muted="#334155",
    accent="#0369A1",
    success="#166534",
    warning="#854D0E",
    error="#991B1B",
)

HIGH_CONTRAST_PALETTE = ThemePalette(
    name="high-contrast",
    background="#000000",
    surface="#000000",
    text="#FFFFFF",
    muted="#E5E7EB",
    accent="#00FFFF",
    success="#00FF66",
    warning="#FFFF00",
    error="#FF80FF",
)


def _srgb_channel_luminance(channel_byte: int) -> float:
    """Convert an 8-bit sRGB channel to linear relative luminance component."""
    normalized = channel_byte / 255.0
    if normalized <= 0.04045:
        return normalized / 12.92
    return float(((normalized + 0.055) / 1.055) ** 2.4)


def relative_luminance(hex_color: str) -> float:
    """Compute WCAG 2.1 relative luminance for a 6-digit hex color string."""
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"Expected 6-digit hex color, got {hex_color!r}")
    red = int(cleaned[0:2], 16)
    green = int(cleaned[2:4], 16)
    blue = int(cleaned[4:6], 16)
    return (
        0.2126 * _srgb_channel_luminance(red)
        + 0.7152 * _srgb_channel_luminance(green)
        + 0.0722 * _srgb_channel_luminance(blue)
    )


def contrast_ratio(foreground_hex: str, background_hex: str) -> float:
    """Compute WCAG 2.1 contrast ratio between two hex colors (1.0 to 21.0)."""
    lum_fg = relative_luminance(foreground_hex)
    lum_bg = relative_luminance(background_hex)
    lighter = max(lum_fg, lum_bg)
    darker = min(lum_fg, lum_bg)
    return (lighter + 0.05) / (darker + 0.05)


def get_active_palette(*, dark: bool = True, high_contrast: bool = False) -> ThemePalette:
    """Return the active contrast-verified palette for the current TUI mode."""
    if high_contrast:
        return HIGH_CONTRAST_PALETTE
    return DARK_PALETTE if dark else LIGHT_PALETTE


def format_status_badge(
    status: str,
    *,
    dark: bool = True,
    high_contrast: bool = False,
) -> str:
    """Render a Rich-markup status badge meeting WCAG contrast ratios."""
    palette = get_active_palette(dark=dark, high_contrast=high_contrast)
    normalized = status.strip().upper() or "UNKNOWN"
    if normalized in {"COMPLETED", "COMPLETE", "SUCCESS", "ACTIVE", "RESOLVED", "CLOSED", "VALID"}:
        color = palette.success
    elif normalized in {
        "PROCESSING",
        "PENDING",
        "RUNNING",
        "QUEUED",
        "OPEN",
        "NEW",
        "UNDER_REVIEW",
    }:
        color = palette.warning
    else:
        color = palette.error
    return f"[bold {color}]{normalized}[/bold {color}]"


def format_progress_bar(
    progress: int | None,
    *,
    width: int = 16,
    high_contrast: bool = False,
) -> str:
    """Render a deterministic text progress bar from a 0..100 integer percentage."""
    pct = 0 if progress is None else max(0, min(100, int(progress)))
    bar_width = max(4, width)
    filled = round(bar_width * pct / 100.0)
    empty = max(0, bar_width - filled)
    fill_char = "#" if high_contrast else "█"
    empty_char = "-" if high_contrast else "░"
    return f"[{fill_char * filled}{empty_char * empty}] {pct:>3d}%"


WORKBENCH_CSS = """
Screen {
    background: #0F172A;
    color: #F8FAFC;
}

App.-light-mode Screen {
    background: #FFFFFF;
    color: #0F172A;
}

#dashboard-grid {
    layout: grid;
    grid-size: 3 2;
    grid-columns: 1fr 1fr 2fr;
    grid-rows: 2fr 3fr;
    height: 1fr;
}

#studies-box {
    border: solid #38BDF8;
    background: #111827;
    color: #F8FAFC;
}

#sites-box {
    border: solid #4ADE80;
    background: #111827;
    color: #F8FAFC;
}

#subjects-box {
    border: solid #FACC15;
    background: #111827;
    color: #F8FAFC;
}

#bottom-pane {
    column-span: 3;
    border-top: solid #38BDF8;
    background: #0F172A;
    color: #F8FAFC;
    height: 1fr;
}

App.-light-mode #studies-box {
    border: solid #0369A1;
    background: #F8FAFC;
    color: #0F172A;
}

App.-light-mode #sites-box {
    border: solid #166534;
    background: #F8FAFC;
    color: #0F172A;
}

App.-light-mode #subjects-box {
    border: solid #854D0E;
    background: #F8FAFC;
    color: #0F172A;
}

App.-light-mode #bottom-pane {
    border-top: solid #0369A1;
    background: #FFFFFF;
    color: #0F172A;
}

.pane-toolbar {
    height: auto;
    min-height: 3;
    padding: 0 1;
}

.filter-input {
    width: 1fr;
}

.detail-banner {
    height: auto;
    min-height: 2;
    padding: 0 1;
    background: #111827;
    color: #CBD5E1;
}

App.-light-mode .detail-banner {
    background: #F8FAFC;
    color: #334155;
}

.readonly-badge {
    color: #FACC15;
    text-style: bold;
    padding: 0 1;
}

App.-light-mode .readonly-badge {
    color: #854D0E;
}

/* High contrast mode styles (>= 7:1 AAA contrast) */
.high-contrast,
.high-contrast Screen {
    background: #000000;
    color: #FFFFFF;
}

.high-contrast #studies-box,
.high-contrast #sites-box,
.high-contrast #subjects-box,
.high-contrast JobMonitor,
.high-contrast .detail-banner {
    border: heavy #FFFFFF;
    background: #000000;
    color: #FFFFFF;
}

.high-contrast #bottom-pane {
    border-top: heavy #FFFFFF;
    background: #000000;
    color: #FFFFFF;
}

.high-contrast ListItem,
.high-contrast DataTable,
.high-contrast Input,
.high-contrast Button {
    color: #FFFFFF;
    background: #000000;
}

.high-contrast ListItem:focus,
.high-contrast DataTable > .datatable--cursor,
.high-contrast Button:focus {
    background: #FFFFFF;
    color: #000000;
}

.high-contrast .readonly-badge {
    color: #FFFF00;
}
"""

__all__ = [
    "DARK_PALETTE",
    "HIGH_CONTRAST_PALETTE",
    "LIGHT_PALETTE",
    "WORKBENCH_CSS",
    "ThemePalette",
    "contrast_ratio",
    "format_progress_bar",
    "format_status_badge",
    "get_active_palette",
    "relative_luminance",
]
