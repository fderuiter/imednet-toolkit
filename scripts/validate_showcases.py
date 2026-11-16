#!/usr/bin/env python3
"""Automated showcase verification gate.

Parses abstract syntax trees and validates imports for all example showcases,
and ensures all example paths referenced in documentation literal includes exist on disk.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Known optional integration dependencies that may not be installed in all environments
OPTIONAL_INTEGRATION_MODULES = {
    "airflow",
    "apache_airflow",
    "dotenv",
    "snowflake",
    "neo4j",
    "pymongo",
    "duckdb",
    "pyarrow",
    "pandas",
    "sqlalchemy",
    "openpyxl",
    "matplotlib",
    "opentelemetry",
}

LITERALINCLUDE_PATTERN = re.compile(r"^\s*\.\.\s+literalinclude::\s+(\S+)")


def configure_sys_path(repo_root: Path) -> None:
    """Add workspace package source trees to sys.path so local modules resolve."""
    candidate_dirs = [
        repo_root / "packages" / "core" / "src",
        repo_root / "packages" / "plugins-workflows" / "src",
        repo_root / "packages" / "plugins-sinks" / "src",
        repo_root / "packages" / "providers-airflow" / "src",
    ]
    for p in candidate_dirs:
        resolved = str(p.resolve())
        if p.is_dir() and resolved not in sys.path:
            sys.path.insert(0, resolved)


def is_module_available(mod: str) -> tuple[bool, str]:
    """Check whether an imported module exists or is an optional integration dependency.

    Returns:
        tuple[bool, str]: (is_valid, reason_or_status)
    """
    top_level = mod.split(".", maxsplit=1)[0]

    # Check if the full module can be resolved directly
    try:
        spec = importlib.util.find_spec(mod)
        if spec is not None:
            return True, "resolved"
    except (ModuleNotFoundError, ValueError, AttributeError, ImportError):
        pass

    # Check if the top-level module is resolvable
    try:
        top_spec = importlib.util.find_spec(top_level)
    except (ModuleNotFoundError, ValueError, AttributeError, ImportError):
        top_spec = None

    if top_spec is None:
        if top_level in OPTIONAL_INTEGRATION_MODULES:
            return True, f"optional_dependency_skipped:{top_level}"
        return False, f"Missing required module reference '{top_level}'"

    # Top-level module exists, but requested submodule could not be resolved
    return False, f"Missing submodule reference '{mod}'"


def validate_showcase_ast_and_imports(
    examples_dir: Path, repo_root: Path | None = None
) -> list[str]:
    """Parse AST and verify imports for all Python files under examples_dir."""
    errors: list[str] = []
    if not examples_dir.exists():
        return [f"Examples directory not found: {examples_dir}"]

    if repo_root is not None:
        configure_sys_path(repo_root)

    py_files = sorted(examples_dir.rglob("*.py"))
    if not py_files:
        return [f"No Python showcase files found under {examples_dir}"]

    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(f"{py_file}: Failed to read file: {exc}")
            continue

        try:
            tree = ast.parse(content, filename=str(py_file))
        except SyntaxError as exc:
            errors.append(f"{py_file}:{exc.lineno}: Syntax error: {exc.msg}")
            continue

        for node in ast.walk(tree):
            modules_to_check: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules_to_check.append((alias.name, getattr(node, "lineno", 0)))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules_to_check.append((node.module, getattr(node, "lineno", 0)))

            for mod_name, lineno in modules_to_check:
                ok, reason = is_module_available(mod_name)
                if not ok:
                    errors.append(f"{py_file}:{lineno}: {reason}")

    return errors


def validate_documentation_literal_includes(docs_dir: Path) -> list[str]:
    """Verify that all example paths referenced in documentation literal includes exist on disk."""
    errors: list[str] = []
    if not docs_dir.exists():
        return [f"Documentation directory not found: {docs_dir}"]

    doc_files = sorted(
        p
        for p in docs_dir.rglob("*")
        if p.is_file() and "_build" not in p.parts and p.suffix in {".rst", ".md", ".txt"}
    )

    for doc_file in doc_files:
        try:
            content = doc_file.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(f"{doc_file}: Failed to read file: {exc}")
            continue

        for lineno, line in enumerate(content.splitlines(), 1):
            match = LITERALINCLUDE_PATTERN.match(line)
            if not match:
                continue

            target = match.group(1).strip("'\"")

            if target.startswith("/"):
                # Path starting with / is relative to the Sphinx documentation root directory
                resolved = (docs_dir / target.lstrip("/")).resolve()
            else:
                # Path without leading / is relative to the directory of the current file
                resolved = (doc_file.parent / target).resolve()

            if not resolved.exists():
                errors.append(
                    f"{doc_file}:{lineno}: Referenced literalinclude target not found on disk: "
                    f"'{target}' (resolved to '{resolved}')"
                )

    return errors


def validate_staged_wheels(
    wheels_dir: Path,
    expected_packages: Sequence[str] = ("imednet",),
) -> list[str]:
    """Verify that staged wheels exist, contain required modules, and can be imported in simulated Pyodide.

    Args:
        wheels_dir: Path to directory containing staged wheel artifacts (*.whl).
        expected_packages: Names of packages expected to have staged wheels.

    Returns:
        list[str]: Validation error messages, or an empty list if valid.
    """
    errors: list[str] = []
    if not wheels_dir.exists():
        return [f"Staged wheels directory not found: {wheels_dir}"]

    wheel_files = sorted(wheels_dir.glob("*.whl"))
    if not wheel_files:
        return [f"No staged wheel artifacts (*.whl) found in {wheels_dir}"]

    for pkg_name in expected_packages:
        prefix = pkg_name.replace("-", "_")
        matching = [
            w
            for w in wheel_files
            if w.name.startswith(f"{prefix}-") or w.name.startswith(f"{pkg_name}-")
        ]
        if not matching:
            errors.append(
                f"Missing expected staged wheel artifact for package '{pkg_name}' in {wheels_dir}"
            )
            continue

        wheel_path = matching[0]

        # Verify wheel content archive has required modules
        try:
            with zipfile.ZipFile(wheel_path, "r") as zf:
                namelist = set(zf.namelist())
                if pkg_name == "imednet":
                    required_entries = [
                        "imednet/__init__.py",
                        "imednet/sdk.py",
                        "imednet/config.py",
                    ]
                    for req in required_entries:
                        if req not in namelist:
                            errors.append(
                                f"Staged wheel '{wheel_path.name}' is missing required module entry '{req}'"
                            )
        except Exception as exc:
            errors.append(f"Failed to inspect wheel archive '{wheel_path.name}': {exc}")
            continue

        # Verify simulated Pyodide import
        orig_sys_path = list(sys.path)
        isolated_sys_path = [
            p
            for p in orig_sys_path
            if "packages/core/src" not in p
            and "packages/plugins" not in p
            and "packages/providers" not in p
        ]
        isolated_sys_path.insert(0, str(wheel_path.resolve()))

        saved_modules: dict[str, Any] = {}
        for mod_key in list(sys.modules.keys()):
            if mod_key == prefix or mod_key.startswith(f"{prefix}."):
                saved_modules[mod_key] = sys.modules.pop(mod_key)

        try:
            sys.path = isolated_sys_path
            imported_mod = importlib.import_module(prefix)
            if pkg_name == "imednet":
                sdk_cls = getattr(imported_mod, "ImednetSDK", None)
                if sdk_cls is None:
                    errors.append(
                        f"ImednetSDK not found in imported module '{prefix}' from {wheel_path.name}"
                    )
        except Exception as exc:
            errors.append(f"Simulated Pyodide import failed for '{wheel_path.name}': {exc}")
        finally:
            sys.path = orig_sys_path
            for mod_key in list(sys.modules.keys()):
                if mod_key == prefix or mod_key.startswith(f"{prefix}."):
                    sys.modules.pop(mod_key, None)
            sys.modules.update(saved_modules)

    return errors


def stage_wheels(repo_root: Path, wheels_dir: Path | None = None) -> list[Path]:
    """Build and stage pure Python wheels for workspace packages."""
    if wheels_dir is None:
        wheels_dir = repo_root / "docs" / "_static" / "wheels"

    wheels_dir.mkdir(parents=True, exist_ok=True)
    uv_bin = shutil.which("uv") or "uv"
    build_cmd = [
        uv_bin,
        "build",
        "--all-packages",
        "--wheel",
        "--out-dir",
        str(wheels_dir),
    ]

    try:
        subprocess.run(build_cmd + ["--offline"], check=True, cwd=str(repo_root))
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(build_cmd, check=True, cwd=str(repo_root))
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"Warning: Failed to stage wheels: {exc}")

    for placeholder in wheels_dir.glob("imednet_workspace-*.whl"):
        with contextlib.suppress(OSError):
            placeholder.unlink()

    return sorted(wheels_dir.glob("*.whl"))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate showcase examples and documentation references."
    )
    parser.add_argument(
        "--examples-dir",
        type=Path,
        default=None,
        help="Path to examples directory (defaults to <repo_root>/examples)",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=None,
        help="Path to documentation directory (defaults to <repo_root>/docs)",
    )
    parser.add_argument(
        "--root-dir",
        type=Path,
        default=None,
        help="Path to repository root (defaults to repository root containing scripts/)",
    )
    parser.add_argument(
        "--wheels-dir",
        type=Path,
        default=None,
        help="Path to staged wheels directory (defaults to <docs_dir>/_static/wheels)",
    )
    parser.add_argument(
        "--skip-wheels",
        action="store_true",
        help="Skip staged wheels validation gate",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run showcase and documentation verification checks."""
    args = parse_args(argv)
    repo_root = args.root_dir or Path(__file__).resolve().parent.parent
    examples_dir = args.examples_dir or (repo_root / "examples")
    docs_dir = args.docs_dir or (repo_root / "docs")
    wheels_dir = args.wheels_dir or (docs_dir / "_static" / "wheels")

    configure_sys_path(repo_root)

    print(f"Validating showcase AST and imports in {examples_dir}...")
    showcase_errors = validate_showcase_ast_and_imports(examples_dir, repo_root)

    print(f"Validating documentation literal includes in {docs_dir}...")
    doc_errors = validate_documentation_literal_includes(docs_dir)

    wheel_errors: list[str] = []
    should_check_wheels = not args.skip_wheels and (
        args.wheels_dir is not None
        or (docs_dir / "_static").exists()
        or (repo_root / "packages" / "core").exists()
    )

    if should_check_wheels:
        if (repo_root / "packages" / "core").exists() and not list(wheels_dir.glob("*.whl")):
            print(f"Staged wheels not found in {wheels_dir}, building workspace wheels...")
            stage_wheels(repo_root, wheels_dir)

        print(f"Validating staged wheels and simulated Pyodide imports in {wheels_dir}...")
        wheel_errors = validate_staged_wheels(wheels_dir)

    all_errors = showcase_errors + doc_errors + wheel_errors

    if all_errors:
        print(f"\nShowcase verification failed with {len(all_errors)} error(s):", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    py_count = len(list(examples_dir.rglob("*.py"))) if examples_dir.exists() else 0
    print(
        f"\nShowcase verification passed successfully ({py_count} Python showcase files verified)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
