"""CLI commands for managing study records."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ...endpoints.records import RecordsEndpoint
from ...errors import ValidationError
from ...models.records import Record
from ...sdk import ImednetSDK
from ...utils.job_poller import JobFailedError
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
from ..utils.export import export_list_to_file, resolve_records_export_target
from ..utils.write_helpers import (
    add_write_common_args,
    coerce_batch_summary,
    handle_cli_write_validation_error,
    handle_job_failed_error,
    load_records_from_path_or_stdin,
    parse_email_notify,
    parse_json_records_text,
    parse_key_value_pairs,
    render_write_summary,
    resolve_cli_schema,
    validate_record_file_arg,
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
    if args.input_file and args.json_payload:
        raise ValueError("Cannot combine --file and --json.")

    create_only = bool(getattr(args, "create_only", False))
    record_id = getattr(args, "record_id", None)
    record_oid = getattr(args, "record_oid", None)
    if create_only and (record_id is not None or record_oid):
        raise ValueError("Cannot combine --create-only with --record-id or --record-oid.")

    study_key = getattr(args, "study_key", None)
    inline_data = parse_key_value_pairs(
        args.data_pairs,
        schema=schema,
        form_key=args.form_key,
        form_id=args.form_id,
        study_key=study_key,
    )
    records: list[dict[str, Any]]
    if args.input_file:
        records = load_records_from_path_or_stdin(
            args.input_file,
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    elif args.json_payload:
        records = parse_json_records_text(
            args.json_payload,
            source_label="--json",
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    elif is_update:
        if not _has_update_target_selector(args):
            raise ValueError(
                "Provide --file, --json, or a target selector (--record-id, --record-oid, "
                "--visit-id, --interval-name, or --interval-id) to update a record."
            )
        if not _has_update_mutation_input(args):
            raise ValueError(
                "Provide -d/--data, --record-status, or --reason-for-change to update a record."
            )
        single_update: dict[str, Any] = {}
        if inline_data:
            single_update["data"] = dict(inline_data)
        records = [single_update]
    else:
        has_target = bool(
            args.form_key or args.form_id is not None or record_id is not None or record_oid
        )
        if not has_target:
            raise ValueError(
                "Provide --file, --json, or at least --form-key / --form-id / --record-id / --record-oid to create a record."
            )
        records = [{"data": dict(inline_data)}]

    visit_id = getattr(args, "visit_id", None)
    record_status = getattr(args, "record_status", None)
    reason_for_change = getattr(args, "reason_for_change", None)

    resolved: list[dict[str, Any]] = []
    for item in records:
        rec = dict(item)
        if not create_only:
            if (
                record_id is not None
                and rec.get("recordId") is None
                and rec.get("record_id") is None
            ):
                rec["recordId"] = record_id
            elif record_oid and not (rec.get("recordOid") or rec.get("record_oid")):
                rec["recordOid"] = record_oid
            if visit_id is not None and rec.get("visitId") is None and rec.get("visit_id") is None:
                rec["visitId"] = visit_id
        else:
            rec.pop("recordId", None)
            rec.pop("record_id", None)
            rec.pop("recordOid", None)
            rec.pop("record_oid", None)

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

        if record_status:
            rec["recordStatus"] = record_status
        if reason_for_change:
            rec["reasonForChange"] = reason_for_change

        if args.data_pairs and (args.input_file or args.json_payload):
            rec_fk = args.form_key or rec.get("formKey") or rec.get("form_key")
            raw_rec_fid = (
                args.form_id
                if args.form_id is not None
                else (rec.get("formId") if rec.get("formId") is not None else rec.get("form_id"))
            )
            rec_fid = (
                int(raw_rec_fid)
                if isinstance(raw_rec_fid, int) and not isinstance(raw_rec_fid, bool)
                else None
            )
            row_inline_data = parse_key_value_pairs(
                args.data_pairs,
                schema=schema,
                form_key=str(rec_fk) if rec_fk else None,
                form_id=rec_fid,
                study_key=study_key,
            )
            existing_data = dict(rec.get("data") or rec.get("recordData") or {})
            existing_data.update(row_inline_data)
            rec["data"] = existing_data

        resolved.append(rec)

    ensure_no_masked_sentinel(resolved)
    return resolved


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

    def _add_record_target_args(
        target_parser: argparse.ArgumentParser,
        *,
        allow_create_only: bool,
    ) -> None:
        add_study_key_args(target_parser)
        target_parser.add_argument(
            "-f",
            "--file",
            dest="input_file",
            default=None,
            help=(
                "Path to .csv, .json, .jsonl, .ndjson, .parquet, or .xlsx file "
                "('-' for stdin JSON)."
            ),
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
        if allow_create_only:
            target_parser.add_argument(
                "--create-only",
                action="store_true",
                default=False,
                help="Strip recordId/recordOid and force creation of new records.",
            )
        add_write_common_args(target_parser)

    def _register_records_write_subcommand(
        target_parser: argparse.ArgumentParser,
        *,
        is_update: bool,
        resource: str,
        progress_label: str,
    ) -> None:
        @with_sdk
        def _records_write_cmd(sdk: ImednetSDK, args: argparse.Namespace) -> None:
            no_interactive = getattr(args, "no_interactive", False)
            create_only = bool(getattr(args, "create_only", False))
            payload: list[dict[str, Any]] = []
            try:
                resolved_schema = resolve_cli_schema(
                    sdk,
                    args.study_key,
                    args.schema_source,
                    dry_run=args.dry_run,
                )
                payload = _assemble_records_payload(
                    args,
                    schema=resolved_schema,
                    is_update=is_update,
                )
                email_notify = parse_email_notify(args.email_notify)
                with write_progress_context(
                    progress_label,
                    args.study_key,
                    no_interactive=no_interactive,
                    output_format=args.output_format,
                ) as on_progress:
                    raw_summary = sdk.records.create_batch(
                        args.study_key,
                        payload,
                        email_notify=email_notify,
                        schema=resolved_schema,
                        create_only=create_only,
                        chunk_size=args.chunk_size,
                        wait_for_completion=args.wait,
                        timeout=args.timeout,
                        interval=args.interval,
                        on_progress=on_progress,
                        dry_run=args.dry_run,
                        collect_errors=True,
                    )
            except JobFailedError as exc:
                handle_job_failed_error(
                    exc,
                    study_key=args.study_key,
                    total_records=len(payload),
                    resource=resource,
                    output_format=args.output_format,
                )
                return
            except (ValidationError, ValueError) as exc:
                handle_cli_write_validation_error(
                    exc,
                    study_key=args.study_key,
                    resource=resource,
                    dry_run=args.dry_run,
                    output_format=args.output_format,
                )
                return

            summary = coerce_batch_summary(
                raw_summary,
                study_key=args.study_key,
                total_records=len(payload),
                dry_run=args.dry_run,
            )
            render_write_summary(
                summary,
                resource=resource,
                output_format=args.output_format,
            )

        def _dispatch(args: argparse.Namespace) -> None:
            study_key = resolve_study_key(target_parser, args, required=True)
            assert study_key is not None
            validate_cli_path_segment(study_key, name="study_key")
            validate_write_numeric_flags(
                chunk_size=args.chunk_size,
                timeout=args.timeout,
                interval=args.interval,
            )
            if getattr(args, "create_only", False) and (
                args.record_id is not None or args.record_oid
            ):
                print_error("Error: Cannot combine --create-only with --record-id or --record-oid.")
                sys.exit(1)
            if args.input_file:
                try:
                    validate_record_file_arg(args.input_file)
                except ValueError as exc:
                    print_error(f"Error: {exc}")
                    sys.exit(1)
            if not args.input_file and not args.json_payload:
                if is_update:
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
                elif (
                    not args.form_key
                    and args.form_id is None
                    and args.record_id is None
                    and not args.record_oid
                ):
                    print_error(
                        "Error: Provide --file, --json, or at least --form-key / --form-id "
                        "/ --record-id / --record-oid to create records."
                    )
                    sys.exit(1)
            with simulation_mode_context(bool(getattr(args, "simulation", False))):
                _records_write_cmd(args)

        target_parser.set_defaults(func=_dispatch)

    # 2. records create
    create_parser = sub.add_parser(
        "create",
        help="Create or update one or more records in a study.",
    )
    _add_record_target_args(create_parser, allow_create_only=True)
    _register_records_write_subcommand(
        create_parser,
        is_update=False,
        resource="records_create",
        progress_label="record submission",
    )

    # 3. records update
    update_parser = sub.add_parser(
        "update",
        help="Update one or more existing records in a study.",
    )
    _add_record_target_args(update_parser, allow_create_only=False)
    _register_records_write_subcommand(
        update_parser,
        is_update=True,
        resource="records_update",
        progress_label="record update",
    )

    # 4. records import
    import_parser = sub.add_parser(
        "import",
        help="Import study records from a CSV, JSON, JSONL/NDJSON, Parquet, or Excel file.",
    )
    add_study_key_args(import_parser)
    import_parser.add_argument(
        "file",
        help="Path to .csv, .json, .jsonl, .ndjson, .parquet, .xlsx file, or '-' for stdin.",
    )
    import_parser.add_argument(
        "--create-only",
        action="store_true",
        default=False,
        help="Strip recordId/recordOid and force creation of new records.",
    )
    add_write_common_args(import_parser)

    @with_sdk
    def import_records_cmd(sdk: ImednetSDK, args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        payload: list[dict[str, Any]] = []
        try:
            resolved_schema = resolve_cli_schema(
                sdk,
                args.study_key,
                args.schema_source,
                dry_run=args.dry_run,
            )
            email_notify = parse_email_notify(args.email_notify)
            payload = load_records_from_path_or_stdin(
                args.file,
                schema=resolved_schema,
                create_only=args.create_only,
                study_key=args.study_key,
            )
            with write_progress_context(
                "record import",
                args.study_key,
                no_interactive=no_interactive,
                output_format=args.output_format,
            ) as on_progress:
                raw_summary = sdk.records.create_batch(
                    args.study_key,
                    payload,
                    email_notify=email_notify,
                    schema=resolved_schema,
                    create_only=args.create_only,
                    chunk_size=args.chunk_size,
                    wait_for_completion=args.wait,
                    timeout=args.timeout,
                    interval=args.interval,
                    on_progress=on_progress,
                    dry_run=args.dry_run,
                    collect_errors=True,
                )
        except JobFailedError as exc:
            handle_job_failed_error(
                exc,
                study_key=args.study_key,
                total_records=len(payload),
                resource="records_import",
                output_format=args.output_format,
            )
            return
        except (ValidationError, ValueError) as exc:
            handle_cli_write_validation_error(
                exc,
                study_key=args.study_key,
                resource="records_import",
                dry_run=args.dry_run,
                output_format=args.output_format,
            )
            return

        summary = coerce_batch_summary(
            raw_summary,
            study_key=args.study_key,
            total_records=len(payload),
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
        try:
            validate_record_file_arg(args.file)
        except ValueError as exc:
            print_error(f"Error: {exc}")
            sys.exit(1)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            import_records_cmd(args)

    import_parser.set_defaults(func=_dispatch_import_records)
