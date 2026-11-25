"""Decorators for CLI command functions."""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar
from urllib.parse import urlsplit

from imednet.core.context import _safe_context_attr, sdk_governance_context
from imednet.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    CircuitBreakerError,
    ForbiddenError,
    NotFoundError,
    PathTraversalValidationError,
    RateLimitError,
    RequestError,
    UnauthorizedError,
)
from imednet.utils.security import (
    SensitivityRegistry,
    global_sensitivity_registry,
    sanitize_diagnostic_text,
)
from imednet.utils.url import redact_url_query, validate_path_segment
from imednet.utils.validators import parse_bool

if TYPE_CHECKING:
    from ..sdk import ImednetSDK

P = ParamSpec("P")
R = TypeVar("R")


def redact_cli_secrets(
    msg: str,
    *,
    registry: SensitivityRegistry | None = None,
) -> str:
    """Redact environment secrets, credentials, and inline clinical PHI from CLI messages."""
    redacted = msg
    for env_var in ("IMEDNET_API_KEY", "IMEDNET_SECURITY_KEY", "IMEDNET_OIDC_TOKEN"):
        val = os.environ.get(env_var)
        if val:
            if val in redacted:
                redacted = redacted.replace(val, "********")
            stripped = val.strip()
            if stripped and stripped in redacted:
                redacted = redacted.replace(stripped, "********")
    return sanitize_diagnostic_text(redacted, registry=registry)


_redact_secrets = redact_cli_secrets


def _collapse_single_line(text: str) -> str:
    """Normalize internal newlines and runs of whitespace into a single clean line."""
    return " ".join(text.split())


def resolve_study_configuration(study_key: str | None) -> Any:
    """Lazily resolve ``StudyConfiguration`` without importing ``integrations`` at CLI startup."""
    from imednet.integrations.enrichment import resolve_study_configuration as _resolve

    return _resolve(study_key)


