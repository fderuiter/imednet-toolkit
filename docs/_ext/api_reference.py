"""Native Sphinx extension for API reference signatures, forward refs, and structured contracts."""

from __future__ import annotations

import ast
import builtins
import contextlib
import dataclasses
import functools
import importlib
import inspect
import os
import re
import sys
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    ClassVar,
    Concatenate,
    Final,
    ForwardRef,
    Literal,
    NotRequired,
    ParamSpec,
    ParamSpecArgs,
    ParamSpecKwargs,
    Required,
    TypeVar,
    TypeVarTuple,
    Union,
    get_args,
    get_origin,
    is_typeddict,
)

from docutils.statemachine import StringList
from pydantic import BaseModel, RootModel
from sphinx.ext.autodoc import ClassDocumenter, FunctionDocumenter, MethodDocumenter
from sphinx.pycode import ModuleAnalyzer
from sphinx.util.inspect import evaluate_signature, safe_getattr

FIRST_PARTY_ROOTS: frozenset[str] = frozenset(
    {
        "imednet",
        "imednet_workflows",
        "imednet_sinks",
        "imednet_tui",
        "apache_airflow_providers_imednet",
    }
)

_PY_ATTR_HEADER_RE = re.compile(r"^(\s*)\.\.\s+(?:py:)?attribute::\s+([A-Za-z0-9_]+)\s*$")
_DATACLASS_SYNTHETIC_DUNDERS: frozenset[str] = frozenset(
    {
        "__dataclass_fields__",
        "__dataclass_params__",
        "__eq__",
        "__ge__",
        "__gt__",
        "__hash__",
        "__le__",
        "__lt__",
        "__match_args__",
        "__replace__",
        "__repr__",
    }
)


class _DisplayDefault:
    """Wrapper rendering a pre-formatted default expression in inspect.Signature."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __repr__(self) -> str:
        return self._text

    def __str__(self) -> str:
        return self._text


@dataclass(frozen=True)
class AttributeSpec:
    """Specification for a structured class attribute directive."""

    name: str
    type_str: str
    value_str: str | None = None
    marker: str | None = None
    description: str = ""


def is_first_party_dataclass(cls: Any) -> bool:
    """Return True when cls is decorated directly with @dataclass (excluding external bases)."""
    return isinstance(cls, type) and "__dataclass_params__" in cls.__dict__


def is_typeddict_class(cls: Any) -> bool:
    """Return True for both typing.TypedDict and typing_extensions.TypedDict schemas."""
    if not isinstance(cls, type):
        return False
    if is_typeddict(cls):
        return True
    if cls.__module__ in ("typing", "typing_extensions") and cls.__name__ == "TypedDict":
        return False
    return (
        issubclass(cls, dict)
        and isinstance(getattr(cls, "__required_keys__", None), frozenset)
        and isinstance(getattr(cls, "__optional_keys__", None), frozenset)
    )


def is_protocol_class(cls: Any) -> bool:
    """Return True when cls is a typing.Protocol class (excluding typing.Protocol itself)."""
    if not isinstance(cls, type):
        return False
    if cls.__module__ in ("typing", "typing_extensions") and cls.__name__ == "Protocol":
        return False
    return bool(getattr(cls, "_is_protocol", False))


def get_protocol_bare_annotations(cls: type) -> dict[str, tuple[str, Any]]:
    """Return bare attribute annotations on a Protocol class (excluding properties/methods)."""
    if not is_protocol_class(cls):
        return {}
    collected: dict[str, tuple[str, Any]] = {}
    for base in reversed(cls.__mro__):
        if not is_protocol_class(base):
            continue
        anns = base.__dict__.get("__annotations__", {})
        if not isinstance(anns, dict):
            continue
        for attr_name, raw_ann in anns.items():
            if attr_name.startswith("_"):
                continue
            if any(attr_name in ancestor.__dict__ for ancestor in cls.__mro__):
                continue
            collected[attr_name] = (base.__module__, raw_ann)
    return collected


def _resolve_relative_module(mod_name: str, is_pkg: bool, module: str | None, level: int) -> str:
    """Resolve a relative import module path against mod_name."""
    if level == 0:
        return module or ""
    parts = mod_name.split(".")
    anchor = parts if is_pkg else parts[:-1]
    trim = level - 1
    base = anchor[: len(anchor) - trim] if trim <= len(anchor) else []
    if module:
        base.extend(module.split("."))
    return ".".join(base)


@functools.cache
def _parse_module_ast(mod_name: str) -> tuple[ast.Module | None, str | None, bool]:
    """Return (parsed_ast, file_path, is_package) for an importable Python module."""
    try:
        mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
    except Exception:
        return None, None, False

    mod_file = getattr(mod, "__file__", None)
    if not isinstance(mod_file, str) or not os.path.exists(mod_file):
        return None, None, False

    try:
        source = Path(mod_file).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=mod_file)
    except Exception:
        return None, mod_file, False

    is_pkg = os.path.basename(mod_file) == "__init__.py"
    return tree, mod_file, is_pkg


def _is_type_checking_guard(expr: ast.expr) -> bool:
    """Return True if an AST test expression guards an `if TYPE_CHECKING:` branch."""
    if isinstance(expr, ast.Name):
        return expr.id == "TYPE_CHECKING"
    if isinstance(expr, ast.Attribute):
        return expr.attr == "TYPE_CHECKING"
    if isinstance(expr, ast.BoolOp):
        return any(_is_type_checking_guard(val) for val in expr.values)
    return False


def _is_negated_type_checking_guard(expr: ast.expr) -> bool:
    """Return True if an AST test expression is `not TYPE_CHECKING`."""
    return (
        isinstance(expr, ast.UnaryOp)
        and isinstance(expr.op, ast.Not)
        and _is_type_checking_guard(expr.operand)
    )


@functools.cache
def _make_mock_type(mod_name: str, sym_name: str) -> type:
    """Create a deterministic lightweight type placeholder for mocked optional imports."""
    return type(sym_name, (), {"__module__": mod_name, "__qualname__": sym_name})


def _collect_type_checking_imports(
    stmts: list[ast.stmt],
    mod_name: str,
    is_pkg: bool,
    in_tc_block: bool,
    out_imports: list[tuple[str, str, str]],
) -> None:
    """Walk AST statements and collect (target_module, imported_symbol, local_alias) inside TYPE_CHECKING."""
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            if _is_type_checking_guard(stmt.test):
                _collect_type_checking_imports(stmt.body, mod_name, is_pkg, True, out_imports)
                _collect_type_checking_imports(
                    stmt.orelse, mod_name, is_pkg, in_tc_block, out_imports
                )
            elif _is_negated_type_checking_guard(stmt.test):
                _collect_type_checking_imports(
                    stmt.body, mod_name, is_pkg, in_tc_block, out_imports
                )
                _collect_type_checking_imports(stmt.orelse, mod_name, is_pkg, True, out_imports)
            else:
                _collect_type_checking_imports(
                    stmt.body, mod_name, is_pkg, in_tc_block, out_imports
                )
                _collect_type_checking_imports(
                    stmt.orelse, mod_name, is_pkg, in_tc_block, out_imports
                )
        elif isinstance(stmt, ast.Try):
            _collect_type_checking_imports(stmt.body, mod_name, is_pkg, in_tc_block, out_imports)
            for handler in stmt.handlers:
                _collect_type_checking_imports(
                    handler.body, mod_name, is_pkg, in_tc_block, out_imports
                )
            _collect_type_checking_imports(stmt.orelse, mod_name, is_pkg, in_tc_block, out_imports)
            _collect_type_checking_imports(
                stmt.finalbody, mod_name, is_pkg, in_tc_block, out_imports
            )
        elif in_tc_block and isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    out_imports.append((alias.name, "", alias.asname))
                else:
                    top_name = alias.name.split(".", maxsplit=1)[0]
                    out_imports.append((top_name, "", top_name))
        elif in_tc_block and isinstance(stmt, ast.ImportFrom):
            target_mod = _resolve_relative_module(mod_name, is_pkg, stmt.module, stmt.level)
            if not target_mod:
                continue
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                out_imports.append((target_mod, alias.name, local_name))


def _resolve_imported_symbol(
    target_mod: str,
    sym_name: str,
    mock_roots: tuple[str, ...],
    visited: frozenset[str],
) -> Any:
    """Resolve an imported module or symbol without mutating any module __dict__."""
    root_pkg = target_mod.split(".", maxsplit=1)[0]
    if root_pkg in mock_roots:
        if not sym_name:
            return types.ModuleType(target_mod)
        return _make_mock_type(target_mod, sym_name)

    try:
        mod = sys.modules.get(target_mod) or importlib.import_module(target_mod)
    except Exception:
        if not sym_name:
            return types.ModuleType(target_mod)
        return _make_mock_type(target_mod, sym_name)

    if not sym_name:
        return mod

    with contextlib.suppress(Exception):
        if hasattr(mod, sym_name):
            return getattr(mod, sym_name)

    sub_mod_name = f"{target_mod}.{sym_name}"
    with contextlib.suppress(Exception):
        return sys.modules.get(sub_mod_name) or importlib.import_module(sub_mod_name)

    if target_mod not in visited:
        nested_ns = _resolve_module_type_checking_ns_inner(
            target_mod, mock_roots, visited | {target_mod}
        )
        if sym_name in nested_ns:
            return nested_ns[sym_name]

    return _make_mock_type(target_mod, sym_name)


def _resolve_module_type_checking_ns_inner(
    mod_name: str,
    mock_roots: tuple[str, ...],
    visited: frozenset[str],
) -> dict[str, Any]:
    tree, _mod_file, is_pkg = _parse_module_ast(mod_name)
    if tree is None:
        return {}

    imports: list[tuple[str, str, str]] = []
    _collect_type_checking_imports(tree.body, mod_name, is_pkg, False, imports)

    resolved: dict[str, Any] = {}
    for target_mod, sym_name, local_name in imports:
        resolved[local_name] = _resolve_imported_symbol(target_mod, sym_name, mock_roots, visited)
    return resolved


@functools.cache
def resolve_module_type_checking_ns(
    mod_name: str,
    mock_roots: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return resolved symbols imported inside `if TYPE_CHECKING:` blocks for mod_name."""
    return _resolve_module_type_checking_ns_inner(mod_name, mock_roots, frozenset({mod_name}))


