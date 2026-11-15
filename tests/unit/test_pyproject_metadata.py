"""Unit tests for pyproject metadata."""

import re
from pathlib import Path

import imednet


def test_project_version_is_single_source_of_truth() -> None:
    """Test that project version is single source of truth."""
    pyproject_path = Path(__file__).resolve().parents[2] / "packages" / "core" / "pyproject.toml"
    content = pyproject_path.read_text(encoding="utf-8")

    project_section_match = re.search(
        r"(?ms)^\[project\]\n(.*?)(?=^\[|\Z)",
        content,
    )
    assert project_section_match is not None
    project_section = project_section_match.group(1)

    version_match = re.search(r'^\s*version\s*=\s*"([^"]+)"', project_section, flags=re.MULTILINE)
    assert version_match is not None
    assert version_match.group(1) == imednet.__version__

    readme_match = re.search(r'^\s*readme\s*=\s*"([^"]+)"', project_section, flags=re.MULTILINE)
    assert readme_match is not None
    readme_path = pyproject_path.parent / readme_match.group(1)
    assert readme_path.exists()
    assert readme_path.parent == pyproject_path.parent

    for required_key in ("name", "description", "readme", "authors", "license"):
        assert (
            re.search(rf"^\s*{re.escape(required_key)}\s*=", project_section, flags=re.MULTILINE)
            is not None
        )


def test_hatch_scripts_for_maintenance_tasks() -> None:
    """Test that root pyproject.toml registers Hatch scripts for developer maintenance tasks."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    root_pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with root_pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    hatch_scripts = (
        data.get("tool", {}).get("hatch", {}).get("envs", {}).get("default", {}).get("scripts", {})
    )

    expected_tasks = [
        "check-root-files",
        "ci-plan",
        "aggregate-verification",
        "tighten-dependencies",
        "typecheck-docs",
        "validate-diagrams",
        "validate-docs",
        "validate-package-consumption",
    ]

    for task in expected_tasks:
        assert task in hatch_scripts, (
            f"Hatch script '{task}' should be registered in root pyproject.toml"
        )
        script_file = task.replace("-", "_") + ".py"
        assert f"scripts/{script_file}" in hatch_scripts[task]

    for retired_task in ("post-smoke-record", "post_smoke_record"):
        assert retired_task not in hatch_scripts
