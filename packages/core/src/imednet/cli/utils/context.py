"""CLI context and SDK initialization helpers."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import httpx

from imednet.config import load_config
from imednet.utils.validators import parse_bool

from .output import Status, console, print_error
from .terminal import SIMULATION_MODE_ENV

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK

_OFFLINE_DRY_RUN_CTX: ContextVar[bool] = ContextVar("imednet_offline_dry_run", default=False)


def is_offline_schema_dry_run(*, dry_run: bool, schema_source: str | None) -> bool:
    """Return True when ``--dry-run`` is paired with a local ``--schema`` path."""
    if not dry_run or not schema_source:
        return False
    normalized = schema_source.strip()
    return bool(normalized) and normalized.lower() != "api"


@contextmanager
def offline_dry_run_context(enabled: bool = False) -> Iterator[None]:
    """Mark the active CLI invocation as an offline schema dry-run requiring zero credentials."""
    if not enabled:
        yield
        return
    token = _OFFLINE_DRY_RUN_CTX.set(True)
    try:
        yield
    finally:
        _OFFLINE_DRY_RUN_CTX.reset(token)


def _forbid_offline_http(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(
        f"Unexpected HTTP request during offline --dry-run: {request.method} {request.url}"
    )


def _load_sdk_cls() -> type[ImednetSDK]:
    """Import and cache ImednetSDK in module globals on first access."""
    from imednet.sdk import ImednetSDK

    globals()["ImednetSDK"] = ImednetSDK
    return ImednetSDK


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily load ImednetSDK on demand while supporting module-level mock patching."""
        if name == "ImednetSDK":
            return _load_sdk_cls()
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def bootstrap_cli_dotenv() -> set[str]:
    """Load working-directory ``.env`` defaults without overriding shell environment variables.

    Returns the set of environment variable names newly added to ``os.environ``.
    """
    before_keys = set(os.environ)
    try:
        import dotenv

        load_fn = getattr(dotenv, "load_dotenv", None)
        if not callable(load_fn):
            return set()
        find_fn = getattr(dotenv, "find_dotenv", None)
        if callable(find_fn):
            dotenv_path = find_fn(usecwd=True)
            if not dotenv_path:
                return set()
            try:
                load_fn(dotenv_path, override=False)
            except TypeError:
                load_fn()
        else:
            try:
                load_fn(override=False)
            except TypeError:
                load_fn()
    except Exception:
        return set(os.environ) - before_keys
    return set(os.environ) - before_keys


@contextmanager
def simulation_mode_context(enabled: bool = False) -> Iterator[None]:
    """Temporarily enable ``IMEDNET_SIMULATION_MODE`` and restore prior environment on exit."""
    if not enabled:
        yield
        return
    previous = os.environ.get(SIMULATION_MODE_ENV)
    os.environ[SIMULATION_MODE_ENV] = "1"
    try:
        yield
    finally:
        if previous is not None:
            os.environ[SIMULATION_MODE_ENV] = previous
        else:
            os.environ.pop(SIMULATION_MODE_ENV, None)


def _format_config_error(exc: ValueError) -> str:
    from ..decorators import redact_cli_secrets

    detail = redact_cli_secrets(str(exc))
    hint = "Tip: Pass --simulation or set IMEDNET_SIMULATION_MODE=true for offline simulation."
    if "Security key is required" in detail:
        return f"Error: IMEDNET_SECURITY_KEY environment variable must be set ({detail}). {hint}"
    if "API key is required" in detail:
        return f"Error: IMEDNET_API_KEY environment variable must be set ({detail}). {hint}"
    if "both API key and security key are required" in detail:
        return (
            "Error: IMEDNET_API_KEY and IMEDNET_SECURITY_KEY environment variables must be set. "
            f"({detail}) {hint}"
        )
    return f"Error: {detail}. {hint}"


def get_sdk() -> ImednetSDK:
    """Initialize and return the SDK instance using :func:`load_config`."""
    sdk_cls: type[ImednetSDK]
    if _OFFLINE_DRY_RUN_CTX.get() and not parse_bool(
        os.getenv(SIMULATION_MODE_ENV, "false"), strict=False
    ):
        try:
            sdk_cls = globals()["ImednetSDK"] if "ImednetSDK" in globals() else _load_sdk_cls()
            return sdk_cls(
                api_key="offline-dry-run",  # pragma: allowlist secret
                security_key="offline-dry-run",  # pragma: allowlist secret
                transport=httpx.MockTransport(_forbid_offline_http),
            )
        except Exception as exc:  # pragma: no cover - defensive
            from ..decorators import redact_cli_secrets

            print_error(f"Error initializing SDK: {redact_cli_secrets(str(exc))}")
            sys.exit(1)

    if not (
        os.getenv("IMEDNET_OIDC_TOKEN", "").strip()
        or parse_bool(os.getenv(SIMULATION_MODE_ENV, "false"), strict=False)
        or (
            os.getenv("IMEDNET_API_KEY", "").strip()
            and os.getenv("IMEDNET_SECURITY_KEY", "").strip()
        )
    ):
        bootstrap_cli_dotenv()

    try:
        config = load_config()
    except ValueError as exc:
        print_error(_format_config_error(exc))
        sys.exit(1)

    try:
        sdk_cls = globals()["ImednetSDK"] if "ImednetSDK" in globals() else _load_sdk_cls()
        return sdk_cls(config=config)
    except Exception as exc:  # pragma: no cover - defensive
        from ..decorators import redact_cli_secrets

        print_error(f"Error initializing SDK: {redact_cli_secrets(str(exc))}")
        sys.exit(1)


@contextmanager
def fetching_status(name: str, study_key: str | None = None) -> Iterator[Status]:
    """Context manager to show a spinner while fetching data."""
    if study_key:  # noqa: SIM108
        msg = f"Fetching {name} for study '{study_key}'..."
    else:
        msg = f"Fetching {name}..."
    with console.status(msg) as status:
        yield status
