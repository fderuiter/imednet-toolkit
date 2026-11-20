"""CLI commands for managing study codings."""

from __future__ import annotations

import argparse
from typing import Any

from ...endpoints.codings import CodingsEndpoint
from ...models.codings import Coding
from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import display_list, fetching_status
from ..utils.args import (
    add_list_query_args,
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    collect_list_items_with_remainder,
    prepare_list_query,
    resolve_study_key,
)


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


def _coding_view_model(c: Any) -> dict[str, Any]:
    return {
        "coding_id": _get_val(c, "coding_id", "codingId", "id"),
        "dictionary_name": _get_val(c, "dictionary_name", "dictionaryName", "dictionary"),
        "dictionary_version": _get_val(c, "dictionary_version", "dictionaryVersion", "version"),
        "variable": _get_val(c, "variable"),
        "value": _get_val(c, "value"),
        "code": _get_val(c, "code"),
    }


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("codings", help="Manage medical codings within a study.")
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="List codings for a specific study.")
    add_study_key_args(list_parser)
    add_list_query_args(
        list_parser,
        filter_dest="coding_filter",
        filter_help="Filter criteria (e.g., 'dictionaryName=MedDRA' or 'dictionaryName==MedDRA'). Repeat for multiple filters.",
    )

    add_output_format(list_parser)
    add_simulation_arg(list_parser)

    @with_sdk
    def list_codings(
        sdk: ImednetSDK,
        study_key: str,
        coding_filter: list[str] | None = None,
        output_format: str = "table",
        sort: str | None = None,
        limit: int | None = None,
        fetch_all: bool = False,
        no_interactive: bool = False,
        _prepared_query: tuple[dict[str, Any], int | None] | None = None,
    ) -> None:
        req_kwargs, effective_limit = (
            _prepared_query
            if _prepared_query is not None
            else prepare_list_query(
                filter_args=coding_filter,
                sort=sort,
                limit=limit,
                fetch_all=fetch_all,
                output_format=output_format,
                no_interactive=no_interactive,
                model=Coding,
                allowed_attributes=CodingsEndpoint.FILTER_ATTRIBUTES,
                disallow_study_key=True,
            )
        )

        with fetching_status("codings", study_key):
            codings_list, truncated, load_remaining = collect_list_items_with_remainder(
                sdk.codings.list(study_key, **req_kwargs),
                effective_limit,
            )

        if output_format == "json":
            display_list(codings_list, "codings", output_format="json")
            return

        view_models = [_coding_view_model(c) for c in codings_list]

        def _load_more_codings() -> tuple[list[dict[str, Any]], list[Any]]:
            more = load_remaining() if load_remaining is not None else []
            return [_coding_view_model(c) for c in more], more

        display_list(
            view_models,
            "codings",
            "No codings found matching the criteria.",
            details=codings_list,
            truncated=truncated,
            interactive=False if no_interactive else None,
            on_load_all=_load_more_codings if load_remaining is not None else None,
        )

    def _dispatch_list_codings(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(list_parser, args, required=True)
        assert study_key is not None
        no_interactive = getattr(args, "no_interactive", False)
        prepared = prepare_list_query(
            filter_args=args.coding_filter,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
            output_format=args.output_format,
            no_interactive=no_interactive,
            model=Coding,
            allowed_attributes=CodingsEndpoint.FILTER_ATTRIBUTES,
            disallow_study_key=True,
        )
        list_codings(
            study_key=study_key,
            coding_filter=args.coding_filter,
            output_format=args.output_format,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
            no_interactive=no_interactive,
            _prepared_query=prepared,
        )

    list_parser.set_defaults(func=_dispatch_list_codings)
