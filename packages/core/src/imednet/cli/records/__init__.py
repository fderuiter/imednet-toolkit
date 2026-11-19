"""CLI commands for managing study records."""

from __future__ import annotations

import argparse
import sys

from ...models.records import Record
from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, display_list, fetching_status, print_error
from ..utils.args import add_list_query_args, add_output_format, build_list_query_kwargs
from ..utils.export import export_list_to_file


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
        include_record_data_filter=True,
        filter_dest="record_filter",
        filter_example="form_key=DEMO",
    )

    add_output_format(list_parser)

    @with_sdk
    def list_records(
        sdk: ImednetSDK,
        study_key: str,
        output: str | None = None,
        output_format: str = "table",
        record_filter: list[str] | None = None,
        record_data_filter: str | None = None,
        sort: str | None = None,
        no_interactive: bool = False,
    ) -> None:
        if output and output_format == "json":
            print_error("Choose --format json for stdout or --output for a file.")
            sys.exit(1)
        if output and output.lower() not in {"json", "csv"}:
            print_error("Invalid output format. Use 'json' or 'csv'.")
            sys.exit(1)

        query_kwargs = build_list_query_kwargs(
            filters=record_filter,
            sort=sort,
            record_data_filter=record_data_filter,
            model=Record,
        )

        with fetching_status("records", study_key):
            records = list(sdk.records.list(study_key, **query_kwargs))

        if output_format == "json":
            display_list(records, "records", output_format="json")
        elif output:
            export_list_to_file(records, "records", output.lower())
        else:
            view_models = []
            for r in records:
                view_models.append(
                    {
                        "ID": r.record_id,
                        "Subject": r.subject_key,
                        "Form": r.form_key,
                        "Status": r.record_status,
                        "Created": r.date_created,
                    }
                )
            display_list(
                view_models,
                "records",
                "No records found.",
                details=records,
                interactive=False if no_interactive else None,
            )

    list_parser.set_defaults(
        func=lambda args: list_records(
            study_key=args.study_key,
            output=args.output,
            output_format=args.output_format,
            record_filter=args.record_filter,
            record_data_filter=args.record_data_filter,
            sort=args.sort,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )
