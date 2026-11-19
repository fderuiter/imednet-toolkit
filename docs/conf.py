"""Sphinx configuration."""

# Configuration file for the Sphinx documentation builder.
import ast
import functools
import importlib
import importlib.util
import json
import os
import re
import sys
import types
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from enum import Enum
from pathlib import Path

# mypy: ignore-errors
from typing import Any, Union, get_args, get_origin

import pydantic._internal._utils as _pydantic_internal_utils
from pydantic import BaseModel, RootModel

if not hasattr(_pydantic_internal_utils, "MappingIntStrAny"):
    _pydantic_internal_utils.MappingIntStrAny = Mapping[int, Any] | Mapping[str, Any]
if not hasattr(_pydantic_internal_utils, "AbstractSetIntStr"):
    _pydantic_internal_utils.AbstractSetIntStr = AbstractSet[int] | AbstractSet[str]

"""
Configuration file for the Sphinx documentation builder.
This module contains all necessary configurations for building documentation
using Sphinx. It sets up the project information, extensions, and theme settings.
Attributes:
    project (str): The name of the project ("imednet")
    author (str): The author's name ("Frederick de Ruiter")
    release (str): The full project version from ``imednet.__version__``
    version (str): The short project version from ``imednet.__version__``
    extensions (list): List of Sphinx extensions to be used
    templates_path (list[str]): Path to custom templates
    exclude_patterns (list[str]): Patterns to exclude from documentation
    html_static_path (list[str]): Path to static files
    html_theme (str): The HTML theme to be used ("furo")
    html_theme_path (list): Path to the HTML theme
Note:
    This configuration uses the Furo theme and includes support for
    automatic documentation generation from docstrings and type hints.
"""


# Add package source roots so API modules can be imported for docs builds.
sys.path[:0] = [
    os.path.abspath("../packages/core/src"),
    os.path.abspath("../packages/providers-airflow/src"),
    os.path.abspath("../packages/plugins-workflows/src"),
    os.path.abspath("../packages/plugins-sinks/src"),
    os.path.abspath("../packages/plugins-tui/src"),
]

# Mock heavy optional dependencies so autodoc does not require them.
# Mock pandas to avoid heavy dependency while building docs if not installed.
if not importlib.util.find_spec("pandas"):
    pandas_stub = types.ModuleType("pandas")

    class DataFrame:  # pragma: no cover - simple stub
        """Dummy DataFrame class."""

        def __init__(self, *args: Any, **kwargs: Any):
            """Initialize dummy DataFrame."""
            self.columns = type(
                "MockColumns",
                (),
                {
                    "astype": lambda self, t: self,
                    "str": type(
                        "MockStr",
                        (),
                        {
                            "lower": lambda self: type(
                                "MockLower", (), {"duplicated": lambda self: []}
                            )()
                        },
                    )(),
                },
            )()
            self.loc = type("MockLoc", (), {"__getitem__": lambda self, idx: DataFrame()})()
            self.empty = True

    def json_normalize(*args: Any, **kwargs: Any) -> DataFrame:  # type: ignore
        """Dummy json_normalize function."""
        return DataFrame()

    pandas_stub.DataFrame = DataFrame
    pandas_stub.json_normalize = json_normalize
    sys.modules["pandas"] = pandas_stub
else:
    import pandas  # noqa: F401

from imednet import __version__ as imednet_version

project = "imednet"
author = "Frederick de Ruiter"
release = imednet_version
version = imednet_version

# Sphinx extensions
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "sphinx_autodoc_typehints",
    "sphinx.ext.autosummary",
    "sphinxcontrib.mermaid",
    "sphinxarg.ext",
    "sphinx.ext.doctest",
    "myst_parser",
    "sphinx.ext.githubpages",
    "sphinx_thebe",
]

# Configure sphinx-thebe options for interactive in-browser execution with Pyodide
thebe_config = {
    "always_load": False,
    "selector": "div.highlight-python",
    "selector_input": "pre",
    "selector_output": ".output, .cell_output",
    "kernel_name": "pyodide",
    "wheels": [
        f"_static/wheels/imednet-{imednet_version}-py3-none-any.whl",
    ],
    "wheel_paths": [
        f"_static/wheels/imednet-{imednet_version}-py3-none-any.whl",
    ],
    "requirements": [
        "micropip",
        "pydantic>=2.11.3,<3.0.0",
        "httpx>=0.28.1,<0.29.0",
        "tenacity>=9.1.2,<10.0.0",
        "defusedxml>=0.7.1",
        "python-dotenv",
    ],
    "micropip_requirements": [
        "micropip",
        "pydantic>=2.11.3,<3.0.0",
        "httpx>=0.28.1,<0.29.0",
        "tenacity>=9.1.2,<10.0.0",
        "defusedxml>=0.7.1",
        "python-dotenv",
    ],
}

autosummary_generate = False

# Mock heavy optional dependencies so autodoc does not import them.
# opentelemetry is listed here so that `if TYPE_CHECKING: from opentelemetry…`
# blocks (evaluated when set_type_checking_flag=True) do not cause
# ModuleNotFoundError during the docs build.
autodoc_mock_imports = [
    "matplotlib",
    "airflow",
    "opentelemetry",
    "pyarrow",
    "duckdb",
]

suppress_warnings = [
    "autodoc.import",
    "autodoc",
    "sphinx_autodoc_typehints",
]

_DOCS_DIR = Path(__file__).resolve().parent
_FIRST_PARTY_ROOTS = frozenset(
    {
        "imednet",
        "imednet_workflows",
        "imednet_sinks",
        "imednet_tui",
        "apache_airflow_providers_imednet",
    }
)
_PYDANTIC_INTERNAL_ATTRS = frozenset(
    {
        "model_config",
        "model_fields",
        "model_computed_fields",
        "model_extra",
        "model_fields_set",
    }
)
_SCOPED_INHERITED_EXCLUSIONS = (
    "BaseModel, RootModel, Enum, str, int, Exception, BaseException, object"
)
_AUTOMODULE_RE = re.compile(r"^\.\.\s+automodule::\s+([A-Za-z0-9_.]+)", re.MULTILINE)
_PY_ATTR_DIRECTIVE_RE = re.compile(r"^\.\.\s+(?:py:)?attribute::\s+([A-Za-z0-9_]+)")


@functools.cache
def _get_documented_modules() -> frozenset[str]:
    """Return all canonical module names documented via .. automodule:: in docs/reference/api/."""
    mods: set[str] = set()
    api_dir = _DOCS_DIR / "reference" / "api"
    search_root = api_dir if api_dir.exists() else _DOCS_DIR
    for rst_path in search_root.rglob("*.rst"):
        if "_build" in rst_path.parts:
            continue
        try:
            text = rst_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _AUTOMODULE_RE.finditer(text):
            mods.add(match.group(1))
    return frozenset(mods)


