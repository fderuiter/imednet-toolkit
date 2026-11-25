"""Automated accessibility and contrast ratio regression tests for documentation theme."""

import json
import re
import shutil
import subprocess
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

    # Visible focus styles for preformatted code blocks
    assert "pre:focus-visible" in css_content
    assert "outline: 2px solid var(--color-brand-primary)" in css_content


@pytest.mark.unit
def test_code_block_keyboard_accessibility() -> None:
    """Ensure preformatted code blocks receive keyboard accessibility attributes and focus styles."""
    css_content = (REPO_ROOT / "docs/_static/custom.css").read_text(encoding="utf-8")
    js_content = (REPO_ROOT / "docs/_static/showcase.js").read_text(encoding="utf-8")
    code_blocks_js = (REPO_ROOT / "docs/_static/code_blocks.js").read_text(encoding="utf-8")

    # Verify CSS focus-visible rule for preformatted code blocks and scrollable table wrappers
    assert "pre:focus-visible" in css_content
    assert ".table-wrapper:focus-visible" in css_content
    assert ".explorer-table-wrapper:focus-visible" in css_content
    assert "outline: 2px solid var(--color-brand-primary)" in css_content
    assert "window." not in code_blocks_js

    # Verify script logic detects pre and .table-wrapper elements and tests horizontal overflow
    for script_src in (js_content, code_blocks_js):
        assert "querySelectorAll" in script_src and "pre" in script_src
        assert ".table-wrapper" in script_src
        assert "data-table-region" in script_src
        assert "Scrollable data table" in script_src
        assert "scrollWidth >" in script_src and "clientWidth" in script_src
        assert "tabindex" in script_src and '"0"' in script_src
        assert "role" in script_src and '"region"' in script_src
        assert "aria-label" in script_src and '"Code snippet"' in script_src
        assert "resize" in script_src
        assert "removeAttribute" in script_src and "tabindex" in script_src


@pytest.mark.unit
def test_navigation_keyboard_css_and_controller_contracts() -> None:
    """Verify conf.py, custom.css, and navigation.js contracts for switches, drawers, and modindex."""
    from docs import conf as docs_conf

    assert "navigation.js" in docs_conf.html_js_files

    css_content = (REPO_ROOT / "docs/_static/custom.css").read_text(encoding="utf-8")
    nav_js = (REPO_ROOT / "docs/_static/navigation.js").read_text(encoding="utf-8")

    # 1. .toctree-checkbox transparent 32px switch override & Enter key handler
    assert "input.toctree-checkbox" in css_content
    assert "appearance: none" in css_content
    assert "32px" in css_content
    assert ".toctree-checkbox:focus-visible" in css_content
    assert "input.toctree-checkbox" in nav_js
    assert '"Enter"' in nav_js
    assert '"switch"' in nav_js

    # 2. Responsive drawer visibility guards (<= 82em TOC, <= 63em sidebar) & inert syncing
    assert "@media (max-width: 82em)" in css_content
    assert "#__toc:not(:checked) ~ .page .toc-drawer" in css_content
    assert "@media (max-width: 63em)" in css_content
    assert "#__navigation:not(:checked) ~ .page .sidebar-drawer" in css_content
    assert "visibility: hidden" in css_content
    assert "(max-width: 63em)" in nav_js
    assert "(max-width: 82em)" in nav_js
    assert ".inert" in nav_js
    assert ".nav-overlay-icon" in nav_js
    assert ".toc-header-icon:not(.no-toc)" in nav_js
    assert ".toc-content-icon:not(.no-toc)" in nav_js
    assert "aria-controls" in nav_js
    assert "aria-expanded" in nav_js
    assert '"Escape"' in nav_js

    # 3. py-modindex img.toggler upgrade & focus-visible / forced-colors rules
    assert "img.toggler" in nav_js
    assert "plus.png" in nav_js
    assert "img.toggler:focus-visible" in css_content
    assert ".nav-overlay-icon:focus-visible" in css_content
    assert ".toc-header-icon:focus-visible" in css_content
    assert ".toc-content-icon:focus-visible" in css_content

    # 4. Global #furo-main-content tabindex='-1', modal drawer focus trap, & globalThis purity
    assert "code_blocks.js" in docs_conf.html_js_files
    assert "furo-main-content" in nav_js
    assert "a.back-to-top" in nav_js
    assert "data-btt-bound" in nav_js
    assert "preventScroll: true" in nav_js
    assert "getOpenModalDrawer" in nav_js
    assert '"Tab"' in nav_js and "shiftKey" in nav_js
    assert "window." not in nav_js


@pytest.mark.unit
def test_headerlink_and_viewcode_css_contracts() -> None:
    """Verify custom.css overrides Furo headerlink visibility, [source] float collapse, and 4-mode focus rings."""
    import re

    css_content = (REPO_ROOT / "docs/_static/custom.css").read_text(encoding="utf-8")

    # 1. Override Furo's visibility: hidden across headings, definitions, and captions with opacity: 0 -> 1
    for selector in (
        "a.headerlink",
        ".code-block-caption > .headerlink",
        "dl dt > .headerlink",
        "figcaption p > .headerlink",
        "h1 > .headerlink",
        "h6 > .headerlink",
        "p.caption > .headerlink",
        "table > caption > .headerlink",
    ):
        assert selector in css_content

    for reveal_selector in (
        "a.headerlink:hover",
        "a.headerlink:focus",
        "a.headerlink:focus-visible",
        "dl dt:focus-within > .headerlink",
        "h1:focus-within > .headerlink",
        "p.caption:focus-within > .headerlink",
        "table > caption:focus-within > .headerlink",
    ):
        assert reveal_selector in css_content

    # 2. Separate .viewcode-source-link and a.reference:has(> .viewcode-link) rule blocks
    #    so engines without :has() never drop the .viewcode-source-link rule
    rule_headers = [
        block.rsplit("{", 1)[-1] if "{" in block else block for block in css_content.split("}")
    ]
    for header in rule_headers:
        if ".viewcode-source-link" in header:
            assert ":has(" not in header, (
                f".viewcode-source-link must not share a selector list with :has(): {header!r}"
            )

    assert re.search(
        r"\.viewcode-source-link\s*\{[^}]*float:\s*right;[^}]*text-indent:\s*0;",
        css_content,
        re.DOTALL,
    )
    assert re.search(
        r"a\.reference:has\(>\s*\.viewcode-link\)\s*\{[^}]*float:\s*right;[^}]*text-indent:\s*0;",
        css_content,
        re.DOTALL,
    )
    assert re.search(
        r"\.viewcode-source-link\s*>\s*\.viewcode-link[^{]*\{[^}]*float:\s*none;[^}]*width:\s*auto;",
        css_content,
        re.DOTALL,
    )
    assert re.search(
        r"a\.reference:has\(>\s*\.viewcode-link\)\s*>\s*\.viewcode-link[^{]*\{[^}]*float:\s*none;[^}]*width:\s*auto;",
        css_content,
        re.DOTALL,
    )

    # 3. 2px :focus-visible outlines across default, prefers-contrast: more, and forced-colors: active
    for focus_sel in (
        "a.headerlink:focus-visible",
        ".viewcode-source-link:focus-visible",
        "a.reference:has(> .viewcode-link):focus-visible",
        "a.viewcode-back:focus-visible",
    ):
        assert css_content.count(focus_sel) >= 3, (
            f"Expected {focus_sel} in base, prefers-contrast: more, and forced-colors: active blocks"
        )


@pytest.mark.unit
def test_vpat_document_conformance() -> None:
    """Ensure VPAT document and showcase mock reflect community ownership and verified capabilities."""
    from imednet_tui.app import ImednetTuiApp

    vpat_content = (REPO_ROOT / "docs/reference/VPAT.rst").read_text(encoding="utf-8")
    showcase_js = (REPO_ROOT / "docs/_static/showcase.js").read_text(encoding="utf-8")

    assert "WCAG 2.1 Level AA" in vpat_content
    assert "4.5:1" in vpat_content
    assert "7:1" in vpat_content
    assert "Unofficial Open-Source" in vpat_content
    assert "Mednet Solutions" in vpat_content
    assert "https://github.com/fderuiter/imednet-toolkit/issues" in vpat_content
    assert "accessibility@imednet.com" not in vpat_content
    assert "verified-wcag-aa" not in vpat_content

    assert "https://github.com/fderuiter/imednet-toolkit/issues" in showcase_js
    assert "Unofficial open-source imednet-toolkit" in showcase_js
    assert "accessibility@imednet.com" not in showcase_js
    assert "verified-wcag-aa" not in showcase_js
    assert "modal focus management" not in showcase_js

    assert "imednet tui" in vpat_content
    assert "--high-contrast" in vpat_content
    assert "IMEDNET_HIGH_CONTRAST=1" in vpat_content
    assert "IMEDNET_NON_INTERACTIVE=1" in vpat_content
    assert "InteractiveDataGrid" in vpat_content

    for binding in ImednetTuiApp.BINDINGS:
        key = binding[0] if isinstance(binding, tuple) else binding.key
        assert f"``{key}``" in vpat_content, f"TUI binding '{key}' missing from VPAT.rst"

    for token in (
        "Left",
        "Right",
        "``h``",
        "``l``",
        "``[``",
        "``]``",
        "``{``",
        "``}``",
        "--no-interactive",
        "imednet tui --no-interactive",
    ):
        assert token in vpat_content, f"Expected '{token}' in docs/reference/VPAT.rst"


@pytest.mark.unit
def test_cli_and_plugins_docs_tui_accessibility_coverage() -> None:
    """Verify CLI and plugin guides document TUI commands, grid controls, and accessibility flags."""
    cli_rst = (REPO_ROOT / "docs/how-to/cli.rst").read_text(encoding="utf-8")
    for token in (
        "imednet tui",
        "imednet tui --no-interactive",
        "--no-interactive",
        "--high-contrast",
        "InteractiveDataGrid",
        "IMEDNET_NON_INTERACTIVE=1",
        "--interactive",
        "Left",
        "Right",
        "``h``",
        "``l``",
        "``[``",
        "``]``",
        "``{``",
        "``}``",
    ):
        assert token in cli_rst, f"Expected '{token}' in docs/how-to/cli.rst"

    plugins_rst = (REPO_ROOT / "docs/explanation/plugins.rst").read_text(encoding="utf-8")
    assert "/reference/api/plugins-tui" in plugins_rst
    assert "imednet.cli_plugins" in plugins_rst
    assert 'tui = "imednet_tui.cli:setup_parser"' in plugins_rst


_TERMINAL_PALETTES: dict[str, dict[str, dict[str, str]]] = {
    "XTerm": {
        "dark": {
            "fg": "#E5E5E5",
            "bg": "#000000",
            "0": "#000000",
            "7": "#E5E5E5",
            "8": "#7F7F7F",
            "15": "#FFFFFF",
        },
        "light": {
            "fg": "#000000",
            "bg": "#FFFFFF",
            "0": "#000000",
            "7": "#E5E5E5",
            "8": "#7F7F7F",
            "15": "#FFFFFF",
        },
    },
    "VGA": {
        "dark": {
            "fg": "#AAAAAA",
            "bg": "#000000",
            "0": "#000000",
            "7": "#AAAAAA",
            "8": "#555555",
            "15": "#FFFFFF",
        },
        "light": {
            "fg": "#000000",
            "bg": "#FFFFFF",
            "0": "#000000",
            "7": "#AAAAAA",
            "8": "#555555",
            "15": "#FFFFFF",
        },
    },
    "Windows_Campbell": {
        "dark": {
            "fg": "#CCCCCC",
            "bg": "#0C0C0C",
            "0": "#0C0C0C",
            "7": "#CCCCCC",
            "8": "#767676",
            "15": "#F2F2F2",
        },
        "light": {
            "fg": "#0C0C0C",
            "bg": "#FFFFFF",
            "0": "#0C0C0C",
            "7": "#CCCCCC",
            "8": "#767676",
            "15": "#F2F2F2",
        },
    },
    "GNOME_Tango": {
        "dark": {
            "fg": "#D3D7CF",
            "bg": "#2E3436",
            "0": "#2E3436",
            "7": "#D3D7CF",
            "8": "#555753",
            "15": "#EEEEEC",
        },
        "light": {
            "fg": "#2E3436",
            "bg": "#FFFFFF",
            "0": "#2E3436",
            "7": "#D3D7CF",
            "8": "#555753",
            "15": "#EEEEEC",
        },
    },
    "VSCode_Integrated": {
        "dark": {
            "fg": "#CCCCCC",
            "bg": "#1E1E1E",
            "0": "#000000",
            "7": "#E5E5E5",
            "8": "#666666",
            "15": "#E5E5E5",
        },
        "light": {
            "fg": "#333333",
            "bg": "#FFFFFF",
            "0": "#000000",
            "7": "#555555",
            "8": "#666666",
            "15": "#A5A5A5",
        },
    },
}


def _resolve_sgr_colors(rendered: str, palette: dict[str, str]) -> tuple[str, str]:
    """Resolve effective foreground and background hex colors from an ANSI SGR-wrapped string."""
    match = re.match(r"^\x1b\[([0-9;]*)m", rendered)
    assert match is not None, f"Expected leading ANSI SGR sequence in {rendered!r}"
    params = [int(p) for p in match.group(1).split(";") if p]
    fg_hex = palette["fg"]
    bg_hex = palette["bg"]
    idx = 0
    while idx < len(params):
        code = params[idx]
        if code == 38 and idx + 4 < len(params) and params[idx + 1] == 2:
            r, g, b = params[idx + 2], params[idx + 3], params[idx + 4]
            fg_hex = f"#{r:02X}{g:02X}{b:02X}"
            idx += 5
            continue
        if code == 48 and idx + 4 < len(params) and params[idx + 1] == 2:
            r, g, b = params[idx + 2], params[idx + 3], params[idx + 4]
            bg_hex = f"#{r:02X}{g:02X}{b:02X}"
            idx += 5
            continue
        if 30 <= code <= 37:
            fg_hex = palette[str(code - 30)]
        elif 90 <= code <= 97:
            fg_hex = palette[str(code - 90 + 8)]
        elif 40 <= code <= 47:
            bg_hex = palette[str(code - 40)]
        elif 100 <= code <= 107:
            bg_hex = palette[str(code - 100 + 8)]
        idx += 1
    return fg_hex, bg_hex


