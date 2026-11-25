"""Isolated CLI read-guard helpers for PHI masking, secret redaction, and diagnostics.

Keeping shared read-side PHI sanitization, study-scoped sensitivity resolution, and
plain-language diagnostic formatting in this dedicated private module avoids bloating
high-churn CLI files while exposing narrow, stable contracts via ``imednet.cli.utils``
and ``imednet.spi.cli``. All ``imednet.integrations`` lookups remain lazy so CLI startup
(e.g. ``imednet --help``) never imports heavyweight integration packages.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from contextlib import nullcontext
from typing import Any, TypeVar
from urllib.parse import urlsplit

from imednet.core.context import sdk_governance_context
from imednet.errors import ApiError, CircuitBreakerError, RequestError
from imednet.utils.security import (
    SensitivityRegistry,
    _normalize_key,
    global_sensitivity_registry,
    mask_clinical_phi,
    sanitize_diagnostic_text,
)
from imednet.utils.url import redact_url_query

_MASKED_VALUE = "***MASKED***"
_T = TypeVar("_T")


def _collapse_single_line(text: str) -> str:
    """Normalize internal newlines and runs of whitespace into a single clean line."""
    return " ".join(text.split())


def redact_cli_secrets(
    msg: str,
    *,
    registry: SensitivityRegistry | None = None,
) -> str:
    """Redact environment secrets, credentials, and inline clinical PHI from CLI messages."""
    redacted = msg
    for env_var in ("IMEDNET_API_KEY", "IMEDNET_SECURITY_KEY", "IMEDNET_OIDC_TOKEN"):
        val = os.getenv(env_var)
        if val:
            if val in redacted:
                redacted = redacted.replace(val, "********")
            stripped = val.strip()
            if stripped and stripped in redacted:
                redacted = redacted.replace(stripped, "********")
    return sanitize_diagnostic_text(redacted, registry=registry)


def resolve_study_configuration(study_key: str | None) -> Any:
    """Lazily resolve ``StudyConfiguration`` without importing ``integrations`` at CLI startup."""
    from imednet.integrations.enrichment import resolve_study_configuration as _resolve

    return _resolve(study_key)


def resolve_enrichment_pipeline(study_key: str | None) -> Any:
    """Lazily resolve ``EnrichmentPipeline`` without importing ``integrations`` at CLI startup."""
    from imednet.integrations.enrichment import resolve_enrichment_pipeline as _resolve

    return _resolve(study_key)


def _matches_custom_phi_key(key: str, custom_phi_keys: set[str]) -> bool:
    """Lazily check whether *key* matches normalized custom ``phiFields``."""
    from imednet.integrations.enrichment import _matches_custom_phi_key as _matches

    return _matches(key, custom_phi_keys)


def mask_custom_phi_recursive(value: Any, custom_phi_keys: set[str]) -> Any:
    """Lazily mask custom ``phiFields`` recursively."""
    from imednet.integrations.enrichment import mask_custom_phi_recursive as _mask_custom

    return _mask_custom(value, custom_phi_keys)


def resolve_cli_phi_registry(
    study_key: str | None = None,
    url: str | None = None,
) -> SensitivityRegistry:
    """Build an isolated ``SensitivityRegistry`` including study ``phiFields`` when configured."""
    candidate = study_key.strip() if isinstance(study_key, str) and study_key.strip() else None
    if candidate is None and isinstance(url, str) and "/studies/" in url:
        tail = url.split("/studies/", 1)[1]
        seg = tail.split("/", 1)[0].split("?", 1)[0].strip()
        if seg:
            candidate = seg
    if candidate is None:
        return global_sensitivity_registry
    try:
        cfg = resolve_study_configuration(candidate)
    except Exception:
        return global_sensitivity_registry
    phi_fields = getattr(cfg, "phi_fields", None) if cfg is not None else None
    if not phi_fields:
        return global_sensitivity_registry
    sens_snap, exempt_snap = global_sensitivity_registry.snapshot()
    reg = SensitivityRegistry()
    for key in sens_snap:
        reg.add_sensitive_key(key)
    for key in exempt_snap:
        reg.add_exempt_key(key)
    for custom_key in phi_fields:
        if custom_key:
            reg.add_sensitive_key(str(custom_key))
    return reg


def _format_cli_endpoint(
    method: str | None,
    url: str | None,
    *,
    simulation: bool,
    registry: SensitivityRegistry | None = None,
) -> str | None:
    """Format HTTP method and target URL/path, labeling simulation paths without live hosts."""
    clean_method = method.strip().upper() if isinstance(method, str) and method.strip() else None
    clean_url = (
        redact_url_query(url.strip(), registry=registry)
        if isinstance(url, str) and url.strip()
        else None
    )
    if clean_url is None:
        if simulation and clean_method:
            return f"simulation {clean_method}"
        return clean_method

    if simulation:
        parsed = urlsplit(clean_url)
        sim_path = parsed.path or clean_url
        if parsed.query:
            sim_path = f"{sim_path}?{parsed.query}"
        if clean_method:
            return f"simulation {clean_method} {sim_path}"
        return f"simulation {sim_path}"

    if clean_method:
        return f"{clean_method} {clean_url}"
    return clean_url


def _extract_cli_api_fields(
    exc: ApiError,
    *,
    registry: SensitivityRegistry,
) -> list[str]:
    """Extract formatted offending field pairs from structured ``ApiError`` entries."""
    field_dicts: list[dict[str, Any]] = [
        entry["field"]
        for entry in exc.errors
        if isinstance(entry.get("field"), dict) and entry["field"]
    ]
    if not field_dicts and isinstance(exc.field, dict) and exc.field:
        field_dicts = [exc.field]

    formatted: list[str] = []
    for raw_field in field_dicts:
        attr_raw = raw_field.get("attribute") or raw_field.get("variable")
        attr = str(attr_raw).strip() if attr_raw is not None and str(attr_raw).strip() else None
        if attr is not None and "value" in raw_field:
            val = _MASKED_VALUE if registry.is_sensitive(attr) else raw_field["value"]
            formatted.append(f"{attr}={val!r}")
        elif attr is not None:
            formatted.append(attr)
    return formatted


def format_cli_diagnostic_error(
    exc: BaseException,
    *,
    simulation: bool = False,
    study_key: str | None = None,
) -> str:
    """Format an SDK or transport error into one plain-language redacted CLI diagnostic line."""
    active_registry = resolve_cli_phi_registry(study_key, getattr(exc, "url", None))
    if isinstance(exc, ApiError):
        raw_base = Exception.__str__(exc).strip()
        if exc.description and exc.description.strip():
            primary = exc.description.strip()
        elif raw_base and not (
            (raw_base.startswith("{") and raw_base.endswith("}"))
            or (raw_base.startswith("[") and raw_base.endswith("]"))
        ):
            primary = raw_base
        elif exc.status_code is not None:
            primary = f"HTTP {exc.status_code} request failed"
        else:
            primary = "API request failed"

        meta: list[str] = []
        if exc.status_code is not None and f"HTTP {exc.status_code}" not in primary:
            meta.append(f"HTTP {exc.status_code}")

        formatted_fields = _extract_cli_api_fields(exc, registry=active_registry)
        if formatted_fields:
            meta.append(f"field {'; '.join(formatted_fields)}")

        endpoint_label = _format_cli_endpoint(
            exc.method,
            exc.url,
            simulation=simulation,
            registry=active_registry,
        )
        if endpoint_label:
            meta.append(endpoint_label)

        if exc.retry_after is not None:
            meta.append(f"retry after {exc.retry_after:g}s")

        combined = f"{primary} ({', '.join(meta)})" if meta else primary
        return redact_cli_secrets(_collapse_single_line(combined), registry=active_registry)

    if isinstance(exc, RequestError):
        raw_base = Exception.__str__(exc).strip() or "Network request failed"
        meta = []
        endpoint_label = _format_cli_endpoint(
            exc.method,
            exc.url,
            simulation=simulation,
            registry=active_registry,
        )
        if endpoint_label:
            meta.append(endpoint_label)
        if exc.attempt_count is not None:
            label = "attempt" if exc.attempt_count == 1 else "attempts"
            meta.append(f"{exc.attempt_count} {label}")
        if exc.cause_type:
            meta.append(f"cause: {exc.cause_type}")
        combined = f"{raw_base} ({', '.join(meta)})" if meta else raw_base
        return redact_cli_secrets(_collapse_single_line(combined), registry=active_registry)

    if isinstance(exc, CircuitBreakerError):
        raw_base = (
            Exception.__str__(exc).strip()
            or "Circuit breaker is open; requests are temporarily paused"
        )
        meta = []
        endpoint_label = _format_cli_endpoint(
            getattr(exc, "method", None),
            getattr(exc, "url", None),
            simulation=simulation,
            registry=active_registry,
        )
        if endpoint_label:
            meta.append(endpoint_label)
        combined = f"{raw_base} ({', '.join(meta)})" if meta else raw_base
        return redact_cli_secrets(_collapse_single_line(combined), registry=active_registry)

    return redact_cli_secrets(_collapse_single_line(str(exc)), registry=active_registry)


def _normalize_custom_phi_keys(raw_fields: Collection[str]) -> set[str]:
    """Normalize custom ``phiFields`` into canonical snake_case and collapsed keys."""
    normalized: set[str] = set()
    for raw_key in raw_fields:
        if global_sensitivity_registry.is_exempt(raw_key):
            continue
        norm_key = _normalize_key(raw_key)
        if norm_key:
            normalized.add(norm_key)
            normalized.add(norm_key.replace("_", ""))
    return normalized


def _resolve_study_phi_keys(
    study_key: str | None,
    pipelines: dict[str, Any],
    *,
    sdk: Any = None,
) -> set[str]:
    """Resolve and cache study-configured ``phiFields`` keys for *study_key*."""
    if not isinstance(study_key, str) or not study_key.strip():
        return set()
    sk = study_key.strip()
    if sk not in pipelines:
        ctx = sdk_governance_context(sdk) if sdk is not None else nullcontext()
        with ctx:
            pipelines[sk] = resolve_enrichment_pipeline(sk)
    active_pipeline = pipelines[sk]
    if active_pipeline is None:
        return set()
    return set(getattr(active_pipeline, "phi_keys", ()) or ())


def _is_phi_key(key: str, custom_phi_keys: set[str]) -> bool:
    """Return True if *key* matches default clinical PHI or normalized study ``phiFields``."""
    if global_sensitivity_registry.is_exempt(key):
        return False
    if global_sensitivity_registry.is_sensitive(key):
        return True
    return bool(custom_phi_keys and _matches_custom_phi_key(key, custom_phi_keys))


def is_cli_phi_key_sensitive(
    key: str,
    *,
    custom_phi_fields: Collection[str] | None = None,
) -> bool:
    """Return True if *key* matches default clinical PHI or raw/normalized ``custom_phi_fields``."""
    norm_keys = _normalize_custom_phi_keys(custom_phi_fields) if custom_phi_fields else set()
    return _is_phi_key(key, norm_keys)


def _is_plain_container(value: Any) -> bool:
    """Return True if *value* is a dict/list/tuple tree with no nested Pydantic models."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, dict):
        return all(_is_plain_container(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_is_plain_container(item) for item in value)
    return False


def _sanitize_value_recursive(value: Any, *, custom_phi_keys: set[str]) -> Any:
    """Recursively mask PHI across Pydantic models, mappings, and sequences."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        if isinstance(value, dict) and _is_plain_container(value):
            masked_dict = mask_clinical_phi(value, registry=global_sensitivity_registry)
            if custom_phi_keys:
                masked_dict = mask_custom_phi_recursive(masked_dict, custom_phi_keys)
            return value if masked_dict == value else masked_dict

        changed = False
        rebuilt: dict[Any, Any] = {}
        for k, v in value.items():
            if _is_phi_key(str(k), custom_phi_keys):
                rebuilt[k] = _MASKED_VALUE
                if v != _MASKED_VALUE:
                    changed = True
            else:
                masked_v = _sanitize_value_recursive(v, custom_phi_keys=custom_phi_keys)
                rebuilt[k] = masked_v
                if masked_v is not v and masked_v != v:
                    changed = True
        if not changed and isinstance(value, dict):
            return value
        return rebuilt

    if isinstance(value, list):
        masked_list = [
            _sanitize_value_recursive(item, custom_phi_keys=custom_phi_keys) for item in value
        ]
        if all(
            new_item is old_item for new_item, old_item in zip(masked_list, value, strict=False)
        ):
            return value
        return masked_list

    if isinstance(value, tuple):
        masked_tuple = tuple(
            _sanitize_value_recursive(item, custom_phi_keys=custom_phi_keys) for item in value
        )
        if all(
            new_item is old_item for new_item, old_item in zip(masked_tuple, value, strict=False)
        ):
            return value
        return masked_tuple

    model_fields = getattr(type(value), "model_fields", None)
    model_copy = getattr(value, "model_copy", None)
    if isinstance(model_fields, Mapping) and callable(model_copy):
        updates: dict[str, Any] = {}
        for fname, finfo in model_fields.items():
            raw_val = getattr(value, fname, None)
            if raw_val is None:
                continue
            alias = getattr(finfo, "alias", None)
            if _is_phi_key(fname, custom_phi_keys) or (
                isinstance(alias, str) and _is_phi_key(alias, custom_phi_keys)
            ):
                if isinstance(raw_val, str) and raw_val != _MASKED_VALUE:
                    updates[fname] = _MASKED_VALUE
                continue
            masked_val = _sanitize_value_recursive(raw_val, custom_phi_keys=custom_phi_keys)
            if masked_val is not raw_val and masked_val != raw_val:
                updates[fname] = masked_val

        extra = getattr(value, "__pydantic_extra__", None)
        masked_extra: dict[str, Any] | None = None
        if isinstance(extra, dict) and extra:
            candidate_extra = _sanitize_value_recursive(extra, custom_phi_keys=custom_phi_keys)
            if candidate_extra is not extra and candidate_extra != extra:
                masked_extra = dict(candidate_extra)

        if not updates and masked_extra is None:
            return value

        copied = model_copy(update=updates)
        if masked_extra is not None:
            copied_extra = getattr(copied, "__pydantic_extra__", None)
            if isinstance(copied_extra, dict):
                copied_extra = dict(copied_extra)
                copied_extra.update(masked_extra)
                object.__setattr__(copied, "__pydantic_extra__", copied_extra)
        return copied

    return value


def sanitize_cli_phi(
    payload: _T,
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
    custom_phi_fields: Collection[str] | None = None,
    sdk: Any = None,
) -> _T:
    """Apply default ``mask_clinical_phi`` and ``StudyConfiguration`` ``phiFields`` to *payload*.

    Preserves Pydantic model instances and object identity when unchanged, and returns
    *payload* untouched when ``mask_phi=False``.
    """
    if not mask_phi:
        return payload
    pipelines: dict[str, Any] = {}
    effective_study = study_key
    if effective_study is None:
        if isinstance(payload, Mapping):
            raw_sk = payload.get("studyKey") or payload.get("study_key")
            if isinstance(raw_sk, str):
                effective_study = raw_sk
        else:
            raw_sk = getattr(payload, "study_key", None) or getattr(payload, "studyKey", None)
            if isinstance(raw_sk, str):
                effective_study = raw_sk
    custom_keys = (
        _normalize_custom_phi_keys(custom_phi_fields)
        if custom_phi_fields is not None
        else _resolve_study_phi_keys(effective_study, pipelines, sdk=sdk)
    )
    return _sanitize_value_recursive(payload, custom_phi_keys=custom_keys)  # type: ignore[no-any-return]


def sanitize_cli_records(
    records: Sequence[_T],
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
    sdk: Any = None,
) -> list[_T]:
    """Sanitize a sequence of ``Record`` models or dicts for CLI JSON and interactive views."""
    if not mask_phi:
        return list(records)
    pipelines: dict[str, Any] = {}
    base_phi_keys = _resolve_study_phi_keys(study_key, pipelines, sdk=sdk)
    sanitized: list[_T] = []
    for item in records:
        item_phi_keys = base_phi_keys
        if not study_key:
            raw_sk = (
                (item.get("studyKey") or item.get("study_key"))
                if isinstance(item, Mapping)
                else (getattr(item, "study_key", None) or getattr(item, "studyKey", None))
            )
            if isinstance(raw_sk, str):
                item_phi_keys = _resolve_study_phi_keys(raw_sk, pipelines, sdk=sdk)
        sanitized.append(_sanitize_value_recursive(item, custom_phi_keys=item_phi_keys))
    return sanitized


__all__ = [
    "format_cli_diagnostic_error",
    "is_cli_phi_key_sensitive",
    "redact_cli_secrets",
    "resolve_cli_phi_registry",
    "resolve_enrichment_pipeline",
    "resolve_study_configuration",
    "sanitize_cli_phi",
    "sanitize_cli_records",
]
