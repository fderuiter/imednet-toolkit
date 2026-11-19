"""CLI context and SDK initialization helpers."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from imednet.config import load_config
from imednet.sdk import ImednetSDK

from .output import Status, console, print_error


def get_sdk() -> ImednetSDK:
    """Initialize and return the SDK instance using :func:`load_config`."""
    if not (
        os.getenv("IMEDNET_OIDC_TOKEN", "").strip()
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
        if config.simulation_mode:
            return ImednetSDK(
                api_key=config.api_key,
                security_key=config.security_key,
                base_url=config.base_url,
                simulation_mode=True,
            )
        return ImednetSDK(
            api_key=config.api_key,
            security_key=config.security_key,
            base_url=config.base_url,
        )
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