@pytest.mark.unit
@pytest.mark.parametrize(
    "palette_name",
    ["XTerm", "VGA", "Windows_Campbell", "GNOME_Tango", "VSCode_Integrated"],
)
@pytest.mark.parametrize("theme_mode", ["light", "dark"])
@pytest.mark.parametrize("term_value", ["xterm-256color", "xterm-16color"])
def test_cli_high_contrast_status_badges_meet_wcag_aaa_across_terminal_palettes(
    monkeypatch: pytest.MonkeyPatch,
    palette_name: str,
    theme_mode: str,
    term_value: str,
) -> None:
    """Verify >= 7:1 contrast for all high-contrast status tokens across light and dark terminal palettes."""
    from imednet.cli.utils.output import _HC_STATUS_COLOR_MAP, _colorize

    palette = _TERMINAL_PALETTES[palette_name][theme_mode]
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    monkeypatch.setenv("TERM", term_value)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("imednet.cli.utils.output.supports_ansi_color", lambda _stream=None: True)

    assert _HC_STATUS_COLOR_MAP, "Expected non-empty _HC_STATUS_COLOR_MAP"
    for status_token, color_key in _HC_STATUS_COLOR_MAP.items():
        rendered = _colorize(status_token.upper(), color_key)
        fg_hex, bg_hex = _resolve_sgr_colors(rendered, palette)
        ratio = contrast_ratio(fg_hex, bg_hex)
        assert ratio >= 7.0, (
            f"High-contrast token {status_token!r} ({color_key}) on {palette_name} ({theme_mode}, "
            f"TERM={term_value}) rendered {fg_hex} on {bg_hex} with {ratio:.2f}:1 (< 7.0:1)"
        )


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
def test_thebe_pyodide_mixed_output_announces_failure() -> None:
    """Verify that cell execution failures are announced even when stdout was printed prior to exception (WCAG 4.1.3)."""
    thebe_js = (REPO_ROOT / "docs/_static/thebe_pyodide.js").read_text(encoding="utf-8")

    # 1. Error tracking in Python runner scripts
    assert '"error": bool(_tb_str)' in thebe_js, (
        "thebe_pyodide runner scripts must track error status independently of output buffers"
    )

    # 2. Status selection must not suppress errors when stdout or result is present
    assert "!res.stdout" not in thebe_js, (
        "thebe_pyodide status logic must not gate failure announcement on absence of stdout"
    )
    assert "!res.result" not in thebe_js, (
        "thebe_pyodide status logic must not gate failure announcement on absence of result"
    )
    assert 'cellStatus.textContent = "Execution completed with errors."' in thebe_js

    # 3. Functional verification of the Python runner template with print-then-raise code
    # Simulate execution of user code that outputs stdout before raising an exception
    runner_code = """
import sys, ast, traceback
from io import StringIO

_stdout_buf = StringIO()
_stderr_buf = StringIO()
_old_out = sys.stdout
_old_err = sys.stderr
sys.stdout = _stdout_buf
sys.stderr = _stderr_buf
_eval_res = None
_tb_str = ""

try:
    _parsed = ast.parse(__thebe_user_code__)
    if _parsed.body and isinstance(_parsed.body[-1], ast.Expr):
        _last_expr = ast.Expression(_parsed.body.pop().value)
        _exec_code = compile(_parsed, '<stdin>', 'exec')
        _eval_code = compile(_last_expr, '<stdin>', 'eval')
        exec(_exec_code, globals())
        _eval_res = eval(_eval_code, globals())
    else:
        _exec_code = compile(__thebe_user_code__, '<stdin>', 'exec')
        exec(_exec_code, globals())
except Exception:
    _tb_str = traceback.format_exc()
finally:
    sys.stdout = _old_out
    sys.stderr = _old_err

__result__ = {
    "stdout": _stdout_buf.getvalue(),
    "stderr": _stderr_buf.getvalue() + _tb_str,
    "result": repr(_eval_res) if _eval_res is not None else None,
    "error": bool(_tb_str),
}
"""
    env = {"__thebe_user_code__": "print('Starting validation')\nraise ValueError('Invalid')"}
    exec(runner_code, env)
    result = env["__result__"]

    assert "Starting validation" in result["stdout"], "stdout should capture print statement"
    assert "ValueError: Invalid" in result["stderr"], "stderr should capture exception traceback"
    assert result["error"] is True, "error flag must be True for raised exceptions"

    # 4. Verify decision logic selects failure announcement given mixed output,
    # and respects explicit error: False when stderr contains ordinary warnings
    def evaluate_decision(res: dict) -> str:
        has_error = (
            bool(res["error"])
            if ("error" in res and res["error"] is not None)
            else bool(res.get("stderr") and str(res["stderr"]).strip())
        )
        return "Execution completed with errors." if has_error else "Execution completed."

    assert evaluate_decision(result) == "Execution completed with errors."

    # 5. Verify both extracted async runners from thebe_pyodide.js for warning output,
    #    print-then-raise, SystemExit capture, and asyncio.run() bridging in an active loop.
    import ast
    import asyncio
    import inspect

    assert "runPythonAsync(runnerScript)" in thebe_js, (
        "thebe_pyodide.js must execute cells asynchronously via pyodide.runPythonAsync()"
    )
    assert (
        "pyodide.runPython(" not in thebe_js and "mainThreadPyodide.runPython(" not in thebe_js
    ), "Synchronous runPython() must be replaced with runPythonAsync()"

    extracted_runners = re.findall(r"runnerScript\s*=\s*\\?`([\s\S]*?)\\?`;", thebe_js)
    assert len(extracted_runners) >= 2, (
        "Expected worker and main-thread fallback runner scripts in thebe_pyodide.js"
    )

    async def _exec_extracted_runner(runner_template: str, user_code: str) -> dict:
        script = runner_template.strip()
        script_body, dict_tail = script.rsplit("{", 1)
        dict_body = "{" + dict_tail
        env_ns: dict = {"__thebe_user_code__": user_code, "__name__": "__main__"}
        compiled_body = compile(
            ast.parse(script_body),
            "<runner>",
            "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
        coro_or_none = eval(compiled_body, env_ns)
        if inspect.iscoroutine(coro_or_none):
            await coro_or_none
        return eval(dict_body, env_ns)

    for i, runner_template in enumerate(extracted_runners):
        # Case A: Successful execution with stderr output (e.g. library warnings)
        res_warn = asyncio.run(
            _exec_extracted_runner(
                runner_template, "import sys; print('warning', file=sys.stderr); 42"
            )
        )
        assert res_warn["error"] is False, (
            f"Runner {i} must return error: False for warning-only stderr"
        )
        assert "warning" in res_warn["stderr"]
        assert res_warn["result"] == "42"
        assert evaluate_decision(res_warn) == "Execution completed."

        # Case B: Execution with print before exception
        res_raise = asyncio.run(
            _exec_extracted_runner(
                runner_template, "print('Starting validation')\nraise ValueError('Invalid')"
            )
        )
        assert res_raise["error"] is True, f"Runner {i} must return error: True for exception"
        assert "Starting validation" in res_raise["stdout"]
        assert "ValueError: Invalid" in res_raise["stderr"]
        assert evaluate_decision(res_raise) == "Execution completed with errors."

        # Case C: Execution calling sys.exit(1) preserves buffered stdout and stderr
        res_exit = asyncio.run(
            _exec_extracted_runner(
                runner_template,
                "import sys\nprint('Before exit')\nprint('Error: missing key', file=sys.stderr)\nsys.exit(1)",
            )
        )
        assert res_exit["error"] is True, f"Runner {i} must return error: True for sys.exit(1)"
        assert "Before exit" in res_exit["stdout"]
        assert "Error: missing key" in res_exit["stderr"]
        assert "SystemExit: 1" in res_exit["stderr"]
        assert evaluate_decision(res_exit) == "Execution completed with errors."

        # Case D: Execution calling sys.exit(0) preserves output without error status
        res_exit_ok = asyncio.run(
            _exec_extracted_runner(
                runner_template,
                "import sys\nprint('Clean completion')\nsys.exit(0)",
            )
        )
        assert res_exit_ok["error"] is False, f"Runner {i} must return error: False for sys.exit(0)"
        assert "Clean completion" in res_exit_ok["stdout"]
        assert evaluate_decision(res_exit_ok) == "Execution completed."

        # Case E: Execution calling asyncio.run(main()) inside an active event loop
        res_async = asyncio.run(
            _exec_extracted_runner(
                runner_template,
                "import asyncio\n"
                "async def main():\n"
                "    await asyncio.sleep(0)\n"
                "    print('Async tutorial ok')\n"
                "if __name__ == '__main__':\n"
                "    asyncio.run(main())\n",
            )
        )
        assert res_async["error"] is False, (
            f"Runner {i} failed on asyncio.run(main()): {res_async['stderr']}"
        )
        assert "Async tutorial ok" in res_async["stdout"]
        assert evaluate_decision(res_async) == "Execution completed."

    # 6. Execute real JavaScript decision block and getConfig() in Node if available
    node_bin = shutil.which("node")
    if node_bin:
        match = re.search(
            r"executeCode\(code,\s*\(res\)\s*=>\s*\{([\s\S]*?)\n\s*outputContent\.innerHTML",
            thebe_js,
        )
        assert match is not None, "Could not extract executeCode callback from thebe_pyodide.js"
        callback_body = match.group(1)
        node_script = f"""
        const fs = require("fs");
        const callbackBody = {json.dumps(callback_body)};
        function testDecision(res) {{
            let runBtn = {{ disabled: false, innerHTML: "" }};
            let cellStatus = {{ textContent: "" }};
            const fn = new Function('res', 'runBtn', 'cellStatus', callbackBody);
            fn(res, runBtn, cellStatus);
            return cellStatus.textContent;
        }}
        const resWarn = {{ stdout: "", stderr: "warning\\n", result: "42", error: false }};
        const resRaise = {{ stdout: "Starting validation\\n", stderr: "ValueError: Invalid\\n", result: null, error: true }};
        const resFallbackWarn = {{ stdout: "", stderr: "warning\\n", result: "42" }};

        if (testDecision(resWarn) !== "Execution completed.") {{
            throw new Error("Expected Execution completed. for warning, got: " + testDecision(resWarn));
        }}
        if (testDecision(resRaise) !== "Execution completed with errors.") {{
            throw new Error("Expected Execution completed with errors. for raise, got: " + testDecision(resRaise));
        }}
        if (testDecision(resFallbackWarn) !== "Execution completed with errors.") {{
            throw new Error("Expected Execution completed with errors. for fallback warning, got: " + testDecision(resFallbackWarn));
        }}

        // Verify getConfig() prefers data-content_root over DOCUMENTATION_OPTIONS.URL_ROOT
        // and parses #imednet-pyodide-config JSON payload cleanly when URL_ROOT is absent.
        const thebeSource = fs.readFileSync({json.dumps(str(REPO_ROOT / "docs/_static/thebe_pyodide.js"))}, "utf8");
        const vm = require("vm");
        const sandbox = {{
            URL,
            console,
            window: {{ location: {{ href: "https://example.org/docs/tutorials/quick_start.html" }} }},
            document: {{
                readyState: "complete",
                documentElement: {{
                    getAttribute: (attr) => attr === "data-content_root" ? "../" : null
                }},
                getElementById: (id) => id === "imednet-pyodide-config" ? ({{
                    textContent: JSON.stringify({{
                        wheels: [
                            "_static/wheels/imednet-1.1.0-py3-none-any.whl",
                            "_static/wheels/imednet_workflows-1.1.0-py3-none-any.whl",
                            "_static/wheels/imednet_plugins_sinks-1.0.1-py3-none-any.whl",
                            "_static/wheels/imednet_tui-1.0.0-py3-none-any.whl"
                        ]
                    }})
                }}) : null,
                querySelectorAll: () => []
            }}
        }};
        sandbox.window.document = sandbox.document;
        vm.runInNewContext(thebeSource, sandbox);
        const cfg = sandbox.window.pyodideRuntime.getConfig();
        if (cfg.wheels.length !== 3) {{
            throw new Error("Expected 3 filtered wheels, got: " + JSON.stringify(cfg.wheels));
        }}
        if (cfg.wheels[0] !== "https://example.org/docs/_static/wheels/imednet-1.1.0-py3-none-any.whl") {{
            throw new Error("Unexpected wheel URL via data-content_root: " + cfg.wheels[0]);
        }}
        if (cfg.docRootUrl !== "https://example.org/docs/") {{
            throw new Error("Unexpected docRootUrl: " + cfg.docRootUrl);
        }}
        if (cfg.wheelManifestUrl !== "https://example.org/docs/_static/wheels/manifest.json") {{
            throw new Error("Unexpected wheelManifestUrl: " + cfg.wheelManifestUrl);
        }}

        // Verify standalone page without inline #imednet-pyodide-config returns empty synchronous wheels
        // and fetches _static/wheels/manifest.json in fallbackToMainThread / worker runtime.
        (async () => {{
            const installedMain = [];
            let createdScript = null;
            const standaloneSandbox = {{
                URL,
                console,
                fetch: async (url) => {{
                    if (url !== "https://example.org/docs/_static/wheels/manifest.json") {{
                        throw new Error("Unexpected manifest fetch URL: " + url);
                    }}
                    return {{
                        ok: true,
                        status: 200,
                        json: async () => ({{
                            wheels: [
                                "_static/wheels/imednet-1.2.0-py3-none-any.whl",
                                "_static/wheels/imednet_workflows-1.2.0-py3-none-any.whl",
                                "_static/wheels/imednet_plugins_sinks-1.1.0-py3-none-any.whl",
                                "_static/wheels/imednet_tui-1.0.0-py3-none-any.whl"
                            ]
                        }})
                    }};
                }},
                window: {{
                    location: {{ href: "https://example.org/docs/tutorials/standalone.html" }},
                    loadPyodide: async () => ({{
                        loadPackage: async () => {{}},
                        toPy: (v) => ({{ destroy: () => {{}}, val: v }}),
                        globals: {{ set: () => {{}}, delete: () => {{}} }},
                        runPythonAsync: async (code) => {{
                            const m = code.match(/micropip\\.install\\("([^"]+)"/);
                            if (m) installedMain.push(m[1]);
                        }}
                    }})
                }},
                document: {{
                    readyState: "complete",
                    documentElement: {{
                        getAttribute: (attr) => attr === "data-content_root" ? "../" : null
                    }},
                    getElementById: () => null,
                    querySelectorAll: () => [],
                    createElement: () => {{
                        createdScript = {{}};
                        return createdScript;
                    }},
                    head: {{
                        appendChild: (s) => {{
                            if (s && typeof s.onload === "function") s.onload();
                        }}
                    }}
                }}
            }};
            standaloneSandbox.window.document = standaloneSandbox.document;
            vm.runInNewContext(thebeSource, standaloneSandbox);
            const standaloneCfg = standaloneSandbox.window.pyodideRuntime.getConfig();
            if (standaloneCfg.wheels.length !== 0 || standaloneCfg.hasInlineWheels !== false) {{
                throw new Error("Expected empty inline wheels on standalone page, got: " + JSON.stringify(standaloneCfg));
            }}
            if (standaloneCfg.wheelManifestUrl !== "https://example.org/docs/_static/wheels/manifest.json") {{
                throw new Error("Unexpected standalone wheelManifestUrl: " + standaloneCfg.wheelManifestUrl);
            }}
            standaloneSandbox.window.initPyodide();
            await new Promise((resolve) => setTimeout(resolve, 25));
            const expectedInstalled = [
                "https://example.org/docs/_static/wheels/imednet-1.2.0-py3-none-any.whl",
                "https://example.org/docs/_static/wheels/imednet_workflows-1.2.0-py3-none-any.whl",
                "https://example.org/docs/_static/wheels/imednet_plugins_sinks-1.1.0-py3-none-any.whl"
            ];
            if (JSON.stringify(installedMain) !== JSON.stringify(expectedInstalled)) {{
                throw new Error("Unexpected installed manifest wheels: " + JSON.stringify(installedMain));
            }}

            // Also verify Web Worker runtime fetches _static/wheels/manifest.json when inline wheels are absent.
            const installedWorker = [];
            let capturedWorkerSource = "";
            let workerReady = false;
            const workerHostSandbox = {{
                URL: Object.assign(function(u, b) {{ return new URL(u, b); }}, {{
                    createObjectURL: (blobObj) => {{
                        capturedWorkerSource = blobObj.parts.join("");
                        return "blob:pyodide-worker";
                    }}
                }}),
                Blob: function(parts) {{ this.parts = parts; }},
                Worker: function() {{
                    const workerSelf = this;
                    const workerCtx = {{
                        URL,
                        console,
                        fetch: standaloneSandbox.fetch,
                        importScripts: () => {{}},
                        loadPyodide: async () => ({{
                            loadPackage: async () => {{}},
                            toPy: (v) => ({{ destroy: () => {{}}, val: v }}),
                            globals: {{ set: () => {{}}, delete: () => {{}} }},
                            runPythonAsync: async (code) => {{
                                const m = code.match(/micropip\\.install\\("([^"]+)"/);
                                if (m) installedWorker.push(m[1]);
                            }}
                        }}),
                        postMessage: (msg) => {{
                            if (msg && msg.type === "status" && msg.status === "ready") {{
                                workerReady = true;
                            }}
                            if (typeof workerSelf.onmessage === "function") {{
                                workerSelf.onmessage({{ data: msg }});
                            }}
                        }}
                    }};
                    vm.runInNewContext(capturedWorkerSource, workerCtx);
                    this.postMessage = (data) => workerCtx.onmessage({{ data }});
                }},
                console,
                window: {{ location: {{ href: "https://example.org/docs/tutorials/standalone.html" }} }},
                document: {{
                    readyState: "complete",
                    documentElement: {{
                        getAttribute: (attr) => attr === "data-content_root" ? "../" : null
                    }},
                    getElementById: () => null,
                    querySelectorAll: () => []
                }}
            }};
            workerHostSandbox.window.document = workerHostSandbox.document;
            vm.runInNewContext(thebeSource, workerHostSandbox);
            workerHostSandbox.window.initPyodide();
            await new Promise((resolve) => setTimeout(resolve, 25));
            if (!workerReady || JSON.stringify(installedWorker) !== JSON.stringify(expectedInstalled)) {{
                throw new Error("Unexpected worker manifest wheels: " + JSON.stringify({{ workerReady, installedWorker }}));
            }}
        }})().catch((err) => {{
            console.error(err);
            process.exit(1);
        }});
        """
        proc = subprocess.run([node_bin, "-e", node_script], capture_output=True, text=True)
        assert proc.returncode == 0, (
            f"Node verification of JavaScript runtime failed:\n{proc.stderr}"
        )


@pytest.mark.unit
def test_accessible_html5_translator_toctree_caption_aria_levels(tmp_path: Path) -> None:
    """Verify _AccessibleHTML5Translator emits contextual aria-level on toctree captions (WCAG 4.1.2, 1.3.1, ARIA12, G141)."""
    from docs import conf as docs_conf
    from docutils import nodes
    from docutils.utils import new_document
    from furo.navigation import get_navigation_tree
    from sphinx import addnodes
    from sphinx.application import Sphinx
    from sphinx.util.docutils import docutils_namespace

    # 1. Verify setup(app) registers _AccessibleHTML5Translator for both html and dirhtml
    class _TranslatorAppStub:
        def __init__(self) -> None:
            self.translators: dict[str, object] = {}

        def set_translator(self, name: str, translator_class: object, **_kwargs: object) -> None:
            self.translators[name] = translator_class

        def add_autodocumenter(self, *_args: object, **_kwargs: object) -> None:
            pass

        def connect(self, *_args: object, **_kwargs: object) -> None:
            pass

    stub_app = _TranslatorAppStub()
    docs_conf.setup(stub_app)
    assert stub_app.translators["html"] is docs_conf._AccessibleHTML5Translator
    assert stub_app.translators["dirhtml"] is docs_conf._AccessibleHTML5Translator

    # 2. Spin up minimal Sphinx html and dirhtml apps wired with docs/conf.py translator
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "conf.py").write_text(
        'project = "test"\nhtml_theme = "furo"\nmaster_doc = "index"\n',
        encoding="utf-8",
    )
    (src_dir / "index.rst").write_text("Root\n====\n", encoding="utf-8")

    def _make_toctree_caption(text: str) -> addnodes.compact_paragraph:
        cp = addnodes.compact_paragraph(toctree=True)
        cp += nodes.title("", text)
        return cp

    with docutils_namespace():
        dirhtml_app = Sphinx(
            srcdir=str(src_dir),
            confdir=str(src_dir),
            outdir=str(tmp_path / "out_dirhtml"),
            doctreedir=str(tmp_path / "doctrees_dirhtml"),
            buildername="dirhtml",
        )
        dirhtml_app.set_translator("dirhtml", docs_conf._AccessibleHTML5Translator)
        assert dirhtml_app.builder.get_translator_class() is docs_conf._AccessibleHTML5Translator
        dirhtml_partial = dirhtml_app.builder.render_partial(_make_toctree_caption("Reference:"))[
            "fragment"
        ]
        assert 'aria-level="2"' in dirhtml_partial

    with docutils_namespace():
        app = Sphinx(
            srcdir=str(src_dir),
            confdir=str(src_dir),
            outdir=str(tmp_path / "out_html"),
            doctreedir=str(tmp_path / "doctrees_html"),
            buildername="html",
        )
        app.set_translator("html", docs_conf._AccessibleHTML5Translator)
        assert app.builder.get_translator_class() is docs_conf._AccessibleHTML5Translator

        # 3. Partial sidebar rendering (section_level == 0 -> aria-level="2") + Furo sidebar tree
        sidebar_partial = app.builder.render_partial(_make_toctree_caption("Tutorials:"))[
            "fragment"
        ]
        assert (
            '<p aria-level="2" class="caption" role="heading"><span class="caption-text">Tutorials:</span></p>'
            in sidebar_partial
        )
        furo_sidebar_html = get_navigation_tree(sidebar_partial)
        assert 'role="heading"' in furo_sidebar_html
        assert 'aria-level="2"' in furo_sidebar_html

        # 4. Full document hierarchy: h1 section -> 2, h2 section -> 3, deep sections -> clamped to 6,
        #    plus topic/admonition and table captions delegated cleanly to super().visit_title
        app.builder.prepare_writing({"index"})
        app.builder.current_docname = "index"
        app.builder.secnumbers = {"#h2-sec": (1, 1)}
        app.builder.fignumbers = {"table": {"tbl-1": (1, 1)}}

        doc = new_document("index", app.builder.docsettings)

        sec_h1 = nodes.section(ids=["h1-sec"])
        sec_h1 += nodes.title("", "Contributing")
        sec_h1 += _make_toctree_caption("Contributor Guides")

        sec_h2 = nodes.section(ids=["h2-sec"])
        sec_h2 += nodes.title("", "Documentation Overview")
        sec_h2 += _make_toctree_caption("How-to Guides:")

        # Nest down to depth 7 (section_level 7 + initial_header_level 1 = 8 -> clamped to 6)
        parent_sec = sec_h2
        for depth in range(3, 8):
            nested = nodes.section(ids=[f"sec-{depth}"])
            nested += nodes.title("", f"Heading {depth}")
            if depth in (5, 6, 7):
                nested += _make_toctree_caption(f"Deep Caption {depth}")
            parent_sec += nested
            parent_sec = nested

        # Non-toctree topic title must not get role="heading" or aria-level
        topic_node = nodes.topic()
        topic_node += nodes.title("", "Topic Summary")
        sec_h1 += topic_node

        # Table with title must preserve caption-text wrapper and fignumber without aria-level
        table = nodes.table(ids=["tbl-1"])
        table += nodes.title("", "API Stability Matrix")
        tgroup = nodes.tgroup(cols=1)
        tgroup += nodes.colspec(colwidth=1)
        tbody = nodes.tbody()
        row = nodes.row()
        entry = nodes.entry()
        entry += nodes.paragraph(text="Stable")
        row += entry
        tbody += row
        tgroup += tbody
        table += tgroup
        sec_h1 += table

        sec_h1 += sec_h2
        doc += sec_h1

        translator = docs_conf._AccessibleHTML5Translator(doc, app.builder)
        doc.walkabout(translator)
        rendered = "".join(translator.body)

    assert (
        '<p aria-level="2" class="caption" role="heading"><span class="caption-text">Contributor Guides</span></p>'
        in rendered
    )
    assert (
        '<p aria-level="3" class="caption" role="heading"><span class="caption-text">How-to Guides:</span></p>'
        in rendered
    )
    for depth in (5, 6, 7):
        assert (
            f'<p aria-level="6" class="caption" role="heading"><span class="caption-text">Deep Caption {depth}</span></p>'
            in rendered
        )

    assert '<p class="topic-title">Topic Summary</p>' in rendered
    # Section numbering and figure numbering stay intact without duplication
    assert rendered.count('<span class="section-number">1.1. </span>') == 1
    assert rendered.count('<span class="caption-number">Table 1.1 </span>') == 1
    assert (
        '<caption><span class="caption-number">Table 1.1 </span><span class="caption-text">API Stability Matrix</span>'
        in rendered
    )
    assert (
        "<caption>" in rendered
        and "aria-level" not in rendered.split("<caption>", 1)[1].split("</caption>", 1)[0]
    )


@pytest.mark.unit
def test_accessible_translator_link_labels_and_viewcode_backlinks(tmp_path: Path) -> None:
    """Verify target-specific aria-labels on a.headerlink, [source], and [docs] links."""
    from docs import conf as docs_conf
    from docutils import nodes
    from docutils.utils import new_document
    from sphinx import addnodes
    from sphinx.application import Sphinx
    from sphinx.util.docutils import docutils_namespace

    srcdir = tmp_path / "src_links"
    srcdir.mkdir()
    (srcdir / "conf.py").write_text(
        'project = "test"\nextensions = []\nhtml_theme = "furo"\nmaster_doc = "index"\nhtml_permalinks = True\n',
        encoding="utf-8",
    )
    (srcdir / "index.rst").write_text("Title\n=====\n", encoding="utf-8")

    with docutils_namespace():
        app = Sphinx(
            srcdir=str(srcdir),
            confdir=str(srcdir),
            outdir=str(tmp_path / "out_links"),
            doctreedir=str(tmp_path / "doctrees_links"),
            buildername="html",
        )
        app.set_translator("html", docs_conf._AccessibleHTML5Translator)
        app.builder.prepare_writing({"index"})
        app.builder.current_docname = "index"
        app.builder.secnumbers = {}
        app.builder.fignumbers = {}
        app.builder.add_permalinks = True
        doc = new_document("index", app.builder.docsettings)

        # 1. Top-level section + TOC-backlinked section + duplicate child sections
        sec_root = nodes.section(ids=["cli-reference"])
        sec_root += nodes.title("", "CLI Reference")

        sec_toc = nodes.section(ids=["overview-sec"])
        toc_title = nodes.title("", "Overview", refid="toc-entry-1")
        sec_toc += toc_title
        sec_root += sec_toc

        for sub_slug, sub_name in (("extract", "extract-records"), ("validate", "validate-study")):
            cmd_sec = nodes.section(ids=[f"cmd-{sub_slug}"])
            cmd_sec += nodes.title("", sub_name)
            dup_sec = nodes.section(ids=[f"pos-args-{sub_slug}"])
            dup_sec += nodes.title("", "Positional Arguments")
            cmd_sec += dup_sec
            sec_root += cmd_sec

        # 2. API desc_signature with dt[id] + viewcode [source] link, and :noindex: desc_signature
        desc = addnodes.desc(domain="py", objtype="class")
        sig = addnodes.desc_signature(
            module="imednet.auth.api_key",
            fullname="ApiKeyAuth",
            ids=["imednet.auth.api_key.ApiKeyAuth"],
        )
        sig += addnodes.desc_name("ApiKeyAuth", "ApiKeyAuth")
        src_ref = nodes.reference(
            "", "", internal=True, refuri="../../_modules/imednet/auth/api_key.html#ApiKeyAuth"
        )
        src_span = nodes.inline("", "[source]", classes=["viewcode-link"])
        src_ref += src_span
        sig += src_ref
        desc += sig
        desc += addnodes.desc_content()
        sec_root += desc

        noindex_desc = addnodes.desc(domain="py", objtype="function")
        noindex_sig = addnodes.desc_signature(
            module="imednet.validation.cache",
            fullname="validate_record_data",
        )
        noindex_sig += addnodes.desc_name("validate_record_data", "validate_record_data")
        noindex_ref = nodes.reference(
            "",
            "",
            internal=True,
            refuri="../_modules/imednet/validation/cache.html#validate_record_data",
        )
        noindex_ref += nodes.inline("", "[source]", classes=["viewcode-link"])
        noindex_sig += noindex_ref
        noindex_desc += noindex_sig
        noindex_desc += addnodes.desc_content()
        sec_root += noindex_desc

        # Standalone fallback viewcode link outside desc_signature + regular text link
        standalone_p = nodes.paragraph()
        standalone_src = nodes.reference(
            "",
            "",
            internal=True,
            refuri="_modules/imednet/sdk.html#ImednetSDK",
        )
        standalone_src += nodes.inline("", "[source]", classes=["viewcode-link"])
        normal_link = nodes.reference("", "Standard Guide Link", internal=True, refuri="guide.html")
        standalone_p += standalone_src
        standalone_p += normal_link
        sec_root += standalone_p

        # 3. Captioned table, figure, and code-block container with ids
        table = nodes.table(ids=["stability-table"])
        table += nodes.title("", "Stability Matrix")
        tgroup = nodes.tgroup(cols=1)
        tgroup += nodes.colspec(colwidth=1)
        tbody = nodes.tbody()
        row = nodes.row()
        entry = nodes.entry()
        entry += nodes.paragraph(text="Cell")
        row += entry
        tbody += row
        tgroup += tbody
        table += tgroup
        sec_root += table

        fig = nodes.figure(ids=["arch-fig"])
        fig += nodes.caption("", "Architecture Diagram")
        sec_root += fig

        code_wrap = nodes.container(ids=["sample-code"], classes=["literal-block-wrapper"])
        code_wrap += nodes.caption("", "example.py")
        sec_root += code_wrap

        doc += sec_root

        translator = docs_conf._AccessibleHTML5Translator(doc, app.builder)
        doc.walkabout(translator)
        # Exercise explicit caption permalink helper calls for figure & code-block wrappers
        translator.add_permalink_ref(fig, "Permalink to this image")
        translator.add_permalink_ref(code_wrap, "Permalink to this code")
        rendered = "".join(translator.body)

    # Heading, TOC-backlinked heading, and disambiguated duplicate headings
    assert (
        'href="#cli-reference" title="Permalink to this heading" aria-label="Permalink to CLI Reference"'
        in rendered
    )
    assert (
        'href="#overview-sec" title="Permalink to this heading" aria-label="Permalink to Overview"'
        in rendered
    )
    assert (
        'href="#pos-args-extract" title="Permalink to this heading" '
        'aria-label="Permalink to extract-records — Positional Arguments"'
    ) in rendered
    assert (
        'href="#pos-args-validate" title="Permalink to this heading" '
        'aria-label="Permalink to validate-study — Positional Arguments"'
    ) in rendered

    # API definition permalink + [source] links (both indexed and :noindex: + standalone fallback)
    assert (
        'href="#imednet.auth.api_key.ApiKeyAuth" title="Permalink to this definition" '
        'aria-label="Permalink to imednet.auth.api_key.ApiKeyAuth"'
    ) in rendered
    assert 'aria-label="[source] for imednet.auth.api_key.ApiKeyAuth"' in rendered
    assert 'aria-label="[source] for imednet.validation.cache.validate_record_data"' in rendered
    assert 'aria-label="[source] for imednet.sdk.ImednetSDK"' in rendered
    assert rendered.count("viewcode-source-link") == 3
    assert (
        'aria-label="[source]' not in rendered.split("Standard Guide Link")[0].rsplit("<a ", 1)[-1]
    )

    # Captions (table, figure, code-block)
    assert (
        'href="#stability-table" title="Permalink to this table" aria-label="Permalink to Stability Matrix"'
        in rendered
    )
    assert (
        'href="#arch-fig" title="Permalink to this image" aria-label="Permalink to Architecture Diagram"'
        in rendered
    )
    assert (
        'href="#sample-code" title="Permalink to this code" aria-label="Permalink to example.py"'
        in rendered
    )

    # 4. _label_viewcode_backlinks hook on _modules/ pages vs non-module pages
    mod_ctx = {
        "body": (
            '<div class="viewcode-block" id="ApiKeyAuth">\n'
            '<a class="viewcode-back" href="../../../../reference/api/imednet.auth.html#imednet.auth.api_key.ApiKeyAuth">[docs]</a>'
            "</div>\n"
            '<div class="viewcode-block" id="ApiKeyAuth.apply">'
            '<a class="viewcode-back" href="../../../../reference/api/imednet.auth.html">[docs]</a>'
            "</div>\n"
            '<a class="viewcode-back" href="#existing" aria-label="[docs] for custom.Symbol">[docs]</a>'
        )
    }
    docs_conf._label_viewcode_backlinks(
        None, "_modules/imednet/auth/api_key", "page.html", mod_ctx, None
    )
    assert 'aria-label="[docs] for imednet.auth.api_key.ApiKeyAuth"' in mod_ctx["body"]
    assert 'aria-label="[docs] for imednet.auth.api_key.ApiKeyAuth.apply"' in mod_ctx["body"]
    assert mod_ctx["body"].count('aria-label="[docs] for custom.Symbol"') == 1

    untouched_ctx = {"body": '<a class="viewcode-back" href="foo.html">[docs]</a>'}
    docs_conf._label_viewcode_backlinks(None, "index", "page.html", untouched_ctx, None)
    docs_conf._label_viewcode_backlinks(None, "_modules/index", "page.html", untouched_ctx, None)
    assert "aria-label=" not in untouched_ctx["body"]


