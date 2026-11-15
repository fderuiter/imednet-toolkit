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
