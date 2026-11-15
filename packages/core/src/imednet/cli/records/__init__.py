"""CLI commands for managing study records."""

from __future__ import annotations

import argparse
import sys

from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, display_list, fetching_status, print_error
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

    @with_sdk
    def list_records(sdk: ImednetSDK, study_key: str, output: str | None = None) -> None:
        if output and output.lower() not in {"json", "csv"}:
            print_error("Invalid output format. Use 'json' or 'csv'.")
            sys.exit(1)

        with fetching_status("records", study_key):
            records = list(sdk.records.list(study_key))

        if output:
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
            display_list(view_models, "records", "No records found.")

    list_parser.set_defaults(
        func=lambda args: list_records(study_key=args.study_key, output=args.output)
    )