def _resolve_relative_module(mod_name: str, is_pkg: bool, module: str | None, level: int) -> str:
    """Resolve a relative import target against mod_name."""
    if level == 0:
        return module or ""
    parts = mod_name.split(".")
    anchor = parts if is_pkg else parts[:-1]
    trim = level - 1
    base = anchor[: len(anchor) - trim] if trim <= len(anchor) else []
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _collect_ast_symbols(
    stmts: list[ast.stmt],
    mod_name: str,
    is_pkg: bool,
    imported: dict[str, tuple[str, str]],
    star_imports: list[str],
    defined: set[str],
) -> None:
    """Recursively collect imported and locally defined top-level symbols from AST statements."""
    for node in stmts:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imported[alias.asname] = (alias.name, "")
                else:
                    top = alias.name.split(".")[0]
                    imported[top] = (top, "")
        elif isinstance(node, ast.ImportFrom):
            target_mod = _resolve_relative_module(mod_name, is_pkg, node.module, node.level)
            if not target_mod:
                continue
            for alias in node.names:
                if alias.name == "*":
                    star_imports.append(target_mod)
                else:
                    local_name = alias.asname or alias.name
                    imported[local_name] = (target_mod, alias.name)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
            defined.add(node.name.id)
        elif isinstance(node, ast.If):
            _collect_ast_symbols(node.body, mod_name, is_pkg, imported, star_imports, defined)
            _collect_ast_symbols(node.orelse, mod_name, is_pkg, imported, star_imports, defined)
        elif isinstance(node, ast.Try):
            _collect_ast_symbols(node.body, mod_name, is_pkg, imported, star_imports, defined)
            for handler in node.handlers:
                _collect_ast_symbols(
                    handler.body, mod_name, is_pkg, imported, star_imports, defined
                )
            _collect_ast_symbols(node.orelse, mod_name, is_pkg, imported, star_imports, defined)
            _collect_ast_symbols(node.finalbody, mod_name, is_pkg, imported, star_imports, defined)


@functools.cache
def _analyze_module_symbols(
    mod_name: str,
) -> tuple[dict[str, tuple[str, str]], frozenset[str], dict[str, frozenset[str]]]:
    """Return (imported_map, defined_names, class_ast_members) for a Python module."""
    try:
        mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
    except Exception:
        return {}, frozenset(), {}

    mod_file = getattr(mod, "__file__", None)
    if not mod_file or not os.path.exists(mod_file):
        return {}, frozenset(), {}

    try:
        source = Path(mod_file).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=mod_file)
    except Exception:
        return {}, frozenset(), {}

    is_pkg = os.path.basename(mod_file) == "__init__.py"
    imported: dict[str, tuple[str, str]] = {}
    star_imports: list[str] = []
    defined: set[str] = set()
    _collect_ast_symbols(tree.body, mod_name, is_pkg, imported, star_imports, defined)

    class_members: dict[str, frozenset[str]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            cm: set[str] = set()
            for child in stmt.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    cm.add(child.name)
                elif isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name):
                            cm.add(target.id)
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    cm.add(child.target.id)
            class_members[stmt.name] = frozenset(cm)

    for star_mod_name in star_imports:
        try:
            star_mod = sys.modules.get(star_mod_name) or importlib.import_module(star_mod_name)
        except Exception:
            star_mod = None
        if star_mod is None:
            continue
        star_all = getattr(star_mod, "__all__", None)
        names = (
            list(star_all)
            if star_all is not None
            else [k for k in vars(star_mod) if not k.startswith("_")]
        )
        for sym in names:
            if sym not in imported and sym not in defined:
                imported[sym] = (star_mod_name, sym)

    return imported, frozenset(defined), class_members


def _resolve_canonical_origin(mod_name: str, attr_name: str) -> tuple[str, str]:
    """Follow import re-export chains to find the canonical (module, symbol) origin."""
    visited: set[tuple[str, str]] = set()
    cur_mod, cur_name = mod_name, attr_name
    while (cur_mod, cur_name) not in visited:
        visited.add((cur_mod, cur_name))
        if cur_mod.split(".")[0] not in _FIRST_PARTY_ROOTS:
            return cur_mod, cur_name
        imported, defined, _ = _analyze_module_symbols(cur_mod)
        if cur_name in defined or cur_name not in imported:
            return cur_mod, cur_name
        next_mod, orig_name = imported[cur_name]
        if not orig_name:
            return next_mod, cur_name
        cur_mod, cur_name = next_mod, orig_name
    return cur_mod, cur_name


def _is_exposed_by_documented_module(mod_name: str, sym_name: str) -> bool:
    """Return True if documented module mod_name publicly exposes sym_name for autodoc."""
    if sym_name.startswith("_"):
        return False
    if any(part.startswith("_") for part in mod_name.split(".")):
        return False
    try:
        mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
    except Exception:
        return False
    if not hasattr(mod, sym_name):
        return False
    mod_all = getattr(mod, "__all__", None)
    return mod_all is None or sym_name in mod_all


