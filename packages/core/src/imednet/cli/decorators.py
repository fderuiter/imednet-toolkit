"""Decorators for CLI command functions."""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar

from imednet.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    ClientError,
    ForbiddenError,
    NotFoundError,
    PathTraversalValidationError,
    RateLimitError,
    UnauthorizedError,
    ValidationError,
)
from imednet.utils.security import SensitivityRegistry, sanitize_diagnostic_text
from imednet.utils.url import validate_path_segment

if TYPE_CHECKING:
    from ..sdk import ImednetSDK

P = ParamSpec("P")
R = TypeVar("R")


def redact_cli_secrets(
    msg: str,
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

        mod_get_sdk = func.__globals__.get("get_sdk")
        with _context_mod.offline_dry_run_context(is_offline_dry_run):
            if (
                callable(mod_get_sdk)
                and getattr(mod_get_sdk, "__module__", None) != "imednet.cli.utils.context"
            ):
                sdk = mod_get_sdk()
            else:
                sdk = _context_mod.get_sdk()
        try:
            return func(sdk, *args, **kwargs)
        except SystemExit:
            raise
        except PathTraversalValidationError as exc:
            msg = _redact_secrets(str(exc))
            print_error(f"Error: {msg}")
            sys.exit(1)
        except (AuthenticationError, UnauthorizedError) as exc:
            msg = _redact_secrets(str(exc))
            print_error(f"Authentication Failed: {msg}")
            sys.exit(1)
        except (AuthorizationError, ForbiddenError) as exc:
            msg = _redact_secrets(str(exc))
            print_error(f"Permission Denied: {msg}")
            sys.exit(1)
        except RateLimitError as exc:
            msg = _redact_secrets(str(exc))
            print_error(f"Rate Limit Exceeded: {msg}")
            sys.exit(1)
        except NotFoundError as exc:
            msg = _redact_secrets(str(exc))
            print_error(f"Not Found: {msg}")
            sys.exit(1)
        except ClientError as exc:
            msg = _redact_secrets(str(exc))
            if isinstance(exc, ApiError) and exc.status_code is not None:
                print_error(f"API Error: {msg}")
            elif isinstance(exc, ValidationError):
                print_error(f"Validation Error: {msg}")
            else:
                print_error(f"Error: {msg}")
            sys.exit(1)
        except ApiError as exc:
            msg = _redact_secrets(str(exc))
            print_error(f"API Error: {msg}")
            sys.exit(1)
        except KeyboardInterrupt:
            clear_line()
            print_error("\nAborted by user.")
            sys.exit(130)
        except Exception as exc:  # pragma: no cover - defensive
            msg = _redact_secrets(str(exc))
            print_error(f"Unexpected error: {msg}")
            sys.exit(1)
        finally:
            close = getattr(sdk, "close", None)
            if callable(close):
                close()

    wrapper.__signature__ = wrapper_sig  # type: ignore[attr-defined]

    return wrapper
