"""CLI commands for managing study subjects."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ...sdk import ImednetSDK
from ...utils.enrollment import (
    filter_existing_subject_registrations,
    normalize_subject_registration_inputs,
    validate_subject_registration_sites,
)
from ..decorators import with_sdk
from ..utils import print_error, register_list_command, simulation_mode_context
from ..utils._write_batch import prepare_write_cli_preflight, run_cli_batch_write
from ..utils.args import add_study_key_args
from ..utils.context import is_offline_schema_dry_run
from ..utils.write_helpers import (
    add_write_common_args,
    assemble_cli_write_records,
)


def _subject_view_model(s: Any) -> dict[str, Any]:
    keywords_str = ", ".join(k.keyword_name for k in (s.keywords or ()))
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

        def _prepare_subjects(resolved_schema: Any) -> tuple[list[dict[str, Any]], int]:
            raw_inputs = assemble_cli_write_records(
                args,
                schema=resolved_schema,
                study_key=args.study_key,
                is_subject_registration=True,
            )
            normalized_pairs = normalize_subject_registration_inputs(
                raw_inputs,
                default_form_key=args.form_key,
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
            return filter_existing_subject_registrations(
                normalized_pairs,
                existing_subjects,
                should_filter=args.skip_existing,
                subject_key_field=args.subject_key_field,
            )

        run_cli_batch_write(
            sdk,
            args,
            resource="subjects_register",
            action_label="subject registration",
            prepare_batch=_prepare_subjects,
        )

    def _dispatch_register_subjects(args: argparse.Namespace) -> None:
        prepare_write_cli_preflight(register_parser, args)
        if (
            not args.input_file
            and not args.json_payload
            and (not args.form_key or (not args.site_name and args.site_id is None))
        ):
            print_error(
                "Error: Provide --file, --json, or both --form-key and --site-name to register subjects."
            )
            sys.exit(1)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            register_subjects_cmd(args)

    register_parser.set_defaults(func=_dispatch_register_subjects)
