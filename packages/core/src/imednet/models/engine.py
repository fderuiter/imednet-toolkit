"""Dynamic model generation engine based on unified contract schemas."""

from __future__ import annotations

import ast
import glob
import importlib.resources
import importlib.util
import json
import os
import re
import sys
import types
from typing import Any, ClassVar, Union, get_args, get_origin

from pydantic import Field, create_model

from imednet.models.base import ImednetBaseModel
from imednet.models.contract import APIContract, ContractBuilder

_CONTRACT_CACHE: APIContract | None = None
_SKIP_STUB_FILES = frozenset({"__init__.py", "engine.py", "contract.py"})
_STDLIB_MODULES = frozenset(sys.stdlib_module_names)
_FIRST_PARTY_ROOTS = frozenset(
    {"imednet", "imednet_workflows", "apache_airflow_providers_imednet", "imednet_tui"}
)


def _format_runtime_annotation(annotation: Any) -> str:
    """Format a runtime Python type annotation into modern PEP 604 stub syntax."""
    if annotation is Any:
        return "Any"
    if annotation is None or annotation is type(None):
        return "None"

    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        parts = [_format_runtime_annotation(a) for a in args if a is not type(None)]
        if any(a is type(None) for a in args):
            parts.append("None")
        return " | ".join(parts)

    if origin is not None:
        origin_name = getattr(origin, "__name__", str(origin).replace("typing.", ""))
        args = get_args(annotation)
        if args:
            inner = ", ".join(_format_runtime_annotation(a) for a in args)
            return f"{origin_name}[{inner}]"
        return origin_name

    if isinstance(annotation, type):
        return annotation.__name__

    return str(annotation).replace("typing.", "").replace("NoneType", "None")


class _AnnotationNormalizer(ast.NodeTransformer):
    """Normalize AST annotations for stub emission (e.g., Optional[T] -> T | None)."""

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == "Optional":
            return ast.BinOp(
                left=node.slice,
                op=ast.BitOr(),
                right=ast.Constant(value=None),
            )
        return node


def _format_expr(node: ast.AST) -> str:
    """Format an AST expression using double-quoted string literals."""
    normalized = _AnnotationNormalizer().visit(ast.fix_missing_locations(node))
    if isinstance(normalized, ast.Constant) and isinstance(normalized.value, str):
        return json.dumps(normalized.value)
    text = ast.unparse(normalized)
    # Convert single-quoted string literals inside decorator/type expressions to double quotes
    return re.sub(
        r"(?<![A-Za-z0-9_])'([^'\\]*)'",
        lambda m: json.dumps(m.group(1)),
        text,
    )


def _extract_get_model_call(stmt: ast.stmt) -> tuple[str, str, str | None] | None:
    """Extract (target_name, model_name, base_name) from X = ModelEngine.get_model(...)."""
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return None
    target = stmt.targets[0]
    if not isinstance(target, ast.Name):
        return None
    call = stmt.value
    if not isinstance(call, ast.Call):
        return None
    if not (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "ModelEngine"
        and call.func.attr == "get_model"
    ):
        return None
    if not call.args or not isinstance(call.args[0], ast.Constant):
        return None
    model_name = call.args[0].value
    if not isinstance(model_name, str):
        return None

    base_name: str | None = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Name):
        base_name = call.args[1].id
    else:
        for kw in call.keywords:
            if kw.arg == "base_cls" and isinstance(kw.value, ast.Name):
                base_name = kw.value.id
    return (target.id, model_name, base_name)


def _extract_docstring(stmt: ast.stmt) -> str | None:
    """Return docstring text if statement is a string literal docstring expression."""
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    ):
        return stmt.value.value
    return None


