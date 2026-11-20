"""CLI commands for iMednet workflows.

This module provides the Typer/argparse integration for common clinical data
workflows, including record extraction, cache synchronization, standards
readiness validation, and state ledger management.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from imednet.sdk import ImednetSDK
from imednet.spi.cli import (
    NON_INTERACTIVE_ENV,
    STUDY_KEY_ARG,
    add_no_interactive,
    add_output_format,
    clear_line,
    console,
    display_list,
    emit_json,
    fetching_status,
    is_json_output_mode,
    parse_filter_args,
    print_error,
    resolve_output_format,
    validate_cli_path_segment,
    with_sdk,
)
from imednet.spi.utils import (
    global_sensitivity_registry,
    mask_clinical_phi,
    mask_custom_phi_recursive,
    redact_sensitive_text,
    resolve_study_configuration,
    validate_path_segment,
)

from .data_extraction import DataExtractionWorkflow
from .state_ledger import DEFAULT_LEDGER_PATH, get_state_provider
from .subject_data import SubjectDataWorkflow
from .sync_worker import SyncWorker, SyncWorkerConfig, SyncWorkerState


def _emit_line(text: str = "", *, err: bool = False) -> None:
    """Write a PHI/secret-redacted line safely via structured CLI output."""
    sanitized = redact_sensitive_text(str(text))
    if err:
        print_error(sanitized)
        return
    with contextlib.suppress(BrokenPipeError, OSError):
        builtins.print(sanitized, file=sys.stdout)


def _resolve_custom_phi_fields(study_key: str | None) -> set[str]:
    """Return study-configured custom PHI field names when available."""
    if not study_key:
        return set()
    cfg = resolve_study_configuration(study_key)
    if cfg is not None and cfg.phi_fields:
        return set(cfg.phi_fields)
    return set()


def _is_key_phi_sensitive(key: str, custom_phi_fields: set[str]) -> bool:
    """Return True if *key* matches global or study-configured PHI sensitivity rules."""
    if global_sensitivity_registry.is_sensitive(key):
        return True
    if custom_phi_fields:
        probe = mask_custom_phi_recursive({key: "probe"}, custom_phi_fields)
        return bool(isinstance(probe, Mapping) and probe.get(key) == "***MASKED***")
    return False


def _mask_workflow_value(value: Any, *, custom_phi_fields: set[str]) -> Any:
    """Recursively mask clinical PHI across Pydantic models, mappings, and sequences.

    Preserves object identity when no field values require masking so downstream
    interactive views and SDK model serializers receive unmodified model instances.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        changed = False
        rebuilt: dict[Any, Any] = {}
        for k, v in value.items():
            if _is_key_phi_sensitive(str(k), custom_phi_fields):
                rebuilt[k] = "***MASKED***"
                if v != "***MASKED***":
                    changed = True
            else:
                masked_v = _mask_workflow_value(v, custom_phi_fields=custom_phi_fields)
                rebuilt[k] = masked_v
                if masked_v is not v and masked_v != v:
                    changed = True
        if not changed and isinstance(value, dict):
            return value
        return rebuilt

    if isinstance(value, list):
        masked_list = [
            _mask_workflow_value(item, custom_phi_fields=custom_phi_fields) for item in value
        ]
        if all(
            new_item is old_item for new_item, old_item in zip(masked_list, value, strict=False)
        ):
            return value
        return masked_list

    if isinstance(value, tuple):
        masked_tuple = tuple(
            _mask_workflow_value(item, custom_phi_fields=custom_phi_fields) for item in value
        )
        if all(
            new_item is old_item for new_item, old_item in zip(masked_tuple, value, strict=False)
        ):
            return value
        return masked_tuple

    model_fields = getattr(type(value), "model_fields", None)
    model_copy = getattr(value, "model_copy", None)
    if isinstance(model_fields, Mapping) and callable(model_copy):
        updates: dict[str, Any] = {}
        for fname, finfo in model_fields.items():
            raw_val = getattr(value, fname, None)
            if raw_val is None:
                continue
            alias = getattr(finfo, "alias", None)
            if _is_key_phi_sensitive(fname, custom_phi_fields) or (
                isinstance(alias, str) and _is_key_phi_sensitive(alias, custom_phi_fields)
            ):
                if isinstance(raw_val, str) and raw_val != "***MASKED***":
                    updates[fname] = "***MASKED***"
                continue
            masked_val = _mask_workflow_value(raw_val, custom_phi_fields=custom_phi_fields)
            if masked_val is not raw_val and masked_val != raw_val:
                updates[fname] = masked_val

        extra = getattr(value, "__pydantic_extra__", None)
        masked_extra: dict[str, Any] | None = None
        if isinstance(extra, dict) and extra:
            candidate_extra = _mask_workflow_value(extra, custom_phi_fields=custom_phi_fields)
            if candidate_extra is not extra and candidate_extra != extra:
                masked_extra = dict(candidate_extra)

        if not updates and masked_extra is None:
            return value

        copied = model_copy(update=updates)
        if masked_extra is not None:
            copied_extra = getattr(copied, "__pydantic_extra__", None)
            if isinstance(copied_extra, dict):
                copied_extra = dict(copied_extra)
                copied_extra.update(masked_extra)
                object.__setattr__(copied, "__pydantic_extra__", copied_extra)
        return copied

    return value


