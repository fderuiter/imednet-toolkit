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
    OUTPUT_FORMAT_ENV,
    UniqueStoreAction,
    add_no_interactive,
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    clear_line,
    cli_execution_guard,
    console,
    display_list,
    emit_json,
    fetching_status,
    is_json_output_mode,
    parse_filter_args,
    positive_int,
    print_error,
    redact_cli_secrets,
    resolve_hybrid_study_and_key,
    resolve_output_format,
    resolve_study_key,
    simulation_mode_context,
    validate_cli_path_segment,
    with_sdk,
)
from imednet.spi.utils import validate_path_segment

from .data_extraction import DataExtractionWorkflow
from .state_ledger import DEFAULT_LEDGER_PATH, get_state_provider
from .subject_data import SubjectDataWorkflow
from .sync_worker import SyncWorker, SyncWorkerConfig, SyncWorkerState


def _execute_set_state(
    study_key: str | None,
    stream: str | None,
    timestamp: str | None,
    records_processed: int,
    ledger_path: str,
    interactive: bool = False,
    parser: argparse.ArgumentParser | None = None,
    no_interactive: bool = False,
    force: bool = False,
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
                force=force,
            )
            print(
                f"Successfully set high-water mark for '{study_key}' -> '{stream}' to {dt.isoformat()}"
            )
        except Exception as err:
            print_error(f"Failed to write ledger: {err}")
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
        print_error(f"Failed to write ledger: {err}")
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
        print_error(f"Error: {msg}")
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
        print_error(f"Failed to reset ledger state: {err}")
        sys.exit(1)


def _execute_show_state(
    ledger_path: str,
    study_key: str | None = None,
    *,
    interactive: bool = False,
    no_interactive: bool = False,
    output_format: str | None = None,
) -> None:
    """Display current high-water mark records and stream metadata from the ledger."""
    resolved_format = resolve_output_format(output_format)
    if interactive and resolved_format == "json":
        print_error("Error: Cannot combine --interactive with --format json.")
        sys.exit(1)

    if interactive:
        from .wizard import ensure_interactive_stdin, prompt_input

        ensure_interactive_stdin(force_non_interactive=no_interactive)
        study_input = prompt_input(
            "Enter study key to filter (leave blank for all studies)",
            default=study_key or "",
        )
        study_key = study_input if study_input else None

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
            ts_iso = (
                stream_state.last_timestamp.isoformat()
                if stream_state.last_timestamp is not None
                else None
            )
            entries.append(
                {
                    "studyKey": s_key,
                    "streamName": stream_name,
                    "lastTimestamp": ts_iso,
                    "recordsProcessed": stream_state.records_processed,
                    "status": str(stream_state.last_run_status),
                    "errorMessage": stream_state.error_message,
                }
            )

    if resolved_format == "json":
        emit_json(entries, "ledger_state")
        return

    print(f"iMednet Extraction Ledger ({ledger_path})")
    print(
        "Study Key | Stream Name | Last Timestamp (UTC) | Records Processed | Status | Error Message"
    )
    for entry in entries:
        ts_str = entry["lastTimestamp"] if entry["lastTimestamp"] is not None else "None"
        print(
            f"{entry['studyKey']} | {entry['streamName']} | {ts_str} | {entry['recordsProcessed']} | {entry['status']} | {entry['errorMessage'] or ''}"
        )

    if not entries:
        print("No ledger entries found matching filters.")


