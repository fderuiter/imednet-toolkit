"""API loader for extracting data dictionary from live iMednet EDC SDK."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..models import (
    DataDictionary,
    Form,
    Metadata,
    Variable,
)
from ..normalize import (
    normalize_form_id,
    normalize_form_type,
    normalize_variable_type,
    to_camel_case,
)

_EXCLUDED_FORM_EXTRA_KEYS = {
    "studyKey",
    "formKey",
    "form",
    "formName",
    "name",
    "formId",
    "id",
    "formType",
    "type",
}

_EXCLUDED_VARIABLE_EXTRA_KEYS = {
    "studyKey",
    "formKey",
    "form",
    "formName",
    "formId",
    "variableName",
    "name",
    "label",
    "prompt",
    "variableType",
    "type",
    "sequence",
    "order",
    "required",
    "requiredField",
    "format",
    "precision",
}


def _extract_attr(obj: Any, *keys: str) -> Any:
    """Extract first found attribute or dictionary key from obj."""
    for key in keys:
        if isinstance(obj, dict):
            if key in obj and obj[key] is not None:
                return obj[key]
            camel = to_camel_case(key)
            if camel in obj and obj[camel] is not None:
                return obj[camel]
        elif hasattr(obj, key):
            val = getattr(obj, key)
            if val is not None and not callable(val):
                return val
    return None


def _extract_extra_attributes(obj: Any, excluded_camel_keys: set[str]) -> dict[str, Any]:
    """Extract non-null extra properties from a dict, Pydantic model, or object into camelCase."""
    raw_items: dict[str, Any]
    if isinstance(obj, dict):
        raw_items = obj
    elif isinstance(obj, BaseModel):
        raw_items = obj.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif hasattr(obj, "__dict__") and type(obj).__module__ != "unittest.mock":
        raw_items = {
            k: v for k, v in vars(obj).items() if not k.startswith("_") and not callable(v)
        }
    else:
        return {}

    extra: dict[str, Any] = {}
    for k, v in raw_items.items():
        if v is None:
            continue
        camel = to_camel_case(str(k))
        if camel and camel not in excluded_camel_keys:
            extra[camel] = v
    return extra


def load_from_api(
    sdk: Any,
    study_key: str,
    *,
    no_raw_xml: bool = False,
) -> DataDictionary:
    """Extract data dictionary metadata from live iMednet API via the SDK.

    Note: The iMednet REST API exposes study forms and variables endpoints, but does
    not offer direct REST endpoints for business logic rules or choice code lists.
    This loader extracts available forms and variables and returns a canonical
    DataDictionary with documented partial structure (empty logic and choices).

    Args:
        sdk: Initialized ImednetSDK instance.
        study_key: Unique study identifier.
        no_raw_xml: Whether to omit raw XML (reserved for export jobs).

    Returns:
        Canonical DataDictionary representation.
    """
    warnings: list[str] = [
        "Business logic rules and choices are not exposed by the standard REST API "
        "and require a CSV data dictionary export job. Forms and variables loaded successfully."
    ]

    # 1. Fetch Forms
    forms_list = sdk.forms.list(study_key=study_key)
    forms_map: dict[str, Form] = {}

    for item in forms_list:
        f_key = str(_extract_attr(item, "form_key", "formKey", "form") or "")
        f_name = _extract_attr(item, "form_name", "formName", "name")
        f_id = _extract_attr(item, "form_id", "formId", "id")
        f_type = _extract_attr(item, "form_type", "formType", "type")
        extra = _extract_extra_attributes(item, _EXCLUDED_FORM_EXTRA_KEYS)

        if f_key:
            forms_map[f_key] = Form(
                formKey=f_key,
                formName=str(f_name) if f_name is not None else None,
                formId=normalize_form_id(f_id, form_key=f_key),
                formType=normalize_form_type(
                    str(f_type) if f_type is not None else None,
                    default="Subject",
                ),
                extraAttributes=extra,
            )

    # 2. Fetch Variables
    variables_list = sdk.variables.list(study_key=study_key)
    for idx, item in enumerate(variables_list):
        f_key = str(_extract_attr(item, "form_key", "formKey", "form_name", "form") or "")
        v_name = str(
            _extract_attr(item, "variable_name", "variableName", "variable_oid", "name") or ""
        )
        lbl = _extract_attr(item, "label", "prompt")
        v_type = _extract_attr(item, "variable_type", "variableType", "type")
        seq_val = _extract_attr(item, "sequence", "order")
        seq = int(seq_val) if seq_val is not None and str(seq_val).isdigit() else (idx + 1)
        req_raw = _extract_attr(item, "required", "required_field", "requiredField")
        if isinstance(req_raw, bool):
            req: str | None = "Yes" if req_raw else "No"
        elif req_raw is not None:
            req = str(req_raw)
        else:
            req = None
        fmt = _extract_attr(item, "format")
        prec = _extract_attr(item, "precision")
        prec_str = str(prec) if prec is not None else None

        extra = _extract_extra_attributes(item, _EXCLUDED_VARIABLE_EXTRA_KEYS)

        if not v_name:
            continue

        var = Variable(
            variableName=v_name,
            label=str(lbl) if lbl is not None else None,
            variableType=normalize_variable_type(
                str(v_type) if v_type is not None else None,
                precision=prec_str,
            ),
            sequence=seq,
            required=req,
            format=str(fmt) if fmt is not None else None,
            precision=prec_str,
            extraAttributes=extra,
        )

        if f_key in forms_map:
            forms_map[f_key].variables.append(var)
        else:
            # If form not known or variable form is unspecified, create a form container
            if not f_key:
                f_key = "UNASSIGNED"
            if f_key not in forms_map:
                forms_map[f_key] = Form(
                    formKey=f_key,
                    formType=normalize_form_type(None, default="Subject"),
                )
            forms_map[f_key].variables.append(var)

    metadata = Metadata.create(
        study_key=study_key,
        source="api",
        validation_warnings=warnings,
    )

    return DataDictionary(
        metadata=metadata,
        forms=list(forms_map.values()),
        unassignedBusinessLogic=[],
    )
