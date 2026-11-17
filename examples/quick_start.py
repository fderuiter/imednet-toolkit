"""Synchronous quickstart example."""

# [start_quick_start]
from __future__ import annotations

import sys

from dotenv import load_dotenv

from imednet import ImednetSDK, load_config
from imednet.utils import configure_json_logging

"""Quick start example using environment variables for authentication.

Set ``IMEDNET_API_KEY`` and ``IMEDNET_SECURITY_KEY`` before running this
script. Optionally set ``IMEDNET_BASE_URL`` for non-default instances.

Example:

    cp .env.example .env
    # Or export directly:
    export IMEDNET_API_KEY="your_api_key"
    export IMEDNET_SECURITY_KEY="your_security_key"
    # Optional: Custom base URL for the API endpoint
    # export IMEDNET_BASE_URL="https://edc.prod.imednetapi.com"
    uv run --with "imednet[cli]" python examples/quick_start.py
"""


def main() -> None:
    """Run a minimal SDK example using environment variables."""
    configure_json_logging()
    # Load environment variables from .env file if it exists
    # Note: Ensure you've run `cp .env.example .env` or exported keys to your shell.
    load_dotenv()

    try:
        cfg = load_config()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    with ImednetSDK(
        api_key=cfg.api_key,
        security_key=cfg.security_key,
        base_url=cfg.base_url,
    ) as sdk:
        print(sdk.studies.list())


if __name__ == "__main__":
    main()
# [end_quick_start]
