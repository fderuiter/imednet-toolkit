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
import io
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

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
    load_offline_schema,
    parse_email_notify,
    parse_filter_args,
    positive_int,
    print_error,
    redact_cli_secrets,
    resolve_cli_schema,
    resolve_hybrid_study_and_key,
    resolve_output_format,
    resolve_study_key,
    simulation_mode_context,
    validate_cli_path_segment,
    with_sdk,
)
from imednet.spi.models import PROFILE_REGISTRY
from imednet.spi.utils import (
    PathTraversalValidationError,
    SensitivityRegistry,
    build_study_sensitivity_registry,
    global_sensitivity_registry,
    mask_clinical_phi,
    mask_custom_phi_recursive,
    redact_sensitive_text,
    resolve_study_configuration,
    sanitize_diagnostic_text,
    validate_path_segment,
)
from imednet.spi.validation import DataDictionary, ValidationSchemaProtocol

from ._audit_cli import setup_audit_parsers

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


def _reject_interactive_json_mode(interactive: bool, output_format: str | None) -> None:
    """Exit 1 on stderr with empty stdout when --interactive conflicts with JSON output mode."""
    if not interactive:
        return
    resolved = resolve_output_format(output_format)
    if resolved == "json" or (output_format is None and is_json_output_mode()):
        print_error(
            "Error: Cannot combine --interactive with --format json or IMEDNET_CLI_OUTPUT_FORMAT=json."
        )
        sys.exit(1)


def _exit_uat_validation_error(
    exc: BaseException | str,
    *,
    study_key: str | None = None,
) -> NoReturn:
    """Emit a secret- and PHI-redacted UAT validation error to stderr and exit 1."""
    safe_phi_study: str | None = None
    if isinstance(study_key, str) and study_key.strip():
        with contextlib.suppress(Exception):
            validate_path_segment(study_key.strip(), name="study_key")
            safe_phi_study = study_key.strip()
    custom_phi = _resolve_custom_phi_fields(safe_phi_study)
    sanitized = _sanitize_uat_error_text(str(exc), custom_phi_fields=custom_phi)
    prefix = "" if sanitized.startswith("Validation Error:") else "Validation Error: "
    print_error(f"{prefix}{sanitized}")
    sys.exit(1)


def _validate_uat_subject_count(subject_count: int) -> int:
    """Validate --subject-count bounds (1..100) during CLI preflight."""
    if isinstance(subject_count, bool) or not 1 <= subject_count <= 100:
        raise ValueError("subject_count must be between 1 and 100.")
    return subject_count


def _load_uat_data_dictionary(
    dd_path: str | None,
    *,
    study_key: str,
) -> DataDictionary | None:
    """Load an offline DataDictionary from JSON, CDISC ODM XML, REDCap CSV, ZIP, or CSV dir."""
    if not dd_path:
        return None
    loaded = load_offline_schema(dd_path, study_key=study_key)
    if not isinstance(loaded, DataDictionary):
        raise ValueError(
            f"Unsupported Data Dictionary source '{dd_path}': expected JSON, ODM XML, REDCap CSV, ZIP, or CSV directory."
        )
    return loaded


def _sanitize_phi_issue_message(
    message: str,
    *,
    variable_name: str | None,
    custom_phi_fields: set[str],
) -> str:
    """Redact credentials, sensitive URIs, and clinical PHI values in issue messages."""
    active_registry = build_study_sensitivity_registry(custom_phi_fields)

    sanitized = redact_cli_secrets(
        sanitize_diagnostic_text(redact_sensitive_text(message), registry=active_registry)
    )

    if variable_name is None:
        inferred = re.search(r"variable '([A-Za-z0-9_-]+)'", sanitized)
        if inferred:
            variable_name = inferred.group(1)

    if isinstance(variable_name, str) and _is_key_phi_sensitive(variable_name, custom_phi_fields):

        def _mask_quote(match: re.Match[str]) -> str:
            inner = match.group(1)
            if inner == variable_name:
                return match.group(0)
            return "'***MASKED***'"

        sanitized = re.sub(r"'([^']*)'", _mask_quote, sanitized)
    return sanitized


