"""CLI commands for managing study codings."""

from __future__ import annotations

import argparse
from typing import Any

from ..utils import get_model_value, register_list_command


def _coding_view_model(c: Any) -> dict[str, Any]:
    return {
        "coding_id": get_model_value(c, "coding_id", "codingId", "id"),
        "dictionary_name": get_model_value(c, "dictionary_name", "dictionaryName", "dictionary"),
        "dictionary_version": get_model_value(
            c, "dictionary_version", "dictionaryVersion", "version"
        ),
        "variable": get_model_value(c, "variable"),
        "value": get_model_value(c, "value"),
        "code": get_model_value(c, "code"),
    }


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("codings", help="Manage medical codings within a study.")
    register_list_command(
        parser,
        "codings",
        "codings",
        requires_study_key=True,
        list_help="List codings for a specific study.",
        empty_msg="No codings found matching the criteria.",
        row_mapper=_coding_view_model,
        filter_dest="coding_filter",
        filter_help=(
            "Filter criteria (e.g., 'dictionaryName=MedDRA' or 'dictionaryName==MedDRA'). "
            "Repeat for multiple filters."
        ),
    )