@pytest.mark.unit
def test_pyodide_cells_sphinx_extension_and_conf() -> None:
    """Verify native Sphinx Pyodide extension directives, HTML context hooks, and conf.py config."""
    from docs import conf as docs_conf
    from docs._ext.pyodide_cells import (
        PyodideButtonNode,
        _compute_content_root,
        _inject_pyodide_page_context,
    )
    from docs._ext.pyodide_cells import (
        setup as setup_pyodide_cells,
    )

    assert "sphinx_thebe" not in docs_conf.extensions
    assert "pyodide_cells" in docs_conf.extensions
    assert not hasattr(docs_conf, "thebe_config")

    pyodide_cfg = docs_conf.pyodide_config
    wheels = pyodide_cfg["wheels"]
    assert len(wheels) == 3
    assert any("imednet-" in w for w in wheels)
    assert any("imednet_workflows-" in w for w in wheels)
    assert any("imednet_plugins_sinks-" in w for w in wheels)
    assert not any("airflow" in w or "tui" in w for w in wheels)
    assert set(pyodide_cfg["pyodide_packages"]) >= {
        "micropip",
        "pydantic",
        "cryptography",
        "pandas",
        "ssl",
        "sqlite3",
    }
    assert set(pyodide_cfg["requirements"]) >= {
        "httpx",
        "tenacity",
        "defusedxml",
        "python-dotenv",
        "filelock",
    }
    assert pyodide_cfg["env"]["IMEDNET_SIMULATION_MODE"] == "true"

    # Verify button HTML rendering & escaping
    btn_html = PyodideButtonNode(text="Activate Interactive Runtime").to_html()
    assert 'type="button"' in btn_html
    assert "thebe-launch-button" in btn_html
    assert "pyodide-launch-button" in btn_html
    assert "mybinder.org" not in btn_html

    # Verify content_root computation across page depths
    assert _compute_content_root("index") == "./"
    assert _compute_content_root("tutorials/quick_start") == "../"
    assert _compute_content_root("tutorials/examples/quick_start") == "../../"

    # Verify page context hook scopes #imednet-pyodide-config and thebe_pyodide.js to PyodideButtonNode pages
    from docutils.utils import new_document

    class _DummyApp:
        def __init__(self) -> None:
            self.directives: list[str] = []
            self.js_files: list[str] = []
            self.config = type("Cfg", (), {"pyodide_config": pyodide_cfg})()

        def add_config_value(self, *_args: object, **_kwargs: object) -> None:
            pass

        def add_node(self, *_args: object, **_kwargs: object) -> None:
            pass

        def add_directive(self, name: str, *_args: object, **_kwargs: object) -> None:
            self.directives.append(name)

        def add_js_file(self, filename: str, **_kwargs: object) -> None:
            self.js_files.append(filename)

        def connect(self, *_args: object, **_kwargs: object) -> None:
            pass

    dummy_app = _DummyApp()
    meta = setup_pyodide_cells(dummy_app)
    assert meta["parallel_read_safe"] is True
    assert "pyodide-button" in dummy_app.directives
    assert "thebe-button" in dummy_app.directives

    non_interactive_ctx: dict[str, str] = {"metatags": ""}
    _inject_pyodide_page_context(dummy_app, "404", "page.html", non_interactive_ctx, None)
    assert non_interactive_ctx["content_root"] == "./"
    assert 'id="imednet-pyodide-config"' not in non_interactive_ctx["metatags"]
    assert dummy_app.js_files == []

    interactive_doc = new_document("tutorials/quick_start")
    interactive_doc += PyodideButtonNode()
    ctx: dict[str, str] = {"metatags": ""}
    _inject_pyodide_page_context(
        dummy_app, "tutorials/quick_start", "page.html", ctx, interactive_doc
    )
    assert ctx["content_root"] == "../"
    assert '<script id="imednet-pyodide-config" type="application/json">' in ctx["metatags"]
    assert dummy_app.js_files == ["thebe_pyodide.js"]


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


@pytest.mark.unit
def test_role_card_button_accessible_names_satisfy_wcag_2_5_3() -> None:
    """Verify role card buttons satisfy WCAG 2.2 SC 2.5.3 (Label in Name) and Technique G208.

    Accessible names (aria-label) must contain and begin with the visible text string
    presented to sighted and speech-input users, while omitting leading emojis and preserving
    persona context for screen readers.
    """
    index_rst = (REPO_ROOT / "docs/index.rst").read_text(encoding="utf-8")

    # Match role card launch buttons and their inner span content
    button_pattern = re.compile(
        r'<button\s+type="button"\s+class="(?P<class>[^"]+)"\s+'
        r'data-persona-target="(?P<target>[^"]+)"\s+'
        r'aria-label="(?P<label>[^"]+)">\s*'
        r'<span>(?P<span>[^<]+)</span>\s*'
        r'</button>',
        re.MULTILINE,
    )

    matches = list(button_pattern.finditer(index_rst))
    assert len(matches) == 3, (
        f"Expected 3 role card launch buttons in docs/index.rst, found {len(matches)}"
    )

    expected_cards = {
        "panel-datamanager": {
            "visible_text": "Inspect Form Schemas",
            "context_keywords": ["Clinical Data Manager", "Explorer"],
        },
        "panel-integration": {
            "visible_text": "Run Interactive Queries",
            "context_keywords": ["Integration Engineer", "Console"],
        },
        "panel-auditor": {
            "visible_text": "Verify Audit Trails",
            "context_keywords": ["Regulatory Auditor", "Verification"],
        },
    }

    for match in matches:
        target = match.group("target")
        btn_class = match.group("class")
        aria_label = match.group("label")
        span_text = match.group("span").strip()

        assert "role-card-launch-btn" in btn_class, (
            f"Button for {target} missing role-card-launch-btn class"
        )
        assert target in expected_cards, f"Unexpected persona target: {target}"

        expected = expected_cards[target]
        visible_words = expected["visible_text"]

        # Ensure visible text is in the inner span
        assert visible_words in span_text, (
            f"Inner span text '{span_text}' does not contain expected visible text '{visible_words}'"
        )

        # WCAG 2.2 SC 2.5.3 / Technique F96: aria-label must contain visible text
        assert visible_words in aria_label, (
            f"aria-label '{aria_label}' does not contain visible text '{visible_words}' (violates WCAG 2.2 SC 2.5.3 / F96)"
        )

        # WCAG 2.2 Technique G208: aria-label must start with visible text
        assert aria_label.startswith(visible_words), (
            f"aria-label '{aria_label}' does not begin with visible text '{visible_words}' (violates Technique G208)"
        )

        # Persona role context is preserved for screen readers
        for keyword in expected["context_keywords"]:
            assert keyword in aria_label, (
                f"aria-label '{aria_label}' missing persona context '{keyword}'"
            )

        # Edge case: Ensure emojis are NOT in the accessible name so speech input engines match voice commands
        for emoji in ["📋", "⚡", "🛡️", "🛡"]:
            assert emoji not in aria_label, (
                f"aria-label '{aria_label}' contains emoji '{emoji}', which interferes with speech recognition activation"
            )


@pytest.mark.unit
def test_role_card_trigger_contracts_preserved() -> None:
    """Verify role card buttons maintain trigger contracts expected by showcase.js."""
    index_rst = (REPO_ROOT / "docs/index.rst").read_text(encoding="utf-8")
    showcase_js = (REPO_ROOT / "docs/_static/showcase.js").read_text(encoding="utf-8")

    # showcase.js must query [data-persona-target]
    assert "data-persona-target" in showcase_js, (
        "showcase.js must handle data-persona-target triggers"
    )

    # Targets defined in docs/index.rst buttons must correspond to panels in docs/index.rst
    for target_id in ["panel-datamanager", "panel-integration", "panel-auditor"]:
        assert f'data-persona-target="{target_id}"' in index_rst, (
            f"Trigger data-persona-target='{target_id}' missing in docs/index.rst"
        )
        assert f'id="{target_id}"' in index_rst, (
            f"Target panel id='{target_id}' missing in docs/index.rst"
        )


@pytest.mark.unit
def test_verify_built_tutorial_html_invariants(tmp_path: Path) -> None:
    """Verify static HTML gate catches legacy Thebe/Binder artifacts and validates wheel staging."""
    from scripts.verify_pyodide_tutorials import (
        LEGACY_BINDER_LABEL_PATTERN,
        verify_built_tutorial_html,
    )

    wheels_dir = tmp_path / "_static" / "wheels"
    wheels_dir.mkdir(parents=True)
    wheel_names = [
        "imednet-1.1.0-py3-none-any.whl",
        "imednet_workflows-1.1.0-py3-none-any.whl",
        "imednet_plugins_sinks-1.0.1-py3-none-any.whl",
    ]
    for whl_name in wheel_names:
        (wheels_dir / whl_name).write_bytes(b"PK\x03\x04")

    valid_cfg = {
        "selector": "div.highlight-python",
        "pyodide_packages": [
            "micropip",
            "pydantic",
            "cryptography",
            "pandas",
            "ssl",
            "sqlite3",
        ],
        "requirements": [
            "httpx",
            "tenacity",
            "defusedxml",
            "python-dotenv",
            "filelock",
        ],
        "wheels": [f"_static/wheels/{name}" for name in wheel_names],
        "env": {"IMEDNET_SIMULATION_MODE": "true"},
    }

    tutorials_dir = tmp_path / "tutorials"
    tutorials_dir.mkdir(parents=True)
    valid_html = (
        '<!doctype html><html data-content_root="../"><head>'
        f'<script id="imednet-pyodide-config" type="application/json">{json.dumps(valid_cfg)}</script>'
        '</head><body><button type="button" class="thebe-launch-button">'
        "Activate Interactive Runtime</button></body></html>"
    )
    (tutorials_dir / "quick_start.html").write_text(valid_html, encoding="utf-8")
    (tutorials_dir / "async_quick_start.html").write_text(valid_html, encoding="utf-8")

    assert verify_built_tutorial_html(tmp_path) == []

    # Injecting legacy sphinx-thebe/Binder tokens or forbidden wheels must fail
    bad_cfg = dict(valid_cfg)
    bad_cfg["wheels"] = [*valid_cfg["wheels"], "_static/wheels/imednet_tui-1.0.0-py3-none-any.whl"]
    bad_html = (
        '<!doctype html><html data-content_root=""><head>'
        '<script src="../_static/sphinx-thebe.js"></script>'
        f'<script id="imednet-pyodide-config" type="application/json">{json.dumps(bad_cfg)}</script>'
        '</head><body><button class="thebe-launch-button">Launching from mybinder.org</button>'
        "</body></html>"
    )
    (tutorials_dir / "quick_start.html").write_text(bad_html, encoding="utf-8")
    errs = verify_built_tutorial_html(tmp_path)
    assert (
        "tutorials/quick_start.html: Found forbidden legacy Thebe/Binder token 'sphinx-thebe.js'"
        in errs
    )
    assert (
        "tutorials/quick_start.html: Found forbidden legacy Thebe/Binder token 'mybinder.org'"
        in errs
    )
    assert any("data-content_root" in err for err in errs)
    assert any("imednet_tui" in err for err in errs)

    assert LEGACY_BINDER_LABEL_PATTERN.search("Launching from mybinder.org...") is not None
    assert LEGACY_BINDER_LABEL_PATTERN.search("Connecting to MyBinder") is not None
    assert LEGACY_BINDER_LABEL_PATTERN.search("Activate Interactive Runtime") is None
    assert LEGACY_BINDER_LABEL_PATTERN.search("Pyodide (WebAssembly): ready") is None


@pytest.mark.unit
def test_high_contrast_and_mermaid_conf_tokens() -> None:
    """Verify prefers-contrast: more tokens reach >= 7:1 and Mermaid themeVariables avoid var()."""
    from docs import conf as docs_conf

    hc = docs_conf.high_contrast_css_variables
    for mode in ("light", "dark"):
        tokens = hc[mode]
        bg_primary = tokens["color-background-primary"]
        bg_secondary = tokens["color-background-secondary"]

        for fg_key in (
            "color-foreground-primary",
            "color-foreground-secondary",
            "color-foreground-muted",
            "color-brand-primary",
            "color-brand-content",
            "color-status-success",
            "color-status-warning",
            "color-status-danger",
            "color-diagram-line",
            "color-diagram-node-border",
        ):
            for bg in (bg_primary, bg_secondary):
                ratio = contrast_ratio(tokens[fg_key], bg)
                assert ratio >= 7.0, (
                    f"{mode} high-contrast {fg_key} ({tokens[fg_key]}) vs {bg} is {ratio:.2f}:1 < 7:1"
                )

        btn_text = tokens["color-brand-btn-text"]
        for btn_bg_key in (
            "color-brand-primary",
            "color-brand-primary-hover",
            "color-status-success",
            "color-status-danger",
        ):
            btn_ratio = contrast_ratio(btn_text, tokens[btn_bg_key])
            assert btn_ratio >= 7.0, (
                f"{mode} high-contrast button text ({btn_text}) vs {btn_bg_key} "
                f"({tokens[btn_bg_key]}) is {btn_ratio:.2f}:1 < 7:1"
            )

        note_ratio = contrast_ratio(
            tokens["color-diagram-note-text"], tokens["color-diagram-note-bg"]
        )
        assert note_ratio >= 7.0, (
            f"{mode} high-contrast diagram note contrast {note_ratio:.2f}:1 < 7:1"
        )
        code_ratio = contrast_ratio(
            tokens["color-code-foreground"], tokens["color-code-background"]
        )
        assert code_ratio >= 7.0, f"{mode} high-contrast code contrast {code_ratio:.2f}:1 < 7:1"
        for synced_key in (
            "color-background-border",
            "color-brand-visited",
            "color-brand-button-text",
            "color-badge-success",
            "color-badge-warning",
            "color-badge-danger",
            "color-badge-required",
            "color-badge-optional",
        ):
            assert synced_key in tokens, f"{mode} high-contrast conf missing {synced_key}"

    init_js = docs_conf.mermaid_init_js
    assert init_js.startswith("mermaid.initialize(") and init_js.endswith(");")
    payload = json.loads(init_js[len("mermaid.initialize(") : -2])
    assert payload["theme"] == "base"
    assert payload["useMaxWidth"] is False
    assert payload["flowchart"]["useMaxWidth"] is False
    assert payload["sequence"]["useMaxWidth"] is False

    for key, val in payload["themeVariables"].items():
        assert isinstance(val, str) and val.startswith("#"), (
            f"Mermaid themeVariables[{key!r}] must be a hex color for khroma compatibility, got {val!r}"
        )
        assert "var(" not in val
    assert "var(--color-" in payload["themeCSS"]


@pytest.mark.unit
def test_thebe_cascade_focus_and_forced_colors_css() -> None:
    """Verify CSS cascade ordering, hover overrides, focus-visible outline, and forced-colors rules."""
    css_content = (REPO_ROOT / "docs/_static/custom.css").read_text(encoding="utf-8")
    thebe_js = (REPO_ROOT / "docs/_static/thebe_pyodide.js").read_text(encoding="utf-8")

    pos_thebelab = css_content.index(".thebelab-button {")
    pos_launch = css_content.index(".thebelab-button.thebe-launch-button")
    assert pos_thebelab < pos_launch, (
        ".thebelab-button must precede .thebe-launch-button in custom.css"
    )

    assert ".thebelab-button.thebelab-run-button:hover:not(:disabled)" in css_content
    assert ".thebe-launch-button.thebe-status-ready" in css_content
    assert 'statusSpan.style.color = "inherit"' in thebe_js

    editor_block = re.search(r"\.thebe-editor\s*\{([^}]*)\}", css_content)
    assert editor_block is not None
    assert "outline: none" not in editor_block.group(1)
    assert ".thebe-editor:focus-visible" in css_content

    assert "@media (prefers-contrast: more)" in css_content
    assert "@media (forced-colors: active)" in css_content
    for sys_token in (
        "ButtonFace",
        "ButtonText",
        "Highlight",
        "HighlightText",
        "Canvas",
        "CanvasText",
    ):
        assert sys_token in css_content

    # Dead showcase selectors must not regress in custom.css
    for dead_selector in (
        ".persona-tab-btn",
        ".output-subtab-btn",
        ".explorer-secondary-btn",
        ".status-pill-",
    ):
        assert dead_selector not in css_content, (
            f"Dead selector {dead_selector!r} found in docs/_static/custom.css"
        )

    # Scoped flex layout and focus-visible rules for showcase controls
    assert ".explorer-json-wrapper:not([hidden])" in css_content
    assert ".explorer-copy-btn:focus-visible" in css_content
    assert ".quick-start-callout a:focus-visible" in css_content


@pytest.mark.unit
def test_diagram_validator_skips_acc_directives_and_verifies_all_15_diagrams() -> None:
    """Verify validate_diagrams skips accTitle/accDescr symbols and confirms 15 titled diagrams."""
    from scripts.validate_diagrams import (
        collect_rst_mermaid_directives,
        extract_acc_title,
        extract_nodes_from_mermaid_lines,
    )

    sample = [
        "graph TD\n",
        "    accTitle: Sample Architecture [Unregistered(Symbol)]\n",
        "    accDescr {\n",
        "        Multi-line description mentioning FakeNode[FakeLabel]\n",
        "    }\n",
        "    CLI[CLI] --> Workflows\n",
    ]
    assert extract_acc_title(sample) == "Sample Architecture [Unregistered(Symbol)]"
    nodes = extract_nodes_from_mermaid_lines(sample)
    assert "accTitle" not in nodes
    assert "accDescr" not in nodes
    assert "FakeNode" not in nodes
    assert nodes["CLI"] == "CLI"

    diagrams_dir = REPO_ROOT / "docs" / "diagrams"
    mmd_files = sorted(diagrams_dir.glob("*.mmd"))
    assert len(mmd_files) >= 13
    for mmd in mmd_files:
        lines = mmd.read_text(encoding="utf-8").splitlines()
        assert extract_acc_title(lines), f"{mmd.name} missing accTitle"
        assert any(ln.strip().startswith("accDescr") for ln in lines), (
            f"{mmd.name} missing accDescr"
        )

    external_refs, inline_blocks = collect_rst_mermaid_directives(str(REPO_ROOT / "docs"))
    assert len(external_refs) == 13
    assert len(inline_blocks) == 2
    assert len(external_refs) + len(inline_blocks) == 15
    for rel_path, lineno, block_lines in inline_blocks:
        assert extract_acc_title(block_lines), (
            f"{rel_path}:{lineno} inline diagram missing accTitle"
        )


@pytest.mark.unit
def test_scroll_padding_reduced_motion_and_back_to_top_focus_transfer() -> None:
    """Verify CSS C43 scroll-padding-top, reduced-motion rules, C45 focus ring, and JS focus transfer."""
    css_content = (REPO_ROOT / "docs/_static/custom.css").read_text(encoding="utf-8")
    showcase_js = (REPO_ROOT / "docs/_static/showcase.js").read_text(encoding="utf-8")

    # Requirement 1: Root scroll-padding-top (3.5rem default, 6rem at <= 63em)
    assert re.search(r"html\s*\{\s*scroll-padding-top:\s*3\.5rem;", css_content) is not None
    assert (
        re.search(
            r"@media\s*\(\s*max-width:\s*63em\s*\)\s*\{\s*html\s*\{\s*scroll-padding-top:\s*6rem;",
            css_content,
        )
        is not None
    )

    # Requirement 2: #furo-main-content:focus-visible 2px solid outline in default and forced-colors
    main_focus_block = re.search(
        r"#furo-main-content:focus-visible\s*\{([^}]*)\}",
        css_content,
    )
    assert main_focus_block is not None
    assert "outline: 2px solid var(--color-brand-primary)" in main_focus_block.group(1)
    assert "outline-offset: 2px" in main_focus_block.group(1)

    fc_pos = css_content.index("@media (forced-colors: active)")
    fc_section = css_content[fc_pos:]
    assert "#furo-main-content:focus-visible" in fc_section
    assert "outline: 2px solid Highlight !important" in fc_section

    # Requirement 3: @media (prefers-reduced-motion: reduce) sets scroll-behavior: auto
    rm_match = re.search(
        r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{([^}]*\{[^}]*\})\s*\}",
        css_content,
    )
    assert rm_match is not None
    rm_body = rm_match.group(1)
    for required_sel in ("html", ".sidebar-scroll", ".toc-scroll"):
        assert required_sel in rm_body
    assert "scroll-behavior: auto" in rm_body

    # Requirements 4 & 5 + terminology guardrail in showcase.js
    assert "furo-main-content" in showcase_js
    assert "a.back-to-top" in showcase_js
    assert "globalThis.scrollTo" in showcase_js
    assert "globalThis.matchMedia" in showcase_js
    assert "preventScroll: true" in showcase_js

    node_bin = shutil.which("node")
    if node_bin:
        js_probe = f"""
        const fs = require("fs");
        const vm = require("vm");
        const source = fs.readFileSync({json.dumps(str(REPO_ROOT / "docs/_static/showcase.js"))}, "utf8");

        function runCase(reducedMotion, presetTabindex) {{
            const attrs = presetTabindex !== null ? {{ tabindex: presetTabindex }} : {{}};
            const focusCalls = [];
            const scrollCalls = [];
            const linkAttrs = {{}};
            const clickHandlers = [];
            let prevented = false;

            const mainEl = {{
                id: "furo-main-content",
                hasAttribute: (k) => Object.prototype.hasOwnProperty.call(attrs, k),
                getAttribute: (k) => (Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null),
                setAttribute: (k, v) => {{ attrs[k] = String(v); }},
                focus: (opts) => {{ focusCalls.push(opts || {{}}); }}
            }};

            const bttLink = {{
                hasAttribute: (k) => Object.prototype.hasOwnProperty.call(linkAttrs, k),
                setAttribute: (k, v) => {{ linkAttrs[k] = String(v); }},
                addEventListener: (ev, cb) => {{
                    if (ev === "click") clickHandlers.push(cb);
                }}
            }};

            const win = {{
                location: {{ hash: "#panel-auditor" }},
                history: {{ replaceState: () => {{}} }},
                addEventListener: () => {{}},
                scrollTo: (opts) => {{ scrollCalls.push(opts); }},
                matchMedia: (q) => ({{
                    matches: q === "(prefers-reduced-motion: reduce)" ? reducedMotion : false
                }})
            }};

            const doc = {{
                readyState: "complete",
                getElementById: (id) => (id === "furo-main-content" ? mainEl : null),
                querySelectorAll: (sel) => (sel === "a.back-to-top" ? [bttLink] : []),
                addEventListener: () => {{}},
                body: {{}}
            }};

            const sandbox = {{
                window: win,
                document: doc,
                console
            }};
            sandbox.globalThis = sandbox;
            sandbox.scrollTo = win.scrollTo;
            sandbox.matchMedia = win.matchMedia;

            vm.runInNewContext(source, sandbox);

            const startupTabindex = mainEl.getAttribute("tabindex");
            clickHandlers.forEach((cb) => cb({{ preventDefault: () => {{ prevented = true; }} }}));

            return {{
                startupTabindex,
                prevented,
                hashAfter: win.location.hash,
                scrollCalls,
                focusCalls
            }};
        }}

        const smoothRes = runCase(false, null);
        const reducedRes = runCase(true, null);
        const presetRes = runCase(false, "0");
        console.log(JSON.stringify({{ smoothRes, reducedRes, presetRes }}));
        """
        proc = subprocess.run(
            [node_bin, "-e", js_probe], capture_output=True, text=True, check=True
        )
        payload = json.loads(proc.stdout)

        assert payload["smoothRes"] == {
            "startupTabindex": "-1",
            "prevented": True,
            "hashAfter": "#panel-auditor",
            "scrollCalls": [{"top": 0, "behavior": "smooth"}],
            "focusCalls": [{"preventScroll": True}],
        }
        assert payload["reducedRes"] == {
            "startupTabindex": "-1",
            "prevented": True,
            "hashAfter": "#panel-auditor",
            "scrollCalls": [{"top": 0, "behavior": "auto"}],
            "focusCalls": [{"preventScroll": True}],
        }
        assert payload["presetRes"]["startupTabindex"] == "0"


def _launch_chromium_for_accessibility(playwright_instance: object) -> object:
    """Launch headless Chromium with CI-safe flags and local binary fallbacks."""
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    chromium = playwright_instance.chromium  # type: ignore[attr-defined]
    try:
        return chromium.launch(headless=True, args=launch_args)
    except Exception:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            *[
                str(p)
                for p in sorted(
                    (Path.home() / ".cache" / "ms-playwright").glob(
                        "chromium-*/chrome-linux*/chrome"
                    ),
                    reverse=True,
                )
            ],
        ]
        for candidate in candidates:
            if shutil.which(candidate) or Path(candidate).is_file():
                return chromium.launch(
                    headless=True,
                    executable_path=candidate,
                    args=launch_args,
                )
        raise


