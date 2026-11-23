"""Sphinx configuration."""

# Configuration file for the Sphinx documentation builder.
import ast
import functools
import html
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
import re
import sys
import tomllib
import types
import urllib.parse

# mypy: ignore-errors
from collections.abc import AsyncIterator, Iterator, Mapping
from collections.abc import Set as AbstractSet
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar, get_args, get_origin, get_type_hints

import pydantic._internal._utils as _pydantic_internal_utils
from pydantic import BaseModel, RootModel
from sphinx.util.nodes import make_refnode

if not hasattr(_pydantic_internal_utils, "MappingIntStrAny"):
    _pydantic_internal_utils.MappingIntStrAny = Mapping[int, Any] | Mapping[str, Any]
if not hasattr(_pydantic_internal_utils, "AbstractSetIntStr"):
    _pydantic_internal_utils.AbstractSetIntStr = AbstractSet[int] | AbstractSet[str]

# Add package source roots and local Sphinx extensions so API modules and directives resolve.
_DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_DOCS_DIR, ".."))
sys.path[:0] = [
    os.path.join(_DOCS_DIR, "_ext"),
    os.path.abspath("../packages/core/src"),
    os.path.abspath("../packages/providers-airflow/src"),
    os.path.abspath("../packages/plugins-workflows/src"),
    os.path.abspath("../packages/plugins-sinks/src"),
    os.path.abspath("../packages/plugins-tui/src"),
]

from api_reference import (
    build_pydantic_field_specs,
    format_rst_annotation,
    get_mock_roots,
    is_noindex_options,
    should_skip_structured_or_inherited_member,
    upsert_attribute_directives,
)

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
    "sphinx.ext.autosummary",
    "sphinxcontrib.mermaid",
    "sphinxarg.ext",
    "sphinx.ext.doctest",
    "myst_parser",
    "sphinx.ext.githubpages",
    "pyodide_cells",
    "api_reference",
]

# Configure Mermaid 10.2.0 with hex-only themeVariables (khroma-compatible)
# and Furo --color-* custom properties in themeCSS so light/dark/high-contrast
# toggles apply immediately without JavaScript re-renders.
_MERMAID_THEME_CSS = """
.node rect,
.node circle,
.node ellipse,
.node polygon,
.node path {
  fill: var(--color-diagram-node-bg, var(--color-background-secondary)) !important;
  stroke: var(--color-diagram-node-border, var(--color-brand-primary)) !important;
}
.cluster rect {
  fill: var(--color-diagram-cluster-bg, var(--color-background-secondary)) !important;
  stroke: var(--color-diagram-line, var(--color-foreground-secondary)) !important;
}
.node .label,
.nodeLabel,
.cluster-label .nodeLabel,
.label text,
.label span,
.cluster-label span {
  color: var(--color-foreground-primary) !important;
  fill: var(--color-foreground-primary) !important;
}
.edgePath .path,
.flowchart-link {
  stroke: var(--color-diagram-line, var(--color-foreground-secondary)) !important;
}
.marker,
marker path {
  fill: var(--color-diagram-line, var(--color-foreground-secondary)) !important;
  stroke: var(--color-diagram-line, var(--color-foreground-secondary)) !important;
}
.edgeLabel,
.edgeLabel span,
.edgeLabel p {
  background-color: var(--color-diagram-label-bg, var(--color-background-primary)) !important;
  color: var(--color-foreground-primary) !important;
}
.edgeLabel rect {
  fill: var(--color-diagram-label-bg, var(--color-background-primary)) !important;
  opacity: 1 !important;
}
rect.actor,
.actor-man line,
.actor-man circle {
  fill: var(--color-diagram-node-bg, var(--color-background-secondary)) !important;
  stroke: var(--color-diagram-node-border, var(--color-brand-primary)) !important;
}
text.actor,
text.actor > tspan {
  fill: var(--color-foreground-primary) !important;
}
line[id^="actor"],
.actor-line {
  stroke: var(--color-diagram-line, var(--color-foreground-secondary)) !important;
}
.messageLine0,
.messageLine1,
.loopLine {
  stroke: var(--color-diagram-line, var(--color-foreground-secondary)) !important;
}
.messageText,
.loopText,
.loopText > tspan,
.labelText,
.labelText > tspan {
  fill: var(--color-foreground-primary) !important;
}
.labelBox {
  fill: var(--color-diagram-node-bg, var(--color-background-secondary)) !important;
  stroke: var(--color-diagram-line, var(--color-foreground-secondary)) !important;
}
rect.note {
  fill: var(--color-diagram-note-bg, #fff8c5) !important;
  stroke: var(--color-diagram-note-border, #92400e) !important;
}
text.noteText,
text.noteText > tspan {
  fill: var(--color-diagram-note-text, #1f2328) !important;
}
.activation0,
.activation1,
.activation2 {
  fill: var(--color-diagram-cluster-bg, var(--color-background-secondary)) !important;
  stroke: var(--color-diagram-node-border, var(--color-brand-primary)) !important;
}
"""


def _resolve_mermaid_version(dist_version: str | None = None) -> str:
    """Return a Mermaid JS version compatible with the installed sphinxcontrib-mermaid."""
    try:
        raw_version = (
            dist_version
            if dist_version is not None
            else importlib.metadata.version("sphinxcontrib-mermaid")
        )
        major = int(raw_version.split(".", 1)[0])
    except Exception:
        major = 0
    return "10.2.0" if major < 1 else "11.12.1"