def _unwrap_shadowed_bases(app: Any, name: str, obj: Any, options: Any, bases: list[Any]) -> None:
    """Replace shadowed same-name base classes with their parent classes."""
    if not isinstance(obj, type):
        return

    unwrapped: list[Any] = []
    seen: set[Any] = set()

    def _expand(base: Any) -> None:
        if isinstance(base, type) and base is not obj and base.__name__ == obj.__name__:
            for parent in base.__bases__:
                if parent is not object:
                    _expand(parent)
            return
        if base not in seen:
            seen.add(base)
            unwrapped.append(base)

    for b in list(bases):
        _expand(b)
    bases[:] = unwrapped


def _format_rst_annotation(annotation: Any) -> str:
    """Format a runtime annotation into PEP 604 RST with qualified first-party type targets."""
    if annotation is Any:
        return "Any"
    if annotation is None or annotation is type(None):
        return "None"

    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        parts = [_format_rst_annotation(a) for a in args if a is not type(None)]
        if any(a is type(None) for a in args):
            parts.append("None")
        return " | ".join(parts)

    if origin is not None:
        origin_name = getattr(origin, "__name__", str(origin).replace("typing.", ""))
        args = get_args(annotation)
        if args:
            inner = ", ".join(_format_rst_annotation(a) for a in args)
            return f"{origin_name}[{inner}]"
        return origin_name

    if isinstance(annotation, type):
        mod = getattr(annotation, "__module__", "builtins")
        if mod and mod != "builtins" and mod.split(".")[0] in _FIRST_PARTY_ROOTS:
            return f"~{mod}.{annotation.__qualname__}"
        return annotation.__name__

    return str(annotation).replace("typing.", "").replace("NoneType", "None")


def _emit_pydantic_model_fields(
    app: Any,
    what: str,
    name: str,
    obj: Any,
    options: Any,
    lines: list[str],
) -> None:
    """Emit .. py:attribute:: blocks for Pydantic v2 model_fields."""
    if what != "class" or not isinstance(obj, type):
        return
    if not issubclass(obj, BaseModel) or obj in (BaseModel, RootModel):
        return

    model_fields = getattr(obj, "model_fields", None)
    if not isinstance(model_fields, dict) or not model_fields:
        return

    existing_attrs: set[str] = set()
    for line in lines:
        match = _PY_ATTR_DIRECTIVE_RE.match(line.strip())
        if match:
            existing_attrs.add(match.group(1))

    appended: list[str] = []
    for field_name, field_info in model_fields.items():
        if field_name in existing_attrs:
            continue
        type_str = _format_rst_annotation(field_info.annotation)
        description = field_info.description or ""
        if not description and field_info.alias and field_info.alias != field_name:
            description = f"API field ``{field_info.alias}``."

        appended.append("")
        appended.append(f".. py:attribute:: {field_name}")
        appended.append(f"   :type: {type_str}")
        if description:
            appended.append("")
            for desc_line in description.splitlines():
                appended.append(f"   {desc_line}" if desc_line else "")

    if appended:
        lines.extend(appended)
        lines.append("")


def _find_defining_class(cls: type, member_name: str) -> type | None:
    """Find the first class in cls.__mro__ that defines member_name."""
    for base in cls.__mro__:
        if member_name in base.__dict__:
            return base
        anns = base.__dict__.get("__annotations__", {})
        if isinstance(anns, dict) and member_name in anns:
            return base
    return None