def _sanitize_validation_report_dict(
    report_dict: dict[str, Any],
    *,
    custom_phi_fields: set[str],
) -> dict[str, Any]:
    """Sanitize validationReport issue messages for secrets and clinical PHI."""
    cleaned = dict(report_dict)
    for list_key in ("errors", "warnings"):
        raw_items = cleaned.get(list_key)
        if not isinstance(raw_items, list):
            continue
        sanitized_items: list[Any] = []
        for entry in raw_items:
            if not isinstance(entry, Mapping):
                sanitized_items.append(entry)
                continue
            rebuilt_entry = dict(entry)
            raw_msg = rebuilt_entry.get("message")
            var_name = rebuilt_entry.get("variableName")
            if isinstance(raw_msg, str):
                rebuilt_entry["message"] = _sanitize_phi_issue_message(
                    raw_msg,
                    variable_name=var_name if isinstance(var_name, str) else None,
                    custom_phi_fields=custom_phi_fields,
                )
            sanitized_items.append(rebuilt_entry)
        cleaned[list_key] = sanitized_items
    return cleaned


def _sanitize_uat_error_text(text: str, *, custom_phi_fields: set[str]) -> str:
    """Redact secrets and mask PHI values across multi-line UAT error summaries."""
    sanitized_lines: list[str] = []
    for line in text.splitlines():
        var_match = re.search(r"\[variable=([^\]]+)\]", line)
        var_name = var_match.group(1) if var_match else None
        sanitized_lines.append(
            _sanitize_phi_issue_message(
                line,
                variable_name=var_name,
                custom_phi_fields=custom_phi_fields,
            )
        )
    return "\n".join(sanitized_lines)


def _emit_uat_json_failure(
    exc: BaseException | str,
    *,
    study_key: str | None,
    dry_run: bool,
    failure_phase: str = "PREFLIGHT",
) -> NoReturn:
    """Emit a single PHI-masked, secret-redacted failed uat_run JSON envelope and exit 1."""
    resolved_study = study_key.strip() if isinstance(study_key, str) and study_key.strip() else ""
    safe_phi_study: str | None = None
    if resolved_study:
        with contextlib.suppress(Exception):
            validate_path_segment(resolved_study, name="study_key")
            safe_phi_study = resolved_study
    custom_phi = _resolve_custom_phi_fields(safe_phi_study)
    sanitized_err = _sanitize_uat_error_text(str(exc), custom_phi_fields=custom_phi)
    item: dict[str, Any] = {
        "runId": "",
        "studyKey": resolved_study,
        "dryRun": bool(dry_run),
        "state": "FAILED",
        "stateHistory": [failure_phase, "FAILED"] if failure_phase else ["FAILED"],
        "overallSuccess": False,
        "formsTested": 0,
        "recordsGenerated": 0,
        "recordsSubmitted": 0,
        "jobsMonitored": 0,
        "failurePhase": failure_phase,
        "errorMessage": sanitized_err,
        "validationReport": None,
        "phaseDurations": {},
    }
    masked = _mask_workflow_value(item, custom_phi_fields=custom_phi)
    emit_json([masked if isinstance(masked, dict) else item], "uat_run", status="failed")
    sys.exit(1)


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
        val_rep_dict = _sanitize_validation_report_dict(
            val_rep.to_dict(),
            custom_phi_fields=custom_phi_fields,
        )
    elif isinstance(val_rep, dict):
        val_rep_dict = _sanitize_validation_report_dict(
            val_rep,
            custom_phi_fields=custom_phi_fields,
        )
    else:
        val_rep_dict = None

    err_msg = getattr(result, "error_message", None)
    sanitized_err = (
        _sanitize_uat_error_text(err_msg, custom_phi_fields=custom_phi_fields)
        if isinstance(err_msg, str)
        else None
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
        registry if registry is not None else build_study_sensitivity_registry(custom_phi_fields)
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
            elif isinstance(v, str) and key_str in {"reason_for_change", "reasonForChange"}:
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
            if isinstance(raw_val, str) and fname == "reason_for_change":
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
            candidate_extra = _mask_workflow_value(
                extra,
                custom_phi_fields=custom_phi_fields,
                registry=active_registry,
            )
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
    output_format: str | None = None,
) -> None:
    """Execute high-water mark timestamp update for a study/stream in the ledger."""
    _reject_interactive_json_mode(interactive, output_format)
    if study_key is not None:
        validate_cli_path_segment(study_key, name="study_key")
    if stream is not None:
        validate_cli_path_segment(stream, name="stream")

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
            if resolve_output_format(output_format) == "json":
                emit_json(
                    [
                        {
                            "studyKey": study_key,
                            "streamName": stream,
                            "lastTimestamp": dt.isoformat(),
                            "recordsProcessed": records_processed,
                            "status": "success",
                            "errorMessage": None,
                        }
                    ],
                    "ledger_state",
                )
            else:
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
            force=force,
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
    force: bool = False,
    output_format: str | None = None,
) -> None:
    """Execute high-water mark state deletion for a study/stream."""
    _reject_interactive_json_mode(interactive, output_format)
    if study_key is not None:
        validate_cli_path_segment(study_key, name="study_key")
    if stream is not None:
        validate_cli_path_segment(stream, name="stream")

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
            removed = (
                ledger.delete_entry(study_key, stream, force=True)
                if force
                else ledger.delete_entry(study_key, stream)
            )
            if resolve_output_format(output_format) == "json":
                emit_json(
                    [{"studyKey": study_key, "streamName": stream, "removed": bool(removed)}],
                    "ledger_state",
                )
                return
            if removed:
                _emit_line(f"Successfully reset stream '{stream}' for study '{study_key}'.")
            else:
                _emit_line(f"No stream '{stream}' found for study '{study_key}'.")
                return
        else:
            removed = (
                ledger.delete_entry(study_key, force=True)
                if force
                else ledger.delete_entry(study_key)
            )
            if resolve_output_format(output_format) == "json":
                emit_json(
                    [{"studyKey": study_key, "streamName": None, "removed": bool(removed)}],
                    "ledger_state",
                )
                return
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
    _reject_interactive_json_mode(interactive, output_format)
    resolved_format = resolve_output_format(output_format)
    if study_key is not None:
        validate_cli_path_segment(study_key, name="study_key")

    if interactive:
        from .wizard import ensure_interactive_stdin, prompt_input

        ensure_interactive_stdin(force_non_interactive=no_interactive)
        study_input = prompt_input(
            "Enter study key to filter (leave blank for all studies)",
            default=study_key or "",
        )
        study_key = study_input if study_input else None
        if study_key is not None:
            validate_cli_path_segment(study_key, name="study_key")

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


