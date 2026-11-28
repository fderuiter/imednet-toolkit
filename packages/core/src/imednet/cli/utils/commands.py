"""Helpers for registering common CLI commands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any

from imednet.cli.decorators import validate_cli_path_segment, with_sdk

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
from .output import display_list, print_error

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK


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
    drain_on_fallback: bool = True,
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
            drain_on_fallback=drain_on_fallback,
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
        drain_on_fallback=drain_on_fallback,
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
    multi_bound_attributes: Collection[str] | None = None,
    export_filename_prefix: str | None = None,
    extra_args_fn: Callable[[argparse.ArgumentParser], None] | None = None,
    extra_kwargs_fn: Callable[[argparse.Namespace], Mapping[str, Any]] | None = None,
    preflight_query: bool = True,
) -> None:
    """Attach a standard ``list`` command to parser."""
    metadata_resolved = False
    resolved_model: type[Any] | None = model
    allowed_attributes: frozenset[str] | None = None

    def _ensure_query_metadata() -> tuple[type[Any] | None, frozenset[str] | None]:
        nonlocal metadata_resolved, resolved_model, allowed_attributes
        if not metadata_resolved:
            from imednet.endpoints.registry import ENDPOINT_REGISTRY

            endpoint_cls = ENDPOINT_REGISTRY.get(attr)
            if resolved_model is None and endpoint_cls is not None:
                resolved_model = getattr(endpoint_cls, "MODEL", None)
            if endpoint_cls is not None:
                raw_attrs = getattr(endpoint_cls, "FILTER_ATTRIBUTES", None)
                if isinstance(raw_attrs, frozenset):
                    allowed_attributes = raw_attrs
            metadata_resolved = True
        return resolved_model, allowed_attributes

    def _validate_export_output(output: str | None, explicit_output_format: str | None) -> None:
        if export_filename_prefix is None or output is None:
            return
        from .export import resolve_records_export_target

        if output and explicit_output_format == "json":
            print_error("Choose --format json for stdout or --output for a file.")
            sys.exit(1)
        if resolve_records_export_target(output, export_filename_prefix) is None:
            print_error(
                "Invalid output format. Use 'json' or 'csv'. "
                "Explicit '.json' and '.csv' file paths are also supported."
            )
            sys.exit(1)

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
    list_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask sensitive clinical PHI fields with ***MASKED*** (default: enabled).",
    )

    def _add_export_args(target_parser: argparse.ArgumentParser) -> None:
        if export_filename_prefix is None:
            return
        target_parser.add_argument(
            "-o",
            "--output",
            dest="output",
            default=None,
            help=f"Save {name} to a CSV or JSON file (csv, json, or a .csv/.json path).",
        )

    def _prepare_query(
        *,
        filter_values: list[str] | None,
        sort: str | None,
        limit: int | None,
        fetch_all: bool,
        resolved_format: str,
        has_output_file: bool = False,
        no_interactive: bool,
        disallow_study_key: bool,
    ) -> tuple[dict[str, Any], int | None]:
        target_model, target_allowed_attrs = _ensure_query_metadata()
        return prepare_list_query(
            filter_args=filter_values,
            sort=sort,
            limit=limit,
            fetch_all=fetch_all,
            output_format=resolved_format,
            has_output_file=has_output_file,
            no_interactive=no_interactive,
            model=target_model,
            allowed_attributes=target_allowed_attrs,
            disallow_study_key=disallow_study_key,
            multi_bound_attributes=multi_bound_attributes,
        )

    if requires_study_key:
        add_study_key_args(list_parser)
        _add_export_args(list_parser)
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
            output: str | None = None,
            output_format: str | None = None,
            filter_args: list[str] | None = None,
            sort: str | None = None,
            limit: int | None = None,
            fetch_all: bool = False,
            no_interactive: bool = False,
            mask_phi: bool = True,
            _prepared_query: tuple[dict[str, Any], int | None] | None = None,
            _extra_kwargs: Mapping[str, Any] | None = None,
        ) -> None:
            resolved_format = resolve_output_format(output_format)
            if _prepared_query is None:
                _validate_export_output(output, output_format)
                req_kwargs, effective_limit = _prepare_query(
                    filter_values=filter_args,
                    sort=sort,
                    limit=limit,
                    fetch_all=fetch_all,
                    resolved_format=resolved_format,
                    has_output_file=bool(output),
                    no_interactive=no_interactive,
                    disallow_study_key=True,
                )
            else:
                req_kwargs, effective_limit = _prepared_query
            call_kwargs = {**_extra_kwargs, **req_kwargs} if _extra_kwargs else req_kwargs
            with fetching_status(name, study_key):
                items, truncated, load_remaining = collect_list_items_with_remainder(
                    getattr(sdk, attr).list(study_key, **call_kwargs),
                    effective_limit,
                )
            if export_filename_prefix is not None and output:
                from .export import export_list_to_file

                export_list_to_file(
                    items,
                    export_filename_prefix,
                    output,
                    study_key=study_key,
                    mask_phi=mask_phi,
                    sdk=sdk,
                )
                return

            if mask_phi:
                from .export import mask_cli_models, resolve_cli_phi_fields

                custom_phi_fields = (
                    resolve_cli_phi_fields(study_key, sdk=sdk)
                    if items or load_remaining is not None
                    else None
                )
                items = mask_cli_models(
                    items,
                    study_key=study_key,
                    mask_phi=True,
                    sdk=sdk,
                    custom_phi_fields=custom_phi_fields,
                )
                orig_loader = load_remaining
                if orig_loader is not None:

                    def _masked_loader() -> list[Any]:
                        return mask_cli_models(
                            orig_loader(),
                            study_key=study_key,
                            mask_phi=True,
                            sdk=sdk,
                            custom_phi_fields=custom_phi_fields,
                        )

                    load_remaining = _masked_loader

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
                drain_on_fallback=limit is None,
            )

        def _dispatch_study_list(args: argparse.Namespace) -> None:
            study_key = resolve_study_key(list_parser, args, required=True)
            assert study_key is not None
            validate_cli_path_segment(study_key, name="study_key")
            no_interactive = getattr(args, "no_interactive", False)
            output = getattr(args, "output", None)
            mask_phi = bool(getattr(args, "mask_phi", True))
            _validate_export_output(output, args.output_format)
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
                    has_output_file=bool(output),
                    no_interactive=no_interactive,
                    disallow_study_key=True,
                )
                if preflight_query
                else None
            )
            with simulation_mode_context(bool(getattr(args, "simulation", False))):
                list_cmd(
                    study_key=study_key,
                    output=output,
                    output_format=resolved_format,
                    filter_args=filter_values,
                    sort=args.sort,
                    limit=args.limit,
                    fetch_all=args.fetch_all,
                    no_interactive=no_interactive,
                    mask_phi=mask_phi,
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
            mask_phi: bool = True,
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
            if mask_phi:
                from .export import mask_cli_models

                items = mask_cli_models(items, mask_phi=True, sdk=sdk)
                orig_loader_no_study = load_remaining
                if orig_loader_no_study is not None:

                    def _masked_loader_no_study() -> list[Any]:
                        return mask_cli_models(orig_loader_no_study(), mask_phi=True, sdk=sdk)

                    load_remaining = _masked_loader_no_study

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
                drain_on_fallback=limit is None,
            )

        def _dispatch_no_study_list(args: argparse.Namespace) -> None:
            no_interactive = getattr(args, "no_interactive", False)
            mask_phi = bool(getattr(args, "mask_phi", True))
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
                    mask_phi=mask_phi,
                    _prepared_query=prepared,
                    _extra_kwargs=extra_kwargs,
                )

        list_parser.set_defaults(func=_dispatch_no_study_list)
