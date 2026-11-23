"""CLI commands for managing study records."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...endpoints.records import RecordsEndpoint
from ...errors import ValidationError
from ...models.records import Record
from ...sdk import ImednetSDK
from ...utils.pandas import import_records_csv
from ...utils.tabular import ensure_no_masked_sentinel
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
from ..utils.export import export_list_to_file
from ..utils.write_helpers import (
    add_write_common_args,
    coerce_batch_summary,
    exit_cli_validation_error,
    load_records_from_path_or_stdin,
    parse_email_notify,
    parse_json_records_text,
    parse_key_value_pairs,
    render_write_summary,
    resolve_cli_schema,
    validate_write_numeric_flags,
    write_progress_context,
)


def _assemble_create_records_payload(
    args: argparse.Namespace,
    *,
    schema: Any = None,
) -> list[dict[str, Any]]:
    """Build record creation payload(s) from ``--file``, ``--json``, or inline flags."""
    if args.input_file and args.json_payload:
        raise ValueError("Cannot combine --file and --json.")

    inline_data = parse_key_value_pairs(args.data_pairs)
    records: list[dict[str, Any]]
    if args.input_file:
        records = load_records_from_path_or_stdin(args.input_file, schema=schema)
    elif args.json_payload:
        records = parse_json_records_text(args.json_payload, source_label="--json")
    else:
        if not args.form_key and args.form_id is None:
            raise ValueError(
                "Provide --file, --json, or at least --form-key / --form-id to create a record."
            )
        single: dict[str, Any] = {"data": dict(inline_data)}
        records = [single]

    resolved: list[dict[str, Any]] = []
    for item in records:
        rec = dict(item)
        if args.form_key and not (rec.get("formKey") or rec.get("form_key")):
            rec["formKey"] = args.form_key
        elif args.form_id is not None and rec.get("formId") is None and rec.get("form_id") is None:
            rec["formId"] = args.form_id

        if args.subject_key and not (rec.get("subjectKey") or rec.get("subject_key")):
            rec["subjectKey"] = args.subject_key
        elif args.subject_oid and not (rec.get("subjectOid") or rec.get("subject_oid")):
            rec["subjectOid"] = args.subject_oid
        elif (
            args.subject_id is not None
            and rec.get("subjectId") is None
            and rec.get("subject_id") is None
        ):
            rec["subjectId"] = args.subject_id

        if args.site_name and not (rec.get("siteName") or rec.get("site_name")):
            rec["siteName"] = args.site_name
        elif args.site_id is not None and rec.get("siteId") is None and rec.get("site_id") is None:
            rec["siteId"] = args.site_id

        if args.interval_name and not (rec.get("intervalName") or rec.get("interval_name")):
            rec["intervalName"] = args.interval_name
        elif (
            args.interval_id is not None
            and rec.get("intervalId") is None
            and rec.get("interval_id") is None
        ):
            rec["intervalId"] = args.interval_id

        if inline_data and (args.input_file or args.json_payload):
            existing_data = dict(rec.get("data") or {})
            existing_data.update(inline_data)
            rec["data"] = existing_data

        resolved.append(rec)

    ensure_no_masked_sentinel(resolved)
    return resolved


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
        help="Deprecated: save records to a file (json or csv); prefer --format json or 'imednet export'.",
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
        if output and output.lower() not in {"json", "csv"}:
            print_error("Invalid output format. Use 'json' or 'csv'.")
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
            records_loader: Callable[[], Any] | None = None
            if load_remaining is not None:
                records_loader = load_remaining.map_batches(
                    lambda more: ([_record_view_model(r) for r in more], more)
                )

            display_list(
                view_models,
                "records",
                "No records found.",
                details=records,
                truncated=truncated,
                interactive=False if no_interactive else None,
                output_format=resolved_format,
                on_load_all=records_loader,
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
                _prepared_query=prepared,
            )

    list_parser.set_defaults(func=_dispatch_list_records)

    # 2. records create
    create_parser = sub.add_parser(
        "create",
        help="Create or update one or more records in a study.",
    )
    add_study_key_args(create_parser)
    create_parser.add_argument(
        "-f",
        "--file",
        dest="input_file",
        default=None,
        help="Path to JSON or CSV file of record payloads ('-' for stdin JSON).",
    )
    create_parser.add_argument(
        "--json",
        dest="json_payload",
        default=None,
        help="Inline JSON object or array of record payloads.",
    )
    create_parser.add_argument("--form-key", default=None, help="Target form key (formKey).")
    create_parser.add_argument("--form-id", type=int, default=None, help="Target form ID (formId).")
    create_parser.add_argument(
        "--subject-key", default=None, help="Target subject key (subjectKey)."
    )
    create_parser.add_argument(
        "--subject-oid", default=None, help="Target subject OID (subjectOid)."
    )
    create_parser.add_argument(
        "--subject-id", type=int, default=None, help="Target subject ID (subjectId)."
    )
    create_parser.add_argument("--site-name", default=None, help="Target site name (siteName).")
    create_parser.add_argument("--site-id", type=int, default=None, help="Target site ID (siteId).")
    create_parser.add_argument(
        "--interval-name", default=None, help="Target interval name (intervalName)."
    )
    create_parser.add_argument(
        "--interval-id", type=int, default=None, help="Target interval ID (intervalId)."
    )
    create_parser.add_argument(
        "-d",
        "--data",
        action="append",
        dest="data_pairs",
        default=None,
        help="Clinical variable assignment in KEY=VALUE form. Repeat for multiple variables.",
    )
    add_write_common_args(create_parser)

    @with_sdk
    def create_records_cmd(sdk: ImednetSDK, args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        try:
            resolved_schema = resolve_cli_schema(sdk, args.study_key, args.schema_source)
            payload = _assemble_create_records_payload(args, schema=resolved_schema)
            email_notify = parse_email_notify(args.email_notify)
            with write_progress_context(
                "record submission",
                args.study_key,
                no_interactive=no_interactive,
                output_format=args.output_format,
            ) as on_progress:
                raw_summary = sdk.records.create_batch(
                    args.study_key,
                    payload,
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
        except (ValidationError, ValueError) as exc:
            exit_cli_validation_error(exc)

        summary = coerce_batch_summary(
            raw_summary,
            study_key=args.study_key,
            total_records=len(payload),
            dry_run=args.dry_run,
        )
        render_write_summary(
            summary,
            resource="records_create",
            output_format=args.output_format,
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

    # 3. records import
    import_parser = sub.add_parser(
        "import",
        help="Import study records from a CSV or JSON file.",
    )
    add_study_key_args(import_parser)
    import_parser.add_argument("file", help="Path to CSV or JSON file to import.")
    add_write_common_args(import_parser)

    @with_sdk
    def import_records_cmd(sdk: ImednetSDK, args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        input_path = Path(args.file)
        try:
            resolved_schema = resolve_cli_schema(sdk, args.study_key, args.schema_source)
            email_notify = parse_email_notify(args.email_notify)
            with write_progress_context(
                "record import",
                args.study_key,
                no_interactive=no_interactive,
                output_format=args.output_format,
            ) as on_progress:
                if input_path.suffix.lower() == ".json":
                    payload = load_records_from_path_or_stdin(args.file, schema=resolved_schema)
                    raw_summary = sdk.records.create_batch(
                        args.study_key,
                        payload,
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
                    total_count = len(payload)
                else:
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
                    total_count = getattr(raw_summary, "total_records", 0)
        except (ValidationError, ValueError) as exc:
            exit_cli_validation_error(exc)

        summary = coerce_batch_summary(
            raw_summary,
            study_key=args.study_key,
            total_records=total_count,
            dry_run=args.dry_run,
        )
        render_write_summary(
            summary,
            resource="records_import",
            output_format=args.output_format,
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
        if not Path(args.file).exists():
            print_error(f"Error: Input file not found: '{args.file}'")
            sys.exit(1)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            import_records_cmd(args)

    import_parser.set_defaults(func=_dispatch_import_records)