def get_mock_roots(app: Any) -> tuple[str, ...]:
    """Extract configured autodoc_mock_imports root package names from Sphinx app."""
    config = getattr(app, "config", None)
    raw = getattr(config, "autodoc_mock_imports", ()) if config is not None else ()
    roots = {str(item).split(".", maxsplit=1)[0] for item in raw if item}
    return tuple(sorted(roots))


@functools.cache
def _get_module_base_namespace(
    mod_name: str,
    mock_roots: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build and cache the combined builtins + module globals + TYPE_CHECKING namespace."""
    ns: dict[str, Any] = dict(vars(builtins))
    mod = sys.modules.get(mod_name)
    if mod is not None:
        ns.update(vars(mod))
    tc_ns = resolve_module_type_checking_ns(mod_name, mock_roots)
    for key, val in tc_ns.items():
        ns.setdefault(key, val)
    return ns


def build_eval_namespace(
    mod_name: str | None,
    extra_globals: dict[str, Any] | None = None,
    closure_vars: dict[str, Any] | None = None,
    mock_roots: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Construct an evaluation namespace combining runtime globals and TYPE_CHECKING imports."""
    if not mod_name:
        ns: dict[str, Any] = dict(vars(builtins))
    elif not extra_globals and not closure_vars:
        return _get_module_base_namespace(mod_name, mock_roots)
    else:
        ns = dict(_get_module_base_namespace(mod_name, mock_roots))

    if extra_globals:
        ns.update(extra_globals)
        if mod_name:
            tc_ns = resolve_module_type_checking_ns(mod_name, mock_roots)
            for key, val in tc_ns.items():
                ns.setdefault(key, val)
    if closure_vars:
        ns.update(closure_vars)
    return ns


def evaluate_annotation(
    raw_ann: Any,
    mod_name: str | None,
    *,
    extra_globals: dict[str, Any] | None = None,
    closure_vars: dict[str, Any] | None = None,
    mock_roots: tuple[str, ...] = (),
) -> Any:
    """Evaluate a stringified PEP 563 or ForwardRef annotation against runtime + TYPE_CHECKING scope."""
    expr: str | None = None
    if isinstance(raw_ann, str):
        expr = raw_ann.strip()
    elif isinstance(raw_ann, ForwardRef):
        expr = raw_ann.__forward_arg__.strip()

    if not expr:
        return raw_ann

    ns = build_eval_namespace(
        mod_name,
        extra_globals=extra_globals,
        closure_vars=closure_vars,
        mock_roots=mock_roots,
    )
    try:
        return eval(expr, ns)  # noqa: S307
    except Exception:
        return raw_ann


def _extract_closure_vars(func: Any) -> dict[str, Any]:
    """Extract closure cell variables from a function object."""
    localns: dict[str, Any] = {}
    code = getattr(func, "__code__", None)
    closure = getattr(func, "__closure__", None)
    if code is not None and closure is not None:
        for var_name, cell in zip(code.co_freevars, closure, strict=False):
            try:
                localns[var_name] = cell.cell_contents
            except ValueError:
                continue
    return localns


def resolve_callable_annotations(
    obj: Any,
    mock_roots: tuple[str, ...] = (),
) -> None:
    """Resolve stringified annotations on a callable using AST TYPE_CHECKING imports."""
    raw = obj.__func__ if isinstance(obj, (types.MethodType, classmethod, staticmethod)) else obj
    target = inspect.unwrap(raw) if hasattr(raw, "__wrapped__") else raw
    seen_ids: set[int] = set()
    for candidate in (obj, raw, target):
        if id(candidate) in seen_ids:
            continue
        seen_ids.add(id(candidate))
        if not callable(candidate) or inspect.isclass(candidate):
            continue
        anns = getattr(candidate, "__annotations__", None)
        if not isinstance(anns, dict) or not anns:
            continue
        if not any(isinstance(v, (str, ForwardRef)) for v in anns.values()):
            continue
        fn_globals = getattr(candidate, "__globals__", None)
        mod_name = getattr(candidate, "__module__", None)
        closure_vars = _extract_closure_vars(candidate)
        updated: dict[str, Any] = {}
        changed = False
        for key, val in anns.items():
            resolved = evaluate_annotation(
                val,
                mod_name,
                extra_globals=fn_globals if isinstance(fn_globals, dict) else None,
                closure_vars=closure_vars,
                mock_roots=mock_roots,
            )
            updated[key] = resolved
            if resolved is not val:
                changed = True
        if changed:
            try:
                candidate.__annotations__ = updated
            except (AttributeError, TypeError):
                continue
            cached_sig = candidate.__dict__.get("__signature__")
            if isinstance(cached_sig, inspect.Signature):
                new_params = [
                    p.replace(annotation=updated.get(p.name, p.annotation))
                    for p in cached_sig.parameters.values()
                ]
                new_ret = updated.get("return", cached_sig.return_annotation)
                candidate.__signature__ = cached_sig.replace(
                    parameters=new_params, return_annotation=new_ret
                )


@functools.cache
def _resolve_own_class_annotations(
    cls: type,
    mock_roots: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Resolve and cache own __annotations__ declared directly on cls."""
    anns = cls.__dict__.get("__annotations__", {})
    if not isinstance(anns, dict) or not anns:
        return {}
    return {
        attr_name: evaluate_annotation(raw_ann, cls.__module__, mock_roots=mock_roots)
        for attr_name, raw_ann in anns.items()
    }


def resolve_class_annotations(
    cls: type,
    mock_roots: tuple[str, ...] = (),
    *,
    include_mro: bool = True,
) -> dict[str, Any]:
    """Resolve class __annotations__ against each defining class module's TYPE_CHECKING scope."""
    resolved: dict[str, Any] = {}
    bases = list(reversed(cls.__mro__)) if include_mro else [cls]
    for base in bases:
        if base is object:
            continue
        resolved.update(_resolve_own_class_annotations(base, mock_roots))
    return resolved


@functools.cache
def _extract_lambdas_by_line(filename: str) -> dict[int, list[str]]:
    """Parse a Python source file and map starting line numbers to unparsed lambda body strings."""
    if not filename or not os.path.exists(filename):
        return {}
    try:
        source = Path(filename).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)
    except Exception:
        return {}

    by_line: dict[int, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda):
            with contextlib.suppress(Exception):
                unparsed = ast.unparse(node.body).strip()
                by_line.setdefault(node.lineno, []).append(unparsed)
    return by_line


def format_factory_call(factory: Any) -> str:
    """Format a dataclass or Pydantic default_factory into a clean, readable call expression."""
    if isinstance(factory, functools.partial):
        base_name = format_factory_call(factory.func).removesuffix("()")
        parts = [repr(a) for a in factory.args]
        parts.extend(f"{k}={v!r}" for k, v in (factory.keywords or {}).items())
        return f"{base_name}({', '.join(parts)})"

    fn_name = getattr(factory, "__name__", "")
    if fn_name and fn_name != "<lambda>":
        qualname = getattr(factory, "__qualname__", fn_name)
        clean_name = qualname.split(".<locals>.")[-1]
        return f"{clean_name}()"

    code = getattr(factory, "__code__", None)
    if code is not None:
        lambdas = _extract_lambdas_by_line(code.co_filename).get(code.co_firstlineno, [])
        if lambdas:
            return lambdas[0]

    with contextlib.suppress(Exception):
        src = inspect.getsource(factory).strip()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Lambda):
                return ast.unparse(node.body).strip()

    return "factory()"