def _skip_non_canonical_or_internal(
    app: Any,
    what: str,
    name: str,
    obj: Any,
    skip: bool,
    options: Any,
) -> bool | None:
    """Skip non-canonical module re-exports and external/internal inherited class members."""
    if skip:
        return None

    env = getattr(app, "env", None)
    temp_data = getattr(env, "temp_data", {}) if env is not None else {}
    current_mod = temp_data.get("autodoc:module")
    current_cls_name = temp_data.get("autodoc:class")

    if what == "module" and current_mod:
        if isinstance(obj, types.ModuleType):
            if getattr(obj, "__name__", None) != current_mod:
                return True
            return None

        documented_modules = _get_documented_modules()
        canon_mod, canon_name = _resolve_canonical_origin(current_mod, name)
        if canon_mod != current_mod:
            if canon_mod.split(".")[0] not in _FIRST_PARTY_ROOTS:
                return True
            if canon_mod in documented_modules and _is_exposed_by_documented_module(
                canon_mod, canon_name
            ):
                return True

        origin_mod = getattr(obj, "__module__", None)
        if isinstance(origin_mod, str) and origin_mod != current_mod:
            origin_root = origin_mod.split(".")[0]
            if (isinstance(obj, type) or callable(obj)) and origin_root not in _FIRST_PARTY_ROOTS:
                return True
            if origin_mod in documented_modules and _is_exposed_by_documented_module(
                origin_mod, getattr(obj, "__name__", name)
            ):
                return True
        return None

    if what in ("class", "exception") and current_mod and current_cls_name:
        mod = sys.modules.get(current_mod)
        cls = getattr(mod, current_cls_name, None) if mod is not None else None
        if isinstance(cls, type):
            if issubclass(cls, BaseModel):
                if name in _PYDANTIC_INTERNAL_ATTRS:
                    return True
                model_fields = getattr(cls, "model_fields", {})
                if isinstance(model_fields, dict) and name in model_fields:
                    return True

            defining_cls = _find_defining_class(cls, name)
            if defining_cls is cls:
                _, _, cls_ast_map = _analyze_module_symbols(current_mod)
                local_ast_members = cls_ast_map.get(current_cls_name)
                if local_ast_members is not None and name not in local_ast_members:
                    for parent in cls.__mro__[1:]:
                        if name in parent.__dict__:
                            parent_mod = getattr(parent, "__module__", "") or ""
                            if parent_mod.split(".")[0] not in _FIRST_PARTY_ROOTS:
                                return True
                            break
            elif defining_cls is not None:
                if defining_cls in (BaseModel, RootModel, Enum, object, Exception, BaseException):
                    return True
                def_mod = getattr(defining_cls, "__module__", "") or ""
                if def_mod.split(".")[0] not in _FIRST_PARTY_ROOTS:
                    return True
                if any(part.startswith("_") for part in def_mod.split(".")):
                    return True

    return None


def _sanitize_viewcode_modules(app: Any) -> list[Any]:
    """Filter builtins/unrenderable modules from _viewcode_modules and repair backlink targets."""
    env = getattr(app.builder, "env", None)
    vm = getattr(env, "_viewcode_modules", None) if env is not None else None
    if not isinstance(vm, dict):
        return []

    py_objects = env.domaindata.get("py", {}).get("objects", {})
    for modname in list(vm.keys()):
        entry = vm[modname]
        if modname == "builtins" or not entry or modname.split(".")[0] not in _FIRST_PARTY_ROOTS:
            vm.pop(modname, None)
            continue

        code, tags, used, refname = entry
        canonical_ref = (
            modname if any(f"{modname}.{tag_name}" in py_objects for tag_name in tags) else refname
        )
        clean_used: dict[str, str] = {}
        for tag_name in used:
            full_id = f"{canonical_ref}.{tag_name}"
            obj_entry = py_objects.get(full_id)
            if obj_entry is not None and obj_entry.node_id == full_id:
                clean_used[tag_name] = obj_entry.docname
        vm[modname] = (code, tags, clean_used, canonical_ref)

    return []