def _configure_state_subparsers(state_parser: argparse.ArgumentParser) -> None:
    """Attach show, set, and reset subparsers to a state ArgumentParser."""
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
    add_study_key_args(show_parser, flag_help="Filter the ledger by a specific study key.")
    show_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_output_format(show_parser)

    def _dispatch_show_state(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(show_parser, args, required=False)
        _execute_show_state(
            ledger_path=args.ledger_path,
            study_key=study_key,
            interactive=args.interactive,
            no_interactive=getattr(args, "no_interactive", False),
            output_format=getattr(args, "output_format", None),
        )

    show_parser.set_defaults(func=_dispatch_show_state)

    set_parser = state_sub.add_parser(
        "set", help="Manually set a high-water mark timestamp for a study and stream."
    )
    add_study_key_args(set_parser, flag_help="The study key.")
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
        "-f",
        "--force",
        action="store_true",
        help="Force state transition override even when the current status disallows it.",
    )
    set_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_no_interactive(set_parser)

    def _dispatch_set_state(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(set_parser, args, required=False)
        _execute_set_state(
            study_key=study_key,
            stream=args.stream,
            timestamp=args.timestamp,
            records_processed=args.records_processed,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=set_parser,
            no_interactive=getattr(args, "no_interactive", False),
            force=getattr(args, "force", False),
        )

    set_parser.set_defaults(func=_dispatch_set_state)

    reset_parser = state_sub.add_parser(
        "reset", help="Reset (clear) high-water mark state for a study/stream."
    )
    add_study_key_args(reset_parser, flag_help="The study key.")
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

    def _dispatch_reset_state(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(reset_parser, args, required=False)
        _execute_reset_state(
            study_key=study_key,
            stream=args.stream,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=reset_parser,
            no_interactive=getattr(args, "no_interactive", False),
        )

    reset_parser.set_defaults(func=_dispatch_reset_state)


def setup_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Set up the workflows argparse subparsers."""
    wf_parser = subparsers.add_parser("workflows", help="Execute common data workflows.")
    sub = wf_parser.add_subparsers(dest="wf_command")

    extract_parser = sub.add_parser(
        "extract-records",
        help="Extract records based on criteria spanning subjects, visits, and records.",
    )
    add_study_key_args(extract_parser)
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
    add_simulation_arg(extract_parser)

    @with_sdk
    def extract_records(
        sdk: ImednetSDK,
        study_key: str | None,
        record_filter: list[str] | None = None,
        subject_filter: list[str] | None = None,
        visit_filter: list[str] | None = None,
        interactive: bool = False,
        no_interactive: bool = False,
        output_format: str | None = None,
    ) -> None:
        from imednet.spi.models import Record, Subject, Visit

        from .wizard import print_progress

        resolved_format = resolve_output_format(output_format)
        json_mode = resolved_format == "json" or is_json_output_mode()

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
        validate_path_segment(study_key, name="study_key")
        workflow = DataExtractionWorkflow(sdk)  # type: ignore[arg-type]
        parsed_record_filter = parse_filter_args(record_filter, model=Record)
        parsed_subject_filter = parse_filter_args(subject_filter, model=Subject)
        parsed_visit_filter = parse_filter_args(visit_filter, model=Visit)

        if not json_mode:
            print_progress(f"Extracting records for study '{study_key}'...")
        if interactive and not json_mode:
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

        if resolved_format == "json":
            display_list(records, "records", output_format="json")
            return

        view_models = [
            {
                "ID": getattr(r, "record_id", None),
                "Subject": getattr(r, "subject_key", None),
                "Form": getattr(r, "form_key", None),
                "Status": getattr(r, "record_status", None),
                "Created": getattr(r, "date_created", None),
            }
            for r in records
        ]
        display_list(
            view_models,
            "matching records",
            "No records found matching the criteria.",
            details=records,
            interactive=False if no_interactive else None,
            output_format=resolved_format,
        )

    def _handle_extract_records(args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        output_format = resolve_output_format(getattr(args, "output_format", None))
        if args.interactive and output_format == "json":
            print_error("Error: Cannot combine --interactive with --format json.")
            sys.exit(1)
        study_key = resolve_study_key(extract_parser, args, required=not args.interactive)
        if args.interactive:
            from .wizard import ensure_interactive_stdin

            ensure_interactive_stdin(force_non_interactive=no_interactive)
        else:
            assert study_key is not None
            validate_cli_path_segment(study_key, name="study_key")
            from imednet.spi.models import Record, Subject, Visit

            parse_filter_args(args.record_filter, model=Record)
            parse_filter_args(args.subject_filter, model=Subject)
            parse_filter_args(args.visit_filter, model=Visit)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            extract_records(
                study_key=study_key,
                record_filter=args.record_filter,
                subject_filter=args.subject_filter,
                visit_filter=args.visit_filter,
                interactive=args.interactive,
                no_interactive=no_interactive,
                output_format=output_format,
            )

    extract_parser.set_defaults(func=_handle_extract_records)

    sync_parser = sub.add_parser(
        "sync-worker", help="Run an idempotent background cache refresh worker."
    )
    add_study_key_args(sync_parser)
    sync_parser.add_argument(
        "--interval", type=positive_int, default=900, help="Polling interval in seconds."
    )
    sync_parser.add_argument(
        "--once", action="store_true", help="Run a single sync cycle and exit."
    )
    add_no_interactive(sync_parser)
    add_simulation_arg(sync_parser)

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

        try:
            with console.status(
                f"Starting sync worker for study '{study_key}' (interval={interval}s). Press Ctrl+C to stop.",
                announce_non_interactive=False,
            ) as status:

                def on_cycle(cycle: int, count: int) -> None:
                    status.step(f"Study '{study_key}' - cycle {cycle}: synced {count} records.")

                def on_sleep_tick(cycle: int, remaining: int) -> None:
                    status.update(
                        f"Study '{study_key}' - cycle {cycle}: sleeping ({remaining}s remaining)..."
                    )

                try:
                    worker.run_forever(on_cycle=on_cycle, on_sleep_tick=on_sleep_tick)
                except TypeError:
                    worker.run_forever()
            if worker.state == SyncWorkerState.FAILED:
                err_detail = f": {worker.last_error}" if worker.last_error is not None else ""
                print_error(
                    redact_cli_secrets(
                        f"Sync worker failed after {worker.consecutive_failures}/"
                        f"{worker.max_consecutive_failures} consecutive failures{err_detail}"
                    )
                )
                sys.exit(1)
        except KeyboardInterrupt:
            clear_line()
            worker.stop()
            print("Sync worker termination requested. Exiting cleanly.")

    def _handle_sync_worker(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(sync_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            sync_worker(study_key=study_key, interval=args.interval, once=args.once)

    sync_parser.set_defaults(func=_handle_sync_worker)

    state_parser = sub.add_parser("state", help="Manage high-water mark execution ledger state.")
    _configure_state_subparsers(state_parser)

    # UAT command integration
    uat_parser = sub.add_parser("uat", help="Manage and execute UAT acceptance test pipelines.")
    uat_sub = uat_parser.add_subparsers(dest="uat_command")

    uat_run_parser = uat_sub.add_parser("run", help="Run the end-to-end UAT pipeline for a study.")
    add_study_key_args(uat_run_parser)
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
    add_simulation_arg(uat_run_parser)

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

        validate_path_segment(study_key, name="study_key")

        active_phase_descriptions = {
            UATWorkflowState.INSPECTING: "[Phase 1/5] Inspecting study schema and metadata...",
            UATWorkflowState.BUILDING_SPEC: "[Phase 2/5] Building test specification...",
            UATWorkflowState.GENERATING: "[Phase 3/5] Generating synthetic test records...",
            UATWorkflowState.SUBMITTING: "[Phase 4/5] Submitting records to iMednet API...",
            UATWorkflowState.MONITORING: "[Phase 5/5] Monitoring asynchronous job completion...",
        }

        def on_phase(phase: UATWorkflowState) -> None:
            if phase in (UATWorkflowState.COMPLETED, UATWorkflowState.FAILED):
                return
            msg = active_phase_descriptions.get(phase, f"UAT Phase transition: {phase.value}")
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

        result_state = getattr(result, "state", None)
        is_success = bool(result.overall_success) and result_state != UATWorkflowState.FAILED
        if is_success:
            print_progress("[Complete] UAT pipeline execution finished successfully.")
        else:
            print_progress("[Failed] UAT pipeline execution encountered an error.")

        print(result.summary())
        if not is_success:
            sys.exit(1)

    def _handle_uat_run(args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        study_key = resolve_study_key(uat_run_parser, args, required=not args.interactive)
        if args.interactive:
            from .wizard import ensure_interactive_stdin

            ensure_interactive_stdin(force_non_interactive=no_interactive)
        else:
            assert study_key is not None
            validate_cli_path_segment(study_key, name="study_key")
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            uat_run(
                study_key=study_key,
                interactive=args.interactive,
                subject_count=args.subject_count,
                site_name=args.site_name,
                enabled_form_keys=args.enabled_form_keys,
                email_notify=args.email_notify,
                no_interactive=no_interactive,
            )

    uat_run_parser.set_defaults(func=_handle_uat_run)


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
    add_study_key_args(subj_parser)
    subj_parser.add_argument(
        "-k",
        "--subject-key",
        "--subject",
        dest="subject_key_flag",
        default=None,
        action=UniqueStoreAction,
        help="The key identifying the subject.",
    )
    subj_parser.add_argument(
        "subject_key", nargs="?", default=None, help="The key identifying the subject."
    )
    add_output_format(subj_parser)
    add_simulation_arg(subj_parser)

    @with_sdk
    def subject_data(
        sdk: ImednetSDK,
        study_key: str,
        subject_key: str,
        output_format: str | None = None,
        no_interactive: bool = False,
    ) -> None:
        resolved_format = resolve_output_format(output_format)
        workflow = SubjectDataWorkflow(sdk)  # type: ignore[arg-type]
        if resolved_format == "json" or is_json_output_mode():
            data = workflow.get_all_subject_data(study_key, subject_key)
        else:
            with fetching_status("subject data", study_key):
                data = workflow.get_all_subject_data(study_key, subject_key)

        if data.subject_details is None:
            print_error(f"Error: Subject '{subject_key}' not found in study '{study_key}'.")
            sys.exit(1)

        if resolved_format == "json":
            emit_json([data], "subject_data")
            return

        subj = data.subject_details
        raw_keywords = getattr(subj, "keywords", None) or []
        keywords_str = ", ".join(str(getattr(k, "keyword_name", k)) for k in raw_keywords)
        view_row = {
            "subject_key": getattr(subj, "subject_key", None) or subject_key,
            "status": getattr(subj, "subject_status", None),
            "site": getattr(subj, "site_name", None),
            "enrollment_date": getattr(subj, "enrollment_start_date", None),
            "visits": len(data.visits),
            "records": len(data.records),
            "queries": len(data.queries),
            "keywords": keywords_str or None,
        }
        display_list(
            [view_row],
            "subject data",
            details=[data],
            interactive=False if no_interactive else None,
            output_format=resolved_format,
        )

    def handle_subject_data(args: argparse.Namespace) -> None:
        study_key, subject_key = resolve_hybrid_study_and_key(
            subj_parser,
            args,
            secondary_dest="subject_key",
            secondary_flag_dest="subject_key_flag",
        )
        validate_cli_path_segment(study_key, name="study_key")
        validate_cli_path_segment(subject_key, name="subject_key")
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            subject_data(
                study_key=study_key,
                subject_key=subject_key,
                output_format=resolve_output_format(getattr(args, "output_format", None)),
                no_interactive=getattr(args, "no_interactive", False),
            )

    subj_parser.set_defaults(func=handle_subject_data)


def state_app(args: list[str] | None = None) -> None:
    """Execute the state app logic directly for tests."""
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser(description="Manage high-water mark execution ledger state.")
    add_no_interactive(parser, suppress_default=False)
    _configure_state_subparsers(parser)

    parsed = parser.parse_args(args)
    old_non_interactive = os.environ.get(NON_INTERACTIVE_ENV)
    old_output_format = os.environ.get(OUTPUT_FORMAT_ENV)
    try:
        if getattr(parsed, "no_interactive", False):
            os.environ[NON_INTERACTIVE_ENV] = "1"
        explicit_format = getattr(parsed, "output_format", None)
        if explicit_format == "json":
            os.environ[OUTPUT_FORMAT_ENV] = "json"
        elif explicit_format == "table":
            os.environ.pop(OUTPUT_FORMAT_ENV, None)
        with cli_execution_guard():
            if hasattr(parsed, "func"):
                parsed.func(parsed)
            else:
                parser.print_help()
    finally:
        if old_non_interactive is not None:
            os.environ[NON_INTERACTIVE_ENV] = old_non_interactive
        else:
            os.environ.pop(NON_INTERACTIVE_ENV, None)
        if old_output_format is not None:
            os.environ[OUTPUT_FORMAT_ENV] = old_output_format
        else:
            os.environ.pop(OUTPUT_FORMAT_ENV, None)
