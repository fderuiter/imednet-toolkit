"""CLI commands for iMednet workflows.

This module provides standard-library ``argparse`` integration for all clinical
data workflows in ``imednet-workflows`` plus local SQLite ``TriageStore`` queue
management, adhering to ADR 0007 exit codes and v1.0 JSON output envelopes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imednet.sdk import ImednetSDK
from imednet.spi.cli import (
    STUDY_KEY_ARG,
    add_output_format,
    clear_line,
    console,
    display_list,
    emit_json,
    parse_filter_args,
    print_error,
    with_sdk,
)
from imednet.spi.jobs import record_study_job
from imednet.spi.models import (
    InvalidStateTransitionError,
    RegisterSubjectRequest,
    TriageItem,
    TriageStatus,
    validate_transition,
)

from .data_extraction import DataExtractionWorkflow
from .query_management import QueryManagementWorkflow
from .record_mapper import RecordMapper
from .record_update import RecordUpdateWorkflow
from .register_subjects import RegisterSubjectsWorkflow
from .state_ledger import DEFAULT_LEDGER_PATH, get_state_provider
from .subject_data import SubjectDataWorkflow
from .sync_worker import SyncWorker, SyncWorkerConfig
from .triage_store import TriageStore


def get_default_triage_db_path() -> Path:
    """Resolve the default SQLite file path for TriageStore CLI commands."""
    env_path = os.environ.get("IMEDNET_TRIAGE_DB_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".imednet" / "triage.db"


def _load_json_payload(
    parser: argparse.ArgumentParser,
    payload: str | None,
    payload_file: str | None,
    *,
    required: bool = True,
) -> Any:
    """Load JSON input from ``--payload`` or ``--payload-file``, exiting 2 on usage errors."""
    if payload and payload_file:
        parser.error("provide either --payload or --payload-file, not both")
    raw_text: str | None = None
    if payload_file:
        file_path = Path(payload_file).expanduser()
        if not file_path.is_file():
            print_error(f"Error: Payload file not found: {file_path}")
            sys.exit(1)
        try:
            raw_text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            print_error(f"Error reading payload file '{file_path}': {exc}")
            sys.exit(1)
    elif payload is not None:
        raw_text = payload

    if raw_text is None or not raw_text.strip():
        if required:
            parser.error("one of --payload or --payload-file is required")
        return None

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        parser.error(f"invalid JSON payload: {exc.msg}")
        return None


def _execute_show_state(ledger_path: str, study_key: str | None = None) -> None:
    """Display pipeline state ledger entries for all or a single study."""
    ledger = get_state_provider(ledger_path)
    try:
        state = ledger.read_state()
    except Exception as err:
        print(f"Failed to read ledger from {ledger_path}: {err}")
        sys.exit(1)

    has_data = False
    print(f"iMednet Extraction Ledger ({ledger_path})")
    print(
        "Study Key | Stream Name | Last Timestamp (UTC) | Records Processed | Status | Error Message"
    )
    for s_key, study_state in state.studies.items():
        if study_key and s_key != study_key:
            continue
        for stream_name, stream_state in study_state.streams.items():
            has_data = True
            print(
                f"{s_key} | {stream_name} | {stream_state.last_timestamp.isoformat()} | "
                f"{stream_state.records_processed} | {stream_state.last_run_status} | "
                f"{stream_state.error_message or ''}"
            )

    if not has_data:
        print("No ledger entries found matching filters.")


def _execute_set_state(
    study_key: str | None,
    stream: str | None,
    timestamp: str | None,
    records_processed: int,
    ledger_path: str,
    interactive: bool = False,
    parser: argparse.ArgumentParser | None = None,
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
            print(f"Error: {msg}", file=sys.stderr)
            sys.exit(2)

        try:
            normalized = timestamp.replace("Z", "+00:00")  # type: ignore[union-attr]
            dt = datetime.fromisoformat(normalized)
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError as err:
            print(f"Invalid ISO timestamp format: {err}")
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
            print(
                f"Successfully set high-water mark for '{study_key}' -> '{stream}' to {dt.isoformat()}"
            )
        except Exception as err:
            print(f"Failed to write ledger: {err}")
            sys.exit(1)
        return

    from .wizard import (
        display_ledger_streams,
        print_progress,
        prompt_confirm,
        prompt_iso8601_timestamp,
        prompt_non_negative_int,
        prompt_stream_name,
        prompt_study_key,
        validate_iso8601_timestamp,
    )

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
        print(f"Failed to write ledger: {err}")
        sys.exit(1)


def _execute_reset_state(
    study_key: str | None,
    stream: str | None,
    ledger_path: str,
    interactive: bool = False,
    parser: argparse.ArgumentParser | None = None,
) -> None:
    """Execute high-water mark state deletion for a study/stream."""
    if interactive:
        from .wizard import print_progress, prompt_confirm, prompt_study_key

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
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(2)

    ledger = get_state_provider(ledger_path)
    try:
        if stream:
            removed = ledger.delete_entry(study_key, stream)
            if removed:
                print(f"Successfully reset stream '{stream}' for study '{study_key}'.")
            else:
                print(f"No stream '{stream}' found for study '{study_key}'.")
                return
        else:
            removed = ledger.delete_entry(study_key)
            if removed:
                print(f"Successfully reset all streams for study '{study_key}'.")
            else:
                print(f"No state found for study '{study_key}'.")
                return
    except Exception as err:
        print(f"Failed to reset ledger state: {err}")
        sys.exit(1)


def _register_state_subcommands(state_sub: argparse._SubParsersAction[Any]) -> None:
    """Register ``show``, ``set``, and ``reset`` subcommands on a state subparser."""
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
    show_parser.set_defaults(
        func=lambda args: _execute_show_state(
            ledger_path=args.ledger_path, study_key=args.study_key
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
    set_parser.set_defaults(
        func=lambda args: _execute_set_state(
            study_key=args.study_key,
            stream=args.stream,
            timestamp=args.timestamp,
            records_processed=args.records_processed,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=set_parser,
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
    reset_parser.set_defaults(
        func=lambda args: _execute_reset_state(
            study_key=args.study_key,
            stream=args.stream,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=reset_parser,
        )
    )


def _register_query_subcommands(sub: argparse._SubParsersAction[Any]) -> None:
    """Register ``queries`` subcommands wrapping ``QueryManagementWorkflow``."""
    queries_parser = sub.add_parser(
        "queries",
        aliases=["query-management", "query_management"],
        help="Inspect open queries, subject/site queries, and discrepancy state counts.",
    )
    q_sub = queries_parser.add_subparsers(dest="queries_command")
    queries_parser.set_defaults(func=lambda _args: queries_parser.print_help())

    open_p = q_sub.add_parser("open", help="List open queries for a study.")
    open_p.add_argument("study_key", help=STUDY_KEY_ARG)
    open_p.add_argument("--filter", action="append", help="Additional filter criteria (key=value).")
    add_output_format(open_p)

    @with_sdk
    def run_open_queries(
        sdk: ImednetSDK,
        study_key: str,
        filters: list[str] | None = None,
        output_format: str = "table",
    ) -> None:
        wf = QueryManagementWorkflow(sdk)  # type: ignore[arg-type]
        items = wf.get_open_queries(study_key, additional_filter=parse_filter_args(filters))
        display_list(
            items,
            "open queries",
            fields=["annotation_id", "subject_key", "variable", "status", "description"],
            output_format=output_format,
            interactive=False,
        )

    open_p.set_defaults(
        func=lambda args: run_open_queries(
            study_key=args.study_key,
            filters=args.filter,
            output_format=args.output_format,
        )
    )

    subj_p = q_sub.add_parser("by-subject", help="List queries for a clinical subject.")
    subj_p.add_argument("study_key", help=STUDY_KEY_ARG)
    subj_p.add_argument("subject_key", help="Clinical subject key.")
    subj_p.add_argument("--filter", action="append", help="Additional filter criteria (key=value).")
    add_output_format(subj_p)

    @with_sdk
    def run_subject_queries(
        sdk: ImednetSDK,
        study_key: str,
        subject_key: str,
        filters: list[str] | None = None,
        output_format: str = "table",
    ) -> None:
        wf = QueryManagementWorkflow(sdk)  # type: ignore[arg-type]
        items = wf.get_queries_for_subject(
            study_key,
            subject_key,
            additional_filter=parse_filter_args(filters),
        )
        display_list(
            items,
            "subject queries",
            fields=["annotation_id", "subject_key", "variable", "status", "description"],
            output_format=output_format,
            interactive=False,
        )

    subj_p.set_defaults(
        func=lambda args: run_subject_queries(
            study_key=args.study_key,
            subject_key=args.subject_key,
            filters=args.filter,
            output_format=args.output_format,
        )
    )

    site_p = q_sub.add_parser("by-site", help="List queries for a clinical site.")
    site_p.add_argument("study_key", help=STUDY_KEY_ARG)
    site_p.add_argument("site_name", help="Clinical site name.")
    site_p.add_argument("--filter", action="append", help="Additional filter criteria (key=value).")
    add_output_format(site_p)

    @with_sdk
    def run_site_queries(
        sdk: ImednetSDK,
        study_key: str,
        site_name: str,
        filters: list[str] | None = None,
        output_format: str = "table",
    ) -> None:
        wf = QueryManagementWorkflow(sdk)  # type: ignore[arg-type]
        items = wf.get_queries_by_site(
            study_key,
            site_name,
            additional_filter=parse_filter_args(filters),
        )
        display_list(
            items,
            "site queries",
            fields=["annotation_id", "subject_key", "variable", "status", "description"],
            output_format=output_format,
            interactive=False,
        )

    site_p.set_defaults(
        func=lambda args: run_site_queries(
            study_key=args.study_key,
            site_name=args.site_name,
            filters=args.filter,
            output_format=args.output_format,
        )
    )

    counts_p = q_sub.add_parser("counts", help="Count queries grouped by QueryStatus.")
    counts_p.add_argument("study_key", help=STUDY_KEY_ARG)
    counts_p.add_argument(
        "--filter", action="append", help="Additional filter criteria (key=value)."
    )
    add_output_format(counts_p)

    @with_sdk
    def run_query_counts(
        sdk: ImednetSDK,
        study_key: str,
        filters: list[str] | None = None,
        output_format: str = "table",
    ) -> None:
        wf = QueryManagementWorkflow(sdk)  # type: ignore[arg-type]
        extra = parse_filter_args(filters) or {}
        counts = dict(wf.get_query_state_counts(study_key, **extra))
        rows = [{"status": k, "count": v} for k, v in counts.items()]
        display_list(
            rows,
            "query_counts",
            fields=["status", "count"],
            output_format=output_format,
            interactive=False,
        )

    counts_p.set_defaults(
        func=lambda args: run_query_counts(
            study_key=args.study_key,
            filters=args.filter,
            output_format=args.output_format,
        )
    )


def _register_triage_subcommands(sub: argparse._SubParsersAction[Any]) -> None:
    """Register headless ``triage`` queue subcommands backed by ``TriageStore``."""
    triage_parser = sub.add_parser(
        "triage",
        help="Manage local SQLite clinical triage review queues and status transitions.",
    )
    t_sub = triage_parser.add_subparsers(dest="triage_command")
    triage_parser.set_defaults(func=lambda _args: triage_parser.print_help())

    def _add_db_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db-path",
            default=None,
            help="Path to SQLite triage database (defaults to IMEDNET_TRIAGE_DB_PATH or ~/.imednet/triage.db).",
        )

    def _open_store(raw_db_path: str | None) -> TriageStore:
        resolved = Path(raw_db_path).expanduser() if raw_db_path else get_default_triage_db_path()
        return TriageStore(resolved)

    list_p = t_sub.add_parser("list", help="List triage items for a study.")
    list_p.add_argument("study_key", help=STUDY_KEY_ARG)
    list_p.add_argument(
        "--status",
        choices=[st.value for st in TriageStatus],
        default=None,
        help="Filter queue by TriageStatus.",
    )
    _add_db_arg(list_p)
    add_output_format(list_p)

    def handle_list(args: argparse.Namespace) -> None:
        try:
            store = _open_store(args.db_path)
            status_enum = TriageStatus(args.status) if args.status else None
            items = store.get_queue(args.study_key, status=status_enum)
            display_list(
                items,
                "triage_items",
                empty_msg=f"No triage items found for study '{args.study_key}'.",
                fields=["item_id", "study_key", "status", "severity", "assignee"],
                output_format=args.output_format,
                interactive=False,
            )
        except Exception as exc:
            print_error(f"Error listing triage queue: {exc}")
            sys.exit(1)

    list_p.set_defaults(func=handle_list)

    show_p = t_sub.add_parser(
        "show", help="Show a single triage item with annotations and history."
    )
    show_p.add_argument("item_id", help="Triage item identifier.")
    _add_db_arg(show_p)
    add_output_format(show_p)

    def handle_show(args: argparse.Namespace) -> None:
        try:
            store = _open_store(args.db_path)
            item = store.get_triage_item(args.item_id)
            if item is None:
                print_error(f"Error: Unknown triage item '{args.item_id}'.")
                sys.exit(1)
            if args.output_format == "json":
                emit_json([item], "triage_items")
                return
            print(
                f"Item: {item.item_id} | Study: {item.study_key} | Status: {item.status.value} "
                f"| Severity: {item.severity} | Assignee: {item.assignee or '-'}"
            )
            print(f"Annotations ({len(item.annotations)}):")
            for ann in item.annotations:
                print(f"  - [{ann.timestamp.isoformat()}] {ann.user_id}: {ann.comment}")
            print(f"History ({len(item.history)}):")
            for entry in item.history:
                print(
                    f"  - [{entry.timestamp.isoformat()}] {entry.from_status.value} -> "
                    f"{entry.to_status.value} by {entry.user_id} ({entry.comment or '-'})"
                )
        except Exception as exc:
            print_error(f"Error showing triage item: {exc}")
            sys.exit(1)

    show_p.set_defaults(func=handle_show)

    add_p = t_sub.add_parser("add", help="Create or upsert a triage item in a study queue.")
    add_p.add_argument("study_key", help=STUDY_KEY_ARG)
    add_p.add_argument("item_id", help="Unique triage item identifier.")
    add_p.add_argument(
        "--severity",
        default="MEDIUM",
        choices=["LOW", "MEDIUM", "HIGH", "CRITICAL"],
        help="Clinical triage severity.",
    )
    add_p.add_argument("--assignee", default=None, help="Optional reviewer username.")
    add_p.add_argument("--comment", default=None, help="Optional initial reviewer comment.")
    add_p.add_argument("--user-id", default="cli-user", help="Author user ID for initial comment.")
    _add_db_arg(add_p)
    add_output_format(add_p)

    def handle_add(args: argparse.Namespace) -> None:
        try:
            store = _open_store(args.db_path)
            item = TriageItem(
                item_id=args.item_id,
                study_key=args.study_key,
                status=TriageStatus.NEW,
                assignee=args.assignee,
                severity=args.severity,
            )
            store.upsert_item(item)
            if args.comment and args.comment.strip():
                store.add_annotation(args.item_id, args.user_id, args.comment.strip())
            saved = store.get_triage_item(args.item_id) or item
            if args.output_format == "json":
                emit_json([saved], "triage_items")
            else:
                print(
                    f"Created triage item '{saved.item_id}' in study '{saved.study_key}' "
                    f"[{saved.status.value}]."
                )
        except Exception as exc:
            print_error(f"Error creating triage item: {exc}")
            sys.exit(1)

    add_p.set_defaults(func=handle_add)

    trans_p = t_sub.add_parser(
        "transition",
        help="Transition a triage item status with a required reviewer comment.",
    )
    trans_p.add_argument("item_id", help="Triage item identifier.")
    trans_p.add_argument(
        "to_status",
        choices=[st.value for st in TriageStatus],
        help="Target TriageStatus.",
    )
    trans_p.add_argument(
        "--comment",
        required=True,
        help="Reviewer decision comment documenting the status transition.",
    )
    trans_p.add_argument("--user-id", default="cli-user", help="Reviewer user identifier.")
    _add_db_arg(trans_p)
    add_output_format(trans_p)

    def handle_transition(args: argparse.Namespace) -> None:
        if not args.comment or not args.comment.strip():
            trans_p.error("--comment must not be empty")
        try:
            store = _open_store(args.db_path)
            existing = store.get_triage_item(args.item_id)
            if existing is None:
                print_error(f"Error: Unknown triage item '{args.item_id}'.")
                sys.exit(1)
            target = TriageStatus(args.to_status)
            validate_transition(existing.status, target)
            store.update_status(args.item_id, target, args.user_id, args.comment.strip())
            updated = store.get_triage_item(args.item_id)
            if args.output_format == "json":
                emit_json([updated] if updated else [], "triage_items")
            else:
                print(f"Transitioned '{args.item_id}' to {target.value}.")
        except (InvalidStateTransitionError, ValueError) as exc:
            print_error(f"Error: {exc}")
            sys.exit(1)
        except Exception as exc:
            print_error(f"Error updating triage status: {exc}")
            sys.exit(1)

    trans_p.set_defaults(func=handle_transition)

    assign_p = t_sub.add_parser("assign", help="Assign a triage item to a reviewer.")
    assign_p.add_argument("item_id", help="Triage item identifier.")
    assign_p.add_argument("assignee", help="Reviewer user identifier.")
    _add_db_arg(assign_p)
    add_output_format(assign_p)

    def handle_assign(args: argparse.Namespace) -> None:
        try:
            store = _open_store(args.db_path)
            store.assign_item(args.item_id, args.assignee)
            updated = store.get_triage_item(args.item_id)
            if args.output_format == "json":
                emit_json([updated] if updated else [], "triage_items")
            else:
                print(f"Assigned '{args.item_id}' to '{args.assignee}'.")
        except Exception as exc:
            print_error(f"Error assigning triage item: {exc}")
            sys.exit(1)

    assign_p.set_defaults(func=handle_assign)

    ann_p = t_sub.add_parser("annotate", help="Add a reviewer comment to a triage item.")
    ann_p.add_argument("item_id", help="Triage item identifier.")
    ann_p.add_argument("--comment", required=True, help="Reviewer annotation comment.")
    ann_p.add_argument("--user-id", default="cli-user", help="Reviewer user identifier.")
    _add_db_arg(ann_p)
    add_output_format(ann_p)

    def handle_annotate(args: argparse.Namespace) -> None:
        if not args.comment or not args.comment.strip():
            ann_p.error("--comment must not be empty")
        try:
            store = _open_store(args.db_path)
            store.add_annotation(args.item_id, args.user_id, args.comment.strip())
            updated = store.get_triage_item(args.item_id)
            if args.output_format == "json":
                emit_json([updated] if updated else [], "triage_items")
            else:
                print(f"Added annotation to '{args.item_id}'.")
        except Exception as exc:
            print_error(f"Error annotating triage item: {exc}")
            sys.exit(1)

    ann_p.set_defaults(func=handle_annotate)


def setup_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Set up the workflows argparse subparsers."""
    wf_parser = subparsers.add_parser("workflows", help="Execute common data workflows.")
    sub = wf_parser.add_subparsers(dest="wf_command")
    wf_parser.set_defaults(func=lambda _args: wf_parser.print_help())

    # 1a. data_extraction: extract-records
    extract_parser = sub.add_parser(
        "extract-records",
        aliases=["data-extraction", "data_extraction"],
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
    ) -> None:
        from imednet.spi.models import Record, Subject, Visit

        from .wizard import print_progress

        if interactive:
            from .wizard import prompt_filters, prompt_study_key

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

        if output_format != "json":
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
            emit_json(records, "records")
        elif records:
            print(f"Found {len(records)} matching records:")
            print(records)
        else:
            print("No records found matching the criteria.")

    extract_parser.set_defaults(
        func=lambda args: extract_records(
            study_key=args.study_key,
            record_filter=args.record_filter,
            subject_filter=args.subject_filter,
            visit_filter=args.visit_filter,
            interactive=args.interactive,
            output_format=args.output_format,
        )
    )

    # 1b. data_extraction: extract-audit
    audit_parser = sub.add_parser(
        "extract-audit",
        help="Extract 21 CFR Part 11 record revision audit trail entries for a study.",
    )
    audit_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    audit_parser.add_argument("--start-date", default=None, help="Start date filter (YYYY-MM-DD).")
    audit_parser.add_argument("--end-date", default=None, help="End date filter (YYYY-MM-DD).")
    audit_parser.add_argument(
        "--filter", action="append", help="Additional audit filter criteria (key=value)."
    )
    add_output_format(audit_parser)

    @with_sdk
    def extract_audit(
        sdk: ImednetSDK,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        filters: list[str] | None = None,
        output_format: str = "table",
    ) -> None:
        workflow = DataExtractionWorkflow(sdk)  # type: ignore[arg-type]
        revisions = workflow.extract_audit_trail(
            study_key=study_key,
            start_date=start_date,
            end_date=end_date,
            user_filter=parse_filter_args(filters),
        )
        display_list(
            revisions,
            "record_revisions",
            fields=[
                "record_revision_id",
                "record_id",
                "subject_key",
                "record_status",
                "user",
                "date_created",
            ],
            output_format=output_format,
            interactive=False,
        )

    audit_parser.set_defaults(
        func=lambda args: extract_audit(
            study_key=args.study_key,
            start_date=args.start_date,
            end_date=args.end_date,
            filters=args.filter,
            output_format=args.output_format,
        )
    )

    # 2. duckdb_centralizer
    duck_parser = sub.add_parser(
        "duckdb-centralize",
        aliases=["duckdb-centralizer", "duckdb_centralizer"],
        help="Ingest study record revisions into bronze/silver DuckDB tables.",
    )
    duck_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    duck_parser.add_argument(
        "--db-path",
        default=":memory:",
        help="Target DuckDB file path or ':memory:' (default: ':memory:').",
    )
    duck_parser.add_argument("--start-date", default=None, help="Start date filter (YYYY-MM-DD).")
    duck_parser.add_argument("--end-date", default=None, help="End date filter (YYYY-MM-DD).")
    duck_parser.add_argument(
        "--mode",
        choices=["append", "replace"],
        default="append",
        help="Table write mode for bronze_revisions.",
    )
    duck_parser.add_argument(
        "--skip-silver",
        action="store_true",
        help="Skip building the deduplicated silver_current_state view.",
    )
    add_output_format(duck_parser)

    @with_sdk
    def run_duckdb_centralizer(
        sdk: ImednetSDK,
        study_key: str,
        db_path: str = ":memory:",
        start_date: str | None = None,
        end_date: str | None = None,
        mode: str = "append",
        skip_silver: bool = False,
        output_format: str = "table",
    ) -> None:
        try:
            from .duckdb_centralizer import DuckDBIngestionWorkflow

            wf = DuckDBIngestionWorkflow(sdk, db_path=db_path)  # type: ignore[arg-type]
            ingested = wf.ingest_revisions(
                study_key,
                start_date=start_date,
                end_date=end_date,
                mode=mode,  # type: ignore[arg-type]
            )
            silver_built = False
            if not skip_silver:
                wf.build_silver_view(study_key)
                silver_built = True
            summary_row = {
                "study_key": study_key,
                "db_path": db_path,
                "mode": mode,
                "rows_ingested": ingested,
                "silver_view_built": silver_built,
            }
            if output_format == "json":
                emit_json([summary_row], "duckdb_centralizer")
            else:
                print(
                    f"Centralized {ingested} revision row(s) for study '{study_key}' "
                    f"into DuckDB ({db_path}, mode={mode}, silver_view={silver_built})."
                )
        except ImportError as exc:
            print_error(f"Error: {exc}")
            sys.exit(1)

    duck_parser.set_defaults(
        func=lambda args: run_duckdb_centralizer(
            study_key=args.study_key,
            db_path=args.db_path,
            start_date=args.start_date,
            end_date=args.end_date,
            mode=args.mode,
            skip_silver=args.skip_silver,
            output_format=args.output_format,
        )
    )

    # 3. query_management
    _register_query_subcommands(sub)

    # 4. record_mapper
    mapper_parser = sub.add_parser(
        "map-records",
        aliases=["record-mapper", "record_mapper"],
        help="Map EDC records into tabular DataFrames, CDISC SDTM domains, or hierarchies.",
    )
    mapper_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    mapper_parser.add_argument(
        "--visit-key", default=None, help="Optional numeric visit ID filter."
    )
    mapper_parser.add_argument(
        "--sdtm-domain",
        default=None,
        help="Transform records into a CDISC SDTM domain (e.g. AE, DM, VS, CM, DV, DD).",
    )
    mapper_parser.add_argument(
        "--hierarchy",
        action="store_true",
        help="Emit nested Subject -> Visit -> Form record hierarchy.",
    )
    mapper_parser.add_argument(
        "--use-variable-names",
        action="store_true",
        help="Use raw variable names instead of human labels for column headers.",
    )
    mapper_parser.add_argument(
        "--variable",
        action="append",
        dest="variables",
        help="Whitelist variable name (repeatable).",
    )
    mapper_parser.add_argument(
        "--form-id",
        action="append",
        type=int,
        dest="form_ids",
        help="Whitelist form ID integer (repeatable).",
    )
    mapper_parser.add_argument(
        "--out-file",
        default=None,
        help="Optional output file path (.csv or .json).",
    )
    add_output_format(mapper_parser)

    @with_sdk
    def run_record_mapper(
        sdk: ImednetSDK,
        study_key: str,
        visit_key: str | None = None,
        sdtm_domain: str | None = None,
        hierarchy: bool = False,
        use_variable_names: bool = False,
        variables: list[str] | None = None,
        form_ids: list[int] | None = None,
        out_file: str | None = None,
        output_format: str = "table",
    ) -> None:
        mapper = RecordMapper(sdk)  # type: ignore[arg-type]
        if hierarchy:
            tree = mapper.build_hierarchy(
                study_key=study_key,
                visit_key=visit_key,
                use_labels_as_keys=not use_variable_names,
                variable_whitelist=variables,
                form_whitelist=form_ids,
            )
            if out_file:
                Path(out_file).expanduser().write_text(
                    json.dumps(tree, indent=2, default=str),
                    encoding="utf-8",
                )
            if output_format == "json":
                emit_json([tree], "record_hierarchy")
            else:
                print(f"Mapped hierarchy for {len(tree)} subject(s) in study '{study_key}'.")
                if tree:
                    print(json.dumps(tree, indent=2, default=str))
            return

        if sdtm_domain:
            df = mapper.sdtm_dataframe(
                study_key=study_key,
                domain=sdtm_domain,
                visit_key=visit_key,
                variable_whitelist=variables,
                form_whitelist=form_ids,
            )
            resource_name = f"sdtm_{sdtm_domain.lower()}"
        else:
            df = mapper.dataframe(
                study_key=study_key,
                visit_key=visit_key,
                use_labels_as_columns=not use_variable_names,
                variable_whitelist=variables,
                form_whitelist=form_ids,
            )
            resource_name = "mapped_records"

        if out_file:
            target = Path(out_file).expanduser()
            if target.suffix.lower() == ".json":
                df.to_json(target, orient="records", indent=2)
            else:
                df.to_csv(target, index=False)

        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        display_list(
            rows,
            resource_name,
            output_format=output_format,
            interactive=False,
        )

    mapper_parser.set_defaults(
        func=lambda args: run_record_mapper(
            study_key=args.study_key,
            visit_key=args.visit_key,
            sdtm_domain=args.sdtm_domain,
            hierarchy=args.hierarchy,
            use_variable_names=args.use_variable_names,
            variables=args.variables,
            form_ids=args.form_ids,
            out_file=args.out_file,
            output_format=args.output_format,
        )
    )

    # 5. record_update
    update_parser = sub.add_parser(
        "update-records",
        aliases=["record-update", "record_update"],
        help="Validate and submit record creation/update batches via RecordUpdateWorkflow.",
    )
    update_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    update_parser.add_argument(
        "--payload",
        default=None,
        help="Inline JSON record object or array of record objects.",
    )
    update_parser.add_argument(
        "--payload-file",
        default=None,
        help="Path to JSON file containing a record object or array of record objects.",
    )
    update_parser.add_argument(
        "--wait",
        action="store_true",
        help="Poll the resulting background job until completion.",
    )
    update_parser.add_argument(
        "--timeout", type=int, default=300, help="Job polling timeout in seconds."
    )
    update_parser.add_argument(
        "--poll-interval", type=int, default=5, help="Job polling interval in seconds."
    )
    add_output_format(update_parser)

    @with_sdk
    def run_record_update(
        sdk: ImednetSDK,
        study_key: str,
        records_data: list[dict[str, Any]],
        wait: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        output_format: str = "table",
    ) -> None:
        wf = RecordUpdateWorkflow(sdk)  # type: ignore[arg-type]
        job = wf.create_or_update_records(
            study_key=study_key,
            records_data=records_data,
            wait_for_completion=wait,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        record_study_job(study_key, job, job_type="RecordUpdate", source="cli-workflow")
        if output_format == "json":
            emit_json([job], "jobs")
        else:
            print(
                f"Submitted {len(records_data)} record(s) to study '{study_key}' "
                f"(batch_id={job.batch_id or '-'})."
            )

    def handle_record_update(args: argparse.Namespace) -> None:
        parsed_payload = _load_json_payload(update_parser, args.payload, args.payload_file)
        if isinstance(parsed_payload, dict):
            records_list = [parsed_payload]
        elif isinstance(parsed_payload, list) and all(isinstance(x, dict) for x in parsed_payload):
            records_list = parsed_payload
        else:
            update_parser.error("payload must be a JSON object or array of JSON objects")
            return
        if not records_list:
            update_parser.error("payload must contain at least one record object")
        run_record_update(
            study_key=args.study_key,
            records_data=records_list,
            wait=args.wait,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            output_format=args.output_format,
        )

    update_parser.set_defaults(func=handle_record_update)

    # 6. register_subjects
    reg_parser = sub.add_parser(
        "register-subjects",
        aliases=["register_subjects"],
        help="Validate sites and register one or more subjects via RegisterSubjectsWorkflow.",
    )
    reg_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    reg_parser.add_argument(
        "--payload",
        default=None,
        help="Inline JSON subject registration object or array.",
    )
    reg_parser.add_argument(
        "--payload-file",
        default=None,
        help="Path to JSON file containing subject registration request(s).",
    )
    reg_parser.add_argument(
        "--form-key", default=None, help="Enrollment form key (single subject)."
    )
    reg_parser.add_argument("--site-name", default=None, help="Target site name (single subject).")
    reg_parser.add_argument("--subject-key", default=None, help="Subject key (single subject).")
    reg_parser.add_argument(
        "--data",
        default=None,
        help="Optional JSON object of enrollment variable values (single subject).",
    )
    reg_parser.add_argument("--email-notify", default=None, help="Optional notification email.")
    reg_parser.add_argument(
        "--wait", action="store_true", help="Wait for registration job completion."
    )
    reg_parser.add_argument("--timeout", type=int, default=300, help="Polling timeout in seconds.")
    reg_parser.add_argument(
        "--poll-interval", type=int, default=5, help="Polling interval in seconds."
    )
    add_output_format(reg_parser)

    @with_sdk
    def run_register_subjects(
        sdk: ImednetSDK,
        study_key: str,
        requests: list[RegisterSubjectRequest],
        email_notify: str | None = None,
        wait: bool = False,
        timeout: int = 300,
        poll_interval: int = 5,
        output_format: str = "table",
    ) -> None:
        wf = RegisterSubjectsWorkflow(sdk)  # type: ignore[arg-type]
        job = wf.register_subjects(
            study_key=study_key,
            subjects=requests,
            email_notify=email_notify,
            wait_for_completion=wait,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        record_study_job(study_key, job, job_type="RegisterSubjects", source="cli-workflow")
        if output_format == "json":
            emit_json([job], "jobs")
        else:
            print(
                f"Submitted {len(requests)} subject registration(s) for study '{study_key}' "
                f"(batch_id={job.batch_id or '-'})."
            )

    def handle_register_subjects(args: argparse.Namespace) -> None:
        raw_items: list[dict[str, Any]]
        if args.payload or args.payload_file:
            parsed = _load_json_payload(reg_parser, args.payload, args.payload_file)
            if isinstance(parsed, dict):
                raw_items = [parsed]
            elif isinstance(parsed, list) and all(isinstance(x, dict) for x in parsed):
                raw_items = parsed
            else:
                reg_parser.error("payload must be a JSON object or array of JSON objects")
                return
        elif args.form_key and args.site_name:
            data_dict: dict[str, Any] = {}
            if args.data:
                try:
                    loaded_data = json.loads(args.data)
                except json.JSONDecodeError as exc:
                    reg_parser.error(f"invalid --data JSON: {exc.msg}")
                    return
                if not isinstance(loaded_data, dict):
                    reg_parser.error("--data must be a JSON object")
                    return
                data_dict = loaded_data
            single_entry: dict[str, Any] = {
                "formKey": args.form_key,
                "siteName": args.site_name,
                "data": data_dict,
            }
            if args.subject_key:
                single_entry["subjectKey"] = args.subject_key
            raw_items = [single_entry]
        else:
            reg_parser.error(
                "provide --payload / --payload-file or both --form-key and --site-name"
            )
            return

        try:
            requests = [RegisterSubjectRequest.model_validate(item) for item in raw_items]
        except Exception as exc:
            print_error(f"Error validating subject registration payload: {exc}")
            sys.exit(1)

        run_register_subjects(
            study_key=args.study_key,
            requests=requests,
            email_notify=args.email_notify,
            wait=args.wait,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            output_format=args.output_format,
        )

    reg_parser.set_defaults(func=handle_register_subjects)

    # 7. subject_data (under workflows namespace as well as top-level command)
    wf_subj_parser = sub.add_parser(
        "subject-data",
        aliases=["subject_data"],
        help="Retrieve aggregated clinical dossier for a single subject.",
    )
    wf_subj_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    wf_subj_parser.add_argument("subject_key", help="The key identifying the subject.")
    add_output_format(wf_subj_parser)

    @with_sdk
    def run_wf_subject_data(
        sdk: ImednetSDK,
        study_key: str,
        subject_key: str,
        output_format: str = "table",
    ) -> None:
        workflow = SubjectDataWorkflow(sdk)  # type: ignore[arg-type]
        dossier = workflow.get_all_subject_data(study_key, subject_key)
        if output_format == "json":
            emit_json([dossier], "subject_data")
        else:
            print(dossier.model_dump())

    wf_subj_parser.set_defaults(
        func=lambda args: run_wf_subject_data(
            study_key=args.study_key,
            subject_key=args.subject_key,
            output_format=args.output_format,
        )
    )

    # Sync worker & state ledger commands
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

    state_parser = sub.add_parser("state", help="Manage high-water mark execution ledger state.")
    state_sub = state_parser.add_subparsers(dest="state_command")
    state_parser.set_defaults(func=lambda _args: state_parser.print_help())
    _register_state_subcommands(state_sub)

    # 8 & 9. uat and uat_inspector
    @with_sdk
    def run_uat_inspect(
        sdk: ImednetSDK,
        study_key: str,
        force_refresh: bool = False,
        output_format: str = "table",
    ) -> None:
        from .uat import StudySchemaInspector

        inspector = StudySchemaInspector(sdk)  # type: ignore[arg-type]
        snapshot = inspector.inspect(study_key, force_refresh=force_refresh)
        summary = {
            "study_key": snapshot.study_key,
            "captured_at": snapshot.captured_at.isoformat(),
            "forms_count": len(snapshot.forms),
            "variables_count": len(snapshot.variables),
            "intervals_count": len(snapshot.intervals),
            "sites_count": len(snapshot.sites),
            "enrollment_forms": [f.form_key for f in snapshot.enrollment_forms()],
            "scheduled_forms": [f.form_key for f in snapshot.scheduled_forms()],
            "unscheduled_forms": [f.form_key for f in snapshot.unscheduled_forms()],
            "active_sites": [s.site_name for s in snapshot.active_sites()],
        }
        if output_format == "json":
            emit_json([summary], "uat_inspector")
        else:
            print(
                f"UAT Schema Snapshot ({snapshot.study_key}): "
                f"{len(snapshot.forms)} forms, {len(snapshot.variables)} variables, "
                f"{len(snapshot.intervals)} intervals, {len(snapshot.sites)} sites "
                f"({len(snapshot.active_sites())} active)."
            )

    uat_inspect_top = sub.add_parser(
        "uat-inspect",
        aliases=["uat-inspector", "uat_inspector"],
        help="Inspect structural study metadata snapshot via StudySchemaInspector.",
    )
    uat_inspect_top.add_argument("study_key", help=STUDY_KEY_ARG)
    uat_inspect_top.add_argument(
        "--force-refresh", action="store_true", help="Bypass cached snapshot."
    )
    add_output_format(uat_inspect_top)
    uat_inspect_top.set_defaults(
        func=lambda args: run_uat_inspect(
            study_key=args.study_key,
            force_refresh=args.force_refresh,
            output_format=args.output_format,
        )
    )

    uat_parser = sub.add_parser("uat", help="Manage and execute UAT acceptance test pipelines.")
    uat_sub = uat_parser.add_subparsers(dest="uat_command")
    uat_parser.set_defaults(func=lambda _args: uat_parser.print_help())

    uat_inspect_sub = uat_sub.add_parser(
        "inspect", help="Inspect structural study metadata snapshot for UAT."
    )
    uat_inspect_sub.add_argument("study_key", help=STUDY_KEY_ARG)
    uat_inspect_sub.add_argument(
        "--force-refresh", action="store_true", help="Bypass cached snapshot."
    )
    add_output_format(uat_inspect_sub)
    uat_inspect_sub.set_defaults(
        func=lambda args: run_uat_inspect(
            study_key=args.study_key,
            force_refresh=args.force_refresh,
            output_format=args.output_format,
        )
    )

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
    ) -> None:
        from .uat import UATWorkflow, UATWorkflowState
        from .wizard import print_progress, prompt_input, prompt_positive_int, prompt_study_key

        if interactive:
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
            if output_format != "json":
                msg = phase_descriptions.get(phase, f"UAT Phase transition: {phase.value}")
                print_progress(msg)

        def on_progress(batch_id: str, status: Any, elapsed: float) -> None:
            if output_format != "json":
                state_val = getattr(status, "state", getattr(status, "status", str(status)))
                print_progress(f"[Job Progress] Batch {batch_id}: {state_val} ({elapsed:.1f}s)")

        if output_format != "json":
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

        sub_res = getattr(result, "submission_result", None)
        if sub_res is not None and study_key:
            for bid in getattr(sub_res, "all_batch_ids", []):
                record_study_job(
                    study_key,
                    {"batch_id": bid, "state": "COMPLETED" if result.overall_success else "FAILED"},
                    job_type="UATWorkflow",
                    source="cli-workflow",
                )

        if output_format == "json":
            emit_json([result], "uat_runs")
        else:
            print(result.summary())
        if not result.overall_success:
            sys.exit(1)

    uat_run_parser.set_defaults(
        func=lambda args: uat_run(
            study_key=args.study_key,
            interactive=args.interactive,
            subject_count=args.subject_count,
            site_name=args.site_name,
            enabled_form_keys=args.enabled_form_keys,
            email_notify=args.email_notify,
            output_format=args.output_format,
        )
    )

    # 10. triage (TriageStore)
    _register_triage_subcommands(sub)


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


def setup_subject_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Set up the subject-data argparse subparsers."""
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
        workflow = SubjectDataWorkflow(sdk)  # type: ignore[arg-type]
        data = workflow.get_all_subject_data(study_key, subject_key)
        if output_format == "json":
            emit_json([data], "subject_data")
        else:
            print(data.model_dump())

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
        subject_data(
            study_key=study_key,
            subject_key=subject_key,
            output_format=getattr(args, "output_format", "table"),
        )

    subj_parser.set_defaults(func=handle_subject_data)


def state_app(args: list[str] | None = None) -> None:
    """Execute the state app logic directly for tests."""
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(description="Manage high-water mark execution ledger state.")
    state_sub = parser.add_subparsers(dest="state_command")
    _register_state_subcommands(state_sub)

    parsed = parser.parse_args(args)
    if hasattr(parsed, "func"):
        parsed.func(parsed)
    else:
        parser.print_help()
