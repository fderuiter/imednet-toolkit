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
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import zipfile
from collections.abc import Iterator, Sequence
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
    r"^(\s*)\.\.\s+(?:testcode|code-block|sourcecode)::(?:\s+(python|py))?\s*$"
)
MD_PYTHON_FENCE_OPEN_PATTERN = re.compile(r"^(\s*)```(?:python|py)\s*$")
MD_PYTHON_FENCE_CLOSE_PATTERN = re.compile(r"^\s*```\s*$")


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


def _reset_orphaned_airflow_modules() -> None:
    """Purge partial airflow.* entries left in sys.modules by mocked unit tests."""
    airflow_mod = sys.modules.get("airflow")
    utils_mod = sys.modules.get("airflow.utils")
    if (
        airflow_mod is None
        or getattr(airflow_mod, "__file__", None) is None
        or utils_mod is None
        or getattr(utils_mod, "__file__", None) is None
    ) and any(name == "airflow" or name.startswith("airflow.") for name in sys.modules):
        for name in list(sys.modules):
            if name == "airflow" or name.startswith(
                ("airflow.", "apache_airflow_providers_imednet")
            ):
                sys.modules.pop(name, None)


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

    if top_level in ("airflow", "apache_airflow_providers_imednet"):
        _reset_orphaned_airflow_modules()

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


def validate_ast_docstrings(tree: ast.AST) -> list[tuple[int, str]]:
    """Inspect module, class, and function docstrings for leftover 'TODO' placeholders."""
    diagnostics: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and "TODO" in doc:
                lineno = (
                    node.body[0].lineno
                    if getattr(node, "body", None)
                    else getattr(node, "lineno", 1)
                )
                target_name = getattr(node, "name", "<module>")
                diagnostics.append(
                    (lineno, f"Docstring for '{target_name}' contains 'TODO' placeholder")
                )
    diagnostics.sort(key=lambda item: item[0])
    return diagnostics


def _is_workflow_showcase(py_file: Path, examples_dir: Path) -> bool:
    """Return True if py_file is a top-level workflow_*.py showcase or inside examples/workflows/."""
    if py_file.name.startswith("workflow_"):
        return True
    with contextlib.suppress(ValueError):
        rel_parts = py_file.resolve().relative_to(examples_dir.resolve()).parts
        return "workflows" in rel_parts
    return False


def validate_ast_workflow_bindings(tree: ast.AST) -> list[tuple[int, str]]:
    """Reject discarded workflow or record-set constructor calls in workflow showcases."""
    diagnostics: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        callee_name: str | None = None
        if isinstance(func, ast.Name):
            callee_name = func.id
        elif isinstance(func, ast.Attribute):
            callee_name = func.attr
        if callee_name and (
            callee_name.endswith(("Workflow", "RecordSet")) or callee_name == "ImednetSDK"
        ):
            diagnostics.append(
                (
                    getattr(node, "lineno", 1),
                    f"Unassigned '{callee_name}(...)' call discards its instance without executing workflow actions",
                )
            )
    diagnostics.sort(key=lambda item: item[0])
    return diagnostics


