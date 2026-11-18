"""Engine module."""

"""Dynamic model generation engine based on unified contract schemas."""

import importlib.resources
import os
import re
from typing import Any, ClassVar

from pydantic import Field, create_model

from imednet.models.base import ImednetBaseModel
from imednet.models.contract import APIContract, ContractBuilder

_CONTRACT_CACHE: APIContract | None = None


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

    @staticmethod
    def _format_stub_annotation(annotation: Any) -> str:
        """Format a runtime field annotation into PEP 604 stub syntax."""
        ann = str(annotation)
        ann = (
            ann.replace("typing.", "")
            .replace("imednet.models.jobs.", "")
            .replace("imednet.models.queries.", "")
        )
        ann = ann.replace("NoneType", "None")
        if ann.startswith("Union[") and ann.endswith("]"):
            inner = ann[len("Union[") : -1]
            parts = [p.strip() for p in inner.split(",")]
            ann = " | ".join(parts)
        return ann

    @classmethod
    def _generate_stubs(cls, output_dir: str) -> None:
        """Internal implementation for generating type stubs."""
        import ast
        import glob
        import importlib

        for py_file in sorted(glob.glob(os.path.join(output_dir, "*.py"))):
            basename = os.path.basename(py_file)
            if basename in {"__init__.py", "engine.py", "contract.py"}:
                continue

            with open(py_file, encoding="utf-8") as f:
                content = f.read()

            matches = re.findall(
                r"^([A-Za-z0-9_]+)\s*=\s*ModelEngine\.get_model\(['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z0-9_]+)\)",
                content,
                flags=re.MULTILINE,
            )
            if not matches:
                continue

            dynamic_map: dict[str, str] = {
                class_name: model_name for class_name, model_name, _ in matches
            }

            mod_name = f"imednet.models.{os.path.splitext(basename)[0]}"
            runtime_mod: Any = None
            try:
                candidate_mod = importlib.import_module(mod_name)
                if candidate_mod.__file__ and os.path.abspath(
                    candidate_mod.__file__
                ) == os.path.abspath(py_file):
                    runtime_mod = candidate_mod
            except Exception:
                runtime_mod = None

            tree = ast.parse(content)
            source_lines = content.splitlines()

            leading_comments: list[str] = []
            for line in source_lines:
                stripped = line.strip()
                if stripped.startswith("#"):
                    leading_comments.append(line)
                elif stripped == "":
                    continue
                else:
                    break

            module_doc: str | None = ast.get_docstring(tree, clean=False)
            import_nodes: list[ast.Import | ast.ImportFrom] = []
            body_chunks: list[str] = []
            prev_was_plain_func = False

            def _format_func_stub(
                fn_node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str = ""
            ) -> list[str]:
                lines: list[str] = []
                for dec in fn_node.decorator_list:
                    lines.append(f"{indent}@{ast.unparse(dec)}")
                prefix = "async def" if isinstance(fn_node, ast.AsyncFunctionDef) else "def"
                args_str = ast.unparse(fn_node.args)
                ret_str = f" -> {ast.unparse(fn_node.returns)}" if fn_node.returns else ""
                single = f"{indent}{prefix} {fn_node.name}({args_str}){ret_str}: ..."
                if len(single) > 100 and args_str:
                    lines.append(f"{indent}{prefix} {fn_node.name}(")
                    lines.append(f"{indent}    {args_str}")
                    lines.append(f"{indent}){ret_str}: ...")
                else:
                    lines.append(single)
                return lines

            for node in tree.body:
                if (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    # Module docstring handled separately
                    continue
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.ImportFrom) and node.module == "imednet.models.engine":
                        continue
                    import_nodes.append(node)
                    continue
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in dynamic_map
                ):
                    # Skip X = ModelEngine.get_model(...)
                    continue
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    body_chunks.append(f"{node.target.id}: {ast.unparse(node.annotation)}")
                    prev_was_plain_func = False
                    continue
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                ):
                    target_name = node.targets[0].id
                    if isinstance(node.value, ast.Name):
                        body_chunks.append(f"{target_name} = {node.value.id}")
                        prev_was_plain_func = False
                    continue
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith("_"):
                        continue
                    fn_stub = "\n".join(_format_func_stub(node))
                    is_plain = not node.decorator_list and "\n" not in fn_stub
                    if prev_was_plain_func and is_plain and body_chunks:
                        body_chunks[-1] = f"{body_chunks[-1]}\n{fn_stub}"
                    else:
                        body_chunks.append(fn_stub)
                    prev_was_plain_func = is_plain
                    continue
                prev_was_plain_func = False
                if isinstance(node, ast.ClassDef):
                    cls_lines: list[str] = []
                    for dec in node.decorator_list:
                        cls_lines.append(f"@{ast.unparse(dec)}")
                    bases_str = ", ".join(ast.unparse(b) for b in node.bases)
                    header = (
                        f"class {node.name}({bases_str}):" if bases_str else f"class {node.name}:"
                    )
                    cls_lines.append(header)

                    cls_doc = ast.get_docstring(node, clean=False)
                    has_members = False
                    if cls_doc is not None:
                        doc_stmt = node.body[0]
                        doc_src = ast.get_source_segment(content, doc_stmt)
                        if doc_src:
                            cls_lines.append(f"    {doc_src}")
                            has_members = True

                    existing_fields: set[str] = set()
                    field_lines: list[str] = []
                    method_lines: list[str] = []

                    for item in node.body:
                        if (
                            isinstance(item, ast.Expr)
                            and isinstance(item.value, ast.Constant)
                            and isinstance(item.value.value, str)
                        ):
                            continue
                        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                            fname = item.target.id
                            existing_fields.add(fname)
                            seg = ast.get_source_segment(content, item)
                            if seg:
                                indented = "\n".join(
                                    ln if idx > 0 else f"    {ln.lstrip()}"
                                    for idx, ln in enumerate(seg.splitlines())
                                )
                                field_lines.append(indented)
                            else:
                                field_lines.append(f"    {ast.unparse(item)}")
                            has_members = True
                        elif (
                            isinstance(item, ast.Assign)
                            and len(item.targets) == 1
                            and isinstance(item.targets[0], ast.Name)
                        ):
                            # Enum members or class attributes
                            seg = ast.get_source_segment(content, item)
                            field_lines.append(f"    {seg.lstrip() if seg else ast.unparse(item)}")
                            has_members = True
                        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if item.name in {
                                "_missing_",
                                "__setattr__",
                                "_validate_state",
                                "_resolve_legacy_status",
                                "_parse_timestamp_array",
                                "_clean_empty_dates",
                            }:
                                continue
                            method_lines.extend(_format_func_stub(item, indent="    "))
                            has_members = True

                    dynamic_lines: list[str] = []
                    if node.name in dynamic_map:
                        model_name = dynamic_map[node.name]
                        if runtime_mod is not None and hasattr(runtime_mod, node.name):
                            model_obj = getattr(runtime_mod, node.name)
                        else:
                            model_obj = cls.get_model(model_name)
                        for fname, finfo in model_obj.model_fields.items():
                            if fname in existing_fields:
                                continue
                            ann_str = cls._format_stub_annotation(finfo.annotation)
                            dynamic_lines.append(f"    {fname}: {ann_str}")
                            has_members = True

                    if field_lines:
                        if cls_doc is not None:
                            cls_lines.append("")
                        cls_lines.extend(field_lines)
                    if method_lines:
                        if cls_doc is not None or field_lines:
                            cls_lines.append("")
                        cls_lines.extend(method_lines)
                    if dynamic_lines:
                        if cls_doc is not None or field_lines or method_lines:
                            cls_lines.append("")
                        cls_lines.extend(dynamic_lines)

                    if not has_members and cls_doc is None:
                        cls_lines[-1] = f"{cls_lines[-1]} ..."

                    body_chunks.append("\n".join(cls_lines))

            body_text = "\n\n".join(body_chunks)

            # Reconstruct clean imports keeping only symbols used in body_text
            future_imports: list[str] = []
            stdlib_imports: list[str] = []
            thirdparty_imports: list[str] = []
            firstparty_imports: list[str] = []
            typing_names: list[str] = []

            for imp in import_nodes:
                if isinstance(imp, ast.ImportFrom):
                    mod = imp.module or ""
                    if mod == "__future__":
                        future_imports.append(ast.unparse(imp))
                        continue
                    if mod == "typing":
                        for alias in imp.names:
                            sym = alias.asname or alias.name
                            if re.search(rf"\b{re.escape(sym)}\b", body_text):
                                typing_names.append(
                                    alias.name
                                    if not alias.asname
                                    else f"{alias.name} as {alias.asname}"
                                )
                        continue
                    used_aliases: list[str] = []
                    for alias in imp.names:
                        sym = alias.asname or alias.name
                        if re.search(rf"\b{re.escape(sym)}\b", body_text):
                            used_aliases.append(
                                alias.name
                                if not alias.asname
                                else f"{alias.name} as {alias.asname}"
                            )
                    if not used_aliases:
                        continue
                    dots = "." * imp.level
                    stmt = f"from {dots}{mod} import {', '.join(sorted(used_aliases))}"
                    if imp.level > 0 or mod.startswith("imednet"):
                        firstparty_imports.append(stmt)
                    elif mod.split(".")[0] in {
                        "datetime",
                        "enum",
                        "collections",
                        "os",
                        "re",
                        "uuid",
                    }:
                        stdlib_imports.append(stmt)
                    else:
                        thirdparty_imports.append(stmt)
                elif isinstance(imp, ast.Import):
                    for alias in imp.names:
                        sym = alias.asname or alias.name.split(".")[0]
                        if re.search(rf"\b{re.escape(sym)}\b", body_text):
                            stdlib_imports.append(ast.unparse(imp))

            if re.search(r"\bAny\b", body_text) and "Any" not in typing_names:
                typing_names.append("Any")
            if typing_names:
                stdlib_imports.append(f"from typing import {', '.join(sorted(set(typing_names)))}")

            header_sections: list[str] = []
            prefix_block: list[str] = []
            if leading_comments:
                prefix_block.extend(leading_comments)
            if module_doc is not None:
                doc_src = ast.get_source_segment(content, tree.body[0])
                if doc_src:
                    prefix_block.append(doc_src)
            if prefix_block:
                header_sections.append("\n".join(prefix_block))
            if future_imports:
                header_sections.append("\n".join(future_imports))
            if stdlib_imports:
                header_sections.append("\n".join(sorted(stdlib_imports)))
            if thirdparty_imports:
                header_sections.append("\n".join(sorted(thirdparty_imports)))
            if firstparty_imports:
                header_sections.append("\n".join(sorted(firstparty_imports)))

            full_sections = header_sections + ([body_text] if body_text else [])
            pyi_content_str = "\n\n".join(full_sections).rstrip() + "\n"

            pyi_path = py_file + "i"
            with open(pyi_path, "w", encoding="utf-8") as f:
                f.write(pyi_content_str)


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