_MERMAID_INIT_CONFIG: dict[str, Any] = {
    "startOnLoad": True,
    "theme": "base",
    "useMaxWidth": False,
    "flowchart": {"useMaxWidth": False, "htmlLabels": True},
    "sequence": {"useMaxWidth": False},
    "themeVariables": {
        "background": "#ffffff",
        "primaryColor": "#f8f9fa",
        "primaryTextColor": "#1f2328",
        "primaryBorderColor": "#005fb8",
        "lineColor": "#4a5056",
        "secondaryColor": "#eef2f6",
        "tertiaryColor": "#f8f9fa",
        "mainBkg": "#f8f9fa",
        "nodeBorder": "#005fb8",
        "clusterBkg": "#f0f3f6",
        "clusterBorder": "#4a5056",
        "titleColor": "#1f2328",
        "edgeLabelBackground": "#ffffff",
        "actorBkg": "#f8f9fa",
        "actorBorder": "#005fb8",
        "actorTextColor": "#1f2328",
        "actorLineColor": "#4a5056",
        "signalColor": "#4a5056",
        "signalTextColor": "#1f2328",
        "labelBoxBkgColor": "#f8f9fa",
        "labelBoxBorderColor": "#4a5056",
        "labelTextColor": "#1f2328",
        "loopTextColor": "#1f2328",
        "noteBkgColor": "#fff8c5",
        "noteTextColor": "#1f2328",
        "noteBorderColor": "#92400e",
        "activationBkgColor": "#eef2f6",
        "activationBorderColor": "#005fb8",
    },
    "themeCSS": _MERMAID_THEME_CSS,
}

mermaid_version = _resolve_mermaid_version()
mermaid_init_config = _MERMAID_INIT_CONFIG
mermaid_init_js = "mermaid.initialize(" + json.dumps(_MERMAID_INIT_CONFIG) + ");"

_PYODIDE_WHEEL_SPECS: tuple[tuple[str, str], ...] = (
    ("imednet", "core"),
    ("imednet_workflows", "plugins-workflows"),
    ("imednet_plugins_sinks", "plugins-sinks"),
)


def _resolve_pyodide_wheels() -> list[str]:
    """Resolve dynamic browser wheel paths in installation order."""
    wheels_dir = Path(_DOCS_DIR) / "_static" / "wheels"
    resolved: list[str] = []
    for dist_prefix, pkg_dir_name in _PYODIDE_WHEEL_SPECS:
        staged_matches = (
            sorted(wheels_dir.glob(f"{dist_prefix}-*.whl")) if wheels_dir.is_dir() else []
        )
        if staged_matches:
            resolved.append(f"_static/wheels/{staged_matches[-1].name}")
            continue

        pyproject_file = Path(_REPO_ROOT) / "packages" / pkg_dir_name / "pyproject.toml"
        pkg_version = imednet_version
        if pyproject_file.is_file():
            with pyproject_file.open("rb") as fh:
                pkg_version = str(
                    tomllib.load(fh).get("project", {}).get("version", imednet_version)
                )
        resolved.append(f"_static/wheels/{dist_prefix}-{pkg_version}-py3-none-any.whl")
    return resolved


# Configure client-side Pyodide WebAssembly execution for interactive tutorials
pyodide_config = {
    "selector": "div.highlight-python",
    "pyodide_packages": [
        "micropip",
        "pydantic",
        "cryptography",
        "pandas",
        "ssl",
        "sqlite3",
    ],
    "requirements": [
        "httpx",
        "tenacity",
        "defusedxml",
        "python-dotenv",
        "filelock",
        "python-json-logger",
    ],
    "wheels": _resolve_pyodide_wheels(),
    "env": {
        "IMEDNET_SIMULATION_MODE": "true",
        "IMEDNET_API_KEY": "dummy_api_key",  # pragma: allowlist secret
        "IMEDNET_SECURITY_KEY": "dummy_security_key",  # pragma: allowlist secret
        "SNOWFLAKE_PASSWORD": "dummy_password",  # pragma: allowlist secret
        "IMEDNET_STUDY_KEY": "MY_STUDY",
    },
}

autosummary_generate = False

# Mock heavy optional dependencies so autodoc does not import them.
autodoc_mock_imports = [
    "matplotlib",
    "opentelemetry",
]

suppress_warnings = [
    "autodoc.import",
    "autodoc",
]

_DOCS_PATH = Path(_DOCS_DIR).resolve()
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
    "BaseModel, ImednetBaseModel, RootModel, Enum, str, int, Exception, BaseException, object"
)
_INTERNAL_ENDPOINT_MEMBERS = frozenset(
    {
        "PARAM_PROCESSOR",
        "PARAM_PROCESSOR_CLS",
        "STUDY_KEY_STRATEGY",
        "param_processor",
        "study_key_strategy",
    }
)
_OPERATION_TYPE_NAMES = frozenset(
    {
        "Operation",
        "ListOperation",
        "FilterGetOperation",
        "PathGetOperation",
        "RecordCreateOperation",
        "ListBySiteOperation",
    }
)
_AUTOMODULE_RE = re.compile(r"^\.\.\s+automodule::\s+([A-Za-z0-9_.]+)", re.MULTILINE)
_PY_ATTR_DIRECTIVE_RE = re.compile(r"^\.\.\s+(?:py:)?attribute::\s+([A-Za-z0-9_]+)")


