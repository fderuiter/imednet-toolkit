"""Decorators for CLI command functions."""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar

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
from imednet.utils.url import validate_path_segment
from imednet.utils.validators import parse_bool

from ._read_guards import (
    _collapse_single_line,
    format_cli_diagnostic_error,
    redact_cli_secrets,
    resolve_cli_phi_registry,
)

if TYPE_CHECKING:
    from ..sdk import ImednetSDK

P = ParamSpec("P")
R = TypeVar("R")

_redact_secrets = redact_cli_secrets

__all__ = [
    "_redact_secrets",
    "cli_execution_guard",
    "format_cli_diagnostic_error",
    "redact_cli_secrets",
    "validate_cli_path_segment",
    "with_sdk",
]


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
            raw_schema_src = bound.arguments.get("schema_source") or bound.arguments.get(
                "data_dictionary_path"
            )
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
                    reg = resolve_cli_phi_registry(active_study_key)
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
                    reg = resolve_cli_phi_registry(active_study_key)
                    msg = redact_cli_secrets(_collapse_single_line(str(exc)), registry=reg)
                    print_error(f"Unexpected error: {msg}")
                    sys.exit(1)
        finally:
            close = getattr(sdk, "close", None)
            if callable(close):
                close()

    wrapper.__signature__ = wrapper_sig  # type: ignore[attr-defined]

    return wrapper