def _mask_workflow_item(
    payload: Any,
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
) -> Any:
    """Apply default and study-configured PHI masking to *payload* when enabled."""
    if not mask_phi:
        return payload
    custom_phi_fields = _resolve_custom_phi_fields(study_key)
    return _mask_workflow_value(payload, custom_phi_fields=custom_phi_fields)


def _to_serializable(value: Any) -> Any:
    """Convert Pydantic models, mappings, and sequences into plain JSON-compatible objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _to_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_serializable(item) for item in value]
    model_dump = getattr(type(value), "model_dump", None)
    if callable(model_dump):
        dumped = value.model_dump(mode="json")
        if dumped is not value and isinstance(
            dumped, (Mapping, list, tuple, str, int, float, bool)
        ):
            return _to_serializable(dumped)
    instance_dump = getattr(value, "model_dump", None)
    if callable(instance_dump):
        dumped = instance_dump()
        if dumped is not value and isinstance(
            dumped, (Mapping, list, tuple, str, int, float, bool)
        ):
            return _to_serializable(dumped)
    return str(value)


def _sanitize_workflow_payload(
    payload: Any,
    *,
    study_key: str | None = None,
    mask_phi: bool = True,
) -> Any:
    """Serialize *payload* and apply default + study-configured PHI masking when enabled."""
    serialized = _to_serializable(payload)
    if not mask_phi:
        return serialized
    masked = mask_clinical_phi(serialized)
    custom_phi = _resolve_custom_phi_fields(study_key)
    if custom_phi:
        masked = mask_custom_phi_recursive(masked, custom_phi)
    return masked


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
            _emit_line(f"Error: {msg}", err=True)
            sys.exit(2)

        try:
            normalized = timestamp.replace("Z", "+00:00")  # type: ignore[union-attr]
            dt = datetime.fromisoformat(normalized)
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError as err:
            _emit_line(f"Invalid ISO timestamp format: {err}", err=True)
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
            _emit_line(
                f"Successfully set high-water mark for '{study_key}' -> '{stream}' to {dt.isoformat()}"
            )
        except Exception as err:
            _emit_line(f"Failed to write ledger: {err}", err=True)
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
        _emit_line(
            f"Successfully set high-water mark for '{study_key}' -> '{stream}' to {dt.isoformat()}"
        )
    except Exception as err:
        _emit_line(f"Failed to write ledger: {err}", err=True)
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
        _emit_line(f"Error: {msg}", err=True)
        sys.exit(2)

    ledger = get_state_provider(ledger_path)
    try:
        if stream:
            removed = ledger.delete_entry(study_key, stream)
            if removed:
                _emit_line(f"Successfully reset stream '{stream}' for study '{study_key}'.")
            else:
                _emit_line(f"No stream '{stream}' found for study '{study_key}'.")
                return
        else:
            removed = ledger.delete_entry(study_key)
            if removed:
                _emit_line(f"Successfully reset all streams for study '{study_key}'.")
            else:
                _emit_line(f"No state found for study '{study_key}'.")
                return
    except Exception as err:
        _emit_line(f"Failed to reset ledger state: {err}", err=True)
        sys.exit(1)


def _execute_show_state(
    ledger_path: str,
    study_key: str | None = None,
    *,
    interactive: bool = False,
    no_interactive: bool = False,
) -> None:
    """Display current high-water mark records and stream metadata from the ledger."""
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
        _emit_line(f"Failed to read ledger from {ledger_path}: {err}", err=True)
        sys.exit(1)

    has_data = False
    _emit_line(f"iMednet Extraction Ledger ({ledger_path})")
    _emit_line(
        "Study Key | Stream Name | Last Timestamp (UTC) | Records Processed | Status | Error Message"
    )
    for s_key, study_state in state.studies.items():
        if study_key and s_key != study_key:
            continue
        for stream_name, stream_state in study_state.streams.items():
            has_data = True
            ts_str = (
                stream_state.last_timestamp.isoformat()
                if stream_state.last_timestamp is not None
                else "None"
            )
            _emit_line(
                f"{s_key} | {stream_name} | {ts_str} | {stream_state.records_processed} | {stream_state.last_run_status} | {stream_state.error_message or ''}"
            )

    if not has_data:
        _emit_line("No ledger entries found matching filters.")


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
    show_parser.add_argument("-s", "--study-key", help="Filter the ledger by a specific study key.")
    show_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_no_interactive(show_parser)
    show_parser.set_defaults(
        func=lambda args: _execute_show_state(
            ledger_path=args.ledger_path,
            study_key=args.study_key,
            interactive=args.interactive,
            no_interactive=getattr(args, "no_interactive", False),
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
        "-f",
        "--force",
        action="store_true",
        help="Force state transition override even when the current status disallows it.",
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
            force=getattr(args, "force", False),
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
    extract_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI fields with '***MASKED***' (default: enabled).",
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
        no_interactive: bool = False,
        output_format: str | None = None,
        mask_phi: bool = True,
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

        masked_records = _mask_workflow_item(records, study_key=study_key, mask_phi=mask_phi)

        if resolved_format == "json":
            display_list(masked_records, "records", output_format="json")
            return

        view_models = [
            {
                "ID": r.get("record_id")
                if isinstance(r, Mapping)
                else getattr(r, "record_id", None),
                "Subject": (
                    r.get("subject_key")
                    if isinstance(r, Mapping)
                    else getattr(r, "subject_key", None)
                ),
                "Form": r.get("form_key")
                if isinstance(r, Mapping)
                else getattr(r, "form_key", None),
                "Status": (
                    r.get("record_status")
                    if isinstance(r, Mapping)
                    else getattr(r, "record_status", None)
                ),
                "Created": (
                    r.get("date_created")
                    if isinstance(r, Mapping)
                    else getattr(r, "date_created", None)
                ),
            }
            for r in masked_records
        ]
        display_list(
            view_models,
            "matching records",
            "No records found matching the criteria.",
            details=masked_records,
            interactive=False if no_interactive else None,
            output_format=resolved_format,
        )

    def _handle_extract_records(args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        output_format = resolve_output_format(getattr(args, "output_format", None))
        if args.interactive:
            from .wizard import ensure_interactive_stdin

            ensure_interactive_stdin(force_non_interactive=no_interactive)
        else:
            if not args.study_key:
                extract_parser.error("the following arguments are required: study_key")
            validate_cli_path_segment(args.study_key, name="study_key")
            from imednet.spi.models import Record, Subject, Visit

            parse_filter_args(args.record_filter, model=Record)
            parse_filter_args(args.subject_filter, model=Subject)
            parse_filter_args(args.visit_filter, model=Visit)
        extract_records(
            study_key=args.study_key,
            record_filter=args.record_filter,
            subject_filter=args.subject_filter,
            visit_filter=args.visit_filter,
            interactive=args.interactive,
            no_interactive=no_interactive,
            output_format=output_format,
            mask_phi=getattr(args, "mask_phi", True),
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
            _emit_line(f"Synced {count} cached records for study '{study_key}'.")
            return

        _emit_line(
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
            if worker.state == SyncWorkerState.FAILED:
                err_detail = f": {worker.last_error}" if worker.last_error is not None else ""
                _emit_line(
                    f"Sync worker failed after {worker.consecutive_failures}/"
                    f"{worker.max_consecutive_failures} consecutive failures{err_detail}",
                    err=True,
                )
                sys.exit(1)
        except KeyboardInterrupt:
            clear_line()
            worker.stop()
            _emit_line("Sync worker termination requested. Exiting cleanly.")

    sync_parser.set_defaults(
        func=lambda args: sync_worker(
            study_key=args.study_key, interval=args.interval, once=args.once
        )
    )

    state_parser = sub.add_parser("state", help="Manage high-water mark execution ledger state.")
    _configure_state_subparsers(state_parser)

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

        _emit_line(result.summary())
        if not is_success:
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

    validate_std_parser = sub.add_parser(
        "validate-standards",
        help="Validate study records and schema against CDISC CDASH and SDTM readiness rules.",
    )
    validate_std_parser.add_argument("study_key", nargs="?", default=None, help=STUDY_KEY_ARG)
    validate_std_parser.add_argument(
        "-s",
        "--study-key",
        dest="study_key_flag",
        default=None,
        help=STUDY_KEY_ARG,
    )
    validate_std_parser.add_argument(
        "--profile",
        "--standards-profile",
        dest="standards_profile",
        choices=("general", "drug", "biologic", "vaccine", "device"),
        default="general",
        help="Regulatory CDISC standards profile (default: general).",
    )
    validate_std_parser.add_argument(
        "--domain",
        "--sdtm-domain",
        dest="sdtm_domain",
        default=None,
        help="Optional target SDTM domain code (e.g. DM, AE, LB, VS, CM, EX, MH).",
    )
    validate_std_parser.add_argument(
        "--min-score",
        "--min-standards-score",
        dest="min_standards_score",
        type=float,
        default=None,
        help="Minimum standards readiness score (0-100) required for exit code 0.",
    )
    validate_std_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI in reported diagnostics (default: enabled).",
    )
    add_output_format(validate_std_parser)

    @with_sdk
    def validate_standards(
        sdk: ImednetSDK,
        study_key: str,
        standards_profile: str = "general",
        sdtm_domain: str | None = None,
        min_standards_score: float | None = None,
        output_format: str | None = None,
        mask_phi: bool = True,
    ) -> None:
        from imednet.spi.models import (
            resolve_sdtm_domain,
            validate_cdash_variable,
        )
        from imednet.spi.sink_base import evaluate_standards_readiness

        validate_path_segment(study_key, name="study_key")
        if min_standards_score is not None and not 0.0 <= min_standards_score <= 100.0:
            validate_std_parser.error("min-score must be between 0 and 100")

        resolved_fmt = resolve_output_format(output_format)
        json_mode = resolved_fmt == "json" or is_json_output_mode()

        def _fetch_inputs() -> tuple[list[Any], list[Any], list[Any]]:
            forms = list(sdk.forms.list(study_key=study_key))
            variables = list(sdk.variables.list(study_key=study_key))
            recs = [
                rec
                for rec in sdk.records.list(study_key=study_key)
                if not bool(
                    rec.get("deleted")
                    if isinstance(rec, Mapping)
                    else getattr(rec, "deleted", False)
                )
            ]
            return forms, variables, recs

        if json_mode:
            raw_forms, raw_variables, raw_records = _fetch_inputs()
        else:
            with fetching_status("standards readiness", study_key):
                raw_forms, raw_variables, raw_records = _fetch_inputs()

        sanitized_records = (
            _sanitize_workflow_payload(raw_records, study_key=study_key, mask_phi=True)
            if mask_phi
            else raw_records
        )

        form_id_to_key: dict[Any, str] = {}
        for f_obj in raw_forms:
            fid = (
                f_obj.get("form_id")
                if isinstance(f_obj, Mapping)
                else getattr(f_obj, "form_id", None)
            )
            fkey = (
                f_obj.get("form_key")
                if isinstance(f_obj, Mapping)
                else getattr(f_obj, "form_key", None)
            )
            if fid is not None and fkey:
                form_id_to_key[fid] = str(fkey)

        cdash_warnings: list[str] = []
        for var in raw_variables:
            var_name = (
                var.get("variable_name")
                if isinstance(var, Mapping)
                else getattr(var, "variable_name", None)
            )
            if not var_name:
                continue
            fkey = (
                var.get("form_key") if isinstance(var, Mapping) else getattr(var, "form_key", None)
            )
            if not fkey:
                fid = (
                    var.get("form_id")
                    if isinstance(var, Mapping)
                    else getattr(var, "form_id", None)
                )
                fkey = form_id_to_key.get(fid)
            inferred_dom = resolve_sdtm_domain(sdtm_domain or fkey, fallback="")
            if inferred_dom:
                valid_cdash, warning = validate_cdash_variable(str(var_name), inferred_dom)
                if not valid_cdash and warning and warning not in cdash_warnings:
                    cdash_warnings.append(warning)

        report = evaluate_standards_readiness(
            sanitized_records,
            study_key=study_key,
            standards_profile=standards_profile,
            sdtm_domain=sdtm_domain,
        )
        all_warnings = list(dict.fromkeys([*report.warnings, *cdash_warnings]))
        threshold = 100.0 if min_standards_score is None else float(min_standards_score)
        passed = report.is_ready if min_standards_score is None else report.score >= threshold
        serialized_violations = [
            v.model_dump() if hasattr(v, "model_dump") else str(v) for v in report.violations
        ]

        payload = {
            "study_key": study_key,
            "profile": report.profile,
            "domain": resolve_sdtm_domain(sdtm_domain) if sdtm_domain else None,
            "score": report.score,
            "is_ready": report.is_ready,
            "passed": passed,
            "checked_records": report.checked_records,
            "checked_fields": report.checked_fields,
            "successfully_validated_fields": report.successfully_validated_fields,
            "domain_scores": dict(report.domain_scores),
            "warnings": all_warnings,
            "violations": serialized_violations,
        }
        if resolved_fmt == "json":
            sanitized_payload = _sanitize_workflow_payload(
                payload,
                study_key=study_key,
                mask_phi=mask_phi,
            )
            _emit_line(json.dumps(sanitized_payload, indent=2, default=str))
        else:
            _emit_line(
                f"Standards Readiness Report — Study: {study_key} (profile: {report.profile})"
            )
            _emit_line(
                f"Score: {report.score:.2f}/100.00 | Ready: {report.is_ready} | "
                f"Checked Records: {report.checked_records} | Checked Fields: {report.checked_fields}"
            )
            if report.domain_scores:
                _emit_line("Domain Scores:")
                for dom_code, dom_score in sorted(report.domain_scores.items()):
                    _emit_line(f"  {dom_code}: {dom_score:.2f}")
            if all_warnings:
                _emit_line(f"Warnings ({len(all_warnings)}):")
                for warn_msg in all_warnings:
                    _emit_line(f"  - {warn_msg}")
            if report.violations:
                _emit_line(f"Violations ({len(report.violations)}):")
                for violation in report.violations:
                    v_msg = (
                        f"[{violation.severity}] {violation.field}: {violation.message}"
                        if hasattr(violation, "message")
                        else str(violation)
                    )
                    _emit_line(f"  - {v_msg}")

        if min_standards_score is not None and report.score < min_standards_score:
            sys.exit(1)

    def _handle_validate_standards(args: argparse.Namespace) -> None:
        resolved_study = args.study_key_flag or args.study_key
        if not resolved_study or not str(resolved_study).strip():
            validate_std_parser.error("the following arguments are required: study_key")
        if args.study_key_flag and args.study_key and args.study_key_flag != args.study_key:
            validate_std_parser.error(
                f"conflicting values provided for study_key: "
                f"'{args.study_key_flag}' (flag) and '{args.study_key}' (positional)"
            )
        study_key_str = str(resolved_study).strip()
        validate_cli_path_segment(study_key_str, name="study_key")
        if args.min_standards_score is not None and not 0.0 <= args.min_standards_score <= 100.0:
            validate_std_parser.error("min-score must be between 0 and 100")
        validate_standards(
            study_key=study_key_str,
            standards_profile=args.standards_profile,
            sdtm_domain=args.sdtm_domain,
            min_standards_score=args.min_standards_score,
            output_format=resolve_output_format(getattr(args, "output_format", None)),
            mask_phi=getattr(args, "mask_phi", True),
        )

    validate_std_parser.set_defaults(func=_handle_validate_standards)


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
    subj_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI fields with '***MASKED***' (default: enabled).",
    )
    add_output_format(subj_parser)

    @with_sdk
    def subject_data(
        sdk: ImednetSDK,
        study_key: str,
        subject_key: str,
        output_format: str | None = None,
        no_interactive: bool = False,
        mask_phi: bool = True,
    ) -> None:
        resolved_format = resolve_output_format(output_format)
        workflow = SubjectDataWorkflow(sdk)  # type: ignore[arg-type]
        if resolved_format == "json" or is_json_output_mode():
            data = workflow.get_all_subject_data(study_key, subject_key)
        else:
            with fetching_status("subject data", study_key):
                data = workflow.get_all_subject_data(study_key, subject_key)

        if not isinstance(data, Mapping) and getattr(data, "subject_details", None) is None:
            print_error(f"Error: Subject '{subject_key}' not found in study '{study_key}'.")
            sys.exit(1)

        masked_data = _mask_workflow_item(data, study_key=study_key, mask_phi=mask_phi)

        if resolved_format == "json":
            emit_json([masked_data], "subject_data")
            return

        if isinstance(masked_data, Mapping):
            subj_dict = masked_data.get("subject_details")
            subj_map = subj_dict if isinstance(subj_dict, Mapping) else masked_data
            raw_keywords = subj_map.get("keywords") or []
            keywords_str = ", ".join(
                str(
                    k.get("keyword_name", k)
                    if isinstance(k, Mapping)
                    else getattr(k, "keyword_name", k)
                )
                for k in raw_keywords
            )
            view_row = {
                "subject_key": subj_map.get("subject_key", subj_map.get("subjectKey"))
                or subject_key,
                "status": subj_map.get("subject_status", subj_map.get("subjectStatus")),
                "site": subj_map.get("site_name", subj_map.get("siteName")),
                "enrollment_date": subj_map.get(
                    "enrollment_start_date", subj_map.get("enrollmentStartDate")
                ),
                "visits": len(masked_data.get("visits") or []),
                "records": len(masked_data.get("records") or []),
                "queries": len(masked_data.get("queries") or []),
                "keywords": keywords_str or None,
            }
        else:
            subj = masked_data.subject_details
            raw_keywords = getattr(subj, "keywords", None) or []
            keywords_str = ", ".join(str(getattr(k, "keyword_name", k)) for k in raw_keywords)
            view_row = {
                "subject_key": getattr(subj, "subject_key", None) or subject_key,
                "status": getattr(subj, "subject_status", None),
                "site": getattr(subj, "site_name", None),
                "enrollment_date": getattr(subj, "enrollment_start_date", None),
                "visits": len(masked_data.visits),
                "records": len(masked_data.records),
                "queries": len(masked_data.queries),
                "keywords": keywords_str or None,
            }

        display_list(
            [view_row],
            "subject data",
            details=[masked_data],
            interactive=False if no_interactive else None,
            output_format=resolved_format,
        )

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
        subject_data(
            study_key=study_key,
            subject_key=subject_key,
            output_format=resolve_output_format(getattr(args, "output_format", None)),
            no_interactive=getattr(args, "no_interactive", False),
            mask_phi=getattr(args, "mask_phi", True),
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