def setup(app: Any) -> None:  # pragma: no cover
    """Register Sphinx autodoc and viewcode hooks and client JS configuration.

    Args:
        app: The Sphinx application instance (provided by Sphinx at startup).
    """
    _get_documented_modules.cache_clear()
    _analyze_module_symbols.cache_clear()

    app.connect("autodoc-process-bases", _unwrap_shadowed_bases)
    app.connect("autodoc-process-docstring", _emit_pydantic_model_fields, priority=600)
    app.connect("autodoc-skip-member", _skip_non_canonical_or_internal)
    app.connect("html-collect-pages", _sanitize_viewcode_modules, priority=100)

    # Expose thebe_config containing wheel artifacts and requirements to client JS runtime
    app.add_js_file(None, body=f"window.thebe_config = {json.dumps(thebe_config)};")


# Display type hints in the description instead of the signature to keep
# function signatures concise in the rendered documentation.
autodoc_typehints = "description"

# Autodoc default options applied to all automodule/autoclass directives.
autodoc_default_options = {
    "members": True,
    "inherited-members": _SCOPED_INHERITED_EXCLUSIONS,
    "show-inheritance": True,
}
autodoc_class_signature = "separated"

# Force sphinx-autodoc-typehints to evaluate TYPE_CHECKING blocks so that
# forward references used only under `if TYPE_CHECKING:` are resolved.
set_type_checking_flag = True

# Napoleon – enforce Google-style docstrings.
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True

# Templates and static paths
templates_path: list[str] = ["_templates"]
exclude_patterns: list[str] = [
    "tutorials/examples/dummy.py",
    "agents/**",
    "superpowers/**",
    "reference/rest_tables/**",
]  # annotated per mypy requirement
html_static_path: list[str] = ["_static"]
html_css_files: list[str] = ["custom.css"]
html_js_files: list[str] = ["showcase.js", "thebe_pyodide.js"]

doctest_global_setup = """
import os
import respx
import warnings
import sys
warnings.filterwarnings("ignore")
import logging
logging.getLogger("py.warnings").setLevel(logging.ERROR)
logging.captureWarnings(True)
import warnings
warnings.simplefilter("ignore")
import httpx
from unittest.mock import MagicMock, patch

os.environ["IMEDNET_SIMULATION_MODE"] = "true"
os.environ["IMEDNET_API_KEY"] = "dummy_api_key"  # pragma: allowlist secret
os.environ["IMEDNET_SECURITY_KEY"] = "dummy_security_key"  # pragma: allowlist secret
os.environ["SNOWFLAKE_PASSWORD"] = "dummy_password"  # pragma: allowlist secret
os.environ["IMEDNET_STUDY_KEY"] = "MY_STUDY"

__file__ = "/a/b/c/d/e/dummy.py"

import duckdb
_duckdb_connect_patcher = patch('duckdb.connect', return_value=MagicMock())
_duckdb_connect_patcher.start()

from imednet_workflows.config_version_control import ConfigVersionStore
_cvs_patcher1 = patch.object(ConfigVersionStore, 'diff_configs', return_value={"added": [], "removed": [], "changed": []})
_cvs_patcher1.start()
_cvs_patcher2 = patch.object(ConfigVersionStore, 'commit_config', return_value="mock_commit_id")
_cvs_patcher2.start()
_cvs_patcher3 = patch.object(ConfigVersionStore, 'rollback_config', return_value={})
_cvs_patcher3.start()
_cvs_patcher4 = patch.object(ConfigVersionStore, 'get_history', return_value=[{"version_tag": "v1", "commit_id": "mock_commit_id", "timestamp": "2026-01-01"}])
_cvs_patcher4.start()

import imednet.integrations.export
_export_patcher = patch.object(imednet.integrations.export, '_prepare_export_df', return_value=sys.modules['pandas'].DataFrame())
_export_patcher.start()

from imednet_workflows.register_subjects import RegisterSubjectsWorkflow
_reg_patcher = patch.object(RegisterSubjectsWorkflow, 'register_subjects', return_value={'batch_id': 'MOCK_BATCH'})
_reg_patcher.start()

from unittest.mock import MagicMock, patch
import builtins

_original_print = builtins.print
def _mock_print(*args, **kwargs):
    if args and str(args[0]) in ("True", "2020", "5", "'1.23'", "['x']", "{}"):
        _original_print(*args, **kwargs)

_print_patcher = patch.object(builtins, 'print', side_effect=_mock_print)
_print_patcher.start()

sys.modules["snowflake"] = MagicMock()
sys.modules["snowflake.connector"] = MagicMock()
sys.modules["pymongo"] = MagicMock()
sys.modules["pymongo.errors"] = MagicMock()
sys.modules["neo4j"] = MagicMock()

import imednet.integrations.sink_base
_snowflake_patcher = patch.object(imednet.integrations.sink_base, '_require_optional_dep', return_value=MagicMock())
_snowflake_patcher.start()

import imednet_sinks.warehouse
_wh_patcher = patch.object(imednet_sinks.warehouse, '_require_optional_dep', return_value=MagicMock())
_wh_patcher.start()

import imednet_sinks.document
_doc_patcher = patch.object(imednet_sinks.document, '_require_optional_dep', return_value=MagicMock())
_doc_patcher.start()

import imednet_sinks.graph
_graph_patcher = patch.object(imednet_sinks.graph, '_require_optional_dep', return_value=MagicMock())
_graph_patcher.start()

# Mock airflow and provider
import sys
from unittest.mock import MagicMock
sys.modules['airflow'] = MagicMock()
sys.modules['airflow.decorators'] = MagicMock()
sys.modules['airflow.sdk'] = MagicMock()
sys.modules['apache_airflow_providers_imednet'] = MagicMock()


commit_a = "mock_commit_a"
commit_b = "mock_commit_b"
commit_id = "mock_commit_id"
subject_data = {}
api_response = {}

"""

