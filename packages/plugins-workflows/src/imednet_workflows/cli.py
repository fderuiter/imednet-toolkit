"""CLI commands for iMednet workflows.

This module provides the Typer/argparse integration for common clinical data
workflows, including record extraction, cache synchronization, standards
readiness validation, and state ledger management.
"""

# pylint: disable=duplicate-code

from __future__ import annotations

import argparse
import builtins
import contextlib
import csv
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

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
    export_list_to_file,
    fetching_status,
    is_json_output_mode,
    load_offline_schema,
    parse_email_notify,
    parse_filter_args,
    positive_int,
    print_error,
    redact_cli_secrets,
    resolve_cli_schema,
    resolve_hybrid_study_and_key,
    resolve_output_format,
    resolve_records_export_target,
    resolve_study_key,
    simulation_mode_context,
    validate_cli_path_segment,
    with_sdk,
)
from imednet.spi.models import PROFILE_REGISTRY
from imednet.spi.utils import (
    REVISION_DATE_ATTRIBUTES,
    SensitivityRegistry,
    global_sensitivity_registry,
    mask_clinical_phi,
    mask_custom_phi_recursive,
    normalize_utc_timestamp_bound,
    redact_sensitive_text,
    resolve_study_configuration,
    sanitize_csv_formula,
    sanitize_diagnostic_text,
    validate_path_segment,
)
from imednet.spi.validation import DataDictionary, ValidationSchemaProtocol

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK

DEFAULT_LEDGER_PATH: str = os.environ.get(
    "IMEDNET_WORKFLOW_LEDGER_PATH",
    str(Path.home() / ".imednet" / "pipeline_ledger.json"),
)

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "DataExtractionWorkflow": ("imednet_workflows.data_extraction", "DataExtractionWorkflow"),
    "DuckDBIngestionWorkflow": ("imednet_workflows.duckdb_centralizer", "DuckDBIngestionWorkflow"),
    "SubjectDataWorkflow": ("imednet_workflows.subject_data", "SubjectDataWorkflow"),
    "SyncWorker": ("imednet_workflows.sync_worker", "SyncWorker"),
    "SyncWorkerConfig": ("imednet_workflows.sync_worker", "SyncWorkerConfig"),
    "SyncWorkerState": ("imednet_workflows.sync_worker", "SyncWorkerState"),
    "get_state_provider": ("imednet_workflows.state_ledger", "get_state_provider"),
}


