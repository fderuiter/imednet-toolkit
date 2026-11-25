"""CLI commands for managing study subjects."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ...sdk import ImednetSDK
from ...utils.batch_coordinator import BatchSubmissionSummary
from ...utils.enrollment import (
    filter_existing_subject_registrations,
    normalize_subject_registration_inputs,
    validate_subject_registration_sites,
)
from ..decorators import validate_cli_path_segment, with_sdk
from ..utils import print_error, register_list_command, simulation_mode_context
from ..utils.args import (
    add_study_key_args,
    resolve_study_key,
)
from ..utils.context import is_offline_schema_dry_run
from ..utils.write_helpers import (
    add_write_common_args,
    assemble_cli_write_records,
    execute_cli_write,
    guard_stdin_dash_tty,
    read_cli_stdin_if_dash,
    resolve_cli_schema,
    submit_cli_records_batch,
    validate_write_numeric_flags,
)


def _load_cli_subject_inputs(
    args: argparse.Namespace,
    *,
    schema: Any = None,
    stdin_text: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve CLI subject registration inputs from ``--file``, ``--json``, or inline flags."""
    return assemble_cli_write_records(
        args,
        schema=schema,
        study_key=getattr(args, "study_key", None),
        is_subject_registration=True,
        stdin_text=stdin_text,
    )


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
        help="Path to CSV or JSON file of subject registration requests ('-' for stdin JSON or CSV).",
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
        "--form-id",
        type=int,
        default=None,
        help="Registration form ID (formId) for inline registration or file default.",
    )
    register_parser.add_argument(
        "--site-name",
        default=None,
        help="Enrollment site name (siteName) for inline registration or file default.",
    )
    register_parser.add_argument(
        "--site-id",
        type=int,
        default=None,
        help="Enrollment site ID (siteId) for inline registration or file default.",
    )
    register_parser.add_argument(
        "--subject-key",
        default=None,
        help="Subject key (subjectKey) for inline registration.",
    )
    register_parser.add_argument(
        "--subject-oid",
        default=None,
        help="Subject OID (subjectOid) for inline registration.",
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
        offline_dry_run = is_offline_schema_dry_run(
            dry_run=args.dry_run,
            schema_source=args.schema_source,
        )

        def _run() -> BatchSubmissionSummary:
            stdin_text = read_cli_stdin_if_dash(args.input_file)
            _load_cli_subject_inputs(args, schema=None, stdin_text=stdin_text)
            resolved_schema = resolve_cli_schema(
                sdk,
                args.study_key,
                args.schema_source,
                dry_run=args.dry_run,
            )
            raw_inputs = _load_cli_subject_inputs(
                args, schema=resolved_schema, stdin_text=stdin_text
            )
            normalized_pairs = normalize_subject_registration_inputs(
                raw_inputs,
                default_form_key=args.form_key,
                default_form_id=args.form_id,
                default_site_name=args.site_name,
                default_site_id=args.site_id,
                schema=resolved_schema,
                study_key=args.study_key,
            )
            sites = None if offline_dry_run else list(sdk.sites.list(args.study_key))
            validate_subject_registration_sites(normalized_pairs, sites)

            existing_subjects = (
                list(sdk.subjects.list(args.study_key))
                if (args.skip_existing and not offline_dry_run)
                else None
            )
            subjects_data, skipped_count = filter_existing_subject_registrations(
                normalized_pairs,
                existing_subjects,
                should_filter=args.skip_existing,
                subject_key_field=args.subject_key_field,
            )
            validation_schema = resolved_schema if (args.schema_source or args.dry_run) else None
            return submit_cli_records_batch(
                sdk,
                args,
                subjects_data,
                action_label="subject registration",
                schema=validation_schema,
                skipped_count=skipped_count,
            )

        execute_cli_write(
            _run,
            study_key=args.study_key,
            resource="subjects_register",
            output_format=args.output_format,
            dry_run=args.dry_run,
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
        guard_stdin_dash_tty(args.input_file)
        if args.input_file and args.input_file != "-" and not Path(args.input_file).exists():
            print_error(f"Error: Input file not found: '{args.input_file}'")
            sys.exit(1)
        if (
            not args.input_file
            and not args.json_payload
            and (
                (not args.form_key and args.form_id is None)
                or (not args.site_name and args.site_id is None)
            )
        ):
            print_error(
                "Error: Provide --file, --json, or both --form-key / --form-id and "
                "--site-name / --site-id to register subjects."
            )
            sys.exit(1)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            register_subjects_cmd(args)

    register_parser.set_defaults(func=_dispatch_register_subjects)
