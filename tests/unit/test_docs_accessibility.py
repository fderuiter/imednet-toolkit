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

    # Verify CSS focus-visible rule for preformatted code blocks
    assert "pre:focus-visible" in css_content
    assert "outline: 2px solid var(--color-brand-primary)" in css_content

    # Verify script logic detects pre elements and tests horizontal overflow in both global and showcase scripts
    for script_src in (js_content, code_blocks_js):
        assert "querySelectorAll" in script_src and "pre" in script_src
        assert "scrollWidth >" in script_src and "clientWidth" in script_src
        assert "tabindex" in script_src and '"0"' in script_src
        assert "role" in script_src and '"region"' in script_src
        assert "aria-label" in script_src and '"Code snippet"' in script_src
        assert "resize" in script_src
        assert "removeAttribute" in script_src and "tabindex" in script_src


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
        """
        proc = subprocess.run([node_bin, "-e", node_script], capture_output=True, text=True)
        assert proc.returncode == 0, (
            f"Node verification of JavaScript runtime failed:\n{proc.stderr}"
        )


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

                # Verify @media (prefers-contrast: more) reaches >= 7:1 in light and dark modes
                tut_page.emulate_media(contrast="more")
                for theme in ("light", "dark"):
                    tut_page.evaluate(f"document.body.setAttribute('data-theme', '{theme}')")
                    tut_page.locator(".thebelab-run-button").first.hover()
                    hc_ratios = tut_page.evaluate(
                        """() => {
                            const h = window.__wcagHelpers;
                            return {
                                launch: h.elementContrast(document.querySelector('.thebe-launch-button:not(.thebe-status-ready)')),
                                ready: h.elementContrast(document.getElementById('test-ready-launch-btn')),
                                statusSpan: h.elementContrast(document.querySelector('#test-ready-launch-btn span.status')),
                                runHover: h.elementContrast(document.querySelector('.thebelab-run-button')),
                                cellStatus: h.elementContrast(document.querySelector('.thebelab-cell-status'))
                            };
                        }"""
                    )
                    for ctrl_name, val in hc_ratios.items():
                        assert val >= 7.0, (
                            f"prefers-contrast: more ({theme}) {ctrl_name} contrast {val:.2f}:1 < 7.0:1"
                        )
                tut_page.emulate_media(contrast="no-preference")

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

                sc_253_checks = idx_page.evaluate(
                    """() => {
                        const cleanVisible = (str) =>
                            str.replace(/^[^A-Za-z0-9]+/, '').replace(/\\s+/g, ' ').trim();
                        const ids = [
                            'ie-dataset-select',
                            'cdm-validate-btn',
                            'cdm-copy-btn',
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
                idx_page.emulate_media(forced_colors="none")

                # 4c. @media (prefers-contrast: more) >= 7.0:1 across light and dark modes on index.html
                idx_page.emulate_media(contrast="more")
                for theme in ("light", "dark"):
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
                            return {
                                cdmMetricBadge: h.elementContrast(document.getElementById('cdm-metric-badge')),
                                crfTag: h.elementContrast(document.querySelector('#cdm-view-crf .crf-tag')),
                                crfRequired: h.elementContrast(document.querySelector('#cdm-view-crf .crf-badge-required')),
                                crfOptional: h.elementContrast(document.querySelector('#cdm-view-crf .crf-badge-optional'))
                            };
                        }"""
                    )
                    combined_hc = {**ie_records_hc, **ie_queries_hc, **cdm_hc}
                    for badge_name, ratio_val in combined_hc.items():
                        assert ratio_val >= 7.0, (
                            f"index.html prefers-contrast: more ({theme}) {badge_name} "
                            f"contrast {ratio_val:.2f}:1 < 7.0:1"
                        )

                idx_page.emulate_media(contrast="no-preference")
                idx_page.evaluate("document.body.setAttribute('data-theme', 'light')")

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
                            const r = document.getElementById('{copy_id}').getBoundingClientRect();
                            return {{
                                width: r.width,
                                height: r.height,
                                badge: document.getElementById('{badge_id}').textContent
                            }};
                        }}"""
                    )
                    assert abs(fail_state["width"] - pre_click_rect["width"]) < 0.5
                    assert abs(fail_state["height"] - pre_click_rect["height"]) < 0.5
                    assert "Copy JSON failed" in fail_state["badge"]
                    assert "Clipboard unavailable" in fail_state["badge"]

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
                context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
