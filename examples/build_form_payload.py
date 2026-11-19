#!/usr/bin/env python3
"""Hybrid Entry Point for iMedNet Form Builder.

Usage:
  cp .env.example .env
  # Or export directly:
  # export IMEDNET_API_KEY="your_api_key"
  # export IMEDNET_SECURITY_KEY="your_security_key"
  # export IMEDNET_BASE_URL="https://edc.prod.imednetapi.com"

  # Headless Mode (CLI)
  uv run --with "imednet[cli]" python examples/build_form_payload.py \
      --preset "Demo Form" --form-id 123 --revision 5
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from imednet.form_designer import PRESETS, FormBuilder, FormDesignerClient

# Load env vars
load_dotenv()


def run_headless(
    preset_name: str,
    base_url: str,
    phpsessid: str,
    csrf: str,
    form_id: int,
    comm_id: int,
    revision: int,
) -> None:
    """Run the form builder in headless mode."""
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("imednet.headless")

    if preset_name not in PRESETS:
        logger.error(f"Preset '{preset_name}' not found. Available: {list(PRESETS.keys())}")
        sys.exit(1)

    logger.info(f"Building layout using preset: {preset_name}")
    builder = FormBuilder()
    PRESETS[preset_name](builder)
    layout = builder.build()

    logger.info("Submitting payload...")
    client = FormDesignerClient(base_url, phpsessid)

    try:
        resp = client.save_form(
            csrf_key=csrf, form_id=form_id, community_id=comm_id, revision=revision, layout=layout
        )
        logger.info("Success! Server response:")
        print(resp)
    except Exception as e:
        logger.error(f"Failed to submit form: {e}")
        sys.exit(1)


def main() -> None:
    """Parse CLI arguments and submit a Form Builder layout payload."""
    parser = argparse.ArgumentParser(description="iMedNet Form Builder")

    # Optional arguments for headless mode
    parser.add_argument("--preset", help="Name of the form preset to build")
    parser.add_argument("--base-url", help="iMedNet Base URL")
    parser.add_argument("--phpsessid", help="Browser PHPSESSID")
    parser.add_argument("--csrf", help="CSRF Key")
    parser.add_argument("--form-id", type=int, help="Form ID")
    parser.add_argument("--comm-id", type=int, help="Community ID")
    parser.add_argument("--revision", type=int, help="Next Revision Number")

    args = parser.parse_args()

    # Check if enough args are present for headless mode
    headless_args = [
        args.preset,
        args.base_url,
        args.phpsessid,
        args.csrf,
        args.form_id,
        args.comm_id,
        args.revision,
    ]

    # If ANY headless arg is provided, try to run headless (and fail if others missing)
    if any(headless_args):
        # Allow fallback to env vars for base_url
        base_url = args.base_url or os.getenv("IMEDNET_BASE_URL")

        missing = []
        if not args.preset:
            missing.append("--preset")
        if not base_url:
            missing.append("--base-url")
        if not args.phpsessid:
            missing.append("--phpsessid")
        if not args.csrf:
            missing.append("--csrf")
        if not args.form_id:
            missing.append("--form-id")
        if not args.comm_id:
            missing.append("--comm-id")
        if not args.revision:
            missing.append("--revision")

        if missing:
            print(f"Error: Missing arguments for headless mode: {', '.join(missing)}")
            sys.exit(1)

        run_headless(
            args.preset,
            base_url,
            args.phpsessid,
            args.csrf,
            args.form_id,
            args.comm_id,
            args.revision,
        )
    else:
        print(
            "Provide Form Builder CLI arguments (for example --preset, --form-id, --comm-id, "
            "and --revision) to build and submit a form payload.\n"
            "For interactive Study, Site, Subject, and Job browsing, run: imednet tui"
        )
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