@functools.cache
def _get_documented_modules() -> frozenset[str]:
    """Return all canonical module names documented via .. automodule:: in docs/reference/api/."""
    mods: set[str] = set()
    api_dir = _DOCS_PATH / "reference" / "api"
    search_root = api_dir if api_dir.exists() else _DOCS_PATH
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


def _find_canonical_documented_module(
    canon_mod: str,
    canon_name: str,
    documented_modules: frozenset[str],
) -> str | None:
    """Return the canonical documented module exposing (canon_mod, canon_name), if any."""
    if canon_mod in documented_modules and _is_exposed_by_documented_module(canon_mod, canon_name):
        return canon_mod
    if (
        canon_mod == "imednet.core.paginator"
        and "imednet.pagination" in documented_modules
        and _is_exposed_by_documented_module("imednet.pagination", canon_name)
    ):
        return "imednet.pagination"
    parts = canon_mod.split(".")
    for idx in range(len(parts) - 1, 1, -1):
        parent_mod = ".".join(parts[:idx])
        if parent_mod in documented_modules and _is_exposed_by_documented_module(
            parent_mod, canon_name
        ):
            return parent_mod
    return None


def _is_undocumented_internal_type(tp: Any) -> bool:
    """Return True if tp is a private class or an undocumented internal imednet.core type."""
    if not isinstance(tp, type):
        return False
    cls_name = getattr(tp, "__name__", "")
    mod_name = getattr(tp, "__module__", "") or ""
    if cls_name.startswith("_"):
        return True
    if mod_name.startswith("imednet.core."):
        documented_modules = _get_documented_modules()
        return _find_canonical_documented_module(mod_name, cls_name, documented_modules) is None
    return False


def _has_undocumented_type_arg(arg: Any) -> bool:
    """Return True if a generic type argument or TypeVar bound is undocumented internal."""
    if isinstance(arg, TypeVar):
        bound = getattr(arg, "__bound__", None)
        return _is_undocumented_internal_type(bound) if bound is not None else False
    origin = get_origin(arg)
    if origin is not None:
        if _is_undocumented_internal_type(origin):
            return True
        return any(_has_undocumented_type_arg(sub) for sub in get_args(arg))
    return _is_undocumented_internal_type(arg)


def _unwrap_shadowed_bases(app: Any, name: str, obj: Any, options: Any, bases: list[Any]) -> None:
    """Replace shadowed or undocumented internal base classes with clean public bases."""
    if not isinstance(obj, type):
        return

    unwrapped: list[Any] = []
    seen: set[Any] = set()

    def _add(candidate: Any) -> None:
        if candidate is object or candidate is Generic or candidate in seen:
            return
        seen.add(candidate)
        unwrapped.append(candidate)

    def _expand(base: Any) -> None:
        origin = get_origin(base)
        if origin is Generic or base is Generic:
            return
        if origin is not None and isinstance(origin, type):
            if _is_undocumented_internal_type(origin):
                return
            args = get_args(base)
            if any(_has_undocumented_type_arg(a) for a in args):
                _add(origin)
            else:
                _add(base)
            return
        if isinstance(base, type):
            if base is not obj and base.__name__ == obj.__name__:
                for parent in base.__bases__:
                    if parent is not object:
                        _expand(parent)
                return
            if _is_undocumented_internal_type(base):
                return
        _add(base)

    for b in list(bases):
        _expand(b)
    if not unwrapped:
        type_params = getattr(obj, "__parameters__", ())
        unwrapped = [Generic[type_params]] if type_params else [object]
    bases[:] = unwrapped


def _format_rst_annotation(annotation: Any) -> str:
    """Format a runtime annotation into PEP 604 RST with qualified first-party type targets."""
    return format_rst_annotation(annotation, qualify_first_party=True, short_prefix=True)


def _emit_pydantic_model_fields(
    app: Any,
    what: str,
    name: str,  # noqa: ARG001
    obj: Any,
    options: Any,
    lines: list[str],
) -> None:
    """Emit .. py:attribute:: blocks with :type: and :value:/Required for Pydantic v2 model_fields."""
    if what != "class" or not isinstance(obj, type):
        return
    if not issubclass(obj, BaseModel) or obj in (BaseModel, RootModel):
        return

    specs = build_pydantic_field_specs(obj, mock_roots=get_mock_roots(app))
    upsert_attribute_directives(lines, specs, noindex=is_noindex_options(options))


def _find_defining_class(cls: type, member_name: str) -> type | None:
    """Find the first class in cls.__mro__ that defines member_name."""
    for base in cls.__mro__:
        if member_name in base.__dict__:
            return base
        anns = base.__dict__.get("__annotations__", {})
        if isinstance(anns, dict) and member_name in anns:
            return base
    return None


def _resolve_owner_class(app: Any, full_name: str | None = None) -> type | None:
    """Resolve the currently documented owner class from Sphinx autodoc temp_data or full_name."""
    env = getattr(app, "env", None)
    temp_data = getattr(env, "temp_data", {}) if env is not None else {}
    current_mod = temp_data.get("autodoc:module")
    current_cls_name = temp_data.get("autodoc:class")
    if current_mod and current_cls_name:
        mod = sys.modules.get(current_mod)
        cls = getattr(mod, current_cls_name, None) if mod is not None else None
        if isinstance(cls, type):
            return cls
    if full_name and "." in full_name:
        parent_path, _ = full_name.rsplit(".", 1)
        if "." in parent_path:
            mod_part, cls_part = parent_path.rsplit(".", 1)
            mod = sys.modules.get(mod_part)
            cls = getattr(mod, cls_part, None) if mod is not None else None
            if isinstance(cls, type):
                return cls
    return None


