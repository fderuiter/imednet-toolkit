#!/usr/bin/env python3
"""Headless Chromium Playwright verification gate for interactive Pyodide tutorials."""

from __future__ import annotations

import argparse
import functools
import json
import re
import shutil
import sys
import threading
from collections.abc import Sequence
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

EXPECTED_WHEEL_PREFIXES: tuple[str, ...] = (
    "imednet-",
    "imednet_workflows-",
    "imednet_plugins_sinks-",
)
FORBIDDEN_WHEEL_SUBSTRINGS: tuple[str, ...] = (
    "apache_airflow_providers_imednet",
    "imednet_tui",
    "imednet_workspace",
)
EXPECTED_PYODIDE_PACKAGES: tuple[str, ...] = (
    "micropip",
    "pydantic",
    "cryptography",
    "pandas",
    "ssl",
    "sqlite3",
)
EXPECTED_REQUIREMENTS: tuple[str, ...] = (
    "httpx",
    "tenacity",
    "defusedxml",
    "python-dotenv",
    "filelock",
)
TUTORIAL_PAGES: tuple[str, ...] = (
    "tutorials/quick_start.html",
    "tutorials/async_quick_start.html",
)

CONFIG_SCRIPT_PATTERN = re.compile(
    r'<script\s+id="imednet-pyodide-config"\s+type="application/json">\s*(.*?)\s*</script>',
    re.DOTALL,
)
CONTENT_ROOT_PATTERN = re.compile(r'<html[^>]*\bdata-content_root="([^"]*)"', re.IGNORECASE)


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    """Static file HTTP handler suppressing per-request stderr noise."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def verify_built_tutorial_html(html_dir: Path) -> list[str]:
    """Verify static HTML invariants for built interactive tutorial pages."""
    errors: list[str] = []
    if not html_dir.is_dir():
        return [f"Built HTML directory not found: {html_dir}"]

    for rel_page in TUTORIAL_PAGES:
        page_path = html_dir / rel_page
        if not page_path.is_file():
            errors.append(f"Expected built tutorial page not found: {page_path}")
            continue

        html_text = page_path.read_text(encoding="utf-8")

        for forbidden in (
            "sphinx-thebe.js",
            "text/x-thebe-config",
            "unpkg.com/thebe",
            "window.thebe_config",
            "mybinder.org",
        ):
            if forbidden in html_text:
                errors.append(
                    f"{rel_page}: Found forbidden legacy Thebe/Binder token '{forbidden}'"
                )

        root_match = CONTENT_ROOT_PATTERN.search(html_text)
        if root_match is None or not root_match.group(1):
            errors.append(f"{rel_page}: Missing non-empty <html data-content_root='...'> attribute")
        elif root_match.group(1) != "../":
            errors.append(
                f"{rel_page}: Expected data-content_root='../' on nested tutorial page, "
                f"got '{root_match.group(1)}'"
            )

        if "thebe-launch-button" not in html_text:
            errors.append(f"{rel_page}: Missing interactive launch button (.thebe-launch-button)")

        cfg_match = CONFIG_SCRIPT_PATTERN.search(html_text)
        if cfg_match is None:
            errors.append(
                f"{rel_page}: Missing <script id='imednet-pyodide-config' type='application/json'>"
            )
            continue

        try:
            config = json.loads(cfg_match.group(1))
        except json.JSONDecodeError as exc:
            errors.append(f"{rel_page}: Invalid JSON in #imednet-pyodide-config: {exc}")
            continue

        wheels = config.get("wheels", [])
        if not isinstance(wheels, list):
            errors.append(f"{rel_page}: 'wheels' in #imednet-pyodide-config must be a list")
            continue

        for prefix in EXPECTED_WHEEL_PREFIXES:
            matched = [w for w in wheels if Path(str(w)).name.startswith(prefix)]
            if not matched:
                errors.append(
                    f"{rel_page}: Missing expected wheel prefix '{prefix}' in #imednet-pyodide-config"
                )
                continue
            wheel_disk_path = html_dir / str(matched[0])
            if not wheel_disk_path.is_file():
                errors.append(
                    f"{rel_page}: Staged wheel missing from built HTML output: {wheel_disk_path}"
                )

        for wheel_entry in wheels:
            for forbidden_whl in FORBIDDEN_WHEEL_SUBSTRINGS:
                if forbidden_whl in str(wheel_entry):
                    errors.append(
                        f"{rel_page}: Non-browser wheel '{wheel_entry}' must be excluded from config"
                    )

        pyodide_pkgs = set(config.get("pyodide_packages", []))
        for pkg in EXPECTED_PYODIDE_PACKAGES:
            if pkg not in pyodide_pkgs:
                errors.append(f"{rel_page}: Missing Pyodide package '{pkg}' in pyodide_packages")

        requirements = config.get("requirements", [])
        for req in EXPECTED_REQUIREMENTS:
            if not any(str(item).startswith(req) for item in requirements):
                errors.append(
                    f"{rel_page}: Missing pure-Python requirement '{req}' in requirements"
                )

        env_cfg = config.get("env", {})
        if not isinstance(env_cfg, dict) or env_cfg.get("IMEDNET_SIMULATION_MODE") != "true":
            errors.append(f"{rel_page}: env.IMEDNET_SIMULATION_MODE must be 'true'")

    return errors


def _launch_browser(playwright_instance: Any) -> Any:
    """Launch headless Chromium using Playwright's browser cache or pre-installed system Chrome."""
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    try:
        return playwright_instance.chromium.launch(headless=True, args=launch_args)
    except Exception:
        for candidate in (
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ):
            if shutil.which(candidate) or Path(candidate).is_file():
                return playwright_instance.chromium.launch(
                    headless=True,
                    executable_path=candidate,
                    args=launch_args,
                )
        raise