def _load_lazy_attr(name: str) -> Any:
    """Import and cache a lazy workflow attribute on first access."""
    if name in _LAZY_ATTRS:
        mod_name, attr_name = _LAZY_ATTRS[name]
        mod = import_module(mod_name)  # nosem
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily load workflow execution classes and state providers on demand (PEP 562)."""
        return _load_lazy_attr(name)


def _resolve_attr(name: str) -> Any:
    """Resolve a lazy workflow symbol while preserving module-level monkeypatches."""
    if name in globals():
        return globals()[name]
    return _load_lazy_attr(name)


def _emit_line(text: str = "", *, err: bool = False) -> None:
    """Write a PHI/secret-redacted line safely via structured CLI output."""
    sanitized = redact_cli_secrets(redact_sensitive_text(str(text)))
    if err:
        print_error(sanitized)
        return
    with contextlib.suppress(BrokenPipeError, OSError):
        builtins.print(sanitized, file=sys.stdout)


def _exit_uat_validation_error(exc: BaseException | str) -> NoReturn:
    """Emit a secret- and PHI-redacted UAT validation error to stderr and exit 1."""
    sanitized = redact_cli_secrets(redact_sensitive_text(str(exc)))
    prefix = "" if sanitized.startswith("Validation Error:") else "Validation Error: "
    print_error(f"{prefix}{sanitized}")
    sys.exit(1)


def _validate_uat_subject_count(subject_count: int) -> int:
    """Validate --subject-count bounds (1..100) during CLI preflight."""
    if isinstance(subject_count, bool) or not 1 <= subject_count <= 100:
        _exit_uat_validation_error("subject_count must be between 1 and 100.")
    return subject_count


def _load_uat_data_dictionary(
    dd_path: str | None,
    *,
    study_key: str,
) -> DataDictionary | None:
    """Load an offline DataDictionary from JSON, CDISC ODM XML, REDCap CSV, or CSV dir."""
    if not dd_path:
        return None
    loaded = load_offline_schema(dd_path, study_key=study_key)
    if not isinstance(loaded, DataDictionary):
        raise ValueError(
            f"Unsupported Data Dictionary source '{dd_path}': expected JSON, ODM XML, REDCap CSV, or CSV directory."
        )
    return loaded


def _serialize_uat_run_result(
    result: Any,
    *,
    study_key: str,
    dry_run: bool,
    custom_phi_fields: set[str],
) -> dict[str, Any]:
    """Serialize a UATRunResult into a PHI-masked camelCase dictionary for emit_json."""
    state_raw = getattr(result, "state", None)
    state_str = (
        state_raw.value
        if state_raw is not None and hasattr(state_raw, "value")
        else str(state_raw or "COMPLETED")
    )
    history_raw = getattr(result, "state_history", None)
    history_list = (
        [s.value if hasattr(s, "value") else str(s) for s in history_raw]
        if isinstance(history_raw, list)
        else []
    )
    fail_raw = getattr(result, "failure_phase", None)
    fail_str = (
        fail_raw.value
        if fail_raw is not None and hasattr(fail_raw, "value")
        else (str(fail_raw) if fail_raw is not None else None)
    )
    record_sets = getattr(result, "record_sets", None)
    forms_tested = len(record_sets) if isinstance(record_sets, list) else 0
    records_generated = (
        sum(len(getattr(rs, "payloads", []) or []) for rs in record_sets)
        if isinstance(record_sets, list)
        else 0
    )
    sub_res = getattr(result, "submission_result", None)
    records_submitted = (
        int(sub_res.total_records_submitted)
        if sub_res is not None
        and isinstance(getattr(sub_res, "total_records_submitted", None), int)
        else 0
    )
    poll_sum = getattr(result, "poll_summary", None)
    jobs_monitored = 0
    if poll_sum is not None:
        res_map = getattr(poll_sum, "results", None)
        fail_map = getattr(poll_sum, "failures", None)
        if isinstance(res_map, dict):
            jobs_monitored += len(res_map)
        if isinstance(fail_map, dict):
            jobs_monitored += len(fail_map)

    val_rep = getattr(result, "validation_report", None)
    if val_rep is not None and callable(getattr(val_rep, "to_dict", None)):
        val_rep_dict = val_rep.to_dict()
    elif isinstance(val_rep, dict):
        val_rep_dict = val_rep
    else:
        val_rep_dict = None

    err_msg = getattr(result, "error_message", None)
    sanitized_err = (
        redact_cli_secrets(redact_sensitive_text(err_msg)) if isinstance(err_msg, str) else None
    )
    raw_durations = getattr(result, "phase_durations", None)
    phase_durations = dict(raw_durations) if isinstance(raw_durations, Mapping) else {}

    item: dict[str, Any] = {
        "runId": str(getattr(result, "run_id", "") or ""),
        "studyKey": str(getattr(result, "study_key", None) or study_key),
        "dryRun": bool(getattr(result, "dry_run", dry_run)),
        "state": state_str,
        "stateHistory": history_list,
        "overallSuccess": bool(getattr(result, "overall_success", False)),
        "formsTested": forms_tested,
        "recordsGenerated": records_generated,
        "recordsSubmitted": records_submitted,
        "jobsMonitored": jobs_monitored,
        "failurePhase": fail_str,
        "errorMessage": sanitized_err,
        "validationReport": val_rep_dict,
        "phaseDurations": phase_durations,
    }
    masked = _mask_workflow_value(item, custom_phi_fields=custom_phi_fields)
    return masked if isinstance(masked, dict) else item


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


def _build_study_sensitivity_registry(custom_phi_fields: set[str]) -> SensitivityRegistry:
    """Return a SensitivityRegistry combining global rules with *custom_phi_fields*."""
    if not custom_phi_fields:
        return global_sensitivity_registry
    reg = SensitivityRegistry()
    sens_keys, exempt_keys = global_sensitivity_registry.snapshot()
    for ex_key in exempt_keys:
        reg.add_exempt_key(ex_key)
    for sens_key in sens_keys:
        reg.add_sensitive_key(sens_key)
    for custom_key in custom_phi_fields:
        reg.add_sensitive_key(custom_key)
    return reg


def _mask_workflow_value(
    value: Any,
    *,
    custom_phi_fields: set[str],
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Recursively mask clinical PHI across Pydantic models, mappings, and sequences.

    Preserves object identity when no field values require masking so downstream
    interactive views and SDK model serializers receive unmodified model instances.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    active_registry = (
        registry if registry is not None else _build_study_sensitivity_registry(custom_phi_fields)
    )

    if isinstance(value, Mapping):
        changed = False
        rebuilt: dict[Any, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if _is_key_phi_sensitive(key_str, custom_phi_fields):
                rebuilt[k] = "***MASKED***"
                if v != "***MASKED***":
                    changed = True
            elif key_str in ("reason_for_change", "reasonForChange") and isinstance(v, str) and v:
                scrubbed = sanitize_diagnostic_text(v, registry=active_registry)
                rebuilt[k] = scrubbed
                if scrubbed != v:
                    changed = True
            else:
                masked_v = _mask_workflow_value(
                    v,
                    custom_phi_fields=custom_phi_fields,
                    registry=active_registry,
                )
                rebuilt[k] = masked_v
                if masked_v is not v and masked_v != v:
                    changed = True
        if not changed and isinstance(value, dict):
            return value
        return rebuilt

    if isinstance(value, list):
        masked_list = [
            _mask_workflow_value(
                item,
                custom_phi_fields=custom_phi_fields,
                registry=active_registry,
            )
            for item in value
        ]
        if all(
            new_item is old_item for new_item, old_item in zip(masked_list, value, strict=False)
        ):
            return value
        return masked_list

    if isinstance(value, tuple):
        masked_tuple = tuple(
            _mask_workflow_value(
                item,
                custom_phi_fields=custom_phi_fields,
                registry=active_registry,
            )
            for item in value
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
            if fname == "reason_for_change" and isinstance(raw_val, str) and raw_val:
                scrubbed = sanitize_diagnostic_text(raw_val, registry=active_registry)
                if scrubbed != raw_val:
                    updates[fname] = scrubbed
                continue
            masked_val = _mask_workflow_value(
                raw_val,
                custom_phi_fields=custom_phi_fields,
                registry=active_registry,
            )
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

        ledger = _resolve_attr("get_state_provider")(ledger_path)
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
    ledger = _resolve_attr("get_state_provider")(ledger_path)
    current_state = display_ledger_streams(
        ledger_path=ledger_path,
        study_key=study_key,
        state_provider=ledger,
    )
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

    current_status = "None"
    current_ts_str = "None"
    if current_state is not None:
        try:
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

    ledger = _resolve_attr("get_state_provider")(ledger_path)
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

    ledger = _resolve_attr("get_state_provider")(ledger_path)
    try:
        state = ledger.read_state()
    except Exception as err:
        _emit_line(f"Failed to read ledger from {ledger_path}: {err}", err=True)
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

    _emit_line(f"iMednet Extraction Ledger ({ledger_path})")
    _emit_line(
        "Study Key | Stream Name | Last Timestamp (UTC) | Records Processed | Status | Error Message"
    )
    for entry in entries:
        ts_str = entry["lastTimestamp"] if entry["lastTimestamp"] is not None else "None"
        _emit_line(
            f"{entry['studyKey']} | {entry['streamName']} | {ts_str} | {entry['recordsProcessed']} | {entry['status']} | {entry['errorMessage'] or ''}"
        )

    if not entries:
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
    extract_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI fields with '***MASKED***' (default: enabled).",
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
        workflow = _resolve_attr("DataExtractionWorkflow")(sdk)
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
                mask_phi=getattr(args, "mask_phi", True),
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

        sync_worker_cls = _resolve_attr("SyncWorker")
        sync_worker_cfg_cls = _resolve_attr("SyncWorkerConfig")
        sync_worker_state_cls = _resolve_attr("SyncWorkerState")
        worker = sync_worker_cls(
            CachedRecordsLoader(sdk),  # type: ignore[arg-type]
            config=sync_worker_cfg_cls(study_key=study_key, interval_seconds=interval),
        )

        if once:
            count = worker.run_once()
            _emit_line(f"Synced {count} cached records for study '{study_key}'.")
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
            if worker.state == sync_worker_state_cls.FAILED:
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
            _emit_line("Sync worker termination requested. Exiting cleanly.")

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
    uat_parser.set_defaults(
        func=lambda _args: uat_parser.error("the following arguments are required: uat_command")
    )

    uat_build_spec_parser = uat_sub.add_parser(
        "build-spec",
        help="Inspect a study and export a UATSpecification JSON plan.",
    )
    add_study_key_args(uat_build_spec_parser)
    uat_build_spec_parser.add_argument(
        "--subject-count",
        type=int,
        default=2,
        help="Number of test subjects to generate (1..100).",
    )
    uat_build_spec_parser.add_argument(
        "--site-name",
        default=None,
        help="Target enrollment-eligible site name for synthetic subjects.",
    )
    uat_build_spec_parser.add_argument(
        "--form-key",
        action="append",
        dest="enabled_form_keys",
        help="Target active form key to include. Repeat for multiple forms.",
    )
    uat_build_spec_parser.add_argument(
        "--data-dictionary",
        dest="data_dictionary",
        default=None,
        help="Offline Data Dictionary path (.json, .xml ODM, .csv REDCap, or CSV directory).",
    )
    uat_build_spec_parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed to record in the generated UATSpecification.",
    )
    uat_build_spec_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output file path for UATSpecification JSON (writes to stdout if omitted).",
    )
    add_no_interactive(uat_build_spec_parser)
    add_simulation_arg(uat_build_spec_parser)

    @with_sdk
    def uat_build_spec(
        sdk: ImednetSDK,
        *,
        study_key: str,
        subject_count: int = 2,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        data_dictionary: DataDictionary | None = None,
        seed: int | None = None,
        output_path: str | None = None,
    ) -> None:
        from .uat import StudySchemaInspector, UATSpecificationBuilder
        from .wizard import print_progress

        try:
            snapshot = StudySchemaInspector(sdk).inspect(study_key)  # type: ignore[arg-type]
            spec = UATSpecificationBuilder(data_dictionary=data_dictionary).build(
                snapshot,
                subject_count=subject_count,
                site_name=site_name,
                enabled_form_keys=enabled_form_keys,
                seed=seed,
            )
            serialized = spec.model_dump_json(indent=2, by_alias=True)
            if output_path:
                dest = Path(output_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f"{serialized}\n", encoding="utf-8")
                if not is_json_output_mode():
                    print_progress(f"Wrote UAT specification for study '{study_key}' to '{dest}'.")
                return
            with contextlib.suppress(BrokenPipeError, OSError):
                sys.stdout.write(f"{serialized}\n")
                sys.stdout.flush()
        except (ValueError, OSError) as exc:
            _exit_uat_validation_error(exc)

    def _handle_uat_build_spec(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(uat_build_spec_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        subject_count = _validate_uat_subject_count(args.subject_count)
        try:
            loaded_dd = _load_uat_data_dictionary(args.data_dictionary, study_key=study_key)
        except (ValueError, OSError) as exc:
            _exit_uat_validation_error(exc)
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            uat_build_spec(
                study_key=study_key,
                subject_count=subject_count,
                site_name=args.site_name,
                enabled_form_keys=args.enabled_form_keys,
                data_dictionary=loaded_dd,
                seed=args.seed,
                output_path=args.output,
            )

    uat_build_spec_parser.set_defaults(func=_handle_uat_build_spec)

    uat_run_parser = uat_sub.add_parser("run", help="Run the end-to-end UAT pipeline for a study.")
    add_study_key_args(uat_run_parser)
    uat_run_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    uat_run_parser.add_argument(
        "--subject-count",
        type=int,
        default=2,
        help="Number of test subjects to generate (1..100).",
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
        help="Optional email notification destination or boolean ('true'/'false').",
    )
    uat_run_parser.add_argument(
        "--spec",
        dest="spec_path",
        default=None,
        help="Path to a saved UATSpecification JSON file.",
    )
    uat_run_parser.add_argument(
        "--data-dictionary",
        dest="data_dictionary",
        default=None,
        help="Offline Data Dictionary path (.json, .xml ODM, .csv REDCap, or CSV directory).",
    )
    uat_run_parser.add_argument(
        "--schema",
        dest="schema_source",
        default=None,
        help=(
            "Validation schema path (.json, .xml ODM, .csv REDCap, or CSV dir) "
            "or 'api' to fetch live study variables."
        ),
    )
    uat_run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Inspect, generate, and validate synthetic UAT payloads locally without API writes.",
    )
    uat_run_parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Deterministic random seed for synthetic record generation.",
    )
    add_output_format(uat_run_parser)
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
        *,
        spec: Any = None,
        data_dictionary: DataDictionary | None = None,
        preloaded_schema: ValidationSchemaProtocol | None = None,
        schema_source: str | None = None,
        dry_run: bool = False,
        seed: int | None = None,
        output_format: str | None = None,
    ) -> None:
        from .uat import UATWorkflow, UATWorkflowState
        from .wizard import (
            ensure_interactive_stdin,
            print_progress,
            prompt_input,
            prompt_study_key,
            prompt_subject_count,
        )

        resolved_format = resolve_output_format(output_format)
        json_mode = resolved_format == "json" or is_json_output_mode()

        if interactive:
            ensure_interactive_stdin(force_non_interactive=no_interactive)
            study_key = prompt_study_key(study_key)
            subject_count = prompt_subject_count(
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

        assert study_key is not None
        validate_path_segment(study_key, name="study_key")
        if spec is not None and getattr(spec, "study_key", study_key) != study_key:
            _exit_uat_validation_error(
                f"Specification studyKey '{spec.study_key}' does not match study '{study_key}'."
            )

        try:
            resolved_schema = (
                preloaded_schema
                if preloaded_schema is not None
                else resolve_cli_schema(sdk, study_key, schema_source)
            )
        except (ValueError, OSError) as exc:
            _exit_uat_validation_error(exc)

        def _emit_progress(message: str) -> None:
            if not json_mode:
                print_progress(message)

        if dry_run:
            active_phase_descriptions = {
                UATWorkflowState.INSPECTING: (
                    "[Phase 1/3] Inspecting study schema and metadata..."
                ),
                UATWorkflowState.BUILDING_SPEC: "[Phase 2/3] Building test specification...",
                UATWorkflowState.GENERATING: (
                    "[Phase 3/3] Generating and validating synthetic test records..."
                ),
            }
        else:
            active_phase_descriptions = {
                UATWorkflowState.INSPECTING: (
                    "[Phase 1/5] Inspecting study schema and metadata..."
                ),
                UATWorkflowState.BUILDING_SPEC: "[Phase 2/5] Building test specification...",
                UATWorkflowState.GENERATING: "[Phase 3/5] Generating synthetic test records...",
                UATWorkflowState.SUBMITTING: "[Phase 4/5] Submitting records to iMednet API...",
                UATWorkflowState.MONITORING: (
                    "[Phase 5/5] Monitoring asynchronous job completion..."
                ),
            }

        def on_phase(phase: UATWorkflowState) -> None:
            if phase in (UATWorkflowState.COMPLETED, UATWorkflowState.FAILED):
                return
            msg = active_phase_descriptions.get(phase, f"UAT Phase transition: {phase.value}")
            _emit_progress(msg)

        def on_progress(batch_id: str, status: Any, elapsed: float) -> None:
            state_val = getattr(status, "state", getattr(status, "status", str(status)))
            _emit_progress(f"[Job Progress] Batch {batch_id}: {state_val} ({elapsed:.1f}s)")

        effective_seed = (
            seed
            if seed is not None
            else (getattr(spec, "seed", None) if spec is not None else None)
        )

        _emit_progress(f"Starting UAT pipeline for study '{study_key}'...")
        wf_kwargs: dict[str, Any] = {
            "on_progress": on_progress,
            "on_phase_change": on_phase,
        }
        if effective_seed is not None:
            wf_kwargs["seed"] = effective_seed
        if data_dictionary is not None:
            wf_kwargs["data_dictionary"] = data_dictionary

        workflow = UATWorkflow(
            sdk,  # type: ignore[arg-type]
            **wf_kwargs,
        )

        run_kwargs: dict[str, Any] = {
            "study_key": study_key,
            "subject_count": subject_count,
            "site_name": site_name,
            "enabled_form_keys": enabled_form_keys,
            "email_notify": email_notify,
        }
        if spec is not None:
            run_kwargs["spec"] = spec
        if data_dictionary is not None:
            run_kwargs["data_dictionary"] = data_dictionary
        if resolved_schema is not None:
            run_kwargs["schema"] = resolved_schema
        if effective_seed is not None:
            run_kwargs["seed"] = effective_seed
        if dry_run:
            run_kwargs["dry_run"] = True

        try:
            result = workflow.run(**run_kwargs)
        except (ValueError, OSError) as exc:
            _exit_uat_validation_error(exc)

        result_state = getattr(result, "state", None)
        is_success = bool(result.overall_success) and result_state != UATWorkflowState.FAILED

        if resolved_format == "json":
            has_structured_outcome = (
                is_success
                or getattr(result, "validation_report", None) is not None
                or getattr(result, "submission_result", None) is not None
                or getattr(result, "poll_summary", None) is not None
            )
            if not has_structured_outcome:
                err_detail = (
                    getattr(result, "error_message", None)
                    or "UAT pipeline execution encountered an error."
                )
                print_error(redact_cli_secrets(redact_sensitive_text(str(err_detail))))
                sys.exit(1)

            custom_phi = _resolve_custom_phi_fields(study_key)
            item = _serialize_uat_run_result(
                result,
                study_key=study_key,
                dry_run=dry_run,
                custom_phi_fields=custom_phi,
            )
            emit_json([item], "uat_run")
            if not is_success:
                sys.exit(1)
            return

        if is_success:
            _emit_progress("[Complete] UAT pipeline execution finished successfully.")
        else:
            _emit_progress("[Failed] UAT pipeline execution encountered an error.")

        _emit_line(result.summary())
        if not is_success:
            sys.exit(1)

    def _handle_uat_run(args: argparse.Namespace) -> None:
        no_interactive = getattr(args, "no_interactive", False)
        raw_format = getattr(args, "output_format", None)
        resolved_format = resolve_output_format(raw_format)
        if args.interactive and (resolved_format == "json" or is_json_output_mode()):
            print_error("Error: Cannot combine --interactive with --format json.")
            sys.exit(1)

        subject_count = _validate_uat_subject_count(args.subject_count)
        try:
            normalized_email_notify = parse_email_notify(args.email_notify)
        except ValueError as exc:
            _exit_uat_validation_error(exc)

        loaded_spec: Any = None
        if getattr(args, "spec_path", None):
            from .uat import UATSpecification

            spec_file = Path(args.spec_path)
            if not spec_file.is_file():
                _exit_uat_validation_error(f"Specification file not found: '{args.spec_path}'")
            try:
                loaded_spec = UATSpecification.from_json(spec_file)
            except (ValueError, OSError) as exc:
                _exit_uat_validation_error(f"Invalid UAT specification '{args.spec_path}': {exc}")

        study_key = resolve_study_key(uat_run_parser, args, required=False)
        if study_key is None and loaded_spec is not None:
            study_key = loaded_spec.study_key

        if args.interactive:
            from .wizard import ensure_interactive_stdin

            ensure_interactive_stdin(force_non_interactive=no_interactive)
        else:
            if not study_key:
                uat_run_parser.error("the following arguments are required: study_key")
            assert study_key is not None
            validate_cli_path_segment(study_key, name="study_key")
            if loaded_spec is not None and loaded_spec.study_key != study_key:
                _exit_uat_validation_error(
                    f"Specification studyKey '{loaded_spec.study_key}' does not match study '{study_key}'."
                )

        preflight_study_key = study_key or "STUDY"
        try:
            loaded_dd = _load_uat_data_dictionary(
                getattr(args, "data_dictionary", None),
                study_key=preflight_study_key,
            )
            raw_schema_src = getattr(args, "schema_source", None)
            preloaded_schema = (
                load_offline_schema(raw_schema_src, study_key=preflight_study_key)
                if raw_schema_src and raw_schema_src.strip().lower() != "api"
                else None
            )
        except (ValueError, OSError) as exc:
            _exit_uat_validation_error(exc)

        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            uat_run(
                study_key=study_key,
                interactive=args.interactive,
                subject_count=subject_count,
                site_name=args.site_name,
                enabled_form_keys=args.enabled_form_keys,
                email_notify=normalized_email_notify,
                no_interactive=no_interactive,
                spec=loaded_spec,
                data_dictionary=loaded_dd,
                preloaded_schema=preloaded_schema,
                schema_source=getattr(args, "schema_source", None),
                dry_run=bool(getattr(args, "dry_run", False)),
                seed=getattr(args, "seed", None),
                output_format=raw_format,
            )

    uat_run_parser.set_defaults(func=_handle_uat_run)

    validate_std_parser = sub.add_parser(
        "validate-standards",
        help="Validate study records and schema against CDISC CDASH and SDTM readiness rules.",
    )
    add_study_key_args(validate_std_parser)
    validate_std_parser.add_argument(
        "--profile",
        "--standards-profile",
        dest="standards_profile",
        choices=tuple(PROFILE_REGISTRY.list_profiles()),
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
    add_simulation_arg(validate_std_parser)

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
        from imednet.spi.sink_base import (
            evaluate_standards_readiness,
            filter_forms_by_sdtm_domain,
            redact_batch_records,
        )

        validate_path_segment(study_key, name="study_key")
        if min_standards_score is not None and not 0.0 <= min_standards_score <= 100.0:
            validate_std_parser.error("min-score must be between 0 and 100")

        resolved_fmt = resolve_output_format(output_format)
        json_mode = resolved_fmt == "json" or is_json_output_mode()
        target_domain = (
            resolve_sdtm_domain(sdtm_domain, fallback="") if sdtm_domain else ""
        ) or None

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

        form_id_to_key: dict[Any, str] = {}
        for f_obj in raw_forms:
            fid = (
                f_obj.get("form_id", f_obj.get("formId"))
                if isinstance(f_obj, Mapping)
                else getattr(f_obj, "form_id", getattr(f_obj, "formId", None))
            )
            fkey = (
                f_obj.get("form_key", f_obj.get("formKey"))
                if isinstance(f_obj, Mapping)
                else getattr(f_obj, "form_key", getattr(f_obj, "formKey", None))
            )
            if fid is not None and fkey:
                form_id_to_key[fid] = str(fkey)

        if target_domain is not None:
            if raw_forms:
                matching_forms = filter_forms_by_sdtm_domain(raw_forms, target_domain)
                allowed_form_ids = {
                    f_obj.get("form_id", f_obj.get("formId"))
                    if isinstance(f_obj, Mapping)
                    else getattr(f_obj, "form_id", getattr(f_obj, "formId", None))
                    for f_obj in matching_forms
                } - {None}
                allowed_form_keys = {
                    str(
                        f_obj.get("form_key", f_obj.get("formKey"))
                        if isinstance(f_obj, Mapping)
                        else getattr(f_obj, "form_key", getattr(f_obj, "formKey", ""))
                    )
                    for f_obj in matching_forms
                } - {""}
                raw_variables = [
                    var
                    for var in raw_variables
                    if (
                        (
                            var.get("form_id", var.get("formId"))
                            if isinstance(var, Mapping)
                            else getattr(var, "form_id", getattr(var, "formId", None))
                        )
                        in allowed_form_ids
                        or str(
                            var.get("form_key", var.get("formKey"))
                            if isinstance(var, Mapping)
                            else getattr(var, "form_key", getattr(var, "formKey", ""))
                        )
                        in allowed_form_keys
                    )
                ]
                raw_records = [
                    rec
                    for rec in raw_records
                    if (
                        (
                            rec.get("form_id", rec.get("formId"))
                            if isinstance(rec, Mapping)
                            else getattr(rec, "form_id", getattr(rec, "formId", None))
                        )
                        in allowed_form_ids
                        or str(
                            rec.get("form_key", rec.get("formKey"))
                            if isinstance(rec, Mapping)
                            else getattr(rec, "form_key", getattr(rec, "formKey", ""))
                        )
                        in allowed_form_keys
                    )
                ]
            else:
                raw_records = [
                    rec
                    for rec in raw_records
                    if resolve_sdtm_domain(
                        (
                            rec.get("form_key", rec.get("formKey"))
                            if isinstance(rec, Mapping)
                            else getattr(rec, "form_key", getattr(rec, "formKey", None))
                        )
                        or target_domain,
                        fallback="",
                    )
                    == target_domain
                ]

        serialized_records = _to_serializable(raw_records)
        if mask_phi:
            sanitized_records: Any = redact_batch_records(
                serialized_records, mask_phi=True, as_sdtm=True
            )
            custom_phi = _resolve_custom_phi_fields(study_key)
            if custom_phi:
                sanitized_records = mask_custom_phi_recursive(sanitized_records, custom_phi)
        else:
            sanitized_records = serialized_records

        cdash_warnings: list[str] = []
        for var in raw_variables:
            var_name = (
                var.get("variable_name", var.get("variableName"))
                if isinstance(var, Mapping)
                else getattr(var, "variable_name", getattr(var, "variableName", None))
            )
            if not var_name:
                continue
            fkey = (
                var.get("form_key", var.get("formKey"))
                if isinstance(var, Mapping)
                else getattr(var, "form_key", getattr(var, "formKey", None))
            )
            if not fkey:
                fid = (
                    var.get("form_id", var.get("formId"))
                    if isinstance(var, Mapping)
                    else getattr(var, "form_id", getattr(var, "formId", None))
                )
                fkey = form_id_to_key.get(fid)
            inferred_dom = resolve_sdtm_domain(target_domain or fkey, fallback="")
            if inferred_dom:
                valid_cdash, warning = validate_cdash_variable(str(var_name), inferred_dom)
                if not valid_cdash and warning and warning not in cdash_warnings:
                    cdash_warnings.append(warning)

        report = evaluate_standards_readiness(
            sanitized_records,
            study_key=study_key,
            standards_profile=standards_profile,
            sdtm_domain=target_domain,
            form_domain_map=(
                {
                    str(
                        f_obj.get("form_key", f_obj.get("formKey"))
                        if isinstance(f_obj, Mapping)
                        else getattr(f_obj, "form_key", getattr(f_obj, "formKey", ""))
                    ): resolve_sdtm_domain(
                        (
                            f_obj.get("form_key", f_obj.get("formKey"))
                            if isinstance(f_obj, Mapping)
                            else getattr(f_obj, "form_key", getattr(f_obj, "formKey", None))
                        ),
                        fallback="",
                    )
                    for f_obj in raw_forms
                }
                if raw_forms and target_domain is None
                else None
            ),
        )
        all_warnings = list(dict.fromkeys([*report.warnings, *cdash_warnings]))
        threshold = 100.0 if min_standards_score is None else float(min_standards_score)
        passed = (
            False
            if (report.checked_records == 0 or report.checked_fields == 0)
            else (report.is_ready if min_standards_score is None else report.score >= threshold)
        )
        serialized_violations = [
            v.model_dump() if hasattr(v, "model_dump") else str(v) for v in report.violations
        ]

        payload = {
            "study_key": study_key,
            "profile": report.profile,
            "domain": target_domain,
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
        if json_mode:
            sanitized_payload = _sanitize_workflow_payload(
                payload,
                study_key=study_key,
                mask_phi=mask_phi,
            )
            emit_json([sanitized_payload], "standards_readiness")
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

        if not passed:
            sys.exit(1)

    def _handle_validate_standards(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(validate_std_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        if args.min_standards_score is not None and not 0.0 <= args.min_standards_score <= 100.0:
            validate_std_parser.error("min-score must be between 0 and 100")
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            validate_standards(
                study_key=study_key,
                standards_profile=args.standards_profile,
                sdtm_domain=args.sdtm_domain,
                min_standards_score=args.min_standards_score,
                output_format=resolve_output_format(getattr(args, "output_format", None)),
                mask_phi=getattr(args, "mask_phi", True),
            )

    validate_std_parser.set_defaults(func=_handle_validate_standards)

    audit_parser = sub.add_parser(
        "extract-audit-trail",
        help="Extract date-bounded RecordRevision audit trails with optional 21 CFR Part 11 chain checks.",
    )
    add_study_key_args(audit_parser)
    audit_parser.add_argument(
        "--start-date",
        dest="start_date",
        default=None,
        help="Inclusive UTC lower bound on dateCreated (ISO-8601 date or timestamp).",
    )
    audit_parser.add_argument(
        "--end-date",
        dest="end_date",
        default=None,
        help="Inclusive UTC upper bound on dateCreated (ISO-8601 date or timestamp).",
    )
    audit_parser.add_argument(
        "-f",
        "--filter",
        action="append",
        help=(
            "Filter revisions by attribute (e.g., 'user==sim_user', 'recordId==601'). "
            "Repeatable; dateCreated and lastUpdated support dual lower/upper bounds."
        ),
    )
    audit_parser.add_argument(
        "--sort",
        default=None,
        help="Sort property and direction (e.g., 'recordRevision,asc').",
    )
    audit_parser.add_argument(
        "--validate-chain",
        dest="validate_chain",
        action="store_true",
        default=False,
        help="Validate monotonic 21 CFR Part 11 revision chain invariants.",
    )
    audit_parser.add_argument(
        "--allow-partial",
        dest="allow_partial",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow partial/sliced revision chains when --validate-chain is enabled "
            "(auto-enabled when date bounds or attribute filters are supplied)."
        ),
    )
    audit_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI fields with '***MASKED***' (default: enabled).",
    )
    audit_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Export audit trail directly to file ('csv', 'json', or a .csv/.json path).",
    )
    add_output_format(audit_parser)
    add_simulation_arg(audit_parser)

    @with_sdk
    def extract_audit_trail_cmd(
        sdk: ImednetSDK,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        filters: list[str] | None = None,
        sort: str | None = None,
        validate_chain: bool = False,
        allow_partial: bool | None = None,
        mask_phi: bool = True,
        output: str | None = None,
        output_format: str | None = None,
        no_interactive: bool = False,
    ) -> None:
        from imednet.spi.models import RecordRevision, validate_record_revision_chain

        resolved_format = resolve_output_format(output_format)
        parsed_filters = parse_filter_args(
            filters,
            model=RecordRevision,
            disallow_study_key=True,
            multi_bound_attributes=REVISION_DATE_ATTRIBUTES,
        )
        effective_sort = (
            sort if sort is not None else ("recordRevision,asc" if validate_chain else None)
        )
        sort_kwargs: dict[str, Any] = {"sort": effective_sort} if effective_sort else {}

        workflow = _resolve_attr("DataExtractionWorkflow")(sdk)
        if resolved_format == "json" or is_json_output_mode() or output:
            revisions = workflow.extract_audit_trail(
                study_key=study_key,
                start_date=start_date,
                end_date=end_date,
                user_filter=parsed_filters,
                **sort_kwargs,
            )
        else:
            with fetching_status("audit trail", study_key):
                revisions = workflow.extract_audit_trail(
                    study_key=study_key,
                    start_date=start_date,
                    end_date=end_date,
                    user_filter=parsed_filters,
                    **sort_kwargs,
                )

        if validate_chain:
            effective_partial = (
                allow_partial
                if allow_partial is not None
                else bool(start_date or end_date or parsed_filters)
            )
            try:
                validate_record_revision_chain(revisions, allow_partial=effective_partial)
            except ValueError as exc:
                sanitized_err = redact_cli_secrets(redact_sensitive_text(str(exc)))
                print_error(f"Chain validation failed: {sanitized_err}")
                sys.exit(1)

        if output:
            export_list_to_file(
                revisions,
                "audit_trail",
                output,
                study_key=study_key,
                mask_phi=mask_phi,
            )
            return

        masked_revisions = _mask_workflow_item(revisions, study_key=study_key, mask_phi=mask_phi)
        if resolved_format == "json":
            display_list(masked_revisions, "record_revisions", output_format="json")
            return

        display_list(
            masked_revisions,
            "audit trail",
            "No record revisions found matching the criteria.",
            fields=[
                "record_revision_id",
                "record_id",
                "subject_key",
                "form_key",
                "record_revision",
                "data_revision",
                "user",
                "reason_for_change",
                "date_created",
            ],
            details=masked_revisions,
            interactive=False if no_interactive else None,
            output_format=resolved_format,
        )

    def _handle_extract_audit_trail(args: argparse.Namespace) -> None:
        explicit_format = getattr(args, "output_format", None)
        output_arg = getattr(args, "output", None)
        if output_arg and explicit_format == "json":
            print_error("Error: Cannot combine --output with --format json.")
            sys.exit(1)

        study_key = resolve_study_key(audit_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")

        try:
            if args.start_date:
                normalize_utc_timestamp_bound(args.start_date, is_end=False)
            if args.end_date:
                normalize_utc_timestamp_bound(args.end_date, is_end=True)
            if output_arg:
                resolved_out = resolve_records_export_target(output_arg, "audit_trail")
                if resolved_out is None:
                    raise ValueError(
                        f"Invalid output format: '{output_arg}'. Supported: csv, json, "
                        "or explicit .csv/.json file paths."
                    )
        except ValueError as exc:
            print_error(f"Error: {redact_cli_secrets(redact_sensitive_text(str(exc)))}")
            sys.exit(1)

        from imednet.spi.models import RecordRevision

        parse_filter_args(
            args.filter,
            model=RecordRevision,
            disallow_study_key=True,
            multi_bound_attributes=REVISION_DATE_ATTRIBUTES,
        )

        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            extract_audit_trail_cmd(
                study_key=study_key,
                start_date=args.start_date,
                end_date=args.end_date,
                filters=args.filter,
                sort=args.sort,
                validate_chain=bool(args.validate_chain),
                allow_partial=args.allow_partial,
                mask_phi=getattr(args, "mask_phi", True),
                output=output_arg,
                output_format=resolve_output_format(explicit_format),
                no_interactive=getattr(args, "no_interactive", False),
            )

    audit_parser.set_defaults(func=_handle_extract_audit_trail)

    ingest_parser = sub.add_parser(
        "ingest-revisions",
        help="Ingest Study RecordRevision audit logs into DuckDB bronze/silver lineage tables.",
    )
    add_study_key_args(ingest_parser)
    ingest_parser.add_argument(
        "--db-path",
        dest="db_path",
        default=":memory:",
        help="Path to DuckDB database file or ':memory:' (default: :memory:).",
    )
    ingest_parser.add_argument(
        "--mode",
        choices=("append", "replace"),
        default="append",
        help="DuckDB bronze write mode ('append' deduplicates; 'replace' refreshes only this study).",
    )
    ingest_parser.add_argument(
        "--start-date",
        dest="start_date",
        default=None,
        help="Inclusive UTC lower bound on dateCreated (ISO-8601 date or timestamp).",
    )
    ingest_parser.add_argument(
        "--end-date",
        dest="end_date",
        default=None,
        help="Inclusive UTC upper bound on dateCreated (ISO-8601 date or timestamp).",
    )
    ingest_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI fields prior to DuckDB storage and file export (default: enabled).",
    )
    ingest_parser.add_argument(
        "--validate-chain",
        dest="validate_chain",
        action="store_true",
        default=False,
        help="Validate monotonic 21 CFR Part 11 revision chain invariants prior to ingestion.",
    )
    ingest_parser.add_argument(
        "--allow-partial",
        dest="allow_partial",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow partial/sliced revision chains when --validate-chain is enabled "
            "(auto-enabled when date bounds are supplied)."
        ),
    )
    ingest_parser.add_argument(
        "--include-deleted",
        dest="include_deleted",
        action="store_true",
        default=False,
        help="Include deleted=True revisions in the materialized silver_current_state view.",
    )
    ingest_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Export materialized silver_records_history rows for the study to CSV or JSON.",
    )
    add_output_format(ingest_parser)
    add_simulation_arg(ingest_parser)

    @with_sdk
    def ingest_revisions_cmd(
        sdk: ImednetSDK,
        study_key: str,
        db_path: str = ":memory:",
        mode: Literal["append", "replace"] = "append",
        start_date: str | None = None,
        end_date: str | None = None,
        mask_phi: bool = True,
        validate_chain: bool = False,
        allow_partial: bool | None = None,
        include_deleted: bool = False,
        output: str | None = None,
        output_format: str | None = None,
        no_interactive: bool = False,
    ) -> None:
        from imednet.spi.models import validate_record_revision_chain

        resolved_format = resolve_output_format(output_format)

        if validate_chain:
            effective_partial = (
                allow_partial if allow_partial is not None else bool(start_date or end_date)
            )
            revisions = _resolve_attr("DataExtractionWorkflow")(sdk).extract_audit_trail(
                study_key=study_key,
                start_date=start_date,
                end_date=end_date,
                sort="recordRevision,asc",
            )
            try:
                validate_record_revision_chain(revisions, allow_partial=effective_partial)
            except ValueError as exc:
                sanitized_err = redact_cli_secrets(redact_sensitive_text(str(exc)))
                print_error(f"Chain validation failed: {sanitized_err}")
                sys.exit(1)

        history_rows: list[dict[str, Any]] = []
        workflow_cls = _resolve_attr("DuckDBIngestionWorkflow")
        with workflow_cls(sdk, db_path=db_path, mask_phi=mask_phi) as lakehouse:
            inserted = lakehouse.ingest_revisions(
                study_key,
                start_date=start_date,
                end_date=end_date,
                mode=mode,
                mask_phi=mask_phi,
            )
            lakehouse.build_silver_view(study_key, include_deleted=include_deleted)
            if output:
                conn = getattr(lakehouse, "_connection", None)
                execute_fn = getattr(conn, "execute", None)
                if callable(execute_fn):
                    cursor = execute_fn(
                        "SELECT * FROM silver_records_history WHERE study_key = ? "
                        "ORDER BY record_id ASC NULLS LAST, record_revision ASC NULLS LAST, "
                        "record_revision_id ASC NULLS LAST",
                        [study_key],
                    )
                    desc = getattr(cursor, "description", None)
                    if isinstance(desc, (list, tuple)) and desc:
                        col_names = [str(col[0]) for col in desc]
                        for raw_row in cursor.fetchall():
                            row_dict: dict[str, Any] = {}
                            for col_name, cell in zip(col_names, raw_row, strict=False):
                                row_dict[col_name] = (
                                    cell.isoformat() if isinstance(cell, datetime) else cell
                                )
                            history_rows.append(row_dict)

        if output:
            resolved_target = resolve_records_export_target(output, "revisions_history")
            if resolved_target is None:
                print_error(
                    f"Error: Invalid output format: '{output.lower()}'. Supported: csv, json. "
                    "Explicit .csv and .json file paths are also supported."
                )
                sys.exit(1)
            fmt, target = resolved_target
            sanitized_rows = [
                _mask_workflow_item(row, study_key=study_key, mask_phi=mask_phi)
                for row in history_rows
            ]
            target.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "json":
                target.write_text(
                    json.dumps(sanitized_rows, indent=2, default=str),
                    encoding="utf-8",
                )
            else:
                headers: list[str] = []
                seen_cols: set[str] = set()
                for row in sanitized_rows:
                    for key in row:
                        if key not in seen_cols:
                            seen_cols.add(key)
                            headers.append(key)
                with target.open("w", newline="", encoding="utf-8") as fh:
                    if headers:
                        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
                        writer.writeheader()
                        for row in sanitized_rows:
                            safe_row: dict[str, Any] = {}
                            for h in headers:
                                val = row.get(h, "")
                                if val is None:
                                    safe_row[h] = ""
                                elif isinstance(val, str):
                                    safe_row[h] = sanitize_csv_formula(val)
                                else:
                                    safe_row[h] = val
                            writer.writerow(safe_row)
                    else:
                        fh.write("")
            _emit_line(f"Saved {len(sanitized_rows)} revisions_history to {target}")
            return

        summary_row = {
            "study_key": study_key,
            "db_path": db_path,
            "mode": mode,
            "inserted": int(inserted),
            "mask_phi": bool(mask_phi),
            "validated_chain": bool(validate_chain),
        }
        if resolved_format == "json":
            emit_json([summary_row], "revisions_ingestion")
            return

        display_list(
            [summary_row],
            "ingested revisions",
            details=[summary_row],
            interactive=False if no_interactive else None,
            output_format=resolved_format,
        )

    def _handle_ingest_revisions(args: argparse.Namespace) -> None:
        explicit_format = getattr(args, "output_format", None)
        output_arg = getattr(args, "output", None)
        if output_arg and explicit_format == "json":
            print_error("Error: Cannot combine --output with --format json.")
            sys.exit(1)

        study_key = resolve_study_key(ingest_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")

        try:
            if args.start_date:
                normalize_utc_timestamp_bound(args.start_date, is_end=False)
            if args.end_date:
                normalize_utc_timestamp_bound(args.end_date, is_end=True)
            if output_arg:
                resolved_out = resolve_records_export_target(output_arg, "revisions_history")
                if resolved_out is None:
                    raise ValueError(
                        f"Invalid output format: '{output_arg}'. Supported: csv, json, "
                        "or explicit .csv/.json file paths."
                    )
        except ValueError as exc:
            print_error(f"Error: {redact_cli_secrets(redact_sensitive_text(str(exc)))}")
            sys.exit(1)

        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            ingest_revisions_cmd(
                study_key=study_key,
                db_path=args.db_path,
                mode=args.mode,
                start_date=args.start_date,
                end_date=args.end_date,
                mask_phi=getattr(args, "mask_phi", True),
                validate_chain=bool(args.validate_chain),
                allow_partial=args.allow_partial,
                include_deleted=bool(args.include_deleted),
                output=output_arg,
                output_format=resolve_output_format(explicit_format),
                no_interactive=getattr(args, "no_interactive", False),
            )

    ingest_parser.set_defaults(func=_handle_ingest_revisions)


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
    subj_parser.add_argument(
        "--mask-phi",
        dest="mask_phi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask clinical PHI fields with '***MASKED***' (default: enabled).",
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
        mask_phi: bool = True,
    ) -> None:
        resolved_format = resolve_output_format(output_format)
        workflow = _resolve_attr("SubjectDataWorkflow")(sdk)
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