def _format_docstring(doc: str, indent: str = "") -> list[str]:
    """Format a docstring with triple double quotes."""
    raw_lines = doc.split("\n")
    if len(raw_lines) == 1:
        return [f'{indent}"""{raw_lines[0]}"""']
    out = [f'{indent}"""{raw_lines[0]}']
    for line in raw_lines[1:]:
        if line.strip():
            out.append(f"{indent}{line}" if not line.startswith(indent) else line)
        else:
            out.append("")
    if out[-1] == "":
        out[-1] = f'{indent}"""'
    else:
        out.append(f'{indent}"""')
    return out


def _is_enum_class(cls_node: ast.ClassDef) -> bool:
    """Return True if ClassDef inherits from Enum / StrEnum / IntEnum."""
    for base in cls_node.bases:
        if isinstance(base, ast.Name) and base.id in {"Enum", "StrEnum", "IntEnum", "Flag"}:
            return True
        if isinstance(base, ast.Attribute) and base.attr in {"Enum", "StrEnum", "IntEnum", "Flag"}:
            return True
    return False


def _format_arg(arg: ast.arg, default: ast.expr | None = None, prefix: str = "") -> str:
    """Format a single function parameter."""
    part = f"{prefix}{arg.arg}"
    if arg.annotation is not None:
        part += f": {_format_expr(arg.annotation)}"
    if default is not None:
        sep = " = " if arg.annotation is not None else "="
        part += f"{sep}{_format_expr(default)}"
    return part


def _format_function_stub(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    indent: str = "",
) -> list[str]:
    """Format a function or method with body stripped to `...`."""
    lines: list[str] = []
    for dec in fn.decorator_list:
        lines.append(f"{indent}@{_format_expr(dec)}")

    args = fn.args
    params: list[str] = []

    pos_args = list(args.posonlyargs) + list(args.args)
    num_pos_defaults = len(args.defaults)
    pos_default_start = len(pos_args) - num_pos_defaults

    for idx, arg in enumerate(pos_args):
        default = args.defaults[idx - pos_default_start] if idx >= pos_default_start else None
        params.append(_format_arg(arg, default))
        if args.posonlyargs and idx == len(args.posonlyargs) - 1:
            params.append("/")

    if args.vararg is not None:
        params.append(_format_arg(args.vararg, prefix="*"))
    elif args.kwonlyargs:
        params.append("*")

    for kw_arg, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        params.append(_format_arg(kw_arg, kw_default))

    if args.kwarg is not None:
        params.append(_format_arg(args.kwarg, prefix="**"))

    async_prefix = "async " if isinstance(fn, ast.AsyncFunctionDef) else ""
    ret = f" -> {_format_expr(fn.returns)}" if fn.returns is not None else ""
    joined_params = ", ".join(params)
    single_line = f"{indent}{async_prefix}def {fn.name}({joined_params}){ret}: ..."
    if len(single_line) <= 100:
        lines.append(single_line)
        return lines

    indented_params = f"{indent}    {joined_params}"
    if len(indented_params) <= 100:
        lines.append(f"{indent}{async_prefix}def {fn.name}(")
        lines.append(indented_params)
        lines.append(f"{indent}){ret}: ...")
        return lines

    lines.append(f"{indent}{async_prefix}def {fn.name}(")
    for param in params:
        lines.append(f"{indent}    {param},")
    lines.append(f"{indent}){ret}: ...")
    return lines


