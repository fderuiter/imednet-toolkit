"""CLI commands for managing study records."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ...endpoints.records import RecordsEndpoint
from ...models.records import Record
from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, display_list, fetching_status, print_error
from ..utils.args import (
    add_list_query_args,
    add_output_format,
    collect_list_items_with_remainder,
    prepare_list_query,
    resolve_output_format,
)
from ..utils.export import export_list_to_file


def _record_view_model(r: Any) -> dict[str, Any]:
    return {
        "ID": r.record_id,
        "Subject": r.subject_key,
        "Form": r.form_key,
        "Status": r.record_status,
        "Created": r.date_created,
    }


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("records", help="Manage records within a study.")
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="List records for a study.")
    list_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    list_parser.add_argument(
        "-o", "--output", help="Save records to the given format (json or csv)"
    )
    add_list_query_args(
        list_parser,
        filter_dest="record_filter",
        include_record_data_filter=True,
    )

    add_output_format(list_parser)

    def _validate_records_output(output: str | None, output_format: str) -> None:
        if output and output_format == "json":
            print_error("Choose --format json for stdout or --output for a file.")
            sys.exit(1)
        if output and output.lower() not in {"json", "csv"}:
            print_error("Invalid output format. Use 'json' or 'csv'.")
            sys.exit(1)

    @with_sdk
    def list_records(
        sdk: ImednetSDK,
        study_key: str,
        output: str | None = None,
        output_format: str | None = None,
        record_filter: list[str] | None = None,
        record_data_filter: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        fetch_all: bool = False,
        no_interactive: bool = False,
        _prepared_query: tuple[dict[str, Any], int | None] | None = None,
    ) -> None:
        resolved_format = resolve_output_format(output_format)
        if _prepared_query is None:
            _validate_records_output(output, resolved_format)
            req_kwargs, effective_limit = prepare_list_query(
                filter_args=record_filter,
                record_data_filter=record_data_filter,
                sort=sort,
                limit=limit,
                fetch_all=fetch_all,
                output_format=resolved_format,
                has_output_file=bool(output),
                no_interactive=no_interactive,
                model=Record,
                allowed_attributes=RecordsEndpoint.FILTER_ATTRIBUTES,
                disallow_study_key=True,
            )
        else:
            req_kwargs, effective_limit = _prepared_query

        with fetching_status("records", study_key):
            records, truncated, load_remaining = collect_list_items_with_remainder(
                sdk.records.list(study_key, **req_kwargs),
                effective_limit,
            )

        if resolved_format == "json":
            display_list(records, "records", output_format="json")
        elif output:
            export_list_to_file(records, "records", output.lower())
        else:
            view_models = [_record_view_model(r) for r in records]

            def _load_more_records() -> tuple[list[dict[str, Any]], list[Any]]:
                more = load_remaining() if load_remaining is not None else []
                return [_record_view_model(r) for r in more], more

            display_list(
                view_models,
                "records",
                "No records found.",
                details=records,
                truncated=truncated,
                interactive=False if no_interactive else None,
                output_format=resolved_format,
                on_load_all=_load_more_records if load_remaining is not None else None,
            )

    def _dispatch_list_records(args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        resolved_format = resolve_output_format(args.output_format)
        _validate_records_output(args.output, resolved_format)
        prepared = prepare_list_query(
            filter_args=args.record_filter,
            record_data_filter=args.record_data_filter,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
            output_format=resolved_format,
            has_output_file=bool(args.output),
            no_interactive=no_interactive,
            model=Record,
            allowed_attributes=RecordsEndpoint.FILTER_ATTRIBUTES,
            disallow_study_key=True,
        )
        list_records(
            study_key=args.study_key,
            output=args.output,
            output_format=resolved_format,
            record_filter=args.record_filter,
            record_data_filter=args.record_data_filter,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
            no_interactive=no_interactive,
            _prepared_query=prepared,
        )

    list_parser.set_defaults(func=_dispatch_list_records)
