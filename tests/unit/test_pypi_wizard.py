"""Unit tests for the PyPI OIDC setup wizard script."""

import re
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def test_pypi_wizard_stages_count() -> None:
    """Verify configured total stages in setup-pypi-oidc.sh matches stage function calls."""
    wizard_path = Path(__file__).resolve().parents[2] / "scripts" / "wizards" / "setup-pypi-oidc.sh"
    content = wizard_path.read_text(encoding="utf-8")

    # Match the active TOTAL_STAGES in the STAGES section
    stage_var_matches = re.findall(r"^TOTAL_STAGES=(\d+)", content, flags=re.MULTILINE)
    assert stage_var_matches, "TOTAL_STAGES assignment not found in setup-pypi-oidc.sh"
    active_total_stages = int(stage_var_matches[-1])

    # Find all stage calls: stage "..."
    stage_calls = re.findall(r'^\s*stage\s+"([^"]+)"', content, flags=re.MULTILINE)

    assert active_total_stages == len(stage_calls)
    assert active_total_stages == 6


def test_pypi_wizard_banner_package_count() -> None:
    """Verify banner in setup-pypi-oidc.sh announces the correct package count."""
    wizard_path = Path(__file__).resolve().parents[2] / "scripts" / "wizards" / "setup-pypi-oidc.sh"
    content = wizard_path.read_text(encoding="utf-8")

    banner_match = re.search(
        r'banner\s+"PyPI Trusted Publishing Setup \((\d+)\s+packages\)"', content
    )
    assert banner_match is not None, "Banner package count string not matched"
    announced_package_count = int(banner_match.group(1))

    packages_dir = Path(__file__).resolve().parents[2] / "packages"
    workspace_pyprojects = list(packages_dir.glob("*/pyproject.toml"))
    assert announced_package_count == len(workspace_pyprojects)
    assert announced_package_count == 4


def test_pypi_wizard_package_names_match_workspace() -> None:
    """Verify package names in setup-pypi-oidc.sh match workspace distribution names."""
    repo_root = Path(__file__).resolve().parents[2]
    wizard_path = repo_root / "scripts" / "wizards" / "setup-pypi-oidc.sh"
    packages_dir = repo_root / "packages"

    content = wizard_path.read_text(encoding="utf-8")

    # Collect actual workspace package names from each package pyproject.toml
    workspace_package_names = set()
    for pyproject_file in packages_dir.glob("*/pyproject.toml"):
        with pyproject_file.open("rb") as f:
            data = tomllib.load(f)
            workspace_package_names.add(data["project"]["name"])

    # Extract PyPI Project Name entries from the wizard steps
    wizard_project_names = set(re.findall(r"•\s+PyPI Project Name:\s+([a-zA-Z0-9_\-]+)", content))

    # Extract package names from say "Register Trusted Publisher for '<pkg>'..."
    wizard_say_names = set(re.findall(r"say\s+\"Register Trusted Publisher for '([^']+)'", content))

    assert workspace_package_names == {
        "imednet",
        "imednet-workflows",
        "apache-airflow-providers-imednet",
        "imednet-plugins-sinks",
    }
    assert wizard_project_names == workspace_package_names
    assert wizard_say_names == workspace_package_names


def test_pypi_wizard_no_stale_package_names() -> None:
    """Ensure the stale package name 'imednet-sinks' is not present in setup-pypi-oidc.sh."""
    wizard_path = Path(__file__).resolve().parents[2] / "scripts" / "wizards" / "setup-pypi-oidc.sh"
    content = wizard_path.read_text(encoding="utf-8")

    assert "imednet-sinks" not in content


