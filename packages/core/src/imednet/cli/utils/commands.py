"""Helpers for registering common CLI commands."""

from __future__ import annotations

import argparse
from typing import Any

from imednet.cli.decorators import validate_cli_path_segment, with_sdk
from imednet.endpoints.registry import ENDPOINT_REGISTRY
from imednet.sdk import ImednetSDK

from .args import (
    add_list_query_args,
    add_no_interactive,
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    collect_list_items_with_remainder,
    prepare_list_query,
    resolve_study_key,
)
from .context import fetching_status
from .output import display_list


def register_list_command(
    parser: argparse.ArgumentParser,
    attr: str,
    name: str,
    *,
    requires_study_key: bool = True,
    empty_msg: str | None = None,
    summary_fields: list[str] | None = None,
    model: type[Any] | None = None,
) -> None:
    """Attach a standard ``list`` command to parser."""
    endpoint_cls = ENDPOINT_REGISTRY.get(attr)
    resolved_model = model
    if resolved_model is None and endpoint_cls is not None:
        resolved_model = getattr(endpoint_cls, "MODEL", None)
    allowed_attributes: frozenset[str] | None = None
    if endpoint_cls is not None:
        raw_attrs = getattr(endpoint_cls, "FILTER_ATTRIBUTES", None)
        if isinstance(raw_attrs, frozenset):
            allowed_attributes = raw_attrs

    subparsers = getattr(parser, '_subparsers', None)
    if subparsers is None:
        subparsers = [
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        ]
        if subparsers:  # noqa: SIM108
            subparsers = subparsers[0]
        else:
            subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help=f"List all items of type {name}.")

    add_output_format(list_parser)
    add_no_interactive(list_parser)
    add_simulation_arg(list_parser)

    if requires_study_key:
        add_study_key_args(list_parser)
        add_list_query_args(list_parser)

        @with_sdk
        def list_cmd(
            sdk: ImednetSDK,
            study_key: str,
            output_format: str = "table",
            filter_args: list[str] | None = None,
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
                    filter_args=filter_args,
                    sort=sort,
                    limit=limit,
                    fetch_all=fetch_all,
                    output_format=output_format,
                    no_interactive=no_interactive,
                    model=resolved_model,
                    allowed_attributes=allowed_attributes,
                    disallow_study_key=True,
                )
            )
            with fetching_status(name, study_key):
                items, truncated, load_remaining = collect_list_items_with_remainder(
                    getattr(sdk, attr).list(study_key, **req_kwargs),
                    effective_limit,
                )
            display_list(
                items,
                name,
                empty_msg,
                fields=summary_fields,
                output_format=output_format,
                details=items,
                truncated=truncated,
                interactive=False if no_interactive else None,
                on_load_all=load_remaining,
            )

        def _dispatch_study_list(args: argparse.Namespace) -> None:
            study_key = resolve_study_key(list_parser, args, required=True)
            assert study_key is not None
            validate_cli_path_segment(study_key, name="study_key")
            no_interactive = getattr(args, "no_interactive", False)
            prepared = prepare_list_query(
                filter_args=args.filter_args,
                sort=args.sort,
                limit=args.limit,
                fetch_all=args.fetch_all,
                output_format=args.output_format,
                no_interactive=no_interactive,
                model=resolved_model,
                allowed_attributes=allowed_attributes,
                disallow_study_key=True,
            )
            list_cmd(
                study_key=study_key,
                output_format=args.output_format,
                filter_args=args.filter_args,
                sort=args.sort,
                limit=args.limit,
                fetch_all=args.fetch_all,
                no_interactive=no_interactive,
                _prepared_query=prepared,
            )

        list_parser.set_defaults(func=_dispatch_study_list)

    else:
        add_list_query_args(list_parser)

        @with_sdk
        def list_cmd_no_study(
            sdk: ImednetSDK,
            output_format: str = "table",
            filter_args: list[str] | None = None,
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
                    filter_args=filter_args,
                    sort=sort,
                    limit=limit,
                    fetch_all=fetch_all,
                    output_format=output_format,
                    no_interactive=no_interactive,
                    model=resolved_model,
                    allowed_attributes=allowed_attributes,
                )
            )
            with fetching_status(name):
                items, truncated, load_remaining = collect_list_items_with_remainder(
                    getattr(sdk, attr).list(**req_kwargs),
                    effective_limit,
                )
            display_list(
                items,
                name,
                empty_msg,
                fields=summary_fields,
                output_format=output_format,
                details=items,
                truncated=truncated,
                interactive=False if no_interactive else None,
                on_load_all=load_remaining,
            )

        def _dispatch_no_study_list(args: argparse.Namespace) -> None:
            no_interactive = getattr(args, "no_interactive", False)
            prepared = prepare_list_query(
                filter_args=args.filter_args,
                sort=args.sort,
                limit=args.limit,
                fetch_all=args.fetch_all,
                output_format=args.output_format,
                no_interactive=no_interactive,
                model=resolved_model,
                allowed_attributes=allowed_attributes,
            )
            list_cmd_no_study(
                output_format=args.output_format,
                filter_args=args.filter_args,
                sort=args.sort,
                limit=args.limit,
                fetch_all=args.fetch_all,
                no_interactive=no_interactive,
                _prepared_query=prepared,
            )

        list_parser.set_defaults(func=_dispatch_no_study_list)
