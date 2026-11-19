"""CLI commands for iMednet workflows.

This module provides the argparse integration for clinical data workflows,
including record extraction, subject data retrieval, record updates, subject
registration, query management, DuckDB centralization, record mapping, study
schema inspection, UAT execution, cache synchronization, and state ledger
management.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel

from imednet.sdk import ImednetSDK
from imednet.spi.cli import (
    NON_INTERACTIVE_ENV,
    STUDY_KEY_ARG,
    add_list_query_args,
    add_no_interactive,
    add_output_format,
    build_list_query_kwargs,
    clear_line,
    console,
    display_list,
    emit_json,
    fetching_status,
    parse_filter_args,
    print_error,
    with_sdk,
)

from .data_extraction import DataExtractionWorkflow
from .state_ledger import DEFAULT_LEDGER_PATH, get_state_provider
from .subject_data import SubjectComprehensiveData, SubjectDataWorkflow
from .sync_worker import SyncWorker, SyncWorkerConfig

# Canonical mapping from every ``imednet.workflows`` entry point name to its
# ``imednet workflows <subcommand>`` CLI command.
WORKFLOW_CLI_SUBCOMMANDS: dict[str, str] = {
    "data_extraction": "extract-records",
    "duckdb_centralizer": "centralize-duckdb",
    "query_management": "queries",
    "record_mapper": "map-records",
    "record_update": "update-records",
    "register_subjects": "register-subjects",
    "subject_data": "subject-data",
    "uat": "uat",
    "uat_inspector": "inspect-study",
}

_JOB_SUMMARY_FIELDS: list[str] = [
    "batch_id",
    "job_id",
    "state",
    "progress",
    "date_created",
    "date_finished",
]


def _coerce_key_value_pairs(pairs: list[str] | None) -> dict[str, Any]:
    """Parse repeatable ``KEY=VALUE`` pairs into a dictionary."""
    if not pairs:
        return {}
    data: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            print_error(f"Error: Invalid --set pair '{item}'. Expected 'KEY=VALUE'.")
            sys.exit(1)
        key, raw_val = item.split("=", 1)
        key = key.strip()
        if not key:
            print_error(f"Error: Invalid --set pair '{item}'. Key cannot be empty.")
            sys.exit(1)
        stripped = raw_val.strip()
        lowered = stripped.lower()
        if lowered == "true":
            data[key] = True
        elif lowered == "false":
            data[key] = False
        elif stripped.isdigit():
            data[key] = int(stripped)
        else:
            data[key] = stripped
    return data


def _load_json_payload_objects(
    *,
    file_path: Path | None,
    raw_json: str | None,
    label: str,
) -> list[dict[str, Any]]:
    """Load a JSON object or list of JSON objects from ``--file`` or ``--data``."""
    source_text: str
    if file_path is not None:
        try:
            source_text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            print_error(f"Error: Failed to read {label} file '{file_path}': {exc}")
            sys.exit(1)
    elif raw_json is not None:
        source_text = raw_json
    else:
        return []

    try:
        parsed = json.loads(source_text)
    except json.JSONDecodeError as exc:
        print_error(f"Error: Invalid JSON in {label} payload: {exc}")
        sys.exit(1)

    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list) and parsed and all(isinstance(entry, dict) for entry in parsed):
        return list(parsed)

    print_error(f"Error: {label.capitalize()} payload must be a non-empty JSON object or array.")
    sys.exit(1)


def _parse_csv_ints(raw: str | None, flag_name: str) -> list[int] | None:
    """Parse a comma-separated list of integer IDs."""
    if not raw:
        return None
    values: list[int] = []
    for token in raw.split(","):
        stripped = token.strip()
        if not stripped:
            continue
        try:
            values.append(int(stripped))
        except ValueError:
            print_error(f"Error: Invalid integer '{stripped}' in {flag_name}.")
            sys.exit(1)
    return values or None


def _execute_show_state(
    ledger_path: str,
    study_key: str | None = None,
    output_format: str = "table",
) -> None:
    """Render high-water mark ledger entries on stdout and route errors to stderr."""
    ledger = get_state_provider(ledger_path)
    try:
        state = ledger.read_state()
    except Exception as err:
        print_error(f"Failed to read ledger from {ledger_path}: {err}")
        sys.exit(1)

    entries: list[dict[str, Any]] = []
    for s_key, study_state in state.studies.items():
        if study_key and s_key != study_key:
            continue
        for stream_name, stream_state in study_state.streams.items():
            entries.append(
                {
                    "studyKey": s_key,
                    "streamName": stream_name,
                    "lastTimestamp": (
                        stream_state.last_timestamp.isoformat()
                        if stream_state.last_timestamp
                        else None
                    ),
                    "recordsProcessed": stream_state.records_processed,
                    "status": str(stream_state.last_run_status),
                    "errorMessage": stream_state.error_message or "",
                }
            )

    if output_format == "json":
        emit_json(entries, "workflow_state")
        return

    print(f"iMednet Extraction Ledger ({ledger_path})")
    print(
        "Study Key | Stream Name | Last Timestamp (UTC) | Records Processed | Status | Error Message"
    )
    for row in entries:
        print(
            f"{row['studyKey']} | {row['streamName']} | {row['lastTimestamp']} | "
            f"{row['recordsProcessed']} | {row['status']} | {row['errorMessage']}"
        )

    if not entries:
        print("No ledger entries found matching filters.")


def _execute_set_state(
    study_key: str | None,
    stream: str | None,
    timestamp: str | None,
    records_processed: int,
    ledger_path: str,
    interactive: bool = False,
    parser: argparse.ArgumentParser | None = None,
    output_format: str = "table",
    no_interactive: bool = False,
) -> None:
    """Execute high-water mark timestamp update for a study/stream in the ledger."""
    if not interactive:
        missing = [
            flag
            for flag, val in [
                ("-s/--study-key", study_key),
                ("-m/--stream", stream),
                ("-t/--timestamp", timestamp),
            ]
            if not val
        ]
        if missing:
            msg = f"the following arguments are required: {', '.join(missing)}"
            if parser is not None:
                parser.error(msg)
            print_error(f"Error: {msg}")
            sys.exit(2)

        try:
            normalized = timestamp.replace("Z", "+00:00")  # type: ignore[union-attr]
            dt = datetime.fromisoformat(normalized)
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError as err:
            print_error(f"Invalid ISO timestamp format: {err}")
            sys.exit(1)

        ledger = get_state_provider(ledger_path)
        try:
            ledger.set_last_timestamp(
                study_key=study_key,  # type: ignore[arg-type]
                stream_name=stream,  # type: ignore[arg-type]
                timestamp=dt,
                records_processed=records_processed,
                status="success",
            )
        except Exception as err:
            print_error(f"Failed to write ledger: {err}")
            sys.exit(1)

        if output_format == "json":
            emit_json(
                [
                    {
                        "studyKey": study_key,
                        "streamName": stream,
                        "timestamp": dt.isoformat(),
                        "recordsProcessed": records_processed,
                        "status": "success",
                    }
                ],
                "workflow_state",
            )
        else:
            print(
                f"Successfully set high-water mark for '{study_key}' -> '{stream}' to {dt.isoformat()}"
            )
        return

    from .wizard import (
        display_ledger_streams,
        ensure_interactive_stdin,
        print_progress,
        prompt_confirm,
        prompt_iso8601_timestamp,
        prompt_non_negative_int,
        prompt_stream_name,
        prompt_study_key,
        validate_iso8601_timestamp,
    )

    ensure_interactive_stdin(force_non_interactive=no_interactive)
    display_ledger_streams(ledger_path=ledger_path, study_key=study_key)
    study_key = prompt_study_key(study_key)
    stream = prompt_stream_name(stream)

    if not timestamp:
        _, dt = prompt_iso8601_timestamp(
            "Enter ISO-8601 UTC timestamp (e.g., '2026-05-22T12:00:00Z')"
        )
    else:
        is_valid, dt_val, val_err = validate_iso8601_timestamp(timestamp)
        if not is_valid or dt_val is None:
            print_progress(f"Validation Error in provided timestamp: {val_err}")
            _, dt = prompt_iso8601_timestamp(
                "Enter ISO-8601 UTC timestamp (e.g., '2026-05-22T12:00:00Z')"
            )
        else:
            dt = dt_val

    records_processed = prompt_non_negative_int(
        "Enter number of records processed", default=records_processed
    )

    ledger = get_state_provider(ledger_path)
    current_status = "None"
    current_ts_str = "None"
    try:
        current_state = ledger.read_state()
        if (
            study_key in current_state.studies
            and stream in current_state.studies[study_key].streams
        ):
            st = current_state.studies[study_key].streams[stream]
            current_status = str(st.last_run_status)
            current_ts_str = st.last_timestamp.isoformat() if st.last_timestamp else "None"
    except Exception as exc:
        print_progress(f"Notice: unable to read stream state from ledger: {exc}")

    print_progress("\nProposed State Transition:")
    print_progress(f"  Study:             {study_key}")
    print_progress(f"  Stream:            {stream}")
    print_progress(f"  Current Status:    {current_status} (timestamp: {current_ts_str})")
    print_progress(f"  New Status:        success (timestamp: {dt.isoformat()})")
    print_progress(f"  Records Processed: {records_processed}")

    confirmed = prompt_confirm("Confirm state transition and write to ledger?", default=False)
    if not confirmed:
        print_progress("State update cancelled. Ledger was not modified.")
        return

    try:
        ledger.set_last_timestamp(
            study_key=study_key,
            stream_name=stream,
            timestamp=dt,
            records_processed=records_processed,
            status="success",
            force=True,
        )
        print(
            f"Successfully set high-water mark for '{study_key}' -> '{stream}' to {dt.isoformat()}"
        )
    except Exception as err:
        print_error(f"Failed to write ledger: {err}")
        sys.exit(1)


def _execute_reset_state(
    study_key: str | None,
    stream: str | None,
    ledger_path: str,
    interactive: bool = False,
    parser: argparse.ArgumentParser | None = None,
    output_format: str = "table",
    no_interactive: bool = False,
) -> None:
    """Execute high-water mark state deletion for a study/stream."""
    if interactive:
        from .wizard import (
            ensure_interactive_stdin,
            print_progress,
            prompt_confirm,
            prompt_study_key,
        )

        ensure_interactive_stdin(force_non_interactive=no_interactive)
        study_key = prompt_study_key(study_key)
        target_desc = (
            f"stream '{stream}' for study '{study_key}'"
            if stream
            else f"all streams for study '{study_key}'"
        )
        confirmed = prompt_confirm(f"Are you sure you want to reset {target_desc}?", default=False)
        if not confirmed:
            print_progress("Reset cancelled by user. Ledger was not modified.")
            return
    elif not study_key:
        msg = "the following arguments are required: -s/--study-key"
        if parser is not None:
            parser.error(msg)
        print_error(f"Error: {msg}")
        sys.exit(2)

    ledger = get_state_provider(ledger_path)
    try:
        removed = (
            ledger.delete_entry(study_key, stream) if stream else ledger.delete_entry(study_key)
        )
    except Exception as err:
        print_error(f"Failed to reset ledger state: {err}")
        sys.exit(1)

    if output_format == "json":
        emit_json(
            [
                {
                    "studyKey": study_key,
                    "streamName": stream,
                    "removed": bool(removed),
                }
            ],
            "workflow_state",
        )
        return

    if stream:
        if removed:
            print(f"Successfully reset stream '{stream}' for study '{study_key}'.")
        else:
            print(f"No stream '{stream}' found for study '{study_key}'.")
    elif removed:
        print(f"Successfully reset all streams for study '{study_key}'.")
    else:
        print(f"No state found for study '{study_key}'.")


def _register_state_subcommands(state_sub: argparse._SubParsersAction[Any]) -> None:
    """Attach ``show``, ``set``, and ``reset`` subcommands to a state subparser."""
    show_parser = state_sub.add_parser(
        "show", help="Show the current high-water mark records and stream metadata."
    )
    show_parser.add_argument(
        "-l",
        "--ledger-path",
        default=DEFAULT_LEDGER_PATH,
        help="Path to the pipeline ledger JSON file.",
    )
    show_parser.add_argument("-s", "--study-key", help="Filter the ledger by a specific study key.")
    show_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_output_format(show_parser)
    show_parser.set_defaults(
        func=lambda args: _execute_show_state(
            ledger_path=args.ledger_path,
            study_key=args.study_key,
            output_format=args.output_format,
        )
    )

    set_parser = state_sub.add_parser(
        "set", help="Manually set a high-water mark timestamp for a study and stream."
    )
    set_parser.add_argument("-s", "--study-key", help="The study key.")
    set_parser.add_argument("-m", "--stream", help="The stream name.")
    set_parser.add_argument("-t", "--timestamp", help="The ISO-8601 timestamp (UTC).")
    set_parser.add_argument(
        "-r", "--records-processed", type=int, default=0, help="Number of records processed."
    )
    set_parser.add_argument(
        "-l",
        "--ledger-path",
        default=DEFAULT_LEDGER_PATH,
        help="Path to the pipeline ledger JSON file.",
    )
    set_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_output_format(set_parser)
    set_parser.set_defaults(
        func=lambda args: _execute_set_state(
            study_key=args.study_key,
            stream=args.stream,
            timestamp=args.timestamp,
            records_processed=args.records_processed,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=set_parser,
            output_format=args.output_format,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )

    reset_parser = state_sub.add_parser(
        "reset", help="Reset (clear) high-water mark state for a study/stream."
    )
    reset_parser.add_argument("-s", "--study-key", help="The study key.")
    reset_parser.add_argument(
        "-m",
        "--stream",
        help="The stream name. If omitted, all streams for the study will be reset.",
    )
    reset_parser.add_argument(
        "-l",
        "--ledger-path",
        default=DEFAULT_LEDGER_PATH,
        help="Path to the pipeline ledger JSON file.",
    )
    reset_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_output_format(reset_parser)
    reset_parser.set_defaults(
        func=lambda args: _execute_reset_state(
            study_key=args.study_key,
            stream=args.stream,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=reset_parser,
            output_format=args.output_format,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )


class _UniqueStoreAction(argparse.Action):
    """Store action that rejects conflicting duplicate flag values."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del option_string
        existing = getattr(namespace, self.dest, None)
        if existing is not None and existing != values:
            key_name = self.dest.replace("_flag", "")
            parser.error(f"conflicting values provided for {key_name}: '{existing}' and '{values}'")
        setattr(namespace, self.dest, values)


