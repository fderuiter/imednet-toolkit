"""CLI commands for managing study subjects."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ...endpoints.subjects import SubjectsEndpoint
from ...errors import ValidationError
from ...models.subjects import Subject
from ...sdk import ImednetSDK
from ...utils.enrollment import (
    filter_existing_subject_registrations,
    normalize_subject_registration_inputs,
    validate_subject_registration_sites,
)
from ..decorators import validate_cli_path_segment, with_sdk
from ..utils import STUDY_KEY_ARG, display_list, fetching_status, print_error
from ..utils.args import (
    add_list_query_args,
    add_output_format,
    collect_list_items_with_remainder,
    prepare_list_query,
    resolve_output_format,
)
from ..utils.write_helpers import (
    add_write_common_args,
    coerce_batch_summary,
    exit_cli_validation_error,
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
) -> Any:
    """Resolve CLI subject registration inputs from ``--file``, ``--json``, or inline flags."""
    if args.input_file and args.json_payload:
        raise ValueError("Cannot combine --file and --json.")

    inline_data = parse_key_value_pairs(args.data_pairs)
    if args.input_file:
        if args.input_file == "-":
            return parse_json_records_text(sys.stdin.read(), source_label="stdin")
        return Path(args.input_file)
    if args.json_payload:
        return parse_json_records_text(args.json_payload, source_label="--json")

    if not args.form_key or not args.site_name:
        raise ValueError(
            "Provide --file, --json, or both --form-key and --site-name to register a subject."
        )
    single: dict[str, Any] = {
        "formKey": args.form_key,
        "siteName": args.site_name,
        "data": inline_data,
    }
    if args.subject_key:
        single["subjectKey"] = args.subject_key
    return [single]


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
    list_parser = sub.add_parser("list", help="List subjects for a specific study.")
    list_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    add_list_query_args(
        list_parser,
        filter_dest="subject_filter",
        filter_help="Filter criteria (e.g., 'subject_status=Screened' or 'subjectStatus==Screened'). Repeat for multiple filters.",
    )
    add_output_format(list_parser)

    @with_sdk
    def list_subjects(
        sdk: ImednetSDK,
        study_key: str,
        subject_filter: list[str] | None = None,
        output_format: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        fetch_all: bool = False,
        no_interactive: bool = False,
        _prepared_query: tuple[dict[str, Any], int | None] | None = None,
    ) -> None:
        resolved_format = resolve_output_format(output_format)
        req_kwargs, effective_limit = (
            _prepared_query
            if _prepared_query is not None
            else prepare_list_query(
                filter_args=subject_filter,
                sort=sort,
                limit=limit,
                fetch_all=fetch_all,
                output_format=resolved_format,
                no_interactive=no_interactive,
                model=Subject,
                allowed_attributes=SubjectsEndpoint.FILTER_ATTRIBUTES,
                disallow_study_key=True,
            )
        )

        with fetching_status("subjects", study_key):
            subjects_list, truncated, load_remaining = collect_list_items_with_remainder(
                sdk.subjects.list(study_key, **req_kwargs),
                effective_limit,
            )

        if resolved_format == "json":
            display_list(subjects_list, "subjects", output_format="json")
            return
        view_models = [_subject_view_model(s) for s in subjects_list]

        def _load_more_subjects() -> tuple[list[dict[str, Any]], list[Any]]:
            more = load_remaining() if load_remaining is not None else []
            return [_subject_view_model(s) for s in more], more

        display_list(
            view_models,
            "subjects",
            "No subjects found matching the criteria.",
            details=subjects_list,
            truncated=truncated,
            interactive=False if no_interactive else None,
            output_format=resolved_format,
            on_load_all=_load_more_subjects if load_remaining is not None else None,
        )

    def _dispatch_list_subjects(args: argparse.Namespace) -> None:
        validate_cli_path_segment(args.study_key, name="study_key")
        no_interactive = getattr(args, "no_interactive", False)
        resolved_format = resolve_output_format(args.output_format)
        prepared = prepare_list_query(
            filter_args=args.subject_filter,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
            output_format=resolved_format,
            no_interactive=no_interactive,
            model=Subject,
            allowed_attributes=SubjectsEndpoint.FILTER_ATTRIBUTES,
            disallow_study_key=True,
        )
        list_subjects(
            study_key=args.study_key,
            subject_filter=args.subject_filter,
            output_format=resolved_format,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
            no_interactive=no_interactive,
            _prepared_query=prepared,
        )

    list_parser.set_defaults(func=_dispatch_list_subjects)

    # 2. subjects register
    register_parser = sub.add_parser(
        "register",
        help="Register one or more subjects in a study.",
    )
    register_parser.add_argument("study_key", help=STUDY_KEY_ARG)
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
        try:
            resolved_schema = resolve_cli_schema(sdk, args.study_key, args.schema_source)
            raw_inputs = _load_cli_subject_inputs(args, schema=resolved_schema)
            normalized_pairs = normalize_subject_registration_inputs(
                raw_inputs,
                default_form_key=args.form_key,
                default_site_name=args.site_name,
                schema=resolved_schema,
            )
            sites = list(sdk.sites.list(args.study_key))
            validate_subject_registration_sites(normalized_pairs, sites)

            existing_subjects = (
                list(sdk.subjects.list(args.study_key)) if args.skip_existing else None
            )
            subjects_data, skipped_count = filter_existing_subject_registrations(
                normalized_pairs,
                existing_subjects,
                should_filter=args.skip_existing,
                subject_key_field=args.subject_key_field,
            )

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
        validate_cli_path_segment(args.study_key, name="study_key")
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
        register_subjects_cmd(args)

    register_parser.set_defaults(func=_dispatch_register_subjects)