def _normalize_operation_return_type(ret: Any, owner_cls: type | None) -> Any:
    """Map internal Operation[T] annotations to Iterator[T], AsyncIterator[T], or T."""
    from imednet.core.endpoint.dispatch import AsyncEndpointContext

    is_async_owner = isinstance(owner_cls, type) and issubclass(owner_cls, AsyncEndpointContext)
    raw_model = getattr(owner_cls, "MODEL", None) if owner_cls is not None else None
    model_cls = raw_model if isinstance(raw_model, type) else None

    if isinstance(ret, str):
        stripped = ret.strip()
        op_head = stripped.split("[", 1)[0].rsplit(".", 1)[-1].strip()
        if op_head in _OPERATION_TYPE_NAMES:
            inner_fallback: Any = model_cls if model_cls is not None else Any
            if op_head == "ListOperation":
                return AsyncIterator[inner_fallback] if is_async_owner else Iterator[inner_fallback]
            return inner_fallback
        return ret

    origin = get_origin(ret)
    target_tp = origin if origin is not None else ret
    tp_name = getattr(target_tp, "__name__", "")
    if tp_name not in _OPERATION_TYPE_NAMES:
        if isinstance(ret, TypeVar) and model_cls is not None:
            return model_cls
        return ret

    args = get_args(ret)
    if args:
        inner = model_cls if (isinstance(args[0], TypeVar) and model_cls is not None) else args[0]
    elif isinstance(target_tp, type) and hasattr(target_tp, "execute_sync"):
        try:
            resolved_ret = get_type_hints(target_tp.execute_sync).get("return", Any)
        except Exception:
            resolved_ret = Any
        inner = (
            model_cls
            if (isinstance(resolved_ret, TypeVar) and model_cls is not None)
            else resolved_ret
        )
    else:
        inner = model_cls if model_cls is not None else Any

    if tp_name == "ListOperation":
        return AsyncIterator[inner] if is_async_owner else Iterator[inner]
    return inner


def _resolve_callable_return_hint(obj: Any) -> Any:
    """Resolve PEP 563 stringified return annotations (including closure TypeVars) on callables."""
    anns = getattr(obj, "__annotations__", None)
    if not isinstance(anns, dict) or "return" not in anns:
        return None
    raw_ret = anns["return"]
    if not isinstance(raw_ret, str):
        return raw_ret

    localns: dict[str, Any] = {}
    code = getattr(obj, "__code__", None)
    closure = getattr(obj, "__closure__", None)
    if code is not None and closure is not None:
        for var_name, cell in zip(code.co_freevars, closure, strict=False):
            try:
                localns[var_name] = cell.cell_contents
            except ValueError:
                continue

    try:
        return get_type_hints(obj, localns=localns or None).get("return", raw_ret)
    except Exception:
        return raw_ret


def _before_process_endpoint_signature(app: Any, obj: Any, bound_method: bool) -> None:  # noqa: ARG001
    """Normalize endpoint Operation return annotations before autodoc signature formatting runs."""
    if not callable(obj) or inspect.isclass(obj):
        return
    anns = getattr(obj, "__annotations__", None)
    if not isinstance(anns, dict) or "return" not in anns:
        return
    resolved_ret = _resolve_callable_return_hint(obj)
    owner_cls = _resolve_owner_class(app)
    normalized = _normalize_operation_return_type(resolved_ret, owner_cls)
    if normalized is not anns["return"]:
        updated_anns = dict(anns)
        updated_anns["return"] = normalized
        obj.__annotations__ = updated_anns
        try:
            sig = inspect.signature(obj)
            obj.__signature__ = sig.replace(return_annotation=normalized)
        except (TypeError, ValueError):
            pass


def _process_endpoint_signature(
    app: Any,
    what: str,
    name: str,
    obj: Any,
    options: Any,  # noqa: ARG001
    signature: str | None,
    return_annotation: str | None,
) -> tuple[str | None, str | None] | None:
    """Rewrite residual Operation return strings on endpoint methods during autodoc signature hook."""
    if what != "method" or not return_annotation:
        return None
    for op_name in _OPERATION_TYPE_NAMES:
        if op_name in return_annotation:
            owner_cls = _resolve_owner_class(app, name)
            resolved_ret = _resolve_callable_return_hint(obj) if callable(obj) else None
            if resolved_ret is not None:
                normalized = _normalize_operation_return_type(resolved_ret, owner_cls)
                return signature, _format_rst_annotation(normalized)
            return signature, None
    return None