@pytest.mark.unit
def test_built_html_wcag_and_diagrams_in_chromium(tmp_path: Path) -> None:
    """Verify computed contrast, Mermaid scaling/ARIA, SC 2.5.3 names, and roving tabindex in Chromium."""
    import functools
    import lzma
    import os
    import sys
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    require_pw = os.environ.get("IMEDNET_REQUIRE_PLAYWRIGHT") == "1"
    if require_pw:
        from playwright.sync_api import sync_playwright
    else:
        sync_pw_mod = pytest.importorskip("playwright.sync_api")
        sync_playwright = sync_pw_mod.sync_playwright

    mermaid_xz = REPO_ROOT / "tests" / "fixtures" / "mermaid-10.2.0.min.js.xz"
    assert mermaid_xz.is_file(), f"Offline Mermaid bundle missing: {mermaid_xz}"
    mermaid_js_bytes = lzma.decompress(mermaid_xz.read_bytes())

    env_html_dir = os.environ.get("IMEDNET_BUILT_HTML_DIR")
    default_html_dir = REPO_ROOT / "docs" / "_build" / "html"
    if env_html_dir and Path(env_html_dir).is_dir():
        html_dir = Path(env_html_dir).resolve()
    elif (default_html_dir / "index.html").is_file():
        html_dir = default_html_dir.resolve()
    else:
        html_dir = (tmp_path / "html").resolve()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "sphinx",
                "-b",
                "html",
                "-W",
                "--keep-going",
                str(REPO_ROOT / "docs"),
                str(html_dir),
            ],
            check=True,
            cwd=str(REPO_ROOT),
        )

    class _HeadingRoleAuditParser:
        """Scan built HTML files for [role='heading'] elements and validate aria-level."""

        @staticmethod
        def audit_directory(
            root_dir: Path,
        ) -> tuple[int, list[str], dict[str, list[tuple[str, str | None]]]]:
            from html.parser import HTMLParser

            class _Parser(HTMLParser):
                def __init__(self) -> None:
                    super().__init__()
                    self.headings: list[tuple[str, str | None, str]] = []
                    self._stack: list[tuple[str, set[str]]] = []

                def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                    attr_map = dict(attrs)
                    classes = set((attr_map.get("class") or "").split())
                    if attr_map.get("role") == "heading":
                        in_sidebar = any("sidebar-tree" in c for _, c in self._stack)
                        in_toctree = any("toctree-wrapper" in c for _, c in self._stack)
                        zone = (
                            "sidebar-tree"
                            if in_sidebar
                            else ("toctree-wrapper" if in_toctree else "other")
                        )
                        self.headings.append((zone, attr_map.get("aria-level"), tag))
                    self._stack.append((tag, classes))

                def handle_endtag(self, tag: str) -> None:
                    for idx in range(len(self._stack) - 1, -1, -1):
                        if self._stack[idx][0] == tag:
                            del self._stack[idx:]
                            break

            total_headings = 0
            violations: list[str] = []
            by_page: dict[str, list[tuple[str, str | None]]] = {}
            valid_levels = {"1", "2", "3", "4", "5", "6"}

            for html_file in sorted(root_dir.rglob("*.html")):
                rel = html_file.relative_to(root_dir).as_posix()
                parser = _Parser()
                parser.feed(html_file.read_text(encoding="utf-8", errors="ignore"))
                by_page[rel] = [(zone, lvl) for zone, lvl, _tag in parser.headings]
                for zone, lvl, tag in parser.headings:
                    total_headings += 1
                    if lvl not in valid_levels:
                        violations.append(
                            f"{rel}: <{tag} role='heading'> in {zone} has invalid aria-level={lvl!r}"
                        )
                    elif zone == "sidebar-tree" and lvl != "2":
                        violations.append(
                            f"{rel}: .sidebar-tree heading expected aria-level='2', got {lvl!r}"
                        )
            return total_headings, violations, by_page

    total_role_headings, heading_violations, page_headings = (
        _HeadingRoleAuditParser.audit_directory(html_dir)
    )
    assert total_role_headings > 0, (
        "Expected captioned toctree [role='heading'] nodes in built HTML"
    )
    assert heading_violations == [], (
        "Invalid or missing aria-level on [role='heading'] elements:\n"
        + "\n".join(heading_violations[:25])
    )
    idx_wrapper_levels = [
        lvl for zone, lvl in page_headings.get("index.html", []) if zone == "toctree-wrapper"
    ]
    assert idx_wrapper_levels == ["3", "3", "3", "3", "3"], (
        f"Expected five aria-level='3' toctree captions under <h2> on index.html, got {idx_wrapper_levels}"
    )
    contrib_wrapper_levels = [
        lvl
        for zone, lvl in page_headings.get("reference/contributing.html", [])
        if zone == "toctree-wrapper"
    ]
    assert contrib_wrapper_levels == [], (
        f"Expected zero duplicate toctree captions on reference/contributing.html, got {contrib_wrapper_levels}"
    )
    contrib_sidebar_levels = [
        lvl
        for zone, lvl in page_headings.get("reference/contributing.html", [])
        if zone == "sidebar-tree"
    ]
    assert contrib_sidebar_levels == ["2", "2", "2", "2", "2"], (
        f"Expected five aria-level='2' sidebar toctree captions on reference/contributing.html, got {contrib_sidebar_levels}"
    )

    class _QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(_QuietHandler, directory=str(html_dir)),
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    contrast_helper_js = """
    window.__wcagHelpers = {
        parseRgb(str) {
            if (!str) return null;
            const m = str.match(/rgba?\\(\\s*([0-9.]+)[,\\s]+([0-9.]+)[,\\s]+([0-9.]+)(?:[,/\\s]+([0-9.]+))?\\s*\\)/i);
            if (!m) return null;
            return {
                r: parseFloat(m[1]),
                g: parseFloat(m[2]),
                b: parseFloat(m[3]),
                a: m[4] === undefined ? 1 : parseFloat(m[4])
            };
        },
        blend(fg, bg) {
            const a = fg.a + bg.a * (1 - fg.a);
            if (a === 0) return { r: 255, g: 255, b: 255, a: 1 };
            return {
                r: (fg.r * fg.a + bg.r * bg.a * (1 - fg.a)) / a,
                g: (fg.g * fg.a + bg.g * bg.a * (1 - fg.a)) / a,
                b: (fg.b * fg.a + bg.b * bg.a * (1 - fg.a)) / a,
                a
            };
        },
        effectiveBg(el) {
            const layers = [];
            let cur = el;
            while (cur && cur.nodeType === 1) {
                const cs = getComputedStyle(cur);
                const parsed = this.parseRgb(cs.backgroundColor);
                if (parsed && parsed.a > 0) {
                    layers.push(parsed);
                    if (parsed.a >= 1) break;
                }
                cur = cur.parentElement;
            }
            let base = { r: 255, g: 255, b: 255, a: 1 };
            for (let i = layers.length - 1; i >= 0; i--) {
                base = this.blend(layers[i], base);
            }
            return base;
        },
        luminance(rgb) {
            const lin = (v) => {
                const s = v / 255;
                return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
            };
            return 0.2126 * lin(rgb.r) + 0.7152 * lin(rgb.g) + 0.0722 * lin(rgb.b);
        },
        ratio(c1, c2) {
            const l1 = this.luminance(c1);
            const l2 = this.luminance(c2);
            return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        },
        elementContrast(el) {
            const bg = this.effectiveBg(el);
            const fgRaw = this.parseRgb(getComputedStyle(el).color);
            const fg = fgRaw && fgRaw.a < 1 ? this.blend(fgRaw, bg) : fgRaw;
            return this.ratio(fg, bg);
        }
    };
    """

    try:
        with sync_playwright() as pw:
            browser = _launch_chromium_for_accessibility(pw)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.set_default_timeout(10_000)

                def _route_handler(route: object) -> None:
                    req_url = route.request.url  # type: ignore[attr-defined]
                    if "mermaid" in req_url and req_url.endswith(".js"):
                        route.fulfill(  # type: ignore[attr-defined]
                            status=200,
                            content_type="application/javascript",
                            body=mermaid_js_bytes,
                        )
                    elif req_url.startswith((f"http://127.0.0.1:{port}/", "data:", "blob:")):
                        route.continue_()  # type: ignore[attr-defined]
                    else:
                        route.abort()  # type: ignore[attr-defined]

                context.route("**/*", _route_handler)

                # --------------------------------------------------------------
                # 1. Pyodide controls: light, dark, prefers-contrast, forced-colors
                # --------------------------------------------------------------
                tut_page = context.new_page()
                tut_page.goto(
                    f"http://127.0.0.1:{port}/tutorials/quick_start.html",
                    wait_until="load",
                )
                tut_page.add_script_tag(content=contrast_helper_js)

                # Prepend synthetic sphinx-thebe.css hover rule before custom.css to verify override
                tut_page.evaluate(
                    """() => {
                        window.pyodideRuntime.transformCodeCells();

                        const legacy = document.createElement('style');
                        legacy.id = 'legacy-sphinx-thebe-sim';
                        legacy.textContent = '.thebelab-button:hover { background-color: #fcfcfc; color: #333333; }';
                        document.head.insertBefore(legacy, document.head.firstChild);

                        const launch = document.querySelector('.thebe-launch-button');
                        launch.classList.add('thebelab-button');

                        const readyClone = launch.cloneNode(true);
                        readyClone.id = 'test-ready-launch-btn';
                        launch.parentElement.appendChild(readyClone);
                        window.pyodideRuntime.updateLaunchButtons('ready', 'Pyodide (WebAssembly): ready');

                        // Keep original launch button in initial idle state while readyClone stays ready
                        launch.classList.remove('thebe-status-ready');
                        launch.textContent = 'Activate Interactive Runtime';

                        document.querySelectorAll('.thebelab-run-button').forEach((b) => {
                            b.disabled = false;
                        });
                        document.querySelectorAll('.thebelab-cell-status').forEach((s) => {
                            s.textContent = 'Execution completed.';
                        });
                    }"""
                )

                for theme in ("light", "dark"):
                    tut_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                    run_btn = tut_page.locator(".thebelab-run-button").first
                    run_btn.hover()
                    ratios = tut_page.evaluate(
                        """() => {
                            const h = window.__wcagHelpers;
                            const launch = document.querySelector('.thebe-launch-button:not(.thebe-status-ready)');
                            const ready = document.getElementById('test-ready-launch-btn');
                            const statusSpan = ready.querySelector('span.status');
                            const runBtn = document.querySelector('.thebelab-run-button');
                            return {
                                launch: h.elementContrast(launch),
                                ready: h.elementContrast(ready),
                                statusSpan: h.elementContrast(statusSpan),
                                runHover: h.elementContrast(runBtn)
                            };
                        }"""
                    )
                    for ctrl_name, val in ratios.items():
                        assert val >= 4.5, f"{theme} mode {ctrl_name} contrast {val:.2f}:1 < 4.5:1"

                # Verify .thebe-editor:focus-visible renders a 2px solid outline
                editor_loc = tut_page.locator(".thebe-editor").first
                editor_loc.focus()
                tut_page.keyboard.press("Shift")
                focus_outline = tut_page.evaluate(
                    """() => {
                        const ed = document.querySelector('.thebe-editor');
                        const cs = getComputedStyle(ed);
                        return { style: cs.outlineStyle, width: cs.outlineWidth, matches: ed.matches(':focus-visible') };
                    }"""
                )
                assert focus_outline["matches"] is True
                assert focus_outline["style"] == "solid"
                assert focus_outline["width"] == "2px"

                # Verify @media (prefers-contrast: more) reaches >= 7:1 across auto, light, and dark
                # on UI controls AND div.highlight syntax tokens
                tut_page.emulate_media(contrast="more")
                for color_scheme, theme_attr in (
                    ("light", "auto"),
                    ("dark", "auto"),
                    ("light", "light"),
                    ("dark", "dark"),
                ):
                    tut_page.emulate_media(contrast="more", color_scheme=color_scheme)
                    tut_page.evaluate(f"document.body.setAttribute('data-theme', '{theme_attr}')")
                    tut_page.locator(".thebelab-run-button").first.hover()
                    hc_ratios = tut_page.evaluate(
                        """() => {
                            const h = window.__wcagHelpers;
                            const tokenRatios = [];
                            document.querySelectorAll('div.highlight').forEach((hl) => {
                                if (hl.getClientRects().length === 0) return;
                                const pre = hl.querySelector('pre');
                                if (pre) tokenRatios.push(h.elementContrast(pre));
                                hl.querySelectorAll('span[class]').forEach((sp) => {
                                    if (!(sp.textContent || '').trim()) return;
                                    tokenRatios.push(h.elementContrast(sp));
                                });
                            });
                            return {
                                launch: h.elementContrast(document.querySelector('.thebe-launch-button:not(.thebe-status-ready)')),
                                ready: h.elementContrast(document.getElementById('test-ready-launch-btn')),
                                statusSpan: h.elementContrast(document.querySelector('#test-ready-launch-btn span.status')),
                                runHover: h.elementContrast(document.querySelector('.thebelab-run-button')),
                                cellStatus: h.elementContrast(document.querySelector('.thebelab-cell-status')),
                                minHighlightToken: tokenRatios.length ? Math.min(...tokenRatios) : 0,
                                tokenCount: tokenRatios.length
                            };
                        }"""
                    )
                    assert hc_ratios["tokenCount"] > 0
                    for ctrl_name, val in hc_ratios.items():
                        if ctrl_name == "tokenCount":
                            continue
                        assert val >= 7.0, (
                            f"prefers-contrast: more (scheme={color_scheme}, data-theme={theme_attr}) "
                            f"{ctrl_name} contrast {val:.2f}:1 < 7.0:1"
                        )
                tut_page.emulate_media(contrast="no-preference", color_scheme="light")

                # Verify @media (forced-colors: active) preserves button borders & focus rings
                tut_page.emulate_media(forced_colors="active")
                editor_loc.focus()
                fc_styles = tut_page.evaluate(
                    """() => {
                        const btn = getComputedStyle(document.querySelector('.thebelab-run-button'));
                        const ed = getComputedStyle(document.querySelector('.thebe-editor'));
                        return {
                            btnBorderStyle: btn.borderTopStyle,
                            btnBorderWidth: parseFloat(btn.borderTopWidth),
                            edOutlineStyle: ed.outlineStyle,
                            edOutlineWidth: ed.outlineWidth
                        };
                    }"""
                )
                assert fc_styles["btnBorderStyle"] == "solid"
                assert fc_styles["btnBorderWidth"] >= 1.0
                assert fc_styles["edOutlineStyle"] == "solid"
                assert fc_styles["edOutlineWidth"] == "2px"
                tut_page.emulate_media(forced_colors="none")
                tut_page.close()

                # --------------------------------------------------------------
                # 2. All 15 Mermaid diagrams: <title>, ARIA, dark contrast, 375px >= 12px
                # --------------------------------------------------------------
                diagram_pages = sorted(
                    p.relative_to(html_dir).as_posix()
                    for p in html_dir.rglob("*.html")
                    if 'class="mermaid"' in p.read_text(encoding="utf-8", errors="ignore")
                )
                rendered_diagram_count = 0
                overflowing_mobile_regions = 0

                diag_page = context.new_page()
                for rel_url in diagram_pages:
                    diag_page.set_viewport_size({"width": 1280, "height": 900})
                    diag_page.goto(
                        f"http://127.0.0.1:{port}/{rel_url}",
                        wait_until="load",
                    )
                    diag_page.wait_for_function(
                        "() => Array.from(document.querySelectorAll('.mermaid')).every(m => m.querySelector('svg'))"
                    )
                    diag_page.add_script_tag(content=contrast_helper_js)

                    # Switch to dark mode dynamically (zero JS re-render) and inspect contrast & titles
                    diag_page.evaluate("document.body.setAttribute('data-theme', 'dark')")
                    diag_page.wait_for_timeout(50)

                    dark_audit = diag_page.evaluate(
                        """() => {
                            const h = window.__wcagHelpers;
                            const results = [];
                            document.querySelectorAll('.mermaid').forEach((container, idx) => {
                                const svg = container.querySelector('svg');
                                const titleEl = svg ? svg.querySelector('title') : null;
                                const ariaLabel = svg ? svg.getAttribute('aria-label') : null;
                                const hasError = !!(svg && svg.querySelector('.error-icon'));

                                const containerBg = h.effectiveBg(container);
                                const nodeBgSample = svg.querySelector('.node rect, .node polygon, .node circle, rect.actor');
                                const nodeBg = nodeBgSample ? h.parseRgb(getComputedStyle(nodeBgSample).fill) : containerBg;

                                let minTextContrast = Infinity;
                                svg.querySelectorAll('.nodeLabel, text.actor > tspan, .messageText, .loopText > tspan, .labelText > tspan').forEach((t) => {
                                    if (!(t.textContent || '').trim()) return;
                                    const cs = getComputedStyle(t);
                                    const fg = h.parseRgb(cs.color) || h.parseRgb(cs.fill);
                                    if (!fg) return;
                                    const isInsideBox = !!(t.closest('.node') || t.closest('text.actor') || t.classList.contains('labelText'));
                                    const bg = isInsideBox && nodeBg ? nodeBg : containerBg;
                                    minTextContrast = Math.min(minTextContrast, h.ratio(fg, bg));
                                });

                                svg.querySelectorAll('text.noteText, text.noteText > tspan').forEach((nt) => {
                                    if (!(nt.textContent || '').trim()) return;
                                    const noteRect = svg.querySelector('rect.note');
                                    const bg = noteRect ? h.parseRgb(getComputedStyle(noteRect).fill) : containerBg;
                                    const fg = h.parseRgb(getComputedStyle(nt).fill);
                                    if (fg && bg) {
                                        minTextContrast = Math.min(minTextContrast, h.ratio(fg, bg));
                                    }
                                });

                                svg.querySelectorAll('.edgeLabel span, .edgeLabel').forEach((el) => {
                                    if (!(el.textContent || '').trim()) return;
                                    const bg = h.effectiveBg(el);
                                    const fg = h.parseRgb(getComputedStyle(el).color);
                                    if (fg && bg) {
                                        minTextContrast = Math.min(minTextContrast, h.ratio(fg, bg));
                                    }
                                });

                                let minStrokeContrast = Infinity;
                                svg.querySelectorAll('.node rect, .node polygon, rect.actor, .edgePath .path, .flowchart-link, line[id^="actor"], .messageLine0, .messageLine1').forEach((s) => {
                                    const stroke = h.parseRgb(getComputedStyle(s).stroke);
                                    if (stroke && stroke.a > 0) {
                                        minStrokeContrast = Math.min(minStrokeContrast, h.ratio(stroke, containerBg));
                                    }
                                });

                                results.push({
                                    idx,
                                    hasError,
                                    title: titleEl ? titleEl.textContent.trim() : '',
                                    ariaLabel: ariaLabel || '',
                                    minTextContrast,
                                    minStrokeContrast
                                });
                            });
                            return results;
                        }"""
                    )

                    for item in dark_audit:
                        rendered_diagram_count += 1
                        assert item["hasError"] is False, (
                            f"{rel_url} diagram {item['idx']} failed to parse"
                        )
                        assert item["title"], (
                            f"{rel_url} diagram {item['idx']} missing <svg><title>"
                        )
                        assert item["ariaLabel"], (
                            f"{rel_url} diagram {item['idx']} missing svg[aria-label]"
                        )
                        assert item["minTextContrast"] >= 4.5, (
                            f"{rel_url} diagram {item['idx']} dark text contrast {item['minTextContrast']:.2f}:1 < 4.5:1"
                        )
                        assert item["minStrokeContrast"] >= 3.0, (
                            f"{rel_url} diagram {item['idx']} dark stroke contrast {item['minStrokeContrast']:.2f}:1 < 3.0:1"
                        )

                    # Mobile 375px viewport check: >= 12px effective text & scrollable region semantics
                    diag_page.set_viewport_size({"width": 375, "height": 812})
                    diag_page.evaluate("window.dispatchEvent(new Event('resize'))")
                    diag_page.wait_for_timeout(80)

                    mobile_audit = diag_page.evaluate(
                        """() => {
                            return Array.from(document.querySelectorAll('.mermaid')).map((container, idx) => {
                                const svg = container.querySelector('svg');
                                const vb = svg.viewBox && svg.viewBox.baseVal;
                                const svgRect = svg.getBoundingClientRect();
                                const scale = (vb && vb.width > 0) ? (svgRect.width / vb.width) : 1;
                                let minEffectivePx = Infinity;
                                svg.querySelectorAll('.nodeLabel, text.actor, .messageText, text.noteText').forEach((el) => {
                                    if (!(el.textContent || '').trim()) return;
                                    const fs = parseFloat(getComputedStyle(el).fontSize) || 16;
                                    minEffectivePx = Math.min(minEffectivePx, fs * scale);
                                });
                                const overflowing = container.clientWidth > 0 && container.scrollWidth > container.clientWidth;
                                return {
                                    idx,
                                    overflowX: getComputedStyle(container).overflowX,
                                    minEffectivePx,
                                    overflowing,
                                    tabindex: container.getAttribute('tabindex'),
                                    role: container.getAttribute('role'),
                                    regionLabel: container.getAttribute('aria-label') || ''
                                };
                            });
                        }"""
                    )
                    for m_item in mobile_audit:
                        assert m_item["overflowX"] == "auto"
                        assert m_item["minEffectivePx"] >= 12.0, (
                            f"{rel_url} diagram {m_item['idx']} mobile text shrunk to {m_item['minEffectivePx']:.1f}px < 12px"
                        )
                        if m_item["overflowing"]:
                            overflowing_mobile_regions += 1
                            assert m_item["tabindex"] == "0", (
                                f"{rel_url} overflowing diagram missing tabindex='0'"
                            )
                            assert m_item["role"] == "region", (
                                f"{rel_url} overflowing diagram missing role='region'"
                            )
                            assert m_item["regionLabel"], (
                                f"{rel_url} overflowing diagram missing aria-label"
                            )

                assert rendered_diagram_count == 15, (
                    f"Expected 15 rendered Mermaid diagrams across built HTML, found {rendered_diagram_count}"
                )
                assert overflowing_mobile_regions >= 2, (
                    "Expected wide workflow sequence diagrams to scroll horizontally on 375px viewport"
                )
                diag_page.close()

                # --------------------------------------------------------------
                # 3. Showcase SC 2.5.3 Label in Name & 4 roving tabindex tablists
                # --------------------------------------------------------------
                idx_page = context.new_page()
                idx_page.goto(
                    f"http://127.0.0.1:{port}/index.html",
                    wait_until="load",
                )

                # Verify [role="heading"] aria-level hierarchy & caption styling in Chromium
                idx_heading_audit = idx_page.evaluate(
                    """() => {
                        return Array.from(document.querySelectorAll('[role="heading"]')).map((el) => {
                            const cs = getComputedStyle(el);
                            return {
                                text: (el.textContent || '').trim(),
                                tag: el.tagName,
                                ariaLevel: el.getAttribute('aria-level'),
                                inSidebar: !!el.closest('.sidebar-tree'),
                                inToctreeWrapper: !!el.closest('.toctree-wrapper'),
                                borderBottomStyle: cs.borderBottomStyle,
                                borderBottomWidth: parseFloat(cs.borderBottomWidth)
                            };
                        });
                    }"""
                )
                assert len(idx_heading_audit) >= 10
                idx_wrapper_texts = [h["text"] for h in idx_heading_audit if h["inToctreeWrapper"]]
                assert "Maintainer & Contributor:" in idx_wrapper_texts
                for h_item in idx_heading_audit:
                    assert h_item["ariaLevel"] in {"1", "2", "3", "4", "5", "6"}, (
                        f"index.html [role='heading'] ({h_item['text']!r}) has invalid aria-level={h_item['ariaLevel']!r}"
                    )
                    if h_item["inSidebar"]:
                        assert h_item["ariaLevel"] == "2"
                    if h_item["inToctreeWrapper"]:
                        assert h_item["ariaLevel"] == "3"
                        assert h_item["borderBottomWidth"] == 0.0, (
                            f"index.html caption {h_item['text']!r} unexpectedly picked up h2 bottom border"
                        )

                contrib_page = context.new_page()
                contrib_page.goto(
                    f"http://127.0.0.1:{port}/reference/contributing.html",
                    wait_until="load",
                )
                contrib_heading_audit = contrib_page.evaluate(
                    """() => {
                        return Array.from(document.querySelectorAll('[role="heading"]')).map((el) => {
                            const cs = getComputedStyle(el);
                            return {
                                text: (el.textContent || '').trim(),
                                ariaLevel: el.getAttribute('aria-level'),
                                inSidebar: !!el.closest('.sidebar-tree'),
                                inToctreeWrapper: !!el.closest('.toctree-wrapper'),
                                borderBottomWidth: parseFloat(cs.borderBottomWidth)
                            };
                        });
                    }"""
                )
                contrib_wrapper = [h for h in contrib_heading_audit if h["inToctreeWrapper"]]
                assert contrib_wrapper == []
                contrib_sidebar = [h for h in contrib_heading_audit if h["inSidebar"]]
                assert len(contrib_sidebar) == 5
                assert any("Maintainer & Contributor" in h["text"] for h in contrib_sidebar)
                for h_item in contrib_heading_audit:
                    assert h_item["ariaLevel"] in {"1", "2", "3", "4", "5", "6"}
                    assert h_item["borderBottomWidth"] == 0.0
                    if h_item["inSidebar"]:
                        assert h_item["ariaLevel"] == "2"
                        assert h_item["borderBottomWidth"] == 0.0
                contrib_page.close()

                sc_253_checks = idx_page.evaluate(
                    """() => {
                        const cleanVisible = (str) =>
                            str.replace(/^[^A-Za-z0-9]+/, '').replace(/\\s+/g, ' ').trim();
                        const ids = [
                            'ie-dataset-select',
                            'cdm-form-select',
                            'cdm-validate-btn',
                            'cdm-copy-btn',
                            'aud-scenario-select',
                            'aud-verify-btn',
                            'aud-copy-btn'
                        ];
                        return ids.map((id) => {
                            const el = document.getElementById(id);
                            let visible = '';
                            if (el.tagName === 'SELECT') {
                                const lbl = document.querySelector(`label[for="${id}"]`);
                                visible = lbl ? lbl.textContent : '';
                            } else {
                                visible = el.textContent || '';
                            }
                            const normVisible = cleanVisible(visible);
                            const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
                            return {
                                id,
                                normVisible,
                                aria,
                                startsWithVisible: aria.toLowerCase().startsWith(normVisible.toLowerCase())
                            };
                        });
                    }"""
                )
                for chk in sc_253_checks:
                    assert chk["normVisible"], f"Empty visible label for {chk['id']}"
                    assert chk["startsWithVisible"], (
                        f"SC 2.5.3 violation on #{chk['id']}: aria-label={chk['aria']!r} "
                        f"does not start with visible text {chk['normVisible']!r}"
                    )

                # Test all 4 tablists for ArrowRight, ArrowLeft, End, Home + roving tabindex
                tablists_meta = [
                    (None, '[role="tablist"][aria-label="Live Explorer Persona Selection"]'),
                    ("#tab-integration", '[role="tablist"][aria-label="Output View"]'),
                    ("#tab-datamanager", '[role="tablist"][aria-label="Form Schema View"]'),
                    ("#tab-auditor", '[role="tablist"][aria-label="Auditor View"]'),
                ]
                for parent_persona_btn, tablist_sel in tablists_meta:
                    if parent_persona_btn:
                        idx_page.locator(parent_persona_btn).click()

                    tabs = idx_page.locator(f"{tablist_sel} [role='tab']")
                    count = tabs.count()
                    assert count >= 2

                    # Initial state: first tab selected with tabindex="0", others "-1"
                    tabs.nth(0).click()
                    tabs.nth(0).focus()
                    for i in range(count):
                        expected_ti = "0" if i == 0 else "-1"
                        assert tabs.nth(i).get_attribute("tabindex") == expected_ti

                    # ArrowRight -> index 1
                    idx_page.keyboard.press("ArrowRight")
                    assert tabs.nth(1).get_attribute("aria-selected") == "true"
                    assert tabs.nth(1).get_attribute("tabindex") == "0"
                    assert tabs.nth(0).get_attribute("tabindex") == "-1"
                    focused_id = idx_page.evaluate(
                        "document.activeElement && document.activeElement.id"
                    )
                    assert focused_id == tabs.nth(1).get_attribute("id")

                    # End -> last index
                    idx_page.keyboard.press("End")
                    assert tabs.nth(count - 1).get_attribute("aria-selected") == "true"
                    assert tabs.nth(count - 1).get_attribute("tabindex") == "0"
                    focused_id = idx_page.evaluate(
                        "document.activeElement && document.activeElement.id"
                    )
                    assert focused_id == tabs.nth(count - 1).get_attribute("id")

                    # Home -> index 0
                    idx_page.keyboard.press("Home")
                    assert tabs.nth(0).get_attribute("aria-selected") == "true"
                    assert tabs.nth(0).get_attribute("tabindex") == "0"
                    focused_id = idx_page.evaluate(
                        "document.activeElement && document.activeElement.id"
                    )
                    assert focused_id == tabs.nth(0).get_attribute("id")

                    # ArrowLeft from index 0 wraps to count - 1
                    idx_page.keyboard.press("ArrowLeft")
                    assert tabs.nth(count - 1).get_attribute("aria-selected") == "true"
                    assert tabs.nth(count - 1).get_attribute("tabindex") == "0"
                    focused_id = idx_page.evaluate(
                        "document.activeElement && document.activeElement.id"
                    )
                    assert focused_id == tabs.nth(count - 1).get_attribute("id")

                # --------------------------------------------------------------
                # 4. Showcase index.html: forced-colors, prefers-contrast: more,
                #    focus-visible, 375px mobile layout, clipboard & hashchange
                # --------------------------------------------------------------
                idx_page_errors: list[str] = []
                idx_page.on("pageerror", lambda exc: idx_page_errors.append(str(exc)))
                idx_page.add_script_tag(content=contrast_helper_js)
                idx_page.evaluate(
                    """() => {
                        window.__unhandledRejections = [];
                        window.addEventListener('unhandledrejection', (ev) => {
                            ev.preventDefault();
                            window.__unhandledRejections.push(
                                String(ev.reason && (ev.reason.message || ev.reason))
                            );
                        });
                    }"""
                )

                # Reset to Integration Engineer / Tabular View at desktop viewport
                idx_page.set_viewport_size({"width": 1280, "height": 900})
                idx_page.locator("#tab-integration").click()
                idx_page.locator("#ie-tab-table").click()

                # 4a. Default mode :focus-visible on .explorer-copy-btn and .quick-start-callout a
                idx_page.locator("#ie-tab-json").click()
                idx_page.locator("#ie-copy-btn").focus()
                idx_page.keyboard.press("Shift")
                copy_focus_default = idx_page.evaluate(
                    """() => {
                        const btn = document.getElementById('ie-copy-btn');
                        const cs = getComputedStyle(btn);
                        return {
                            matches: btn.matches(':focus-visible'),
                            style: cs.outlineStyle,
                            width: cs.outlineWidth
                        };
                    }"""
                )
                assert copy_focus_default == {
                    "matches": True,
                    "style": "solid",
                    "width": "2px",
                }

                callout_link = idx_page.locator("#panel-integration .quick-start-callout a")
                callout_link.focus()
                idx_page.keyboard.press("Shift")
                callout_focus_default = idx_page.evaluate(
                    """() => {
                        const link = document.querySelector('#panel-integration .quick-start-callout a');
                        const cs = getComputedStyle(link);
                        return {
                            matches: link.matches(':focus-visible'),
                            style: cs.outlineStyle,
                            width: cs.outlineWidth
                        };
                    }"""
                )
                assert callout_focus_default == {
                    "matches": True,
                    "style": "solid",
                    "width": "2px",
                }

                # 4b. @media (forced-colors: active) on index.html
                idx_page.emulate_media(forced_colors="active")
                idx_page.locator("#tab-integration").click()
                idx_page.locator("#ie-tab-table").click()
                fc_ie_audit = idx_page.evaluate(
                    """() => {
                        const probe = document.createElement('span');
                        probe.style.cssText =
                            'forced-color-adjust: none !important; background-color: Highlight !important; color: HighlightText !important; border: 1px solid CanvasText !important; display: none;';
                        document.body.appendChild(probe);
                        const probeCs = getComputedStyle(probe);
                        const sysHighlight = probeCs.backgroundColor;
                        const sysHighlightText = probeCs.color;
                        const sysCanvasText = probeCs.borderTopColor;
                        probe.remove();

                        const borderInfo = (el) => {
                            const cs = getComputedStyle(el);
                            const styles = [
                                cs.borderTopStyle,
                                cs.borderRightStyle,
                                cs.borderBottomStyle,
                                cs.borderLeftStyle
                            ];
                            const widths = [
                                parseFloat(cs.borderTopWidth),
                                parseFloat(cs.borderRightWidth),
                                parseFloat(cs.borderBottomWidth),
                                parseFloat(cs.borderLeftWidth)
                            ];
                            return {
                                bg: cs.backgroundColor,
                                color: cs.color,
                                borderColor: cs.borderTopColor,
                                allSolid: styles.every((s) => s === 'solid'),
                                minWidth: Math.min(...widths)
                            };
                        };

                        return {
                            sysHighlight,
                            sysHighlightText,
                            sysCanvasText,
                            activePersonaTab: borderInfo(document.getElementById('tab-integration')),
                            inactivePersonaTab: borderInfo(document.getElementById('tab-datamanager')),
                            activeOutputTab: borderInfo(document.getElementById('ie-tab-table')),
                            inactiveOutputTab: borderInfo(document.getElementById('ie-tab-json')),
                            clientBadge: borderInfo(document.querySelector('.showcase-client-badge')),
                            statusBadge: borderInfo(document.querySelector('#ie-view-table .status-badge'))
                        };
                    }"""
                )
                for active_key in ("activePersonaTab", "activeOutputTab"):
                    act = fc_ie_audit[active_key]
                    assert act["bg"] == fc_ie_audit["sysHighlight"], (
                        f"forced-colors {active_key} bg {act['bg']} != Highlight {fc_ie_audit['sysHighlight']}"
                    )
                    assert act["color"] == fc_ie_audit["sysHighlightText"], (
                        f"forced-colors {active_key} color {act['color']} != HighlightText {fc_ie_audit['sysHighlightText']}"
                    )
                    assert act["allSolid"] is True
                    assert act["minWidth"] >= 1.0

                assert (
                    fc_ie_audit["activePersonaTab"]["bg"] != fc_ie_audit["inactivePersonaTab"]["bg"]
                )
                assert (
                    fc_ie_audit["activeOutputTab"]["bg"] != fc_ie_audit["inactiveOutputTab"]["bg"]
                )
                for bordered_key in (
                    "inactivePersonaTab",
                    "inactiveOutputTab",
                    "clientBadge",
                    "statusBadge",
                ):
                    info = fc_ie_audit[bordered_key]
                    assert info["allSolid"] is True, (
                        f"forced-colors {bordered_key} missing solid border"
                    )
                    assert info["minWidth"] >= 1.0, (
                        f"forced-colors {bordered_key} border width {info['minWidth']} < 1px"
                    )
                for canvas_badge_key in ("clientBadge", "statusBadge"):
                    assert (
                        fc_ie_audit[canvas_badge_key]["borderColor"] == fc_ie_audit["sysCanvasText"]
                    )

                idx_page.locator("#tab-datamanager").click()
                idx_page.locator("#cdm-tab-crf").click()
                fc_cdm_badges = idx_page.evaluate(
                    """() => {
                        const probe = document.createElement('span');
                        probe.style.cssText =
                            'forced-color-adjust: none !important; border: 1px solid CanvasText !important; display: none;';
                        document.body.appendChild(probe);
                        const sysCanvasText = getComputedStyle(probe).borderTopColor;
                        probe.remove();

                        const check = (sel) => {
                            const el = document.querySelector(sel);
                            const cs = getComputedStyle(el);
                            return {
                                allSolid: [
                                    cs.borderTopStyle,
                                    cs.borderRightStyle,
                                    cs.borderBottomStyle,
                                    cs.borderLeftStyle
                                ].every((s) => s === 'solid'),
                                minWidth: Math.min(
                                    parseFloat(cs.borderTopWidth),
                                    parseFloat(cs.borderRightWidth),
                                    parseFloat(cs.borderBottomWidth),
                                    parseFloat(cs.borderLeftWidth)
                                ),
                                borderColor: cs.borderTopColor
                            };
                        };
                        return {
                            sysCanvasText,
                            crfTag: check('#cdm-view-crf .crf-tag'),
                            crfRequired: check('#cdm-view-crf .crf-badge-required'),
                            crfOptional: check('#cdm-view-crf .crf-badge-optional')
                        };
                    }"""
                )
                for crf_key in ("crfTag", "crfRequired", "crfOptional"):
                    c_info = fc_cdm_badges[crf_key]
                    assert c_info["allSolid"] is True
                    assert c_info["minWidth"] >= 1.0
                    assert c_info["borderColor"] == fc_cdm_badges["sysCanvasText"]

                idx_page.locator("#cdm-tab-json").click()
                idx_page.locator("#cdm-copy-btn").focus()
                idx_page.keyboard.press("Shift")
                fc_copy_focus = idx_page.evaluate(
                    """() => {
                        const btn = document.getElementById('cdm-copy-btn');
                        const cs = getComputedStyle(btn);
                        return {
                            allSolid: [
                                cs.borderTopStyle,
                                cs.borderRightStyle,
                                cs.borderBottomStyle,
                                cs.borderLeftStyle
                            ].every((s) => s === 'solid'),
                            minWidth: Math.min(
                                parseFloat(cs.borderTopWidth),
                                parseFloat(cs.borderRightWidth),
                                parseFloat(cs.borderBottomWidth),
                                parseFloat(cs.borderLeftWidth)
                            ),
                            matchesFocus: btn.matches(':focus-visible'),
                            outlineStyle: cs.outlineStyle,
                            outlineWidth: cs.outlineWidth
                        };
                    }"""
                )
                assert fc_copy_focus["allSolid"] is True
                assert fc_copy_focus["minWidth"] >= 1.0
                assert fc_copy_focus["matchesFocus"] is True
                assert fc_copy_focus["outlineStyle"] == "solid"
                assert fc_copy_focus["outlineWidth"] == "2px"

                idx_page.locator("#panel-datamanager .quick-start-callout a").first.focus()
                idx_page.keyboard.press("Shift")
                fc_callout_focus = idx_page.evaluate(
                    """() => {
                        const link = document.querySelector('#panel-datamanager .quick-start-callout a');
                        const cs = getComputedStyle(link);
                        return {
                            matches: link.matches(':focus-visible'),
                            style: cs.outlineStyle,
                            width: cs.outlineWidth
                        };
                    }"""
                )
                assert fc_callout_focus == {
                    "matches": True,
                    "style": "solid",
                    "width": "2px",
                }
                fc_selects = idx_page.evaluate(
                    """() => ['#ie-dataset-select', '#cdm-form-select', '#aud-scenario-select'].map((sel) => {
                        const el = document.querySelector(sel);
                        const cs = getComputedStyle(el);
                        return {
                            sel,
                            appearance: cs.appearance,
                            bgImage: cs.backgroundImage,
                            borderSolid: cs.borderTopStyle === 'solid' && parseFloat(cs.borderTopWidth) >= 1
                        };
                    })"""
                )
                for sel_info in fc_selects:
                    assert sel_info["appearance"] != "none", (
                        f"forced-colors {sel_info['sel']} appearance stayed 'none' (hides native chevron)"
                    )
                    assert sel_info["bgImage"] == "none", (
                        f"forced-colors {sel_info['sel']} retained custom background-image"
                    )
                    assert sel_info["borderSolid"] is True
                idx_page.emulate_media(forced_colors="none")

                # 4b-2. .explorer-select SVG chevron contrast >= 4.5:1 in dark & auto-dark modes
                for scheme, theme_attr in (("light", "light"), ("dark", "dark"), ("dark", "auto")):
                    idx_page.emulate_media(color_scheme=scheme, contrast="no-preference")
                    idx_page.evaluate(f"document.body.setAttribute('data-theme', '{theme_attr}')")
                    chevron_audit = idx_page.evaluate(
                        """async () => {
                            const h = window.__wcagHelpers;
                            const el = document.getElementById('ie-dataset-select');
                            const cs = getComputedStyle(el);
                            const bg = h.effectiveBg(el);
                            const m = cs.backgroundImage.match(/^url\\("?(data:image\\/svg\\+xml[^")]+)"?\\)$/);
                            if (!m) return { ok: false, reason: cs.backgroundImage };
                            const commaIdx = m[1].indexOf(',');
                            const decoded = decodeURIComponent(m[1].slice(commaIdx + 1));
                            const strokeMatch = decoded.match(/stroke=['"](#[0-9a-fA-F]{6})['"]/);
                            if (!strokeMatch) return { ok: false, reason: decoded };
                            const hex = strokeMatch[1];
                            const fg = {
                                r: parseInt(hex.slice(1, 3), 16),
                                g: parseInt(hex.slice(3, 5), 16),
                                b: parseInt(hex.slice(5, 7), 16),
                                a: 1
                            };
                            // Also rasterize onto a canvas to verify rendered non-transparent chevron pixels
                            const img = new Image();
                            img.src = m[1];
                            await new Promise((res, rej) => { img.onload = res; img.onerror = rej; });
                            const canvas = document.createElement('canvas');
                            canvas.width = 24;
                            canvas.height = 24;
                            const ctx = canvas.getContext('2d');
                            ctx.fillStyle = `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`;
                            ctx.fillRect(0, 0, 24, 24);
                            ctx.drawImage(img, 0, 0, 24, 24);
                            const data = ctx.getImageData(0, 0, 24, 24).data;
                            let maxPixelContrast = 1;
                            for (let i = 0; i < data.length; i += 4) {
                                const px = { r: data[i], g: data[i + 1], b: data[i + 2], a: 1 };
                                maxPixelContrast = Math.max(maxPixelContrast, h.ratio(px, bg));
                            }
                            return {
                                ok: true,
                                strokeRatio: h.ratio(fg, bg),
                                maxPixelContrast
                            };
                        }"""
                    )
                    assert chevron_audit["ok"] is True, f"Invalid select SVG: {chevron_audit}"
                    assert chevron_audit["strokeRatio"] >= 4.5, (
                        f"Select chevron stroke contrast {chevron_audit['strokeRatio']:.2f}:1 < 4.5:1 "
                        f"for scheme={scheme}, data-theme={theme_attr}"
                    )
                    assert chevron_audit["maxPixelContrast"] >= 4.5
                idx_page.emulate_media(color_scheme="light", contrast="no-preference")

                # 4c. @media (prefers-contrast: more) >= 7.0:1 across auto, light, and dark modes on index.html
                for color_scheme, theme in (
                    ("light", "auto"),
                    ("dark", "auto"),
                    ("light", "light"),
                    ("dark", "dark"),
                ):
                    idx_page.emulate_media(contrast="more", color_scheme=color_scheme)
                    idx_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                    idx_page.locator("#tab-integration").click()
                    idx_page.locator("#ie-tab-table").click()
                    idx_page.locator("#ie-dataset-select").select_option("records")
                    ie_records_hc = idx_page.evaluate(
                        """() => {
                            const h = window.__wcagHelpers;
                            const badges = Array.from(document.querySelectorAll('#ie-view-table .status-badge'));
                            return {
                                clientBadge: h.elementContrast(document.querySelector('.showcase-client-badge')),
                                ieMetricBadge: h.elementContrast(document.getElementById('ie-metric-badge')),
                                minStatusSuccess: Math.min(...badges.map((b) => h.elementContrast(b)))
                            };
                        }"""
                    )
                    idx_page.locator("#ie-dataset-select").select_option("queries")
                    ie_queries_hc = idx_page.evaluate(
                        """() => {
                            const h = window.__wcagHelpers;
                            const badges = Array.from(document.querySelectorAll('#ie-view-table .status-badge'));
                            return {
                                minStatusQueries: Math.min(...badges.map((b) => h.elementContrast(b)))
                            };
                        }"""
                    )
                    idx_page.locator("#ie-dataset-select").select_option("records")

                    idx_page.locator("#tab-datamanager").click()
                    idx_page.locator("#cdm-tab-crf").click()
                    cdm_hc = idx_page.evaluate(
                        """() => {
                            const h = window.__wcagHelpers;
                            const hlSpans = Array.from(document.querySelectorAll('div.highlight span[class]'))
                                .filter((sp) => sp.getClientRects().length > 0 && (sp.textContent || '').trim());
                            return {
                                cdmMetricBadge: h.elementContrast(document.getElementById('cdm-metric-badge')),
                                crfTag: h.elementContrast(document.querySelector('#cdm-view-crf .crf-tag')),
                                crfRequired: h.elementContrast(document.querySelector('#cdm-view-crf .crf-badge-required')),
                                crfOptional: h.elementContrast(document.querySelector('#cdm-view-crf .crf-badge-optional')),
                                activeTab: h.elementContrast(document.getElementById('tab-datamanager')),
                                inactiveTab: h.elementContrast(document.getElementById('tab-integration')),
                                validateBtn: h.elementContrast(document.getElementById('cdm-validate-btn')),
                                selectCtrl: h.elementContrast(document.getElementById('cdm-form-select')),
                                minHighlightSpan: hlSpans.length ? Math.min(...hlSpans.map((sp) => h.elementContrast(sp))) : 7.5
                            };
                        }"""
                    )
                    combined_hc = {**ie_records_hc, **ie_queries_hc, **cdm_hc}
                    for badge_name, ratio_val in combined_hc.items():
                        assert ratio_val >= 7.0, (
                            f"index.html prefers-contrast: more (scheme={color_scheme}, theme={theme}) "
                            f"{badge_name} contrast {ratio_val:.2f}:1 < 7.0:1"
                        )

                idx_page.emulate_media(contrast="no-preference", color_scheme="light")
                idx_page.evaluate("document.body.setAttribute('data-theme', 'light')")

                # 4c-2. Role card order parity, reduced-motion instant launch + focus into showcase,
                #       REST envelope + 4 queries + nested recordData, live button announcements, and VPAT tabs
                role_order_audit = idx_page.evaluate(
                    """() => ({
                        cards: Array.from(document.querySelectorAll('.role-card-launch-btn')).map(
                            (b) => b.getAttribute('data-persona-target')
                        ),
                        tabs: Array.from(document.querySelectorAll('.showcase-persona-tab')).map(
                            (t) => t.getAttribute('data-target')
                        )
                    })"""
                )
                assert role_order_audit["cards"] == [
                    "panel-integration",
                    "panel-datamanager",
                    "panel-auditor",
                ]
                assert role_order_audit["cards"] == role_order_audit["tabs"]

                idx_page.emulate_media(reduced_motion="reduce")
                rm_transitions = idx_page.evaluate(
                    """() => ({
                        roleCard: getComputedStyle(document.querySelector('.role-card')).transitionDuration,
                        personaTab: getComputedStyle(document.getElementById('tab-integration')).transitionDuration,
                        runBtn: getComputedStyle(document.getElementById('cdm-validate-btn')).transitionDuration,
                        copyBtn: getComputedStyle(document.getElementById('ie-copy-btn')).transitionDuration
                    })"""
                )
                assert rm_transitions == {
                    "roleCard": "0s",
                    "personaTab": "0s",
                    "runBtn": "0s",
                    "copyBtn": "0s",
                }
                idx_page.evaluate("globalThis.scrollTo({ top: 0, behavior: 'auto' })")
                cdm_launch_btn = idx_page.locator(
                    '.role-card-launch-btn[data-persona-target="panel-datamanager"]'
                )
                cdm_launch_btn.focus()
                idx_page.keyboard.press("Enter")
                after_enter = idx_page.evaluate(
                    """() => ({
                        activeId: document.activeElement && document.activeElement.id,
                        inShowcase: !!(
                            document.activeElement &&
                            document.activeElement.closest('#interactive-showcase-root')
                        ),
                        scrollY: window.scrollY
                    })"""
                )
                assert after_enter["activeId"] == "tab-datamanager"
                assert after_enter["inShowcase"] is True
                assert after_enter["scrollY"] > 0
                idx_page.keyboard.press("Tab")
                after_tab = idx_page.evaluate(
                    """() => ({
                        activeId: document.activeElement && document.activeElement.id,
                        inShowcase: !!(
                            document.activeElement &&
                            document.activeElement.closest('#interactive-showcase-root')
                        ),
                        inRoleCard: !!(
                            document.activeElement &&
                            document.activeElement.closest('.role-card')
                        )
                    })"""
                )
                assert after_tab["inShowcase"] is True
                assert after_tab["inRoleCard"] is False
                idx_page.emulate_media(reduced_motion="no-preference")

                # REST mode envelope, nested recordData, and 4 Queries verification
                idx_page.locator("#tab-integration").click()
                idx_page.locator("#ie-dataset-select").select_option("records")
                idx_page.locator("#ie-mode-rest").click()
                rest_records_payload = json.loads(
                    idx_page.evaluate("document.querySelector('#ie-json-pre code').textContent")
                )
                assert set(rest_records_payload.keys()) == {"metadata", "pagination", "data"}
                assert rest_records_payload["pagination"]["size"] == 10
                assert rest_records_payload["pagination"]["totalElements"] == 15
                assert isinstance(rest_records_payload["data"][0]["recordData"], dict)

                idx_page.locator("#ie-dataset-select").select_option("queries")
                rest_queries_payload = json.loads(
                    idx_page.evaluate("document.querySelector('#ie-json-pre code').textContent")
                )
                assert set(rest_queries_payload.keys()) == {"metadata", "pagination", "data"}
                assert rest_queries_payload["pagination"]["size"] == 25
                assert len(rest_queries_payload["data"]) == 4
                assert rest_queries_payload["data"][0]["annotationId"] == 701
                assert rest_queries_payload["data"][0]["recordId"] == 611
                idx_page.locator("#ie-mode-sdk").click()
                idx_page.locator("#ie-dataset-select").select_option("records")

                # Live button announcements & VPAT sub-tab labels
                idx_page.locator("#tab-datamanager").click()
                cdm_b0 = idx_page.evaluate(
                    "document.getElementById('cdm-metric-badge').textContent"
                )
                idx_page.locator("#cdm-validate-btn").click()
                cdm_b1 = idx_page.evaluate(
                    "document.getElementById('cdm-metric-badge').textContent"
                )
                idx_page.locator("#cdm-validate-btn").click()
                cdm_b2 = idx_page.evaluate(
                    "document.getElementById('cdm-metric-badge').textContent"
                )
                assert cdm_b1 != cdm_b0
                assert cdm_b2 != cdm_b1

                idx_page.locator("#tab-auditor").click()
                aud_b0 = idx_page.evaluate(
                    "document.getElementById('aud-metric-badge').textContent"
                )
                idx_page.locator("#aud-verify-btn").click()
                aud_b1 = idx_page.evaluate(
                    "document.getElementById('aud-metric-badge').textContent"
                )
                assert aud_b1 != aud_b0
                idx_page.locator("#aud-scenario-select").select_option("vpat")
                vpat_tabs = idx_page.evaluate(
                    """() => ({
                        table: document.getElementById('aud-tab-table').textContent,
                        json: document.getElementById('aud-tab-json').textContent
                    })"""
                )
                assert vpat_tabs == {
                    "table": "VPAT Conformance Criteria",
                    "json": "VPAT Conformance JSON",
                }
                idx_page.locator("#aud-scenario-select").select_option("part11")
                part11_tabs = idx_page.evaluate(
                    """() => ({
                        table: document.getElementById('aud-tab-table').textContent,
                        json: document.getElementById('aud-tab-json').textContent
                    })"""
                )
                assert part11_tabs == {
                    "table": "Audit Trail Records",
                    "json": "Signed Audit JSON",
                }

                # 4d. Mobile 375x812 viewport: wrapped tabs, non-overlapping JSON copy buttons, >= 44px targets
                idx_page.set_viewport_size({"width": 375, "height": 812})
                idx_page.evaluate("window.dispatchEvent(new Event('resize'))")
                idx_page.wait_for_timeout(60)

                mobile_tabs_audit = idx_page.evaluate(
                    """() => {
                        const container = document.querySelector('.showcase-persona-tabs');
                        const cRect = container.getBoundingClientRect();
                        const cStyle = getComputedStyle(container);
                        const tabs = ['tab-integration', 'tab-datamanager', 'tab-auditor'].map((id) => {
                            const el = document.getElementById(id);
                            const r = el.getBoundingClientRect();
                            const cs = getComputedStyle(el);
                            return {
                                id,
                                left: r.left,
                                right: r.right,
                                height: r.height,
                                whiteSpace: cs.whiteSpace,
                                fitsHorizontally: r.left >= cRect.left - 0.5 && r.right <= cRect.right + 0.5
                            };
                        });
                        return {
                            flexWrap: cStyle.flexWrap,
                            overflowing: container.scrollWidth > container.clientWidth + 1,
                            tabs
                        };
                    }"""
                )
                assert mobile_tabs_audit["flexWrap"] == "wrap"
                assert mobile_tabs_audit["overflowing"] is False, (
                    "Persona tabs container horizontally overflows at 375px"
                )
                for t_info in mobile_tabs_audit["tabs"]:
                    assert t_info["whiteSpace"] == "normal"
                    assert t_info["fitsHorizontally"] is True, (
                        f"#{t_info['id']} horizontally clipped at 375px: {t_info}"
                    )
                    assert t_info["height"] >= 44.0, (
                        f"#{t_info['id']} mobile height {t_info['height']:.1f}px < 44px"
                    )

                panel_json_specs = [
                    (
                        "#tab-integration",
                        "#ie-tab-table",
                        "#ie-tab-json",
                        "ie-view-json",
                        "ie-copy-btn",
                        "ie-json-pre",
                        "#ie-dataset-select",
                        True,
                    ),
                    (
                        "#tab-datamanager",
                        "#cdm-tab-crf",
                        "#cdm-tab-json",
                        "cdm-view-json",
                        "cdm-copy-btn",
                        "cdm-json-pre",
                        "#cdm-form-select",
                        False,
                    ),
                    (
                        "#tab-auditor",
                        "#aud-tab-table",
                        "#aud-tab-json",
                        "aud-view-json",
                        "aud-copy-btn",
                        "aud-json-pre",
                        "#aud-scenario-select",
                        False,
                    ),
                ]
                for (
                    persona_sel,
                    primary_subtab_sel,
                    json_subtab_sel,
                    wrapper_id,
                    copy_id,
                    pre_id,
                    select_sel,
                    has_mode_pills,
                ) in panel_json_specs:
                    idx_page.locator(persona_sel).click()
                    idx_page.locator(primary_subtab_sel).click()
                    # Hidden JSON wrapper must stay display: none
                    hidden_display = idx_page.evaluate(
                        f"getComputedStyle(document.getElementById('{wrapper_id}')).display"
                    )
                    assert hidden_display == "none", (
                        f"Hidden #{wrapper_id} must remain display: none, got {hidden_display!r}"
                    )

                    idx_page.locator(json_subtab_sel).click()
                    mobile_panel_audit = idx_page.evaluate(
                        f"""() => {{
                            const intersects = (a, b) => !(
                                a.right <= b.left ||
                                a.left >= b.right ||
                                a.bottom <= b.top ||
                                a.top >= b.bottom
                            );
                            const wrapper = document.getElementById('{wrapper_id}');
                            const copyBtn = document.getElementById('{copy_id}');
                            const pre = document.getElementById('{pre_id}');
                            const selectEl = document.querySelector('{select_sel}');
                            const outTabs = Array.from(wrapper.parentElement.querySelectorAll('.explorer-output-tab'));
                            const modePills = {str(has_mode_pills).lower()}
                                ? Array.from(wrapper.parentElement.querySelectorAll('.mode-pill-btn'))
                                : [];

                            const btnBefore = copyBtn.getBoundingClientRect();
                            const preBefore = pre.getBoundingClientRect();
                            const overlapBefore = intersects(btnBefore, preBefore);

                            pre.scrollTop = pre.scrollHeight;
                            pre.scrollLeft = pre.scrollWidth;
                            const btnAfter = copyBtn.getBoundingClientRect();
                            const preAfter = pre.getBoundingClientRect();
                            const overlapAfter = intersects(btnAfter, preAfter);

                            return {{
                                wrapperDisplay: getComputedStyle(wrapper).display,
                                wrapperFlexDir: getComputedStyle(wrapper).flexDirection,
                                overlapBefore,
                                overlapAfter,
                                btnAbovePre: btnAfter.bottom <= preAfter.top + 0.5,
                                copyHeight: btnBefore.height,
                                selectHeight: selectEl.getBoundingClientRect().height,
                                minOutputTabHeight: Math.min(...outTabs.map((t) => t.getBoundingClientRect().height)),
                                minModePillHeight: modePills.length
                                    ? Math.min(...modePills.map((p) => p.getBoundingClientRect().height))
                                    : 44.0
                            }};
                        }}"""
                    )
                    assert mobile_panel_audit["wrapperDisplay"] == "flex"
                    assert mobile_panel_audit["wrapperFlexDir"] == "column"
                    assert mobile_panel_audit["overlapBefore"] is False, (
                        f"#{copy_id} overlaps #{pre_id} before scrolling at 375px"
                    )
                    assert mobile_panel_audit["overlapAfter"] is False, (
                        f"#{copy_id} overlaps #{pre_id} after scrolling at 375px"
                    )
                    assert mobile_panel_audit["btnAbovePre"] is True
                    assert mobile_panel_audit["copyHeight"] >= 44.0
                    assert mobile_panel_audit["selectHeight"] >= 44.0
                    assert mobile_panel_audit["minOutputTabHeight"] >= 44.0
                    assert mobile_panel_audit["minModePillHeight"] >= 44.0

                # 4e. Copy button dimension stability, clipboard failure/missing announcements, and hashchange preservation
                idx_page.set_viewport_size({"width": 1280, "height": 900})
                idx_page.locator("#tab-integration").click()
                idx_page.locator("#ie-tab-json").click()

                # Mock resolved clipboard to verify stable button dimensions and 1500ms reset
                idx_page.evaluate(
                    """() => {
                        Object.defineProperty(navigator, 'clipboard', {
                            configurable: true,
                            value: { writeText: () => Promise.resolve() }
                        });
                    }"""
                )
                dims_before = idx_page.evaluate(
                    """() => {
                        const r = document.getElementById('ie-copy-btn').getBoundingClientRect();
                        return { width: r.width, height: r.height };
                    }"""
                )
                idx_page.locator("#ie-copy-btn").click()
                idx_page.wait_for_function(
                    "() => document.getElementById('ie-copy-btn').textContent === 'Copied!'"
                )
                dims_copied = idx_page.evaluate(
                    """() => {
                        const r = document.getElementById('ie-copy-btn').getBoundingClientRect();
                        return {
                            width: r.width,
                            height: r.height,
                            badge: document.getElementById('ie-metric-badge').textContent
                        };
                    }"""
                )
                assert abs(dims_copied["width"] - dims_before["width"]) < 0.5
                assert abs(dims_copied["height"] - dims_before["height"]) < 0.5
                assert "Copied to clipboard" in dims_copied["badge"]

                idx_page.wait_for_function(
                    "() => document.getElementById('ie-copy-btn').textContent === 'Copy JSON'",
                    timeout=3000,
                )

                # Mock rejected clipboard (NotAllowedError) across all 3 panels
                idx_page.evaluate(
                    """() => {
                        Object.defineProperty(navigator, 'clipboard', {
                            configurable: true,
                            value: {
                                writeText: () => Promise.reject(
                                    new DOMException('Write permission denied.', 'NotAllowedError')
                                )
                            }
                        });
                    }"""
                )
                for persona_sel, json_subtab_sel, copy_id, badge_id in [
                    ("#tab-integration", "#ie-tab-json", "ie-copy-btn", "ie-metric-badge"),
                    ("#tab-datamanager", "#cdm-tab-json", "cdm-copy-btn", "cdm-metric-badge"),
                    ("#tab-auditor", "#aud-tab-json", "aud-copy-btn", "aud-metric-badge"),
                ]:
                    idx_page.locator(persona_sel).click()
                    idx_page.locator(json_subtab_sel).click()
                    pre_click_rect = idx_page.evaluate(
                        f"""() => {{
                            const r = document.getElementById('{copy_id}').getBoundingClientRect();
                            return {{ width: r.width, height: r.height }};
                        }}"""
                    )
                    idx_page.locator(f"#{copy_id}").click()
                    idx_page.wait_for_function(
                        f"() => document.getElementById('{copy_id}').textContent === 'Copy failed'"
                    )
                    fail_state = idx_page.evaluate(
                        f"""() => {{
                            const h = window.__wcagHelpers;
                            const r = document.getElementById('{copy_id}').getBoundingClientRect();
                            const badgeEl = document.getElementById('{badge_id}');
                            return {{
                                width: r.width,
                                height: r.height,
                                badge: badgeEl.textContent,
                                hasDangerClass: badgeEl.classList.contains('explorer-metric-badge--danger'),
                                dangerContrast: h.elementContrast(badgeEl)
                            }};
                        }}"""
                    )
                    assert abs(fail_state["width"] - pre_click_rect["width"]) < 0.5
                    assert abs(fail_state["height"] - pre_click_rect["height"]) < 0.5
                    assert "Copy JSON failed" in fail_state["badge"]
                    assert "Clipboard unavailable" in fail_state["badge"]
                    assert fail_state["hasDangerClass"] is True
                    assert fail_state["dangerContrast"] >= 4.5

                # Verify prefers-contrast: more danger badge contrast >= 7.0:1 and 1500ms reset to baseline
                idx_page.emulate_media(contrast="more")
                hc_danger_contrast = idx_page.evaluate(
                    "window.__wcagHelpers.elementContrast(document.getElementById('aud-metric-badge'))"
                )
                assert hc_danger_contrast >= 7.0
                idx_page.emulate_media(contrast="no-preference")
                idx_page.wait_for_function(
                    """() => {
                        const b = document.getElementById('aud-metric-badge');
                        return !b.classList.contains('explorer-metric-badge--danger') &&
                            !b.textContent.includes('Copy JSON failed');
                    }""",
                    timeout=3000,
                )

                # Missing navigator.clipboard API fallback check
                idx_page.evaluate(
                    """() => {
                        Object.defineProperty(navigator, 'clipboard', {
                            configurable: true,
                            value: undefined
                        });
                    }"""
                )
                idx_page.locator("#aud-verify-btn").click()
                idx_page.locator("#aud-copy-btn").click()
                missing_clip_badge = idx_page.evaluate(
                    "document.getElementById('aud-metric-badge').textContent"
                )
                assert "Copy JSON failed" in missing_clip_badge
                assert "Clipboard unavailable" in missing_clip_badge

                idx_page.wait_for_timeout(80)
                unhandled = idx_page.evaluate("window.__unhandledRejections")
                assert unhandled == [], f"Unhandled clipboard rejections detected: {unhandled}"
                assert idx_page_errors == [], (
                    f"Page errors detected on index.html: {idx_page_errors}"
                )

                # In-page anchor navigation (#documentation-overview) preserves active persona panel
                for persona_btn_id, panel_id in [
                    ("tab-datamanager", "panel-datamanager"),
                    ("tab-auditor", "panel-auditor"),
                ]:
                    idx_page.locator(f"#{persona_btn_id}").click()
                    idx_page.evaluate("window.location.hash = '#documentation-overview'")
                    idx_page.wait_for_timeout(50)
                    hash_state = idx_page.evaluate(
                        f"""() => {{
                            const tab = document.getElementById('{persona_btn_id}');
                            const panel = document.getElementById('{panel_id}');
                            return {{
                                hash: window.location.hash,
                                tabSelected: tab.getAttribute('aria-selected'),
                                panelHidden: panel.hasAttribute('hidden'),
                                integrationHidden: document.getElementById('panel-integration').hasAttribute('hidden')
                            }};
                        }}"""
                    )
                    assert hash_state["hash"] == "#documentation-overview"
                    assert hash_state["tabSelected"] == "true", (
                        f"#{persona_btn_id} lost selection on #documentation-overview hashchange"
                    )
                    assert hash_state["panelHidden"] is False, (
                        f"#{panel_id} was hidden on #documentation-overview hashchange"
                    )
                    assert hash_state["integrationHidden"] is True

                idx_page.close()

                # --------------------------------------------------------------
                # 5. Keyboard navigation: .toctree-checkbox switches, responsive
                #    drawers (1280x900, 375x812, 320x640, >82em), py-modindex
                #    img.toggler controls, and 4-mode :focus-visible outlines
                # --------------------------------------------------------------
                nav_page = context.new_page()
                nav_page.set_viewport_size({"width": 1280, "height": 900})
                nav_page.goto(
                    f"http://127.0.0.1:{port}/index.html",
                    wait_until="load",
                )
                nav_page.add_script_tag(content=contrast_helper_js)

                # 5a. Sequential Tab order & .toctree-checkbox switches on index.html
                first_tab_stop = None
                index_tabbed_switches: list[str] = []
                index_toc_drawer_stops = 0
                for step_idx in range(90):
                    nav_page.keyboard.press("Tab")
                    stop = nav_page.evaluate(
                        """() => {
                            const el = document.activeElement;
                            if (!el || el === document.body) return null;
                            return {
                                id: el.id || '',
                                cls: String(el.className || ''),
                                inToc: !!el.closest('.toc-drawer')
                            };
                        }"""
                    )
                    if step_idx == 0:
                        first_tab_stop = stop
                    if stop:
                        if stop["inToc"]:
                            index_toc_drawer_stops += 1
                        if (
                            "toctree-checkbox" in stop["cls"]
                            and stop["id"] not in index_tabbed_switches
                        ):
                            index_tabbed_switches.append(stop["id"])

                assert first_tab_stop is not None and "skip-to-content" in first_tab_stop["cls"], (
                    f"Expected .skip-to-content as first Tab stop, got {first_tab_stop}"
                )
                assert index_toc_drawer_stops == 0, (
                    f"Closed .toc-drawer at 1280x900 trapped {index_toc_drawer_stops} Tab stops"
                )

                expected_index_switches = nav_page.evaluate(
                    """() => Array.from(document.querySelectorAll('.toctree-checkbox'))
                        .filter((el) => el.getClientRects().length > 0)
                        .map((el) => ({
                            id: el.id,
                            role: el.getAttribute('role'),
                            label: el.getAttribute('aria-label') || '',
                            w: el.getBoundingClientRect().width,
                            h: el.getBoundingClientRect().height
                        }))"""
                )
                assert len(expected_index_switches) >= 9
                for sw_meta in expected_index_switches:
                    assert sw_meta["role"] == "switch"
                    assert sw_meta["label"]
                    assert sw_meta["w"] >= 32.0 and sw_meta["h"] >= 32.0
                assert index_tabbed_switches == [s["id"] for s in expected_index_switches]

                # Space and Enter toggle child <ul> and expose/hide nested switches in Tab order
                nested_parent = nav_page.locator("#toctree-checkbox-5")
                nested_parent.focus()
                assert (
                    nav_page.evaluate("document.getElementById('toctree-checkbox-5').checked")
                    is False
                )
                assert (
                    nav_page.evaluate(
                        "getComputedStyle(document.querySelector('#toctree-checkbox-5 ~ ul')).display"
                    )
                    == "none"
                )
                nav_page.keyboard.press("Enter")
                assert (
                    nav_page.evaluate("document.getElementById('toctree-checkbox-5').checked")
                    is True
                )
                assert (
                    nav_page.evaluate(
                        "getComputedStyle(document.querySelector('#toctree-checkbox-5 ~ ul')).display"
                    )
                    == "block"
                )
                assert (
                    nav_page.evaluate(
                        "document.querySelector('#toctree-checkbox-5 ~ ul .toctree-checkbox').getClientRects().length > 0"
                    )
                    is True
                )
                nav_page.keyboard.press("Space")
                assert (
                    nav_page.evaluate("document.getElementById('toctree-checkbox-5').checked")
                    is False
                )
                assert (
                    nav_page.evaluate(
                        "getComputedStyle(document.querySelector('#toctree-checkbox-5 ~ ul')).display"
                    )
                    == "none"
                )

                # Verify sequential Tab reaches every visible .toctree-checkbox on reference/api/imednet.html
                nav_page.goto(
                    f"http://127.0.0.1:{port}/reference/api/imednet.html",
                    wait_until="load",
                )
                api_visible_switches = nav_page.evaluate(
                    """() => Array.from(document.querySelectorAll('.toctree-checkbox'))
                        .filter((el) => el.getClientRects().length > 0)
                        .map((el) => el.id)"""
                )
                assert len(api_visible_switches) >= 10
                api_tabbed_switches: list[str] = []
                for _ in range(130):
                    nav_page.keyboard.press("Tab")
                    sw_id = nav_page.evaluate(
                        """() => {
                            const el = document.activeElement;
                            return el && el.classList.contains('toctree-checkbox') ? el.id : '';
                        }"""
                    )
                    if sw_id and sw_id not in api_tabbed_switches:
                        api_tabbed_switches.append(sw_id)
                assert api_tabbed_switches == api_visible_switches

                # 5b. Responsive drawers & triggers across 1280x900, 375x812, 320x640, and 1400x900
                nav_page.goto(
                    f"http://127.0.0.1:{port}/index.html",
                    wait_until="load",
                )
                nav_page.add_script_tag(content=contrast_helper_js)

                drawer_1280 = nav_page.evaluate(
                    """() => {
                        const sd = document.querySelector('.sidebar-drawer');
                        const td = document.querySelector('.toc-drawer');
                        const tocTrigger = document.querySelector('.toc-content-icon');
                        return {
                            sdVis: getComputedStyle(sd).visibility,
                            sdInert: sd.inert,
                            tdVis: getComputedStyle(td).visibility,
                            tdInert: td.inert,
                            tocRole: tocTrigger.getAttribute('role'),
                            tocTabIndex: tocTrigger.tabIndex,
                            tocControls: tocTrigger.getAttribute('aria-controls'),
                            tocExpanded: tocTrigger.getAttribute('aria-expanded'),
                            tocLabel: tocTrigger.getAttribute('aria-label') || ''
                        };
                    }"""
                )
                assert drawer_1280["sdVis"] == "visible" and drawer_1280["sdInert"] is False
                assert drawer_1280["tdVis"] == "hidden" and drawer_1280["tdInert"] is True
                assert drawer_1280["tocRole"] == "button"
                assert drawer_1280["tocTabIndex"] == 0
                assert drawer_1280["tocControls"] == "furo-toc-drawer"
                assert drawer_1280["tocExpanded"] == "false"
                assert drawer_1280["tocLabel"]

                # Open .toc-drawer via Enter, verify focus moves inside, close via Escape -> focus returns
                nav_page.locator(".toc-content-icon").focus()
                nav_page.keyboard.press("Enter")
                toc_open_state = nav_page.evaluate(
                    """() => {
                        const td = document.querySelector('.toc-drawer');
                        const trigger = document.querySelector('.toc-content-icon');
                        return {
                            checked: document.getElementById('__toc').checked,
                            vis: getComputedStyle(td).visibility,
                            inert: td.inert,
                            expanded: trigger.getAttribute('aria-expanded'),
                            focusInside: td.contains(document.activeElement)
                        };
                    }"""
                )
                assert toc_open_state == {
                    "checked": True,
                    "vis": "visible",
                    "inert": False,
                    "expanded": "true",
                    "focusInside": True,
                }
                # Verify Tab and Shift+Tab wrap inside open modal .toc-drawer at 1280x900
                toc_bounds_1280 = nav_page.evaluate(
                    """() => {
                        const td = document.querySelector('.toc-drawer');
                        const items = Array.from(td.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'))
                            .filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
                        items[items.length - 1].focus();
                        return { count: items.length };
                    }"""
                )
                assert toc_bounds_1280["count"] >= 2
                nav_page.keyboard.press("Tab")
                assert (
                    nav_page.evaluate(
                        """() => {
                            const td = document.querySelector('.toc-drawer');
                            const items = Array.from(td.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'))
                                .filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
                            return document.activeElement === items[0];
                        }"""
                    )
                    is True
                )
                nav_page.keyboard.press("Shift+Tab")
                assert (
                    nav_page.evaluate(
                        """() => {
                            const td = document.querySelector('.toc-drawer');
                            const items = Array.from(td.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'))
                                .filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
                            return document.activeElement === items[items.length - 1];
                        }"""
                    )
                    is True
                )
                nav_page.keyboard.press("Escape")
                nav_page.wait_for_timeout(280)
                toc_closed_state = nav_page.evaluate(
                    """() => {
                        const td = document.querySelector('.toc-drawer');
                        const trigger = document.querySelector('.toc-content-icon');
                        return {
                            checked: document.getElementById('__toc').checked,
                            vis: getComputedStyle(td).visibility,
                            inert: td.inert,
                            expanded: trigger.getAttribute('aria-expanded'),
                            focusRestored: document.activeElement === trigger
                        };
                    }"""
                )
                assert toc_closed_state == {
                    "checked": False,
                    "vis": "hidden",
                    "inert": True,
                    "expanded": "false",
                    "focusRestored": True,
                }

                # Reopen via Space and verify TOC link click closes drawer & restores trigger focus
                nav_page.keyboard.press("Space")
                assert nav_page.evaluate("document.getElementById('__toc').checked") is True
                nav_page.locator(".toc-drawer .toc-tree a").nth(1).click()
                nav_page.wait_for_timeout(30)
                assert nav_page.evaluate("document.getElementById('__toc').checked") is False
                assert nav_page.evaluate("document.querySelector('.toc-drawer').inert") is True
                assert (
                    nav_page.evaluate(
                        "document.activeElement === document.querySelector('.toc-content-icon')"
                    )
                    is True
                )

                # Mobile viewports (375x812 and 320x640): both drawers closed, 0 off-screen Tab stops
                for vp_w, vp_h in ((375, 812), (320, 640)):
                    nav_page.set_viewport_size({"width": vp_w, "height": vp_h})
                    nav_page.wait_for_timeout(280)
                    closed_mobile = nav_page.evaluate(
                        """() => {
                            const sd = document.querySelector('.sidebar-drawer');
                            const td = document.querySelector('.toc-drawer');
                            return {
                                sdVis: getComputedStyle(sd).visibility,
                                sdInert: sd.inert,
                                tdVis: getComputedStyle(td).visibility,
                                tdInert: td.inert
                            };
                        }"""
                    )
                    assert closed_mobile == {
                        "sdVis": "hidden",
                        "sdInert": True,
                        "tdVis": "hidden",
                        "tdInert": True,
                    }, f"{vp_w}x{vp_h} closed drawers mismatch: {closed_mobile}"

                    # Press Tab 15 times and confirm 0 stops land inside closed drawers
                    nav_page.evaluate("document.activeElement && document.activeElement.blur()")
                    for _ in range(15):
                        nav_page.keyboard.press("Tab")
                        in_closed = nav_page.evaluate(
                            """() => {
                                const el = document.activeElement;
                                return !!(
                                    el && (el.closest('.sidebar-drawer') || el.closest('.toc-drawer'))
                                );
                            }"""
                        )
                        assert in_closed is False, (
                            f"Closed drawer trapped Tab stop at {vp_w}x{vp_h}"
                        )

                    # Keyboard toggle #__navigation and #__toc and restore on Escape
                    for trig_sel, input_id, drawer_sel in (
                        (".nav-overlay-icon", "__navigation", ".sidebar-drawer"),
                        (".toc-header-icon", "__toc", ".toc-drawer"),
                    ):
                        nav_page.locator(trig_sel).focus()
                        nav_page.keyboard.press("Space")
                        opened = nav_page.evaluate(
                            f"""() => {{
                                const dr = document.querySelector('{drawer_sel}');
                                const tr = document.querySelector('{trig_sel}');
                                return {{
                                    checked: document.getElementById('{input_id}').checked,
                                    vis: getComputedStyle(dr).visibility,
                                    inert: dr.inert,
                                    expanded: tr.getAttribute('aria-expanded'),
                                    focusInside: dr.contains(document.activeElement)
                                }};
                            }}"""
                        )
                        assert opened == {
                            "checked": True,
                            "vis": "visible",
                            "inert": False,
                            "expanded": "true",
                            "focusInside": True,
                        }
                        # Verify Tab and Shift+Tab wrap inside open modal drawer on mobile
                        nav_page.evaluate(
                            f"""() => {{
                                const dr = document.querySelector('{drawer_sel}');
                                const items = Array.from(dr.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'))
                                    .filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
                                items[items.length - 1].focus();
                            }}"""
                        )
                        nav_page.keyboard.press("Tab")
                        assert (
                            nav_page.evaluate(
                                f"""() => {{
                                    const dr = document.querySelector('{drawer_sel}');
                                    const items = Array.from(dr.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'))
                                        .filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
                                    return document.activeElement === items[0];
                                }}"""
                            )
                            is True
                        )
                        nav_page.keyboard.press("Shift+Tab")
                        assert (
                            nav_page.evaluate(
                                f"""() => {{
                                    const dr = document.querySelector('{drawer_sel}');
                                    const items = Array.from(dr.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'))
                                        .filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
                                    return document.activeElement === items[items.length - 1];
                                }}"""
                            )
                            is True
                        )
                        nav_page.keyboard.press("Escape")
                        assert (
                            nav_page.evaluate(f"document.getElementById('{input_id}').checked")
                            is False
                        )
                        assert (
                            nav_page.evaluate(f"document.querySelector('{drawer_sel}').inert")
                            is True
                        )
                        assert (
                            nav_page.evaluate(
                                f"document.activeElement === document.querySelector('{trig_sel}')"
                            )
                            is True
                        )

                # Wide desktop (> 82em = 1400x900) leaves both drawers visible and non-inert
                nav_page.set_viewport_size({"width": 1400, "height": 900})
                nav_page.wait_for_timeout(50)
                wide_drawers = nav_page.evaluate(
                    """() => {
                        const sd = document.querySelector('.sidebar-drawer');
                        const td = document.querySelector('.toc-drawer');
                        return {
                            sdVis: getComputedStyle(sd).visibility,
                            sdInert: sd.inert,
                            tdVis: getComputedStyle(td).visibility,
                            tdInert: td.inert
                        };
                    }"""
                )
                assert wide_drawers == {
                    "sdVis": "visible",
                    "sdInert": False,
                    "tdVis": "visible",
                    "tdInert": False,
                }

                # 5c. py-modindex.html: .no-toc triggers skipped & 5 img.toggler controls
                nav_page.set_viewport_size({"width": 1280, "height": 900})
                nav_page.goto(
                    f"http://127.0.0.1:{port}/py-modindex.html",
                    wait_until="load",
                )
                nav_page.add_script_tag(content=contrast_helper_js)

                no_toc_audit = nav_page.evaluate(
                    """() => {
                        const hdr = document.querySelector('.toc-header-icon');
                        const cnt = document.querySelector('.toc-content-icon');
                        return {
                            hdrNoToc: hdr.classList.contains('no-toc'),
                            cntNoToc: cnt.classList.contains('no-toc'),
                            hdrDisplay: getComputedStyle(hdr).display,
                            cntDisplay: getComputedStyle(cnt).display,
                            hdrHasTabindex: hdr.hasAttribute('tabindex'),
                            cntHasTabindex: cnt.hasAttribute('tabindex')
                        };
                    }"""
                )
                assert no_toc_audit == {
                    "hdrNoToc": True,
                    "cntNoToc": True,
                    "hdrDisplay": "none",
                    "cntDisplay": "none",
                    "hdrHasTabindex": False,
                    "cntHasTabindex": False,
                }

                togglers_meta = nav_page.evaluate(
                    """() => Array.from(document.querySelectorAll('img.toggler')).map((img) => {
                        const num = img.id.replace(/^toggle-/, '');
                        return {
                            id: img.id,
                            tabIndex: img.tabIndex,
                            role: img.getAttribute('role'),
                            expanded: img.getAttribute('aria-expanded'),
                            label: img.getAttribute('aria-label') || '',
                            alt: img.getAttribute('alt'),
                            rowCount: document.querySelectorAll('tr.cg-' + num).length
                        };
                    })"""
                )
                assert len(togglers_meta) == 5
                total_toggled_rows = sum(item["rowCount"] for item in togglers_meta)
                assert total_toggled_rows >= 191, (
                    f"Expected >= 191 module rows across 5 img.toggler groups, got {total_toggled_rows}"
                )
                expected_pkgs = {
                    "apache_airflow_providers_imednet",
                    "imednet",
                    "imednet_sinks",
                    "imednet_tui",
                    "imednet_workflows",
                }
                assert {item["label"] for item in togglers_meta} == {
                    f"Toggle {pkg} submodules" for pkg in expected_pkgs
                }

                for item in togglers_meta:
                    tid = item["id"]
                    num = tid.replace("toggle-", "")
                    assert item["tabIndex"] == 0
                    assert item["role"] == "button"
                    assert item["expanded"] == "true"
                    assert item["alt"] == "-"

                    loc = nav_page.locator(f"#{tid}")
                    loc.focus()
                    nav_page.keyboard.press("Enter")
                    collapsed_check = nav_page.evaluate(
                        f"""() => {{
                            const img = document.getElementById('{tid}');
                            const rows = Array.from(document.querySelectorAll('tr.cg-{num}'));
                            return {{
                                expanded: img.getAttribute('aria-expanded'),
                                alt: img.getAttribute('alt'),
                                allHidden: rows.length > 0 && rows.every((r) => getComputedStyle(r).display === 'none')
                            }};
                        }}"""
                    )
                    assert collapsed_check == {
                        "expanded": "false",
                        "alt": "+",
                        "allHidden": True,
                    }

                    nav_page.keyboard.press("Space")
                    expanded_check = nav_page.evaluate(
                        f"""() => {{
                            const img = document.getElementById('{tid}');
                            const rows = Array.from(document.querySelectorAll('tr.cg-{num}'));
                            return {{
                                expanded: img.getAttribute('aria-expanded'),
                                alt: img.getAttribute('alt'),
                                allVisible: rows.length > 0 && rows.every((r) => getComputedStyle(r).display !== 'none')
                            }};
                        }}"""
                    )
                    assert expanded_check == {
                        "expanded": "true",
                        "alt": "-",
                        "allVisible": True,
                    }

                # 5d. 4-mode :focus-visible outlines (light, dark, prefers-contrast: more, forced-colors)
                outline_helper_js = """
                window.__checkToggleOutline = (sel) => {
                    const h = window.__wcagHelpers;
                    const el = document.querySelector(sel);
                    const cs = getComputedStyle(el);
                    const oc = h.parseRgb(cs.outlineColor);
                    const bg = h.effectiveBg(el);
                    return {
                        matches: el.matches(':focus-visible'),
                        style: cs.outlineStyle,
                        width: cs.outlineWidth,
                        color: cs.outlineColor,
                        contrast: oc ? h.ratio(oc, bg) : 0
                    };
                };
                """
                nav_page.add_script_tag(content=outline_helper_js)
                for theme in ("light", "dark"):
                    nav_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                    nav_page.locator("#toggle-1").focus()
                    nav_page.keyboard.press("Shift")
                    mod_out = nav_page.evaluate("window.__checkToggleOutline('#toggle-1')")
                    assert mod_out["matches"] is True
                    assert mod_out["style"] == "solid" and mod_out["width"] == "2px"
                    assert mod_out["contrast"] >= 4.5

                nav_page.emulate_media(contrast="more")
                for theme in ("light", "dark"):
                    nav_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                    nav_page.locator("#toggle-1").focus()
                    nav_page.keyboard.press("Shift")
                    mod_hc = nav_page.evaluate("window.__checkToggleOutline('#toggle-1')")
                    assert mod_hc["matches"] is True
                    assert mod_hc["style"] == "solid" and mod_hc["width"] == "2px"
                    assert mod_hc["contrast"] >= 7.0
                nav_page.emulate_media(contrast="no-preference")

                nav_page.emulate_media(forced_colors="active")
                sys_hl_mod = nav_page.evaluate(
                    """() => {
                        const p = document.createElement('span');
                        p.style.cssText = 'forced-color-adjust: none !important; outline: 2px solid Highlight !important; display: none;';
                        document.body.appendChild(p);
                        const c = getComputedStyle(p).outlineColor;
                        p.remove();
                        return c;
                    }"""
                )
                nav_page.locator("#toggle-1").focus()
                nav_page.keyboard.press("Shift")
                mod_fc = nav_page.evaluate("window.__checkToggleOutline('#toggle-1')")
                assert mod_fc["matches"] is True
                assert mod_fc["style"] == "solid" and mod_fc["width"] == "2px"
                assert mod_fc["color"] == sys_hl_mod
                nav_page.emulate_media(forced_colors="none")

                nav_page.goto(
                    f"http://127.0.0.1:{port}/index.html",
                    wait_until="load",
                )
                nav_page.add_script_tag(content=contrast_helper_js)
                nav_page.add_script_tag(content=outline_helper_js)

                for theme in ("light", "dark"):
                    nav_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                    nav_page.set_viewport_size({"width": 1280, "height": 900})
                    for sel in ("#toctree-checkbox-1", ".toc-content-icon"):
                        nav_page.locator(sel).focus()
                        nav_page.keyboard.press("Shift")
                        res = nav_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                        assert res["matches"] is True
                        assert res["style"] == "solid" and res["width"] == "2px"
                        assert res["contrast"] >= 4.5
                    nav_page.set_viewport_size({"width": 375, "height": 812})
                    for sel in (".nav-overlay-icon", ".toc-header-icon"):
                        nav_page.locator(sel).focus()
                        nav_page.keyboard.press("Shift")
                        res = nav_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                        assert res["matches"] is True
                        assert res["style"] == "solid" and res["width"] == "2px"
                        assert res["contrast"] >= 4.5

                nav_page.emulate_media(contrast="more")
                for theme in ("light", "dark"):
                    nav_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                    nav_page.set_viewport_size({"width": 1280, "height": 900})
                    for sel in ("#toctree-checkbox-1", ".toc-content-icon"):
                        nav_page.locator(sel).focus()
                        nav_page.keyboard.press("Shift")
                        res = nav_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                        assert res["matches"] is True
                        assert res["style"] == "solid" and res["width"] == "2px"
                        assert res["contrast"] >= 7.0
                    nav_page.set_viewport_size({"width": 375, "height": 812})
                    for sel in (".nav-overlay-icon", ".toc-header-icon"):
                        nav_page.locator(sel).focus()
                        nav_page.keyboard.press("Shift")
                        res = nav_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                        assert res["matches"] is True
                        assert res["style"] == "solid" and res["width"] == "2px"
                        assert res["contrast"] >= 7.0
                nav_page.emulate_media(contrast="no-preference")

                nav_page.emulate_media(forced_colors="active")
                sys_hl_idx = nav_page.evaluate(
                    """() => {
                        const p = document.createElement('span');
                        p.style.cssText = 'forced-color-adjust: none !important; outline: 2px solid Highlight !important; display: none;';
                        document.body.appendChild(p);
                        const c = getComputedStyle(p).outlineColor;
                        p.remove();
                        return c;
                    }"""
                )
                nav_page.set_viewport_size({"width": 1280, "height": 900})
                for sel in ("#toctree-checkbox-1", ".toc-content-icon"):
                    nav_page.locator(sel).focus()
                    nav_page.keyboard.press("Shift")
                    res = nav_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                    assert res["matches"] is True
                    assert res["style"] == "solid" and res["width"] == "2px"
                    assert res["color"] == sys_hl_idx
                nav_page.set_viewport_size({"width": 375, "height": 812})
                for sel in (".nav-overlay-icon", ".toc-header-icon"):
                    nav_page.locator(sel).focus()
                    nav_page.keyboard.press("Shift")
                    res = nav_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                    assert res["matches"] is True
                    assert res["style"] == "solid" and res["width"] == "2px"
                    assert res["color"] == sys_hl_idx
                nav_page.emulate_media(forced_colors="none")

                # 5e. Overflowing .table-wrapper focusable region & :focus-visible outline on mobile
                nav_page.set_viewport_size({"width": 375, "height": 812})
                nav_page.goto(
                    f"http://127.0.0.1:{port}/reference/configuration.html",
                    wait_until="load",
                )
                nav_page.add_script_tag(content=contrast_helper_js)
                nav_page.add_script_tag(content=outline_helper_js)
                table_region_audit = nav_page.evaluate(
                    """() => {
                        const wrappers = Array.from(document.querySelectorAll('.table-wrapper'));
                        const overflowing = wrappers.filter((w) => w.clientWidth > 0 && w.scrollWidth > w.clientWidth);
                        if (!overflowing.length) return { count: 0 };
                        const first = overflowing[0];
                        first.id = 'test-overflow-table-wrapper';
                        return {
                            count: overflowing.length,
                            tabindex: first.getAttribute('tabindex'),
                            role: first.getAttribute('role'),
                            label: first.getAttribute('aria-label') || ''
                        };
                    }"""
                )
                assert table_region_audit["count"] >= 1, (
                    "Expected >= 1 overflowing .table-wrapper on reference/configuration.html at 375px"
                )
                assert table_region_audit["tabindex"] == "0"
                assert table_region_audit["role"] == "region"
                assert table_region_audit["label"]
                nav_page.locator("#test-overflow-table-wrapper").focus()
                nav_page.keyboard.press("Shift")
                tbl_focus_std = nav_page.evaluate(
                    "window.__checkToggleOutline('#test-overflow-table-wrapper')"
                )
                assert tbl_focus_std["matches"] is True
                assert tbl_focus_std["style"] == "solid" and tbl_focus_std["width"] == "2px"
                assert tbl_focus_std["contrast"] >= 4.5

                nav_page.emulate_media(forced_colors="active")
                nav_page.locator("#test-overflow-table-wrapper").focus()
                nav_page.keyboard.press("Shift")
                tbl_focus_fc = nav_page.evaluate(
                    "window.__checkToggleOutline('#test-overflow-table-wrapper')"
                )
                assert tbl_focus_fc["matches"] is True
                assert tbl_focus_fc["style"] == "solid" and tbl_focus_fc["width"] == "2px"
                nav_page.emulate_media(forced_colors="none")

                nav_page.close()

                # --------------------------------------------------------------
                # 6. Scroll-padding-top (C43), Shift+Tab SC 2.4.11 visibility &
                #    a.back-to-top SC 2.4.7 focus transfer across 5 viewports
                # --------------------------------------------------------------
                nav_page = context.new_page()
                cdp_session = context.new_cdp_session(nav_page)
                cdp_session.send("Page.enable")
                try:
                    # 5a. Reduced-motion scroll-behavior & smooth back-to-top check at 1280x900
                    nav_page.set_viewport_size({"width": 1280, "height": 900})
                    nav_page.emulate_media(reduced_motion="no-preference")
                    nav_page.goto(
                        f"http://127.0.0.1:{port}/index.html",
                        wait_until="load",
                    )
                    default_scroll_styles = nav_page.evaluate(
                        """() => ({
                            html: getComputedStyle(document.documentElement).scrollBehavior,
                            sidebar: getComputedStyle(document.querySelector('.sidebar-scroll')).scrollBehavior,
                            toc: getComputedStyle(document.querySelector('.toc-scroll')).scrollBehavior
                        })"""
                    )
                    assert default_scroll_styles == {
                        "html": "smooth",
                        "sidebar": "smooth",
                        "toc": "smooth",
                    }

                    # Smooth scroll + focus transfer on index.html with #panel-auditor active
                    nav_page.locator("#tab-auditor").click()
                    nav_page.evaluate("globalThis.scrollTo({ top: 900, behavior: 'auto' })")
                    nav_page.wait_for_timeout(40)
                    nav_page.evaluate("globalThis.scrollTo({ top: 550, behavior: 'auto' })")
                    nav_page.wait_for_function(
                        "() => getComputedStyle(document.querySelector('a.back-to-top')).display === 'flex'"
                    )
                    nav_page.locator("a.back-to-top").focus()
                    nav_page.keyboard.press("Enter")
                    nav_page.wait_for_function("() => window.scrollY === 0", timeout=4000)
                    smooth_btt = nav_page.evaluate(
                        """() => {
                            const mc = document.getElementById('furo-main-content');
                            const cs = getComputedStyle(mc);
                            return {
                                hash: window.location.hash,
                                activeId: document.activeElement && document.activeElement.id,
                                focusVisible: mc.matches(':focus-visible'),
                                outlineStyle: cs.outlineStyle,
                                outlineWidth: cs.outlineWidth,
                                outlineOffset: cs.outlineOffset,
                                auditorSelected: document.getElementById('tab-auditor').getAttribute('aria-selected'),
                                auditorHidden: document.getElementById('panel-auditor').hasAttribute('hidden')
                            };
                        }"""
                    )
                    assert smooth_btt == {
                        "hash": "#panel-auditor",
                        "activeId": "furo-main-content",
                        "focusVisible": True,
                        "outlineStyle": "solid",
                        "outlineWidth": "2px",
                        "outlineOffset": "2px",
                        "auditorSelected": "true",
                        "auditorHidden": False,
                    }

                    # Forced-colors focus ring on #furo-main-content:focus-visible (2px solid Highlight)
                    nav_page.emulate_media(forced_colors="active")
                    fc_main_ring = nav_page.evaluate(
                        """() => {
                            const probe = document.createElement('span');
                            probe.style.cssText =
                                'forced-color-adjust: none !important; outline: 2px solid Highlight !important; display: none;';
                            document.body.appendChild(probe);
                            const sysHighlight = getComputedStyle(probe).outlineColor;
                            probe.remove();

                            const mc = document.getElementById('furo-main-content');
                            const cs = getComputedStyle(mc);
                            return {
                                matches: mc.matches(':focus-visible'),
                                style: cs.outlineStyle,
                                width: cs.outlineWidth,
                                offset: cs.outlineOffset,
                                color: cs.outlineColor,
                                sysHighlight
                            };
                        }"""
                    )
                    assert fc_main_ring["matches"] is True
                    assert fc_main_ring["style"] == "solid"
                    assert fc_main_ring["width"] == "2px"
                    assert fc_main_ring["offset"] == "2px"
                    assert fc_main_ring["color"] == fc_main_ring["sysHighlight"]
                    nav_page.emulate_media(forced_colors="none")

                    # Reduced-motion scroll-behavior: auto & .skip-to-content preservation of #panel-auditor
                    nav_page.emulate_media(reduced_motion="reduce")
                    reduced_scroll_styles = nav_page.evaluate(
                        """() => ({
                            html: getComputedStyle(document.documentElement).scrollBehavior,
                            sidebar: getComputedStyle(document.querySelector('.sidebar-scroll')).scrollBehavior,
                            toc: getComputedStyle(document.querySelector('.toc-scroll')).scrollBehavior
                        })"""
                    )
                    assert reduced_scroll_styles == {
                        "html": "auto",
                        "sidebar": "auto",
                        "toc": "auto",
                    }

                    nav_page.locator("#tab-auditor").click()
                    nav_page.evaluate("globalThis.scrollTo({ top: 700, behavior: 'auto' })")
                    nav_page.locator("a.skip-to-content").focus()
                    nav_page.keyboard.press("Enter")
                    skip_state = nav_page.evaluate(
                        """() => {
                            const mc = document.getElementById('furo-main-content');
                            const cs = getComputedStyle(mc);
                            return {
                                scrollY: window.scrollY,
                                activeId: document.activeElement && document.activeElement.id,
                                focusVisible: mc.matches(':focus-visible'),
                                outlineStyle: cs.outlineStyle,
                                outlineWidth: cs.outlineWidth,
                                auditorSelected: document.getElementById('tab-auditor').getAttribute('aria-selected'),
                                auditorHidden: document.getElementById('panel-auditor').hasAttribute('hidden')
                            };
                        }"""
                    )
                    assert skip_state["scrollY"] == 0
                    assert skip_state["activeId"] == "furo-main-content"
                    assert skip_state["focusVisible"] is True
                    assert skip_state["outlineStyle"] == "solid"
                    assert skip_state["outlineWidth"] == "2px"
                    assert skip_state["auditorSelected"] == "true"
                    assert skip_state["auditorHidden"] is False

                    # 5b. Exhaustive 5-viewport audit: scroll-padding-top, Shift+Tab SC 2.4.11,
                    #     and a.back-to-top SC 2.4.7 + SC 2.4.3 Tab order
                    viewport_matrix = [
                        ("320x640@16px", 320, 640, 16, "96px"),
                        ("640x450@16px", 640, 450, 16, "96px"),
                        ("768x600@16px", 768, 600, 16, "96px"),
                        ("1280x900@16px", 1280, 900, 16, "56px"),
                        ("1280x900@32px", 1280, 900, 32, "192px"),
                    ]
                    audit_pages = [
                        "index.html",
                        "how-to/cli.html",
                        "tutorials/quick_start.html",
                        "reference/VPAT.html",
                    ]
                    traversed_pre_regions = 0

                    for vp_label, vp_w, vp_h, std_fs, expected_padding in viewport_matrix:
                        nav_page.set_viewport_size({"width": vp_w, "height": vp_h})
                        cdp_session.send(
                            "Page.setFontSizes",
                            {"fontSizes": {"standard": std_fs, "fixed": std_fs}},
                        )

                        for rel_page in audit_pages:
                            nav_page.goto(
                                f"http://127.0.0.1:{port}/{rel_page}",
                                wait_until="load",
                            )
                            page_meta = nav_page.evaluate(
                                """() => {
                                    const mc = document.getElementById('furo-main-content');
                                    return {
                                        scrollPaddingTop: getComputedStyle(document.documentElement).scrollPaddingTop,
                                        mainTabindex: mc ? mc.getAttribute('tabindex') : null
                                    };
                                }"""
                            )
                            assert page_meta["scrollPaddingTop"] == expected_padding, (
                                f"{vp_label} {rel_page} scroll-padding-top={page_meta['scrollPaddingTop']!r} "
                                f"!= {expected_padding!r}"
                            )
                            assert page_meta["mainTabindex"] == "-1", (
                                f"{vp_label} {rel_page} #furo-main-content missing tabindex='-1'"
                            )

                            # Reverse Shift+Tab traversal on representative long pages
                            if rel_page in ("index.html", "how-to/cli.html"):
                                nav_page.evaluate(
                                    "globalThis.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'auto' })"
                                )
                                nav_page.locator("footer a").last.focus()
                                checked_stops = 0
                                obscured_stops: list[dict[str, object]] = []

                                for _ in range(55):
                                    nav_page.keyboard.press("Shift+Tab")
                                    stop_info = nav_page.evaluate(
                                        """() => {
                                            const el = document.activeElement;
                                            if (!el || el === document.body) return { done: true };
                                            if (el.classList.contains('skip-to-content')) return { done: true };
                                            if (!el.closest('.content')) return { skip: true };
                                            const r = el.getBoundingClientRect();
                                            if (r.width <= 0 || r.height <= 0) return { skip: true };

                                            const mh = document.querySelector('header.mobile-header');
                                            const mhR = mh && getComputedStyle(mh).display !== 'none'
                                                ? mh.getBoundingClientRect()
                                                : null;
                                            const btt = document.querySelector('a.back-to-top');
                                            const bttR = btt && getComputedStyle(btt).display !== 'none'
                                                ? btt.getBoundingClientRect()
                                                : null;

                                            const completelyUnderMh = !!(
                                                mhR && r.bottom <= mhR.bottom + 0.5 && r.top < mhR.bottom
                                            );
                                            const completelyUnderBtt = !!(
                                                bttR &&
                                                r.left >= bttR.left - 0.5 &&
                                                r.right <= bttR.right + 0.5 &&
                                                r.top >= bttR.top - 0.5 &&
                                                r.bottom <= bttR.bottom + 0.5
                                            );

                                            // Multi-point hit-test across visible box intersection
                                            const vx0 = Math.max(1, r.left + 1);
                                            const vx1 = Math.min(window.innerWidth - 2, r.right - 1);
                                            const vy0 = Math.max(1, r.top + 1);
                                            const vy1 = Math.min(window.innerHeight - 2, r.bottom - 1);
                                            let anyUncoveredPoint = false;
                                            if (vx1 >= vx0 && vy1 >= vy0) {
                                                for (const fx of [0.2, 0.5, 0.8]) {
                                                    for (const fy of [0.2, 0.5, 0.8]) {
                                                        const px = vx0 + (vx1 - vx0) * fx;
                                                        const py = vy0 + (vy1 - vy0) * fy;
                                                        const hit = document.elementFromPoint(px, py);
                                                        if (
                                                            hit &&
                                                            !hit.closest('header.mobile-header') &&
                                                            !hit.closest('a.back-to-top')
                                                        ) {
                                                            anyUncoveredPoint = true;
                                                        }
                                                    }
                                                }
                                            }

                                            return {
                                                tag: el.tagName,
                                                id: el.id,
                                                cls: el.className,
                                                top: Math.round(r.top),
                                                bottom: Math.round(r.bottom),
                                                mhBottom: mhR ? Math.round(mhR.bottom) : 0,
                                                completelyObscured:
                                                    completelyUnderMh || completelyUnderBtt || !anyUncoveredPoint
                                            };
                                        }"""
                                    )
                                    if stop_info.get("done"):
                                        break
                                    if stop_info.get("skip"):
                                        continue
                                    checked_stops += 1
                                    if stop_info.get("tag") == "PRE":
                                        traversed_pre_regions += 1
                                    if stop_info.get("completelyObscured"):
                                        obscured_stops.append(stop_info)

                                assert checked_stops >= 8, (
                                    f"Expected >= 8 Shift+Tab stops inside .content on {vp_label} {rel_page}, "
                                    f"got {checked_stops}"
                                )
                                assert obscured_stops == [], (
                                    f"SC 2.4.11 obscured Shift+Tab stops on {vp_label} {rel_page}: {obscured_stops}"
                                )

                            # Back-to-top Enter activation & subsequent Tab focus order (SC 2.4.7 & SC 2.4.3)
                            if rel_page == "index.html":
                                nav_page.locator("#tab-auditor").click()
                            nav_page.evaluate(
                                "globalThis.scrollTo({ top: Math.min(900, document.documentElement.scrollHeight), behavior: 'auto' })"
                            )
                            nav_page.wait_for_timeout(30)
                            nav_page.evaluate("globalThis.scrollTo({ top: 300, behavior: 'auto' })")
                            nav_page.wait_for_function(
                                "() => getComputedStyle(document.querySelector('a.back-to-top')).display === 'flex'"
                            )
                            hash_before = nav_page.evaluate("window.location.hash")
                            nav_page.locator("a.back-to-top").focus()
                            nav_page.keyboard.press("Enter")

                            btt_check = nav_page.evaluate(
                                """() => {
                                    const mc = document.getElementById('furo-main-content');
                                    const cs = getComputedStyle(mc);
                                    return {
                                        scrollY: window.scrollY,
                                        hash: window.location.hash,
                                        activeId: document.activeElement && document.activeElement.id,
                                        focusVisible: mc.matches(':focus-visible'),
                                        outlineStyle: cs.outlineStyle,
                                        outlineWidth: cs.outlineWidth,
                                        outlineOffset: cs.outlineOffset
                                    };
                                }"""
                            )
                            assert btt_check == {
                                "scrollY": 0,
                                "hash": hash_before,
                                "activeId": "furo-main-content",
                                "focusVisible": True,
                                "outlineStyle": "solid",
                                "outlineWidth": "2px",
                                "outlineOffset": "2px",
                            }, f"{vp_label} {rel_page} back-to-top failed: {btt_check}"

                            if rel_page == "index.html":
                                auditor_preserved = nav_page.evaluate(
                                    """() => ({
                                        selected: document.getElementById('tab-auditor').getAttribute('aria-selected'),
                                        hidden: document.getElementById('panel-auditor').hasAttribute('hidden')
                                    })"""
                                )
                                assert auditor_preserved == {"selected": "true", "hidden": False}

                            nav_page.keyboard.press("Tab")
                            next_tab_inside_main = nav_page.evaluate(
                                """() => {
                                    const mc = document.getElementById('furo-main-content');
                                    const ae = document.activeElement;
                                    return Boolean(mc && ae && ae !== mc && mc.contains(ae));
                                }"""
                            )
                            assert next_tab_inside_main is True, (
                                f"{vp_label} {rel_page} Tab after back-to-top did not focus first control inside #furo-main-content"
                            )

                    assert traversed_pre_regions >= 1, (
                        "Expected Shift+Tab audit to traverse overflowing pre[tabindex='0'] code regions"
                    )
                finally:
                    cdp_session.send(
                        "Page.setFontSizes",
                        {"fontSizes": {"standard": 16, "fixed": 16}},
                    )
                    cdp_session.detach()
                    nav_page.close()

                # --------------------------------------------------------------
                # 7. Sphinx link aria-labels, Furo .headerlink focus visibility,
                #    [source] anchor geometry, & 4-mode [docs]/[source]/¶ outlines
                # --------------------------------------------------------------
                from scripts.validate_docs import _parse_html_file

                all_built_html = sorted(html_dir.rglob("*.html"))
                static_headerlink_count = 0
                static_source_count = 0
                static_backlink_count = 0
                generic_hl_labels = {
                    "Permalink to this heading",
                    "Permalink to this definition",
                    "Permalink to this table",
                    "Permalink to this code",
                    "Permalink to this image",
                    "Permalink to this toctree",
                    "Permalink to",
                }
                for built_page in all_built_html:
                    parsed_built = _parse_html_file(built_page)
                    rel_built = built_page.relative_to(html_dir)
                    for hl_href, hl_aria in parsed_built.headerlinks:
                        static_headerlink_count += 1
                        assert hl_aria and hl_aria not in generic_hl_labels, (
                            f"{rel_built} {hl_href} has missing/generic aria-label: {hl_aria!r}"
                        )
                        assert (
                            hl_aria.startswith("Permalink to ")
                            and hl_aria.removeprefix("Permalink to ").strip()
                        ), f"{rel_built} {hl_href} invalid permalink aria-label: {hl_aria!r}"
                    for src_href, src_aria, has_src_cls in parsed_built.source_links:
                        static_source_count += 1
                        assert has_src_cls is True, (
                            f"{rel_built} {src_href} missing .viewcode-source-link class"
                        )
                        assert src_aria and src_aria.startswith("[source] for "), (
                            f"{rel_built} {src_href} invalid [source] aria-label: {src_aria!r}"
                        )
                    if rel_built.parts[0] == "_modules" and rel_built.name != "index.html":
                        for back_href, back_aria in parsed_built.backlink_labels:
                            static_backlink_count += 1
                            assert back_aria and back_aria.startswith("[docs] for "), (
                                f"{rel_built} {back_href} invalid [docs] aria-label: {back_aria!r}"
                            )

                assert static_headerlink_count >= 4000
                assert static_source_count >= 1400
                assert static_backlink_count >= 1300

                # Duplicate section heading disambiguation on how-to/cli.html
                cli_parsed = _parse_html_file(html_dir / "how-to" / "cli.html")
                cli_pos_labels = [
                    lbl
                    for _href, lbl in cli_parsed.headerlinks
                    if lbl and "Positional Arguments" in lbl
                ]
                assert len(cli_pos_labels) >= 2
                assert len(set(cli_pos_labels)) == len(cli_pos_labels), (
                    f"Duplicate Positional Arguments headerlink labels on how-to/cli.html: {cli_pos_labels}"
                )

                link_page = context.new_page()
                try:
                    link_page.set_viewport_size({"width": 1280, "height": 800})

                    # 7a. Zero a.headerlink elements compute visibility: hidden
                    sample_headerlink_pages = (
                        "index.html",
                        "how-to/cli.html",
                        "explanation/schema_validation.html",
                        "explanation/parsing.html",
                        "reference/api/imednet.auth.html",
                    )
                    for sample_rel in sample_headerlink_pages:
                        link_page.goto(
                            f"http://127.0.0.1:{port}/{sample_rel}",
                            wait_until="load",
                        )
                        hl_vis_audit = link_page.evaluate(
                            """() => {
                                const links = Array.from(document.querySelectorAll('a.headerlink'));
                                const hidden = links.filter((a) => getComputedStyle(a).visibility === 'hidden');
                                const unlabeled = links.filter((a) => !(a.getAttribute('aria-label') || '').trim());
                                const resting = links[0] ? getComputedStyle(links[0]) : null;
                                return {
                                    total: links.length,
                                    hiddenCount: hidden.length,
                                    unlabeledCount: unlabeled.length,
                                    firstVisibility: resting ? resting.visibility : null,
                                    firstOpacity: resting ? resting.opacity : null
                                };
                            }"""
                        )
                        assert hl_vis_audit["total"] >= 1, f"Expected headerlinks on {sample_rel}"
                        assert hl_vis_audit["hiddenCount"] == 0, (
                            f"{sample_rel} still has visibility:hidden headerlinks: {hl_vis_audit}"
                        )
                        assert hl_vis_audit["unlabeledCount"] == 0
                        assert hl_vis_audit["firstVisibility"] == "visible"
                        assert hl_vis_audit["firstOpacity"] == "0"

                    # 7b. Sequential Tab focus into heading, API definition, and table caption headerlinks
                    #     + [source] outer anchor non-zero bounding box & 4-mode :focus-visible outlines
                    link_page.goto(
                        f"http://127.0.0.1:{port}/reference/api/imednet.auth.html",
                        wait_until="load",
                    )
                    link_page.add_script_tag(content=contrast_helper_js)
                    link_page.add_script_tag(content=outline_helper_js)

                    # Tab from last control before h1 > a.headerlink into h1 > a.headerlink
                    link_page.evaluate(
                        """() => {
                            const h1Link = document.querySelector('h1 > a.headerlink');
                            h1Link.id = 'test-h1-headerlink';
                            const focusables = Array.from(
                                document.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), [tabindex="0"]')
                            ).filter((el) => {
                                const cs = getComputedStyle(el);
                                const r = el.getBoundingClientRect();
                                return cs.visibility !== 'hidden' && cs.display !== 'none' && (r.width > 0 || r.height > 0);
                            });
                            const idx = focusables.indexOf(h1Link);
                            if (idx > 0) focusables[idx - 1].focus();
                        }"""
                    )
                    link_page.keyboard.press("Tab")
                    h1_tab_check = link_page.evaluate(
                        """() => {
                            const el = document.getElementById('test-h1-headerlink');
                            const cs = getComputedStyle(el);
                            return {
                                isFocused: document.activeElement === el,
                                opacity: cs.opacity,
                                label: el.getAttribute('aria-label') || '',
                                outline: window.__checkToggleOutline('#test-h1-headerlink')
                            };
                        }"""
                    )
                    assert h1_tab_check["isFocused"] is True
                    assert h1_tab_check["opacity"] == "1"
                    assert h1_tab_check["label"].startswith("Permalink to ")
                    assert h1_tab_check["outline"]["matches"] is True
                    assert (
                        h1_tab_check["outline"]["style"] == "solid"
                        and h1_tab_check["outline"]["width"] == "2px"
                    )
                    assert h1_tab_check["outline"]["contrast"] >= 4.5

                    # API definition dt#imednet.auth.api_key.ApiKeyAuth: [source] geometry + focus -> Tab -> ¶
                    api_dt_geom = link_page.evaluate(
                        """() => {
                            const dt = document.getElementById('imednet.auth.api_key.ApiKeyAuth');
                            const srcA = dt.querySelector('a.viewcode-source-link');
                            const srcSpan = srcA.querySelector('.viewcode-link');
                            const hl = dt.querySelector('a.headerlink');
                            srcA.id = 'test-apikey-source-link';
                            hl.id = 'test-apikey-headerlink';
                            const aRect = srcA.getBoundingClientRect();
                            const spanRect = srcSpan.getBoundingClientRect();
                            const aCs = getComputedStyle(srcA);
                            const spanCs = getComputedStyle(srcSpan);
                            return {
                                aWidth: aRect.width,
                                aHeight: aRect.height,
                                spanWidth: spanRect.width,
                                widthDiff: Math.abs(aRect.width - spanRect.width),
                                textIndent: aCs.textIndent,
                                spanFloat: spanCs.float,
                                srcLabel: srcA.getAttribute('aria-label') || '',
                                hlLabel: hl.getAttribute('aria-label') || ''
                            };
                        }"""
                    )
                    assert api_dt_geom["aWidth"] >= 40, f"Collapsed [source] anchor: {api_dt_geom}"
                    assert api_dt_geom["aHeight"] >= 12
                    assert api_dt_geom["widthDiff"] <= 2.0, (
                        f"Outer [source] anchor width diverges from inner span: {api_dt_geom}"
                    )
                    assert api_dt_geom["textIndent"] == "0px"
                    assert api_dt_geom["spanFloat"] == "none"
                    assert api_dt_geom["srcLabel"] == "[source] for imednet.auth.api_key.ApiKeyAuth"
                    assert api_dt_geom["hlLabel"] == "Permalink to imednet.auth.api_key.ApiKeyAuth"

                    # Focus preceding tabbable before [source], Tab into [source], then Tab into ¶
                    link_page.evaluate(
                        """() => {
                            const srcA = document.getElementById('test-apikey-source-link');
                            srcA.scrollIntoView({ block: 'center' });
                            const focusables = Array.from(
                                document.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), [tabindex="0"]')
                            ).filter((el) => {
                                const cs = getComputedStyle(el);
                                const r = el.getBoundingClientRect();
                                return cs.visibility !== 'hidden' && cs.display !== 'none' && (r.width > 0 || r.height > 0);
                            });
                            const idx = focusables.indexOf(srcA);
                            if (idx > 0) focusables[idx - 1].focus();
                        }"""
                    )
                    link_page.keyboard.press("Tab")
                    src_tab_check = link_page.evaluate(
                        """() => {
                            const srcA = document.getElementById('test-apikey-source-link');
                            const hl = document.getElementById('test-apikey-headerlink');
                            return {
                                isFocused: document.activeElement === srcA,
                                siblingHlOpacity: getComputedStyle(hl).opacity,
                                outline: window.__checkToggleOutline('#test-apikey-source-link')
                            };
                        }"""
                    )
                    assert src_tab_check["isFocused"] is True
                    assert src_tab_check["siblingHlOpacity"] == "1"
                    assert src_tab_check["outline"]["matches"] is True
                    assert (
                        src_tab_check["outline"]["style"] == "solid"
                        and src_tab_check["outline"]["width"] == "2px"
                    )
                    assert src_tab_check["outline"]["contrast"] >= 4.5

                    link_page.keyboard.press("Tab")
                    dt_hl_tab_check = link_page.evaluate(
                        """() => {
                            const hl = document.getElementById('test-apikey-headerlink');
                            return {
                                isFocused: document.activeElement === hl,
                                opacity: getComputedStyle(hl).opacity,
                                outline: window.__checkToggleOutline('#test-apikey-headerlink')
                            };
                        }"""
                    )
                    assert dt_hl_tab_check["isFocused"] is True
                    assert dt_hl_tab_check["opacity"] == "1"
                    assert dt_hl_tab_check["outline"]["matches"] is True
                    assert (
                        dt_hl_tab_check["outline"]["style"] == "solid"
                        and dt_hl_tab_check["outline"]["width"] == "2px"
                    )
                    assert dt_hl_tab_check["outline"]["contrast"] >= 4.5

                    # 4-mode focus-visible verification on ApiKeyAuth [source] and ¶
                    for sel in ("#test-apikey-source-link", "#test-apikey-headerlink"):
                        for theme in ("light", "dark"):
                            link_page.evaluate(
                                f"document.body.setAttribute('data-theme', '{theme}')"
                            )
                            link_page.locator(sel).focus()
                            link_page.keyboard.press("Shift")
                            std_out = link_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                            assert std_out["matches"] is True
                            assert std_out["style"] == "solid" and std_out["width"] == "2px"
                            assert std_out["contrast"] >= 4.5

                        link_page.emulate_media(contrast="more")
                        for theme in ("light", "dark"):
                            link_page.evaluate(
                                f"document.body.setAttribute('data-theme', '{theme}')"
                            )
                            link_page.locator(sel).focus()
                            link_page.keyboard.press("Shift")
                            hc_out = link_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                            assert hc_out["matches"] is True
                            assert hc_out["style"] == "solid" and hc_out["width"] == "2px"
                            assert hc_out["contrast"] >= 7.0
                        link_page.emulate_media(contrast="no-preference")

                        link_page.emulate_media(forced_colors="active")
                        fc_hl_color = link_page.evaluate(
                            """() => {
                                const p = document.createElement('span');
                                p.style.cssText = 'forced-color-adjust: none !important; outline: 2px solid Highlight !important; display: none;';
                                document.body.appendChild(p);
                                const c = getComputedStyle(p).outlineColor;
                                p.remove();
                                return c;
                            }"""
                        )
                        link_page.locator(sel).focus()
                        link_page.keyboard.press("Shift")
                        fc_out = link_page.evaluate(f"window.__checkToggleOutline('{sel}')")
                        assert fc_out["matches"] is True
                        assert fc_out["style"] == "solid" and fc_out["width"] == "2px"
                        assert fc_out["color"] == fc_hl_color
                        link_page.emulate_media(forced_colors="none")

                    link_page.evaluate("document.body.setAttribute('data-theme', 'light')")

                    # Guide page :noindex: [source] anchor geometry on explanation/parsing.html
                    link_page.goto(
                        f"http://127.0.0.1:{port}/explanation/parsing.html",
                        wait_until="load",
                    )
                    parsing_src_geom = link_page.evaluate(
                        """() => {
                            const srcA = document.querySelector('a.viewcode-source-link');
                            const srcSpan = srcA ? srcA.querySelector('.viewcode-link') : null;
                            if (!srcA || !srcSpan) return null;
                            const aRect = srcA.getBoundingClientRect();
                            const spanRect = srcSpan.getBoundingClientRect();
                            return {
                                aWidth: aRect.width,
                                spanWidth: spanRect.width,
                                widthDiff: Math.abs(aRect.width - spanRect.width),
                                label: srcA.getAttribute('aria-label') || ''
                            };
                        }"""
                    )
                    assert parsing_src_geom is not None
                    assert parsing_src_geom["aWidth"] >= 40
                    assert parsing_src_geom["widthDiff"] <= 2.0
                    assert parsing_src_geom["label"].startswith("[source] for imednet.")

                    # Table caption a.headerlink sequential Tab focus on explanation/schema_validation.html
                    link_page.goto(
                        f"http://127.0.0.1:{port}/explanation/schema_validation.html",
                        wait_until="load",
                    )
                    link_page.add_script_tag(content=contrast_helper_js)
                    link_page.add_script_tag(content=outline_helper_js)
                    link_page.evaluate(
                        """() => {
                            const capHl = document.querySelector('table > caption > a.headerlink');
                            capHl.id = 'test-caption-headerlink';
                            capHl.scrollIntoView({ block: 'center' });
                            const focusables = Array.from(
                                document.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), [tabindex="0"]')
                            ).filter((el) => {
                                const cs = getComputedStyle(el);
                                const r = el.getBoundingClientRect();
                                return cs.visibility !== 'hidden' && cs.display !== 'none' && (r.width > 0 || r.height > 0);
                            });
                            const idx = focusables.indexOf(capHl);
                            if (idx > 0) focusables[idx - 1].focus();
                        }"""
                    )
                    link_page.keyboard.press("Tab")
                    cap_tab_check = link_page.evaluate(
                        """() => {
                            const el = document.getElementById('test-caption-headerlink');
                            return {
                                isFocused: document.activeElement === el,
                                opacity: getComputedStyle(el).opacity,
                                label: el.getAttribute('aria-label') || '',
                                outline: window.__checkToggleOutline('#test-caption-headerlink')
                            };
                        }"""
                    )
                    assert cap_tab_check["isFocused"] is True
                    assert cap_tab_check["opacity"] == "1"
                    assert cap_tab_check["label"].startswith("Permalink to ")
                    assert cap_tab_check["label"] != "Permalink to this table"
                    assert cap_tab_check["outline"]["matches"] is True
                    assert (
                        cap_tab_check["outline"]["style"] == "solid"
                        and cap_tab_check["outline"]["width"] == "2px"
                    )
                    assert cap_tab_check["outline"]["contrast"] >= 4.5

                    # 7c. _modules/imednet/auth/api_key.html a.viewcode-back [docs] aria-labels & 4-mode focus ring
                    link_page.goto(
                        f"http://127.0.0.1:{port}/_modules/imednet/auth/api_key.html",
                        wait_until="load",
                    )
                    link_page.add_script_tag(content=contrast_helper_js)
                    link_page.add_script_tag(content=outline_helper_js)
                    backlink_meta = link_page.evaluate(
                        """() => {
                            const backlinks = Array.from(document.querySelectorAll('a.viewcode-back'));
                            if (backlinks[0]) backlinks[0].id = 'test-viewcode-back';
                            return backlinks.map((el) => ({
                                label: el.getAttribute('aria-label') || '',
                                width: el.getBoundingClientRect().width
                            }));
                        }"""
                    )
                    assert len(backlink_meta) >= 1
                    assert all(
                        item["label"].startswith("[docs] for imednet.auth.api_key.")
                        and item["width"] >= 30
                        for item in backlink_meta
                    )

                    for theme in ("light", "dark"):
                        link_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                        link_page.locator("#test-viewcode-back").focus()
                        link_page.keyboard.press("Shift")
                        back_std = link_page.evaluate(
                            "window.__checkToggleOutline('#test-viewcode-back')"
                        )
                        assert back_std["matches"] is True
                        assert back_std["style"] == "solid" and back_std["width"] == "2px"
                        assert back_std["contrast"] >= 4.5

                    link_page.emulate_media(contrast="more")
                    for theme in ("light", "dark"):
                        link_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                        link_page.locator("#test-viewcode-back").focus()
                        link_page.keyboard.press("Shift")
                        back_hc = link_page.evaluate(
                            "window.__checkToggleOutline('#test-viewcode-back')"
                        )
                        assert back_hc["matches"] is True
                        assert back_hc["style"] == "solid" and back_hc["width"] == "2px"
                        assert back_hc["contrast"] >= 7.0
                    link_page.emulate_media(contrast="no-preference")

                    link_page.emulate_media(forced_colors="active")
                    fc_back_hl = link_page.evaluate(
                        """() => {
                            const p = document.createElement('span');
                            p.style.cssText = 'forced-color-adjust: none !important; outline: 2px solid Highlight !important; display: none;';
                            document.body.appendChild(p);
                            const c = getComputedStyle(p).outlineColor;
                            p.remove();
                            return c;
                        }"""
                    )
                    link_page.locator("#test-viewcode-back").focus()
                    link_page.keyboard.press("Shift")
                    back_fc = link_page.evaluate(
                        "window.__checkToggleOutline('#test-viewcode-back')"
                    )
                    assert back_fc["matches"] is True
                    assert back_fc["style"] == "solid" and back_fc["width"] == "2px"
                    assert back_fc["color"] == fc_back_hl
                    link_page.emulate_media(forced_colors="none")
                    link_page.evaluate("document.body.setAttribute('data-theme', 'light')")

                    # 7d. Zero horizontal overflow at 1280x800 and 375x667 across key pages
                    overflow_pages = (
                        "reference/api/imednet.auth.html",
                        "explanation/parsing.html",
                        "explanation/schema_validation.html",
                        "_modules/imednet/auth/api_key.html",
                    )
                    for vp_w, vp_h in ((1280, 800), (375, 667)):
                        link_page.set_viewport_size({"width": vp_w, "height": vp_h})
                        for ov_rel in overflow_pages:
                            link_page.goto(
                                f"http://127.0.0.1:{port}/{ov_rel}",
                                wait_until="load",
                            )
                            ov_dims = link_page.evaluate(
                                """() => ({
                                    scrollWidth: document.documentElement.scrollWidth,
                                    innerWidth: window.innerWidth
                                })"""
                            )
                            assert ov_dims["scrollWidth"] <= ov_dims["innerWidth"], (
                                f"Horizontal overflow on {ov_rel} at {vp_w}x{vp_h}: {ov_dims}"
                            )
                finally:
                    link_page.close()

                context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