doctest_global_cleanup = """
_duckdb_connect_patcher.stop()
_cvs_patcher1.stop()
_cvs_patcher2.stop()
_cvs_patcher3.stop()
_cvs_patcher4.stop()
_export_patcher.stop()
_reg_patcher.stop()
_snowflake_patcher.stop()
_wh_patcher.stop()
_doc_patcher.stop()
_graph_patcher.stop()
_print_patcher.stop()
"""

html_theme = "furo"
html_title = f"{project} Documentation"
html_baseurl = "https://fderuiter.github.io/imednet-toolkit/"
html_logo = "_static/logo.png"
html_favicon = "_static/logo.png"

html_theme_options: dict[str, Any] = {
    "source_repository": "https://github.com/fderuiter/imednet-toolkit",
    "source_branch": "main",
    "source_directory": "docs/",
    "light_css_variables": {
        "color-brand-primary": "#005fb8",
        "color-brand-content": "#005fb8",
        "color-brand-visited": "#5c3d99",
        "color-brand-btn-text": "#ffffff",
        "color-brand-button-text": "#ffffff",
        "color-status-success": "#065f46",
        "color-status-warning": "#92400e",
        "color-status-danger": "#b91c1c",
        "color-badge-success": "#065f46",
        "color-badge-warning": "#92400e",
        "color-badge-danger": "#b91c1c",
        "color-badge-required": "#b91c1c",
    },
    "dark_css_variables": {
        "color-brand-primary": "#58a6ff",
        "color-brand-content": "#79c0ff",
        "color-brand-visited": "#c4a0ff",
        "color-brand-btn-text": "#131416",
        "color-brand-button-text": "#131416",
        "color-status-success": "#34d399",
        "color-status-warning": "#fbbf24",
        "color-status-danger": "#f87171",
        "color-badge-success": "#34d399",
        "color-badge-warning": "#fbbf24",
        "color-badge-danger": "#f87171",
        "color-badge-required": "#f87171",
    },
}

master_doc = 'index'

# Linkcheck configuration
linkcheck_ignore = [
    r"https://.*\.imednet\.com.*",
    r"https://cdn\.jsdelivr\.net/.*",
    r"https://unpkg\.com/.*",
]
linkcheck_timeout = 30
linkcheck_retries = 3
