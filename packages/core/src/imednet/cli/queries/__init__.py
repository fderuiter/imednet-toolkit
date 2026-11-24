"""CLI commands for managing queries."""

from __future__ import annotations

import argparse

from ..utils import register_list_command


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("queries", help="Manage queries.")
    register_list_command(
        parser,
        "queries",
        "queries",
        requires_study_key=True,
        summary_fields=[
            "annotation_id",
            "status",
            "subject_key",
            "variable",
            "annotation_type",
            "description",
            "date_created",
        ],
    )
