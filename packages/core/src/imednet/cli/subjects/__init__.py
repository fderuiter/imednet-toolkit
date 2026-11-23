"""CLI commands for managing study subjects."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...models.records import RegisterSubjectRequest

from ...errors import ValidationError
from ...sdk import ImednetSDK
from ...utils.enrollment import (
    filter_existing_subject_registrations,
    normalize_subject_registration_inputs,
    validate_subject_registration_sites,
)
from ...utils.job_poller import JobFailedError
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
    parse_email_notify,
    parse_json_records_text,
    parse_key_value_pairs,
    render_write_summary,
    resolve_cli_schema,
    summary_from_job_failure,
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

    study_key: str | None = getattr(args, "study_key", None)
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
    inline_data = parse_key_value_pairs(
        args.data_pairs,
        schema=schema,
        study_key=study_key,
        form_key=args.form_key,
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
        subjects_data: list[dict[str, Any]] = []
        skipped_count = 0
        try:
            resolved_schema = resolve_cli_schema(sdk, args.study_key, args.schema_source)
            raw_inputs = _load_cli_subject_inputs(args, schema=resolved_schema)
            normalized_pairs = normalize_subject_registration_inputs(
                raw_inputs,
                default_form_key=args.form_key,
                default_site_name=args.site_name,
                schema=resolved_schema,
                study_key=args.study_key,
            )
            if args.data_pairs and (args.input_file or args.json_payload):
                updated_pairs: list[
                    tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]
                ] = []
                for _raw_item, model_item in normalized_pairs:
                    eff_fk = model_item.form_key or args.form_key
                    extra_data = parse_key_value_pairs(
                        args.data_pairs,
                        schema=resolved_schema,
                        study_key=args.study_key,
                        form_key=eff_fk or None,
                    )
                    dumped_model = model_item.model_dump(by_alias=True)
                    merged_data = dict(dumped_model.get("data") or {})
                    merged_data.update(extra_data)
                    dumped_model["data"] = merged_data
                    if (
                        args.subject_key
                        and len(normalized_pairs) == 1
                        and not dumped_model.get("subjectKey")
                    ):
                        dumped_model["subjectKey"] = args.subject_key
                    updated_pairs.append((dumped_model, model_item.model_validate(dumped_model)))
                normalized_pairs = updated_pairs
            elif (
                args.subject_key
                and (args.input_file or args.json_payload)
                and len(normalized_pairs) == 1
                and not normalized_pairs[0][1].subject_key
            ):
                dumped_only = normalized_pairs[0][1].model_dump(by_alias=True)
                dumped_only["subjectKey"] = args.subject_key
                normalized_pairs = [
                    (dumped_only, normalized_pairs[0][1].model_validate(dumped_only))
                ]

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
        except JobFailedError as exc:
            failed_summary = summary_from_job_failure(
                exc,
                study_key=args.study_key,
                total_records=len(subjects_data),
                skipped_count=skipped_count,
            )
            render_write_summary(
                failed_summary,
                resource="subjects_register",
                output_format=args.output_format,
            )
            return
        except (ValidationError, ValueError) as exc:
            exit_cli_validation_error(
                exc,
                resource="subjects_register",
                study_key=args.study_key,
                output_format=args.output_format,
            )

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