def _render_subject_data(
    data: Any,
    *,
    study_key: str,
    subject_key: str,
    output_format: str,
) -> None:
    """Emit structured table or versioned JSON for subject-data on stdout."""
    if output_format == "json":
        if isinstance(data, BaseModel):
            emit_json([data], "subject_data")
            return
        dumped = data.model_dump() if hasattr(data, "model_dump") else None
        payload = (
            dumped
            if isinstance(dumped, dict)
            else {"studyKey": study_key, "subjectKey": subject_key}
        )
        emit_json([payload], "subject_data")
        return

    if isinstance(data, SubjectComprehensiveData):
        details = data.subject_details
        summary_row = {
            "study_key": study_key,
            "subject_key": getattr(details, "subject_key", None) or subject_key,
            "status": getattr(details, "subject_status", None),
            "site": getattr(details, "site_name", None),
            "visits": len(data.visits),
            "records": len(data.records),
            "queries": len(data.queries),
        }
        display_list([summary_row], "subject_data", details=[data], interactive=False)
        return

    display_list(
        [{"study_key": study_key, "subject_key": subject_key}],
        "subject_data",
        interactive=False,
    )


def setup_subject_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Set up the subject-data argparse subcommand."""
    subj_parser = subparsers.add_parser(
        "subject-data",
        help="Retrieve all data for a single subject.",
        description=(
            "Retrieve all data for a single subject within a study. "
            "Arguments may be provided via flags (--study-key, --subject-key/--subject), "
            "positional arguments (<study_key> <subject_key>), or hybrid combinations "
            "where a remaining positional argument maps deterministically to the missing key. "
            "Flags take precedence over positional arguments."
        ),
    )
    subj_parser.add_argument(
        "-s",
        "--study-key",
        dest="study_key_flag",
        default=None,
        action=_UniqueStoreAction,
        help=STUDY_KEY_ARG,
    )
    subj_parser.add_argument(
        "-k",
        "--subject-key",
        "--subject",
        dest="subject_key_flag",
        default=None,
        action=_UniqueStoreAction,
        help="The key identifying the subject.",
    )
    subj_parser.add_argument("study_key", nargs="?", default=None, help=STUDY_KEY_ARG)
    subj_parser.add_argument(
        "subject_key", nargs="?", default=None, help="The key identifying the subject."
    )
    add_output_format(subj_parser)

    @with_sdk
    def subject_data(
        sdk: ImednetSDK,
        study_key: str,
        subject_key: str,
        output_format: str = "table",
    ) -> None:
        with fetching_status(f"subject '{subject_key}' data", study_key):
            workflow = SubjectDataWorkflow(sdk)  # type: ignore[arg-type]
            data = workflow.get_all_subject_data(study_key, subject_key)
        _render_subject_data(
            data,
            study_key=study_key,
            subject_key=subject_key,
            output_format=output_format,
        )

    def handle_subject_data(args: argparse.Namespace) -> None:
        flag_study: str | None = args.study_key_flag
        flag_subject: str | None = args.subject_key_flag
        pos_study: str | None = args.study_key
        pos_subject: str | None = args.subject_key

        def _is_empty(val: str | None) -> bool:
            return val is None or not val.strip()

        if flag_study is not None and flag_subject is not None:
            study_key = flag_study
            subject_key = flag_subject
        elif flag_study is not None and flag_subject is None:
            study_key = flag_study
            if pos_subject is not None and pos_study is not None:
                if (
                    not _is_empty(pos_study)
                    and not _is_empty(flag_study)
                    and pos_study != flag_study
                ):
                    subj_parser.error(
                        f"conflicting values provided for study_key: '{flag_study}' (flag) and '{pos_study}' (positional)"
                    )
                subject_key = pos_subject
            elif pos_study is not None:
                subject_key = pos_study
            else:
                subject_key = None
        elif flag_study is None and flag_subject is not None:
            subject_key = flag_subject
            if pos_study is not None and pos_subject is not None:
                if (
                    not _is_empty(pos_subject)
                    and not _is_empty(flag_subject)
                    and pos_subject != flag_subject
                ):
                    subj_parser.error(
                        f"conflicting values provided for subject_key: '{flag_subject}' (flag) and '{pos_subject}' (positional)"
                    )
                study_key = pos_study
            elif pos_study is not None:
                study_key = pos_study
            else:
                study_key = None
        else:
            study_key = pos_study
            subject_key = pos_subject

        missing_study = _is_empty(study_key)
        missing_subject = _is_empty(subject_key)
        if missing_study and missing_subject:
            subj_parser.error("the following arguments are required: study_key, subject_key")
        elif missing_study:
            subj_parser.error("the following arguments are required: study_key")
        elif missing_subject:
            subj_parser.error("the following arguments are required: subject_key")

        assert study_key is not None
        assert subject_key is not None
        if args.output_format == "table":
            subject_data(study_key=study_key, subject_key=subject_key)
        else:
            subject_data(
                study_key=study_key,
                subject_key=subject_key,
                output_format=args.output_format,
            )

    subj_parser.set_defaults(func=handle_subject_data)


def setup_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Set up the workflows argparse subparsers."""
    wf_parser = subparsers.add_parser("workflows", help="Execute common data workflows.")
    sub = wf_parser.add_subparsers(dest="wf_command")

    # 1. data_extraction -> extract-records
    extract_parser = sub.add_parser(
        "extract-records",
        help="Extract records based on criteria spanning subjects, visits, and records.",
    )
    extract_parser.add_argument("study_key", nargs="?", default=None, help=STUDY_KEY_ARG)
    extract_parser.add_argument(
        "--record-filter",
        action="append",
        help="Record filter criteria (e.g., 'form_key=DEMO'). Repeat for multiple filters.",
    )
    extract_parser.add_argument(
        "--subject-filter",
        action="append",
        help="Subject filter criteria (e.g., 'subject_status=Screened'). Repeat for multiple filters.",
    )
    extract_parser.add_argument(
        "--visit-filter",
        action="append",
        help="Visit filter criteria (e.g., 'interval_name=Screening'). Repeat for multiple filters.",
    )
    extract_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run in interactive wizard mode with step-by-step prompts.",
    )
    add_output_format(extract_parser)

    @with_sdk
    def extract_records(
        sdk: ImednetSDK,
        study_key: str | None,
        record_filter: list[str] | None = None,
        subject_filter: list[str] | None = None,
        visit_filter: list[str] | None = None,
        interactive: bool = False,
        output_format: str = "table",
        no_interactive: bool = False,
    ) -> None:
        from imednet.spi.models import Record, Subject, Visit

        from .wizard import print_progress

        if interactive:
            from .wizard import ensure_interactive_stdin, prompt_filters, prompt_study_key

            ensure_interactive_stdin(force_non_interactive=no_interactive)
            study_key = prompt_study_key(study_key)
            record_filter = prompt_filters(
                "record filter",
                "form_key=DEMO",
                existing_filters=record_filter,
                model=Record,
            )
            subject_filter = prompt_filters(
                "subject filter",
                "subject_status=Screened",
                existing_filters=subject_filter,
                model=Subject,
            )
            visit_filter = prompt_filters(
                "visit filter",
                "interval_name=Screening",
                existing_filters=visit_filter,
                model=Visit,
            )
        elif not study_key:
            extract_parser.error("the following arguments are required: study_key")

        assert study_key is not None
        workflow = DataExtractionWorkflow(sdk)  # type: ignore[arg-type]
        parsed_record_filter = parse_filter_args(record_filter, model=Record)
        parsed_subject_filter = parse_filter_args(subject_filter, model=Subject)
        parsed_visit_filter = parse_filter_args(visit_filter, model=Visit)

        print_progress(f"Extracting records for study '{study_key}'...")
        if interactive:
            records = workflow.extract_records_by_criteria(
                study_key=study_key,
                record_filter=parsed_record_filter,
                subject_filter=parsed_subject_filter,
                visit_filter=parsed_visit_filter,
                on_progress=lambda msg: print_progress(f"[Progress] {msg}"),
            )
        else:
            records = workflow.extract_records_by_criteria(
                study_key=study_key,
                record_filter=parsed_record_filter,
                subject_filter=parsed_subject_filter,
                visit_filter=parsed_visit_filter,
            )

        if output_format == "json":
            display_list(records, "records", output_format="json")
            return

        view_models = [
            {
                "ID": getattr(rec, "record_id", None),
                "Subject": getattr(rec, "subject_key", None),
                "Form": getattr(rec, "form_key", None),
                "Status": getattr(rec, "record_status", None),
                "Created": getattr(rec, "date_created", None),
            }
            for rec in records
        ]
        display_list(
            view_models,
            "matching records",
            "No records found matching the criteria.",
            details=records,
            interactive=False if no_interactive else None,
        )

    def _handle_extract_records(args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        if args.interactive:
            from .wizard import ensure_interactive_stdin

            ensure_interactive_stdin(force_non_interactive=no_interactive)
        extract_records(
            study_key=args.study_key,
            record_filter=args.record_filter,
            subject_filter=args.subject_filter,
            visit_filter=args.visit_filter,
            interactive=args.interactive,
            output_format=args.output_format,
            no_interactive=no_interactive,
        )

    extract_parser.set_defaults(func=_handle_extract_records)

    # 2. subject_data -> subject-data (also registered at top-level via setup_subject_parser)
    setup_subject_parser(sub)

    # 3. record_update -> update-records
    update_parser = sub.add_parser(
        "update-records",
        help="Create or update study records from JSON payloads or field flags.",
    )
    update_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    update_parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Path to a JSON file containing a record object or list of record objects.",
    )
    update_parser.add_argument(
        "--data",
        default=None,
        help="Inline JSON string containing a record object or list of record objects.",
    )
    update_parser.add_argument("--form-key", default=None, help="Form key for a single record.")
    update_parser.add_argument(
        "--subject-key", default=None, help="Subject key for a single record."
    )
    update_parser.add_argument("--site-name", default=None, help="Site name for a single record.")
    update_parser.add_argument(
        "--interval-name", default=None, help="Interval name for a scheduled record."
    )
    update_parser.add_argument(
        "--set",
        action="append",
        dest="field_values",
        default=None,
        help="Record variable assignment 'KEY=VALUE'. Repeat for multiple fields.",
    )
    update_parser.add_argument(
        "--wait",
        action="store_true",
        help="Poll until the background record submission job reaches a terminal state.",
    )
    update_parser.add_argument(
        "--timeout", type=int, default=300, help="Maximum seconds to wait when --wait is set."
    )
    update_parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Polling interval in seconds when --wait is set.",
    )
    add_output_format(update_parser)

    @with_sdk
    def update_records_cmd(
        sdk: ImednetSDK,
        study_key: str,
        file_path: Path | None = None,
        raw_data: str | None = None,
        form_key: str | None = None,
        subject_key: str | None = None,
        site_name: str | None = None,
        interval_name: str | None = None,
        field_values: list[str] | None = None,
        wait: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        output_format: str = "table",
    ) -> None:
        from .record_update import RecordUpdateWorkflow

        if file_path is not None and raw_data is not None:
            print_error("Error: Provide either --file or --data, not both.")
            sys.exit(1)

        workflow = RecordUpdateWorkflow(sdk)  # type: ignore[arg-type]
        if file_path is not None or raw_data is not None:
            records_payload = _load_json_payload_objects(
                file_path=file_path,
                raw_json=raw_data,
                label="record",
            )
        else:
            if not form_key:
                print_error("Error: Provide --file, --data, or --form-key with --set KEY=VALUE.")
                sys.exit(1)
            kv_data = _coerce_key_value_pairs(field_values)
            single_record: dict[str, Any] = {"formKey": form_key, "data": kv_data}
            if subject_key is not None:
                single_record["subjectKey"] = subject_key
            if site_name is not None:
                single_record["siteName"] = site_name
            if interval_name is not None:
                single_record["intervalName"] = interval_name
            records_payload = [single_record]

        with fetching_status("record submission", study_key):
            job = workflow.create_or_update_records(
                study_key=study_key,
                records_data=records_payload,
                wait_for_completion=wait,
                timeout=timeout,
                poll_interval=poll_interval,
            )

        display_list(
            [job],
            "jobs",
            fields=_JOB_SUMMARY_FIELDS,
            output_format=output_format,
            details=[job],
            interactive=False,
        )

    update_parser.set_defaults(
        func=lambda args: update_records_cmd(
            study_key=args.study_key,
            file_path=args.file,
            raw_data=args.data,
            form_key=args.form_key,
            subject_key=args.subject_key,
            site_name=args.site_name,
            interval_name=args.interval_name,
            field_values=args.field_values,
            wait=args.wait,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            output_format=args.output_format,
        )
    )

    # 4. register_subjects -> register-subjects
    reg_parser = sub.add_parser(
        "register-subjects",
        help="Register one or more subjects in a study.",
    )
    reg_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    reg_parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Path to a JSON file containing subject registration payload(s).",
    )
    reg_parser.add_argument(
        "--data",
        default=None,
        help="Inline JSON string containing subject registration payload(s).",
    )
    reg_parser.add_argument(
        "--form-key",
        default=None,
        help="Enrollment form key when registering via CLI flags.",
    )
    reg_parser.add_argument(
        "--site-name",
        default=None,
        help="Target site name when registering via CLI flags.",
    )
    reg_parser.add_argument(
        "--set",
        action="append",
        dest="field_values",
        default=None,
        help="Enrollment variable assignment 'KEY=VALUE'. Repeat for multiple fields.",
    )
    reg_parser.add_argument(
        "--email-notify",
        default=None,
        help="Optional email address to notify upon job completion.",
    )
    reg_parser.add_argument(
        "--wait",
        action="store_true",
        help="Poll until the background registration job reaches a terminal state.",
    )
    reg_parser.add_argument(
        "--timeout", type=int, default=300, help="Maximum seconds to wait when --wait is set."
    )
    reg_parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Polling interval in seconds when --wait is set.",
    )
    add_output_format(reg_parser)

    @with_sdk
    def register_subjects_cmd(
        sdk: ImednetSDK,
        study_key: str,
        file_path: Path | None = None,
        raw_data: str | None = None,
        form_key: str | None = None,
        site_name: str | None = None,
        field_values: list[str] | None = None,
        email_notify: str | None = None,
        wait: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        output_format: str = "table",
    ) -> None:
        from imednet.spi.models import RegisterSubjectRequest

        from .register_subjects import RegisterSubjectsWorkflow

        if file_path is not None and raw_data is not None:
            print_error("Error: Provide either --file or --data, not both.")
            sys.exit(1)

        if file_path is not None or raw_data is not None:
            raw_items = _load_json_payload_objects(
                file_path=file_path,
                raw_json=raw_data,
                label="subject registration",
            )
        else:
            if not form_key or not site_name:
                print_error("Error: Provide --file, --data, or both --form-key and --site-name.")
                sys.exit(1)
            raw_items = [
                {
                    "formKey": form_key,
                    "siteName": site_name,
                    "data": _coerce_key_value_pairs(field_values),
                }
            ]

        requests: list[RegisterSubjectRequest] = []
        for idx, entry in enumerate(raw_items):
            try:
                req = RegisterSubjectRequest.model_validate(entry)
            except Exception as exc:
                print_error(f"Error: Invalid subject payload at index {idx}: {exc}")
                sys.exit(1)
            if not req.form_key or not req.site_name:
                print_error(
                    f"Error: Subject payload at index {idx} requires both 'formKey' and 'siteName'."
                )
                sys.exit(1)
            requests.append(req)

        workflow = RegisterSubjectsWorkflow(sdk)  # type: ignore[arg-type]
        with fetching_status("subject registration", study_key):
            job = workflow.register_subjects(
                study_key=study_key,
                subjects=requests,
                email_notify=email_notify,
                wait_for_completion=wait,
                timeout=timeout,
                poll_interval=poll_interval,
            )

        display_list(
            [job],
            "jobs",
            fields=_JOB_SUMMARY_FIELDS,
            output_format=output_format,
            details=[job],
            interactive=False,
        )

    reg_parser.set_defaults(
        func=lambda args: register_subjects_cmd(
            study_key=args.study_key,
            file_path=args.file,
            raw_data=args.data,
            form_key=args.form_key,
            site_name=args.site_name,
            field_values=args.field_values,
            email_notify=args.email_notify,
            wait=args.wait,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            output_format=args.output_format,
        )
    )

    # 5. query_management -> queries
    qm_parser = sub.add_parser(
        "queries",
        help="Inspect open queries, subject/site queries, or query state counts.",
    )
    qm_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    qm_parser.add_argument(
        "--subject-key", default=None, help="Filter queries for a specific subject key."
    )
    qm_parser.add_argument(
        "--site-name", default=None, help="Filter queries across subjects at a specific site."
    )
    qm_parser.add_argument(
        "--counts",
        action="store_true",
        help="Summarize query counts grouped by query state.",
    )
    qm_parser.add_argument(
        "--open-only",
        action="store_true",
        help="Restrict subject or site query results to open queries only.",
    )
    add_list_query_args(qm_parser, filter_example="variable=V_AGE")
    add_output_format(qm_parser)

    @with_sdk
    def queries_workflow_cmd(
        sdk: ImednetSDK,
        study_key: str,
        subject_key: str | None = None,
        site_name: str | None = None,
        counts: bool = False,
        open_only: bool = False,
        filters: list[str] | None = None,
        sort: str | None = None,
        output_format: str = "table",
        no_interactive: bool = False,
    ) -> None:
        from imednet.spi.models import Query

        from .query_management import QueryManagementWorkflow

        if subject_key and site_name:
            qm_parser.error("choose either --subject-key or --site-name, not both")

        query_kwargs = build_list_query_kwargs(
            filters=filters,
            sort=sort,
            model=Query,
        )
        sort_val = query_kwargs.pop("sort", None)
        extra_call_kwargs: dict[str, Any] = {"sort": sort_val} if sort_val is not None else {}

        workflow = QueryManagementWorkflow(sdk)  # type: ignore[arg-type]
        with fetching_status("workflow queries", study_key):
            if counts:
                state_counts = workflow.get_query_state_counts(
                    study_key, **query_kwargs, **extra_call_kwargs
                )
                count_rows = [
                    {"status": status_name, "count": count_val}
                    for status_name, count_val in state_counts.items()
                ]
                display_list(
                    count_rows,
                    "query_counts",
                    "No queries found.",
                    output_format=output_format,
                    interactive=False if no_interactive else None,
                )
                return

            if subject_key:
                items = workflow.get_queries_for_subject(
                    study_key,
                    subject_key,
                    additional_filter=query_kwargs or None,
                    **extra_call_kwargs,
                )
                if open_only:
                    items = [q for q in items if workflow.is_query_open(q)]
            elif site_name:
                items = workflow.get_queries_by_site(
                    study_key,
                    site_name,
                    additional_filter=query_kwargs or None,
                    **extra_call_kwargs,
                )
                if open_only:
                    items = [q for q in items if workflow.is_query_open(q)]
            else:
                items = workflow.get_open_queries(
                    study_key,
                    additional_filter=query_kwargs or None,
                    **extra_call_kwargs,
                )

        if output_format == "json":
            display_list(items, "queries", output_format="json")
            return

        view_rows = [
            {
                "annotation_id": getattr(q, "annotation_id", None),
                "subject_key": getattr(q, "subject_key", None),
                "record_id": getattr(q, "record_id", None),
                "variable": getattr(q, "variable", None),
                "status": getattr(q, "status", None),
                "description": getattr(q, "description", None),
            }
            for q in items
        ]
        display_list(
            view_rows,
            "queries",
            "No matching queries found.",
            output_format="table",
            details=items,
            interactive=False if no_interactive else None,
        )

    qm_parser.set_defaults(
        func=lambda args: queries_workflow_cmd(
            study_key=args.study_key,
            subject_key=args.subject_key,
            site_name=args.site_name,
            counts=args.counts,
            open_only=args.open_only,
            filters=args.filters,
            sort=args.sort,
            output_format=args.output_format,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )

    # 6. duckdb_centralizer -> centralize-duckdb (lazy-loads duckdb!)
    ddb_parser = sub.add_parser(
        "centralize-duckdb",
        help="Centralize study record revisions into bronze/silver DuckDB tables.",
    )
    ddb_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    ddb_parser.add_argument(
        "db_path",
        nargs="?",
        default=None,
        help="Destination DuckDB file path or ':memory:' (default: ':memory:').",
    )
    ddb_parser.add_argument(
        "--db-path",
        dest="db_path_flag",
        default=None,
        help="Destination DuckDB file path override.",
    )
    ddb_parser.add_argument(
        "--start-date",
        default=None,
        help="Optional inclusive UTC start date/timestamp bound (ISO-8601).",
    )
    ddb_parser.add_argument(
        "--end-date",
        default=None,
        help="Optional inclusive UTC end date/timestamp bound (ISO-8601).",
    )
    ddb_parser.add_argument(
        "--mode",
        choices=("append", "replace"),
        default="append",
        help="Write mode for bronze_revisions table (default: append).",
    )
    ddb_parser.add_argument(
        "--no-silver-view",
        action="store_false",
        dest="build_silver_view",
        default=True,
        help="Skip creating/updating the silver_current_state DuckDB view.",
    )
    add_output_format(ddb_parser)

    @with_sdk
    def centralize_duckdb_cmd(
        sdk: ImednetSDK,
        study_key: str,
        db_path: str = ":memory:",
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
        build_silver_view: bool = True,
        output_format: str = "table",
    ) -> None:
        if importlib.util.find_spec("duckdb") is None:
            print_error(
                "Error: duckdb is required for centralize-duckdb. "
                "Install with \"pip install 'imednet[duckdb]'\"."
            )
            sys.exit(1)

        from .duckdb_centralizer import DuckDBIngestionWorkflow

        workflow = DuckDBIngestionWorkflow(sdk, db_path=db_path)  # type: ignore[arg-type]
        try:
            with fetching_status("record revisions for DuckDB centralization", study_key):
                revisions_ingested = workflow.ingest_revisions(
                    study_key,
                    start_date=start_date,
                    end_date=end_date,
                    mode=mode,
                )
                if build_silver_view:
                    workflow.build_silver_view(study_key)
        finally:
            workflow.close()

        summary = {
            "studyKey": study_key,
            "dbPath": db_path,
            "mode": mode,
            "revisionsIngested": revisions_ingested,
            "silverViewBuilt": build_silver_view,
        }
        display_list(
            [summary],
            "duckdb_centralization",
            output_format=output_format,
            interactive=False,
        )

    ddb_parser.set_defaults(
        func=lambda args: centralize_duckdb_cmd(
            study_key=args.study_key,
            db_path=args.db_path_flag or args.db_path or ":memory:",
            start_date=args.start_date,
            end_date=args.end_date,
            mode=cast(Literal["append", "replace"], args.mode),
            build_silver_view=args.build_silver_view,
            output_format=args.output_format,
        )
    )

    # 7. record_mapper -> map-records
    map_parser = sub.add_parser(
        "map-records",
        help="Map study records into typed tabular rows, SDTM domains, or hierarchies.",
    )
    map_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    map_parser.add_argument(
        "--visit-key", default=None, help="Optional integer visit ID to filter records."
    )
    map_parser.add_argument(
        "--use-labels",
        action="store_true",
        help="Use variable labels instead of variable names as keys/columns.",
    )
    map_parser.add_argument(
        "--vars",
        dest="vars_",
        default=None,
        help="Comma-separated list of variable names to include.",
    )
    map_parser.add_argument(
        "--forms",
        default=None,
        help="Comma-separated list of integer form IDs to include.",
    )
    map_parser.add_argument(
        "--sdtm-domain",
        default=None,
        help="Transform records into a CDISC SDTM domain (e.g., DM, VS, AE).",
    )
    map_parser.add_argument(
        "--hierarchical",
        action="store_true",
        help="Build nested Subject -> Visit -> Form hierarchy.",
    )
    add_list_query_args(
        map_parser,
        include_record_data_filter=True,
        filter_example="form_key=DEMO",
    )
    add_output_format(map_parser)

    @with_sdk
    def map_records_cmd(
        sdk: ImednetSDK,
        study_key: str,
        visit_key: str | None = None,
        use_labels: bool = False,
        vars_: str | None = None,
        forms: str | None = None,
        sdtm_domain: str | None = None,
        hierarchical: bool = False,
        filters: list[str] | None = None,
        record_data_filter: str | None = None,
        sort: str | None = None,
        output_format: str = "table",
        no_interactive: bool = False,
    ) -> None:
        from imednet.spi.models import Record

        from .record_mapper import RecordMapper

        if hierarchical and sdtm_domain:
            map_parser.error("choose either --hierarchical or --sdtm-domain, not both")

        var_list = [v.strip() for v in vars_.split(",") if v.strip()] if vars_ else None
        form_list = _parse_csv_ints(forms, "--forms")
        extra_filters = build_list_query_kwargs(
            filters=filters,
            sort=sort,
            record_data_filter=record_data_filter,
            model=Record,
        )

        mapper = RecordMapper(sdk)  # type: ignore[arg-type]
        with fetching_status("mapped study records", study_key):
            if hierarchical:
                hierarchy = mapper.build_hierarchy(
                    study_key,
                    visit_key=visit_key,
                    use_labels_as_keys=use_labels,
                    variable_whitelist=var_list,
                    form_whitelist=form_list,
                    extra_filters=extra_filters or None,
                )
                if output_format == "json":
                    emit_json(hierarchy, "mapped_records")
                else:
                    summary_rows = [
                        {
                            "subject_key": node.get("subject_key"),
                            "visits": len(node.get("visits", [])),
                        }
                        for node in hierarchy
                    ]
                    display_list(
                        summary_rows,
                        "mapped_records",
                        "No mapped records found.",
                        interactive=False if no_interactive else None,
                    )
                return

            if importlib.util.find_spec("pandas") is None:
                print_error(
                    "Error: pandas is required for map-records tabular output. "
                    "Install with \"pip install 'imednet[pandas]'\"."
                )
                sys.exit(1)

            import pandas as pd

            df = mapper.dataframe(
                study_key,
                visit_key=visit_key,
                use_labels_as_columns=use_labels,
                variable_whitelist=var_list,
                form_whitelist=form_list,
                as_sdtm=bool(sdtm_domain),
                domain=sdtm_domain,
                extra_filters=extra_filters or None,
            )

        rows: list[dict[str, Any]] = []
        if not df.empty:
            clean_df = df.astype(object).where(pd.notnull(df), None)
            rows = cast(list[dict[str, Any]], clean_df.to_dict(orient="records"))

        display_list(
            rows,
            "mapped_records",
            "No mapped records found.",
            output_format=output_format,
            interactive=False if no_interactive else None,
        )

    map_parser.set_defaults(
        func=lambda args: map_records_cmd(
            study_key=args.study_key,
            visit_key=args.visit_key,
            use_labels=args.use_labels,
            vars_=args.vars_,
            forms=args.forms,
            sdtm_domain=args.sdtm_domain,
            hierarchical=args.hierarchical,
            filters=args.filters,
            record_data_filter=args.record_data_filter,
            sort=args.sort,
            output_format=args.output_format,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )

    # 8. uat_inspector -> inspect-study
    inspect_parser = sub.add_parser(
        "inspect-study",
        help="Inspect and summarize structural study metadata for UAT readiness.",
    )
    inspect_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    inspect_parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass cached snapshot and re-fetch study metadata.",
    )
    add_output_format(inspect_parser)

    @with_sdk
    def inspect_study_cmd(
        sdk: ImednetSDK,
        study_key: str,
        force_refresh: bool = False,
        output_format: str = "table",
    ) -> None:
        from .uat.inspector import StudySchemaInspector

        inspector = StudySchemaInspector(sdk)  # type: ignore[arg-type]
        with fetching_status("study schema metadata", study_key):
            snapshot = inspector.inspect(study_key, force_refresh=force_refresh)

        if output_format == "json":
            emit_json(
                [
                    {
                        "studyKey": snapshot.study_key,
                        "capturedAt": snapshot.captured_at,
                        "formsCount": len(snapshot.forms),
                        "enrollmentFormsCount": len(snapshot.enrollment_forms()),
                        "scheduledFormsCount": len(snapshot.scheduled_forms()),
                        "unscheduledFormsCount": len(snapshot.unscheduled_forms()),
                        "variablesCount": len(snapshot.variables),
                        "intervalsCount": len(snapshot.intervals),
                        "sitesCount": len(snapshot.sites),
                        "activeSitesCount": len(snapshot.active_sites()),
                        "forms": snapshot.forms,
                        "intervals": snapshot.intervals,
                        "sites": snapshot.sites,
                        "variables": snapshot.variables,
                    }
                ],
                "study_schema",
            )
            return

        summary_row = {
            "study_key": snapshot.study_key,
            "forms": len(snapshot.forms),
            "variables": len(snapshot.variables),
            "intervals": len(snapshot.intervals),
            "sites": len(snapshot.sites),
            "active_sites": len(snapshot.active_sites()),
        }
        display_list([summary_row], "study_schema", details=[snapshot], interactive=False)

    inspect_parser.set_defaults(
        func=lambda args: inspect_study_cmd(
            study_key=args.study_key,
            force_refresh=args.force_refresh,
            output_format=args.output_format,
        )
    )

    # Sync worker
    sync_parser = sub.add_parser(
        "sync-worker", help="Run an idempotent background cache refresh worker."
    )
    sync_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    sync_parser.add_argument(
        "--interval", type=int, default=900, help="Polling interval in seconds."
    )
    sync_parser.add_argument(
        "--once", action="store_true", help="Run a single sync cycle and exit."
    )
    add_no_interactive(sync_parser)

    @with_sdk
    def sync_worker(
        sdk: ImednetSDK,
        study_key: str,
        interval: int = 900,
        once: bool = False,
    ) -> None:
        from .cached_loader import CachedRecordsLoader

        worker = SyncWorker(
            CachedRecordsLoader(sdk),  # type: ignore[arg-type]
            config=SyncWorkerConfig(study_key=study_key, interval_seconds=interval),
        )

        if once:
            count = worker.run_once()
            print(f"Synced {count} cached records for study '{study_key}'.")
            return

        print(
            f"Starting sync worker for study '{study_key}' "
            f"(interval={interval}s). Press Ctrl+C to stop."
        )
        try:
            with console.status(f"Starting sync worker for study '{study_key}'...") as status:

                def on_cycle(cycle: int, count: int) -> None:
                    status.update(f"Study '{study_key}' - cycle {cycle}: synced {count} records.")

                def on_sleep_tick(cycle: int, remaining: int) -> None:
                    status.update(
                        f"Study '{study_key}' - cycle {cycle}: sleeping ({remaining}s remaining)..."
                    )

                try:
                    worker.run_forever(on_cycle=on_cycle, on_sleep_tick=on_sleep_tick)
                except TypeError:
                    worker.run_forever()
        except KeyboardInterrupt:
            clear_line()
            worker.stop()
            print("Sync worker termination requested. Exiting cleanly.")

    sync_parser.set_defaults(
        func=lambda args: sync_worker(
            study_key=args.study_key, interval=args.interval, once=args.once
        )
    )

    # State ledger subcommands
    state_parser = sub.add_parser("state", help="Manage high-water mark execution ledger state.")
    state_sub = state_parser.add_subparsers(dest="state_command")
    _register_state_subcommands(state_sub)

    # 9. uat -> uat run
    uat_parser = sub.add_parser("uat", help="Manage and execute UAT acceptance test pipelines.")
    uat_sub = uat_parser.add_subparsers(dest="uat_command")

    uat_run_parser = uat_sub.add_parser("run", help="Run the end-to-end UAT pipeline for a study.")
    uat_run_parser.add_argument("study_key", nargs="?", default=None, help=STUDY_KEY_ARG)
    uat_run_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    uat_run_parser.add_argument(
        "--subject-count", type=int, default=2, help="Number of test subjects to generate."
    )
    uat_run_parser.add_argument(
        "--site-name", default=None, help="Target site name for synthetic subjects."
    )
    uat_run_parser.add_argument(
        "--form-key",
        action="append",
        dest="enabled_form_keys",
        help="Target form key to include. Repeat for multiple forms.",
    )
    uat_run_parser.add_argument(
        "--email-notify",
        default=None,
        help="Optional email notification destination or flag.",
    )
    add_output_format(uat_run_parser)

    @with_sdk
    def uat_run(
        sdk: ImednetSDK,
        study_key: str | None,
        interactive: bool = False,
        subject_count: int = 2,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        email_notify: bool | str | None = None,
        output_format: str = "table",
        no_interactive: bool = False,
    ) -> None:
        from .uat import UATWorkflow, UATWorkflowState
        from .wizard import (
            ensure_interactive_stdin,
            print_progress,
            prompt_input,
            prompt_positive_int,
            prompt_study_key,
        )

        if interactive:
            ensure_interactive_stdin(force_non_interactive=no_interactive)
            study_key = prompt_study_key(study_key)
            subject_count = prompt_positive_int(
                "Enter number of test subjects", default=subject_count
            )
            site_input = prompt_input(
                "Enter target site name (leave blank for study default)",
                default=site_name or "",
            )
            site_name = site_input if site_input else None
            form_input = prompt_input(
                "Enter target form keys (comma-separated, leave blank for all forms)",
                default=",".join(enabled_form_keys) if enabled_form_keys else "",
            )
            if form_input:
                enabled_form_keys = [k.strip() for k in form_input.split(",") if k.strip()]
            else:
                enabled_form_keys = None
        elif not study_key:
            uat_run_parser.error("the following arguments are required: study_key")

        phase_descriptions = {
            UATWorkflowState.INSPECTING: "[Phase 1/5] Inspecting study schema and metadata...",
            UATWorkflowState.BUILDING_SPEC: "[Phase 2/5] Building test specification...",
            UATWorkflowState.GENERATING: "[Phase 3/5] Generating synthetic test records...",
            UATWorkflowState.SUBMITTING: "[Phase 4/5] Submitting records to iMednet API...",
            UATWorkflowState.MONITORING: "[Phase 5/5] Monitoring asynchronous job completion...",
            UATWorkflowState.COMPLETED: "[Complete] UAT pipeline execution finished successfully.",
            UATWorkflowState.FAILED: "[Failed] UAT pipeline execution encountered an error.",
        }

        def on_phase(phase: UATWorkflowState) -> None:
            msg = phase_descriptions.get(phase, f"UAT Phase transition: {phase.value}")
            print_progress(msg)

        def on_progress(batch_id: str, status: Any, elapsed: float) -> None:
            state_val = getattr(status, "state", getattr(status, "status", str(status)))
            print_progress(f"[Job Progress] Batch {batch_id}: {state_val} ({elapsed:.1f}s)")

        print_progress(f"Starting UAT pipeline for study '{study_key}'...")
        workflow = UATWorkflow(
            sdk,  # type: ignore[arg-type]
            on_progress=on_progress,
            on_phase_change=on_phase,
        )
        result = workflow.run(
            study_key=study_key,  # type: ignore[arg-type]
            subject_count=subject_count,
            site_name=site_name,
            enabled_form_keys=enabled_form_keys,
            email_notify=email_notify,
        )

        if output_format == "json":
            emit_json([result], "uat_run")
        else:
            print(result.summary())
        if not result.overall_success:
            sys.exit(1)

    def _handle_uat_run(args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        if args.interactive:
            from .wizard import ensure_interactive_stdin

            ensure_interactive_stdin(force_non_interactive=no_interactive)
        uat_run(
            study_key=args.study_key,
            interactive=args.interactive,
            subject_count=args.subject_count,
            site_name=args.site_name,
            enabled_form_keys=args.enabled_form_keys,
            email_notify=args.email_notify,
            output_format=args.output_format,
            no_interactive=no_interactive,
        )

    uat_run_parser.set_defaults(func=_handle_uat_run)


def state_app(args: list[str] | None = None) -> None:
    """Execute the state app logic directly for tests."""
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(description="Manage high-water mark execution ledger state.")
    add_no_interactive(parser, suppress_default=False)
    state_sub = parser.add_subparsers(dest="state_command")
    _register_state_subcommands(state_sub)

    parsed = parser.parse_args(args)
    old_non_interactive = os.environ.get(NON_INTERACTIVE_ENV)
    try:
        if getattr(parsed, "no_interactive", False):
            os.environ[NON_INTERACTIVE_ENV] = "1"
        if hasattr(parsed, "func"):
            parsed.func(parsed)
        else:
            parser.print_help()
    finally:
        if old_non_interactive is not None:
            os.environ[NON_INTERACTIVE_ENV] = old_non_interactive
        else:
            os.environ.pop(NON_INTERACTIVE_ENV, None)