def _resolve_cli_phi_registry(
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
            val = "***MASKED***" if registry.is_sensitive(attr) else raw_field["value"]
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
    active_registry = _resolve_cli_phi_registry(study_key, getattr(exc, "url", None))
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


@contextmanager
def cli_execution_guard() -> Iterator[None]:
    """Guard top-level CLI execution so user interrupts exit cleanly with code 130."""
    from .utils.output import clear_line, print_error

    try:
        yield
    except SystemExit:
        raise
    except KeyboardInterrupt:
        clear_line()
        print_error("\nAborted by user.")
        sys.exit(130)


def validate_cli_path_segment(value: Any, name: str = "segment") -> None:
    """Validate a CLI dynamic URL segment, printing a clean stderr error and exiting 1 on failure."""
    from .utils.output import print_error

    try:
        validate_path_segment(value, name=name)
    except PathTraversalValidationError as exc:
        print_error(f"Error: {_redact_secrets(str(exc))}")
        sys.exit(1)


def with_sdk(func: Callable[Concatenate[ImednetSDK, P], R]) -> Callable[P, R]:
    """Initialize the SDK and pass it to the wrapped command function."""
    sig = inspect.signature(func)
    wrapper_params = list(sig.parameters.values())[1:]
    wrapper_sig = sig.replace(parameters=wrapper_params)

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        """Initialize the SDK, execute the command, and handle common API errors."""
        from .utils import context as _context_mod
        from .utils.output import clear_line, print_error

        bound = wrapper_sig.bind_partial(*args, **kwargs)
        is_interactive = bool(bound.arguments.get("interactive", False))
        for seg_param in ("study_key", "batch_id", "item_id"):
            if seg_param in bound.arguments:
                seg_val = bound.arguments[seg_param]
                if seg_val is not None and not (
                    seg_param == "study_key" and is_interactive and seg_val == ""
                ):
                    validate_cli_path_segment(seg_val, name=seg_param)

        is_offline_dry_run = False
        for arg_val in bound.arguments.values():
            if hasattr(arg_val, "dry_run") and hasattr(arg_val, "schema_source"):
                is_offline_dry_run = _context_mod.is_offline_schema_dry_run(
                    dry_run=bool(getattr(arg_val, "dry_run", False)),
                    schema_source=getattr(arg_val, "schema_source", None),
                )
                break
        if not is_offline_dry_run and "dry_run" in bound.arguments:
            raw_schema_src = bound.arguments.get("schema_source")
            is_offline_dry_run = _context_mod.is_offline_schema_dry_run(
                dry_run=bool(bound.arguments.get("dry_run", False)),
                schema_source=raw_schema_src if isinstance(raw_schema_src, str) else None,
            )

        raw_study_arg = bound.arguments.get("study_key")
        if raw_study_arg is None:
            for arg_val in bound.arguments.values():
                cand_sk = _safe_context_attr(arg_val, "study_key") or _safe_context_attr(
                    arg_val, "study"
                )
                if isinstance(cand_sk, str) and cand_sk.strip():
                    raw_study_arg = cand_sk
                    break
        active_study_key = (
            raw_study_arg.strip()
            if isinstance(raw_study_arg, str) and raw_study_arg.strip()
            else None
        )

        mod_get_sdk = func.__globals__.get("get_sdk")
        with _context_mod.offline_dry_run_context(is_offline_dry_run):
            if (
                callable(mod_get_sdk)
                and getattr(mod_get_sdk, "__module__", None) != "imednet.cli.utils.context"
            ):
                sdk = mod_get_sdk()
            else:
                sdk = _context_mod.get_sdk()
        cfg = _safe_context_attr(sdk, "config")
        is_simulation = (_safe_context_attr(cfg, "simulation_mode") is True) or parse_bool(
            os.environ.get("IMEDNET_SIMULATION_MODE"), strict=False
        )
        try:
            with sdk_governance_context(sdk):
                try:
                    return func(sdk, *args, **kwargs)
                except SystemExit:
                    raise
                except PathTraversalValidationError as exc:
                    reg = _resolve_cli_phi_registry(active_study_key)
                    msg = redact_cli_secrets(_collapse_single_line(str(exc)), registry=reg)
                    print_error(f"Error: {msg}")
                    sys.exit(1)
                except (AuthenticationError, UnauthorizedError) as exc:
                    msg = format_cli_diagnostic_error(
                        exc, simulation=is_simulation, study_key=active_study_key
                    )
                    print_error(f"Authentication Failed: {msg}")
                    sys.exit(1)
                except (AuthorizationError, ForbiddenError) as exc:
                    msg = format_cli_diagnostic_error(
                        exc, simulation=is_simulation, study_key=active_study_key
                    )
                    print_error(f"Permission Denied: {msg}")
                    sys.exit(1)
                except RateLimitError as exc:
                    msg = format_cli_diagnostic_error(
                        exc, simulation=is_simulation, study_key=active_study_key
                    )
                    print_error(f"Rate Limit Exceeded: {msg}")
                    sys.exit(1)
                except NotFoundError as exc:
                    msg = format_cli_diagnostic_error(
                        exc, simulation=is_simulation, study_key=active_study_key
                    )
                    print_error(f"Not Found: {msg}")
                    sys.exit(1)
                except ApiError as exc:
                    msg = format_cli_diagnostic_error(
                        exc, simulation=is_simulation, study_key=active_study_key
                    )
                    print_error(f"API Error: {msg}")
                    sys.exit(1)
                except CircuitBreakerError as exc:
                    msg = format_cli_diagnostic_error(
                        exc, simulation=is_simulation, study_key=active_study_key
                    )
                    print_error(f"Circuit Breaker Open: {msg}")
                    sys.exit(1)
                except RequestError as exc:
                    msg = format_cli_diagnostic_error(
                        exc, simulation=is_simulation, study_key=active_study_key
                    )
                    print_error(f"Network Error: {msg}")
                    sys.exit(1)
                except KeyboardInterrupt:
                    clear_line()
                    print_error("\nAborted by user.")
                    sys.exit(130)
                except Exception as exc:  # pragma: no cover - defensive
                    reg = _resolve_cli_phi_registry(active_study_key)
                    msg = redact_cli_secrets(_collapse_single_line(str(exc)), registry=reg)
                    print_error(f"Unexpected error: {msg}")
                    sys.exit(1)
        finally:
            close = getattr(sdk, "close", None)
            if callable(close):
                close()

    wrapper.__signature__ = wrapper_sig  # type: ignore[attr-defined]

    return wrapper