def _is_empty_str_default_expr(expr: ast.expr) -> bool:
    """Return True when expr is '' or os.environ.get(..., '') / os.getenv(..., '')."""
    if isinstance(expr, ast.Constant) and expr.value == "":
        return True
    if isinstance(expr, ast.Call):
        func = expr.func
        is_env_get = (
            (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
            )
            or (isinstance(func, ast.Name) and func.id == "getenv")
            or (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
        )
        if is_env_get and len(expr.args) >= 2:
            default_arg = expr.args[1]
            if isinstance(default_arg, ast.Constant) and default_arg.value == "":
                return True
    return False


def validate_ast_sdk_credentials(tree: ast.AST) -> list[tuple[int, str]]:
    """Verify ImednetSDK / AsyncImednetSDK calls pass security_key whenever api_key is provided outside simulation_mode=True."""
    diagnostics: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee_name: str | None = None
        if isinstance(func, ast.Name) and func.id in ("ImednetSDK", "AsyncImednetSDK"):
            callee_name = func.id
        elif isinstance(func, ast.Attribute) and func.attr in ("ImednetSDK", "AsyncImednetSDK"):
            callee_name = func.attr
        if callee_name is None:
            continue

        if any(kw.arg is None for kw in node.keywords):
            continue
        kwargs_map = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        sim_expr = kwargs_map.get("simulation_mode")
        is_explicit_sim = isinstance(sim_expr, ast.Constant) and sim_expr.value is True
        has_alt_auth = any(
            key in kwargs_map for key in ("oidc_token", "config", "credential_source")
        )

        if "api_key" in kwargs_map and not is_explicit_sim and not has_alt_auth:
            if "security_key" not in kwargs_map:
                diagnostics.append(
                    (
                        getattr(node, "lineno", 1),
                        f"{callee_name}(...) passes 'api_key' without 'security_key' outside simulation_mode=True",
                    )
                )
            elif _is_empty_str_default_expr(kwargs_map["security_key"]):
                diagnostics.append(
                    (
                        getattr(node, "lineno", 1),
                        f"{callee_name}(...) defaults 'security_key' to an empty string; 'security_key' is required outside simulation mode",
                    )
                )
        elif (
            "security_key" in kwargs_map
            and not is_explicit_sim
            and _is_empty_str_default_expr(kwargs_map["security_key"])
        ):
            diagnostics.append(
                (
                    getattr(node, "lineno", 1),
                    f"{callee_name}(...) defaults 'security_key' to an empty string; 'security_key' is required outside simulation mode",
                )
            )

    diagnostics.sort(key=lambda item: item[0])
    return diagnostics


def validate_showcase_ast_and_imports(
    examples_dir: Path, repo_root: Path | None = None
) -> list[str]:
    """Parse AST and verify docstrings, modules, and symbol imports for Python files under examples_dir."""
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

        for lineno, reason in validate_ast_docstrings(tree):
            errors.append(f"{py_file}:{lineno}: {reason}")

        for lineno, reason in validate_ast_imports(tree):
            errors.append(f"{py_file}:{lineno}: {reason}")

        for lineno, reason in validate_ast_sdk_credentials(tree):
            errors.append(f"{py_file}:{lineno}: {reason}")

        if _is_workflow_showcase(py_file, examples_dir):
            for lineno, reason in validate_ast_workflow_bindings(tree):
                errors.append(f"{py_file}:{lineno}: {reason}")

    return errors


_SIMULATION_SITECUSTOMIZE = """\
import os
import sys
import types
from unittest.mock import MagicMock

if os.environ.get("IMEDNET_STUB_SINKS") == "1":
    try:
        import pymongo
    except ImportError:
        pymongo = types.ModuleType("pymongo")
        pymongo.UpdateOne = MagicMock()
        sys.modules["pymongo"] = pymongo
    pymongo.MongoClient = MagicMock(name="StubMongoClient")

    try:
        import neo4j
    except ImportError:
        neo4j = types.ModuleType("neo4j")
        sys.modules["neo4j"] = neo4j
    neo4j.GraphDatabase = MagicMock(name="StubGraphDatabase")

    try:
        import snowflake.connector  # noqa: F401
    except ImportError:
        sf_pkg = sys.modules.setdefault("snowflake", types.ModuleType("snowflake"))
        sf_conn = types.ModuleType("snowflake.connector")
        sf_pkg.connector = sf_conn
        sys.modules["snowflake.connector"] = sf_conn
    sys.modules["snowflake.connector"].connect = MagicMock(name="StubSnowflakeConnect")
"""

_RUNTIME_ERROR_LINE_PATTERN = re.compile(
    r"^\s*(?:Error:|ERROR:|CRITICAL:|FATAL:|An error occurred:|Unexpected error:|"
    r"Validation Error:|API Error(?:\s*\(|:)|Failed to\b)"
)
_JSON_ERROR_LOG_PATTERN = re.compile(r'"level(?:name)?"\s*:\s*"(?:ERROR|CRITICAL|FATAL)"')


def _find_output_error(stdout: str, stderr: str) -> str | None:
    """Detect Python tracebacks or error messages in showcase stdout/stderr."""
    combined = f"{stdout}\n{stderr}"
    if "Traceback (most recent call last):" in combined:
        return "Traceback detected in output"
    for line in combined.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _JSON_ERROR_LOG_PATTERN.search(stripped):
            return f"Error log emitted: {stripped}"
        if _RUNTIME_ERROR_LINE_PATTERN.match(stripped):
            return f"Error message emitted: {stripped}"
    return None


def execute_showcases_in_simulation(
    examples_dir: Path,
    repo_root: Path | None = None,
    timeout_seconds: float = 60.0,
) -> list[str]:
    """Execute all Python showcase scripts under IMEDNET_SIMULATION_MODE=true in isolated temp dirs."""
    if not examples_dir.exists():
        return [f"Examples directory not found: {examples_dir}"]

    py_files = sorted(examples_dir.rglob("*.py"))
    if not py_files:
        return [f"No Python showcase files found under {examples_dir}"]

    default_root = Path(__file__).resolve().parent.parent
    roots = [r for r in (repo_root, default_root) if r is not None]
    src_paths: list[str] = []
    for root in roots:
        for pkg_sub in (
            "packages/core/src",
            "packages/plugins-workflows/src",
            "packages/plugins-sinks/src",
            "packages/plugins-tui/src",
            "packages/providers-airflow/src",
        ):
            candidate = (root / pkg_sub).resolve()
            if candidate.is_dir() and str(candidate) not in src_paths:
                src_paths.append(str(candidate))

    stripped_env_keys = {
        "IMEDNET_API_KEY",
        "IMEDNET_SECURITY_KEY",
        "IMEDNET_BASE_URL",
        "IMEDNET_STUDY_KEY",
        "IMEDNET_OUTPUT_DIR",
        "MONGO_URI",
        "NEO4J_URI",
        "NEO4J_USER",
        "NEO4J_PASSWORD",
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_WAREHOUSE",
    }

    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="imednet_showcase_stubs_") as stub_root_str:
        stub_root = Path(stub_root_str)
        (stub_root / "sitecustomize.py").write_text(_SIMULATION_SITECUSTOMIZE, encoding="utf-8")
        airflow_home = stub_root / "airflow_home"
        airflow_home.mkdir(parents=True, exist_ok=True)

        base_env = {
            k: v
            for k, v in os.environ.items()
            if k not in stripped_env_keys
            and not k.upper().startswith(("COV_", "COVERAGE_"))
            and "cov_core" not in k.lower()
        }
        py_path_entries = [str(stub_root), *src_paths]
        if base_env.get("PYTHONPATH"):
            py_path_entries.append(base_env["PYTHONPATH"])
        base_env["PYTHONPATH"] = os.pathsep.join(py_path_entries)
        base_env["IMEDNET_SIMULATION_MODE"] = "true"
        base_env["IMEDNET_STUB_SINKS"] = "1"
        base_env["IMEDNET_NON_INTERACTIVE"] = "1"
        base_env["AIRFLOW_HOME"] = str(airflow_home)

        for py_file in py_files:
            resolved_script = py_file.resolve()
            with tempfile.TemporaryDirectory(prefix="imednet_showcase_cwd_") as run_cwd:
                try:
                    proc = subprocess.run(
                        [sys.executable, str(resolved_script)],
                        cwd=run_cwd,
                        env=base_env,
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired:
                    errors.append(
                        f"{py_file}: Timed out after {timeout_seconds:.1f}s in simulation mode"
                    )
                    continue

                if proc.returncode != 0:
                    detail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
                    tail = detail[-1] if detail else "no output"
                    errors.append(
                        f"{py_file}: Exited with non-zero code {proc.returncode} in simulation mode ({tail})"
                    )
                    continue

                err_reason = _find_output_error(proc.stdout, proc.stderr)
                if err_reason is not None:
                    errors.append(f"{py_file}: {err_reason}")
                    continue

                if _is_workflow_showcase(py_file, examples_dir):
                    combined_output = f"{proc.stdout}\n{proc.stderr}"
                    if "Skipping execution" in combined_output:
                        errors.append(
                            f"{py_file}: Workflow showcase skipped execution instead of running in simulation mode"
                        )
                    elif py_file.name.startswith("workflow_") and not proc.stdout.strip():
                        errors.append(
                            f"{py_file}: Workflow showcase produced empty stdout (silent no-op) in simulation mode"
                        )
                    elif not (proc.stdout.strip() or proc.stderr.strip()):
                        errors.append(
                            f"{py_file}: Workflow showcase produced empty output (silent no-op) in simulation mode"
                        )
                elif "airflow" not in py_file.relative_to(examples_dir).parts:
                    has_file_output = any(
                        p.is_file() and p.stat().st_size > 0 for p in Path(run_cwd).rglob("*")
                    )
                    if not (proc.stdout.strip() or proc.stderr.strip() or has_file_output):
                        errors.append(f"{py_file}: Produced empty output in simulation mode")

    return errors


_AIRFLOW_DAG_VALIDATION_RUNNER = """\
import ast
import importlib.util
import os
import sys
from pathlib import Path

import airflow
from airflow.sdk import DAG as SdkDAG
from apache_airflow_providers_imednet import ImednetExportOperator

dag_classes = tuple({airflow.DAG, SdkDAG})

airflow_dir = Path(sys.argv[1])
export_root = Path(sys.argv[2])
export_root.mkdir(parents=True, exist_ok=True)
os.environ["IMEDNET_EXPORT_ROOT"] = str(export_root)

errors: list[str] = []

for dag_file in sorted(airflow_dir.glob("*.py")):
    source = dag_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(dag_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "schedule_interval":
                    errors.append(
                        f"{dag_file}:{node.lineno}: Deprecated Airflow 2 'schedule_interval' "
                        "argument found; use 'schedule=None' for Airflow 3.x"
                    )

    spec = importlib.util.spec_from_file_location(f"_sim_dag_{dag_file.stem}", dag_file)
    if spec is None or spec.loader is None:
        errors.append(f"{dag_file}: Could not create module spec")
        continue
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        errors.append(f"{dag_file}: Failed to import DAG under Airflow {airflow.__version__}: {exc}")
        continue

    dags = [val for val in vars(module).values() if isinstance(val, dag_classes)]
    if not dags:
        errors.append(f"{dag_file}: Did not define any airflow.DAG object")
        continue

    for dag in dags:
        if getattr(dag, "schedule", "UNSET") is not None:
            errors.append(f"{dag_file}: Expected dag.schedule to be None, got {dag.schedule!r}")

        for task_obj in dag.tasks:
            if isinstance(task_obj, ImednetExportOperator):
                out_csv = export_root / f"{dag.dag_id}_{task_obj.task_id}.csv"
                study_key = "STUDY" if task_obj.study_key in ("STUDY_KEY", "{{ params.study_key }}") else task_obj.study_key
                run_op = ImednetExportOperator(
                    task_id=f"sim_{task_obj.task_id}",
                    study_key=study_key,
                    destination=task_obj.destination,
                    output_path=str(out_csv),
                    export_func=task_obj.export_func,
                    export_kwargs=dict(task_obj.export_kwargs),
                    imednet_conn_id=task_obj.imednet_conn_id,
                )
                try:
                    run_op.execute(context={})
                except Exception as exc:
                    errors.append(
                        f"{dag_file}:{task_obj.task_id}: CSV export failed: {type(exc).__name__}: {exc}"
                    )
                    continue
                if not out_csv.is_file() or out_csv.stat().st_size == 0:
                    errors.append(f"{dag_file}:{task_obj.task_id}: Expected non-empty CSV at {out_csv}")
            elif type(task_obj).__name__ == "MappedOperator" and getattr(task_obj, "operator_class", None) is ImednetExportOperator:
                discover_fn = getattr(module, "discover_export_targets", None)
                raw_fn = getattr(discover_fn, "function", None)
                if callable(raw_fn):
                    try:
                        targets = raw_fn()
                    except Exception as exc:
                        errors.append(f"{dag_file}: Discovery task failed: {type(exc).__name__}: {exc}")
                        continue
                    if not targets:
                        errors.append(f"{dag_file}: Discovery task returned zero targets")
                        continue
                    partial_kw = dict(getattr(task_obj, "partial_kwargs", {}))
                    wrote_non_empty = False
                    for idx, target in enumerate(targets):
                        study_key = target["study_key"]
                        out_csv = Path(target["output_path"])
                        out_csv.parent.mkdir(parents=True, exist_ok=True)
                        run_op = ImednetExportOperator(
                            task_id=f"sim_mapped_{idx}",
                            study_key=study_key,
                            destination=partial_kw.get("destination"),
                            output_path=str(out_csv),
                            export_func=partial_kw.get("export_func"),
                            export_kwargs=dict(target.get("export_kwargs") or {}),
                            imednet_conn_id=partial_kw.get("imednet_conn_id", "imednet_default"),
                        )
                        try:
                            run_op.execute(context={})
                        except Exception as exc:
                            errors.append(
                                f"{dag_file}: Mapped CSV export for '{study_key}' failed: "
                                f"{type(exc).__name__}: {exc}"
                            )
                            break
                        if study_key == "STUDY" and out_csv.is_file() and out_csv.stat().st_size > 0:
                            wrote_non_empty = True
                    if not errors and not wrote_non_empty:
                        errors.append(
                            f"{dag_file}: Mapped CSV export for 'STUDY' did not produce a non-empty file"
                        )

if errors:
    for err in errors:
        print(err, file=sys.stderr)
    sys.exit(1)
"""


def _build_simulation_subprocess_env(repo_root: Path | None = None) -> dict[str, str]:
    """Construct clean offline simulation environment for subprocess validation."""
    default_root = Path(__file__).resolve().parent.parent
    roots = [r for r in (repo_root, default_root) if r is not None]
    src_paths: list[str] = []
    for root in roots:
        for pkg_sub in (
            "packages/core/src",
            "packages/plugins-workflows/src",
            "packages/plugins-sinks/src",
            "packages/plugins-tui/src",
            "packages/providers-airflow/src",
        ):
            candidate = (root / pkg_sub).resolve()
            if candidate.is_dir() and str(candidate) not in src_paths:
                src_paths.append(str(candidate))

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("COV_", "COVERAGE_")) and "cov_core" not in k.lower()
    }
    py_path_entries = list(src_paths)
    if env.get("PYTHONPATH"):
        py_path_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_path_entries)
    env["IMEDNET_SIMULATION_MODE"] = "true"
    env["IMEDNET_NON_INTERACTIVE"] = "1"
    env["AIRFLOW_CONN_IMEDNET_DEFAULT"] = "http://mock:mock@localhost"  # pragma: allowlist secret
    env["AIRFLOW_CONN_MY_IMEDNET_CONN"] = "http://mock:mock@localhost"  # pragma: allowlist secret
    return env


