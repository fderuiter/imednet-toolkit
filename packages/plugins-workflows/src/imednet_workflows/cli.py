"""CLI commands for iMednet workflows.

This module provides the Typer/argparse integration for common clinical data
workflows, including record extraction, cache synchronization, and state
ledger management.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from imednet.sdk import ImednetSDK
from imednet.spi.cli import STUDY_KEY_ARG, parse_filter_args, with_sdk

from .data_extraction import DataExtractionWorkflow
from .state_ledger import DEFAULT_LEDGER_PATH, get_state_provider
from .subject_data import SubjectDataWorkflow
from .sync_worker import SyncWorker, SyncWorkerConfig


def setup_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Set up the workflows argparse subparsers."""
    wf_parser = subparsers.add_parser("workflows", help="Execute common data workflows.")
    sub = wf_parser.add_subparsers(dest="wf_command")

    extract_parser = sub.add_parser(
        "extract-records",
        help="Extract records based on criteria spanning subjects, visits, and records.",
    )
    extract_parser.add_argument("study_key", help=STUDY_KEY_ARG)
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
        help="Visit filter criteria (e.g., 'visit_key=SCREENING'). Repeat for multiple filters.",
    )

    @with_sdk
    def extract_records(
        sdk: ImednetSDK,
        study_key: str,
        record_filter: list[str] | None = None,
        subject_filter: list[str] | None = None,
        visit_filter: list[str] | None = None,
    ) -> None:
        workflow = DataExtractionWorkflow(sdk)  # type: ignore[arg-type]
        parsed_record_filter = parse_filter_args(record_filter)
        parsed_subject_filter = parse_filter_args(subject_filter)
        parsed_visit_filter = parse_filter_args(visit_filter)

        print(f"Extracting records for study '{study_key}'...")
        records = workflow.extract_records_by_criteria(
            study_key=study_key,
            record_filter=parsed_record_filter,
            subject_filter=parsed_subject_filter,
            visit_filter=parsed_visit_filter,
        )

        if records:
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
        )
    )

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
            worker.run_forever()
        except KeyboardInterrupt:
            worker.stop()
            print("Sync worker termination requested. Exiting cleanly.")

    sync_parser.set_defaults(
        func=lambda args: sync_worker(
            study_key=args.study_key, interval=args.interval, once=args.once
        )
    )

    state_parser = sub.add_parser("state", help="Manage high-water mark execution ledger state.")
    state_sub = state_parser.add_subparsers(dest="state_command")

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

    def show_state(ledger_path: str, study_key: str | None = None) -> None:  # pragma: no cover
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
                    f"{s_key} | {stream_name} | {stream_state.last_timestamp.isoformat()} | {stream_state.records_processed} | {stream_state.last_run_status} | {stream_state.error_message or ''}"
                )

        if not has_data:
            print("No ledger entries found matching filters.")

    show_parser.set_defaults(
        func=lambda args: show_state(ledger_path=args.ledger_path, study_key=args.study_key)
    )

    set_parser = state_sub.add_parser(
        "set", help="Manually set a high-water mark timestamp for a study and stream."
    )
    set_parser.add_argument("-s", "--study-key", required=True, help="The study key.")
    set_parser.add_argument("-m", "--stream", required=True, help="The stream name.")
    set_parser.add_argument(
        "-t", "--timestamp", required=True, help="The ISO-8601 timestamp (UTC)."
    )
    set_parser.add_argument(
        "-r", "--records-processed", type=int, default=0, help="Number of records processed."
    )
    set_parser.add_argument(
        "-l",
        "--ledger-path",
        default=DEFAULT_LEDGER_PATH,
        help="Path to the pipeline ledger JSON file.",
    )

    def set_state(
        study_key: str, stream: str, timestamp: str, records_processed: int, ledger_path: str
    ) -> None:  # pragma: no cover
        try:
            normalized = timestamp.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError as err:
            print(f"Invalid ISO timestamp format: {err}")
            sys.exit(1)

        ledger = get_state_provider(ledger_path)
        try:
            ledger.set_last_timestamp(
                study_key=study_key,
                stream_name=stream,
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

    set_parser.set_defaults(
        func=lambda args: set_state(
            study_key=args.study_key,
            stream=args.stream,
            timestamp=args.timestamp,
            records_processed=args.records_processed,
            ledger_path=args.ledger_path,
        )
    )

    reset_parser = state_sub.add_parser(
        "reset", help="Reset (clear) high-water mark state for a study/stream."
    )
    reset_parser.add_argument("-s", "--study-key", required=True, help="The study key.")
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

    def reset_state(
        study_key: str, stream: str | None, ledger_path: str
    ) -> None:  # pragma: no cover
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

    reset_parser.set_defaults(
        func=lambda args: reset_state(
            study_key=args.study_key, stream=args.stream, ledger_path=args.ledger_path
        )
    )


def setup_subject_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Set up the subject-data argparse subparsers."""
    subj_parser = subparsers.add_parser(
        "subject-data", help="Retrieve all data for a single subject."
    )
    subj_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    subj_parser.add_argument("subject_key", help="The key identifying the subject.")

    @with_sdk
    def subject_data(sdk: ImednetSDK, study_key: str, subject_key: str) -> None:
        workflow = SubjectDataWorkflow(sdk)  # type: ignore[arg-type]
        data = workflow.get_all_subject_data(study_key, subject_key)
        print(data.model_dump())

    subj_parser.set_defaults(
        func=lambda args: subject_data(study_key=args.study_key, subject_key=args.subject_key)
    )


def state_app(args: list[str] | None = None) -> None:
    """Execute the state app logic directly for tests."""
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(description="Manage high-water mark execution ledger state.")
    state_sub = parser.add_subparsers(dest="state_command")

    # We duplicate the state logic here for tests
    show_parser = state_sub.add_parser("show")
    show_parser.add_argument("-l", "--ledger-path", default=DEFAULT_LEDGER_PATH)
    show_parser.add_argument("-s", "--study-key")

    def show_state(ledger_path: str, study_key: str | None = None) -> None:
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
                    f"{s_key} | {stream_name} | {stream_state.last_timestamp.isoformat()} | {stream_state.records_processed} | {stream_state.last_run_status} | {stream_state.error_message or ''}"
                )
        if not has_data:
            print("No ledger entries found matching filters.")

    show_parser.set_defaults(
        func=lambda args: show_state(ledger_path=args.ledger_path, study_key=args.study_key)
    )

    set_parser = state_sub.add_parser("set")
    set_parser.add_argument("-s", "--study-key", required=True)
    set_parser.add_argument("-m", "--stream", required=True)
    set_parser.add_argument("-t", "--timestamp", required=True)
    set_parser.add_argument("-r", "--records-processed", type=int, default=0)
    set_parser.add_argument("-l", "--ledger-path", default=DEFAULT_LEDGER_PATH)

    def set_state(
        study_key: str, stream: str, timestamp: str, records_processed: int, ledger_path: str
    ) -> None:
        try:
            normalized = timestamp.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError as err:
            print(f"Invalid ISO timestamp format: {err}")
            sys.exit(1)
        ledger = get_state_provider(ledger_path)
        try:
            ledger.set_last_timestamp(
                study_key=study_key,
                stream_name=stream,
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

    set_parser.set_defaults(
        func=lambda args: set_state(
            study_key=args.study_key,
            stream=args.stream,
            timestamp=args.timestamp,
            records_processed=args.records_processed,
            ledger_path=args.ledger_path,
        )
    )

    reset_parser = state_sub.add_parser("reset")
    reset_parser.add_argument("-s", "--study-key", required=True)
    reset_parser.add_argument("-m", "--stream")
    reset_parser.add_argument("-l", "--ledger-path", default=DEFAULT_LEDGER_PATH)

    def reset_state(study_key: str, stream: str | None, ledger_path: str) -> None:
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

    reset_parser.set_defaults(
        func=lambda args: reset_state(
            study_key=args.study_key, stream=args.stream, ledger_path=args.ledger_path
        )
    )

    parsed = parser.parse_args(args)
    if hasattr(parsed, "func"):
        parsed.func(parsed)
    else:
        parser.print_help()
