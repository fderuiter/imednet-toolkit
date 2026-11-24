"""CLI commands for managing study records."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ...endpoints.records import RecordsEndpoint
from ...models.records import Record
from ...sdk import ImednetSDK
from ...utils.batch_coordinator import BatchSubmissionSummary
from ...utils.pandas import import_records_csv
from ..decorators import validate_cli_path_segment, with_sdk
from ..utils import display_list, fetching_status, print_error, simulation_mode_context
from ..utils.args import (
    add_list_query_args,
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    collect_list_items_with_remainder,
    prepare_list_query,
    resolve_output_format,
    resolve_study_key,
)
from ..utils.export import export_list_to_file, resolve_records_export_target
from ..utils.write_helpers import (
    add_write_common_args,
    assemble_cli_write_records,
    coerce_batch_summary,
    execute_cli_write,
    guard_stdin_dash_tty,
    load_records_from_path_or_stdin,
    parse_email_notify,
    resolve_cli_schema,
    submit_cli_records_batch,
    validate_write_numeric_flags,
    write_progress_context,
)


def _has_update_target_selector(args: argparse.Namespace) -> bool:
    """Return True if at least one record update selector flag is set on *args*."""
    return bool(
        getattr(args, "record_id", None) is not None
        or getattr(args, "record_oid", None)
        or getattr(args, "visit_id", None) is not None
        or getattr(args, "interval_name", None)
        or getattr(args, "interval_id", None) is not None
    )


def _has_update_mutation_input(args: argparse.Namespace) -> bool:
    """Return True if at least one record mutation flag is provided on *args*."""
    return bool(
        getattr(args, "data_pairs", None)
        or getattr(args, "record_status", None)
        or getattr(args, "reason_for_change", None)
    )


def _assemble_records_payload(
    args: argparse.Namespace,
    *,
    schema: Any = None,
    is_update: bool = False,
) -> list[dict[str, Any]]:
    """Build record create/update payload(s) from ``--file``, ``--json``, or inline flags."""
    return assemble_cli_write_records(
        args,
        schema=schema,
        study_key=getattr(args, "study_key", None),
        is_subject_registration=False,
        is_update=is_update,
    )


def _assemble_create_records_payload(
    args: argparse.Namespace,
    *,
    schema: Any = None,
) -> list[dict[str, Any]]:
    """Build record creation payload(s) from ``--file``, ``--json``, or inline flags."""
    return _assemble_records_payload(args, schema=schema, is_update=False)


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

    # 1. records list
    list_parser = sub.add_parser("list", help="List records for a study.")
    add_study_key_args(list_parser)
    list_parser.add_argument(
        "-o",
        "--output",
        help=(
            "Deprecated: save records to a file (json, csv, or a .json/.csv path); "
            "prefer --format json or 'imednet export'."
        ),
    )
    list_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action="store_true",
        default=True,
        help="Mask sensitive Subject PHI fields with ***MASKED*** (enabled by default).",
    )
    list_parser.add_argument(
        "--no-mask-phi",
        dest="mask_phi",
        action="store_false",
        help="Disable default PHI masking for authorized raw exports.",
    )
    add_list_query_args(
        list_parser,
        filter_dest="record_filter",
        include_record_data_filter=True,
    )
    add_output_format(list_parser)
    add_simulation_arg(list_parser)

    def _validate_records_output(output: str | None, output_format: str) -> None:
        if output and output_format == "json":
            print_error("Choose --format json for stdout or --output for a file.")
            sys.exit(1)
        if output is not None and resolve_records_export_target(output, "records") is None:
            print_error(
                "Invalid output format. Use 'json' or 'csv'. "
                "Explicit '.json' and '.csv' file paths are also supported."
            )
            sys.exit(1)
        if output:
            print_error(
                "Warning: '-o/--output' on 'records list' is deprecated; use "
                "'--format json' for stdout or 'imednet export' for file exports."
            )

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
        mask_phi: bool = True,
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
            export_list_to_file(
                records,
                "records",
                output,
                study_key=study_key,
                mask_phi=mask_phi,
            )
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
        study_key = resolve_study_key(list_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
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
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            list_records(
                study_key=study_key,
                output=args.output,
                output_format=resolved_format,
                record_filter=args.record_filter,
                record_data_filter=args.record_data_filter,
                sort=args.sort,
                limit=args.limit,
                fetch_all=args.fetch_all,
                no_interactive=no_interactive,
                mask_phi=bool(getattr(args, "mask_phi", True)),
                _prepared_query=prepared,
            )

    list_parser.set_defaults(func=_dispatch_list_records)

    def _add_record_mutation_args(target_parser: argparse.ArgumentParser) -> None:
        add_study_key_args(target_parser)
        target_parser.add_argument(
            "-f",
            "--file",
            dest="input_file",
            default=None,
            help="Path to JSON or CSV file of record payloads ('-' for stdin JSON or CSV).",
        )
        target_parser.add_argument(
            "--json",
            dest="json_payload",
            default=None,
            help="Inline JSON object or array of record payloads.",
        )
        target_parser.add_argument("--form-key", default=None, help="Target form key (formKey).")
        target_parser.add_argument(
            "--form-id", type=int, default=None, help="Target form ID (formId)."
        )
        target_parser.add_argument(
            "--record-id", type=int, default=None, help="Target record ID (recordId)."
        )
        target_parser.add_argument(
            "--record-oid", default=None, help="Target record OID (recordOid)."
        )
        target_parser.add_argument(
            "--visit-id", type=int, default=None, help="Target visit ID (visitId)."
        )
        target_parser.add_argument(
            "--record-status", default=None, help="Target record status (recordStatus)."
        )
        target_parser.add_argument(
            "--reason-for-change",
            default=None,
            help="Audit reason for modifying an existing record (reasonForChange).",
        )
        target_parser.add_argument(
            "--subject-key", default=None, help="Target subject key (subjectKey)."
        )
        target_parser.add_argument(
            "--subject-oid", default=None, help="Target subject OID (subjectOid)."
        )
        target_parser.add_argument(
            "--subject-id", type=int, default=None, help="Target subject ID (subjectId)."
        )
        target_parser.add_argument("--site-name", default=None, help="Target site name (siteName).")
        target_parser.add_argument(
            "--site-id", type=int, default=None, help="Target site ID (siteId)."
        )
        target_parser.add_argument(
            "--interval-name", default=None, help="Target interval name (intervalName)."
        )
        target_parser.add_argument(
            "--interval-id", type=int, default=None, help="Target interval ID (intervalId)."
        )
        target_parser.add_argument(
            "-d",
            "--data",
            action="append",
            dest="data_pairs",
            default=None,
            help="Clinical variable assignment in KEY=VALUE form. Repeat for multiple variables.",
        )
        add_write_common_args(target_parser)

    # 2. records create
    create_parser = sub.add_parser(
        "create",
        help="Create or update one or more records in a study.",
    )
    _add_record_mutation_args(create_parser)

    @with_sdk
    def create_records_cmd(sdk: ImednetSDK, args: argparse.Namespace) -> None:
        def _run() -> BatchSubmissionSummary:
            resolved_schema = resolve_cli_schema(
                sdk,
                args.study_key,
                args.schema_source,
                dry_run=args.dry_run,
            )
            payload = _assemble_create_records_payload(args, schema=resolved_schema)
            return submit_cli_records_batch(
                sdk,
                args,
                payload,
                action_label="record submission",
                schema=resolved_schema,
            )

        execute_cli_write(
            _run,
            study_key=args.study_key,
            resource="records_create",
            output_format=args.output_format,
            dry_run=args.dry_run,
        )

    def _dispatch_create_records(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(create_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        validate_write_numeric_flags(
            chunk_size=args.chunk_size,
            timeout=args.timeout,
            interval=args.interval,
        )
        guard_stdin_dash_tty(args.input_file)
        if args.input_file and args.input_file != "-" and not Path(args.input_file).exists():
            print_error(f"Error: Input file not found: '{args.input_file}'")
            sys.exit(1)
        if (
            not args.input_file
            and not args.json_payload
            and not args.form_key
            and args.form_id is None
        ):
            print_error(
                "Error: Provide --file, --json, or at least --form-key / --form-id to create records."
            )
            sys.exit(1)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            create_records_cmd(args)

    create_parser.set_defaults(func=_dispatch_create_records)

    # 3. records update
    update_parser = sub.add_parser(
        "update",
        help="Update one or more existing records in a study.",
    )
    _add_record_mutation_args(update_parser)

    @with_sdk
    def update_records_cmd(sdk: ImednetSDK, args: argparse.Namespace) -> None:
        def _run() -> BatchSubmissionSummary:
            resolved_schema = resolve_cli_schema(
                sdk,
                args.study_key,
                args.schema_source,
                dry_run=args.dry_run,
            )
            payload = _assemble_records_payload(args, schema=resolved_schema, is_update=True)
            return submit_cli_records_batch(
                sdk,
                args,
                payload,
                action_label="record update",
                schema=resolved_schema,
            )

        execute_cli_write(
            _run,
            study_key=args.study_key,
            resource="records_update",
            output_format=args.output_format,
            dry_run=args.dry_run,
        )

    def _dispatch_update_records(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(update_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        validate_write_numeric_flags(
            chunk_size=args.chunk_size,
            timeout=args.timeout,
            interval=args.interval,
        )
        guard_stdin_dash_tty(args.input_file)
        if args.input_file and args.input_file != "-" and not Path(args.input_file).exists():
            print_error(f"Error: Input file not found: '{args.input_file}'")
            sys.exit(1)
        if not args.input_file and not args.json_payload:
            if not _has_update_target_selector(args):
                print_error(
                    "Error: Provide --file, --json, or a target selector (--record-id, "
                    "--record-oid, --visit-id, --interval-name, or --interval-id) to update records."
                )
                sys.exit(1)
            if not _has_update_mutation_input(args):
                print_error(
                    "Error: Provide -d/--data, --record-status, or --reason-for-change to update records."
                )
                sys.exit(1)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            update_records_cmd(args)

    update_parser.set_defaults(func=_dispatch_update_records)

    # 4. records import
    import_parser = sub.add_parser(
        "import",
        help="Import study records from a CSV or JSON file (or '-' for stdin).",
    )
    add_study_key_args(import_parser)
    import_parser.add_argument(
        "file",
        help="Path to CSV or JSON file to import ('-' for stdin JSON or CSV).",
    )
    add_write_common_args(import_parser)

    @with_sdk
    def import_records_cmd(sdk: ImednetSDK, args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        input_path = Path(args.file)

        def _run() -> BatchSubmissionSummary:
            resolved_schema = resolve_cli_schema(
                sdk,
                args.study_key,
                args.schema_source,
                dry_run=args.dry_run,
            )
            if args.file == "-" or input_path.suffix.lower() == ".json":
                payload = load_records_from_path_or_stdin(args.file, schema=resolved_schema)
                return submit_cli_records_batch(
                    sdk,
                    args,
                    payload,
                    action_label="record import",
                    schema=resolved_schema,
                )
            email_notify = parse_email_notify(args.email_notify)
            with write_progress_context(
                "record import",
                args.study_key,
                no_interactive=no_interactive,
                output_format=args.output_format,
                dry_run=args.dry_run,
            ) as on_progress:
                raw_summary = import_records_csv(
                    sdk,
                    args.study_key,
                    input_path,
                    email_notify=email_notify,
                    schema=resolved_schema,
                    chunk_size=args.chunk_size,
                    wait_for_completion=args.wait,
                    timeout=args.timeout,
                    interval=args.interval,
                    on_progress=on_progress,
                    dry_run=args.dry_run,
                    collect_errors=True,
                )
            return coerce_batch_summary(
                raw_summary,
                study_key=args.study_key,
                total_records=getattr(raw_summary, "total_records", None) or 0,
                dry_run=args.dry_run,
            )

        execute_cli_write(
            _run,
            study_key=args.study_key,
            resource="records_import",
            output_format=args.output_format,
            dry_run=args.dry_run,
        )

    def _dispatch_import_records(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(import_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        validate_write_numeric_flags(
            chunk_size=args.chunk_size,
            timeout=args.timeout,
            interval=args.interval,
        )
        guard_stdin_dash_tty(args.file)
        if args.file != "-" and not Path(args.file).exists():
            print_error(f"Error: Input file not found: '{args.file}'")
            sys.exit(1)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            import_records_cmd(args)

    import_parser.set_defaults(func=_dispatch_import_records)
