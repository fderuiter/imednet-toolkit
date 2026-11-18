"""Unit tests for release workflow trigger matching and package dispatch."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture
def release_workflow() -> dict:
    source = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    return yaml.load(source, Loader=yaml.BaseLoader)


@pytest.fixture
def determine_packages_script(release_workflow: dict) -> str:
    steps = release_workflow["jobs"]["plan"]["steps"]
    step = next(s for s in steps if s.get("id") == "determine-packages")
    return step["run"]


def test_release_workflow_tag_triggers(release_workflow: dict) -> None:
    """Verify that push tag triggers include imednet-tui-v*.*.* and all packages."""
    tags = release_workflow["on"]["push"]["tags"]
    assert "imednet-tui-v*.*.*" in tags
    assert "imednet-v*.*.*" in tags
    assert "imednet-workflows-v*.*.*" in tags
    assert "apache-airflow-providers-imednet-v*.*.*" in tags
    assert "imednet-plugins-sinks-v*.*.*" in tags
    assert "v*.*.*" in tags


def test_release_workflow_permissions(release_workflow: dict) -> None:
    """Verify top-level permissions are empty and job-level permissions are explicitly scoped."""
    assert release_workflow.get("permissions") == {}
    jobs = release_workflow["jobs"]
    assert jobs["plan"]["permissions"] == {"contents": "read"}
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish"]["permissions"] == {
        "id-token": "write",
        "attestations": "write",
        "contents": "read",
    }
    assert jobs["publish"]["environment"] == "pypi"


def _run_determine_packages(
    script: str, github_ref: str, tmp_path: Path, cwd: Path | None = None
) -> list[str]:
    output_file = tmp_path / "github_output"
    output_file.touch()

    env = {
        **os.environ,
        "GITHUB_REF": github_ref,
        "GITHUB_OUTPUT": str(output_file),
    }

    subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=cwd or tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    content = output_file.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("packages="):
            return json.loads(line[len("packages=") :])
    raise ValueError(f"packages output not found in {output_file}: {content}")


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is required for script execution")
@pytest.mark.parametrize(
    "github_ref,expected_packages",
    [
        ("refs/tags/imednet-tui-v1.0.0", ["plugins-tui"]),
        ("refs/tags/imednet-tui-v2.3.4", ["plugins-tui"]),
        ("refs/tags/imednet-v1.1.0", ["core"]),
        ("refs/tags/imednet-workflows-v1.1.0", ["plugins-workflows"]),
        ("refs/tags/apache-airflow-providers-imednet-v1.1.0", ["providers-airflow"]),
        ("refs/tags/imednet-plugins-sinks-v1.0.1", ["plugins-sinks"]),
        (
            "refs/tags/v1.0.0",
            ["core", "plugins-workflows", "providers-airflow", "plugins-sinks", "plugins-tui"],
        ),
        (
            "refs/tags/custom-tag",
            ["core", "plugins-workflows", "providers-airflow", "plugins-sinks", "plugins-tui"],
        ),
        (
            "refs/heads/other-branch",
            ["core", "plugins-workflows", "providers-airflow", "plugins-sinks", "plugins-tui"],
        ),
    ],
)
def test_tag_and_fallback_package_resolution(
    determine_packages_script: str, tmp_path: Path, github_ref: str, expected_packages: list[str]
) -> None:
    """Verify that tag resolution and fallback resolution output expected packages."""
    packages = _run_determine_packages(determine_packages_script, github_ref, tmp_path)
    assert packages == expected_packages


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is required for script execution")
def test_main_branch_empty_plan_fallback(determine_packages_script: str, tmp_path: Path) -> None:
    """Verify that refs/heads/main falls back to all packages when ci_plan build is empty."""
    packages = _run_determine_packages(
        determine_packages_script, "refs/heads/main", tmp_path, cwd=ROOT
    )
    assert "plugins-tui" in packages
    assert set(packages) == {
        "core",
        "plugins-workflows",
        "providers-airflow",
        "plugins-sinks",
        "plugins-tui",
    }


def test_fallback_package_list_matches_workspace() -> None:
    """Verify that the 5-package fallback set matches all workspace package directories."""
    packages_dir = ROOT / "packages"
    workspace_dirs = {p.name for p in packages_dir.iterdir() if p.is_dir() and (p / "pyproject.toml").exists()}
    expected_fallback = {"core", "plugins-workflows", "providers-airflow", "plugins-sinks", "plugins-tui"}
    assert workspace_dirs == expected_fallback
