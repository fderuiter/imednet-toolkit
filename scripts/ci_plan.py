"""Compute impacted monorepo packages from a list of changed file paths."""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from typing import Any

_IGNORED_PREFIXES = ("docs/", "examples/", ".agents/")
_WORKFLOW_UNIT_TESTS = frozenset(
    {
        "tests/integration/test_sync_worker_integration.py",
        "tests/unit/test_chunked_pipeline.py",
        "tests/unit/test_config_version_control.py",
        "tests/unit/test_duckdb_ingestion_workflow.py",
        "tests/unit/test_job_poller.py",
        "tests/unit/test_record_mapper.py",
        "tests/unit/test_record_mapper_hierarchy.py",
        "tests/unit/test_state_ledger.py",
        "tests/unit/test_state_ledger_providers.py",
        "tests/unit/test_study_structure.py",
        "tests/unit/test_sync_worker.py",
        "tests/unit/test_cached_records_loader.py",
    }
)
_CORE_AND_WORKFLOW_TESTS = frozenset(
    {
        "tests/unit/test_batch_validation_and_polling.py",
        "tests/unit/test_keyed_upserts_and_watermark_recovery.py",
        "tests/unit/test_mode_safe_plugin_protocols.py",
        "tests/unit/test_tabular_import.py",
    }
)
_SHARED_TEST_PREFIXES = ("tests/fixtures/", "tests/utils/", "tests/typing/")
_SHARED_TEST_FILES = frozenset({"tests/__init__.py", "tests/conftest.py"})


def _normalize_path(file_path: str) -> str:
    """Normalize OS separators and leading relative prefixes in repo paths."""
    normalized = file_path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_ignored_path(file_path: str) -> bool:
    """Return True when a changed path requires no package build or test run."""
    normalized = _normalize_path(file_path)
    parts = normalized.split("/")
    if len(parts) == 3 and parts[0] == "packages" and parts[1] and parts[2] == "README.md":
        return False
    return normalized.startswith(_IGNORED_PREFIXES) or normalized.endswith(".md")


def parse_dependency_name(dep_str: str) -> str:
    """Extract the base package name from a PEP 508 dependency string.

    This safely strips out optional extras, version bounds, and environment markers.
    """
    match = re.match(r"^([a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?)", dep_str.strip())
    if match:
        return match.group(1)
    return dep_str.strip()


def _resolve_package_dirs(
    candidates: Sequence[tuple[str, str]],
    packages: Mapping[str, Mapping[str, Any]],
    name_to_dir: Mapping[str, str],
) -> set[str]:
    """Resolve canonical (dir_name, package_name) candidates against loaded workspace packages."""
    resolved: set[str] = set()
    for dir_name, pkg_name in candidates:
        if dir_name in packages:
            resolved.add(dir_name)
        elif pkg_name in name_to_dir:
            resolved.add(name_to_dir[pkg_name])
    return resolved


def _owners_for_top_level_test(
    normalized: str,
    packages: Mapping[str, Mapping[str, Any]],
    name_to_dir: Mapping[str, str],
) -> set[str]:
    """Map top-level tests/ paths to owning package directories per .github/workflows/main.yml."""
    if normalized in _SHARED_TEST_FILES or normalized.startswith(_SHARED_TEST_PREFIXES):
        return set(packages.keys())

    if fnmatchcase(normalized, "tests/unit/test_airflow*.py") or "airflow" in normalized:
        return _resolve_package_dirs(
            [("providers-airflow", "apache-airflow-providers-imednet")],
            packages,
            name_to_dir,
        )

    if (
        normalized == "tests/unit/cli/test_tui_cli.py"
        or normalized.startswith("tests/unit/tui/")
        or "/tui/" in normalized
    ):
        return _resolve_package_dirs(
            [("plugins-tui", "imednet-tui")],
            packages,
            name_to_dir,
        )

    if "sinks" in normalized:
        return _resolve_package_dirs(
            [("plugins-sinks", "imednet-plugins-sinks")],
            packages,
            name_to_dir,
        )

    if (
        fnmatchcase(normalized, "tests/unit/test_workflows*.py")
        or normalized.startswith(("tests/unit/workflows/", "tests/workflows/"))
        or normalized in _WORKFLOW_UNIT_TESTS
        or "workflows" in normalized
    ):
        return _resolve_package_dirs(
            [("plugins-workflows", "imednet-workflows")],
            packages,
            name_to_dir,
        )

    if normalized.startswith("tests/unit/cli/") or normalized in _CORE_AND_WORKFLOW_TESTS:
        return _resolve_package_dirs(
            [("core", "imednet"), ("plugins-workflows", "imednet-workflows")],
            packages,
            name_to_dir,
        )

    return _resolve_package_dirs([("core", "imednet")], packages, name_to_dir)


def plan_ci_impact(
    changed_files: Sequence[str],
    packages: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Determine which monorepo packages must build and test for changed paths."""
    cleaned_files = [line.strip() for line in changed_files if line.strip()]

    def fallback() -> dict[str, list[str]]:
        all_pkgs = list(packages.keys())
        return {"test": all_pkgs, "build": list(all_pkgs)}

    if not cleaned_files:
        return fallback()

    if all(is_ignored_path(path) for path in cleaned_files):
        return {"test": [], "build": []}

    name_to_dir = {
        str(info["name"]): dir_name for dir_name, info in packages.items() if info.get("name")
    }
    build_impacted: set[str] = set()
    test_only_impacted: set[str] = set()

    for file_path in cleaned_files:
        normalized = _normalize_path(file_path)

        if is_ignored_path(normalized):
            continue

        if normalized.startswith("packages/"):
            parts = normalized.split("/")
            if len(parts) > 1 and parts[1] in packages:
                changed_pkg_dir = parts[1]
                if len(parts) > 2 and parts[2] == "tests":
                    test_only_impacted.add(changed_pkg_dir)
                else:
                    build_impacted.add(changed_pkg_dir)
                continue

        if normalized.startswith("tests/"):
            owners = _owners_for_top_level_test(normalized, packages, name_to_dir)
            if owners:
                test_only_impacted.update(owners)
                continue

        return fallback()

    behavior_changed = set(build_impacted)
    added = True
    while added:
        added = False
        for pkg_dir, info in packages.items():
            if pkg_dir in behavior_changed:
                continue
            for dep in info.get("deps", []):
                dep_dir = name_to_dir.get(dep)
                if dep_dir and dep_dir in behavior_changed:
                    behavior_changed.add(pkg_dir)
                    added = True
                    break

    test_impacted = behavior_changed.union(test_only_impacted)
    ordered_build = [pkg for pkg in packages if pkg in build_impacted]
    ordered_test = [pkg for pkg in packages if pkg in test_impacted]
    return {"build": ordered_build, "test": ordered_test}


def main() -> None:
    """Read changed file paths from stdin and print the impacted package JSON plan."""
    import tomllib

    packages: dict[str, dict[str, Any]] = {}
    for pyproject in sorted(glob.glob("packages/*/pyproject.toml")):
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        pkg_dir = os.path.basename(os.path.dirname(pyproject))
        pkg_name = data.get("project", {}).get("name")
        deps = list(data.get("project", {}).get("dependencies", []))
        packages[pkg_dir] = {
            "name": pkg_name,
            "deps": [parse_dependency_name(dep) for dep in deps],
        }

    changed_files = sys.stdin.read().splitlines()
    print(json.dumps(plan_ci_impact(changed_files, packages)))


if __name__ == "__main__":
    main()
