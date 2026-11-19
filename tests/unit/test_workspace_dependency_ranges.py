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


def test_snowflake_extras_declare_connector_and_pyarrow():
    """Confirm Snowflake extras in core and plugins-sinks declare snowflake-connector-python alongside pyarrow."""
    import importlib.metadata

    core_pyproject = tomllib.loads((ROOT / "packages" / "core" / "pyproject.toml").read_text())[
        "project"
    ]
    sinks_pyproject = tomllib.loads(
        (ROOT / "packages" / "plugins-sinks" / "pyproject.toml").read_text()
    )["project"]

    def _extra_pkg_names(entries: list[str]) -> set[str]:
        return {canonicalize_name(Requirement(item).name) for item in entries}

    core_sf = _extra_pkg_names(core_pyproject["optional-dependencies"]["snowflake"])
    assert {
        "imednet-plugins-sinks",
        "snowflake-connector-python",
        "pyarrow",
    } <= core_sf

    sinks_sf = _extra_pkg_names(sinks_pyproject["optional-dependencies"]["snowflake"])
    assert {"snowflake-connector-python", "pyarrow"} <= sinks_sf

    sinks_all = _extra_pkg_names(sinks_pyproject["optional-dependencies"]["all"])
    assert {"snowflake-connector-python", "pyarrow"} <= sinks_all

    # Verify installed distribution metadata and environment resolution match
    core_installed_reqs = importlib.metadata.requires("imednet") or []
    sinks_installed_reqs = importlib.metadata.requires("imednet-plugins-sinks") or []

    assert any(
        canonicalize_name(Requirement(r).name) == "snowflake-connector-python" and "snowflake" in r
        for r in core_installed_reqs
    )
    assert any(
        canonicalize_name(Requirement(r).name) == "snowflake-connector-python" and "snowflake" in r
        for r in sinks_installed_reqs
    )
    assert any(
        canonicalize_name(Requirement(r).name) == "snowflake-connector-python" and "all" in r
        for r in sinks_installed_reqs
    )

    import pyarrow
    import snowflake.connector

    assert pyarrow.__version__
    assert snowflake.connector.__version__
