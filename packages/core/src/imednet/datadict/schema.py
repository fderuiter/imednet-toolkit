"""JSON Schema generation and validation for iMednet Data Dictionary."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .logic import LogicTreeEvaluator
from .models import BusinessLogicRule, DataDictionary
from .normalize import _is_file_target, _safe_is_file

SCHEMA_PATH = Path(__file__).parent / "resources" / "data-dictionary-1.0.0.schema.json"
_RULE_VALIDATOR = LogicTreeEvaluator()

__all__ = [
    "SCHEMA_PATH",
    "_is_file_target",
    "_safe_is_file",
    "get_schema",
    "validate",
]


def _validate_rule_list(
    rules: list[BusinessLogicRule],
    loc_prefix: str,
) -> list[str]:
    errors: list[str] = []
    for idx, rule in enumerate(rules):
        rule_label = str(rule.name or rule.id or f"index {idx}")
        for issue in _RULE_VALIDATOR.validate_rule_regex_patterns(rule):
            errors.append(f"[{loc_prefix}.{idx}.logic] Rule '{rule_label}': {issue}")
    return errors


def _validate_business_logic_regexes(dd: DataDictionary) -> list[str]:
    errors: list[str] = []
    for form_idx, form in enumerate(dd.forms):
        errors.extend(_validate_rule_list(form.businessLogic, f"forms.{form_idx}.businessLogic"))
    errors.extend(_validate_rule_list(dd.unassignedBusinessLogic, "unassignedBusinessLogic"))
    return errors


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
        if _is_file_target(target):
            if not _safe_is_file(p):
                return [f"FileNotFoundError: Target file '{target}' does not exist"]
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                return [f"Failed to read JSON file '{target}': {exc}"]
        elif _safe_is_file(p):
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

    # 2. If a custom JSON schema is provided, validate via jsonschema
    if schema is not None:
        try:
            import jsonschema
        except ImportError as exc:
            raise ImportError(
                "Validating against an explicit JSON Schema requires the 'jsonschema' package."
            ) from exc

        validator = jsonschema.Draft202012Validator(schema)
        for err in validator.iter_errors(data):
            loc = ".".join(str(p) for p in err.absolute_path)
            prefix = f"[{loc}] " if loc else ""
            errors.append(f"{prefix}{err.message}")
        if not errors:
            with contextlib.suppress(ValidationError):
                dd_checked = DataDictionary.model_validate(data)
                errors.extend(_validate_business_logic_regexes(dd_checked))
        return errors

    # 3. Model validation via Pydantic
    try:
        dd = DataDictionary.model_validate(data)
    except ValidationError as val_err:
        for err in val_err.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "Validation error")
            errors.append(f"[{loc}] {msg}")
    else:
        errors.extend(_validate_business_logic_regexes(dd))

    return errors
