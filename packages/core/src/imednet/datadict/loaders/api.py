"""API loader for extracting data dictionary from live iMednet EDC SDK."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from unittest.mock import NonCallableMock

from pydantic import BaseModel

from imednet.utils.async_bridge import (
    SyncToAsyncFacadeAdapter,
    is_async_facade,
    run_sync_over_async,
)

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

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

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


def _is_truthy_flag(val: Any) -> bool:
    """Return True only when ``val`` is explicitly boolean True or a truthy string flag."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "yes", "y", "1"}
    return False


def _is_retired_api_variable(item: Any) -> bool:
    """Return True if an API variable item is marked deleted or disabled."""
    return _is_truthy_flag(_extract_attr(item, "deleted")) or _is_truthy_flag(
        _extract_attr(item, "disabled")
    )


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


def can_load_from_api(sdk: Any) -> bool:
    """Return True if *sdk* exposes forms and variables endpoints or facade methods."""
    if sdk is None:
        return False
    if isinstance(sdk, NonCallableMock):
        mock_children = getattr(sdk, "_mock_children", {})
        obj_vars = vars(sdk)
        has_forms = any(
            k in mock_children or k in obj_vars for k in ("forms", "get_forms", "async_get_forms")
        )
        has_vars = any(
            k in mock_children or k in obj_vars
            for k in ("variables", "get_variables", "async_get_variables")
        )
        return has_forms and has_vars
    has_forms_attr = any(hasattr(sdk, attr) for attr in ("forms", "get_forms", "async_get_forms"))
    has_vars_attr = any(
        hasattr(sdk, attr) for attr in ("variables", "get_variables", "async_get_variables")
    )
    return has_forms_attr and has_vars_attr


def _build_datadict_from_items(
    study_key: str,
    forms_list: Iterable[Any],
    variables_list: Iterable[Any],
) -> DataDictionary:
    """Construct a canonical DataDictionary from iterable form and variable payloads."""
    warnings: list[str] = [
        "Business logic rules and choices are not exposed by the standard REST API "
        "and require a CSV data dictionary export job. Forms and variables loaded successfully."
    ]

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

    for idx, item in enumerate(variables_list):
        f_key = str(_extract_attr(item, "form_key", "formKey", "form_name", "form") or "")
        f_id = _extract_attr(item, "form_id", "formId")
        if _is_retired_api_variable(item):
            if f_key:
                if f_key not in forms_map:
                    forms_map[f_key] = Form(
                        formKey=f_key,
                        formId=normalize_form_id(f_id, form_key=f_key),
                        formType=normalize_form_type(None, default="Subject"),
                    )
                elif forms_map[f_key].formId is None and f_id is not None:
                    forms_map[f_key].formId = normalize_form_id(f_id, form_key=f_key)
            continue
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


async def _fetch_async_collection(
    sdk: Any,
    study_key: str,
    *,
    endpoint_attr: str,
    facade_method: str,
    legacy_async_method: str,
) -> list[Any]:
    async def _consume(candidate: Any) -> list[Any]:
        if candidate is None:
            return []
        if inspect.isawaitable(candidate):
            resolved = await candidate
            return list(resolved) if isinstance(resolved, Iterable) else []
        if hasattr(candidate, "__aiter__"):
            return [item async for item in candidate]
        if isinstance(candidate, Iterable):
            return list(candidate)
        return []

    if isinstance(sdk, NonCallableMock):
        mock_children = getattr(sdk, "_mock_children", {})
        obj_vars = vars(sdk)
        if legacy_async_method in mock_children or legacy_async_method in obj_vars:
            return await _consume(getattr(sdk, legacy_async_method)(study_key=study_key))
        if endpoint_attr in mock_children or endpoint_attr in obj_vars:
            return await _consume(getattr(sdk, endpoint_attr).list(study_key=study_key))
        if facade_method in mock_children or facade_method in obj_vars:
            return await _consume(getattr(sdk, facade_method)(study_key=study_key))
        return []

    try:
        inst_dict = object.__getattribute__(sdk, "__dict__")
    except AttributeError:
        inst_dict = {}

    if legacy_async_method in inst_dict and facade_method not in inst_dict:
        return await _consume(inst_dict[legacy_async_method](study_key=study_key))

    ep = getattr(sdk, endpoint_attr, None)
    if ep is not None and callable(getattr(ep, "list", None)):
        return await _consume(ep.list(study_key=study_key))
    facade_fn = getattr(sdk, facade_method, None)
    if callable(facade_fn):
        return await _consume(facade_fn(study_key=study_key))
    legacy_fn = getattr(sdk, legacy_async_method, None)
    if callable(legacy_fn):
        return await _consume(legacy_fn(study_key=study_key))
    return []


