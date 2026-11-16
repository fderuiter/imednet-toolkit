#!/usr/bin/env python3
"""Automated showcase verification gate.

Parses abstract syntax trees and validates imports for all example showcases,
and ensures all example paths referenced in documentation literal includes exist on disk.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
from collections.abc import Sequence
from pathlib import Path

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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run showcase and documentation verification checks."""
    args = parse_args(argv)
    repo_root = args.root_dir or Path(__file__).resolve().parent.parent
    examples_dir = args.examples_dir or (repo_root / "examples")
    docs_dir = args.docs_dir or (repo_root / "docs")

    configure_sys_path(repo_root)

    print(f"Validating showcase AST and imports in {examples_dir}...")
    showcase_errors = validate_showcase_ast_and_imports(examples_dir, repo_root)

    print(f"Validating documentation literal includes in {docs_dir}...")
    doc_errors = validate_documentation_literal_includes(docs_dir)

    all_errors = showcase_errors + doc_errors

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
