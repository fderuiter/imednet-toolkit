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
import os
import re
import shutil
import subprocess
import sys
import textwrap
import zipfile
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
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
    "textual",
}

WORKSPACE_PACKAGES = {
    "imednet",
    "imednet_workflows",
    "imednet_sinks",
    "imednet_tui",
    "apache_airflow_providers_imednet",
}

LITERALINCLUDE_PATTERN = re.compile(r"^\s*\.\.\s+literalinclude::\s+(\S+)")
RST_PYTHON_BLOCK_PATTERN = re.compile(
    r"^(\s*)\.\.\s+(?:testcode|code-block)::(?:\s+(python|py))?\s*$"
)


def configure_sys_path(repo_root: Path) -> None:
    """Add workspace package source trees to sys.path so local modules resolve."""
    candidate_dirs = [
        repo_root / "packages" / "core" / "src",
        repo_root / "packages" / "plugins-workflows" / "src",
        repo_root / "packages" / "plugins-sinks" / "src",
        repo_root / "packages" / "plugins-tui" / "src",
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
    if top_level in OPTIONAL_INTEGRATION_MODULES:
        return True, f"optional_dependency_skipped:{mod}"
    return False, f"Missing submodule reference '{mod}'"


def _ast_exports_symbol(spec: ModuleSpec | None, symbol: str) -> bool | None:
    """Fallback AST inspection for symbol presence when optional transitive imports fail."""
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        return None
    origin_path = Path(spec.origin)
    if not origin_path.is_file():
        return None
    try:
        tree = ast.parse(origin_path.read_text(encoding="utf-8"), filename=str(origin_path))
    except (OSError, SyntaxError):
        return None

    defined_names: set[str] = set()
    explicit_all: set[str] | None = None
    has_star_import = False

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined_names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                defined_names.add(alias.asname or alias.name.split(".", maxsplit=1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    has_star_import = True
                else:
                    defined_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined_names.add(target.id)
                    if target.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                        explicit_all = {
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        }
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined_names.add(node.target.id)

    if symbol in defined_names or (explicit_all is not None and symbol in explicit_all):
        return True
    if has_star_import:
        return None
    return False


def is_symbol_available(mod: str, symbol: str) -> tuple[bool, str]:
    """Check whether an imported symbol exists on a resolved module or as a submodule.

    Skips uninstalled optional third-party integration dependencies while enforcing
    symbol existence for workspace packages and available required modules.
    """
    if symbol == "*":
        return is_module_available(mod)

    ok, reason = is_module_available(mod)
    if not ok:
        return False, reason

    top_level = mod.split(".", maxsplit=1)[0]
    if (
        reason.startswith("optional_dependency_skipped:")
        or top_level in OPTIONAL_INTEGRATION_MODULES
    ):
        return True, f"optional_dependency_skipped:{top_level}"

    # Check if mod.symbol is a resolvable submodule
    with contextlib.suppress(ModuleNotFoundError, ValueError, AttributeError, ImportError):
        if importlib.util.find_spec(f"{mod}.{symbol}") is not None:
            return True, "resolved_submodule"

    try:
        module_obj = importlib.import_module(mod)
    except ModuleNotFoundError as exc:
        missing_root = (exc.name or "").split(".", maxsplit=1)[0]
        if missing_root in OPTIONAL_INTEGRATION_MODULES:
            spec = None
            with contextlib.suppress(ModuleNotFoundError, ValueError, AttributeError, ImportError):
                spec = importlib.util.find_spec(mod)
            ast_result = _ast_exports_symbol(spec, symbol)
            if ast_result is False:
                return False, f"Missing symbol '{symbol}' in module '{mod}'"
            return True, f"optional_dependency_skipped:{missing_root}"
        return False, f"Failed to import module '{mod}' while checking symbol '{symbol}': {exc}"
    except ImportError as exc:
        return False, f"Failed to import module '{mod}' while checking symbol '{symbol}': {exc}"

    if hasattr(module_obj, symbol):
        return True, "resolved"

    return False, f"Missing symbol '{symbol}' in module '{mod}'"


def validate_ast_imports(
    tree: ast.AST,
    *,
    workspace_only: bool = False,
) -> list[tuple[int, str]]:
    """Validate module and symbol imports in an AST, returning sorted (lineno, error) pairs."""
    diagnostics: list[tuple[int, str]] = []
    nodes_with_lines = [
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    nodes_with_lines.sort(key=lambda n: getattr(n, "lineno", 0))

    for node in nodes_with_lines:
        lineno = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level = alias.name.split(".", maxsplit=1)[0]
                if workspace_only and top_level not in WORKSPACE_PACKAGES:
                    continue
                ok, reason = is_module_available(alias.name)
                if not ok:
                    diagnostics.append((lineno, reason))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level = node.module.split(".", maxsplit=1)[0]
            if workspace_only and top_level not in WORKSPACE_PACKAGES:
                continue
            ok, reason = is_module_available(node.module)
            if not ok:
                diagnostics.append((lineno, reason))
                continue
            if reason.startswith("optional_dependency_skipped:"):
                continue
            for alias in node.names:
                sym_ok, sym_reason = is_symbol_available(node.module, alias.name)
                if not sym_ok:
                    diagnostics.append((lineno, sym_reason))

    return diagnostics


def validate_showcase_ast_and_imports(
    examples_dir: Path, repo_root: Path | None = None
) -> list[str]:
    """Parse AST and verify module and symbol imports for all Python files under examples_dir."""
    errors: list[str] = []
    if not examples_dir.exists():
        return [f"Examples directory not found: {examples_dir}"]

    configure_sys_path(Path(__file__).resolve().parent.parent)
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

        for lineno, reason in validate_ast_imports(tree):
            errors.append(f"{py_file}:{lineno}: {reason}")

    return errors


def extract_literalinclude_slice(
    target_content: str,
    options: dict[str, str],
) -> tuple[str, bool, list[str]]:
    """Extract code slice from target content based on Sphinx literalinclude options.

    Args:
        target_content: The full content of the target file.
        options: Dictionary of directive options (e.g. start-after, end-before).

    Returns:
        tuple[str, bool, list[str]]: (sliced_content, is_sliced, error_messages)
    """
    errors: list[str] = []
    lines = target_content.splitlines(keepends=True)
    is_sliced = False

    if "start-after" in options and "start-at" in options:
        errors.append("Cannot use both 'start-after' and 'start-at' options")
    elif "start-after" in options:
        is_sliced = True
        pattern = options["start-after"]
        found = False
        for idx, line in enumerate(lines):
            if pattern in line:
                lines = lines[idx + 1 :]
                found = True
                break
        if not found:
            errors.append(f"start-after pattern '{pattern}' not found")

    elif "start-at" in options:
        is_sliced = True
        pattern = options["start-at"]
        found = False
        for idx, line in enumerate(lines):
            if pattern in line:
                lines = lines[idx:]
                found = True
                break
        if not found:
            errors.append(f"start-at pattern '{pattern}' not found")

    if "end-before" in options and "end-at" in options:
        errors.append("Cannot use both 'end-before' and 'end-at' options")
    elif "end-before" in options:
        is_sliced = True
        pattern = options["end-before"]
        found = False
        for idx, line in enumerate(lines):
            if idx > 0 and pattern in line:
                lines = lines[:idx]
                found = True
                break
        if not found:
            errors.append(f"end-before pattern '{pattern}' not found")

    elif "end-at" in options:
        is_sliced = True
        pattern = options["end-at"]
        found = False
        for idx, line in enumerate(lines):
            if pattern in line:
                lines = lines[: idx + 1]
                found = True
                break
        if not found:
            errors.append(f"end-at pattern '{pattern}' not found")

    if "lines" in options:
        is_sliced = True
        linespec = options["lines"]
        try:
            selected_indices: list[int] = []
            for raw_item in linespec.split(","):
                item = raw_item.strip()
                if not item:
                    continue
                if "-" in item:
                    if item.count("-") > 1:
                        raise ValueError(f"invalid range '{item}'")
                    start_str, end_str = item.split("-", 1)
                    if not start_str.strip() and not end_str.strip():
                        raise ValueError("empty range '-'")
                    s = int(start_str) if start_str.strip() else 1
                    e = int(end_str) if end_str.strip() else len(lines)
                    if s > e:
                        raise ValueError(f"invalid range '{s}-{e}': start exceeds end")
                    for i in range(s - 1, min(e, len(lines))):
                        if i >= 0 and i not in selected_indices:
                            selected_indices.append(i)
                else:
                    i = int(item) - 1
                    if 0 <= i < len(lines) and i not in selected_indices:
                        selected_indices.append(i)
            if not selected_indices and lines:
                errors.append(f"Line specification '{linespec}' pulled no lines from file")
            lines = [lines[i] for i in selected_indices]
        except ValueError as exc:
            errors.append(f"Invalid lines specification '{linespec}': {exc}")

    if "dedent" in options:
        is_sliced = True
        dedent_val = options["dedent"]
        if dedent_val:
            try:
                d = int(dedent_val)
                if d < 0:
                    errors.append(
                        f"Invalid dedent specification '{dedent_val}': must be non-negative integer"
                    )
                else:
                    new_lines: list[str] = []
                    for line in lines:
                        new_line = line[d:]
                        if line.endswith("\n") and not new_line:
                            new_line = "\n"
                        new_lines.append(new_line)
                    lines = new_lines
            except ValueError:
                errors.append(f"Invalid dedent specification '{dedent_val}': must be an integer")
        else:
            lines = textwrap.dedent("".join(lines)).splitlines(keepends=True)

    sliced_content = "".join(lines)
    if not errors and not sliced_content.strip():
        errors.append("Extracted literalinclude slice is empty")

    return sliced_content, is_sliced, errors


def validate_python_syntax(
    slice_text: str,
    target_name: str,
) -> tuple[ast.AST | None, list[str]]:
    """Parse extracted code slice as AST and verify Python syntax validity.

    All Python files and slices referenced in documentation must be syntactically valid Python.

    Args:
        slice_text: Code string extracted from the target file.
        target_name: Name or path of the target file being validated.

    Returns:
        tuple[ast.AST | None, list[str]]: (Parsed AST tree if successful, error messages)
    """
    errors: list[str] = []
    dedented = textwrap.dedent(slice_text)

    try:
        tree = ast.parse(dedented, filename=target_name)
        return tree, errors
    except SyntaxError as exc:
        return None, [
            f"Sliced code from '{target_name}' has syntax error: {exc.msg} (line {exc.lineno})"
        ]


def validate_quick_start_ast_structure(
    tree: ast.AST,
    target_name: str,
    is_sliced: bool = True,
) -> list[str]:
    """Verify structural completeness for quick-start showcases: executable calls and function definitions.

    Where the stronger requirement applies:
    This stronger requirement applies specifically to quick-start showcase tutorials (e.g.
    `docs/tutorials/quick_start.rst` and files targeting quick-start examples). Unlike general
    documentation snippets which may be partial variable assignments or class stubs, quick-start
    showcase slices must demonstrate a complete, executable workflow with function definitions
    and call invocations to avoid documentation truncation.

    Args:
        tree: Parsed AST tree of the slice.
        target_name: Name or path of the target file being validated.
        is_sliced: True if slice boundaries (markers or line limits) were applied.

    Returns:
        list[str]: Validation error messages, or an empty list if valid.
    """
    errors: list[str] = []
    call_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    if not call_nodes:
        errors.append(f"Extracted slice from '{target_name}' contains zero executable call nodes")

    func_nodes = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if is_sliced and not func_nodes:
        errors.append(
            f"Extracted slice from '{target_name}' contains zero executable function definitions"
        )

    return errors


def validate_python_slice(
    slice_text: str,
    target_name: str,
    is_sliced: bool = False,
    *,
    is_quick_start: bool = False,
) -> list[str]:
    """Parse extracted slice as AST and verify syntax, with optional quick-start structural checks.

    Args:
        slice_text: Code string extracted from the target file.
        target_name: Name or path of the target file being validated.
        is_sliced: True if slice boundaries (markers or line limits) were applied.
        is_quick_start: If True, enforce stronger quick-start requirements
            (executable function definitions and call nodes). General Python documentation
            snippets only require valid syntax.

    Returns:
        list[str]: Validation error messages, or an empty list if valid.
    """
    tree, errors = validate_python_syntax(slice_text, target_name)
    if errors or tree is None:
        return errors

    for slice_lineno, reason in validate_ast_imports(tree):
        errors.append(f"Sliced code from '{target_name}' (line {slice_lineno}): {reason}")

    if is_quick_start:
        errors.extend(validate_quick_start_ast_structure(tree, target_name, is_sliced=is_sliced))

    return errors


def _validate_inline_rst_python_blocks(doc_file: Path, doc_lines: list[str]) -> list[str]:
    """Validate workspace symbol imports inside inline reStructuredText Python blocks."""
    errors: list[str] = []
    idx = 0
    total = len(doc_lines)

    while idx < total:
        line = doc_lines[idx]
        m = RST_PYTHON_BLOCK_PATTERN.match(line)
        if not m:
            idx += 1
            continue

        is_testcode = ".. testcode::" in line
        lang = m.group(2)
        if not is_testcode and lang not in ("python", "py"):
            idx += 1
            continue

        directive_indent = len(m.group(1))
        directive_lineno = idx + 1
        idx += 1

        # Skip directive options and leading blank lines
        while idx < total:
            curr = doc_lines[idx]
            stripped = curr.strip()
            curr_indent = len(curr) - len(curr.lstrip())
            if not stripped:
                idx += 1
                continue
            if curr_indent > directive_indent and stripped.startswith(":"):
                idx += 1
                continue
            break

        block_start_idx = idx
        block_lines: list[str] = []
        content_indent: int | None = None
        while idx < total:
            curr = doc_lines[idx]
            if not curr.strip():
                block_lines.append("")
                idx += 1
                continue
            curr_indent = len(curr) - len(curr.lstrip())
            if curr_indent <= directive_indent:
                break
            if content_indent is None:
                content_indent = curr_indent
            block_lines.append(
                curr[content_indent:] if curr_indent >= content_indent else curr.lstrip()
            )
            idx += 1

        snippet = "\n".join(block_lines)
        if not snippet.strip():
            continue

        try:
            tree = ast.parse(textwrap.dedent(snippet), filename=str(doc_file))
        except SyntaxError as exc:
            if is_testcode:
                line_offset = (exc.lineno or 1) - 1
                errors.append(
                    f"{doc_file}:{block_start_idx + 1 + line_offset}: "
                    f"Inline testcode syntax error: {exc.msg}"
                )
            continue

        for rel_lineno, reason in validate_ast_imports(tree, workspace_only=True):
            abs_lineno = block_start_idx + max(rel_lineno, 1)
            errors.append(
                f"{doc_file}:{abs_lineno}: {reason} (in directive at line {directive_lineno})"
            )

    return errors


def validate_documentation_literal_includes(
    docs_dir: Path, repo_root: Path | None = None
) -> list[str]:
    """Verify all documentation literal includes exist on disk and extracted slices parse as valid AST.

    Slicing semantics mirror Sphinx's `literalinclude` directive (supporting `start-after`,
    `start-at`, `end-before`, `end-at`, `lines`, and `dedent`).

    Python validation applies at two tiers:
    1. Syntax validity: All included Python slices across all documentation pages must parse
       as syntactically valid Python code without syntax errors.
    2. Quick-start showcase completeness: Quick-start tutorials (`quick_start.rst` and
       quick-start targets) are additionally verified to contain executable function
       definitions and call invocations, preventing showcase truncation.
    """
    errors: list[str] = []
    if not docs_dir.exists():
        return [f"Documentation directory not found: {docs_dir}"]

    configure_sys_path(Path(__file__).resolve().parent.parent)
    if repo_root is not None:
        configure_sys_path(repo_root)

    excluded_doc_dirs = {"_build", "agents", "superpowers"}
    doc_files = sorted(
        p
        for p in docs_dir.rglob("*")
        if p.is_file()
        and not (excluded_doc_dirs & set(p.relative_to(docs_dir).parts))
        and p.suffix in {".rst", ".md", ".txt"}
    )

    for doc_file in doc_files:
        try:
            content = doc_file.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(f"{doc_file}: Failed to read file: {exc}")
            continue

        doc_lines = content.splitlines()
        for line_idx, line in enumerate(doc_lines):
            match = LITERALINCLUDE_PATTERN.match(line)
            if not match:
                continue

            lineno = line_idx + 1
            target = match.group(1).strip("'\"")

            # Parse directive options indented below the literalinclude directive
            options: dict[str, str] = {}
            idx = line_idx + 1
            while idx < len(doc_lines):
                curr = doc_lines[idx]
                opt_match = re.match(r"^\s+:([a-zA-Z0-9_-]+):(?:\s+(.*))?$", curr)
                if opt_match:
                    opt_name = opt_match.group(1)
                    opt_val = (opt_match.group(2) or "").strip()
                    options[opt_name] = opt_val
                    idx += 1
                elif not curr.strip():
                    has_next_opt = False
                    for next_idx in range(idx + 1, len(doc_lines)):
                        if not doc_lines[next_idx].strip():
                            continue
                        if re.match(r"^\s+:([a-zA-Z0-9_-]+):", doc_lines[next_idx]):
                            has_next_opt = True
                        break
                    if has_next_opt:
                        idx += 1
                    else:
                        break
                else:
                    break

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
                continue

            try:
                target_content = resolved.read_text(encoding="utf-8")
            except Exception as exc:
                errors.append(
                    f"{doc_file}:{lineno}: Failed to read referenced file '{target}': {exc}"
                )
                continue

            slice_text, is_sliced, slice_errs = extract_literalinclude_slice(
                target_content, options
            )
            for s_err in slice_errs:
                errors.append(f"{doc_file}:{lineno}: {s_err} in '{resolved}'")
            if slice_errs:
                continue

            is_python = resolved.suffix == ".py" or options.get("language") in ("python", "py")
            if is_python:
                # Stronger structural expectation (executable function definitions and calls)
                # applies specifically to quick-start showcase documents or targets.
                is_quick_start = (
                    "quick_start" in doc_file.name.lower() or "quick_start" in target.lower()
                )
                slice_validation_errs = validate_python_slice(
                    slice_text=slice_text,
                    target_name=target,
                    is_sliced=is_sliced,
                    is_quick_start=is_quick_start,
                )
                for v_err in slice_validation_errs:
                    errors.append(f"{doc_file}:{lineno}: {v_err}")

        if doc_file.suffix == ".rst":
            errors.extend(_validate_inline_rst_python_blocks(doc_file, doc_lines))

    return errors


BROWSER_WHEEL_PACKAGES: tuple[str, ...] = (
    "imednet",
    "imednet_workflows",
    "imednet_plugins_sinks",
)

WHEEL_IMPORT_MODULES: dict[str, str] = {
    "imednet": "imednet",
    "imednet_workflows": "imednet_workflows",
    "imednet_plugins_sinks": "imednet_sinks",
}

WHEEL_REQUIRED_ENTRIES: dict[str, tuple[str, ...]] = {
    "imednet": (
        "imednet/__init__.py",
        "imednet/sdk.py",
        "imednet/config.py",
    ),
    "imednet_workflows": (
        "imednet_workflows/__init__.py",
        "imednet_workflows/data_extraction.py",
    ),
    "imednet_plugins_sinks": (
        "imednet_sinks/__init__.py",
        "imednet_sinks/plugin.py",
    ),
}


def validate_staged_wheels(
    wheels_dir: Path,
    expected_packages: Sequence[str] = BROWSER_WHEEL_PACKAGES,
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

    core_wheels = [w for w in wheel_files if w.name.startswith("imednet-")]

    for pkg_name in expected_packages:
        prefix = pkg_name.replace("-", "_")
        import_module = WHEEL_IMPORT_MODULES.get(prefix, prefix)
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
                required_entries = WHEEL_REQUIRED_ENTRIES.get(prefix, ())
                for req in required_entries:
                    if req not in namelist:
                        errors.append(
                            f"Staged wheel '{wheel_path.name}' is missing required module entry '{req}'"
                        )
        except Exception as exc:
            errors.append(f"Failed to inspect wheel archive '{wheel_path.name}': {exc}")
            continue

        # Verify simulated Pyodide import in an isolated subprocess
        # Running in a separate process guarantees true environment isolation and prevents
        # polluting sys.modules or coverage measurement in the host test runner.
        staged_sys_paths = [str(wheel_path.resolve())]
        if prefix != "imednet" and core_wheels:
            staged_sys_paths.append(str(core_wheels[0].resolve()))

        code = (
            "import sys\n"
            "sys.path = [p for p in sys.path if 'packages/core/src' not in p "
            "and 'packages/plugins' not in p and 'packages/providers' not in p]\n"
            f"sys.path[:0] = {staged_sys_paths!r}\n"
            f"import {import_module}\n"
        )
        if prefix == "imednet":
            code += f"assert hasattr({import_module}, 'ImednetSDK'), 'ImednetSDK not found'\n"
        elif prefix == "imednet_plugins_sinks":
            code += (
                "import imednet_sinks.plugin\n"
                "assert hasattr(imednet_sinks.plugin, 'create_sinks'), 'create_sinks not found'\n"
            )
        elif prefix == "imednet_workflows":
            code += (
                "import imednet_workflows.data_extraction\n"
                "assert hasattr(imednet_workflows.data_extraction, 'DataExtractionWorkflow'), "
                "'DataExtractionWorkflow not found'\n"
            )

        clean_env = {
            k: v
            for k, v in os.environ.items()
            if not (
                k.upper().startswith(("COV_", "COVERAGE_"))
                or "cov_core" in k.lower()
                or "coverage" in k.lower()
            )
        }

        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=clean_env,
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or proc.stdout.strip()
            errors.append(f"Simulated Pyodide import failed for '{wheel_path.name}': {err_msg}")

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

    excluded_patterns = (
        "imednet_workspace-*.whl",
        "apache_airflow_providers_imednet-*.whl",
        "imednet_tui-*.whl",
    )
    for pattern in excluded_patterns:
        for excluded_whl in wheels_dir.glob(pattern):
            with contextlib.suppress(OSError):
                excluded_whl.unlink()

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
    doc_errors = validate_documentation_literal_includes(docs_dir, repo_root)

    wheel_errors: list[str] = []
    should_check_wheels = not args.skip_wheels and (
        args.wheels_dir is not None
        or (docs_dir / "_static").exists()
        or (repo_root / "packages" / "core").exists()
    )

    if should_check_wheels:
        created_temp_wheels = False
        if (repo_root / "packages" / "core").exists() and not list(wheels_dir.glob("*.whl")):
            print(f"Staged wheels not found in {wheels_dir}, building workspace wheels...")
            created_temp_wheels = not wheels_dir.exists()
            stage_wheels(repo_root, wheels_dir)

        try:
            print(f"Validating staged wheels and simulated Pyodide imports in {wheels_dir}...")
            wheel_errors = validate_staged_wheels(wheels_dir)
        finally:
            if created_temp_wheels and args.wheels_dir is None:
                shutil.rmtree(wheels_dir, ignore_errors=True)

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