def validate_airflow_dags_in_simulation(
    airflow_examples_dir: Path,
    repo_root: Path | None = None,
    timeout_seconds: float = 60.0,
) -> list[str]:
    """Import Airflow 3.x DAGs and execute CSV export operators against SimulationTransport."""
    if not airflow_examples_dir.exists():
        return []
    if not list(airflow_examples_dir.glob("*.py")):
        return []
    try:
        if importlib.util.find_spec("airflow") is None:
            return []
    except (ModuleNotFoundError, ValueError, ImportError):
        return []

    env = _build_simulation_subprocess_env(repo_root)
    with tempfile.TemporaryDirectory(prefix="imednet_airflow_sim_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        airflow_home = tmp_dir / "airflow_home"
        airflow_home.mkdir(parents=True, exist_ok=True)
        export_root = tmp_dir / "exports"
        env["AIRFLOW_HOME"] = str(airflow_home)

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _AIRFLOW_DAG_VALIDATION_RUNNER,
                    str(airflow_examples_dir.resolve()),
                    str(export_root.resolve()),
                ],
                cwd=str(tmp_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return [
                f"{airflow_examples_dir}: Airflow DAG validation timed out after {timeout_seconds:.1f}s"
            ]

        if proc.returncode != 0:
            lines = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
            if lines:
                return lines
            return [
                f"{airflow_examples_dir}: Airflow DAG validation failed with exit code {proc.returncode}"
            ]

    return []


_MD_PYTHON_BLOCK_RE = re.compile(r"^```(?:python|py)\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def _tree_imports_airflow(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "airflow"
                or alias.name.startswith(("airflow.", "apache_airflow_providers_imednet"))
                for alias in node.names
            ):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (
                node.module == "airflow"
                or node.module.startswith(("airflow.", "apache_airflow_providers_imednet"))
            )
        ):
            return True
    return False


