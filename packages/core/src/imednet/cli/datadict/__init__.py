"""CLI commands for managing and processing iMednet data dictionaries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ...datadict import diff, get_schema, load_from_api, load_from_csv, to_file, to_json, validate
from ...datadict.models import DataDictionary
from ..utils import get_sdk


def _handle_from_csv(args: argparse.Namespace) -> None:
    """Handle `imednet datadict from-csv` command."""
    try:
        dd = load_from_csv(
            args.path,
            strict=args.strict,
            no_raw_xml=args.no_raw_xml,
        )
    except Exception as exc:
        print(f"Error loading from CSV: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        to_file(dd, args.output)
        print(f"Successfully wrote data dictionary to '{args.output}'")
    else:
        print(to_json(dd), end="")


def _handle_from_api(args: argparse.Namespace) -> None:
    """Handle `imednet datadict from-api` command."""
    try:
        sdk = get_sdk()
        dd = load_from_api(sdk, args.study_key, no_raw_xml=args.no_raw_xml)
    except Exception as exc:
        print(f"Error loading from API: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        to_file(dd, args.output)
        print(f"Successfully wrote data dictionary to '{args.output}'")
    else:
        print(to_json(dd), end="")


def _handle_validate(args: argparse.Namespace) -> None:
    """Handle `imednet datadict validate` command."""
    schema_dict = None
    if args.schema:
        try:
            schema_text = Path(args.schema).read_text(encoding="utf-8")
            schema_dict = json.loads(schema_text)
        except Exception as exc:
            print(f"Error reading schema file '{args.schema}': {exc}", file=sys.stderr)
            sys.exit(1)

    errors = validate(args.file, schema=schema_dict)
    if errors:
        print(f"Validation failed with {len(errors)} error(s):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Validation passed: '{args.file}' conforms to the data dictionary specification.")


def _handle_diff(args: argparse.Namespace) -> None:
    """Handle `imednet datadict diff` command."""
    try:
        old_text = Path(args.old).read_text(encoding="utf-8")
        old_dd = DataDictionary.model_validate_json(old_text)
    except Exception as exc:
        print(f"Error reading baseline data dictionary '{args.old}': {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        new_text = Path(args.new).read_text(encoding="utf-8")
        new_dd = DataDictionary.model_validate_json(new_text)
    except Exception as exc:
        print(f"Error reading target data dictionary '{args.new}': {exc}", file=sys.stderr)
        sys.exit(1)

    report = diff(old_dd, new_dd)

    if args.format == "json":
        print(report.to_json(), end="")
    elif args.format == "md":
        print(report.to_markdown(), end="")
    else:
        print(report.to_text(), end="")


def _handle_schema(args: argparse.Namespace) -> None:
    """Handle `imednet datadict schema` command."""
    schema_dict = get_schema()
    json_text = json.dumps(schema_dict, indent=2) + "\n"

    if args.output:
        try:
            Path(args.output).write_text(json_text, encoding="utf-8")
            print(f"Successfully wrote schema to '{args.output}'")
        except Exception as exc:
            print(f"Error writing schema to '{args.output}': {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        print(json_text, end="")


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup parser for `imednet datadict` commands."""
    datadict_parser = subparsers.add_parser(
        "datadict",
        help="Manage, transform, and validate study data dictionaries.",
    )
    cmd_subparsers = datadict_parser.add_subparsers(dest="datadict_command")

    # 1. from-csv
    csv_parser = cmd_subparsers.add_parser(
        "from-csv",
        help="Compile canonical JSON data dictionary from CSV directory or ZIP archive.",
    )
    csv_parser.add_argument(
        "path",
        help="Path to directory or ZIP archive containing the 4 data dictionary CSV files.",
    )
    csv_parser.add_argument(
        "-o",
        "--output",
        help="Destination JSON file path (default: stdout).",
    )
    csv_parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail fast on unknown columns, unreferenced forms, or format issues.",
    )
    csv_parser.add_argument(
        "--no-raw-xml",
        action="store_true",
        help="Omit verbatim XML strings from business logic rules.",
    )
    csv_parser.set_defaults(func=_handle_from_csv)

    # 2. from-api
    api_parser = cmd_subparsers.add_parser(
        "from-api",
        help="Extract study forms and variables from live iMednet REST API.",
    )
    api_parser.add_argument(
        "--study-key",
        required=True,
        help="Study key identifier to query.",
    )
    api_parser.add_argument(
        "-o",
        "--output",
        help="Destination JSON file path (default: stdout).",
    )
    api_parser.add_argument(
        "--no-raw-xml",
        action="store_true",
        help="Omit verbatim XML strings.",
    )
    api_parser.set_defaults(func=_handle_from_api)

    # 3. validate
    val_parser = cmd_subparsers.add_parser(
        "validate",
        help="Validate a data dictionary JSON file against model and schema rules.",
    )
    val_parser.add_argument(
        "file",
        help="Path to data dictionary JSON file to validate.",
    )
    val_parser.add_argument(
        "--schema",
        help="Optional path to custom JSON schema file for validation.",
    )
    val_parser.set_defaults(func=_handle_validate)

    # 4. diff
    diff_parser = cmd_subparsers.add_parser(
        "diff",
        help="Compare two data dictionary JSON files structurally.",
    )
    diff_parser.add_argument("old", help="Baseline data dictionary JSON file.")
    diff_parser.add_argument("new", help="Updated data dictionary JSON file.")
    diff_parser.add_argument(
        "--format",
        choices=["text", "json", "md"],
        default="text",
        help="Output format: text, json, or md (default: text).",
    )
    diff_parser.set_defaults(func=_handle_diff)

    # 5. schema
    schema_parser = cmd_subparsers.add_parser(
        "schema",
        help="Export the canonical JSON Schema for data dictionaries.",
    )
    schema_parser.add_argument(
        "-o",
        "--output",
        help="Destination path for schema JSON file (default: stdout).",
    )
    schema_parser.set_defaults(func=_handle_schema)

    datadict_parser.set_defaults(
        func=lambda args: (
            datadict_parser.print_help() if not getattr(args, "datadict_command", None) else None
        )
    )