def _resolve_reexported_py_reference(app: Any, env: Any, node: Any, contnode: Any) -> Any:
    """Resolve internal imednet.core.* cross-references to curated imednet.core or imednet.pagination anchors."""
    if node.get("refdomain") != "py" or not hasattr(app.builder, "get_relative_uri"):
        return None
    reftarget = node.get("reftarget", "")
    if not isinstance(reftarget, str) or not reftarget:
        return None

    py_objects = env.domaindata.get("py", {}).get("objects", {})
    if not py_objects:
        return None

    alias_rewrites = {
        "EdcGenericListGetEndpoint": "EdcSyncListGetEndpoint",
        "EdcGenericGetEndpoint": "EdcSyncGetEndpoint",
        "EdcGenericListEndpoint": "EdcSyncListEndpoint",
    }

    candidates: list[str] = []
    if reftarget == "imednet.core.context.study_context":
        candidates.append("imednet.sdk.ImednetSDK.study_context")
    elif reftarget.startswith("imednet.core.paginator."):
        tail = reftarget.removeprefix("imednet.core.paginator.")
        candidates.extend([f"imednet.pagination.{tail}", f"imednet.core.{tail}"])
    elif reftarget.startswith("imednet.core."):
        parts = reftarget.split(".")
        for idx in range(2, len(parts)):
            tail_parts = list(parts[idx:])
            if tail_parts[0] in alias_rewrites:
                tail_parts[0] = alias_rewrites[tail_parts[0]]
            tail = ".".join(tail_parts)
            candidates.extend([f"imednet.core.{tail}", f"imednet.pagination.{tail}"])
    elif "." not in reftarget or reftarget.split(".")[0][0].isupper():
        head, dot, rest = reftarget.partition(".")
        mapped_head = alias_rewrites.get(head, head)
        norm_target = f"{mapped_head}{dot}{rest}"
        for prefix in ("imednet.core", "imednet.pagination", "imednet.sdk", "imednet.endpoints"):
            candidates.append(f"{prefix}.{norm_target}")

    for cand in candidates:
        entry = py_objects.get(cand)
        if entry is not None:
            return make_refnode(
                app.builder,
                node["refdoc"],
                entry.docname,
                entry.node_id,
                contnode,
                cand,
            )
    return None


def _skip_non_canonical_or_internal(
    app: Any,
    what: str,
    name: str,
    obj: Any,
    skip: bool,
    options: Any,  # noqa: ARG001
) -> bool | None:
    """Skip non-canonical module re-exports and external/internal inherited class members."""
    env = getattr(app, "env", None)
    temp_data = getattr(env, "temp_data", {}) if env is not None else {}
    current_mod = temp_data.get("autodoc:module")
    current_cls_name = temp_data.get("autodoc:class")

    if what in ("class", "exception") and current_mod and current_cls_name and name == "__init__":
        mod = sys.modules.get(current_mod)
        cls = getattr(mod, current_cls_name, None) if mod is not None else None
        if isinstance(cls, type):
            _, _, cls_ast_map = _analyze_module_symbols(current_mod)
            local_ast_members = cls_ast_map.get(current_cls_name)
            init_decision = should_skip_structured_or_inherited_member(
                cls, "__init__", local_ast_members
            )
            if init_decision is not None:
                return init_decision

    if skip:
        return None

    if what == "module" and current_mod:
        if isinstance(obj, types.ModuleType):
            if getattr(obj, "__name__", None) != current_mod:
                return True
            return None

        documented_modules = _get_documented_modules()
        canon_mod, canon_name = _resolve_canonical_origin(current_mod, name)
        if canon_mod != current_mod and canon_mod.split(".")[0] not in _FIRST_PARTY_ROOTS:
            return True
        doc_home = _find_canonical_documented_module(canon_mod, canon_name, documented_modules)
        if doc_home is not None and doc_home != current_mod:
            return True

        origin_mod = getattr(obj, "__module__", None)
        if isinstance(origin_mod, str) and origin_mod != current_mod:
            origin_root = origin_mod.split(".")[0]
            if (isinstance(obj, type) or callable(obj)) and origin_root not in _FIRST_PARTY_ROOTS:
                return True
            obj_doc_home = _find_canonical_documented_module(
                origin_mod, getattr(obj, "__name__", name), documented_modules
            )
            if obj_doc_home is not None and obj_doc_home != current_mod:
                return True
        return None

    if what in ("class", "exception") and current_mod and current_cls_name:
        if name in _INTERNAL_ENDPOINT_MEMBERS:
            return True

        mod = sys.modules.get(current_mod)
        cls = getattr(mod, current_cls_name, None) if mod is not None else None
        if isinstance(cls, type):
            _, _, cls_ast_map = _analyze_module_symbols(current_mod)
            local_ast_members = cls_ast_map.get(current_cls_name)
            structured_skip = should_skip_structured_or_inherited_member(
                cls, name, local_ast_members
            )
            if structured_skip is not None:
                return structured_skip

            if name in ("__iter__", "__aiter__") and issubclass(
                cls, (BaseModel, Enum, dict, list, tuple, set, frozenset, str, bytes)
            ):
                return True

            if issubclass(cls, BaseModel):
                if name in _PYDANTIC_INTERNAL_ATTRS:
                    return True
                model_fields = getattr(cls, "model_fields", {})
                if isinstance(model_fields, dict) and name in model_fields:
                    return True

            defining_cls = _find_defining_class(cls, name)
            if defining_cls is cls:
                if local_ast_members is not None and name not in local_ast_members:
                    for parent in cls.__mro__[1:]:
                        if name in parent.__dict__:
                            parent_mod = getattr(parent, "__module__", "") or ""
                            if parent_mod.split(".")[0] not in _FIRST_PARTY_ROOTS:
                                return True
                            break
            elif defining_cls is not None:
                if (
                    defining_cls
                    in (
                        BaseModel,
                        RootModel,
                        Enum,
                        object,
                        Exception,
                        BaseException,
                    )
                    or getattr(defining_cls, "__name__", "") == "ImednetBaseModel"
                ):
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


