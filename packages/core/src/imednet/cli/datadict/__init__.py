"""CLI commands for managing and processing iMednet data dictionaries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ...datadict import (
    OdmArchive,
    diff,
    get_schema,
    load_from_api,
    load_from_cdisc_odm,
    load_from_csv,
    load_from_redcap,
    load_odm_archive,
    to_file,
    to_json,
    to_odm_file,
    to_odm_xml,
    validate,
)
from ...datadict.models import DataDictionary
from ...sdk import ImednetSDK
from ..decorators import redact_cli_secrets, validate_cli_path_segment, with_sdk
from ..utils import (
    UniqueStoreAction,
    add_simulation_arg,
    add_study_key_args,
    get_sdk,
    print_error,
    resolve_output_format,
    resolve_study_key,
    simulation_mode_context,
)

__all__ = ["get_sdk", "setup_parser"]


def _handle_to_odm(args: argparse.Namespace) -> None:
    """Handle `imednet datadict to-odm` command."""
    try:
        input_text = Path(args.file).read_text(encoding="utf-8")
        raw_payload = json.loads(input_text)
        source_obj: DataDictionary | OdmArchive
        if isinstance(raw_payload, dict) and "dataDictionary" in raw_payload:
            source_obj = OdmArchive.model_validate(raw_payload)
        else:
            source_obj = DataDictionary.model_validate(raw_payload)
    except Exception as exc:
        print_error(redact_cli_secrets(f"Error reading data dictionary from '{args.file}': {exc}"))
        sys.exit(1)

    try:
        if args.output:
            to_odm_file(source_obj, args.output, file_oid=getattr(args, "file_oid", None))
            print(f"Successfully wrote CDISC ODM XML to '{args.output}'", file=sys.stderr)
        else:
            print(to_odm_xml(source_obj, file_oid=getattr(args, "file_oid", None)), end="")
    except OSError as exc:
        print_error(redact_cli_secrets(f"Error writing CDISC ODM XML to '{args.output}': {exc}"))
        sys.exit(1)
    except Exception as exc:
        print_error(redact_cli_secrets(f"Error exporting to CDISC ODM XML: {exc}"))
        sys.exit(1)


def _handle_from_csv(args: argparse.Namespace) -> None:
    """Handle `imednet datadict from-csv` command."""
    try:
        dd = load_from_csv(
            args.path,
            strict=args.strict,
            no_raw_xml=args.no_raw_xml,
        )
    except Exception as exc:
        print_error(redact_cli_secrets(f"Error loading from CSV: {exc}"))
        sys.exit(1)

    if args.output:
        try:
            to_file(dd, args.output)
            print(f"Successfully wrote data dictionary to '{args.output}'", file=sys.stderr)
        except OSError as exc:
            print_error(
                redact_cli_secrets(f"Error writing data dictionary to '{args.output}': {exc}")
            )
            sys.exit(1)
    else:
        print(to_json(dd), end="")


def _handle_from_odm(args: argparse.Namespace) -> None:
    """Handle `imednet datadict from-odm` command."""
    if getattr(args, "archive", False):
        try:
            archive = load_odm_archive(
                args.path,
                study_key=getattr(args, "study_key", None),
                strict=args.strict,
                no_raw_xml=args.no_raw_xml,
            )
        except Exception as exc:
            print_error(redact_cli_secrets(f"Error loading from CDISC ODM: {exc}"))
            sys.exit(1)

        archive_json = (
            json.dumps(
                archive.model_dump(mode="json", by_alias=True, exclude_none=True),
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        if args.output:
            try:
                out_path = Path(args.output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(archive_json, encoding="utf-8")
                print(f"Successfully wrote ODM archive JSON to '{args.output}'", file=sys.stderr)
            except OSError as exc:
                print_error(
                    redact_cli_secrets(f"Error writing ODM archive to '{args.output}': {exc}")
                )
                sys.exit(1)
        else:
            print(archive_json, end="")
        return

    try:
        dd = load_from_cdisc_odm(
            args.path,
            study_key=getattr(args, "study_key", None),
            strict=args.strict,
            no_raw_xml=args.no_raw_xml,
        )
    except Exception as exc:
        print_error(redact_cli_secrets(f"Error loading from CDISC ODM: {exc}"))
        sys.exit(1)

    if args.output:
        try:
            to_file(dd, args.output)
            print(f"Successfully wrote data dictionary to '{args.output}'", file=sys.stderr)
        except OSError as exc:
            print_error(
                redact_cli_secrets(f"Error writing data dictionary to '{args.output}': {exc}")
            )
            sys.exit(1)
    else:
        print(to_json(dd), end="")


def _handle_from_redcap(args: argparse.Namespace) -> None:
    """Handle `imednet datadict from-redcap` command."""
    try:
        dd = load_from_redcap(
            Path(args.path),
            study_key=getattr(args, "study_key", None),
            strict=args.strict,
            no_raw_xml=args.no_raw_xml,
        )
    except Exception as exc:
        print_error(redact_cli_secrets(f"Error loading from REDCap: {exc}"))
        sys.exit(1)

    if args.output:
        try:
            to_file(dd, args.output)
            print(f"Successfully wrote data dictionary to '{args.output}'", file=sys.stderr)
        except OSError as exc:
            print_error(
                redact_cli_secrets(f"Error writing data dictionary to '{args.output}': {exc}")
            )
            sys.exit(1)
    else:
        print(to_json(dd), end="")


@with_sdk
def _from_api(
    sdk: ImednetSDK,
    study_key: str,
    output: str | None = None,
    no_raw_xml: bool = False,
) -> None:
    """Load data dictionary from live API with managed SDK lifecycle and error redaction."""
    dd = load_from_api(sdk, study_key, no_raw_xml=no_raw_xml)
    if output:
        try:
            to_file(dd, output)
            print(f"Successfully wrote data dictionary to '{output}'", file=sys.stderr)
        except OSError as exc:
            print_error(redact_cli_secrets(f"Error writing data dictionary to '{output}': {exc}"))
            sys.exit(1)
    else:
        print(to_json(dd), end="")


def _handle_from_api(args: argparse.Namespace) -> None:
    """Handle `imednet datadict from-api` command."""
    parser: argparse.ArgumentParser | None = getattr(args, "_api_parser", None)
    if parser is not None:
        study_key = resolve_study_key(parser, args, required=True)
    else:
        study_key = getattr(args, "study_key", None)
    assert study_key is not None
    validate_cli_path_segment(study_key, name="study_key")
    with simulation_mode_context(bool(getattr(args, "simulation", False))):
        _from_api(
            study_key=study_key,
            output=args.output,
            no_raw_xml=args.no_raw_xml,
        )


def _emit_validate_json(file_path: str, errors: list[str]) -> None:
    """Emit dedicated single-document JSON validation payload on stdout."""
    payload = {
        "schemaVersion": "1.0",
        "file": redact_cli_secrets(str(file_path)),
        "valid": len(errors) == 0,
        "error_count": len(errors),
        "errors": [redact_cli_secrets(err) for err in errors],
    }
    print(json.dumps(payload, indent=2) + "\n", end="")


def _handle_validate(args: argparse.Namespace) -> None:
    """Handle `imednet datadict validate` command."""
    output_format = resolve_output_format(getattr(args, "format", None), default="text")
    schema_dict = None
    if args.schema:
        try:
            schema_text = Path(args.schema).read_text(encoding="utf-8")
            schema_dict = json.loads(schema_text)
        except Exception as exc:
            err_msg = f"Error reading schema file '{args.schema}': {exc}"
            if output_format == "json":
                _emit_validate_json(args.file, [err_msg])
            else:
                print_error(redact_cli_secrets(err_msg))
            sys.exit(1)

    errors = validate(args.file, schema=schema_dict)
    if output_format == "json":
        _emit_validate_json(args.file, errors)
        if errors:
            sys.exit(1)
        return

    if errors:
        print_error(f"Validation failed with {len(errors)} error(s):")
        for err in errors:
            print_error(redact_cli_secrets(f"  - {err}"))
        sys.exit(1)
    else:
        print(f"Validation passed: '{args.file}' conforms to the data dictionary specification.")


def _handle_diff(args: argparse.Namespace) -> None:
    """Handle `imednet datadict diff` command."""
    try:
        old_text = Path(args.old).read_text(encoding="utf-8")
        old_dd = DataDictionary.model_validate_json(old_text)
    except Exception as exc:
        print_error(
            redact_cli_secrets(f"Error reading baseline data dictionary '{args.old}': {exc}")
        )
        sys.exit(1)

    try:
        new_text = Path(args.new).read_text(encoding="utf-8")
        new_dd = DataDictionary.model_validate_json(new_text)
    except Exception as exc:
        print_error(redact_cli_secrets(f"Error reading target data dictionary '{args.new}': {exc}"))
        sys.exit(1)

    report = diff(old_dd, new_dd)
    output_format = resolve_output_format(args.format, default="text")

    if output_format == "json":
        print(report.to_json(), end="")
    elif output_format == "md":
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
            print(f"Successfully wrote schema to '{args.output}'", file=sys.stderr)
        except Exception as exc:
            print_error(redact_cli_secrets(f"Error writing schema to '{args.output}': {exc}"))
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

    # 1b. from-odm
    odm_parser = cmd_subparsers.add_parser(
        "from-odm",
        help="Compile canonical JSON data dictionary from CDISC ODM 1.3 XML file.",
    )
    odm_parser.add_argument(
        "path",
        help="Path to CDISC ODM XML file.",
    )
    odm_parser.add_argument(
        "-o",
        "--output",
        help="Destination JSON file path (default: stdout).",
    )
    odm_parser.add_argument(
        "--study-key",
        action=UniqueStoreAction,
        help="Optional study key identifier override.",
    )
    odm_parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail fast on unknown elements, unreferenced definitions, or format issues.",
    )
    odm_parser.add_argument(
        "--no-raw-xml",
        action="store_true",
        help="Omit verbatim XML strings from business logic rules.",
    )
    odm_parser.add_argument(
        "--archive",
        action="store_true",
        help="Parse full ODM 1.3.2 archive including AdminData, ReferenceData, and ClinicalData.",
    )
    odm_parser.set_defaults(func=_handle_from_odm)

    # 1b2. to-odm
    to_odm_parser = cmd_subparsers.add_parser(
        "to-odm",
        help="Export data dictionary to CDISC ODM 1.3 XML format.",
    )
    to_odm_parser.add_argument(
        "file",
        help="Path to data dictionary JSON file to export.",
    )
    to_odm_parser.add_argument(
        "-o",
        "--output",
        help="Destination XML file path (default: stdout).",
    )
    to_odm_parser.add_argument(
        "--file-oid",
        help="Optional ODM FileOID attribute override.",
    )
    to_odm_parser.set_defaults(func=_handle_to_odm)

    # 1c. from-redcap
    redcap_parser = cmd_subparsers.add_parser(
        "from-redcap",
        help="Compile canonical JSON data dictionary from REDCap CSV file.",
    )
    redcap_parser.add_argument(
        "path",
        help="Path to REDCap data dictionary CSV file.",
    )
    redcap_parser.add_argument(
        "-o",
        "--output",
        help="Destination JSON file path (default: stdout).",
    )
    redcap_parser.add_argument(
        "--study-key",
        action=UniqueStoreAction,
        help="Optional study key identifier override.",
    )
    redcap_parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail fast on unknown columns, unreferenced forms, or format issues.",
    )
    redcap_parser.add_argument(
        "--no-raw-xml",
        action="store_true",
        help="Omit verbatim XML strings.",
    )
    redcap_parser.set_defaults(func=_handle_from_redcap)

    # 2. from-api
    api_parser = cmd_subparsers.add_parser(
        "from-api",
        help="Extract study forms and variables from live iMednet REST API.",
    )
    add_study_key_args(
        api_parser,
        flag_help="Study key identifier to query.",
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
    add_simulation_arg(api_parser)
    api_parser.set_defaults(func=_handle_from_api, _api_parser=api_parser)

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
    val_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default=None,
        help="Output format: text or json (default: text).",
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
        default=None,
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