def _run_cell_and_wait(page: Any, cell_index: int, timeout_ms: int) -> dict[str, str]:
    """Click Run on a transformed cell and wait until execution status settles."""
    cell_selector = f"#thebe-codecell-{cell_index}"
    run_selector = f"{cell_selector} .thebelab-run-button"
    status_selector = f"{cell_selector} .thebelab-cell-status"

    page.locator(status_selector).evaluate("el => { el.textContent = ''; }")
    page.locator(run_selector).click()
    page.wait_for_function(
        """([sel]) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            const txt = (el.textContent || "").trim();
            return txt.startsWith("Execution completed");
        }""",
        arg=[status_selector],
        timeout=timeout_ms,
    )

    status_text = (page.locator(status_selector).text_content() or "").strip()
    stdout_loc = page.locator(f"{cell_selector} .thebe-stdout")
    stderr_loc = page.locator(f"{cell_selector} .thebe-stderr")
    stdout_text = stdout_loc.text_content() if stdout_loc.count() > 0 else ""
    stderr_text = stderr_loc.text_content() if stderr_loc.count() > 0 else ""
    return {
        "status": status_text,
        "stdout": stdout_text or "",
        "stderr": stderr_text or "",
    }


def verify_tutorials_in_browser(
    html_dir: Path,
    timeout_ms: int = 120_000,
    screenshot_dir: Path | None = None,
) -> list[str]:
    """Serve built HTML locally and execute all interactive tutorial cells in headless Chromium."""
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    handler = functools.partial(_QuietStaticHandler, directory=str(html_dir.resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    if screenshot_dir is not None:
        screenshot_dir.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as playwright_instance:
            browser = _launch_browser(playwright_instance)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1100})

                # 1. Verify synchronous + mixed tutorial cells on tutorials/quick_start.html
                qs_page = context.new_page()
                qs_wheel_responses: dict[str, int] = {}

                def _record_qs_response(response: Any) -> None:
                    if response.url.endswith(".whl") and f"127.0.0.1:{port}" in response.url:
                        qs_wheel_responses[Path(response.url).name] = response.status

                qs_page.on("response", _record_qs_response)
                qs_page.goto(
                    f"http://127.0.0.1:{port}/tutorials/quick_start.html",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )

                launch_btn = qs_page.locator(".thebe-launch-button").first
                initial_label = (launch_btn.text_content() or "").strip()
                if "mybinder.org" in initial_label.lower():
                    errors.append(
                        f"quick_start.html: Launch button showed Binder label: '{initial_label}'"
                    )

                launch_btn.click()
                qs_page.wait_for_selector(
                    ".thebe-launch-button.thebe-status-ready",
                    timeout=timeout_ms,
                )
                ready_label = (launch_btn.text_content() or "").strip()
                if "mybinder.org" in ready_label.lower():
                    errors.append(
                        f"quick_start.html: Ready button showed Binder label: '{ready_label}'"
                    )
                if ready_label not in ("Pyodide (WebAssembly): ready", "Pyodide Ready"):
                    errors.append(f"quick_start.html: Unexpected ready button text '{ready_label}'")

                for prefix in EXPECTED_WHEEL_PREFIXES:
                    matched_statuses = [
                        status
                        for name, status in qs_wheel_responses.items()
                        if name.startswith(prefix)
                    ]
                    if not matched_statuses or matched_statuses[-1] != 200:
                        errors.append(
                            f"quick_start.html: Expected HTTP 200 for wheel '{prefix}*', "
                            f"observed {qs_wheel_responses}"
                        )

                cell_count = qs_page.locator(".thebelab-cell").count()
                if cell_count != 3:
                    errors.append(
                        f"quick_start.html: Expected 3 interactive Python cells, found {cell_count}"
                    )

                for idx in range(cell_count):
                    outcome = _run_cell_and_wait(qs_page, idx, timeout_ms)
                    if outcome["status"] != "Execution completed.":
                        errors.append(
                            f"quick_start.html cell {idx}: Expected 'Execution completed.', "
                            f"got '{outcome['status']}' (stderr: {outcome['stderr']!r})"
                        )
                    if "Traceback (most recent call last)" in outcome["stderr"]:
                        errors.append(
                            f"quick_start.html cell {idx}: Unexpected traceback in stderr:\n"
                            f"{outcome['stderr']}"
                        )

                if screenshot_dir is not None:
                    qs_page.screenshot(
                        path=str(screenshot_dir / "quick_start_pyodide.png"),
                        full_page=True,
                    )

                # Verify SimulationTransport routing, plugin wheel imports, and SystemExit capture
                if cell_count >= 3:
                    editor = qs_page.locator("#thebe-codecell-2 .thebe-editor")
                    editor.fill(
                        "import imednet_workflows, imednet_sinks\n"
                        "assert type(sdk._client._transport).__name__ == 'SimulationTransport', "
                        "type(sdk._client._transport).__name__\n"
                        "print('PLUGINS_READY', sdk.workflows is not None, sdk.sinks is not None)\n"
                    )
                    plugin_outcome = _run_cell_and_wait(qs_page, 2, timeout_ms)
                    if (
                        plugin_outcome["status"] != "Execution completed."
                        or "PLUGINS_READY True True" not in plugin_outcome["stdout"]
                    ):
                        errors.append(f"quick_start.html plugin check failed: {plugin_outcome}")

                    editor.fill(
                        "import sys\n"
                        "print('BUFFERED_STDOUT')\n"
                        "print('Error: missing credentials', file=sys.stderr)\n"
                        "sys.exit(1)\n"
                    )
                    exit_outcome = _run_cell_and_wait(qs_page, 2, timeout_ms)
                    if exit_outcome["status"] != "Execution completed with errors.":
                        errors.append(
                            f"quick_start.html SystemExit(1) status mismatch: {exit_outcome}"
                        )
                    if "BUFFERED_STDOUT" not in exit_outcome["stdout"]:
                        errors.append(
                            f"quick_start.html SystemExit(1) lost buffered stdout: {exit_outcome}"
                        )
                    if (
                        "Error: missing credentials" not in exit_outcome["stderr"]
                        or "SystemExit: 1" not in exit_outcome["stderr"]
                    ):
                        errors.append(
                            f"quick_start.html SystemExit(1) lost stderr/exit status: {exit_outcome}"
                        )

                qs_page.close()

                # 2. Verify async tutorial cell on tutorials/async_quick_start.html
                #    with DOCUMENTATION_OPTIONS.URL_ROOT absent (Sphinx 7.2+/8.x simulation)
                async_page = context.new_page()
                async_page.add_init_script(
                    """
                    (() => {
                        let _docOpts = {};
                        Object.defineProperty(window, "DOCUMENTATION_OPTIONS", {
                            configurable: true,
                            enumerable: true,
                            get() { return _docOpts; },
                            set(val) {
                                const copy = Object.assign({}, val || {});
                                delete copy.URL_ROOT;
                                _docOpts = copy;
                            }
                        });
                    })();
                    """
                )
                async_wheel_responses: dict[str, int] = {}

                def _record_async_response(response: Any) -> None:
                    if response.url.endswith(".whl") and f"127.0.0.1:{port}" in response.url:
                        async_wheel_responses[Path(response.url).name] = response.status

                async_page.on("response", _record_async_response)
                async_page.goto(
                    f"http://127.0.0.1:{port}/tutorials/async_quick_start.html",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )

                url_root_absent = async_page.evaluate(
                    "() => typeof DOCUMENTATION_OPTIONS === 'undefined' || !('URL_ROOT' in DOCUMENTATION_OPTIONS)"
                )
                if not url_root_absent:
                    errors.append(
                        "async_quick_start.html: Failed to strip DOCUMENTATION_OPTIONS.URL_ROOT"
                    )

                async_launch = async_page.locator(".thebe-launch-button").first
                async_launch.click()
                async_page.wait_for_selector(
                    ".thebe-launch-button.thebe-status-ready",
                    timeout=timeout_ms,
                )

                async_ready_label = (async_launch.text_content() or "").strip()
                if "mybinder.org" in async_ready_label.lower():
                    errors.append(
                        f"async_quick_start.html: Ready button showed Binder label: '{async_ready_label}'"
                    )
                if async_ready_label not in ("Pyodide (WebAssembly): ready", "Pyodide Ready"):
                    errors.append(
                        f"async_quick_start.html: Unexpected ready button text '{async_ready_label}'"
                    )

                for prefix in EXPECTED_WHEEL_PREFIXES:
                    matched_statuses = [
                        status
                        for name, status in async_wheel_responses.items()
                        if name.startswith(prefix)
                    ]
                    if not matched_statuses or matched_statuses[-1] != 200:
                        errors.append(
                            f"async_quick_start.html: Expected HTTP 200 via data-content_root for "
                            f"wheel '{prefix}*', observed {async_wheel_responses}"
                        )

                async_cells = async_page.locator(".thebelab-cell").count()
                if async_cells != 1:
                    errors.append(
                        f"async_quick_start.html: Expected 1 interactive Python cell, found {async_cells}"
                    )
                else:
                    async_outcome = _run_cell_and_wait(async_page, 0, timeout_ms)
                    if async_outcome["status"] != "Execution completed.":
                        errors.append(
                            f"async_quick_start.html cell 0: Expected 'Execution completed.', "
                            f"got '{async_outcome['status']}' (stderr: {async_outcome['stderr']!r})"
                        )
                    if "Traceback (most recent call last)" in async_outcome["stderr"]:
                        errors.append(
                            f"async_quick_start.html cell 0: Unexpected traceback:\n"
                            f"{async_outcome['stderr']}"
                        )
                    if (
                        "DEMO-STUDY" not in async_outcome["stdout"]
                        or "COMPLETED" not in async_outcome["stdout"]
                    ):
                        errors.append(
                            f"async_quick_start.html cell 0: Missing expected simulated output, "
                            f"stdout was: {async_outcome['stdout']!r}"
                        )

                if screenshot_dir is not None:
                    async_page.screenshot(
                        path=str(screenshot_dir / "async_quick_start_pyodide.png"),
                        full_page=True,
                    )

                async_page.close()
                context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    return errors


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Verify interactive Sphinx Pyodide tutorials in headless Chromium."
    )
    parser.add_argument(
        "--html-dir",
        type=Path,
        default=Path("docs/_build/html"),
        help="Path to built Sphinx HTML directory (default: docs/_build/html)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=120_000,
        help="Timeout in milliseconds for Pyodide runtime initialization and cell execution",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="Run static HTML configuration checks without launching headless Chromium",
    )
    parser.add_argument(
        "--screenshot-dir",
        type=Path,
        default=None,
        help="Optional directory outside the repository to save verification screenshots",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run static and browser verification gates for interactive Pyodide tutorials."""
    args = parse_args(argv)
    print(f"Verifying built tutorial HTML invariants in {args.html_dir}...")
    errors = verify_built_tutorial_html(args.html_dir)

    if not errors and not args.static_only:
        print("Executing interactive tutorial cells in headless Chromium via Pyodide...")
        errors.extend(
            verify_tutorials_in_browser(
                html_dir=args.html_dir,
                timeout_ms=args.timeout_ms,
                screenshot_dir=args.screenshot_dir,
            )
        )

    if errors:
        print(
            f"\nPyodide tutorial verification failed with {len(errors)} error(s):",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("Interactive Pyodide tutorial verification passed successfully (4/4 tutorial cells ok).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