_DEFAULT_SITE_DESCRIPTION = (
    "Unofficial Python SDK, CLI, workflow plugins, TUI, and Apache Airflow provider "
    "for the iMednet clinical trials EDC API."
)
_SPECIAL_PAGE_TITLES = {
    "genindex": "Index",
    "py-modindex": "Python Module Index",
    "search": "Search",
    "_modules/index": "Module Code",
}
_META_DESC_RE = re.compile(
    r"<meta\s+(?:name=[\"']description[\"']\s+content=[\"'](?P<c1>[^\"']*)[\"']"
    r"|content=[\"'](?P<c2>[^\"']*)[\"']\s+name=[\"']description[\"'])\s*/?>",
    re.IGNORECASE,
)


def _strip_html(raw: str) -> str:
    """Convert an HTML fragment into normalized plain text."""
    text = re.sub(r"<[^>]+>", "", raw)
    return " ".join(html.unescape(text).split())


def _extract_doctree_description(doctree: Any) -> str:
    """Extract a concise plain-text summary from the first body paragraph in doctree."""
    if doctree is None:
        return ""
    for node in doctree.findall():
        if node.__class__.__name__ != "paragraph":
            continue
        parent_name = getattr(getattr(node, "parent", None), "__class__", type(None)).__name__
        if parent_name in {"note", "warning", "admonition", "tip", "important", "caution"}:
            continue
        text = " ".join(node.astext().split())
        if text:
            if len(text) > 200:
                return text[:197].rstrip() + "..."
            return text
    return ""


def _resolve_page_title(pagename: str, context: dict[str, Any], site_title: str) -> str:
    """Resolve a clean plain-text page title for social metadata tags."""
    raw_title = context.get("title")
    if isinstance(raw_title, str) and raw_title.strip():
        cleaned = _strip_html(raw_title)
        if cleaned:
            return site_title if pagename == master_doc else f"{cleaned} — {site_title}"
    if pagename in _SPECIAL_PAGE_TITLES:
        return f"{_SPECIAL_PAGE_TITLES[pagename]} — {site_title}"
    if pagename.startswith("_modules/"):
        mod_name = pagename.removeprefix("_modules/").replace("/", ".")
        return f"Source code for {mod_name} — {site_title}"
    fallback_slug = pagename.rsplit("/", maxsplit=1)[-1].replace("_", " ").replace("-", " ").title()
    return f"{fallback_slug} — {site_title}"


def _canonical_page_url(base_url: str, pagename: str) -> str:
    """Build the canonical public URL for a given Sphinx page name."""
    normalized_base = base_url.rstrip("/") + "/"
    if pagename == master_doc:
        return normalized_base
    return f"{normalized_base}{pagename}.html"


def _inject_seo_metadata(
    app: Any,
    pagename: str,
    templatename: str,  # noqa: ARG001
    context: dict[str, Any],
    doctree: Any,
) -> None:
    """Inject description, OpenGraph, and Twitter Card metadata into HTML pages."""
    base_url = getattr(app.config, "html_baseurl", "") or html_baseurl
    normalized_base = base_url.rstrip("/") + "/"
    site_title = getattr(app.config, "html_title", None) or f"{project} Documentation"

    existing_metatags = context.get("metatags") or ""
    match = _META_DESC_RE.search(existing_metatags)
    if match:
        raw_desc = html.unescape(match.group("c1") or match.group("c2") or "").strip()
        description = raw_desc or _DEFAULT_SITE_DESCRIPTION
        escaped_desc = html.escape(description, quote=True)
        existing_metatags = _META_DESC_RE.sub(
            f'<meta name="description" content="{escaped_desc}" />',
            existing_metatags,
            count=1,
        )
        has_meta_desc = True
    else:
        doctree_desc = _extract_doctree_description(doctree)
        page_label = _resolve_page_title(pagename, context, site_title)
        description = doctree_desc or f"{page_label}. {_DEFAULT_SITE_DESCRIPTION}"
        has_meta_desc = False

    full_title = _resolve_page_title(pagename, context, site_title)
    canonical_url = _canonical_page_url(normalized_base, pagename)
    logo_rel = (getattr(app.config, "html_logo", None) or "_static/logo.png").lstrip("/")
    image_url = f"{normalized_base}{logo_rel}"

    esc_title = html.escape(full_title, quote=True)
    esc_desc = html.escape(description, quote=True)
    esc_url = html.escape(canonical_url, quote=True)
    esc_image = html.escape(image_url, quote=True)
    esc_site = html.escape(site_title, quote=True)

    injected_lines: list[str] = []
    if not has_meta_desc:
        injected_lines.append(f'<meta name="description" content="{esc_desc}" />')
    injected_lines.extend(
        [
            f'<meta property="og:title" content="{esc_title}" />',
            f'<meta property="og:description" content="{esc_desc}" />',
            '<meta property="og:type" content="website" />',
            f'<meta property="og:url" content="{esc_url}" />',
            f'<meta property="og:image" content="{esc_image}" />',
            f'<meta property="og:site_name" content="{esc_site}" />',
            '<meta name="twitter:card" content="summary" />',
            f'<meta name="twitter:title" content="{esc_title}" />',
            f'<meta name="twitter:description" content="{esc_desc}" />',
            f'<meta name="twitter:image" content="{esc_image}" />',
        ]
    )
    prefix = (
        existing_metatags
        if existing_metatags.endswith("\n") or not existing_metatags
        else f"{existing_metatags}\n"
    )
    context["metatags"] = prefix + "\n".join(injected_lines) + "\n"


