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
                py_type = str | None
            elif field_def.type_name == "integer":
                py_type = int | None  # type: ignore
            elif field_def.type_name == "boolean":
                py_type = bool | None  # type: ignore
            else:
                py_type = Any  # type: ignore

            new_field = Field(default=field_def.default_value, alias=field_def.alias)
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
    def _generate_stubs(cls, output_dir: str) -> None:
        """Internal implementation for generating type stubs."""
        import glob
        import re

        for py_file in glob.glob(os.path.join(output_dir, "*.py")):
            if (
                os.path.basename(py_file) == "__init__.py"
                or os.path.basename(py_file) == "engine.py"
                or os.path.basename(py_file) == "contract.py"
            ):
                continue

            with open(py_file) as f:
                content = f.read()

            matches = re.findall(
                r"([A-Za-z0-9_]+)\s*=\s*ModelEngine\.get_model\(['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z0-9_]+)\)",
                content,
            )
            if not matches:
                continue

            pyi_content_str = content

            imports_re = re.search(r"^(.*?)(?=\n\nclass|\nclass)", pyi_content_str, re.DOTALL)
            if imports_re:
                original_imports = imports_re.group(1)
                new_imports = original_imports
                if "from typing import" not in new_imports:
                    new_imports += "\nfrom typing import Any, Optional\n"
                else:
                    if "Any" not in new_imports:
                        new_imports = new_imports.replace(
                            "from typing import ", "from typing import Any, "
                        )
                    if "Optional" not in new_imports:
                        new_imports = new_imports.replace(
                            "from typing import ", "from typing import Optional, "
                        )
                pyi_content_str = pyi_content_str.replace(original_imports, new_imports, 1)

            for class_name, model_name, base_class_name in matches:  # noqa: B007
                model = cls.get_model(model_name)

                pattern = rf"(class {class_name}\([^)]*\):.*?)\n+{class_name}\s*=\s*ModelEngine\.get_model\(['\"]{model_name}['\"]\s*,\s*{class_name}\)"

                def replace_func(m: "re.Match[str]") -> str:
                    class_body = str(m.group(1))
                    existing_fields = set(
                        re.findall(r"^\s+([a-zA-Z0-9_]+)\s*:", class_body, re.MULTILINE)
                    )

                    filtered_stub_lines = []
                    has_filtered_fields = False
                    for field_name, field_info in model.model_fields.items():  # noqa: B023
                        if field_name in existing_fields:
                            continue
                        ann = str(field_info.annotation)
                        ann = ann.replace("typing.", "")
                        ann = ann.replace("NoneType", "None")
                        if "Union" in ann and "None" in ann:
                            inner = (
                                ann.replace("Union[", "")
                                .replace(", None]", "")
                                .replace("None, ", "")
                            )
                            ann = f"Optional[{inner}]"
                        filtered_stub_lines.append(f"    {field_name}: {ann}")
                        has_filtered_fields = True

                    if not has_filtered_fields and not existing_fields:
                        filtered_stub_lines.append("    pass")

                    s = "\n".join(filtered_stub_lines)
                    return class_body + "\n\n" + s

                pyi_content_str = re.sub(pattern, replace_func, pyi_content_str, flags=re.DOTALL)

            pyi_path = py_file + "i"
            with open(pyi_path, "w") as f:
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
