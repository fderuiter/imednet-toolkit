"""CLI commands for managing study subjects."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ...errors import ValidationError
from ...sdk import ImednetSDK
from ...utils.enrollment import (
    build_site_validation_hook,
    filter_existing_subject_registrations,
    normalize_subject_registration_inputs,
)
from ...validation.cache import RecordValidationIssue
from ..decorators import validate_cli_path_segment, with_sdk
from ..utils import print_error, register_list_command, simulation_mode_context
from ..utils.args import (
    add_study_key_args,
    resolve_study_key,
)
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


def _load_cli_subject_inputs(
    args: argparse.Namespace,
    *,
    schema: Any = None,
    study_key: str | None = None,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> list[dict[str, Any]]:
    """Resolve CLI subject registration inputs from ``--file``, ``--json``, or inline flags."""
    if args.input_file and args.json_payload:
        raise ValueError("Cannot combine --file and --json.")

    effective_study_key = study_key or getattr(args, "study_key", None)
    raw_records: list[dict[str, Any]]
    if args.input_file:
        raw_records = load_records_from_path_or_stdin(
            args.input_file,
            schema=schema,
            study_key=effective_study_key,
            default_form_key=args.form_key,
            collect_errors=collect_errors,
            issues=issues,
        )
    elif args.json_payload:
        raw_records = parse_json_records_text(
            args.json_payload,
            source_label="--json",
            default_form_key=args.form_key,
            collect_errors=collect_errors,
            issues=issues,
        )
    else:
        if not args.form_key or not args.site_name:
            raise ValueError(
                "Provide --file, --json, or both --form-key and --site-name to register a subject."
            )
        raw_records = [{"formKey": args.form_key, "siteName": args.site_name}]

    invalid_site_rows = {
        iss.record_index
        for iss in (issues or ())
        if iss.rule_id == "invalid_identifier" and iss.variable_name in {"siteId", "site_id"}
    }

    merged: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_records):
        rec = dict(item)
        has_form = bool(
            rec.get("formKey")
            or rec.get("form_key")
            or rec.get("formId") is not None
            or rec.get("form_id") is not None
        )
        if args.form_key and not has_form:
            rec["formKey"] = args.form_key

        has_site = bool(
            rec.get("siteName")
            or rec.get("site_name")
            or rec.get("siteId") is not None
            or rec.get("site_id") is not None
        )
        if args.site_name and not has_site and idx not in invalid_site_rows:
            rec["siteName"] = args.site_name

        if args.subject_key and (
            len(raw_records) == 1 or not (rec.get("subjectKey") or rec.get("subject_key"))
        ):
            rec.pop("subject_key", None)
            rec["subjectKey"] = args.subject_key

        if args.data_pairs:
            raw_fk = rec.get("formKey") or rec.get("form_key")
            rec_form_key = str(raw_fk) if raw_fk else None
            raw_fid = rec.get("formId") if rec.get("formId") is not None else rec.get("form_id")
            rec_form_id = (
                raw_fid if isinstance(raw_fid, int) and not isinstance(raw_fid, bool) else None
            )
            inline_data = parse_key_value_pairs(
                args.data_pairs,
                schema=schema,
                study_key=effective_study_key,
                form_key=rec_form_key,
                form_id=rec_form_id,
                collect_errors=collect_errors,
                issues=issues,
                record_index=idx,
            )
            existing_data = dict(rec.get("data") or {})
            existing_data.update(inline_data)
            rec["data"] = existing_data
        elif "data" not in rec:
            rec["data"] = {}

        merged.append(rec)

    return merged


def _subject_view_model(s: Any) -> dict[str, Any]:
    keywords_str = ", ".join(k.keyword_name for k in s.keywords)
    return {
        "subject_key": s.subject_key,
        "status": s.subject_status,
        "site": s.site_name,
        "enrollment_date": s.enrollment_start_date,
        "keywords": keywords_str,
    }


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("subjects", help="Manage subjects within a study.")
    sub = parser.add_subparsers(dest="command")

    # 1. subjects list
    register_list_command(
        parser,
        "subjects",
        "subjects",
        requires_study_key=True,
        list_help="List subjects for a specific study.",
        empty_msg="No subjects found matching the criteria.",
        row_mapper=_subject_view_model,
        filter_dest="subject_filter",
        filter_help=(
            "Filter criteria (e.g., 'subject_status=Screened' or 'subjectStatus==Screened'). "
            "Repeat for multiple filters."
        ),
    )

    # 2. subjects register
    register_parser = sub.add_parser(
        "register",
        help="Register one or more subjects in a study.",
    )
    add_study_key_args(register_parser)
    register_parser.add_argument(
        "-f",
        "--file",
        dest="input_file",
        default=None,
        help="Path to CSV or JSON file of subject registration requests ('-' for stdin JSON).",
    )
    register_parser.add_argument(
        "--json",
        dest="json_payload",
        default=None,
        help="Inline JSON object or array of subject registration requests.",
    )
    register_parser.add_argument(
        "--form-key",
        default=None,
        help="Registration form key (formKey) for inline registration or file default.",
    )
    register_parser.add_argument(
        "--site-name",
        default=None,
        help="Enrollment site name (siteName) for inline registration or file default.",
    )
    register_parser.add_argument(
        "--subject-key",
        default=None,
        help="Subject key (subjectKey) for inline registration.",
    )
    register_parser.add_argument(
        "-d",
        "--data",
        action="append",
        dest="data_pairs",
        default=None,
        help="Registration variable assignment in KEY=VALUE form. Repeat for multiple variables.",
    )
    register_parser.add_argument(
        "--skip-existing",
        "--filter-enrolled",
        dest="skip_existing",
        action="store_true",
        default=False,
        help="Skip subjects whose key or OID is already enrolled in the study.",
    )
    register_parser.add_argument(
        "--subject-key-field",
        default=None,
        help="Optional variable name inside record data holding the subject key.",
    )
    add_write_common_args(register_parser)

    @with_sdk
    def register_subjects_cmd(sdk: ImednetSDK, args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        collect_errors = bool(args.dry_run)
        load_issues: list[RecordValidationIssue] = []
        norm_issues: list[RecordValidationIssue] = []
        try:
            resolved_schema = resolve_cli_schema(sdk, args.study_key, args.schema_source)
            raw_inputs = _load_cli_subject_inputs(
                args,
                schema=resolved_schema,
                study_key=args.study_key,
                collect_errors=collect_errors,
                issues=load_issues,
            )
            invalid_loaded_sites = {
                iss.record_index
                for iss in load_issues
                if iss.rule_id == "invalid_identifier"
                and iss.variable_name in {"siteId", "site_id"}
            }
            normalized_pairs = normalize_subject_registration_inputs(
                raw_inputs,
                default_form_key=args.form_key,
                default_site_name=None if invalid_loaded_sites else args.site_name,
                schema=resolved_schema,
                study_key=args.study_key,
                collect_errors=collect_errors,
                issues=norm_issues,
            )
            all_issues: list[RecordValidationIssue] = list(load_issues)
            seen_masked = {
                (iss.record_index, iss.variable_name)
                for iss in all_issues
                if iss.rule_id == "masked_phi"
            }
            for iss in norm_issues:
                if (
                    iss.rule_id == "masked_phi"
                    and (iss.record_index, iss.variable_name) in seen_masked
                ):
                    continue
                all_issues.append(iss)

            sites = list(sdk.sites.list(args.study_key))
            existing_subjects = (
                list(sdk.subjects.list(args.study_key)) if args.skip_existing else None
            )
            subjects_data, skipped_count = filter_existing_subject_registrations(
                normalized_pairs,
                existing_subjects,
                should_filter=args.skip_existing,
                subject_key_field=args.subject_key_field,
                issues=all_issues,
            )
            invalid_site_indices = {
                iss.record_index
                for iss in all_issues
                if iss.rule_id == "invalid_identifier"
                and iss.variable_name in {"siteId", "site_id"}
            }
            site_hook = build_site_validation_hook(sites, skip_indices=invalid_site_indices)

            email_notify = parse_email_notify(args.email_notify)
            with write_progress_context(
                "subject registration",
                args.study_key,
                no_interactive=no_interactive,
                output_format=args.output_format,
            ) as on_progress:
                raw_summary = sdk.records.create_batch(
                    args.study_key,
                    subjects_data,
                    email_notify=email_notify,
                    schema=resolved_schema,
                    default_form_key=args.form_key,
                    validate_batch_fn=site_hook,
                    precollected_issues=all_issues,
                    skipped_count=skipped_count,
                    chunk_size=args.chunk_size,
                    wait_for_completion=args.wait,
                    timeout=args.timeout,
                    interval=args.interval,
                    on_progress=on_progress,
                    dry_run=args.dry_run,
                    collect_errors=collect_errors,
                )
        except (ValidationError, ValueError) as exc:
            exit_cli_validation_error(exc)

        summary = coerce_batch_summary(
            raw_summary,
            study_key=args.study_key,
            total_records=len(subjects_data),
            dry_run=args.dry_run,
            skipped_count=skipped_count,
        )
        summary.skipped_count = skipped_count
        render_write_summary(
            summary,
            resource="subjects_register",
            output_format=args.output_format,
        )

    def _dispatch_register_subjects(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(register_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        validate_write_numeric_flags(
            chunk_size=args.chunk_size,
            timeout=args.timeout,
            interval=args.interval,
        )
        if args.input_file and args.input_file != "-" and not Path(args.input_file).exists():
            print_error(f"Error: Input file not found: '{args.input_file}'")
            sys.exit(1)
        if (
            not args.input_file
            and not args.json_payload
            and (not args.form_key or not args.site_name)
        ):
            print_error(
                "Error: Provide --file, --json, or both --form-key and --site-name to register subjects."
            )
            sys.exit(1)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            register_subjects_cmd(args)

    register_parser.set_defaults(func=_dispatch_register_subjects)
