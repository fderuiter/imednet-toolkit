"""API loader for extracting data dictionary from live iMednet EDC SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import (
    DataDictionary,
    Form,
    Metadata,
    Variable,
)
from ..normalize import to_camel_case


def _extract_attr(obj: Any, *keys: str) -> Any:
    """Extract first found attribute or dictionary key from obj."""
    for key in keys:
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            camel = to_camel_case(key)
            if camel in obj:
                return obj[camel]
        elif hasattr(obj, key):
            val = getattr(obj, key)
            if val is not None and not callable(val):
                return val
    return None


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
        f_key = _extract_attr(item, "form_key", "formKey", "form") or ""
        f_name = _extract_attr(item, "form_name", "formName", "name")
        f_id = _extract_attr(item, "form_id", "formId", "id")
        f_type = _extract_attr(item, "form_type", "formType", "type")

        # Collect any other fields as extraAttributes
        extra: dict[str, Any] = {}
        if isinstance(item, dict):
            for k, v in item.items():
                camel = to_camel_case(k)
                if camel not in {"formKey", "formName", "formId", "formType"}:
                    extra[camel] = v

        if f_key:
            forms_map[f_key] = Form(
                formKey=f_key,
                formName=f_name,
                formId=f_id,
                formType=f_type,
                extraAttributes=extra,
            )

    # 2. Fetch Variables
    variables_list = sdk.variables.list(study_key=study_key)
    for idx, item in enumerate(variables_list):
        f_key = _extract_attr(item, "form_key", "formKey", "form_name", "form") or ""
        v_name = _extract_attr(item, "variable_name", "variableName", "variable_oid", "name") or ""
        lbl = _extract_attr(item, "label", "prompt")
        v_type = _extract_attr(item, "variable_type", "variableType", "type")
        seq_val = _extract_attr(item, "sequence", "order")
        seq = int(seq_val) if seq_val is not None and str(seq_val).isdigit() else (idx + 1)
        req = _extract_attr(item, "required", "required_field", "requiredField")

        extra = {}
        if isinstance(item, dict):
            for k, v in item.items():
                camel = to_camel_case(k)
                if camel not in {
                    "formKey",
                    "form",
                    "variableName",
                    "name",
                    "label",
                    "variableType",
                    "sequence",
                    "required",
                }:
                    extra[camel] = v

        if not v_name:
            continue

        var = Variable(
            variableName=v_name,
            label=lbl,
            variableType=v_type,
            sequence=seq,
            required=req,
            extraAttributes=extra,
        )

        if f_key in forms_map:
            forms_map[f_key].variables.append(var)
        else:
            # If form not known or variable form is unspecified, create a form container
            if not f_key:
                f_key = "UNASSIGNED"
            if f_key not in forms_map:
                forms_map[f_key] = Form(formKey=f_key)
            forms_map[f_key].variables.append(var)

    now_timestamp = datetime.now(timezone.utc).strftime("%m-%d-%Y %H:%M:%S")
    metadata = Metadata(
        schemaVersion="1.0.0",
        studyKey=study_key,
        generatedAt=now_timestamp,
        source="api",
        validationWarnings=warnings,
    )

    return DataDictionary(
        metadata=metadata,
        forms=list(forms_map.values()),
        unassignedBusinessLogic=[],
    )
