"""Automated accessibility and contrast ratio regression tests for documentation theme."""

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
def test_interactive_cells_contrast() -> None:
    """Verify interactive code cells meet WCAG AA (>= 4.5:1) in both themes."""
    light_vars = html_theme_options.get("light_css_variables", {})
    dark_vars = html_theme_options.get("dark_css_variables", {})

    # Run button contrast in light mode
    light_btn_bg = light_vars["color-brand-primary"]
    light_btn_text = light_vars["color-brand-btn-text"]
    light_btn_ratio = contrast_ratio(light_btn_text, light_btn_bg)
    assert light_btn_ratio >= 4.5, f"Light mode run button contrast {light_btn_ratio:.2f}:1 < 4.5:1"

    # Run button contrast in dark mode
    dark_btn_bg = dark_vars["color-brand-primary"]
    dark_btn_text = dark_vars["color-brand-btn-text"]
    dark_btn_ratio = contrast_ratio(dark_btn_text, dark_btn_bg)
    assert dark_btn_ratio >= 4.5, f"Dark mode run button contrast {dark_btn_ratio:.2f}:1 < 4.5:1"

    # Stderr contrast in light mode (8% tint over white page)
    light_stderr_color = light_vars["color-status-danger"]
    light_stderr_bg = blend_tint("#cf222e", 0.08, "#ffffff")
    light_err_ratio = contrast_ratio(light_stderr_color, light_stderr_bg)
    assert light_err_ratio >= 4.5, f"Light mode stderr contrast {light_err_ratio:.2f}:1 < 4.5:1"

    # Stderr contrast in dark mode (8% tint over dark container backgrounds #131416 and #1a1c1e)
    dark_stderr_color = dark_vars["color-status-danger"]
    for container_bg in ["#131416", "#1a1c1e"]:
        dark_stderr_bg = blend_tint("#cf222e", 0.08, container_bg)
        dark_err_ratio = contrast_ratio(dark_stderr_color, dark_stderr_bg)
        assert dark_err_ratio >= 4.5, (
            f"Dark mode stderr contrast {dark_err_ratio:.2f}:1 on {container_bg} < 4.5:1"
        )


@pytest.mark.unit
def test_interactive_cells_token_bindings() -> None:
    """Verify custom.css binds interactive cells to semantic tokens and js has no inline color."""
    css_content = (REPO_ROOT / "docs/_static/custom.css").read_text(encoding="utf-8")
    js_content = (REPO_ROOT / "docs/_static/thebe_pyodide.js").read_text(encoding="utf-8")

    # Ensure custom.css binds run button to --color-brand-btn-text
    assert "var(--color-brand-btn-text" in css_content

    # Ensure custom.css binds .thebe-stderr to --color-status-danger
    assert "var(--color-status-danger" in css_content

    # Ensure hardcoded inaccessible #cf222e is removed from both custom.css and thebe_pyodide.js
    assert "#cf222e" not in css_content, "Deprecated hardcoded #cf222e found in custom.css"
    assert "errPre.style.color" not in js_content, "Inline style override found in thebe_pyodide.js"
    assert "#cf222e" not in js_content, "Deprecated hardcoded #cf222e found in thebe_pyodide.js"