def _rewrite_not_found_assets(not_found_path: Path, base_path: str) -> None:
    """Rewrite relative asset and navigation URLs on 404.html so nested paths render cleanly."""
    if not not_found_path.exists():
        return
    prefix = "/" + base_path.strip("/") + "/" if base_path.strip("/") else "/"
    content = not_found_path.read_text(encoding="utf-8")

    # Anchor Sphinx content_root and DOCUMENTATION_OPTIONS.URL_ROOT for scripts/search
    content = re.sub(
        r'data-content_root="[^"]*"',
        f'data-content_root="{prefix}"',
        content,
    )
    content = re.sub(
        r"(URL_ROOT\s*:\s*)(['\"])(?:\./|)\2",
        rf"\g<1>\g<2>{prefix}\g<2>",
        content,
    )

    # Rewrite relative _static/ asset paths (including JSON config strings and tag attributes)
    content = re.sub(
        r'(?P<q>["\'])(?:\./)?_static/',
        rf"\g<q>{prefix}_static/",
        content,
    )

    # Rewrite remaining root-relative href/action links so sidebar and search work from nested 404 URLs
    def _anchor_attr(match: re.Match[str]) -> str:
        attr = match.group("attr")
        quote = match.group("q")
        target = match.group("target")
        if target.startswith(("/", "#", "http://", "https://", "mailto:", "javascript:", "data:")):
            return match.group(0)
        cleaned = target.removeprefix("./")
        return f"{attr}={quote}{prefix}{cleaned}{quote}"

    content = re.sub(
        r'(?P<attr>\b(?:href|action))=(?P<q>["\'])(?P<target>[^"\']+)(?P=q)',
        _anchor_attr,
        content,
    )
    not_found_path.write_text(content, encoding="utf-8")


def _on_build_finished(app: Any, exception: Exception | None) -> None:
    """Finalize HTML build by rewriting 404.html asset paths and emitting sitemap.xml and robots.txt."""
    if exception is not None or getattr(app.builder, "format", "") != "html":
        return

    outdir = Path(app.outdir)
    base_url = (getattr(app.config, "html_baseurl", "") or html_baseurl).rstrip("/") + "/"
    base_path = urllib.parse.urlsplit(base_url).path or "/"

    _rewrite_not_found_assets(outdir / "404.html", base_path)

    found_docs = sorted(doc for doc in getattr(app.env, "found_docs", set()) if doc != "404")
    sitemap_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for docname in found_docs:
        loc = html.escape(_canonical_page_url(base_url, docname), quote=False)
        sitemap_lines.append(f"  <url><loc>{loc}</loc></url>")
    sitemap_lines.append("</urlset>\n")
    (outdir / "sitemap.xml").write_text("\n".join(sitemap_lines), encoding="utf-8")

    robots_txt = f"User-agent: *\nAllow: /\n\nSitemap: {base_url}sitemap.xml\n"
    (outdir / "robots.txt").write_text(robots_txt, encoding="utf-8")


def setup(app: Any) -> dict[str, Any]:  # pragma: no cover
    """Register Sphinx autodoc, viewcode, and native SEO / GitHub Pages hooks.

    Args:
        app: The Sphinx application instance (provided by Sphinx at startup).
    """
    _get_documented_modules.cache_clear()
    _analyze_module_symbols.cache_clear()

    app.connect("autodoc-process-bases", _unwrap_shadowed_bases)
    app.connect(
        "autodoc-before-process-signature", _before_process_endpoint_signature, priority=400
    )
    app.connect("autodoc-process-signature", _process_endpoint_signature, priority=600)
    app.connect("autodoc-process-docstring", _emit_pydantic_model_fields, priority=600)
    app.connect("autodoc-skip-member", _skip_non_canonical_or_internal, priority=400)
    app.connect("autodoc-skip-member", _skip_non_canonical_or_internal, priority=600)
    app.connect("missing-reference", _resolve_reexported_py_reference)
    app.connect("html-collect-pages", _sanitize_viewcode_modules, priority=100)

    app.connect("html-page-context", _inject_seo_metadata)
    app.connect("build-finished", _on_build_finished)
    return {
        "version": "1.0.0",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }


# Display type hints in the description instead of the signature to keep
# function signatures concise in the rendered documentation.
autodoc_typehints = "description"

# Autodoc default options applied to all automodule/autoclass directives.
autodoc_default_options = {
    "members": True,
    "inherited-members": _SCOPED_INHERITED_EXCLUSIONS,
    "special-members": "__iter__, __aiter__, __call__",
    "show-inheritance": True,
}
autodoc_class_signature = "separated"

# Napoleon – enforce Google-style docstrings.
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True

# Templates and static paths
templates_path: list[str] = []
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
import builtins
import logging
import os
import sys
import tempfile
import warnings
from unittest.mock import MagicMock, patch

for _mod in [m for m in list(sys.modules) if m.startswith("apache_airflow_providers_imednet")]:
    del sys.modules[_mod]

warnings.filterwarnings("ignore")
warnings.simplefilter("ignore")
logging.getLogger("py.warnings").setLevel(logging.ERROR)
logging.captureWarnings(True)