def validate_package_readme_blocks(
    readme_path: Path,
    repo_root: Path | None = None,
    timeout_seconds: float = 60.0,
) -> list[str]:
    """Validate and execute Python blocks in a package README.md without MagicMock or missing security_key."""
    if not readme_path.exists():
        return [f"Package README not found: {readme_path}"]

    try:
        content = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{readme_path}: Failed to read file: {exc}"]

    if "[amazon]" in content:
        return [f"{readme_path}: Contains nonexistent '[amazon]' installation extra"]

    blocks = [m.group(1) for m in _MD_PYTHON_BLOCK_RE.finditer(content)]
    if not blocks:
        return [f"{readme_path}: No Python code blocks found"]

    configure_sys_path(Path(__file__).resolve().parent.parent)
    if repo_root is not None:
        configure_sys_path(repo_root)

    errors: list[str] = []
    parsed_blocks: list[tuple[int, str, ast.Module]] = []
    for idx, raw_block in enumerate(blocks, start=1):
        code = textwrap.dedent(raw_block).strip()
        if "MagicMock" in code:
            errors.append(f"{readme_path} (block {idx}): Disallowed MagicMock usage")
            continue
        try:
            tree = ast.parse(code, filename=f"{readme_path}:block_{idx}")
        except SyntaxError as exc:
            errors.append(f"{readme_path} (block {idx}): Syntax error: {exc.msg}")
            continue
        for lineno, reason in validate_ast_imports(tree):
            errors.append(f"{readme_path} (block {idx}, line {lineno}): {reason}")
        for lineno, reason in validate_ast_sdk_credentials(tree):
            errors.append(f"{readme_path} (block {idx}, line {lineno}): {reason}")
        parsed_blocks.append((idx, code, tree))

    if errors:
        return errors

    try:
        airflow_available = importlib.util.find_spec("airflow") is not None
    except (ModuleNotFoundError, ValueError, ImportError):
        airflow_available = False

    env = _build_simulation_subprocess_env(repo_root)
    with tempfile.TemporaryDirectory(prefix="imednet_readme_sim_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        stub_dir = tmp_dir / "stubs"
        stub_dir.mkdir(parents=True, exist_ok=True)
        (stub_dir / "sitecustomize.py").write_text(_SIMULATION_SITECUSTOMIZE, encoding="utf-8")
        airflow_home = tmp_dir / "airflow_home"
        airflow_home.mkdir(parents=True, exist_ok=True)
        run_cwd = tmp_dir / "cwd"
        run_cwd.mkdir(parents=True, exist_ok=True)

        env["PYTHONPATH"] = os.pathsep.join([str(stub_dir), env["PYTHONPATH"]])
        env["AIRFLOW_HOME"] = str(airflow_home)
        env["IMEDNET_STUB_SINKS"] = "1"
        env["IMEDNET_API_KEY"] = "sim-readme-api-placeholder"  # pragma: allowlist secret
        env["IMEDNET_SECURITY_KEY"] = "sim-readme-sec-placeholder"  # pragma: allowlist secret

        for idx, code, tree in parsed_blocks:
            if _tree_imports_airflow(tree) and not airflow_available:
                continue
            zero_arg_funcs = [
                node.name
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and not node.args.posonlyargs
                and not node.args.args
                and not node.args.kwonlyargs
            ]
            runner = code + "\n" + "\n".join(f"{fn_name}()" for fn_name in zero_arg_funcs) + "\n"
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", runner],
                    cwd=str(run_cwd),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                errors.append(
                    f"{readme_path} (block {idx}): Timed out after {timeout_seconds:.1f}s"
                )
                continue
            if proc.returncode != 0:
                tail = (
                    proc.stderr.strip().splitlines()[-1]
                    if proc.stderr.strip()
                    else "execution failed"
                )
                errors.append(f"{readme_path} (block {idx}): Execution failed: {tail}")
                continue
            err_reason = _find_output_error(proc.stdout, proc.stderr)
            if err_reason is not None:
                errors.append(f"{readme_path} (block {idx}): {err_reason}")

    return errors


def validate_provider_readme_blocks(
    readme_path: Path,
    repo_root: Path | None = None,
    timeout_seconds: float = 60.0,
) -> list[str]:
    """Validate and execute Python blocks in packages/providers-airflow/README.md without MagicMock."""
    if not readme_path.exists():
        return [f"Provider README not found: {readme_path}"]
    return validate_package_readme_blocks(readme_path, repo_root, timeout_seconds=timeout_seconds)


def validate_all_package_readmes(
    packages_dir: Path,
    repo_root: Path | None = None,
    timeout_seconds: float = 60.0,
) -> list[str]:
    """Validate and execute Python blocks across all workspace packages/*/README.md files."""
    if not packages_dir.is_dir():
        return [f"Packages directory not found: {packages_dir}"]

    pkg_dirs = sorted(
        d for d in packages_dir.iterdir() if d.is_dir() and (d / "pyproject.toml").is_file()
    )
    if not pkg_dirs:
        return [f"No workspace packages found under {packages_dir}"]

    errors: list[str] = []
    for pkg_dir in pkg_dirs:
        readme_path = pkg_dir / "README.md"
        errors.extend(
            validate_package_readme_blocks(readme_path, repo_root, timeout_seconds=timeout_seconds)
        )
    return errors


def validate_export_examples_require_security_key(
    examples_dir: Path,
    repo_root: Path | None = None,
    timeout_seconds: float = 30.0,
) -> list[str]:
    """Verify examples/*_export.py require IMEDNET_SECURITY_KEY when IMEDNET_SIMULATION_MODE is disabled."""
    if not examples_dir.is_dir():
        return []
    export_scripts = sorted(examples_dir.glob("*_export.py"))
    if not export_scripts:
        return []

    sim_env = _build_simulation_subprocess_env(repo_root)
    env = {
        k: v
        for k, v in sim_env.items()
        if k
        not in {
            "IMEDNET_SIMULATION_MODE",
            "IMEDNET_SECURITY_KEY",
            "IMEDNET_OIDC_TOKEN",
        }
    }
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="imednet_export_sec_check_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        (tmp_dir / "sitecustomize.py").write_text(_SIMULATION_SITECUSTOMIZE, encoding="utf-8")
        env["PYTHONPATH"] = os.pathsep.join([str(tmp_dir), env["PYTHONPATH"]])
        env["IMEDNET_STUB_SINKS"] = "1"
        env["IMEDNET_API_KEY"] = "non-sim-api-placeholder"  # pragma: allowlist secret

        for script_path in export_scripts:
            try:
                proc = subprocess.run(
                    [sys.executable, str(script_path.resolve())],
                    cwd=str(tmp_dir),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                errors.append(
                    f"{script_path}: Timed out checking missing IMEDNET_SECURITY_KEY rejection"
                )
                continue
            if proc.returncode == 0:
                errors.append(
                    f"{script_path}: Succeeded without IMEDNET_SECURITY_KEY outside simulation mode"
                )
                continue
            if (
                "IMEDNET_SECURITY_KEY" not in proc.stderr
                and "Security key is required" not in proc.stderr
            ):
                tail = (
                    proc.stderr.strip().splitlines()[-1]
                    if proc.stderr.strip()
                    else "unexpected failure"
                )
                errors.append(
                    f"{script_path}: Expected missing IMEDNET_SECURITY_KEY error, got: {tail}"
                )
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
            line_offset = (exc.lineno or 1) - 1
            label = (
                "Inline testcode syntax error"
                if is_testcode
                else "Inline Python block syntax error"
            )
            errors.append(f"{doc_file}:{block_start_idx + 1 + line_offset}: {label}: {exc.msg}")
            continue

        for rel_lineno, reason in validate_ast_imports(tree, workspace_only=True):
            abs_lineno = block_start_idx + max(rel_lineno, 1)
            errors.append(
                f"{doc_file}:{abs_lineno}: {reason} (in directive at line {directive_lineno})"
            )

    return errors


def _validate_markdown_python_blocks(md_file: Path, md_lines: list[str]) -> list[str]:
    """Validate syntax and workspace symbol imports inside fenced Markdown Python blocks."""
    errors: list[str] = []
    idx = 0
    total = len(md_lines)

    while idx < total:
        match = MD_PYTHON_FENCE_OPEN_PATTERN.match(md_lines[idx])
        if not match:
            idx += 1
            continue

        fence_indent = len(match.group(1))
        fence_lineno = idx + 1
        idx += 1
        block_start_idx = idx
        buf: list[str] = []

        while idx < total and not MD_PYTHON_FENCE_CLOSE_PATTERN.match(md_lines[idx]):
            raw = md_lines[idx]
            buf.append(raw[fence_indent:] if len(raw) >= fence_indent else raw)
            idx += 1

        if idx < total:
            idx += 1

        snippet = textwrap.dedent("\n".join(buf))
        if not snippet.strip():
            continue

        try:
            tree = ast.parse(snippet, filename=str(md_file))
        except SyntaxError as exc:
            line_offset = (exc.lineno or 1) - 1
            errors.append(
                f"{md_file}:{block_start_idx + 1 + line_offset}: "
                f"Markdown Python block syntax error: {exc.msg}"
            )
            continue

        for rel_lineno, reason in validate_ast_imports(tree, workspace_only=True):
            abs_lineno = block_start_idx + max(rel_lineno, 1)
            errors.append(
                f"{md_file}:{abs_lineno}: {reason} (in fenced block at line {fence_lineno})"
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
        elif doc_file.suffix == ".md":
            errors.extend(_validate_markdown_python_blocks(doc_file, doc_lines))

    if repo_root is not None:
        seen_md = {p.resolve() for p in doc_files if p.suffix == ".md"}
        extra_md_candidates: list[Path] = []
        root_readme = repo_root / "README.md"
        if root_readme.is_file():
            extra_md_candidates.append(root_readme)
        packages_dir = repo_root / "packages"
        if packages_dir.exists():
            extra_md_candidates.extend(sorted(packages_dir.glob("*/README.md")))
        examples_dir = repo_root / "examples"
        if examples_dir.exists():
            extra_md_candidates.extend(sorted(examples_dir.rglob("*.md")))

        for md_file in extra_md_candidates:
            resolved_md = md_file.resolve()
            if resolved_md in seen_md or not md_file.is_file():
                continue
            seen_md.add(resolved_md)
            try:
                md_content = md_file.read_text(encoding="utf-8")
            except Exception as exc:
                errors.append(f"{md_file}: Failed to read file: {exc}")
                continue
            errors.extend(_validate_markdown_python_blocks(md_file, md_content.splitlines()))

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


SHOWCASE_EXPECTED_SDK_SCENARIOS: tuple[str, ...] = (
    "records",
    "studies",
    "forms",
    "queries",
)

CONTEXT_REQUIRED_CANONICAL_TERMS: tuple[str, ...] = (
    "Study",
    "Query",
    "Variable",
)

CONTEXT_DISALLOWED_SYNONYMS: dict[str, tuple[str, ...]] = {
    "Study": ("trial", "trials", "project", "projects", "experiment", "experiments"),
    "Subject": ("patient", "patients", "participant", "participants", "candidate", "candidates"),
    "Site": ("center", "centers", "clinic", "clinics", "location", "locations"),
    "Form": ("ecrf", "ecrfs", "questionnaire", "questionnaires", "survey", "surveys"),
    "Record": ("submission", "submissions", "entry", "entries", "row", "rows"),
    "Visit": ("appointment", "appointments", "event", "events", "milestone", "milestones"),
    "Interval": ("phase", "phases", "window", "windows", "period", "periods"),
    "Variable": (
        "field",
        "fields",
        "fieldId",
        "fieldCount",
        "column",
        "columns",
        "parameter",
        "parameters",
        "question",
        "questions",
    ),
    "Query": ("discrepancy", "discrepancies", "ticket", "tickets", "flag", "flags", "flagged"),
}

_JS_BROWSER_GLOBALS_PATTERN = re.compile(
    r"\bwindow\.(?:location|history|addEventListener|dispatchEvent)\b"
)

_CANONICAL_QUERY_COMMENT_KEYS: frozenset[str] = frozenset(
    {"sequence", "annotationStatus", "user", "comment", "closed", "date"}
)


def _extract_js_object_literal(js_content: str, const_name: str) -> str | None:
    """Extract balanced `{ ... }` literal assigned to `const <const_name> = {`."""
    marker = re.search(rf"\bconst\s+{re.escape(const_name)}\s*=\s*\{{", js_content)
    if not marker:
        return None
    start = marker.end() - 1
    depth = 0
    in_str: str | None = None
    escaped = False
    i = start
    n = len(js_content)
    while i < n:
        ch = js_content[i]
        if in_str is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
        elif ch in ('"', "'", "`"):
            in_str = ch
        elif ch == "/" and i + 1 < n and js_content[i + 1] == "/":
            newline_pos = js_content.find("\n", i + 2)
            i = n if newline_pos == -1 else newline_pos
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return js_content[start : i + 1]
        i += 1
    return None


def _parse_js_mock_data(js_content: str) -> tuple[dict[str, object] | None, str | None]:
    """Parse MOCK_DATA object literal from showcase.js into Python dicts/lists."""
    literal = _extract_js_object_literal(js_content, "MOCK_DATA")
    if literal is None:
        return None, "MOCK_DATA object literal not found"

    # Strip single-line comments outside string literals
    cleaned_chars: list[str] = []
    in_str: str | None = None
    escaped = False
    i = 0
    n = len(literal)
    while i < n:
        ch = literal[i]
        if in_str is not None:
            cleaned_chars.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
            cleaned_chars.append(ch)
        elif ch == "/" and i + 1 < n and literal[i + 1] == "/":
            newline_pos = literal.find("\n", i + 2)
            if newline_pos == -1:
                break
            i = newline_pos
            continue
        else:
            cleaned_chars.append(ch)
        i += 1

    normalized = "".join(cleaned_chars)
    # Quote bare JS object property keys while leaving string contents untouched
    out_parts: list[str] = []
    token_re = re.compile(r'"(?:\\.|[^"\\])*"|([\{,\s])([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)')
    pos = 0
    for m in token_re.finditer(normalized):
        out_parts.append(normalized[pos : m.start()])
        if m.group(2) is not None:
            out_parts.append(f'{m.group(1)}"{m.group(2)}"{m.group(3)}')
        else:
            out_parts.append(m.group(0))
        pos = m.end()
    out_parts.append(normalized[pos:])
    json_ready = re.sub(r",\s*([\}\]])", r"\1", "".join(out_parts))

    try:
        parsed = json.loads(json_ready)
    except json.JSONDecodeError as exc:
        return None, f"Failed to parse MOCK_DATA JSON structure: {exc}"

    if not isinstance(parsed, dict):
        return None, "Expected MOCK_DATA to parse as an object"
    return parsed, None


def load_postman_contract_schemas(postman_path: Path) -> tuple[dict[str, set[str]], list[str]]:
    """Extract canonical Study, Form, and Query response attribute sets from Postman collection."""
    if not postman_path.exists():
        return {}, [f"Postman collection not found: {postman_path}"]

    try:
        collection = json.loads(postman_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{postman_path}: Failed to parse JSON: {exc}"]

    target_requests = {
        "List records": "records",
        "List studies": "studies",
        "List forms": "forms",
        "List queries": "queries",
        "List record revisions": "audits",
    }
    schemas: dict[str, set[str]] = {
        "records": set(),
        "studies": set(),
        "forms": set(),
        "queries": set(),
        "audits": set(),
    }

    def _walk_items(items: object) -> None:
        if not isinstance(items, list):
            return
        for entry in items:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name in target_requests:
                bucket = target_requests[name]
                for resp in entry.get("response", []) or []:
                    if not isinstance(resp, dict) or resp.get("code") != 200:
                        continue
                    body = resp.get("body")
                    if not isinstance(body, str) or not body.strip():
                        continue
                    with contextlib.suppress(json.JSONDecodeError):
                        payload = json.loads(body)
                        if isinstance(payload, dict):
                            data_section = payload.get("data")
                            if isinstance(data_section, list):
                                for sample in data_section:
                                    if isinstance(sample, dict):
                                        schemas[bucket].update(sample.keys())
                            elif "studyKey" in payload:
                                schemas[bucket].update(payload.keys())
            if "item" in entry:
                _walk_items(entry["item"])

    _walk_items(collection.get("item", []))
    missing = [name for name, keys in schemas.items() if not keys]
    if missing:
        return schemas, [
            f"{postman_path}: Missing canonical schema definitions for {sorted(missing)}"
        ]
    return schemas, []


_CANONICAL_DATADICT_FORM_KEYS: frozenset[str] = frozenset(
    {"formKey", "formName", "formId", "formType", "variables", "businessLogic", "extraAttributes"}
)


def validate_showcase_mock_data_schemas(
    showcase_js_path: Path,
    postman_path: Path,
) -> list[str]:
    """Validate MOCK_DATA REST (records, studies, formsCatalog, queries, audits) and DataDictionary (forms) schemas."""
    if not showcase_js_path.exists():
        return [f"Showcase script not found: {showcase_js_path}"]

    schemas, schema_errors = load_postman_contract_schemas(postman_path)
    if schema_errors:
        return schema_errors

    try:
        js_content = showcase_js_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{showcase_js_path}: Failed to read file: {exc}"]

    mock_data, parse_err = _parse_js_mock_data(js_content)
    if parse_err is not None or mock_data is None:
        return [f"{showcase_js_path}: {parse_err}"]

    errors: list[str] = []

    section_specs: list[tuple[str, set[str], bool]] = [
        ("studies", schemas["studies"], True),
        ("formsCatalog", schemas["forms"], True),
        ("queries", schemas["queries"], True),
        ("audits", schemas["audits"], False),
    ]

    for section_name, allowed_keys, required in section_specs:
        raw_section = mock_data.get(section_name)
        if raw_section is None and not required:
            continue
        if not isinstance(raw_section, list):
            errors.append(
                f"{showcase_js_path}: MOCK_DATA.{section_name} is missing or has invalid container type"
            )
            continue
        if not raw_section:
            errors.append(f"{showcase_js_path}: MOCK_DATA.{section_name} must not be empty")
            continue

        for idx, item_val in enumerate(raw_section):
            item_label = f"{section_name}[{idx}]"
            if not isinstance(item_val, dict) or not item_val:
                errors.append(
                    f"{showcase_js_path}: MOCK_DATA.{item_label} must be a non-empty object"
                )
                continue
            item_keys = set(item_val.keys())
            unexpected = sorted(item_keys - allowed_keys)
            if unexpected:
                errors.append(
                    f"{showcase_js_path}: MOCK_DATA.{item_label} contains non-contract keys "
                    f"{unexpected}; allowed Postman {section_name} keys: {sorted(allowed_keys)}"
                )
            if section_name == "queries" and "queryComments" in item_val:
                comments = item_val["queryComments"]
                if not isinstance(comments, list):
                    errors.append(
                        f"{showcase_js_path}: MOCK_DATA.{item_label}.queryComments must be a list"
                    )
                else:
                    for c_idx, comment in enumerate(comments):
                        if not isinstance(comment, dict):
                            errors.append(
                                f"{showcase_js_path}: MOCK_DATA.{item_label}.queryComments[{c_idx}] "
                                "must be an object"
                            )
                            continue
                        extra_comment_keys = sorted(
                            set(comment.keys()) - _CANONICAL_QUERY_COMMENT_KEYS
                        )
                        if extra_comment_keys:
                            errors.append(
                                f"{showcase_js_path}: MOCK_DATA.{item_label}.queryComments[{c_idx}] "
                                f"contains non-contract keys {extra_comment_keys}"
                            )

    raw_records = mock_data.get("records")
    if isinstance(raw_records, dict) and raw_records:
        allowed_record_keys = schemas["records"]
        for rec_domain, domain_items in raw_records.items():
            if not isinstance(domain_items, list) or not domain_items:
                errors.append(
                    f"{showcase_js_path}: MOCK_DATA.records.{rec_domain} must be a non-empty list"
                )
                continue
            for r_idx, rec_val in enumerate(domain_items):
                rec_label = f"records.{rec_domain}[{r_idx}]"
                if not isinstance(rec_val, dict) or not rec_val:
                    errors.append(
                        f"{showcase_js_path}: MOCK_DATA.{rec_label} must be a non-empty object"
                    )
                    continue
                unexpected_rec = sorted(set(rec_val.keys()) - allowed_record_keys)
                if unexpected_rec:
                    errors.append(
                        f"{showcase_js_path}: MOCK_DATA.{rec_label} contains non-contract keys "
                        f"{unexpected_rec}; allowed Postman records keys: "
                        f"{sorted(allowed_record_keys)}"
                    )
                if "recordData" not in rec_val or not isinstance(rec_val.get("recordData"), dict):
                    errors.append(
                        f"{showcase_js_path}: MOCK_DATA.{rec_label}.recordData must be an object"
                    )
    else:
        errors.append(
            f"{showcase_js_path}: MOCK_DATA.records is missing or has invalid container type"
        )

    raw_forms = mock_data.get("forms")
    if isinstance(raw_forms, dict) and raw_forms:
        for form_domain, form_val in raw_forms.items():
            if not isinstance(form_val, dict) or not form_val:
                errors.append(
                    f"{showcase_js_path}: MOCK_DATA.forms.{form_domain} must be a non-empty object"
                )
                continue
            unexpected_dd = sorted(set(form_val.keys()) - _CANONICAL_DATADICT_FORM_KEYS)
            if unexpected_dd:
                errors.append(
                    f"{showcase_js_path}: MOCK_DATA.forms.{form_domain} contains non-contract keys "
                    f"{unexpected_dd}; allowed DataDictionary Form keys: "
                    f"{sorted(_CANONICAL_DATADICT_FORM_KEYS)}"
                )
    else:
        errors.append(
            f"{showcase_js_path}: MOCK_DATA.forms is missing or has invalid container type"
        )

    return errors


def extract_showcase_sdk_snippets(showcase_js_path: Path) -> tuple[dict[str, str], list[str]]:
    """Extract SNIPPETS.sdk Python snippets from docs/_static/showcase.js."""
    if not showcase_js_path.exists():
        return {}, [f"Showcase script not found: {showcase_js_path}"]

    try:
        content = showcase_js_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [f"{showcase_js_path}: Failed to read file: {exc}"]

    sdk_match = re.search(r"sdk:\s*\{(.*?)\n\s*\},?\s*rest:", content, re.DOTALL)
    if not sdk_match:
        return {}, [f"{showcase_js_path}: SNIPPETS.sdk dictionary not found"]

    raw_snippets = dict(re.findall(r"(\w+):\s*`([^`]+)`", sdk_match.group(1)))
    snippets = {name: raw.replace(r"\n", "\n") for name, raw in raw_snippets.items()}

    expected = set(SHOWCASE_EXPECTED_SDK_SCENARIOS)
    actual = set(snippets.keys())
    errors: list[str] = []
    if actual != expected:
        errors.append(
            f"{showcase_js_path}: Expected SNIPPETS.sdk keys {sorted(expected)}, "
            f"found {sorted(actual)}"
        )
    return snippets, errors


def validate_canonical_showcase_terminology(file_path: Path) -> list[str]:
    """Validate that a showcase file uses canonical CONTEXT.md terminology and zero disallowed synonyms."""
    if not file_path.exists():
        return [f"Showcase file not found: {file_path}"]

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{file_path}: Failed to read file: {exc}"]

    errors: list[str] = []
    for canonical_term in CONTEXT_REQUIRED_CANONICAL_TERMS:
        if not re.search(rf"\b{re.escape(canonical_term)}\b", content):
            errors.append(
                f"{file_path}: Missing required canonical CONTEXT.md term '{canonical_term}'"
            )

    lines = content.splitlines()
    for line_idx, raw_line in enumerate(lines, start=1):
        scan_line = raw_line
        if file_path.suffix == ".js":
            scan_line = _JS_BROWSER_GLOBALS_PATTERN.sub("", scan_line)

        for canonical_term, disallowed_forms in CONTEXT_DISALLOWED_SYNONYMS.items():
            for synonym in disallowed_forms:
                match = re.search(rf"\b{re.escape(synonym)}\b", scan_line, re.IGNORECASE)
                if match:
                    errors.append(
                        f"{file_path}:{line_idx}: Disallowed CONTEXT.md synonym "
                        f"'{match.group(0)}' (use canonical '{canonical_term}')"
                    )
    return errors


def validate_landing_showcase(docs_dir: Path, repo_root: Path | None = None) -> list[str]:
    """Verify landing-page read-only showcase markup, deterministic timing, vocabulary, and offline SDK execution."""
    errors: list[str] = []
    showcase_js = docs_dir / "_static" / "showcase.js"
    if not showcase_js.exists():
        return [f"Showcase script not found: {showcase_js}"]

    configure_sys_path(Path(__file__).resolve().parent.parent)
    if repo_root is not None:
        configure_sys_path(repo_root)

    try:
        js_content = showcase_js.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{showcase_js}: Failed to read file: {exc}"]

    if "Math.random" in js_content:
        errors.append(
            f"{showcase_js}: Forbidden Math.random() latency simulation found in showcase.js"
        )
    for legacy_selector in ("#ie-code-input", "#ie-run-btn"):
        if legacy_selector in js_content:
            errors.append(
                f"{showcase_js}: Legacy editable sandbox selector '{legacy_selector}' must be removed"
            )

    errors.extend(validate_canonical_showcase_terminology(showcase_js))

    default_root = Path(__file__).resolve().parent.parent
    for candidate_root in [r for r in (repo_root, default_root) if r is not None]:
        postman_candidate = candidate_root / "imednet.postman_collection.json"
        if postman_candidate.is_file():
            errors.extend(validate_showcase_mock_data_schemas(showcase_js, postman_candidate))
            break

    index_rst = docs_dir / "index.rst"
    if index_rst.exists():
        try:
            index_content = index_rst.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{index_rst}: Failed to read file: {exc}")
            index_content = ""

        if "interactive-showcase-root" in index_content:
            for forbidden_markup in ("<textarea", "ie-code-input", "ie-run-btn"):
                if forbidden_markup in index_content:
                    errors.append(
                        f"{index_rst}: Legacy editable sandbox markup '{forbidden_markup}' "
                        f"must not appear in landing showcase"
                    )
            for required_markup in (
                'id="ie-code-display"',
                'id="ie-snippet-copy-btn"',
                "tutorials/quick_start.html",
            ):
                if required_markup not in index_content:
                    errors.append(
                        f"{index_rst}: Missing required read-only showcase element '{required_markup}'"
                    )
            errors.extend(validate_canonical_showcase_terminology(index_rst))

    snippets, snippet_errors = extract_showcase_sdk_snippets(showcase_js)
    errors.extend(snippet_errors)
    if snippet_errors:
        return errors

    try:
        from imednet.models.forms import Form
        from imednet.models.queries import Query
        from imednet.models.records import Record
        from imednet.models.studies import Study
        from imednet.simulation.state import get_default_simulation_state
    except Exception as exc:
        errors.append(f"{showcase_js}: Failed to import imednet simulation dependencies: {exc}")
        return errors

    expected_models: dict[str, type[object]] = {
        "records": Record,
        "studies": Study,
        "forms": Form,
        "queries": Query,
    }

    for scenario in SHOWCASE_EXPECTED_SDK_SCENARIOS:
        code_py = snippets[scenario]
        snippet_label = f"{showcase_js}:SNIPPETS.sdk.{scenario}"

        if "ImednetClient" in code_py:
            errors.append(
                f"{snippet_label}: Must instantiate canonical ImednetSDK(simulation_mode=True) "
                "instead of ImednetClient"
            )
        if "STUDY-101" in code_py:
            errors.append(f"{snippet_label}: Must target seeded 'STUDY' instead of 'STUDY-101'")

        try:
            tree = ast.parse(code_py, filename=snippet_label)
        except SyntaxError as exc:
            errors.append(f"{snippet_label}:{exc.lineno}: Syntax error: {exc.msg}")
            continue

        for lineno, reason in validate_ast_imports(tree):
            errors.append(f"{snippet_label}:{lineno}: {reason}")

        get_default_simulation_state().reset()
        namespace: dict[str, object] = {"__name__": "__showcase__"}
        stdout_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_buf):
                exec(compile(tree, filename=snippet_label, mode="exec"), namespace)  # noqa: S102
        except Exception as exc:
            errors.append(
                f"{snippet_label}: Offline simulation execution failed: {type(exc).__name__}: {exc}"
            )
            continue

        for var_name, var_val in namespace.items():
            if isinstance(var_val, (types.GeneratorType, Iterator)):
                errors.append(
                    f"{snippet_label}: Variable '{var_name}' is an unmaterialized lazy iterator "
                    f"({type(var_val).__name__}); materialize lazy iterators with list(...)"
                )

        expected_cls = expected_models[scenario]
        result_items = namespace.get(scenario)
        if not isinstance(result_items, (list, tuple)):
            errors.append(
                f"{snippet_label}: Expected materialized list variable '{scenario}', "
                f"got {type(result_items).__name__}"
            )
            continue
        if not result_items:
            errors.append(
                f"{snippet_label}: Simulation execution returned 0 {expected_cls.__name__} items"
            )
            continue
        if not all(isinstance(item, expected_cls) for item in result_items):
            errors.append(
                f"{snippet_label}: Expected all items in '{scenario}' to be "
                f"{expected_cls.__name__} instances"
            )

        if scenario == "records":
            df_obj = namespace.get("df")
            if df_obj is None or len(df_obj) == 0:  # type: ignore[arg-type]
                errors.append(f"{snippet_label}: Expected non-empty DataFrame variable 'df'")

    get_default_simulation_state().reset()
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
    parser.add_argument(
        "--skip-execution",
        action="store_true",
        help="Skip executing showcase scripts under IMEDNET_SIMULATION_MODE=true",
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

    print(f"Validating showcase AST, docstrings, and imports in {examples_dir}...")
    showcase_errors = validate_showcase_ast_and_imports(examples_dir, repo_root)

    execution_errors: list[str] = []
    if not args.skip_execution and not showcase_errors:
        print(f"Executing showcase scripts in simulation mode from {examples_dir}...")
        execution_errors = execute_showcases_in_simulation(examples_dir, repo_root)
        execution_errors.extend(
            validate_export_examples_require_security_key(examples_dir, repo_root)
        )
        if (examples_dir / "airflow").exists():
            print(f"Validating Airflow 3.x DAGs and CSV exports in {examples_dir / 'airflow'}...")
            execution_errors.extend(
                validate_airflow_dags_in_simulation(examples_dir / "airflow", repo_root)
            )

    packages_dir = repo_root / "packages"
    readme_errors: list[str] = []
    if packages_dir.is_dir():
        print(f"Validating package README Python blocks across {packages_dir}/*/README.md...")
        readme_errors = validate_all_package_readmes(packages_dir, repo_root)

    print(f"Validating documentation literal includes in {docs_dir}...")
    doc_errors = validate_documentation_literal_includes(docs_dir, repo_root)

    landing_errors: list[str] = []
    if (docs_dir / "_static" / "showcase.js").exists():
        print(f"Validating landing-page simulation showcase in {docs_dir}...")
        landing_errors = validate_landing_showcase(docs_dir, repo_root)

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

    all_errors = (
        showcase_errors
        + execution_errors
        + readme_errors
        + doc_errors
        + landing_errors
        + wheel_errors
    )

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
