"""CLI commands for managing study codings."""

from __future__ import annotations

import argparse
from typing import Any

from ...models.codings import Coding
from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, display_list, fetching_status, parse_filter_args
from ..utils.args import add_output_format


def _get_val(obj: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(obj, dict):
            if k in obj and obj[k] is not None:
                return obj[k]
        else:
            val = getattr(obj, k, None)
            if val is not None:
                return val
    return default


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("codings", help="Manage medical codings within a study.")
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="List codings for a specific study.")
    list_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    list_parser.add_argument(
        "-f",
        "--filter",
        action="append",
        dest="coding_filter",
        help="Filter criteria (e.g., 'dictionaryName=MedDRA'). Repeat for multiple filters.",
    )

    add_output_format(list_parser)

    @with_sdk
    def list_codings(
        sdk: ImednetSDK,
        study_key: str,
        coding_filter: list[str] | None = None,
        output_format: str = "table",
    ) -> None:
        parsed_filter = parse_filter_args(coding_filter, model=Coding)

        with fetching_status("codings", study_key):
            codings_list = list(sdk.codings.list(study_key, **(parsed_filter or {})))

        if output_format == "json":
            display_list(codings_list, "codings", output_format="json")
            return

        view_models = []
        for c in codings_list:
            view_models.append(
                {
                    "coding_id": _get_val(c, "coding_id", "codingId", "id"),
                    "dictionary_name": _get_val(
                        c, "dictionary_name", "dictionaryName", "dictionary"
                    ),
                    "dictionary_version": _get_val(
                        c, "dictionary_version", "dictionaryVersion", "version"
                    ),
                    "variable": _get_val(c, "variable"),
                    "value": _get_val(c, "value"),
                    "code": _get_val(c, "code"),
                }
            )

        display_list(view_models, "codings", "No codings found matching the criteria.")

    list_parser.set_defaults(
        func=lambda args: list_codings(
            study_key=args.study_key,
            coding_filter=args.coding_filter,
            output_format=args.output_format,
        )
    )
