"""CLI entry point for the iMednet TUI plugin."""

from __future__ import annotations

import argparse
import os
from typing import Any

from imednet.sdk import ImednetSDK
from imednet.spi.cli import with_sdk


def setup_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Register the 'tui' subparser."""
    tui_parser = subparsers.add_parser(
        "tui",
        help="Launch interactive terminal interface.",
        description="Interactive terminal interface for browsing clinical studies, sites, subjects, and jobs.",
    )
    tui_parser.add_argument(
        "--high-contrast",
        action="store_true",
        help="Enable high-contrast mode for accessibility.",
    )

    @with_sdk
    def run_tui_command(sdk: ImednetSDK, args: argparse.Namespace | None = None) -> None:
        if args is not None and getattr(args, "high_contrast", False):
            os.environ["IMEDNET_HIGH_CONTRAST"] = "1"
        from .app import run_tui

        run_tui(sdk)

    tui_parser.set_defaults(func=run_tui_command)
