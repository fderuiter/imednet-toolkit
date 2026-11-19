"""Helpers for registering common CLI commands."""

from __future__ import annotations

import argparse
from typing import Any

from imednet.cli.decorators import with_sdk
from imednet.sdk import ImednetSDK

from .args import (
    STUDY_KEY_ARG,
    add_list_query_args,
    add_no_interactive,
    add_output_format,
    build_list_query_kwargs,
)
from .context import fetching_status
from .output import display_list


def _resolve_endpoint_model(attr: str) -> type[Any] | None:
    """Return the Pydantic resource model class associated with SDK endpoint ``attr``."""
    from imednet import models as _models

    mapping: dict[str, type[Any]] = {
        "codings": _models.Coding,
        "forms": _models.Form,
        "intervals": _models.Interval,
        "jobs": _models.Job,
        "queries": _models.Query,
        "record_revisions": _models.RecordRevision,
        "records": _models.Record,
        "sites": _models.Site,
        "studies": _models.Study,
        "subjects": _models.Subject,
        "users": _models.User,
        "variables": _models.Variable,
        "visits": _models.Visit,
    }
    return mapping.get(attr)


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

    add_list_query_args(list_parser)
    add_output_format(list_parser)
    add_no_interactive(list_parser)
    resolved_model = model if model is not None else _resolve_endpoint_model(attr)

    if requires_study_key:
        list_parser.add_argument("study_key", help=STUDY_KEY_ARG)

        @with_sdk
        def list_cmd(
            sdk: ImednetSDK,
            study_key: str,
            filters: list[str] | None = None,
            sort: str | None = None,
            output_format: str = "table",
            no_interactive: bool = False,
        ) -> None:
            query_kwargs = build_list_query_kwargs(
                filters=filters,
                sort=sort,
                model=resolved_model,
            )
            with fetching_status(name, study_key):
                items = list(getattr(sdk, attr).list(study_key, **query_kwargs))
            display_list(
                items,
                name,
                empty_msg,
                fields=summary_fields,
                output_format=output_format,
                details=items,
                interactive=False if no_interactive else None,
            )

        list_parser.set_defaults(
            func=lambda args: list_cmd(
                study_key=args.study_key,
                filters=args.filters,
                sort=args.sort,
                output_format=args.output_format,
                no_interactive=getattr(args, "no_interactive", False),
            )
        )

    else:

        @with_sdk
        def list_cmd_no_study(
            sdk: ImednetSDK,
            filters: list[str] | None = None,
            sort: str | None = None,
            output_format: str = "table",
            no_interactive: bool = False,
        ) -> None:
            query_kwargs = build_list_query_kwargs(
                filters=filters,
                sort=sort,
                model=resolved_model,
            )
            with fetching_status(name):
                items = list(getattr(sdk, attr).list(**query_kwargs))
            display_list(
                items,
                name,
                empty_msg,
                fields=summary_fields,
                output_format=output_format,
                details=items,
                interactive=False if no_interactive else None,
            )

        list_parser.set_defaults(
            func=lambda args: list_cmd_no_study(
                filters=args.filters,
                sort=args.sort,
                output_format=args.output_format,
                no_interactive=getattr(args, "no_interactive", False),
            )
        )
