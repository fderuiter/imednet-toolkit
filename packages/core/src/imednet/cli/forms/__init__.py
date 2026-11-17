"""CLI commands for managing forms."""

from __future__ import annotations

import argparse

from ..utils import register_list_command


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("forms", help="Manage forms.")
    register_list_command(
        parser,
        "forms",
        "forms",
        requires_study_key=True,
        summary_fields=["form_id", "form_key", "form_name", "form_type", "disabled"],
    )
