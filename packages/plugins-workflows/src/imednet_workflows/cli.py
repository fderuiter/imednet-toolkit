"""CLI commands for iMednet workflows.

This module provides the Typer/argparse integration for common clinical data
workflows, including record extraction, cache synchronization, and state
ledger management.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from typing import Any

from imednet.sdk import ImednetSDK
from imednet.spi.cli import (
    NON_INTERACTIVE_ENV,
    STUDY_KEY_ARG,
    add_no_interactive,
    clear_line,
    console,
    parse_filter_args,
    with_sdk,
)

from .data_extraction import DataExtractionWorkflow
from .state_ledger import DEFAULT_LEDGER_PATH, get_state_provider
from .subject_data import SubjectDataWorkflow
from .sync_worker import SyncWorker, SyncWorkerConfig


def _execute_set_state(
    study_key: str | None,
    stream: str | None,
    timestamp: str | None,
    records_processed: int,
    ledger_path: str,
    interactive: bool = False,
    parser: argparse.ArgumentParser | None = None,
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
        print(f"Failed to write ledger: {err}")
        sys.exit(1)


def _execute_reset_state(
    study_key: str | None,
    stream: str | None,
    ledger_path: str,
    interactive: bool = False,
    parser: argparse.ArgumentParser | None = None,
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


def setup_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Set up the workflows argparse subparsers."""
    wf_parser = subparsers.add_parser("workflows", help="Execute common data workflows.")
    sub = wf_parser.add_subparsers(dest="wf_command")

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
    add_no_interactive(extract_parser)

    @with_sdk
    def extract_records(
        sdk: ImednetSDK,
        study_key: str | None,
        record_filter: list[str] | None = None,
        subject_filter: list[str] | None = None,
        visit_filter: list[str] | None = None,
        interactive: bool = False,
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

        if records:
            print(f"Found {len(records)} matching records:")
            print(records)
        else:
            print("No records found matching the criteria.")

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
            no_interactive=no_interactive,
        )

    extract_parser.set_defaults(func=_handle_extract_records)

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
    show_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_no_interactive(show_parser)

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
    add_no_interactive(set_parser)

    set_parser.set_defaults(
        func=lambda args: _execute_set_state(
            study_key=args.study_key,
            stream=args.stream,
            timestamp=args.timestamp,
            records_processed=args.records_processed,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=set_parser,
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
    add_no_interactive(reset_parser)

    reset_parser.set_defaults(
        func=lambda args: _execute_reset_state(
            study_key=args.study_key,
            stream=args.stream,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=reset_parser,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )

    # UAT command integration
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
    add_no_interactive(uat_run_parser)

    @with_sdk
    def uat_run(
        sdk: ImednetSDK,
        study_key: str | None,
        interactive: bool = False,
        subject_count: int = 2,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        email_notify: bool | str | None = None,
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
            no_interactive=no_interactive,
        )

    uat_run_parser.set_defaults(func=_handle_uat_run)


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
    add_no_interactive(subj_parser)

    @with_sdk
    def subject_data(sdk: ImednetSDK, study_key: str, subject_key: str) -> None:
        workflow = SubjectDataWorkflow(sdk)  # type: ignore[arg-type]
        data = workflow.get_all_subject_data(study_key, subject_key)
        print(data.model_dump())

    def handle_subject_data(args: argparse.Namespace) -> None:
        flag_study: str | None = args.study_key_flag
        flag_subject: str | None = args.subject_key_flag
        pos_study: str | None = args.study_key
        pos_subject: str | None = args.subject_key

        def _is_empty(val: str | None) -> bool:
            return val is None or not val.strip()

        # Determine study_key and subject_key based on flags, positionals, and hybrid grammar
        if flag_study is not None and flag_subject is not None:
            # Flags take precedence over positionals
            study_key = flag_study
            subject_key = flag_subject
        elif flag_study is not None and flag_subject is None:
            # Study key provided by flag; subject key must come from positional
            study_key = flag_study
            if pos_subject is not None and pos_study is not None:
                # Two positionals provided: pos_study is study_key, pos_subject is subject_key
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
                # Single positional provided: deterministically map to missing subject_key
                subject_key = pos_study
            else:
                subject_key = None
        elif flag_study is None and flag_subject is not None:
            # Subject key provided by flag; study key must come from positional
            subject_key = flag_subject
            if pos_study is not None and pos_subject is not None:
                # Two positionals provided: pos_study is study_key, pos_subject is subject_key
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
                # Single positional provided: deterministically map to missing study_key
                study_key = pos_study
            else:
                study_key = None
        else:
            # Both flags missing: both keys come from positionals
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
        subject_data(study_key=study_key, subject_key=subject_key)

    subj_parser.set_defaults(func=handle_subject_data)


def state_app(args: list[str] | None = None) -> None:
    """Execute the state app logic directly for tests."""
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(description="Manage high-water mark execution ledger state.")
    add_no_interactive(parser, suppress_default=False)
    state_sub = parser.add_subparsers(dest="state_command")

    # Show parser
    show_parser = state_sub.add_parser("show")
    show_parser.add_argument("-l", "--ledger-path", default=DEFAULT_LEDGER_PATH)
    show_parser.add_argument("-s", "--study-key")
    show_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_no_interactive(show_parser)

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
    set_parser.add_argument("-s", "--study-key")
    set_parser.add_argument("-m", "--stream")
    set_parser.add_argument("-t", "--timestamp")
    set_parser.add_argument("-r", "--records-processed", type=int, default=0)
    set_parser.add_argument("-l", "--ledger-path", default=DEFAULT_LEDGER_PATH)
    set_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_no_interactive(set_parser)

    set_parser.set_defaults(
        func=lambda args: _execute_set_state(
            study_key=args.study_key,
            stream=args.stream,
            timestamp=args.timestamp,
            records_processed=args.records_processed,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=set_parser,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )

    reset_parser = state_sub.add_parser("reset")
    reset_parser.add_argument("-s", "--study-key")
    reset_parser.add_argument("-m", "--stream")
    reset_parser.add_argument("-l", "--ledger-path", default=DEFAULT_LEDGER_PATH)
    reset_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_no_interactive(reset_parser)

    reset_parser.set_defaults(
        func=lambda args: _execute_reset_state(
            study_key=args.study_key,
            stream=args.stream,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=reset_parser,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )

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
