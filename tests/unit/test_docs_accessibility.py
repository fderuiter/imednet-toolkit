"""Automated accessibility and contrast ratio regression tests for documentation theme."""

import re
from pathlib import Path

import pytest
from docs.conf import html_theme_options

REPO_ROOT = Path(__file__).resolve().parents[2]


def relative_luminance(hex_code: str) -> float:
    """Calculate WCAG 2.1 relative luminance for an sRGB hex code."""
    hex_code = hex_code.lstrip("#")
    r, g, b = (int(hex_code[i : i + 2], 16) / 255.0 for i in (0, 2, 4))

    def to_linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * to_linear(r) + 0.7152 * to_linear(g) + 0.0722 * to_linear(b)


def contrast_ratio(hex1: str, hex2: str) -> float:
    """Compute WCAG 2.1 contrast ratio between two colors."""
    l1 = relative_luminance(hex1)
    l2 = relative_luminance(hex2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def blend_tint(fg_hex: str, alpha: float, bg_hex: str) -> str:
    """Blend foreground with alpha over a solid background."""
    fg = fg_hex.lstrip("#")
    bg = bg_hex.lstrip("#")
    r1, g1, b1 = (int(fg[i : i + 2], 16) for i in (0, 2, 4))
    r2, g2, b2 = (int(bg[i : i + 2], 16) for i in (0, 2, 4))
    r = int(r1 * alpha + r2 * (1.0 - alpha))
    g = int(g1 * alpha + g2 * (1.0 - alpha))
    b = int(b1 * alpha + b2 * (1.0 - alpha))
    return f"#{r:02x}{g:02x}{b:02x}"


@pytest.mark.unit
def test_light_mode_brand_button_contrast() -> None:
    """Active brand buttons in light mode must provide at least 4.5:1 contrast."""
    light_vars = html_theme_options.get("light_css_variables", {})
    btn_text = light_vars.get("color-brand-btn-text") or light_vars.get("color-brand-button-text")
    brand_primary = light_vars.get("color-brand-primary")

    assert btn_text is not None, "Brand button text token missing in light theme"
    assert brand_primary is not None, "Brand primary color missing in light theme"

    ratio = contrast_ratio(btn_text, brand_primary)
    assert ratio >= 4.5, f"Light mode brand button contrast {ratio:.2f}:1 fails WCAG AA (>= 4.5:1)"


@pytest.mark.unit
def test_dark_mode_brand_button_contrast() -> None:
    """Active brand buttons in dark mode must provide at least 4.5:1 contrast."""
    dark_vars = html_theme_options.get("dark_css_variables", {})
    btn_text = dark_vars.get("color-brand-btn-text") or dark_vars.get("color-brand-button-text")
    brand_primary = dark_vars.get("color-brand-primary")

    assert btn_text is not None, "Brand button text token missing in dark theme"
    assert brand_primary is not None, "Brand primary color missing in dark theme"

    ratio = contrast_ratio(btn_text, brand_primary)
    assert ratio >= 4.5, f"Dark mode brand button contrast {ratio:.2f}:1 fails WCAG AA (>= 4.5:1)"


@pytest.mark.unit
def test_light_mode_status_badge_contrast() -> None:
    """Status badge colors in light mode must provide at least 4.5:1 contrast on white and card containers."""
    light_vars = html_theme_options.get("light_css_variables", {})
    white_bg = "#ffffff"

    for token_name, tint_hex in [
        ("color-status-success", "#10b981"),
        ("color-status-warning", "#f59e0b"),
        ("color-status-danger", "#ef4444"),
    ]:
        color = light_vars.get(token_name)
        assert color is not None, f"Semantic token {token_name} missing in light theme"

        # On solid white
        ratio_white = contrast_ratio(color, white_bg)
        assert ratio_white >= 4.5, (
            f"Light mode {token_name} ({color}) on white is {ratio_white:.2f}:1, below 4.5:1"
        )

        # On 15% tinted badge background
        tint_bg = blend_tint(tint_hex, 0.15, white_bg)
        ratio_tint = contrast_ratio(color, tint_bg)
        assert ratio_tint >= 4.5, (
            f"Light mode {token_name} ({color}) on tinted bg {tint_bg} is {ratio_tint:.2f}:1, below 4.5:1"
        )


@pytest.mark.unit
def test_dark_mode_status_badge_contrast() -> None:
    """Status badge colors in dark mode must provide at least 4.5:1 contrast on card containers."""
    dark_vars = html_theme_options.get("dark_css_variables", {})
    card_bg = "#1a1c1e"  # Furo dark mode background-secondary

    for token_name, tint_hex in [
        ("color-status-success", "#10b981"),
        ("color-status-warning", "#f59e0b"),
        ("color-status-danger", "#ef4444"),
    ]:
        color = dark_vars.get(token_name)
        assert color is not None, f"Semantic token {token_name} missing in dark theme"

        # On card background
        ratio_card = contrast_ratio(color, card_bg)
        assert ratio_card >= 4.5, (
            f"Dark mode {token_name} ({color}) on card {card_bg} is {ratio_card:.2f}:1, below 4.5:1"
        )

        # On 15% tinted badge background
        tint_bg = blend_tint(tint_hex, 0.15, card_bg)
        ratio_tint = contrast_ratio(color, tint_bg)
        assert ratio_tint >= 4.5, (
            f"Dark mode {token_name} ({color}) on tinted bg {tint_bg} is {ratio_tint:.2f}:1, below 4.5:1"
        )


@pytest.mark.unit
def test_custom_css_rules_and_overrides() -> None:
    """Ensure custom.css uses semantic tokens and includes dark mode overrides."""
    css_content = (REPO_ROOT / "docs/_static/custom.css").read_text(encoding="utf-8")

    # Inaccessible hardcoded status colors must not appear
    for non_compliant_hex in ["#059669", "#d97706", "#dc2626"]:
        assert non_compliant_hex not in css_content, (
            f"Found deprecated non-compliant color {non_compliant_hex} in docs/_static/custom.css"
        )

    # Brand button text token must be consumed
    assert (
        "var(--color-brand-btn-text" in css_content
        or "var(--color-brand-button-text" in css_content
    )

    # Dark theme overrides for cards and table hover must exist
    assert 'body[data-theme="dark"] .role-card' in css_content
    assert 'body[data-theme="dark"] .explorer-dataframe-table' in css_content


@pytest.mark.unit
def test_vpat_document_conformance() -> None:
    """Ensure VPAT document reflects verified WCAG 2.1 AA conformance data."""
    vpat_content = (REPO_ROOT / "docs/reference/VPAT.rst").read_text(encoding="utf-8")

    assert "WCAG 2.1 Level AA" in vpat_content
    assert "4.5:1" in vpat_content
    assert "verified-wcag-aa" in vpat_content


@pytest.mark.unit
def test_showcase_metric_badges_aria_live_attributes() -> None:
    """Verify that docs/index.rst declares role='status', aria-live='polite', and aria-atomic='true' on all metric badges (WCAG 4.1.3, ARIA22)."""
    index_rst = (REPO_ROOT / "docs/index.rst").read_text(encoding="utf-8")

    badge_ids = ["ie-metric-badge", "cdm-metric-badge", "aud-metric-badge"]
    for badge_id in badge_ids:
        # Match tag with id and required ARIA attributes
        pattern = rf'<div[^>]*id="{badge_id}"[^>]*>'
        match = re.search(pattern, index_rst)
        assert match is not None, f"Badge {badge_id} not found in docs/index.rst"
        tag = match.group(0)
        assert 'role="status"' in tag, f"{badge_id} missing role=\"status\": {tag}"
        assert 'aria-live="polite"' in tag, f"{badge_id} missing aria-live=\"polite\": {tag}"
        assert 'aria-atomic="true"' in tag, f"{badge_id} missing aria-atomic=\"true\": {tag}"


@pytest.mark.unit
def test_thebe_pyodide_cell_status_aria_live_attributes() -> None:
    """Verify that thebe_pyodide.js configures .thebelab-cell-status with ARIA live region attributes."""
    thebe_js = (REPO_ROOT / "docs/_static/thebe_pyodide.js").read_text(encoding="utf-8")

    assert 'cellStatus.setAttribute("role", "status")' in thebe_js, (
        "cellStatus must set role='status'"
    )
    assert 'cellStatus.setAttribute("aria-live", "polite")' in thebe_js, (
        "cellStatus must set aria-live='polite'"
    )
    assert 'cellStatus.setAttribute("aria-atomic", "true")' in thebe_js, (
        "cellStatus must set aria-atomic='true'"
    )


@pytest.mark.unit
def test_thebe_pyodide_execution_feedback() -> None:
    """Verify that thebe_pyodide.js provides spoken feedback when execution starts and finishes."""
    thebe_js = (REPO_ROOT / "docs/_static/thebe_pyodide.js").read_text(encoding="utf-8")

    # Start announcement
    assert 'cellStatus.textContent = "Executing..."' in thebe_js, (
        "thebe_pyodide.js must announce start state"
    )

    # Completion announcement
    assert "Execution completed" in thebe_js, "thebe_pyodide.js must announce completion state"


@pytest.mark.unit
def test_showcase_mutation_handlers_target_live_containers() -> None:
    """Verify that showcase.js mutates textContent within decorated metric badges without stealing focus."""
    showcase_js = (REPO_ROOT / "docs/_static/showcase.js").read_text(encoding="utf-8")

    assert "ieMetricBadge.textContent =" in showcase_js
    assert "cdmMetricBadge.textContent =" in showcase_js
    assert "audMetricBadge.textContent =" in showcase_js

    # Trigger action handlers must not steal focus
    # (focus() should only be used for tab arrow navigation)
    for handler_name in [
        "executeIntegrationQuery",
        "executeCdmValidation",
        "executeAuditorVerification",
    ]:
        assert handler_name in showcase_js