def _load_module_models(
    py_file: str,
    model_targets: list[tuple[str, str, str | None]],
) -> dict[str, type[Any]]:
    """Execute a model module in isolation to resolve base_cls and dynamic models."""
    stem = os.path.splitext(os.path.basename(py_file))[0]
    temp_mod_name = f"_stubgen_isolated_{stem}"
    saved_cache = ModelEngine._model_cache.copy()
    spec = importlib.util.spec_from_file_location(temp_mod_name, py_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec for {py_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[temp_mod_name] = module
    try:
        spec.loader.exec_module(module)
        resolved: dict[str, type[Any]] = {}
        for target_name, model_name, base_name in model_targets:
            attr = getattr(module, target_name, None)
            if isinstance(attr, type):
                resolved[target_name] = attr
            else:
                base_cls = (
                    getattr(module, base_name, ImednetBaseModel) if base_name else ImednetBaseModel
                )
                resolved[target_name] = ModelEngine.get_model(model_name, base_cls)
        return resolved
    finally:
        sys.modules.pop(temp_mod_name, None)
        ModelEngine._model_cache = saved_cache


def _collect_used_names(stub_body_source: str) -> set[str]:
    """Parse stub body statements and collect all loaded identifier names."""
    parsed = ast.parse(stub_body_source)
    used: set[str] = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
    return used


def _classify_import_group(module_name: str) -> int:
    """Classify module name for isort grouping: 0=__future__, 1=stdlib, 2=third-party, 3=local."""
    if module_name == "__future__":
        return 0
    root = module_name.split(".", maxsplit=1)[0]
    if root in _FIRST_PARTY_ROOTS:
        return 3
    if root in _STDLIB_MODULES:
        return 1
    return 2


def _build_pruned_imports(
    import_nodes: list[ast.Import | ast.ImportFrom],
    used_names: set[str],
) -> list[str]:
    """Prune unused imports (including ModelEngine, Optional, Any) and emit sorted lines."""
    pruned_forbidden = {"ModelEngine", "Optional"}
    grouped: dict[int, list[tuple[tuple[int, str, str], str]]] = {0: [], 1: [], 2: [], 3: []}
    typing_aliases: list[ast.alias] = []
    has_future_annotations = False
    any_imported = False

    for node in import_nodes:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(a.name == "annotations" for a in node.names):
                has_future_annotations = True
            continue

        if isinstance(node, ast.ImportFrom) and node.module == "typing" and node.level == 0:
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound in pruned_forbidden:
                    continue
                if bound in used_names:
                    typing_aliases.append(alias)
                    if bound == "Any":
                        any_imported = True
            continue

        if isinstance(node, ast.Import):
            kept = [
                alias
                for alias in node.names
                if (alias.asname or alias.name.split(".", maxsplit=1)[0]) in used_names
                and (alias.asname or alias.name) not in pruned_forbidden
            ]
            for alias in kept:
                mod = alias.name
                grp = _classify_import_group(mod)
                rendered = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
                grouped[grp].append(((0, mod, alias.asname or alias.name), rendered))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            kept = [
                alias
                for alias in node.names
                if (alias.asname or alias.name) in used_names
                and (alias.asname or alias.name) not in pruned_forbidden
            ]
            if kept:
                grp = 3 if node.level > 0 else _classify_import_group(mod)
                sorted_kept = sorted(
                    kept,
                    key=lambda a: (
                        not a.name.isupper(),
                        not a.name[:1].isupper(),
                        a.name.lower(),
                        a.name,
                    ),
                )
                names_str = ", ".join(
                    f"{a.name} as {a.asname}" if a.asname else a.name for a in sorted_kept
                )
                dots = "." * node.level
                rendered = f"from {dots}{mod} import {names_str}"
                grouped[grp].append(((1, f"{dots}{mod}", names_str), rendered))

    if "Any" in used_names and not any_imported:
        typing_aliases.append(ast.alias(name="Any"))

    if typing_aliases:
        dedup: dict[tuple[str, str | None], ast.alias] = {}
        for alias in typing_aliases:
            dedup[(alias.name, alias.asname)] = alias
        sorted_typing = sorted(
            dedup.values(),
            key=lambda a: (
                not a.name.isupper(),
                not a.name[:1].isupper(),
                a.name.lower(),
                a.name,
            ),
        )
        names_str = ", ".join(
            f"{a.name} as {a.asname}" if a.asname else a.name for a in sorted_typing
        )
        grouped[1].append(((1, "typing", names_str), f"from typing import {names_str}"))

    if has_future_annotations:
        grouped[0].append(((1, "__future__", "annotations"), "from __future__ import annotations"))

    blocks: list[str] = []
    for grp in (0, 1, 2, 3):
        if grouped[grp]:
            items = [line for _, line in sorted(grouped[grp], key=lambda item: item[0])]
            blocks.append("\n".join(items))

    return "\n\n".join(blocks).splitlines() if blocks else []


def get_contract() -> APIContract:
    """Load API schema contracts lazily.

    Returns:
        The unified API contract.

    Raises:
        FileNotFoundError: If no contract specification can be located.
        RuntimeError: If the contract specification contains no valid models.
    """
    global _CONTRACT_CACHE
    if _CONTRACT_CACHE is not None:
        return _CONTRACT_CACHE

    builder = ContractBuilder()

    # Priority 1: OpenAPI if provided via IMEDNET_OPENAPI_PATH
    openapi_path = os.environ.get("IMEDNET_OPENAPI_PATH")
    if openapi_path:
        if not os.path.exists(openapi_path):
            raise FileNotFoundError(
                f"Configured OpenAPI specification file not found: {openapi_path}"
            )
        builder.ingest_openapi(openapi_path)
    else:
        # Priority 2: Postman override if provided via IMEDNET_POSTMAN_PATH
        postman_path = os.environ.get("IMEDNET_POSTMAN_PATH")
        if postman_path:
            if not os.path.exists(postman_path):
                raise FileNotFoundError(
                    f"Configured Postman collection file not found: {postman_path}"
                )
            builder.ingest_postman(postman_path)
        else:
            # Priority 3: Bundled package resource via importlib.resources
            try:
                resource = importlib.resources.files("imednet.resources").joinpath(
                    "imednet.postman_collection.json"
                )
            except Exception:
                resource = importlib.resources.files("imednet").joinpath(
                    "resources", "imednet.postman_collection.json"
                )

            if resource.is_file():
                with importlib.resources.as_file(resource) as bundled_path:
                    builder.ingest_postman(str(bundled_path))
            else:
                # Local dev checkout fallback
                local_dev_path = os.path.join(
                    os.path.dirname(
                        os.path.dirname(
                            os.path.dirname(
                                os.path.dirname(
                                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                                )
                            )
                        )
                    ),
                    "imednet.postman_collection.json",
                )
                if os.path.exists(local_dev_path):
                    builder.ingest_postman(local_dev_path)
                else:
                    raise FileNotFoundError(
                        "API contract specification could not be found via package resources or local workspace."
                    )

    if not builder.contract.models:
        raise RuntimeError("No valid models could be loaded from contract specification.")

    _CONTRACT_CACHE = builder.contract
    return _CONTRACT_CACHE


def to_snake(name: str) -> str:
    """Convert camelCase or PascalCase strings to snake_case."""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


try:
    from opentelemetry import trace as _trace

    tracer: Any = _trace.get_tracer(__name__)
except Exception:
    tracer = None


class ModelEngine:
    """Engine for dynamically creating Pydantic models from schemas."""

    _model_cache: ClassVar[dict[tuple[str, type[Any]], type[Any]]] = {}

    @classmethod
    def get_model(cls, model_name: str, base_cls: type[Any] = ImednetBaseModel) -> type[Any]:
        """Get or create a dynamic Pydantic model for the given name.

        Args:
            model_name: The name of the model (e.g., 'Subject', 'Record').
            base_cls: The base class to inherit from.

        Returns:
            A Pydantic model class.
        """
        cache_key = (model_name, base_cls)
        if cache_key in cls._model_cache:
            return cls._model_cache[cache_key]

        if tracer:
            with tracer.start_as_current_span("ModelEngine.get_model") as span:
                span.set_attribute("model_name", model_name)
                model = cls._get_model(model_name, base_cls)
        else:
            model = cls._get_model(model_name, base_cls)

        cls._model_cache[cache_key] = model
        return model

    @classmethod
    def _get_model(cls, model_name: str, base_cls: type[Any] = ImednetBaseModel) -> type[Any]:
        """Internal implementation for dynamic model creation."""
        contract = get_contract()
        if model_name not in contract.models:
            return create_model(model_name, __base__=base_cls)

        model_def = contract.models[model_name]
        fields: dict[str, Any] = {}

        for snake_key, field_def in model_def.fields.items():
            # If the base class already defined this field, don't overwrite it
            if snake_key in base_cls.model_fields:
                continue

            # Map internal type name to Python type
            if field_def.type_name == "string":
                py_type: Any = str | None
            elif field_def.type_name == "integer":
                py_type = int | None
            elif field_def.type_name == "boolean":
                py_type = bool | None
            elif field_def.type_name == "dict":
                py_type = dict[str, Any] | None
            elif field_def.type_name == "list":
                py_type = list[Any] | None
            else:
                py_type = Any

            new_field = Field(default=None, alias=field_def.alias)
            fields[snake_key] = (py_type, new_field)

        model = create_model(model_name, __base__=base_cls, **fields)
        return model  # noqa: RET504

    @classmethod
    def generate_stubs(cls, output_dir: str) -> None:
        """Generate type stubs for dynamic models."""
        if tracer:
            with tracer.start_as_current_span("ModelEngine.generate_stubs"):
                cls._generate_stubs(output_dir)
        else:
            cls._generate_stubs(output_dir)

    @classmethod
    def _emit_class_stub(
        cls,
        cls_node: ast.ClassDef,
        dynamic_model: type[Any] | None = None,
        override_base_name: str | None = None,
    ) -> tuple[str, list[str]]:
        """Render a ClassDef AST node and optional dynamic model fields as `.pyi` lines."""
        bases_list = (
            [override_base_name]
            if override_base_name and override_base_name != cls_node.name
            else [_format_expr(b) for b in cls_node.bases]
        )
        for kw in cls_node.keywords:
            if kw.arg:
                bases_list.append(f"{kw.arg}={_format_expr(kw.value)}")
            else:
                bases_list.append(f"**{_format_expr(kw.value)}")

        bases_str = f"({', '.join(bases_list)})" if bases_list else ""
        header_lines = [f"@{_format_expr(dec)}" for dec in cls_node.decorator_list]
        header = f"class {cls_node.name}{bases_str}:"

        is_enum = _is_enum_class(cls_node)
        doc_lines: list[str] = []
        field_lines: list[str] = []
        method_lines: list[str] = []
        declared_fields: set[str] = set()

        body_stmts = list(cls_node.body)
        if body_stmts:
            cls_doc = _extract_docstring(body_stmts[0])
            if cls_doc is not None:
                body_stmts.pop(0)
                doc_lines = _format_docstring(cls_doc, indent="    ")

        for stmt in body_stmts:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                field_name = stmt.target.id
                declared_fields.add(field_name)
                ann_part = f"    {field_name}: {_format_expr(stmt.annotation)}"
                if stmt.value is None:
                    field_lines.append(ann_part)
                else:
                    val_str = _format_expr(stmt.value)
                    single = f"{ann_part} = {val_str}"
                    if len(single) <= 100 or not isinstance(stmt.value, ast.Call):
                        field_lines.append(single)
                    else:
                        func_str = _format_expr(stmt.value.func)
                        call_parts = [_format_expr(a) for a in stmt.value.args] + [
                            f"{kw.arg}={_format_expr(kw.value)}"
                            if kw.arg
                            else f"**{_format_expr(kw.value)}"
                            for kw in stmt.value.keywords
                        ]
                        joined_call = f"        {', '.join(call_parts)}"
                        field_lines.append(f"{ann_part} = {func_str}(")
                        if len(joined_call) <= 100:
                            field_lines.append(joined_call)
                        else:
                            for cp in call_parts:
                                field_lines.append(f"        {cp},")
                        field_lines.append("    )")
            elif is_enum and isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        declared_fields.add(target.id)
                        field_lines.append(f"    {target.id} = {_format_expr(stmt.value)}")
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_lines.extend(_format_function_stub(stmt, indent="    "))

        if dynamic_model is not None:
            for field_name, field_info in dynamic_model.model_fields.items():
                if field_name in declared_fields:
                    continue
                ann_str = _format_runtime_annotation(field_info.annotation)
                field_lines.append(f"    {field_name}: {ann_str}")

        if not doc_lines and not field_lines and not method_lines:
            return ("class_single", [*header_lines, f"{header} ..."])

        out = [*header_lines, header]
        sections: list[list[str]] = [s for s in (doc_lines, field_lines, method_lines) if s]
        for idx, section in enumerate(sections):
            if idx > 0:
                out.append("")
            out.extend(section)
        return ("class_multi", out)

    @classmethod
    def _render_module_stub(cls, py_file: str, source: str) -> str | None:
        """Parse a model `.py` file into a `.pyi` stub string, or None if not a dynamic model."""
        tree = ast.parse(source, filename=py_file)
        model_targets: list[tuple[str, str, str | None]] = []
        for stmt in tree.body:
            extracted = _extract_get_model_call(stmt)
            if extracted is not None:
                model_targets.append(extracted)

        if not model_targets:
            return None

        resolved_models = _load_module_models(py_file, model_targets)
        target_map: dict[str, tuple[str, str | None]] = {
            target_name: (model_name, base_name)
            for target_name, model_name, base_name in model_targets
        }
        class_names_in_ast = {stmt.name for stmt in tree.body if isinstance(stmt, ast.ClassDef)}

        leading_comments: list[str] = []
        for raw_line in source.splitlines():
            if raw_line.startswith("# pylint:"):
                leading_comments.append(raw_line)
            elif not raw_line.strip() or raw_line.startswith("#"):
                continue
            else:
                break

        module_doc_lines: list[str] = []
        import_nodes: list[ast.Import | ast.ImportFrom] = []
        body_blocks: list[tuple[str, list[str]]] = []

        stmts = list(tree.body)
        if stmts:
            mod_doc = _extract_docstring(stmts[0])
            if mod_doc is not None:
                stmts.pop(0)
                module_doc_lines = _format_docstring(mod_doc)

        for stmt in stmts:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                import_nodes.append(stmt)
                continue

            extracted = _extract_get_model_call(stmt)
            if extracted is not None:
                target_name, _model_name, base_name = extracted
                if target_name not in class_names_in_ast:
                    synth_mod = ast.parse(
                        f"class {target_name}({base_name or 'ImednetBaseModel'}): ..."
                    )
                    synth_cls = synth_mod.body[0]
                    if isinstance(synth_cls, ast.ClassDef):
                        synth_cls.body = []
                        body_blocks.append(
                            cls._emit_class_stub(synth_cls, resolved_models.get(target_name))
                        )
                continue

            if isinstance(stmt, ast.ClassDef):
                dyn_model = resolved_models.get(stmt.name)
                override_base = target_map[stmt.name][1] if stmt.name in target_map else None
                body_blocks.append(cls._emit_class_stub(stmt, dyn_model, override_base))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_blocks.append(("func", _format_function_stub(stmt)))
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                body_blocks.append(
                    ("ann_assign", [f"{stmt.target.id}: {_format_expr(stmt.annotation)}"])
                )
            elif isinstance(stmt, ast.Assign):
                targets_str = " = ".join(_format_expr(t) for t in stmt.targets)
                body_blocks.append(("assign", [f"{targets_str} = {_format_expr(stmt.value)}"]))

        rendered_body_lines: list[str] = []
        prev_kind: str | None = None
        for kind, block_lines in body_blocks:
            if prev_kind is not None:
                same_compact = kind == prev_kind and kind in {
                    "class_single",
                    "func",
                    "ann_assign",
                    "assign",
                }
                if not same_compact:
                    rendered_body_lines.append("")
            rendered_body_lines.extend(block_lines)
            prev_kind = kind

        used_names = _collect_used_names("\n".join(rendered_body_lines))
        import_lines = _build_pruned_imports(import_nodes, used_names)

        final_lines: list[str] = []
        if leading_comments:
            final_lines.extend(leading_comments)
        if module_doc_lines:
            final_lines.extend(module_doc_lines)
        if import_lines:
            if module_doc_lines or leading_comments:
                final_lines.append("")
            final_lines.extend(import_lines)
        if rendered_body_lines:
            if final_lines:
                final_lines.append("")
            final_lines.extend(rendered_body_lines)

        return "\n".join(final_lines) + "\n"

    @classmethod
    def _generate_stubs(cls, output_dir: str) -> None:
        """Internal implementation for generating type stubs via AST inspection."""
        for py_file in sorted(glob.glob(os.path.join(output_dir, "*.py"))):
            if os.path.basename(py_file) in _SKIP_STUB_FILES:
                continue

            with open(py_file, encoding="utf-8") as f:
                source = f.read()

            stub_content = cls._render_module_stub(py_file, source)
            if stub_content is None:
                continue

            pyi_path = f"{py_file}i"
            with open(pyi_path, "w", encoding="utf-8") as f:
                f.write(stub_content)


class ResourceRegistry:
    """Unified resource governance registry serving as the single source of truth for fields."""

    @classmethod
    def get_fields(cls, model_name: str) -> list[str]:
        """Get the field names for a specific dynamic model.

        Args:
            model_name: The name of the model (e.g., 'Subject', 'Record').

        Returns:
            A list of field names in snake_case.
        """
        model = ModelEngine.get_model(model_name)
        return list(model.model_fields.keys())


def _run_cli(argv: list[str] | None = None) -> int:
    """Generate or verify `.pyi` model stubs from the command line."""
    import argparse
    import difflib

    parser = argparse.ArgumentParser(description="Generate or verify iMedNet model `.pyi` stubs.")
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="Directory containing dynamic model `.py` modules.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed `.pyi` stubs match freshly generated stubs without modifying files.",
    )
    args = parser.parse_args(argv)
    target_dir = os.path.abspath(args.output_dir)

    if not args.check:
        ModelEngine.generate_stubs(target_dir)
        return 0

    generated: dict[str, list[str]] = {}
    for py_file in sorted(glob.glob(os.path.join(target_dir, "*.py"))):
        if os.path.basename(py_file) in _SKIP_STUB_FILES:
            continue
        with open(py_file, encoding="utf-8") as f:
            source = f.read()
        rendered = ModelEngine._render_module_stub(py_file, source)
        if rendered is not None:
            generated[f"{os.path.basename(py_file)}i"] = rendered.splitlines(keepends=True)

    committed = {
        os.path.basename(p): p for p in sorted(glob.glob(os.path.join(target_dir, "*.pyi")))
    }

    drift = False
    for name in sorted(set(generated) | set(committed)):
        if name not in committed:
            sys.stderr.write(f"Missing committed stub: {name}\n")
            drift = True
            continue
        if name not in generated:
            sys.stderr.write(f"Unexpected committed stub: {name}\n")
            drift = True
            continue
        with open(committed[name], encoding="utf-8") as cf:
            committed_lines = cf.readlines()
        generated_lines = generated[name]
        if committed_lines != generated_lines:
            drift = True
            diff = difflib.unified_diff(
                committed_lines,
                generated_lines,
                fromfile=f"committed/{name}",
                tofile=f"generated/{name}",
            )
            sys.stderr.writelines(diff)

    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(_run_cli())
