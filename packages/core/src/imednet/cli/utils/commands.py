"""Helpers for registering common CLI commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
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
    resolve_output_format,
    resolve_study_key,
)
from .context import fetching_status, simulation_mode_context
from .output import display_list


def get_model_value(obj: Any, *keys: str, default: Any = None) -> Any:
    """Return the first non-``None`` dictionary entry or object attribute matching ``keys``."""
    for key in keys:
        if isinstance(obj, dict):
            if key in obj and obj[key] is not None:
                return obj[key]
        else:
            val = getattr(obj, key, None)
            if val is not None:
                return val
    return default


def _render_list_results(
    items: list[Any],
    *,
    name: str,
    empty_msg: str | None,
    summary_fields: list[str] | None,
    row_mapper: Callable[[Any], dict[str, Any]] | None,
    resolved_format: str,
    truncated: bool,
    no_interactive: bool,
    load_remaining: Callable[[], list[Any]] | None,
) -> None:
    """Render fetched SDK items via ``display_list`` with optional table ``row_mapper``."""
    if resolved_format == "json":
        display_list(items, name, output_format="json")
        return

    if row_mapper is not None:
        mapped_items = [row_mapper(item) for item in items]

        def _load_more_mapped() -> tuple[list[dict[str, Any]], list[Any]]:
            more = load_remaining() if load_remaining is not None else []
            return [row_mapper(item) for item in more], more

        display_list(
            mapped_items,
            name,
            empty_msg,
            fields=summary_fields,
            output_format=resolved_format,
            details=items,
            truncated=truncated,
            interactive=False if no_interactive else None,
            on_load_all=_load_more_mapped if load_remaining is not None else None,
        )
        return

    display_list(
        items,
        name,
        empty_msg,
        fields=summary_fields,
        output_format=resolved_format,
        details=items,
        truncated=truncated,
        interactive=False if no_interactive else None,
        on_load_all=load_remaining,
    )


def register_list_command(
    parser: argparse.ArgumentParser,
    attr: str,
    name: str,
    *,
    requires_study_key: bool = True,
    list_help: str | None = None,
    empty_msg: str | None = None,
    summary_fields: list[str] | None = None,
    row_mapper: Callable[[Any], dict[str, Any]] | None = None,
    model: type[Any] | None = None,
    include_filter: bool = True,
    filter_dest: str = "filter_args",
    filter_help: str | None = None,
    extra_args_fn: Callable[[argparse.ArgumentParser], None] | None = None,
    extra_kwargs_fn: Callable[[argparse.Namespace], Mapping[str, Any]] | None = None,
    preflight_query: bool = True,
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

    subparsers: argparse._SubParsersAction[Any] | None = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers = action
            break
    if subparsers is None:
        subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser(
        "list",
        help=list_help or f"List all items of type {name}.",
    )

    add_output_format(list_parser)
    add_no_interactive(list_parser)
    add_simulation_arg(list_parser)

    def _prepare_query(
        *,
        filter_values: list[str] | None,
        sort: str | None,
        limit: int | None,
        fetch_all: bool,
        resolved_format: str,
        no_interactive: bool,
        disallow_study_key: bool,
    ) -> tuple[dict[str, Any], int | None]:
        return prepare_list_query(
            filter_args=filter_values,
            sort=sort,
            limit=limit,
            fetch_all=fetch_all,
            output_format=resolved_format,
            no_interactive=no_interactive,
            model=resolved_model,
            allowed_attributes=allowed_attributes,
            disallow_study_key=disallow_study_key,
        )

    if requires_study_key:
        add_study_key_args(list_parser)
        if extra_args_fn is not None:
            extra_args_fn(list_parser)
        add_list_query_args(
            list_parser,
            include_filter=include_filter,
            filter_dest=filter_dest,
            filter_help=filter_help,
        )

        @with_sdk
        def list_cmd(
            sdk: ImednetSDK,
            study_key: str,
            output_format: str | None = None,
            filter_args: list[str] | None = None,
            sort: str | None = None,
            limit: int | None = None,
            fetch_all: bool = False,
            no_interactive: bool = False,
            _prepared_query: tuple[dict[str, Any], int | None] | None = None,
            _extra_kwargs: Mapping[str, Any] | None = None,
        ) -> None:
            resolved_format = resolve_output_format(output_format)
            req_kwargs, effective_limit = (
                _prepared_query
                if _prepared_query is not None
                else _prepare_query(
                    filter_values=filter_args,
                    sort=sort,
                    limit=limit,
                    fetch_all=fetch_all,
                    resolved_format=resolved_format,
                    no_interactive=no_interactive,
                    disallow_study_key=True,
                )
            )
            call_kwargs = {**_extra_kwargs, **req_kwargs} if _extra_kwargs else req_kwargs
            with fetching_status(name, study_key):
                items, truncated, load_remaining = collect_list_items_with_remainder(
                    getattr(sdk, attr).list(study_key, **call_kwargs),
                    effective_limit,
                )
            _render_list_results(
                items,
                name=name,
                empty_msg=empty_msg,
                summary_fields=summary_fields,
                row_mapper=row_mapper,
                resolved_format=resolved_format,
                truncated=truncated,
                no_interactive=no_interactive,
                load_remaining=load_remaining,
            )

        def _dispatch_study_list(args: argparse.Namespace) -> None:
            study_key = resolve_study_key(list_parser, args, required=True)
            assert study_key is not None
            validate_cli_path_segment(study_key, name="study_key")
            no_interactive = getattr(args, "no_interactive", False)
            resolved_format = resolve_output_format(args.output_format)
            filter_values = getattr(args, filter_dest, None) if include_filter else None
            extra_kwargs = dict(extra_kwargs_fn(args)) if extra_kwargs_fn is not None else None
            prepared = (
                _prepare_query(
                    filter_values=filter_values,
                    sort=args.sort,
                    limit=args.limit,
                    fetch_all=args.fetch_all,
                    resolved_format=resolved_format,
                    no_interactive=no_interactive,
                    disallow_study_key=True,
                )
                if preflight_query
                else None
            )
            with simulation_mode_context(bool(getattr(args, "simulation", False))):
                list_cmd(
                    study_key=study_key,
                    output_format=resolved_format,
                    filter_args=filter_values,
                    sort=args.sort,
                    limit=args.limit,
                    fetch_all=args.fetch_all,
                    no_interactive=no_interactive,
                    _prepared_query=prepared,
                    _extra_kwargs=extra_kwargs,
                )

        list_parser.set_defaults(func=_dispatch_study_list)

    else:
        if extra_args_fn is not None:
            extra_args_fn(list_parser)
        add_list_query_args(
            list_parser,
            include_filter=include_filter,
            filter_dest=filter_dest,
            filter_help=filter_help,
        )

        @with_sdk
        def list_cmd_no_study(
            sdk: ImednetSDK,
            output_format: str | None = None,
            filter_args: list[str] | None = None,
            sort: str | None = None,
            limit: int | None = None,
            fetch_all: bool = False,
            no_interactive: bool = False,
            _prepared_query: tuple[dict[str, Any], int | None] | None = None,
            _extra_kwargs: Mapping[str, Any] | None = None,
        ) -> None:
            resolved_format = resolve_output_format(output_format)
            req_kwargs, effective_limit = (
                _prepared_query
                if _prepared_query is not None
                else _prepare_query(
                    filter_values=filter_args,
                    sort=sort,
                    limit=limit,
                    fetch_all=fetch_all,
                    resolved_format=resolved_format,
                    no_interactive=no_interactive,
                    disallow_study_key=False,
                )
            )
            call_kwargs = {**_extra_kwargs, **req_kwargs} if _extra_kwargs else req_kwargs
            with fetching_status(name):
                items, truncated, load_remaining = collect_list_items_with_remainder(
                    getattr(sdk, attr).list(**call_kwargs),
                    effective_limit,
                )
            _render_list_results(
                items,
                name=name,
                empty_msg=empty_msg,
                summary_fields=summary_fields,
                row_mapper=row_mapper,
                resolved_format=resolved_format,
                truncated=truncated,
                no_interactive=no_interactive,
                load_remaining=load_remaining,
            )

        def _dispatch_no_study_list(args: argparse.Namespace) -> None:
            no_interactive = getattr(args, "no_interactive", False)
            resolved_format = resolve_output_format(args.output_format)
            filter_values = getattr(args, filter_dest, None) if include_filter else None
            extra_kwargs = dict(extra_kwargs_fn(args)) if extra_kwargs_fn is not None else None
            prepared = (
                _prepare_query(
                    filter_values=filter_values,
                    sort=args.sort,
                    limit=args.limit,
                    fetch_all=args.fetch_all,
                    resolved_format=resolved_format,
                    no_interactive=no_interactive,
                    disallow_study_key=False,
                )
                if preflight_query
                else None
            )
            with simulation_mode_context(bool(getattr(args, "simulation", False))):
                list_cmd_no_study(
                    output_format=resolved_format,
                    filter_args=filter_values,
                    sort=args.sort,
                    limit=args.limit,
                    fetch_all=args.fetch_all,
                    no_interactive=no_interactive,
                    _prepared_query=prepared,
                    _extra_kwargs=extra_kwargs,
                )

        list_parser.set_defaults(func=_dispatch_no_study_list)
