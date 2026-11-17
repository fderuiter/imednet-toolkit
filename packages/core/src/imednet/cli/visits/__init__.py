"""CLI commands for managing visits."""

from __future__ import annotations

import argparse

from ..utils import register_list_command


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("visits", help="Manage visits.")
    register_list_command(
        parser,
        "visits",
        "visits",
        requires_study_key=True,
        summary_fields=["visit_id", "interval_name", "subject_key", "visit_date"],
    )
