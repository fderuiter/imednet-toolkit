"""Compute impacted monorepo packages from a list of changed file paths."""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any

_IGNORED_PREFIXES = ("docs/", "examples/", ".agents/")


def _normalize_path(file_path: str) -> str:
    """Normalize OS separators and leading relative prefixes in repo paths."""
    normalized = file_path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_ignored_path(file_path: str) -> bool:
    """Return True when a changed path requires no package build or test run."""
    normalized = _normalize_path(file_path)
    return normalized.startswith(_IGNORED_PREFIXES) or normalized.endswith(".md")


def parse_dependency_name(dep_str: str) -> str:
    """Extract the base package name from a PEP 508 dependency string.

    This safely strips out optional extras, version bounds, and environment markers.
    """
    match = re.match(r"^([a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?)", dep_str.strip())
    if match:
        return match.group(1)
    return dep_str.strip()


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
    return {"build": list(build_impacted), "test": list(test_impacted)}


def main() -> None:
    """Read changed file paths from stdin and print the impacted package JSON plan."""
    import tomllib

    packages: dict[str, dict[str, Any]] = {}
    for pyproject in glob.glob("packages/*/pyproject.toml"):
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
