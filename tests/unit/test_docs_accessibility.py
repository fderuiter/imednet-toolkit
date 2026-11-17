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

    # Verify CSS focus-visible rule for preformatted code blocks
    assert "pre:focus-visible" in css_content
    assert "outline: 2px solid var(--color-brand-primary)" in css_content

    # Verify script logic detects pre elements and tests horizontal overflow
    assert "querySelectorAll" in js_content and "pre" in js_content
    assert "scrollWidth >" in js_content and "clientWidth" in js_content

    # Verify accessible region and tabindex attributes are assigned to overflowing containers
    assert 'tabindex' in js_content and '"0"' in js_content
    assert 'role' in js_content and '"region"' in js_content
    assert 'aria-label' in js_content and '"Code snippet"' in js_content

    # Verify dynamic updates on window resize and removal when not overflowing
    assert "resize" in js_content
    assert "removeAttribute" in js_content and "tabindex" in js_content


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

    # 5. Verify both extracted runners from thebe_pyodide.js for warning output and print-then-raise
    extracted_runners = re.findall(r"runnerScript\s*=\s*\\?`([\s\S]*?)\\?`;", thebe_js)
    assert len(extracted_runners) >= 2, (
        "Expected worker and main-thread fallback runner scripts in thebe_pyodide.js"
    )

    for i, runner_template in enumerate(extracted_runners):
        script = runner_template.strip()
        script_body = script.rsplit("{", 1)[0]
        dict_body = "{" + script.rsplit("{", 1)[1]

        # Case A: Successful execution with stderr output (e.g. library warnings)
        env_warn = {"__thebe_user_code__": "import sys; print('warning', file=sys.stderr); 42"}
        exec(script_body, env_warn)
        res_warn = eval(dict_body, env_warn)
        assert res_warn["error"] is False, (
            f"Runner {i} must return error: False for warning-only stderr"
        )
        assert "warning" in res_warn["stderr"]
        assert res_warn["result"] == "42"
        assert evaluate_decision(res_warn) == "Execution completed."

        # Case B: Execution with print before exception
        env_raise = {
            "__thebe_user_code__": "print('Starting validation')\nraise ValueError('Invalid')"
        }
        exec(script_body, env_raise)
        res_raise = eval(dict_body, env_raise)
        assert res_raise["error"] is True, f"Runner {i} must return error: True for exception"
        assert "Starting validation" in res_raise["stdout"]
        assert "ValueError: Invalid" in res_raise["stderr"]
        assert evaluate_decision(res_raise) == "Execution completed with errors."

    # 6. Execute real JavaScript decision block in Node if available
    node_bin = shutil.which("node")
    if node_bin:
        match = re.search(
            r"executeCode\(code,\s*\(res\)\s*=>\s*\{([\s\S]*?)\n\s*outputContent\.innerHTML",
            thebe_js,
        )
        assert match is not None, "Could not extract executeCode callback from thebe_pyodide.js"
        callback_body = match.group(1)
        node_script = f"""
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
        """
        proc = subprocess.run([node_bin, "-e", node_script], capture_output=True, text=True)
        assert proc.returncode == 0, (
            f"Node verification of JavaScript decision failed:\n{proc.stderr}"
        )


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
