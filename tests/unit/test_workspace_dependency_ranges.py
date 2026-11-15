"""Catch release metadata that cannot install the checked-in package set."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]


def test_workspace_dependency_ranges_accept_checked_in_versions():
    projects = [
        tomllib.loads(path.read_text())["project"]
        for path in (ROOT / "packages").glob("*/pyproject.toml")
    ]
    versions = {canonicalize_name(p["name"]): p["version"] for p in projects}
    checked = set()
    for project in projects:
        dependencies = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            dependencies.extend(extra)
        for dependency in dependencies:
            requirement = Requirement(dependency)
            name = canonicalize_name(requirement.name)
            if name in versions:
                checked.add(project["name"])
                assert requirement.specifier.contains(versions[name]), (
                    f"{project['name']} requires {requirement}, but {name} is {versions[name]}"
                )
    assert {
        "imednet-workflows",
        "imednet-plugins-sinks",
        "apache-airflow-providers-imednet",
    } <= checked
