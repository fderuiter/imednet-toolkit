"""Dedicated workflow CLI subcommands for audit-trail extraction and DuckDB revision ingestion."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from imednet.spi.cli import (
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    display_list,
    emit_json,
    export_list_to_file,
    fetching_status,
    is_json_output_mode,
    parse_filter_args,
    print_error,
    redact_cli_secrets,
    resolve_output_format,
    resolve_records_export_target,
    resolve_study_key,
    simulation_mode_context,
    validate_cli_path_segment,
    with_sdk,
)
from imednet.spi.utils import (
    REVISION_DATE_ATTRIBUTES,
    build_study_sensitivity_registry,
    normalize_utc_timestamp_bound,
    redact_sensitive_text,
    sanitize_csv_formula,
    sanitize_diagnostic_text,
)

if TYPE_CHECKING:
    from imednet import ImednetSDK


def setup_audit_parsers(
    sub: argparse._SubParsersAction[Any],
    *,
    resolve_attr: Callable[[str], Any],
    resolve_custom_phi_fields: Callable[[str | None], set[str]],
    mask_workflow_item: Callable[..., Any],
    emit_line: Callable[..., None],
) -> None:
    """Register ``extract-audit-trail`` and ``ingest-revisions`` under ``imednet workflows``."""
    audit_parser = sub.add_parser(
        "extract-audit-trail",
        help="Extract date-bounded RecordRevision audit trails with optional 21 CFR Part 11 chain checks.",
    )
    add_study_key_args(audit_parser)
    audit_parser.add_argument(
        "--start-date",
        dest="start_date",
        default=None,
        help="Inclusive UTC lower bound on dateCreated (ISO-8601 date or timestamp).",
    )
    audit_parser.add_argument(
        "--end-date",
        dest="end_date",
        default=None,
        help="Inclusive UTC upper bound on dateCreated (ISO-8601 date or timestamp).",
    )
    audit_parser.add_argument(
        "-f",
        "--filter",
        action="append",
        help=(
            "Filter revisions by attribute (e.g., 'user==sim_user', 'recordId==601'). "
            "Repeatable; dateCreated and lastUpdated support dual lower/upper bounds."
        ),
    )
    audit_parser.add_argument(
        "--sort",
        default=None,
        help="Sort property and direction (e.g., 'recordRevision,asc').",
    )
    audit_parser.add_argument(
        "--validate-chain",
        dest="validate_chain",
        action="store_true",
        default=False,
        help="Validate monotonic 21 CFR Part 11 revision chain invariants.",
    )
    audit_parser.add_argument(
        "--allow-partial",
        dest="allow_partial",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow partial/sliced revision chains when --validate-chain is enabled "
            "(auto-enabled when date bounds or attribute filters are supplied)."
        ),
    )
    audit_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI fields with '***MASKED***' (default: enabled).",
    )
    audit_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Export audit trail directly to file ('csv', 'json', or a .csv/.json path).",
    )
    add_output_format(audit_parser)
    add_simulation_arg(audit_parser)

    @with_sdk
    def extract_audit_trail_cmd(
        sdk: ImednetSDK,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        filters: list[str] | None = None,
        sort: str | None = None,
        validate_chain: bool = False,
        allow_partial: bool | None = None,
        mask_phi: bool = True,
        output: str | None = None,
        output_format: str | None = None,
        no_interactive: bool = False,
    ) -> None:
        from imednet.spi.models import RecordRevision, validate_record_revision_chain

        resolved_format = resolve_output_format(output_format)
        study_reg = build_study_sensitivity_registry(resolve_custom_phi_fields(study_key))
        parsed_filters = parse_filter_args(
            filters,
            model=RecordRevision,
            disallow_study_key=True,
            multi_bound_attributes=REVISION_DATE_ATTRIBUTES,
        )
        effective_sort = (
            sort if sort is not None else ("recordRevision,asc" if validate_chain else None)
        )
        sort_kwargs: dict[str, Any] = {"sort": effective_sort} if effective_sort else {}

        workflow = resolve_attr("DataExtractionWorkflow")(sdk)
        if resolved_format == "json" or is_json_output_mode() or output:
            revisions = workflow.extract_audit_trail(
                study_key=study_key,
                start_date=start_date,
                end_date=end_date,
                user_filter=parsed_filters,
                **sort_kwargs,
            )
        else:
            with fetching_status("audit trail", study_key):
                revisions = workflow.extract_audit_trail(
                    study_key=study_key,
                    start_date=start_date,
                    end_date=end_date,
                    user_filter=parsed_filters,
                    **sort_kwargs,
                )

        if validate_chain:
            effective_partial = (
                allow_partial
                if allow_partial is not None
                else bool(start_date or end_date or parsed_filters)
            )
            try:
                validate_record_revision_chain(revisions, allow_partial=effective_partial)
            except ValueError as exc:
                sanitized_err = redact_cli_secrets(
                    sanitize_diagnostic_text(
                        redact_sensitive_text(str(exc), registry=study_reg),
                        registry=study_reg,
                    )
                )
                print_error(f"Chain validation failed: {sanitized_err}")
                sys.exit(1)

        if output:
            export_list_to_file(
                revisions,
                "audit_trail",
                output,
                study_key=study_key,
                mask_phi=mask_phi,
            )
            return

        masked_revisions = mask_workflow_item(revisions, study_key=study_key, mask_phi=mask_phi)
        if resolved_format == "json":
            display_list(masked_revisions, "record_revisions", output_format="json")
            return

        display_list(
            masked_revisions,
            "audit trail",
            "No record revisions found matching the criteria.",
            fields=[
                "record_revision_id",
                "record_id",
                "subject_key",
                "form_key",
                "record_revision",
                "data_revision",
                "user",
                "reason_for_change",
                "date_created",
            ],
            details=masked_revisions,
            interactive=False if no_interactive else None,
            output_format=resolved_format,
        )

    def _handle_extract_audit_trail(args: argparse.Namespace) -> None:
        explicit_format = getattr(args, "output_format", None)
        output_arg = getattr(args, "output", None)
        if output_arg and explicit_format == "json":
            print_error("Error: Cannot combine --output with --format json.")
            sys.exit(1)

        study_key = resolve_study_key(audit_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        study_reg = build_study_sensitivity_registry(resolve_custom_phi_fields(study_key))

        try:
            if args.start_date:
                normalize_utc_timestamp_bound(args.start_date, is_end=False)
            if args.end_date:
                normalize_utc_timestamp_bound(args.end_date, is_end=True)
            if output_arg:
                resolved_out = resolve_records_export_target(output_arg, "audit_trail")
                if resolved_out is None:
                    raise ValueError(
                        f"Invalid output format: '{output_arg}'. Supported: csv, json, "
                        "or explicit .csv/.json file paths."
                    )
        except ValueError as exc:
            sanitized_err = redact_cli_secrets(
                sanitize_diagnostic_text(
                    redact_sensitive_text(str(exc), registry=study_reg),
                    registry=study_reg,
                )
            )
            print_error(f"Error: {sanitized_err}")
            sys.exit(1)

        from imednet.spi.models import RecordRevision

        parse_filter_args(
            args.filter,
            model=RecordRevision,
            disallow_study_key=True,
            multi_bound_attributes=REVISION_DATE_ATTRIBUTES,
        )

        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            extract_audit_trail_cmd(
                study_key=study_key,
                start_date=args.start_date,
                end_date=args.end_date,
                filters=args.filter,
                sort=args.sort,
                validate_chain=bool(args.validate_chain),
                allow_partial=args.allow_partial,
                mask_phi=getattr(args, "mask_phi", True),
                output=output_arg,
                output_format=resolve_output_format(explicit_format),
                no_interactive=getattr(args, "no_interactive", False),
            )

    audit_parser.set_defaults(func=_handle_extract_audit_trail)

    ingest_parser = sub.add_parser(
        "ingest-revisions",
        help="Ingest Study RecordRevision audit logs into DuckDB bronze/silver lineage tables.",
    )
    add_study_key_args(ingest_parser)
    ingest_parser.add_argument(
        "--db-path",
        dest="db_path",
        default=":memory:",
        help="Path to DuckDB database file or ':memory:' (default: :memory:).",
    )
    ingest_parser.add_argument(
        "--mode",
        choices=("append", "replace"),
        default="append",
        help="DuckDB bronze write mode ('append' deduplicates; 'replace' refreshes only this study).",
    )
    ingest_parser.add_argument(
        "--start-date",
        dest="start_date",
        default=None,
        help="Inclusive UTC lower bound on dateCreated (ISO-8601 date or timestamp).",
    )
    ingest_parser.add_argument(
        "--end-date",
        dest="end_date",
        default=None,
        help="Inclusive UTC upper bound on dateCreated (ISO-8601 date or timestamp).",
    )
    ingest_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI fields prior to DuckDB storage and file export (default: enabled).",
    )
    ingest_parser.add_argument(
        "--validate-chain",
        dest="validate_chain",
        action="store_true",
        default=False,
        help="Validate monotonic 21 CFR Part 11 revision chain invariants prior to ingestion.",
    )
    ingest_parser.add_argument(
        "--allow-partial",
        dest="allow_partial",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow partial/sliced revision chains when --validate-chain is enabled "
            "(auto-enabled when date bounds are supplied)."
        ),
    )
    ingest_parser.add_argument(
        "--include-deleted",
        dest="include_deleted",
        action="store_true",
        default=False,
        help="Include deleted=True revisions in the materialized silver_current_state view.",
    )
    ingest_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Export materialized silver_records_history rows for the study to CSV or JSON.",
    )
    add_output_format(ingest_parser)
    add_simulation_arg(ingest_parser)

    @with_sdk
    def ingest_revisions_cmd(
        sdk: ImednetSDK,
        study_key: str,
        db_path: str = ":memory:",
        mode: Literal["append", "replace"] = "append",
        start_date: str | None = None,
        end_date: str | None = None,
        mask_phi: bool = True,
        validate_chain: bool = False,
        allow_partial: bool | None = None,
        include_deleted: bool = False,
        output: str | None = None,
        output_format: str | None = None,
        no_interactive: bool = False,
    ) -> None:
        from imednet.spi.models import validate_record_revision_chain

        resolved_format = resolve_output_format(output_format)
        study_reg = build_study_sensitivity_registry(resolve_custom_phi_fields(study_key))

        if validate_chain:
            effective_partial = (
                allow_partial if allow_partial is not None else bool(start_date or end_date)
            )
            revisions = resolve_attr("DataExtractionWorkflow")(sdk).extract_audit_trail(
                study_key=study_key,
                start_date=start_date,
                end_date=end_date,
                sort="recordRevision,asc",
            )
            try:
                validate_record_revision_chain(revisions, allow_partial=effective_partial)
            except ValueError as exc:
                sanitized_err = redact_cli_secrets(
                    sanitize_diagnostic_text(
                        redact_sensitive_text(str(exc), registry=study_reg),
                        registry=study_reg,
                    )
                )
                print_error(f"Chain validation failed: {sanitized_err}")
                sys.exit(1)

        history_rows: list[dict[str, Any]] = []
        workflow_cls = resolve_attr("DuckDBIngestionWorkflow")
        with workflow_cls(sdk, db_path=db_path, mask_phi=mask_phi) as lakehouse:
            inserted = lakehouse.ingest_revisions(
                study_key,
                start_date=start_date,
                end_date=end_date,
                mode=mode,
                mask_phi=mask_phi,
            )
            lakehouse.build_silver_view(study_key, include_deleted=include_deleted)
            if output:
                conn = getattr(lakehouse, "_connection", None)
                execute_fn = getattr(conn, "execute", None)
                if callable(execute_fn):
                    cursor = execute_fn(
                        "SELECT * FROM silver_records_history WHERE study_key = ? "
                        "ORDER BY record_id ASC NULLS LAST, record_revision ASC NULLS LAST, "
                        "record_revision_id ASC NULLS LAST",
                        [study_key],
                    )
                    desc = getattr(cursor, "description", None)
                    if isinstance(desc, (list, tuple)) and desc:
                        col_names = [str(col[0]) for col in desc]
                        for raw_row in cursor.fetchall():
                            row_dict: dict[str, Any] = {}
                            for col_name, cell in zip(col_names, raw_row, strict=False):
                                row_dict[col_name] = (
                                    cell.isoformat() if isinstance(cell, datetime) else cell
                                )
                            history_rows.append(row_dict)

        if output:
            resolved_target = resolve_records_export_target(output, "revisions_history")
            if resolved_target is None:
                print_error(
                    f"Error: Invalid output format: '{output.lower()}'. Supported: csv, json. "
                    "Explicit .csv and .json file paths are also supported."
                )
                sys.exit(1)
            fmt, target = resolved_target
            sanitized_rows = [
                mask_workflow_item(row, study_key=study_key, mask_phi=mask_phi)
                for row in history_rows
            ]
            target.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "json":
                target.write_text(
                    json.dumps(sanitized_rows, indent=2, default=str),
                    encoding="utf-8",
                )
            else:
                headers: list[str] = []
                seen_cols: set[str] = set()
                for row in sanitized_rows:
                    for key in row:
                        if key not in seen_cols:
                            seen_cols.add(key)
                            headers.append(key)
                with target.open("w", newline="", encoding="utf-8") as fh:
                    if headers:
                        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
                        writer.writeheader()
                        for row in sanitized_rows:
                            safe_row: dict[str, Any] = {}
                            for h in headers:
                                val = row.get(h, "")
                                if val is None:
                                    safe_row[h] = ""
                                elif isinstance(val, str):
                                    safe_row[h] = sanitize_csv_formula(val)
                                else:
                                    safe_row[h] = val
                            writer.writerow(safe_row)
                    else:
                        fh.write("")
            emit_line(f"Saved {len(sanitized_rows)} revisions_history to {target}")
            return

        summary_row = {
            "study_key": study_key,
            "db_path": db_path,
            "mode": mode,
            "inserted": int(inserted),
            "mask_phi": bool(mask_phi),
            "validated_chain": bool(validate_chain),
        }
        if resolved_format == "json":
            emit_json([summary_row], "revisions_ingestion")
            return

        display_list(
            [summary_row],
            "ingested revisions",
            details=[summary_row],
            interactive=False if no_interactive else None,
            output_format=resolved_format,
        )

    def _handle_ingest_revisions(args: argparse.Namespace) -> None:
        explicit_format = getattr(args, "output_format", None)
        output_arg = getattr(args, "output", None)
        if output_arg and explicit_format == "json":
            print_error("Error: Cannot combine --output with --format json.")
            sys.exit(1)

        study_key = resolve_study_key(ingest_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        study_reg = build_study_sensitivity_registry(resolve_custom_phi_fields(study_key))

        try:
            if args.start_date:
                normalize_utc_timestamp_bound(args.start_date, is_end=False)
            if args.end_date:
                normalize_utc_timestamp_bound(args.end_date, is_end=True)
            if output_arg:
                resolved_out = resolve_records_export_target(output_arg, "revisions_history")
                if resolved_out is None:
                    raise ValueError(
                        f"Invalid output format: '{output_arg}'. Supported: csv, json, "
                        "or explicit .csv/.json file paths."
                    )
        except ValueError as exc:
            sanitized_err = redact_cli_secrets(
                sanitize_diagnostic_text(
                    redact_sensitive_text(str(exc), registry=study_reg),
                    registry=study_reg,
                )
            )
            print_error(f"Error: {sanitized_err}")
            sys.exit(1)

        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            ingest_revisions_cmd(
                study_key=study_key,
                db_path=args.db_path,
                mode=args.mode,
                start_date=args.start_date,
                end_date=args.end_date,
                mask_phi=getattr(args, "mask_phi", True),
                validate_chain=bool(args.validate_chain),
                allow_partial=args.allow_partial,
                include_deleted=bool(args.include_deleted),
                output=output_arg,
                output_format=resolve_output_format(explicit_format),
                no_interactive=getattr(args, "no_interactive", False),
            )

    ingest_parser.set_defaults(func=_handle_ingest_revisions)
