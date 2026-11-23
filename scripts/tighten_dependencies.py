#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def get_internal_packages(manifest_path: Path):
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)

    base_dir = manifest_path.parent
    internal_packages = {}
    for folder, version in manifest.items():
        pyproject_path = base_dir / folder / "pyproject.toml"
        if pyproject_path.exists():
            with pyproject_path.open("rb") as f:
                data = tomllib.load(f)
            pkg_name = data.get("project", {}).get("name")
            if pkg_name:
                internal_packages[pkg_name] = {
                    "version": version,
                    "folder": folder,
                    "pyproject_path": pyproject_path,
                }
    return internal_packages


def compute_upper_bound(version_str: str) -> str:
    parts = version_str.split('.')
    major = int(parts[0])
    minor = int(parts[1])
    return f"<{major}.{minor + 1}.0"


def rewrite_spec(current_spec: str, pkg_version: str) -> str:
    upper_bound = compute_upper_bound(pkg_version)
    if not current_spec:
        return f">={pkg_version},{upper_bound}"

    clauses = current_spec.split(',')
    new_clauses = []
    for c in clauses:
        c_strip = c.strip()
        if not c_strip.startswith('<'):
            new_clauses.append(c_strip)

    new_clauses.append(upper_bound)
    return ",".join(new_clauses)


def process_dependency(dep_str: str, internal_packages: dict) -> str:
    for pkg_name, pkg_info in internal_packages.items():
        pkg_version = pkg_info["version"]

        # Regex to check if this dependency string is for this internal package
        pattern = r'^(' + re.escape(pkg_name) + r')(\[[^\]]*\])?([><=~^!].*)?$'
        match = re.match(pattern, dep_str)
        if match:
            name = match.group(1)
            extras = match.group(2) or ""
            spec = match.group(3) or ""
            new_spec = rewrite_spec(spec, pkg_version)
            return f"{name}{extras}{new_spec}"
    return dep_str


def tighten_file(pyproject_path: Path, internal_packages: dict):
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    deps_to_replace = {}

    for dep in data.get("project", {}).get("dependencies", []):
        new_dep = process_dependency(dep, internal_packages)
        if new_dep != dep:
            deps_to_replace[dep] = new_dep

    opt_deps = data.get("project", {}).get("optional-dependencies", {})
    for deps in opt_deps.values():
        for dep in deps:
            new_dep = process_dependency(dep, internal_packages)
            if new_dep != dep:
                deps_to_replace[dep] = new_dep

    if not deps_to_replace:
        return False

    content = pyproject_path.read_text(encoding="utf-8")
    new_content = content

    for old_dep, new_dep in deps_to_replace.items():
        # Replace occurrences in quotes safely
        # We assume the string is exact, inside double or single quotes
        new_content = new_content.replace(f'"{old_dep}"', f'"{new_dep}"')
        new_content = new_content.replace(f"'{old_dep}'", f"'{new_dep}'")

    if new_content != content:
        # Create backup
        backup_path = pyproject_path.with_suffix('.toml.bak')
        shutil.copy2(pyproject_path, backup_path)
        pyproject_path.write_text(new_content, encoding="utf-8")
        print(f"Tightened dependencies in {pyproject_path}")
        return True
    return False


def restore_files(internal_packages: dict):
    for pkg_info in internal_packages.values():
        pyproject_path = pkg_info["pyproject_path"]
        backup_path = pyproject_path.with_suffix('.toml.bak')
        if backup_path.exists():
            shutil.copy2(backup_path, pyproject_path)
            backup_path.unlink()
            print(f"Restored {pyproject_path} from backup")


def main():
    parser = argparse.ArgumentParser(description="Tighten internal SemVer dependencies")
    parser.add_argument(
        "--rewrite", action="store_true", help="Rewrite pyproject.toml files to use tight bounds"
    )
    parser.add_argument(
        "--restore", action="store_true", help="Restore pyproject.toml files from backups"
    )

    args = parser.parse_args()

    manifest_path = Path(".release-please-manifest.json")
    if not manifest_path.exists():
        print(f"Manifest not found at {manifest_path}")
        sys.exit(1)

    internal_packages = get_internal_packages(manifest_path)

    if args.restore:
        restore_files(internal_packages)
    elif args.rewrite:
        for pkg_info in internal_packages.values():
            tighten_file(pkg_info["pyproject_path"], internal_packages)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
