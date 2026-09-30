"""JSON Schema generation and validation for iMednet Data Dictionary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import DataDictionary

SCHEMA_PATH = Path(__file__).parent / "resources" / "data-dictionary-1.0.0.schema.json"


def get_schema() -> dict[str, Any]:
    """Generate or retrieve the canonical JSON Schema for DataDictionary.

    Returns:
        JSON Schema dictionary.
    """
    schema = DataDictionary.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "iMednet Data Dictionary Schema"
    schema["version"] = "1.0.0"
    return schema


def validate(
    target: dict[str, Any] | str | Path | DataDictionary,
    schema: dict[str, Any] | None = None,
) -> list[str]:
    """Validate a data dictionary against the canonical model or an optional JSON schema.

    Args:
        target: DataDictionary instance, dict, JSON string, or path to JSON file.
        schema: Optional JSON Schema to validate against via jsonschema.

    Returns:
        List of error strings (empty if valid).
    """
    errors: list[str] = []

    # 1. Load target to dict/object
    data: Any
    if isinstance(target, DataDictionary):
        data = target.model_dump(mode="json")
    elif isinstance(target, str | Path):
        p = Path(target)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                return [f"Failed to read JSON file '{target}': {exc}"]
        else:
            try:
                data = json.loads(str(target))
            except Exception as exc:
                return [f"Failed to parse JSON string: {exc}"]
    elif isinstance(target, dict):
        data = target
    else:
        return [f"Unsupported target type for validation: {type(target)}"]

    # 2. If a custom JSON schema is provided, validate via jsonschema if available
    if schema is not None:
        try:
            import jsonschema

            validator = jsonschema.Draft202012Validator(schema)
            for err in validator.iter_errors(data):
                loc = ".".join(str(p) for p in err.absolute_path)
                prefix = f"[{loc}] " if loc else ""
                errors.append(f"{prefix}{err.message}")
            return errors
        except ImportError:
            errors.append(
                "jsonschema library is not installed; falling back to Pydantic model validation."
            )

    # 3. Model validation via Pydantic
    try:
        DataDictionary.model_validate(data)
    except ValidationError as val_err:
        for err in val_err.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "Validation error")
            errors.append(f"[{loc}] {msg}")

    return errors