def _extract_open_url_definition(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    match = re.search(r"(open_url\(\)\s*\{[\s\S]*?\n\})", content)
    assert match is not None, f"open_url() definition not found in {path}"
    return match.group(1).strip()


def test_wizard_open_url_shared_across_scripts_and_template() -> None:
    """Verify all wizard scripts and the wizard template share the identical open_url implementation."""
    repo_root = Path(__file__).resolve().parents[2]
    script_paths = [
        repo_root / "scripts" / "wizards" / "setup-pypi-oidc.sh",
        repo_root / "scripts" / "wizards" / "setup-ci-live-testing.sh",
        repo_root / "scripts" / "wizards" / "setup-imednet-env.sh",
        repo_root / ".agents" / "skills" / "wizard" / "template.sh",
    ]

    implementations = [_extract_open_url_definition(p) for p in script_paths]
    for i, path in enumerate(script_paths[1:], start=1):
        assert implementations[i] == implementations[0], (
            f"open_url implementation in {path} differs from setup-pypi-oidc.sh"
        )


def test_wizard_open_url_headless_fallback() -> None:
    """Verify open_url prints URL and instructions without invoking xdg-open in headless mode."""
    import subprocess
    import tempfile

    repo_root = Path(__file__).resolve().parents[2]
    wizard_path = repo_root / "scripts" / "wizards" / "setup-pypi-oidc.sh"
    open_url_func = _extract_open_url_definition(wizard_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        mock_bin = Path(tmpdir) / "bin"
        mock_bin.mkdir()
        xdg_open = mock_bin / "xdg-open"
        log_file = Path(tmpdir) / "browser.log"
        xdg_open.write_text(f"#!/bin/sh\necho 'called' > '{log_file}'\n", encoding="utf-8")
        xdg_open.chmod(0o755)

        script = f"""
GREEN=""
RESET=""
warn() {{ printf 'WARN: %s\\n' "$1"; }}
{open_url_func}

unset DISPLAY WAYLAND_DISPLAY
PATH="{mock_bin}:$PATH" open_url "https://example.com/headless-test"
"""
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
        assert not log_file.exists(), (
            "xdg-open should not be invoked when DISPLAY and WAYLAND_DISPLAY are unset"
        )
        assert (
            "couldn't open a browser; visit it manually: https://example.com/headless-test"
            in res.stdout
        )


def test_wizard_open_url_graphical_dispatch() -> None:
    """Verify open_url launches browser when DISPLAY or WAYLAND_DISPLAY is active."""
    import subprocess
    import tempfile
    import time

    repo_root = Path(__file__).resolve().parents[2]
    wizard_path = repo_root / "scripts" / "wizards" / "setup-pypi-oidc.sh"
    open_url_func = _extract_open_url_definition(wizard_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        mock_bin = Path(tmpdir) / "bin"
        mock_bin.mkdir()
        xdg_open = mock_bin / "xdg-open"
        log_file = Path(tmpdir) / "browser.log"
        xdg_open.write_text(f"#!/bin/sh\necho \"opened $1\" >> '{log_file}'\n", encoding="utf-8")
        xdg_open.chmod(0o755)

        # 1. Test with DISPLAY set
        script_x11 = f"""
GREEN=""
RESET=""
warn() {{ printf 'WARN: %s\\n' "$1"; }}
{open_url_func}

export DISPLAY=":0"
unset WAYLAND_DISPLAY
PATH="{mock_bin}:$PATH" open_url "https://example.com/x11-test"
"""
        res_x11 = subprocess.run(
            ["bash", "-c", script_x11], capture_output=True, text=True, check=True
        )
        assert "↗ opening https://example.com/x11-test" in res_x11.stdout

        time.sleep(0.1)
        assert log_file.exists()
        assert "opened https://example.com/x11-test" in log_file.read_text(encoding="utf-8")

        # 2. Test with WAYLAND_DISPLAY set
        log_file.unlink()
        script_wayland = f"""
GREEN=""
RESET=""
warn() {{ printf 'WARN: %s\\n' "$1"; }}
{open_url_func}

unset DISPLAY
export WAYLAND_DISPLAY="wayland-0"
PATH="{mock_bin}:$PATH" open_url "https://example.com/wayland-test"
"""
        res_wayland = subprocess.run(
            ["bash", "-c", script_wayland], capture_output=True, text=True, check=True
        )
        assert "↗ opening https://example.com/wayland-test" in res_wayland.stdout

        time.sleep(0.1)
        assert log_file.exists()
        assert "opened https://example.com/wayland-test" in log_file.read_text(encoding="utf-8")


def test_wizard_open_url_background_isolation() -> None:
    """Verify child browser processes do not hold terminal stdin."""
    import subprocess
    import tempfile

    repo_root = Path(__file__).resolve().parents[2]
    wizard_path = repo_root / "scripts" / "wizards" / "setup-pypi-oidc.sh"
    open_url_func = _extract_open_url_definition(wizard_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        mock_bin = Path(tmpdir) / "bin"
        mock_bin.mkdir()
        xdg_open = mock_bin / "xdg-open"
        log_file = Path(tmpdir) / "child_stdin.log"
        # Mock browser attempts to read stdin
        xdg_open.write_text(
            f"#!/bin/sh\nread -r data\necho \"read: $data\" > '{log_file}'\n", encoding="utf-8"
        )
        xdg_open.chmod(0o755)

        script = f"""
GREEN=""
RESET=""
warn() {{ printf 'WARN: %s\\n' "$1"; }}
{open_url_func}

export DISPLAY=":0"
unset WAYLAND_DISPLAY
PATH="{mock_bin}:$PATH" open_url "https://example.com/stdin-test"
read -r parent_line
echo "parent received: $parent_line"
"""
        res = subprocess.run(
            ["bash", "-c", script],
            input="expected_parent_input\n",
            capture_output=True,
            text=True,
            check=True,
        )
        assert "parent received: expected_parent_input" in res.stdout


def test_wizard_open_url_wsl_and_macos() -> None:
    """Verify WSL tools and macOS open are preserved even without DISPLAY."""
    import subprocess
    import tempfile
    import time

    repo_root = Path(__file__).resolve().parents[2]
    wizard_path = repo_root / "scripts" / "wizards" / "setup-pypi-oidc.sh"
    open_url_func = _extract_open_url_definition(wizard_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        mock_bin = Path(tmpdir) / "bin"
        mock_bin.mkdir()
        wslview = mock_bin / "wslview"
        mac_open = mock_bin / "open"
        log_file = Path(tmpdir) / "browser.log"

        wslview.write_text(f"#!/bin/sh\necho \"wslview: $1\" >> '{log_file}'\n", encoding="utf-8")
        wslview.chmod(0o755)
        mac_open.write_text(f"#!/bin/sh\necho \"open: $1\" >> '{log_file}'\n", encoding="utf-8")
        mac_open.chmod(0o755)

        # 1. Test WSL wslview when DISPLAY is unset
        script_wsl = f"""
GREEN=""
RESET=""
warn() {{ printf 'WARN: %s\\n' "$1"; }}
{open_url_func}

unset DISPLAY WAYLAND_DISPLAY
PATH="{mock_bin}:$PATH" open_url "https://example.com/wsl-test"
"""
        res_wsl = subprocess.run(
            ["bash", "-c", script_wsl], capture_output=True, text=True, check=True
        )
        assert "↗ opening https://example.com/wsl-test" in res_wsl.stdout
        time.sleep(0.1)
        assert "wslview: https://example.com/wsl-test" in log_file.read_text(encoding="utf-8")

        # 2. Test macOS open when Darwin and DISPLAY is unset
        wslview.unlink()
        log_file.unlink()
        script_mac = f"""
GREEN=""
RESET=""
warn() {{ printf 'WARN: %s\\n' "$1"; }}
{open_url_func}

unset DISPLAY WAYLAND_DISPLAY
OSTYPE=darwin21 PATH="{mock_bin}:$PATH" open_url "https://example.com/mac-test"
"""
        res_mac = subprocess.run(
            ["bash", "-c", script_mac], capture_output=True, text=True, check=True
        )
        assert "↗ opening https://example.com/mac-test" in res_mac.stdout
        time.sleep(0.1)
        assert "open: https://example.com/mac-test" in log_file.read_text(encoding="utf-8")
