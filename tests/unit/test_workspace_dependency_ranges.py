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
    """Confirm Snowflake extras declare snowflake-connector-python alongside pyarrow while keeping default dev env independent."""
    import importlib.metadata

    root_pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    root_project = root_pyproject["project"]
    core_pyproject = tomllib.loads((ROOT / "packages" / "core" / "pyproject.toml").read_text())[
        "project"
    ]
    sinks_pyproject = tomllib.loads(
        (ROOT / "packages" / "plugins-sinks" / "pyproject.toml").read_text()
    )["project"]

    def _pkg_names(entries: list[str]) -> set[str]:
        return {canonicalize_name(Requirement(item).name) for item in entries}

    # Default workspace and Hatch default test environments must remain independent of Snowflake drivers
    default_env_entries = [
        *root_project.get("dependencies", []),
        *root_project.get("optional-dependencies", {}).get("dev", []),
        *root_project.get("optional-dependencies", {}).get("docs", []),
        *root_pyproject["tool"]["hatch"]["envs"]["default"].get("dependencies", []),
        *core_project_deps(core_pyproject),
        *core_project_deps(sinks_pyproject),
    ]
    assert "snowflake-connector-python" not in _pkg_names(default_env_entries)

    core_sf_reqs = {
        canonicalize_name(req.name): req
        for req in (
            Requirement(item) for item in core_pyproject["optional-dependencies"]["snowflake"]
        )
    }
    assert {
        "imednet-plugins-sinks",
        "snowflake-connector-python",
        "pyarrow",
    } <= set(core_sf_reqs)
    assert core_sf_reqs["snowflake-connector-python"].specifier.contains("3.0.0")
    assert core_sf_reqs["pyarrow"].specifier.contains("14.0.1")

    sinks_sf = _pkg_names(sinks_pyproject["optional-dependencies"]["snowflake"])
    assert {"snowflake-connector-python", "pyarrow"} <= sinks_sf

    sinks_all = _pkg_names(sinks_pyproject["optional-dependencies"]["all"])
    assert {"snowflake-connector-python", "pyarrow"} <= sinks_all

    # Verify installed distribution metadata markers without importing optional Snowflake drivers
    def _active_dist_pkg_names(dist_name: str, extra: str) -> set[str]:
        reqs = [Requirement(raw) for raw in (importlib.metadata.requires(dist_name) or [])]
        return {
            canonicalize_name(req.name)
            for req in reqs
            if req.marker is None or req.marker.evaluate({"extra": extra})
        }

    assert "snowflake-connector-python" not in _active_dist_pkg_names("imednet", "")
    assert "snowflake-connector-python" not in _active_dist_pkg_names("imednet-plugins-sinks", "")
    assert {"snowflake-connector-python", "pyarrow"} <= _active_dist_pkg_names(
        "imednet", "snowflake"
    )
    assert {"snowflake-connector-python", "pyarrow"} <= _active_dist_pkg_names(
        "imednet-plugins-sinks", "snowflake"
    )
    assert {"snowflake-connector-python", "pyarrow"} <= _active_dist_pkg_names(
        "imednet-plugins-sinks", "all"
    )

    # Verify lockfile resolution wires snowflake-connector-python only via optional extras
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    by_name = {canonicalize_name(pkg["name"]): pkg for pkg in locked["package"]}
    assert "snowflake-connector-python" in by_name

    workspace_locked = by_name["imednet-workspace"]
    workspace_dev_names = {
        canonicalize_name(dep["name"])
        for group in workspace_locked.get("optional-dependencies", {}).values()
        for dep in group
    }
    assert "snowflake-connector-python" not in workspace_dev_names

    core_locked_sf = {
        canonicalize_name(dep["name"])
        for dep in by_name["imednet"]["optional-dependencies"]["snowflake"]
    }
    assert {"snowflake-connector-python", "pyarrow"} <= core_locked_sf

    sinks_locked_opts = by_name["imednet-plugins-sinks"]["optional-dependencies"]
    for extra_name in ("snowflake", "all"):
        locked_names = {canonicalize_name(dep["name"]) for dep in sinks_locked_opts[extra_name]}
        assert {"snowflake-connector-python", "pyarrow"} <= locked_names


def core_project_deps(project_table: dict) -> list[str]:
    """Return base dependencies declared on a project table."""
    return list(project_table.get("dependencies", []))
