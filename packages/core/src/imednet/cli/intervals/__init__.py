"""CLI commands for managing intervals."""

from __future__ import annotations

import argparse

from ..utils import register_list_command


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("intervals", help="Manage intervals.")
    register_list_command(
        parser,
        "intervals",
        "intervals",
        requires_study_key=True,
        summary_fields=["interval_id", "interval_name", "interval_sequence"],
        include_filter=True,
        preflight_query=True,
    )