def _resolve_state_study_key(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> str | None:
    """Resolve study_key for state commands, exiting 1 on blank/whitespace or path-traversal keys."""
    flag_val = getattr(args, "study_key_flag", None)
    pos_val = getattr(args, "study_key", None)
    if isinstance(flag_val, str) and not flag_val.strip():
        validate_cli_path_segment(flag_val, name="study_key")
    if isinstance(pos_val, str) and not pos_val.strip():
        validate_cli_path_segment(pos_val, name="study_key")
    study_key = resolve_study_key(parser, args, required=False)
    if study_key is not None:
        validate_cli_path_segment(study_key, name="study_key")
    return study_key


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
        _reject_interactive_json_mode(args.interactive, getattr(args, "output_format", None))
        study_key = _resolve_state_study_key(show_parser, args)
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
    add_output_format(set_parser)

    def _dispatch_set_state(args: argparse.Namespace) -> None:
        _reject_interactive_json_mode(args.interactive, getattr(args, "output_format", None))
        study_key = _resolve_state_study_key(set_parser, args)
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
            output_format=getattr(args, "output_format", None),
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
        "-f",
        "--force",
        action="store_true",
        help="Force state reset even when an active RUNNING lease is held.",
    )
    reset_parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive wizard mode."
    )
    add_output_format(reset_parser)

    def _dispatch_reset_state(args: argparse.Namespace) -> None:
        _reject_interactive_json_mode(args.interactive, getattr(args, "output_format", None))
        study_key = _resolve_state_study_key(reset_parser, args)
        _execute_reset_state(
            study_key=study_key,
            stream=args.stream,
            ledger_path=args.ledger_path,
            interactive=args.interactive,
            parser=reset_parser,
            no_interactive=getattr(args, "no_interactive", False),
            force=getattr(args, "force", False),
            output_format=getattr(args, "output_format", None),
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

        _reject_interactive_json_mode(interactive, output_format)
        resolved_format = resolve_output_format(output_format)
        json_mode = resolved_format == "json" or (output_format is None and is_json_output_mode())

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
        raw_format = getattr(args, "output_format", None)
        _reject_interactive_json_mode(args.interactive, raw_format)
        output_format = resolve_output_format(raw_format)
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
        help="Offline Data Dictionary path (.json, .xml ODM, .csv REDCap, .zip, or CSV directory).",
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
        try:
            subject_count = _validate_uat_subject_count(args.subject_count)
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
        default=None,
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
        help="Offline Data Dictionary path (.json, .xml ODM, .csv REDCap, .zip, or CSV directory).",
    )
    uat_run_parser.add_argument(
        "--schema",
        dest="schema_source",
        default=None,
        help=(
            "Validation schema path (.json, .xml ODM, .csv REDCap, .zip, or CSV dir) "
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
        subject_count: int | None = None,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        email_notify: bool | str | None = None,
        no_interactive: bool = False,
        *,
        spec: Any = None,
        data_dictionary: DataDictionary | None = None,
        data_dictionary_path: str | None = None,
        preloaded_schema: ValidationSchemaProtocol | None = None,
        schema_source: str | None = None,
        dry_run: bool = False,
        seed: int | None = None,
        output_format: str | None = None,
        _entered_marker: list[bool] | None = None,
    ) -> None:
        if _entered_marker is not None:
            _entered_marker.append(True)

        from .uat import UATWorkflow, UATWorkflowState
        from .wizard import (
            ensure_interactive_stdin,
            print_progress,
            prompt_email_notify,
            prompt_input,
            prompt_study_key,
            prompt_subject_count,
        )

        _reject_interactive_json_mode(interactive, output_format)
        resolved_format = resolve_output_format(output_format)
        json_mode = resolved_format == "json" or (output_format is None and is_json_output_mode())

        effective_subject_count: int | None = subject_count
        if interactive:
            ensure_interactive_stdin(force_non_interactive=no_interactive)
            study_key = prompt_study_key(study_key)
            if subject_count is not None:
                spec_default_count = subject_count
            elif spec is not None and getattr(spec, "subject_specs", None):
                raw_sc = getattr(spec.subject_specs[0], "subject_count", 2)
                spec_default_count = (
                    int(raw_sc) if isinstance(raw_sc, int) and not isinstance(raw_sc, bool) else 2
                )
            elif spec is not None and getattr(spec, "form_specs", None):
                raw_fc = getattr(spec.form_specs[0], "subject_count", 2)
                spec_default_count = (
                    int(raw_fc) if isinstance(raw_fc, int) and not isinstance(raw_fc, bool) else 2
                )
            else:
                spec_default_count = 2
            spec_default_count = min(max(spec_default_count, 1), 100)
            prompted_count = prompt_subject_count(
                "Enter number of test subjects", default=spec_default_count
            )
            if spec is not None and subject_count is None and prompted_count == spec_default_count:
                effective_subject_count = None
            else:
                effective_subject_count = prompted_count

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
            if not dry_run:
                email_notify = prompt_email_notify(default=email_notify)
        elif not study_key:
            uat_run_parser.error("the following arguments are required: study_key")
        elif spec is None and effective_subject_count is None:
            effective_subject_count = 2

        assert study_key is not None
        try:
            validate_path_segment(study_key, name="study_key")
        except PathTraversalValidationError as exc:
            if json_mode:
                _emit_uat_json_failure(
                    exc,
                    study_key=study_key,
                    dry_run=dry_run,
                    failure_phase="PREFLIGHT",
                )
            validate_cli_path_segment(study_key, name="study_key")

        if spec is not None and getattr(spec, "study_key", study_key) != study_key:
            mismatch_msg = (
                f"Specification studyKey '{spec.study_key}' does not match study '{study_key}'."
            )
            if json_mode:
                _emit_uat_json_failure(
                    mismatch_msg,
                    study_key=study_key,
                    dry_run=dry_run,
                    failure_phase="PREFLIGHT",
                )
            _exit_uat_validation_error(mismatch_msg)

        try:
            if data_dictionary is None and data_dictionary_path:
                data_dictionary = _load_uat_data_dictionary(
                    data_dictionary_path,
                    study_key=study_key,
                )
            if (
                preloaded_schema is None
                and schema_source
                and schema_source.strip().lower() != "api"
            ):
                preloaded_schema = load_offline_schema(schema_source, study_key=study_key)
            resolved_schema = (
                preloaded_schema
                if preloaded_schema is not None
                else resolve_cli_schema(sdk, study_key, schema_source)
            )
        except Exception as exc:
            if json_mode:
                phase = (
                    "INSPECTING"
                    if schema_source and schema_source.strip().lower() == "api"
                    else "PREFLIGHT"
                )
                _emit_uat_json_failure(
                    exc,
                    study_key=study_key,
                    dry_run=dry_run,
                    failure_phase=phase,
                )
            if isinstance(exc, (ValueError, OSError)):
                _exit_uat_validation_error(exc)
            raise

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
            "subject_count": effective_subject_count,
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
        except Exception as exc:
            if json_mode:
                last_res = getattr(workflow, "last_result", None)
                if last_res is not None and isinstance(getattr(last_res, "run_id", None), str):
                    custom_phi = _resolve_custom_phi_fields(study_key)
                    item = _serialize_uat_run_result(
                        last_res,
                        study_key=study_key,
                        dry_run=dry_run,
                        custom_phi_fields=custom_phi,
                    )
                    emit_json([item], "uat_run", status="failed")
                    sys.exit(1)
                _emit_uat_json_failure(
                    exc,
                    study_key=study_key,
                    dry_run=dry_run,
                    failure_phase="PREFLIGHT",
                )
            if isinstance(exc, (ValueError, OSError)):
                _exit_uat_validation_error(exc)
            raise

        result_state = getattr(result, "state", None)
        is_success = bool(result.overall_success) and result_state != UATWorkflowState.FAILED

        if json_mode:
            custom_phi = _resolve_custom_phi_fields(study_key)
            item = _serialize_uat_run_result(
                result,
                study_key=study_key,
                dry_run=dry_run,
                custom_phi_fields=custom_phi,
            )
            emit_json([item], "uat_run", status="complete" if is_success else "failed")
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
        dry_run_flag = bool(getattr(args, "dry_run", False))
        _reject_interactive_json_mode(args.interactive, raw_format)
        resolved_format = resolve_output_format(raw_format)
        json_mode = not args.interactive and (
            resolved_format == "json" or (raw_format is None and is_json_output_mode())
        )

        def _fail_preflight(
            exc: BaseException | str, *, resolved_study: str | None = None
        ) -> NoReturn:
            if json_mode:
                _emit_uat_json_failure(
                    exc,
                    study_key=resolved_study,
                    dry_run=dry_run_flag,
                    failure_phase="PREFLIGHT",
                )
            _exit_uat_validation_error(exc)

        raw_flag_study = getattr(args, "study_key_flag", None)
        raw_pos_study = getattr(args, "study_key", None)
        candidate_study = (
            raw_flag_study
            if isinstance(raw_flag_study, str) and raw_flag_study.strip()
            else (
                raw_pos_study if isinstance(raw_pos_study, str) and raw_pos_study.strip() else None
            )
        )

        subject_count: int | None = None
        if args.subject_count is not None:
            try:
                subject_count = _validate_uat_subject_count(args.subject_count)
            except ValueError as exc:
                _fail_preflight(exc, resolved_study=candidate_study)

        try:
            normalized_email_notify = parse_email_notify(args.email_notify)
        except ValueError as exc:
            _fail_preflight(exc, resolved_study=candidate_study)

        loaded_spec: Any = None
        if getattr(args, "spec_path", None):
            from .uat import UATSpecification

            spec_file = Path(args.spec_path)
            if not spec_file.is_file():
                _fail_preflight(
                    f"Specification file not found: '{args.spec_path}'",
                    resolved_study=candidate_study,
                )
            try:
                loaded_spec = UATSpecification.from_json(spec_file)
            except (ValueError, OSError) as exc:
                _fail_preflight(
                    f"Invalid UAT specification '{args.spec_path}': {exc}",
                    resolved_study=candidate_study,
                )

        if not args.interactive:
            if isinstance(raw_flag_study, str) and not raw_flag_study.strip():
                _fail_preflight(
                    "Invalid study_key: segment must not be empty.",
                    resolved_study="",
                )
            if isinstance(raw_pos_study, str) and not raw_pos_study.strip():
                _fail_preflight(
                    "Invalid study_key: segment must not be empty.",
                    resolved_study="",
                )

        study_key = resolve_study_key(uat_run_parser, args, required=False)
        if study_key is None and loaded_spec is not None:
            study_key = loaded_spec.study_key

        loaded_dd: DataDictionary | None = None
        preloaded_schema: ValidationSchemaProtocol | None = None
        raw_dd_path = getattr(args, "data_dictionary", None)
        raw_schema_src = getattr(args, "schema_source", None)

        if args.interactive:
            from .wizard import ensure_interactive_stdin

            ensure_interactive_stdin(force_non_interactive=no_interactive)
            if study_key is not None:
                validate_cli_path_segment(study_key, name="study_key")
        else:
            if not study_key:
                uat_run_parser.error("the following arguments are required: study_key")
            assert study_key is not None
            try:
                validate_path_segment(study_key, name="study_key")
            except PathTraversalValidationError as exc:
                if json_mode:
                    _emit_uat_json_failure(
                        exc,
                        study_key=study_key,
                        dry_run=dry_run_flag,
                        failure_phase="PREFLIGHT",
                    )
                validate_cli_path_segment(study_key, name="study_key")
            if loaded_spec is not None and loaded_spec.study_key != study_key:
                _fail_preflight(
                    f"Specification studyKey '{loaded_spec.study_key}' does not match study '{study_key}'.",
                    resolved_study=study_key,
                )
            try:
                loaded_dd = _load_uat_data_dictionary(raw_dd_path, study_key=study_key)
                preloaded_schema = (
                    load_offline_schema(raw_schema_src, study_key=study_key)
                    if raw_schema_src and raw_schema_src.strip().lower() != "api"
                    else None
                )
            except (ValueError, OSError) as exc:
                _fail_preflight(exc, resolved_study=study_key)

        entered_marker: list[bool] = []
        uat_call_kwargs: dict[str, Any] = {
            "study_key": study_key,
            "interactive": args.interactive,
            "subject_count": subject_count,
            "site_name": args.site_name,
            "enabled_form_keys": args.enabled_form_keys,
            "email_notify": normalized_email_notify,
            "no_interactive": no_interactive,
            "spec": loaded_spec,
            "data_dictionary": loaded_dd,
            "data_dictionary_path": raw_dd_path,
            "preloaded_schema": preloaded_schema,
            "schema_source": raw_schema_src,
            "dry_run": dry_run_flag,
            "seed": getattr(args, "seed", None),
            "output_format": raw_format,
            "_entered_marker": entered_marker,
        }
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            if json_mode:
                sdk_err_buf = io.StringIO()
                try:
                    with contextlib.redirect_stderr(sdk_err_buf):
                        uat_run(**uat_call_kwargs)
                except SystemExit as exc:
                    if not entered_marker and exc.code not in (0, None, 130):
                        captured_err = (
                            sdk_err_buf.getvalue().strip() or "SDK initialization failed."
                        )
                        _emit_uat_json_failure(
                            captured_err,
                            study_key=study_key,
                            dry_run=dry_run_flag,
                            failure_phase="PREFLIGHT",
                        )
                    captured = sdk_err_buf.getvalue()
                    if captured:
                        print_error(captured, end="")
                    raise
            else:
                uat_run(**uat_call_kwargs)

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
            build_form_sdtm_domain_map,
            evaluate_standards_readiness,
            filter_forms_by_sdtm_domain,
            merge_form_ids_into_record_filter,
            record_matches_sdtm_domain,
            redact_batch_records,
            resolve_form_sdtm_domain,
        )
        from imednet.spi.sinks import resolve_study_configuration

        validate_path_segment(study_key, name="study_key")
        if min_standards_score is not None and not 0.0 <= min_standards_score <= 100.0:
            validate_std_parser.error("min-score must be between 0 and 100")

        resolved_fmt = resolve_output_format(output_format)
        json_mode = resolved_fmt == "json" or is_json_output_mode()
        target_domain = (
            resolve_sdtm_domain(sdtm_domain, fallback="") if sdtm_domain else ""
        ) or None
        study_cfg = resolve_study_configuration(study_key)

        def _fetch_inputs() -> tuple[list[Any], list[Any], list[Any]]:
            forms = list(sdk.forms.list(study_key=study_key))
            variables = list(sdk.variables.list(study_key=study_key))
            rec_kwargs: dict[str, Any] = {}
            if target_domain is not None and forms:
                matched_for_fetch = filter_forms_by_sdtm_domain(
                    forms,
                    target_domain,
                    study_config=study_cfg,
                )
                if not matched_for_fetch:
                    return forms, variables, []
                merged_rec_filter = merge_form_ids_into_record_filter(None, matched_for_fetch)
                if isinstance(merged_rec_filter, Mapping):
                    rec_kwargs.update(merged_rec_filter)
            recs = [
                rec
                for rec in sdk.records.list(study_key=study_key, **rec_kwargs)
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

        cli_form_map: dict[Any, str] = build_form_sdtm_domain_map(
            raw_forms,
            study_config=study_cfg,
        )
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
            if fkey and fkey not in cli_form_map:
                fallback_dom = resolve_sdtm_domain(str(fkey), fallback="")
                if fallback_dom:
                    cli_form_map[str(fkey)] = fallback_dom
                    if fid is not None and fid not in cli_form_map:
                        cli_form_map[fid] = fallback_dom

        def _resolve_item_domain(item: Any) -> str:
            fid = (
                item.get("form_id", item.get("formId"))
                if isinstance(item, Mapping)
                else getattr(item, "form_id", getattr(item, "formId", None))
            )
            fkey = (
                item.get("form_key", item.get("formKey"))
                if isinstance(item, Mapping)
                else getattr(item, "form_key", getattr(item, "formKey", None))
            )
            if not fkey and fid is not None:
                fkey = form_id_to_key.get(fid)
            resolved_dom = resolve_form_sdtm_domain(
                item if fkey is None else fkey,
                study_config=study_cfg,
                form_domain_map=cli_form_map or None,
                fallback="",
            )
            if not resolved_dom and fid is not None and fid in cli_form_map:
                resolved_dom = cli_form_map[fid]
            if resolved_dom:
                return resolved_dom
            if isinstance(fkey, str) and fkey.strip():
                return resolve_sdtm_domain(fkey, fallback="")
            raw_dom = (
                item.get("DOMAIN") or item.get("domain")
                if isinstance(item, Mapping)
                else getattr(item, "domain", None)
            )
            if isinstance(raw_dom, str) and raw_dom.strip():
                return resolve_sdtm_domain(raw_dom, fallback="")
            return ""

        if target_domain is not None:
            raw_variables = [
                var for var in raw_variables if _resolve_item_domain(var) == target_domain
            ]
            raw_records = [
                rec
                for rec in raw_records
                if record_matches_sdtm_domain(
                    rec,
                    target_domain,
                    form_domain_map=cli_form_map,
                    study_config=study_cfg,
                )
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
            inferred_dom = _resolve_item_domain(var)
            if inferred_dom:
                valid_cdash, warning = validate_cdash_variable(str(var_name), inferred_dom)
                if not valid_cdash and warning and warning not in cdash_warnings:
                    cdash_warnings.append(warning)

        report = evaluate_standards_readiness(
            sanitized_records,
            study_key=study_key,
            standards_profile=standards_profile,
            sdtm_domain=target_domain,
            form_domain_map=cli_form_map if (cli_form_map or raw_forms) else None,
        )
        all_warnings = list(dict.fromkeys([*report.warnings, *cdash_warnings]))
        threshold = 100.0 if min_standards_score is None else float(min_standards_score)
        has_domain_level_error = any(
            v.severity.upper() == "ERROR" and v.field in {*report.domain_scores, "study"}
            for v in report.violations
        )
        passed = (
            False
            if (report.checked_records == 0 or report.checked_fields == 0 or has_domain_level_error)
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

    setup_audit_parsers(
        sub,
        resolve_attr=_resolve_attr,
        resolve_custom_phi_fields=_resolve_custom_phi_fields,
        mask_workflow_item=_mask_workflow_item,
        emit_line=_emit_line,
    )


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