def prepare_dataclass_init_signature(
    cls: type,
    mock_roots: tuple[str, ...] = (),
) -> None:
    """Attach a sanitized __signature__ to a first-party @dataclass __init__ method.

    Replaces `<factory>` tokens with readable `factory()` calls and masks defaults on
    `repr=False` fields with `...` without mutating runtime `__defaults__`.
    """
    if not is_first_party_dataclass(cls):
        return
    init_fn = cls.__dict__.get("__init__")
    if not callable(init_fn):
        return

    field_map = {f.name: f for f in dataclasses.fields(cls)}
    resolved_anns = resolve_class_annotations(cls, mock_roots=mock_roots, include_mro=True)

    try:
        raw_sig = inspect.signature(init_fn)
    except (TypeError, ValueError):
        return

    updated_params: list[inspect.Parameter] = []
    changed = False
    for param in raw_sig.parameters.values():
        if param.name == "self":
            updated_params.append(param)
            continue

        new_ann = resolved_anns.get(
            param.name,
            evaluate_annotation(param.annotation, cls.__module__, mock_roots=mock_roots),
        )
        new_default = param.default
        dc_field = field_map.get(param.name)
        if dc_field is not None:
            has_default = (
                dc_field.default is not dataclasses.MISSING
                or dc_field.default_factory is not dataclasses.MISSING
            )
            if not dc_field.repr and has_default:
                new_default = _DisplayDefault("...")
            elif dc_field.default_factory is not dataclasses.MISSING:
                new_default = _DisplayDefault(format_factory_call(dc_field.default_factory))
        elif repr(param.default) == "<factory>":
            new_default = _DisplayDefault("...")

        if new_ann is not param.annotation or new_default is not param.default:
            changed = True
            updated_params.append(param.replace(annotation=new_ann, default=new_default))
        else:
            updated_params.append(param)

    ret_ann = evaluate_annotation(raw_sig.return_annotation, cls.__module__, mock_roots=mock_roots)
    if ret_ann is not raw_sig.return_annotation:
        changed = True

    if changed:
        with contextlib.suppress(AttributeError, TypeError):
            init_fn.__signature__ = raw_sig.replace(
                parameters=updated_params,
                return_annotation=ret_ann,
            )


