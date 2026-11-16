"""Decorators for CLI command functions."""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Callable
from functools import wraps
from typing import Concatenate, ParamSpec, TypeVar

from imednet.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
)

from ..sdk import ImednetSDK

P = ParamSpec("P")
R = TypeVar("R")


def _redact_secrets(msg: str) -> str:
    """Redact secrets from error messages."""
    for env_var in ["IMEDNET_API_KEY", "IMEDNET_SECURITY_KEY", "IMEDNET_OIDC_TOKEN"]:
        val = os.environ.get(env_var)
        if val and val in msg:
            msg = msg.replace(val, "********")
    return msg


def with_sdk(func: Callable[Concatenate[ImednetSDK, P], R]) -> Callable[P, R]:
    """Initialize the SDK and pass it to the wrapped command function."""
    sig = inspect.signature(func)
    wrapper_params = list(sig.parameters.values())[1:]

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        """Initialize the SDK, execute the command, and handle common API errors."""
        from .utils.context import get_sdk
        from .utils.output import print_error

        sdk = get_sdk()
        try:
            return func(sdk, *args, **kwargs)
        except SystemExit:
            raise
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
        except ApiError as exc:
            msg = _redact_secrets(str(exc))
            print_error(f"API Error: {msg}")
            sys.exit(1)
        except KeyboardInterrupt:
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

    wrapper.__signature__ = sig.replace(parameters=wrapper_params)  # type: ignore[attr-defined]

    return wrapper
