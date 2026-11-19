"""CLI context and SDK initialization helpers."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from imednet.config import load_config
from imednet.sdk import ImednetSDK
from imednet.utils.validators import parse_bool

from .output import Status, console, print_error


def get_sdk() -> ImednetSDK:
    """Initialize and return the SDK instance using :func:`load_config`."""
    if not (
        os.getenv("IMEDNET_OIDC_TOKEN", "").strip()
        or parse_bool(os.getenv("IMEDNET_SIMULATION_MODE", "false"))
        or (
            os.getenv("IMEDNET_API_KEY", "").strip()
            and os.getenv("IMEDNET_SECURITY_KEY", "").strip()
        )
    ):
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except Exception:  # noqa: S110
            pass

    try:
        config = load_config()
    except ValueError:
        print_error(
            "Error: IMEDNET_API_KEY and IMEDNET_SECURITY_KEY environment variables must be set."
        )
        sys.exit(1)

    try:
        return ImednetSDK(config=config)
    except Exception as exc:  # pragma: no cover - defensive
        print_error(f"Error initializing SDK: {exc}")
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