async def async_load_from_api(
    sdk: AsyncImednetFacade,
    study_key: str,
    *,
    no_raw_xml: bool = False,
) -> DataDictionary:
    """Asynchronously extract data dictionary metadata from live iMednet API via the SDK.

    Note: The iMednet REST API exposes study forms and variables endpoints, but does
    not offer direct REST endpoints for business logic rules or choice code lists.
    This canonical async loader extracts available forms and variables concurrently
    and returns a canonical :class:`DataDictionary` with documented partial structure.

    Args:
        sdk: Initialized :class:`AsyncImednetSDK` or :class:`AsyncImednetFacade` instance.
        study_key: Unique study identifier.
        no_raw_xml: Whether to omit raw XML (reserved for export jobs).

    Returns:
        Canonical :class:`DataDictionary` representation.
    """
    _ = no_raw_xml
    if type(sdk).__name__ == "ImednetSDK":
        raise TypeError(
            "async_load_from_api requires an AsyncImednetSDK or AsyncImednetFacade instance; "
            "for synchronous ImednetSDK, call load_from_api(sdk, study_key) instead."
        )
    if not isinstance(sdk, SyncToAsyncFacadeAdapter) and not can_load_from_api(sdk):
        raise TypeError(
            "sdk must expose forms and variables endpoints or AsyncImednetFacade methods."
        )

    forms_list, variables_list = await asyncio.gather(
        _fetch_async_collection(
            sdk,
            study_key,
            endpoint_attr="forms",
            facade_method="get_forms",
            legacy_async_method="async_get_forms",
        ),
        _fetch_async_collection(
            sdk,
            study_key,
            endpoint_attr="variables",
            facade_method="get_variables",
            legacy_async_method="async_get_variables",
        ),
    )
    return _build_datadict_from_items(study_key, forms_list, variables_list)


def load_from_api(
    sdk: ImednetFacade,
    study_key: str,
    *,
    no_raw_xml: bool = False,
) -> DataDictionary:
    """Extract data dictionary metadata from live iMednet API via the synchronous SDK.

    Delegates to the canonical :func:`async_load_from_api` pipeline over a
    sync-to-async facade adapter while safely handling active event loops in
    Jupyter and Pyodide.

    Args:
        sdk: Initialized :class:`ImednetSDK` or :class:`ImednetFacade` instance.
        study_key: Unique study identifier.
        no_raw_xml: Whether to omit raw XML (reserved for export jobs).

    Returns:
        Canonical :class:`DataDictionary` representation.

    Raises:
        TypeError: If an :class:`AsyncImednetSDK` or async facade is passed.
    """
    if is_async_facade(sdk):
        raise TypeError(
            "load_from_api requires a synchronous ImednetSDK or ImednetFacade instance; "
            "for AsyncImednetSDK, await async_load_from_api(sdk, study_key) instead."
        )
    if not can_load_from_api(sdk):
        raise TypeError("sdk must expose forms and variables endpoints or ImednetFacade methods.")

    adapter = SyncToAsyncFacadeAdapter(sdk)
    return run_sync_over_async(
        lambda: async_load_from_api(adapter, study_key, no_raw_xml=no_raw_xml)
    )