def _canonical_type_reference(tp: type, *, qualify_first_party: bool, short_prefix: bool) -> str:
    """Format a concrete Python class reference without leaking internal submodule paths."""
    mod = getattr(tp, "__module__", "builtins") or "builtins"
    qualname = getattr(tp, "__qualname__", tp.__name__)
    if mod in ("builtins", "__builtin__"):
        return qualname

    root = mod.split(".", maxsplit=1)[0]
    if root in FIRST_PARTY_ROOTS:
        if mod == "imednet.core.paginator":
            mod = "imednet.pagination"
        elif mod.startswith("imednet.core."):
            core_mod = sys.modules.get("imednet.core")
            core_all = getattr(core_mod, "__all__", ()) if core_mod is not None else ()
            if tp.__name__ in core_all:
                mod = "imednet.core"
            else:
                return tp.__name__
        if qualify_first_party:
            prefix = "~" if short_prefix else ""
            return f"{prefix}{mod}.{qualname}"
        return qualname

    if mod in ("typing", "typing_extensions", "types", "collections.abc"):
        return qualname

    if short_prefix and qualify_first_party:
        return f"~{mod}.{qualname}"
    return qualname


def format_rst_annotation(
    annotation: Any,
    *,
    qualify_first_party: bool = True,
    short_prefix: bool = True,
) -> str:
    """Format an annotation into clean PEP 604 syntax without UnionType, ~P, or ~T tokens."""
    if annotation is Any:
        return "Any"
    if annotation is None or annotation is types.NoneType:
        return "None"
    if annotation is Ellipsis:
        return "..."

    if isinstance(annotation, str):
        cleaned = annotation.strip()
        if (cleaned.startswith("'") and cleaned.endswith("'")) or (
            cleaned.startswith('"') and cleaned.endswith('"')
        ):
            cleaned = cleaned[1:-1]
        return cleaned.replace("typing.", "").replace("NoneType", "None")

    if isinstance(annotation, ForwardRef):
        return annotation.__forward_arg__.replace("typing.", "").replace("NoneType", "None")

    if isinstance(annotation, (TypeVar, ParamSpec)):
        return annotation.__name__
    if isinstance(annotation, TypeVarTuple):
        return f"*{annotation.__name__}"
    if isinstance(annotation, ParamSpecArgs):
        origin_ps = getattr(annotation, "__origin__", None)
        ps_name = getattr(origin_ps, "__name__", "P")
        return f"{ps_name}.args"
    if isinstance(annotation, ParamSpecKwargs):
        origin_ps = getattr(annotation, "__origin__", None)
        ps_name = getattr(origin_ps, "__name__", "P")
        return f"{ps_name}.kwargs"

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Annotated, Required, NotRequired, ClassVar, Final):
        if args:
            return format_rst_annotation(
                args[0],
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
        return getattr(origin, "__name__", str(origin).replace("typing.", ""))

    if origin is Union or origin is types.UnionType or isinstance(annotation, types.UnionType):
        union_args = args or getattr(annotation, "__args__", ())
        parts = [
            format_rst_annotation(
                a,
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
            for a in union_args
            if a is not types.NoneType
        ]
        if any(a is types.NoneType for a in union_args):
            parts.append("None")
        deduped: list[str] = []
        for item in parts:
            if item not in deduped:
                deduped.append(item)
        return " | ".join(deduped) if deduped else "Any"

    if origin is Literal:
        inner = ", ".join(repr(a) for a in args)
        return f"Literal[{inner}]"

    if origin is Concatenate:
        inner = ", ".join(
            format_rst_annotation(
                a,
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
            for a in args
        )
        return f"Concatenate[{inner}]"

    if origin in (Callable, getattr(types, "BuiltinFunctionType", None)) or (
        getattr(origin, "__name__", "") == "Callable"
    ):
        if not args:
            return "Callable"
        if len(args) == 2 and (
            args[0] is Ellipsis
            or isinstance(args[0], ParamSpec)
            or get_origin(args[0]) is Concatenate
        ):
            p_str = format_rst_annotation(
                args[0],
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
            r_str = format_rst_annotation(
                args[1],
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
            return f"Callable[{p_str}, {r_str}]"
        if len(args) == 2 and isinstance(args[0], (list, tuple)):
            p_items = ", ".join(
                format_rst_annotation(
                    a,
                    qualify_first_party=qualify_first_party,
                    short_prefix=short_prefix,
                )
                for a in args[0]
            )
            r_str = format_rst_annotation(
                args[1],
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
            return f"Callable[[{p_items}], {r_str}]"
        p_items = ", ".join(
            format_rst_annotation(
                a,
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
            for a in args[:-1]
        )
        r_str = format_rst_annotation(
            args[-1],
            qualify_first_party=qualify_first_party,
            short_prefix=short_prefix,
        )
        return f"Callable[[{p_items}], {r_str}]"

    if origin is tuple:
        if args == ((),) or (len(args) == 1 and args[0] == ()):
            return "tuple[()]"
        if not args:
            return "tuple"
        inner = ", ".join(
            format_rst_annotation(
                a,
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
            for a in args
        )
        return f"tuple[{inner}]"

    if origin is not None:
        if isinstance(origin, type):
            origin_name = _canonical_type_reference(
                origin,
                qualify_first_party=qualify_first_party,
                short_prefix=short_prefix,
            )
        else:
            origin_name = getattr(origin, "__name__", None) or getattr(
                origin, "_name", str(origin).replace("typing.", "")
            )
        if args:
            inner = ", ".join(
                format_rst_annotation(
                    a,
                    qualify_first_party=qualify_first_party,
                    short_prefix=short_prefix,
                )
                for a in args
            )
            return f"{origin_name}[{inner}]"
        return str(origin_name)

    if isinstance(annotation, type):
        return _canonical_type_reference(
            annotation,
            qualify_first_party=qualify_first_party,
            short_prefix=short_prefix,
        )

    raw_str = str(annotation).replace("typing.", "").replace("types.UnionType", "Union")
    return raw_str.replace("NoneType", "None").lstrip("~")


def format_clean_signature(
    sig: inspect.Signature,
    *,
    show_annotation: bool = True,
    show_return_annotation: bool = True,
    unqualified_typehints: bool = True,
) -> str:
    """Stringify an inspect.Signature with clean PEP 604, ParamSpec, TypeVar, and factory tokens."""
    parts: list[str] = []
    last_kind: inspect._ParameterKind | None = None
    pos_only_emitted = False
    kw_only_emitted = False

    params = list(sig.parameters.values())
    has_var_positional = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)

    for param in params:
        if (
            last_kind == inspect.Parameter.POSITIONAL_ONLY
            and param.kind != inspect.Parameter.POSITIONAL_ONLY
            and not pos_only_emitted
        ):
            parts.append("/")
            pos_only_emitted = True

        if (
            param.kind == inspect.Parameter.KEYWORD_ONLY
            and not kw_only_emitted
            and not has_var_positional
        ):
            parts.append("*")
            kw_only_emitted = True

        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            piece = f"*{param.name}"
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            piece = f"**{param.name}"
        else:
            piece = param.name

        has_ann = show_annotation and param.annotation is not inspect.Parameter.empty
        if has_ann:
            ann_str = format_rst_annotation(
                param.annotation,
                qualify_first_party=not unqualified_typehints,
                short_prefix=False,
            )
            piece = f"{piece}: {ann_str}"

        if param.default is not inspect.Parameter.empty:
            def_str = repr(param.default)
            if def_str == "<factory>":
                def_str = "..."
            sep = " = " if has_ann else "="
            piece = f"{piece}{sep}{def_str}"

        parts.append(piece)
        last_kind = param.kind

    if last_kind == inspect.Parameter.POSITIONAL_ONLY and not pos_only_emitted:
        parts.append("/")

    rendered = f"({', '.join(parts)})"
    if (
        show_annotation
        and show_return_annotation
        and sig.return_annotation is not inspect.Signature.empty
    ):
        ret_str = format_rst_annotation(
            sig.return_annotation,
            qualify_first_party=not unqualified_typehints,
            short_prefix=False,
        )
        rendered = f"{rendered} -> {ret_str}"
    return rendered


@functools.cache
def _get_module_overloads(mod_name: str) -> dict[str, list[inspect.Signature]]:
    """Analyze and cache @overload signature maps for mod_name."""
    try:
        analyzer = ModuleAnalyzer.for_module(mod_name)
        analyzer.analyze()
    except Exception:
        return {}
    return dict(analyzer.overloads)


def _find_overloads_for_member(
    analyzer: ModuleAnalyzer | None,
    objpath: list[str],
    parent: Any = None,
) -> tuple[list[inspect.Signature] | None, str | None]:
    """Locate @overload signatures in ModuleAnalyzer (falling back to defining MRO base module)."""
    key = ".".join(objpath)
    if analyzer is not None and key in analyzer.overloads:
        return analyzer.overloads[key], getattr(analyzer, "modname", None)

    if isinstance(parent, type) and objpath:
        member_name = objpath[-1]
        for base in parent.__mro__[1:]:
            if base is object:
                continue
            if member_name in base.__dict__:
                base_overloads = _get_module_overloads(base.__module__)
                base_key = f"{base.__qualname__}.{member_name}"
                if base_key in base_overloads:
                    return base_overloads[base_key], base.__module__
                break
    return None, None


class ApiFunctionDocumenter(FunctionDocumenter):
    """FunctionDocumenter resolving AST TYPE_CHECKING forward refs across @overload signatures."""

    priority = FunctionDocumenter.priority + 1

    def format_signature(self, **kwargs: Any) -> str:
        overloads, overload_mod = _find_overloads_for_member(self.analyzer, self.objpath)
        if overloads and self.config.autodoc_typehints != "none":
            mock_roots = get_mock_roots(self.env.app)
            actual = inspect.signature(self.object)
            fn_globals = safe_getattr(self.object, "__globals__", {})
            mod_name = overload_mod or getattr(self.object, "__module__", self.modname)
            eval_ns = build_eval_namespace(mod_name, fn_globals, mock_roots=mock_roots)
            unqualified = self.config.autodoc_typehints_format == "short"
            sigs: list[str] = []
            for overload in overloads:
                merged = self.merge_default_value(actual, overload)
                evaluated = evaluate_signature(
                    merged,
                    eval_ns,
                    self.config.autodoc_type_aliases,
                )
                sigs.append(
                    format_clean_signature(
                        evaluated,
                        show_annotation=True,
                        show_return_annotation=True,
                        unqualified_typehints=unqualified,
                    )
                )
            return "\n".join(sigs)
        return super().format_signature(**kwargs)


class ApiMethodDocumenter(MethodDocumenter):
    """MethodDocumenter preserving async markers and typed @overload method signatures."""

    priority = MethodDocumenter.priority + 1

    def add_directive_header(self, sig: str) -> None:
        super().add_directive_header(sig)
        raw = (
            self.parent.__dict__.get(self.object_name, self.object)
            if isinstance(self.parent, type)
            else self.object
        )
        raw_is_async = inspect.iscoroutinefunction(raw) or inspect.isasyncgenfunction(raw)
        bound_is_async = inspect.iscoroutinefunction(self.object) or inspect.isasyncgenfunction(
            self.object
        )
        if bound_is_async and not raw_is_async:
            self.add_line("   :async:", self.get_sourcename())

    def format_signature(self, **kwargs: Any) -> str:
        mock_roots = get_mock_roots(self.env.app)
        if self.object_name == "__init__" and is_first_party_dataclass(self.parent):
            prepare_dataclass_init_signature(self.parent, mock_roots=mock_roots)

        overloads, overload_mod = _find_overloads_for_member(
            self.analyzer, self.objpath, self.parent
        )
        if overloads and self.config.autodoc_typehints != "none":
            is_static = isinstance(
                self.parent.__dict__.get(self.object_name)
                if isinstance(self.parent, type)
                else None,
                staticmethod,
            )
            try:
                actual_for_merge: inspect.Signature | None = inspect.signature(self.object)
            except (TypeError, ValueError):
                actual_for_merge = None

            fn_globals = safe_getattr(self.object, "__globals__", {})
            mod_name = overload_mod or getattr(self.object, "__module__", self.modname)
            eval_ns = build_eval_namespace(mod_name, fn_globals, mock_roots=mock_roots)
            unqualified = self.config.autodoc_typehints_format == "short"
            sigs: list[str] = []
            for overload in overloads:
                merged = (
                    self.merge_default_value(actual_for_merge, overload)
                    if actual_for_merge is not None
                    else overload
                )
                evaluated = evaluate_signature(
                    merged,
                    eval_ns,
                    self.config.autodoc_type_aliases,
                )
                if not is_static:
                    params = list(evaluated.parameters.values())
                    if params and params[0].name in ("self", "cls"):
                        evaluated = evaluated.replace(parameters=params[1:])
                sigs.append(
                    format_clean_signature(
                        evaluated,
                        show_annotation=True,
                        show_return_annotation=True,
                        unqualified_typehints=unqualified,
                    )
                )
            return "\n".join(sigs)
        return super().format_signature(**kwargs)


class ApiClassDocumenter(ClassDocumenter):
    """ClassDocumenter rendering explicit Protocol type aliases and sanitized dataclass __init__."""

    priority = ClassDocumenter.priority + 1

    def _is_module_protocol_alias(self) -> bool:
        return (
            bool(getattr(self, "doc_as_attr", False))
            and len(self.objpath) == 1
            and is_protocol_class(self.object)
        )

    def format_signature(self, **kwargs: Any) -> str:
        if is_first_party_dataclass(self.object):
            prepare_dataclass_init_signature(self.object, mock_roots=get_mock_roots(self.env.app))
        return super().format_signature(**kwargs)

    def add_directive_header(self, sig: str) -> None:
        if self._is_module_protocol_alias():
            sourcename = self.get_sourcename()
            self.directivetype = "data"
            documenter_base = ClassDocumenter.__mro__[2]
            documenter_base.add_directive_header(self, sig)
            target_name = getattr(self.object, "__name__", "")
            self.add_line("   :type: TypeAlias", sourcename)
            if target_name:
                self.add_line(f"   :value: {target_name}", sourcename)
            return
        super().add_directive_header(sig)

    def add_content(self, more_content: StringList | None) -> None:
        if self._is_module_protocol_alias():
            target_mod = getattr(self.object, "__module__", self.modname)
            target_qual = getattr(self.object, "__qualname__", getattr(self.object, "__name__", ""))
            alias_lines = StringList(
                [
                    f"Explicit protocol type alias for :class:`~{target_mod}.{target_qual}`.",
                    "",
                ],
                source="",
            )
            documenter_base = ClassDocumenter.__mro__[2]
            documenter_base.add_content(self, alias_lines)
            return
        super().add_content(more_content)


@functools.cache
def _get_module_attr_docs(mod_name: str) -> dict[tuple[str, str], str]:
    """Extract and cache inline #: or attribute docstrings for mod_name."""
    try:
        analyzer = ModuleAnalyzer.for_module(mod_name)
        raw_docs = analyzer.find_attr_docs()
    except Exception:
        return {}

    cleaned_map: dict[tuple[str, str], str] = {}
    for key, doc_lines in raw_docs.items():
        cleaned = "\n".join(line for line in doc_lines if line is not None).strip()
        if cleaned:
            cleaned_map[key] = cleaned
    return cleaned_map


def _get_analyzer_attr_docs(cls: type) -> dict[str, str]:
    """Extract inline #: or attribute docstrings across cls.__mro__ from cached ModuleAnalyzer."""
    docs: dict[str, str] = {}
    for base in reversed(cls.__mro__):
        if base is object or base.__module__.split(".", maxsplit=1)[0] not in FIRST_PARTY_ROOTS:
            continue
        mod_docs = _get_module_attr_docs(base.__module__)
        cls_qual = base.__qualname__
        for (scope, attr_name), cleaned in mod_docs.items():
            if scope == cls_qual:
                docs[attr_name] = cleaned
    return docs


def is_noindex_options(options: Any) -> bool:
    """Return True if Sphinx autodoc options request :noindex: or :no-index:."""
    if options is None:
        return False
    if isinstance(options, dict):
        return bool(options.get("noindex") or options.get("no-index"))
    return bool(getattr(options, "noindex", False) or getattr(options, "no_index", False))


def upsert_attribute_directives(
    lines: list[str],
    specs: Sequence[AttributeSpec],
    *,
    noindex: bool = False,
) -> None:
    """Enrich existing .. attribute:: blocks or append new .. py:attribute:: directives."""
    if not specs:
        return

    spec_by_name = {spec.name: spec for spec in specs}
    seen: set[str] = set()

    idx = 0
    while idx < len(lines):
        match = _PY_ATTR_HEADER_RE.match(lines[idx])
        if not match:
            idx += 1
            continue

        indent_str, attr_name = match.group(1), match.group(2)
        spec = spec_by_name.get(attr_name)
        if spec is None:
            idx += 1
            continue

        seen.add(attr_name)
        opt_indent = f"{indent_str}   "
        opt_end = idx + 1
        has_type = False
        has_value = False
        has_noindex = False
        while opt_end < len(lines):
            stripped = lines[opt_end].strip()
            if stripped.startswith(":type:"):
                has_type = True
                opt_end += 1
            elif stripped.startswith(":value:"):
                has_value = True
                opt_end += 1
            elif stripped in (":noindex:", ":no-index:"):
                has_noindex = True
                opt_end += 1
            elif stripped.startswith(":") and ":" in stripped[1:]:
                opt_end += 1
            else:
                break

        insert_opts: list[str] = []
        if noindex and not has_noindex:
            insert_opts.append(f"{opt_indent}:noindex:")
        if not has_type and spec.type_str:
            insert_opts.append(f"{opt_indent}:type: {spec.type_str}")
        if not has_value and spec.value_str is not None:
            insert_opts.append(f"{opt_indent}:value: {spec.value_str}")
        if insert_opts:
            lines[opt_end:opt_end] = insert_opts
            opt_end += len(insert_opts)

        if spec.marker:
            body_idx = opt_end
            while body_idx < len(lines) and not lines[body_idx].strip():
                body_idx += 1
            if body_idx < len(lines) and lines[body_idx].startswith(opt_indent):
                if spec.marker not in lines[body_idx]:
                    lines[body_idx] = f"{opt_indent}{spec.marker} {lines[body_idx].strip()}"
            else:
                marker_block = ["", f"{opt_indent}{spec.marker}"]
                lines[opt_end:opt_end] = marker_block
                opt_end += len(marker_block)

        idx = opt_end

    appended: list[str] = []
    for spec in specs:
        if spec.name in seen:
            continue
        appended.append("")
        appended.append(f".. py:attribute:: {spec.name}")
        if noindex:
            appended.append("   :noindex:")
        if spec.type_str:
            appended.append(f"   :type: {spec.type_str}")
        if spec.value_str is not None:
            appended.append(f"   :value: {spec.value_str}")

        desc_lines = spec.description.splitlines() if spec.description else []
        if spec.marker or desc_lines:
            appended.append("")
            if spec.marker and desc_lines:
                appended.append(f"   {spec.marker} {desc_lines[0]}")
                for extra_line in desc_lines[1:]:
                    appended.append(f"   {extra_line}" if extra_line else "")
            elif spec.marker:
                appended.append(f"   {spec.marker}")
            else:
                for extra_line in desc_lines:
                    appended.append(f"   {extra_line}" if extra_line else "")

    if appended:
        lines.extend(appended)
        lines.append("")


def build_pydantic_field_specs(
    cls: type[BaseModel],
    mock_roots: tuple[str, ...] = (),
) -> list[AttributeSpec]:
    """Build AttributeSpec entries with :type: and :value: / Required markers for a BaseModel."""
    model_fields = getattr(cls, "model_fields", None)
    if not isinstance(model_fields, dict) or not model_fields:
        return []

    resolved_anns = resolve_class_annotations(cls, mock_roots=mock_roots, include_mro=True)
    attr_docs = _get_analyzer_attr_docs(cls)
    specs: list[AttributeSpec] = []

    for field_name, field_info in model_fields.items():
        ann = resolved_anns.get(field_name, field_info.annotation)
        if isinstance(ann, (str, ForwardRef)):
            ann = evaluate_annotation(ann, cls.__module__, mock_roots=mock_roots)
        type_str = format_rst_annotation(ann)

        description = field_info.description or attr_docs.get(field_name, "")
        if not description and field_info.alias and field_info.alias != field_name:
            description = f"API field ``{field_info.alias}``."

        is_req = bool(field_info.is_required())
        value_str: str | None = None
        marker: str | None = None
        if is_req:
            marker = "**Required.**"
        elif field_info.repr is False:
            value_str = "..."
        elif field_info.default_factory is not None:
            value_str = format_factory_call(field_info.default_factory)
        else:
            value_str = repr(field_info.default)

        specs.append(
            AttributeSpec(
                name=field_name,
                type_str=type_str,
                value_str=value_str,
                marker=marker,
                description=description,
            )
        )
    return specs


def emit_structured_class_members(
    app: Any,
    what: str,
    name: str,  # noqa: ARG001
    obj: Any,
    options: Any,
    lines: list[str],
) -> None:
    """Emit structured attribute/value/key directives for Enum, @dataclass, TypedDict, and Protocol."""
    if what != "class" or not isinstance(obj, type):
        return
    if issubclass(obj, BaseModel) and obj not in (BaseModel, RootModel):
        return

    mock_roots = get_mock_roots(app)
    attr_docs = _get_analyzer_attr_docs(obj)
    noindex = is_noindex_options(options)

    if issubclass(obj, Enum) and obj is not Enum:
        enum_specs: list[AttributeSpec] = []
        for member_name, member in obj.__members__.items():
            raw_val = member.value
            val_type = type(raw_val).__name__ if raw_val is not None else "None"
            member_doc = getattr(member, "__doc__", None)
            default_enum_doc = getattr(obj, "__doc__", None)
            clean_doc = attr_docs.get(member_name, "")
            if (
                not clean_doc
                and isinstance(member_doc, str)
                and member_doc.strip()
                and member_doc not in (default_enum_doc, Enum.__doc__)
            ):
                clean_doc = member_doc.strip()
            enum_specs.append(
                AttributeSpec(
                    name=member_name,
                    type_str=val_type,
                    value_str=repr(raw_val),
                    description=clean_doc,
                )
            )
        upsert_attribute_directives(lines, enum_specs, noindex=noindex)
        return

    if is_first_party_dataclass(obj):
        resolved_anns = resolve_class_annotations(obj, mock_roots=mock_roots, include_mro=True)
        dc_specs: list[AttributeSpec] = []
        for dc_field in dataclasses.fields(obj):
            if dc_field.name.startswith("_"):
                continue
            ann = resolved_anns.get(dc_field.name, dc_field.type)
            if isinstance(ann, (str, ForwardRef)):
                ann = evaluate_annotation(ann, obj.__module__, mock_roots=mock_roots)
            type_str = format_rst_annotation(ann)

            value_str: str | None = None
            marker: str | None = None
            has_default = (
                dc_field.default is not dataclasses.MISSING
                or dc_field.default_factory is not dataclasses.MISSING
            )
            if not has_default:
                marker = "**Required.**"
            elif not dc_field.repr:
                value_str = "..."
            elif dc_field.default_factory is not dataclasses.MISSING:
                value_str = format_factory_call(dc_field.default_factory)
            else:
                value_str = repr(dc_field.default)

            dc_specs.append(
                AttributeSpec(
                    name=dc_field.name,
                    type_str=type_str,
                    value_str=value_str,
                    marker=marker,
                    description=attr_docs.get(dc_field.name, ""),
                )
            )
        upsert_attribute_directives(lines, dc_specs, noindex=noindex)
        return

    if is_typeddict_class(obj):
        resolved_anns = resolve_class_annotations(obj, mock_roots=mock_roots, include_mro=True)
        req_keys: frozenset[str] = frozenset(getattr(obj, "__required_keys__", ()))
        opt_keys: frozenset[str] = frozenset(getattr(obj, "__optional_keys__", ()))
        td_specs: list[AttributeSpec] = []
        ordered_keys = list(resolved_anns.keys())
        for extra_k in (*sorted(req_keys), *sorted(opt_keys)):
            if extra_k not in resolved_anns:
                ordered_keys.append(extra_k)
        for key_name in ordered_keys:
            if key_name.startswith("_"):
                continue
            ann = resolved_anns.get(key_name, Any)
            origin = get_origin(ann)
            is_req = key_name in req_keys or origin is Required
            if origin is NotRequired:
                is_req = False
            type_str = format_rst_annotation(ann)
            td_specs.append(
                AttributeSpec(
                    name=key_name,
                    type_str=type_str,
                    marker="**Required.**" if is_req else "**Optional.**",
                    description=attr_docs.get(key_name, ""),
                )
            )
        upsert_attribute_directives(lines, td_specs, noindex=noindex)
        return

    if is_protocol_class(obj):
        bare_anns = get_protocol_bare_annotations(obj)
        if not bare_anns:
            return
        proto_specs: list[AttributeSpec] = []
        for attr_name, (def_mod, raw_ann) in bare_anns.items():
            resolved = evaluate_annotation(raw_ann, def_mod, mock_roots=mock_roots)
            proto_specs.append(
                AttributeSpec(
                    name=attr_name,
                    type_str=format_rst_annotation(resolved),
                    description=attr_docs.get(attr_name, ""),
                )
            )
        upsert_attribute_directives(lines, proto_specs, noindex=noindex)


def before_process_signature_hook(app: Any, obj: Any, bound_method: bool) -> None:  # noqa: ARG001
    """Resolve AST TYPE_CHECKING forward references and sanitize dataclass __init__ signatures."""
    mock_roots = get_mock_roots(app)
    if isinstance(obj, type):
        if is_first_party_dataclass(obj):
            prepare_dataclass_init_signature(obj, mock_roots=mock_roots)
        return

    if callable(obj):
        env = getattr(app, "env", None)
        temp_data = getattr(env, "temp_data", {}) if env is not None else {}
        cls_mod = temp_data.get("autodoc:module")
        cls_name = temp_data.get("autodoc:class")
        if cls_mod and cls_name and getattr(obj, "__name__", "") == "__init__":
            mod = sys.modules.get(cls_mod)
            owner_cls = getattr(mod, cls_name, None) if mod is not None else None
            if is_first_party_dataclass(owner_cls):
                prepare_dataclass_init_signature(owner_cls, mock_roots=mock_roots)
                return
        resolve_callable_annotations(obj, mock_roots=mock_roots)


def sanitize_recorded_typehints(
    app: Any,
    what: str,  # noqa: ARG001
    name: str,
    obj: Any,
    options: Any,  # noqa: ARG001
    args: str | None,  # noqa: ARG001
    retann: str | None,  # noqa: ARG001
) -> None:
    """Replace recorded temp_data['annotations'][name] strings to prevent ~P, ~T, and UnionType."""
    if not callable(obj):
        return
    env = getattr(app, "env", None)
    if env is None:
        return
    all_annotations = env.temp_data.get("annotations")
    if not isinstance(all_annotations, dict):
        return
    recorded = all_annotations.get(name)
    if not isinstance(recorded, dict) or not recorded:
        return

    short_fmt = getattr(app.config, "autodoc_typehints_format", "short") == "short"
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return

    for param in sig.parameters.values():
        if param.annotation is not inspect.Parameter.empty and param.name in recorded:
            recorded[param.name] = format_rst_annotation(
                param.annotation,
                qualify_first_party=True,
                short_prefix=short_fmt,
            )
    if sig.return_annotation is not inspect.Signature.empty and "return" in recorded:
        recorded["return"] = format_rst_annotation(
            sig.return_annotation,
            qualify_first_party=True,
            short_prefix=short_fmt,
        )


def should_skip_structured_or_inherited_member(
    cls: type,
    name: str,
    local_ast_members: frozenset[str] | None = None,
) -> bool | None:
    """Return True when a class member should be skipped to prevent leaks or duplicate directives."""
    if name == "__init__":
        has_local_init = local_ast_members is not None and "__init__" in local_ast_members
        if (
            issubclass(cls, (BaseModel, RootModel, Enum))
            or is_typeddict_class(cls)
            or is_protocol_class(cls)
        ) and not has_local_init:
            return True
        if is_first_party_dataclass(cls):
            return None
        for base in cls.__mro__:
            if "__init__" in base.__dict__:
                if base in (BaseModel, RootModel, Enum, object, Exception, BaseException):
                    return True
                if getattr(base, "__name__", "") == "ImednetBaseModel":
                    return True
                base_mod = getattr(base, "__module__", "") or ""
                if base_mod.split(".", maxsplit=1)[0] not in FIRST_PARTY_ROOTS:
                    return True
                break
        return None

    if issubclass(cls, Enum) and cls is not Enum and name in cls.__members__:
        return True

    if is_first_party_dataclass(cls):
        if name in _DATACLASS_SYNTHETIC_DUNDERS and (
            local_ast_members is None or name not in local_ast_members
        ):
            return True
        if any(f.name == name for f in dataclasses.fields(cls)):
            return True

    if is_typeddict_class(cls):
        req = getattr(cls, "__required_keys__", ())
        opt = getattr(cls, "__optional_keys__", ())
        if name in req or name in opt:
            return True

    if is_protocol_class(cls) and name in get_protocol_bare_annotations(cls):
        return True

    return None


def resolve_protocol_alias_reference(app: Any, env: Any, node: Any, contnode: Any) -> Any:
    """Resolve unqualified :class: cross-references to module-level Protocol aliases in imednet.plugins."""
    from sphinx.util.nodes import make_refnode

    if node.get("refdomain") != "py" or not hasattr(app.builder, "get_relative_uri"):
        return None
    reftarget = node.get("reftarget", "")
    if not isinstance(reftarget, str) or not reftarget:
        return None

    py_objects = env.domaindata.get("py", {}).get("objects", {})
    if not py_objects:
        return None

    candidates = [reftarget]
    if "." not in reftarget:
        candidates.append(f"imednet.plugins.{reftarget}")

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


def clear_caches() -> None:
    """Clear AST and type-checking import caches across fresh Sphinx builds."""
    _parse_module_ast.cache_clear()
    _make_mock_type.cache_clear()
    resolve_module_type_checking_ns.cache_clear()
    _get_module_base_namespace.cache_clear()
    _resolve_own_class_annotations.cache_clear()
    _extract_lambdas_by_line.cache_clear()
    _get_module_overloads.cache_clear()
    _get_module_attr_docs.cache_clear()


def setup(app: Any) -> dict[str, Any]:
    """Register native API reference documenters and AST/typehint hooks."""
    clear_caches()
    app.add_autodocumenter(ApiFunctionDocumenter, override=True)
    app.add_autodocumenter(ApiMethodDocumenter, override=True)
    app.add_autodocumenter(ApiClassDocumenter, override=True)
    app.connect("autodoc-before-process-signature", before_process_signature_hook, priority=350)
    app.connect("autodoc-process-signature", sanitize_recorded_typehints, priority=700)
    app.connect("autodoc-process-docstring", emit_structured_class_members, priority=600)
    app.connect("missing-reference", resolve_protocol_alias_reference, priority=600)
    return {
        "version": "1.0.0",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