os.environ["IMEDNET_SIMULATION_MODE"] = "true"
os.environ["IMEDNET_API_KEY"] = "dummy_api_key"  # pragma: allowlist secret
os.environ["IMEDNET_SECURITY_KEY"] = "dummy_security_key"  # pragma: allowlist secret
os.environ["SNOWFLAKE_PASSWORD"] = "dummy_password"  # pragma: allowlist secret
os.environ["IMEDNET_STUDY_KEY"] = "STUDY"
os.environ["AIRFLOW_CONN_IMEDNET_DEFAULT"] = "http://mock:mock@localhost"  # pragma: allowlist secret

_doctest_orig_cwd = os.getcwd()
_doctest_tmpdir = tempfile.TemporaryDirectory(prefix="imednet_doctest_")
os.chdir(_doctest_tmpdir.name)

from imednet.simulation.state import get_default_simulation_state
get_default_simulation_state().reset()

__file__ = "/a/b/c/d/e/dummy.py"

_mongo_client_patcher = patch("pymongo.MongoClient", return_value=MagicMock())
_mongo_client_patcher.start()

_neo4j_driver_patcher = patch("neo4j.GraphDatabase.driver", return_value=MagicMock())
_neo4j_driver_patcher.start()

_snowflake_connect_patcher = patch("snowflake.connector.connect", return_value=MagicMock())
_snowflake_connect_patcher.start()

_original_print = builtins.print


def _mock_print(*args, **kwargs):
    if kwargs.get("file") is not None:
        _original_print(*args, **kwargs)
        return
    if args and str(args[0]) in ("True", "2020", "5", "'1.23'", "['x']", "{}"):
        _original_print(*args, **kwargs)


_print_patcher = patch.object(builtins, "print", side_effect=_mock_print)
_print_patcher.start()
"""

doctest_global_cleanup = """
_mongo_client_patcher.stop()
_neo4j_driver_patcher.stop()
_snowflake_connect_patcher.stop()
_print_patcher.stop()
get_default_simulation_state().reset()
os.chdir(_doctest_orig_cwd)
_doctest_tmpdir.cleanup()
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
        "color-brand-primary-hover": "#004c94",
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
        "color-diagram-node-bg": "#f8f9fa",
        "color-diagram-node-border": "#005fb8",
        "color-diagram-cluster-bg": "#f0f3f6",
        "color-diagram-line": "#4a5056",
        "color-diagram-label-bg": "#ffffff",
        "color-diagram-note-bg": "#fff8c5",
        "color-diagram-note-text": "#1f2328",
        "color-diagram-note-border": "#92400e",
    },
    "dark_css_variables": {
        "color-brand-primary": "#58a6ff",
        "color-brand-primary-hover": "#79c0ff",
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
        "color-diagram-node-bg": "#1e2228",
        "color-diagram-node-border": "#58a6ff",
        "color-diagram-cluster-bg": "#17191d",
        "color-diagram-line": "#9ca3af",
        "color-diagram-label-bg": "#131416",
        "color-diagram-note-bg": "#2d2611",
        "color-diagram-note-text": "#fef3c7",
        "color-diagram-note-border": "#fbbf24",
    },
}

# WCAG 2.1 AAA (>= 7:1) high-contrast token definitions mirrored in
# docs/_static/custom.css under @media (prefers-contrast: more)
high_contrast_css_variables: dict[str, dict[str, str]] = {
    "light": {
        "color-background-primary": "#ffffff",
        "color-background-secondary": "#f4f6f8",
        "color-foreground-primary": "#000000",
        "color-foreground-secondary": "#1f2328",
        "color-foreground-muted": "#24292f",
        "color-brand-primary": "#004385",
        "color-brand-primary-hover": "#003366",
        "color-brand-content": "#004385",
        "color-brand-btn-text": "#ffffff",
        "color-status-success": "#065f46",
        "color-status-warning": "#78350f",
        "color-status-danger": "#991b1b",
        "color-diagram-node-bg": "#ffffff",
        "color-diagram-node-border": "#004385",
        "color-diagram-cluster-bg": "#f4f6f8",
        "color-diagram-line": "#1f2328",
        "color-diagram-label-bg": "#ffffff",
        "color-diagram-note-bg": "#fffbeb",
        "color-diagram-note-text": "#000000",
        "color-diagram-note-border": "#78350f",
    },
    "dark": {
        "color-background-primary": "#0a0c10",
        "color-background-secondary": "#11141a",
        "color-foreground-primary": "#ffffff",
        "color-foreground-secondary": "#e6edf3",
        "color-foreground-muted": "#c9d1d9",
        "color-brand-primary": "#79c0ff",
        "color-brand-primary-hover": "#a5d6ff",
        "color-brand-content": "#79c0ff",
        "color-brand-btn-text": "#0a0c10",
        "color-status-success": "#6ee7b7",
        "color-status-warning": "#fde68a",
        "color-status-danger": "#fca5a5",
        "color-diagram-node-bg": "#11141a",
        "color-diagram-node-border": "#79c0ff",
        "color-diagram-cluster-bg": "#0a0c10",
        "color-diagram-line": "#e6edf3",
        "color-diagram-label-bg": "#0a0c10",
        "color-diagram-note-bg": "#1c1908",
        "color-diagram-note-text": "#ffffff",
        "color-diagram-note-border": "#fde68a",
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
