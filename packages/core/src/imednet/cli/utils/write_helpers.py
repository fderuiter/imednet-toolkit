"""Shared CLI helpers for write commands (records create/import and subjects register)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

from imednet.datadict import load_from_cdisc_odm, load_from_csv, load_from_redcap
from imednet.datadict.models import DataDictionary
from imednet.models.jobs import Job, JobStatus
from imednet.sdk import ImednetSDK
from imednet.utils.batch_coordinator import DEFAULT_BATCH_CHUNK_SIZE, BatchSubmissionSummary
from imednet.utils.job_poller import JobFailedError, JobPollSummary, JobProgressHandler
from imednet.utils.tabular import (
    MASKED_SENTINEL,
    _coerce_clinical_value,
    _contains_masked_sentinel,
    _resolve_expected_var_type,
    csv_to_records,
    ensure_no_masked_sentinel,
)
from imednet.validation.cache import SchemaCache, ValidationSchemaProtocol

from ..decorators import _redact_secrets
from .args import add_output_format, add_simulation_arg, resolve_output_format
from .context import fetching_status
from .output import display_list, emit_json, format_job_progress, print_error
from .terminal import is_json_output_mode, is_non_interactive, supports_live_status


def add_write_common_args(
    parser: argparse.ArgumentParser,
    *,
    default_chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
) -> None:
    """Register standard write flags (schema, dry-run, wait, polling, output format)."""
    parser.add_argument(
        "--schema",
        dest="schema_source",
        default=None,
        help=(
            "Validation schema path (.json DataDictionary, .xml ODM, .csv REDCap, or CSV dir) "
            "or 'api' to fetch live study variables."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate payload(s) locally without sending HTTP write requests.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        default=False,
        help="Wait for submitted background job(s) to reach a terminal state.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Maximum seconds to wait when --wait is enabled (default: 300).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds when --wait is enabled (default: 5).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=default_chunk_size,
        help=f"Maximum records per POST batch chunk (default: {default_chunk_size}).",
    )
    parser.add_argument(
        "--email-notify",
        default=None,
        help="Optional email address or boolean flag for job completion notifications.",
    )
    add_output_format(parser)
    add_simulation_arg(parser)


def validate_write_numeric_flags(
    *,
    chunk_size: int,
    timeout: float,
    interval: float,
) -> None:
    """Validate common numeric write flags before SDK initialization."""
    if chunk_size <= 0:
        print_error("Error: --chunk-size must be a positive integer.")
        sys.exit(1)
    if timeout <= 0:
        print_error("Error: --timeout must be greater than 0.")
        sys.exit(1)
    if interval < 0:
        print_error("Error: --interval cannot be negative.")
        sys.exit(1)


def parse_email_notify(raw_email_notify: str | None) -> bool | str | None:
    """Normalize ``--email-notify`` value into bool or email string."""
    if raw_email_notify is None:
        return None
    low = raw_email_notify.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return raw_email_notify.strip()


def load_offline_schema(schema_source: str, *, study_key: str) -> ValidationSchemaProtocol:
    """Load an offline :class:`DataDictionary` from JSON, ODM XML, REDCap CSV, or CSV dir."""
    path = Path(schema_source)
    if not path.exists():
        raise ValueError(f"Schema path not found: '{schema_source}'")
    if path.is_dir():
        return load_from_csv(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return DataDictionary.model_validate_json(path.read_text(encoding="utf-8"))
    if suffix == ".xml":
        return load_from_cdisc_odm(path, study_key=study_key)
    if suffix == ".csv":
        return load_from_redcap(path, study_key=study_key)
    raise ValueError(
        f"Unsupported schema file format '{path.suffix}'. Expected .json, .xml, .csv, or directory."
    )


def resolve_cli_schema(
    sdk: ImednetSDK,
    study_key: str,
    schema_source: str | None,
) -> ValidationSchemaProtocol | None:
    """Resolve ``--schema`` from disk or live study API variables."""
    if not schema_source:
        return None
    if schema_source.strip().lower() == "api":
        cache = SchemaCache()
        cache.refresh(sdk.forms, sdk.variables, study_key=study_key)
        return cache
    return load_offline_schema(schema_source, study_key=study_key)


def parse_key_value_pairs(
    pairs: Sequence[str] | None,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    form_key: str | None = None,
    form_id: int | None = None,
) -> dict[str, Any]:
    """Parse repeatable ``KEY=VALUE`` pairs from ``-d/--data`` flags."""
    if not pairs:
        return {}
    parsed: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"Invalid --data entry '{item}': expected KEY=VALUE format.")
        raw_key, _, raw_val = item.partition("=")
        key = raw_key.strip()
        if not key:
            raise ValueError(f"Invalid --data entry '{item}': variable name cannot be empty.")
        if _contains_masked_sentinel(raw_val):
            raise ValueError(
                f"Variable '{key}' contains masked PHI sentinel '{MASKED_SENTINEL}' and cannot be submitted."
            )
        expected_var_type = _resolve_expected_var_type(
            schema,
            form_key,
            form_id,
            key,
            study_key=study_key,
        )
        parsed[key] = _coerce_clinical_value(
            raw_val,
            from_csv_str=True,
            expected_var_type=expected_var_type,
        )
    return parsed


def parse_json_records_text(raw_text: str, *, source_label: str) -> list[dict[str, Any]]:
    """Parse a JSON object or JSON array of record payload dicts."""
    try:
        loaded = json.loads(raw_text)
    except ValueError as exc:
        raise ValueError(f"Invalid JSON in {source_label}: {exc}") from exc

    if isinstance(loaded, dict):
        if ("items" in loaded and ("schemaVersion" in loaded or "resource" in loaded)) or (
            isinstance(loaded.get("data"), list)
            and not any(k in loaded for k in ("formKey", "form_key", "formId", "form_id"))
        ):
            raise ValueError(
                f"JSON payload in {source_label} is a list response envelope, "
                "not a Record write payload or array of Record payloads."
            )
        items = [loaded]
    elif isinstance(loaded, list) and all(isinstance(x, dict) for x in loaded):
        items = list(loaded)
    else:
        raise ValueError(
            f"JSON payload in {source_label} must be a JSON object or array of objects."
        )
    ensure_no_masked_sentinel(items)
    return items


def load_records_from_path_or_stdin(
    file_arg: str,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    default_form_key: str | None = None,
    default_form_id: int | None = None,
) -> list[dict[str, Any]]:
    """Load record payloads from ``'-'`` (stdin JSON), ``.json``, or ``.csv``."""
    if file_arg == "-":
        return parse_json_records_text(sys.stdin.read(), source_label="stdin")
    file_path = Path(file_arg)
    if not file_path.exists():
        raise ValueError(f"Input file not found: '{file_arg}'")
    if file_path.suffix.lower() == ".json":
        return parse_json_records_text(
            file_path.read_text(encoding="utf-8"), source_label=str(file_path)
        )
    return csv_to_records(
        file_path,
        schema=schema,
        study_key=study_key,
        default_form_key=default_form_key,
        default_form_id=default_form_id,
    )


@contextmanager
def write_progress_context(
    action_label: str,
    study_key: str,
    *,
    no_interactive: bool = False,
    output_format: str | None = None,
) -> Iterator[JobProgressHandler | None]:
    """Manage spinner and polling progress callbacks while honoring non-interactive/JSON modes."""
    resolved_format = resolve_output_format(output_format)
    quiet = (
        no_interactive or resolved_format == "json" or is_non_interactive() or is_json_output_mode()
    )
    if quiet:
        yield None
        return

    with fetching_status(action_label, study_key) as status_ctrl:

        def _on_progress(event: Any) -> None:
            msg = format_job_progress(event)
            if status_ctrl is not None and hasattr(status_ctrl, "update"):
                status_ctrl.update(msg)
            elif supports_live_status(sys.stderr):
                try:
                    sys.stderr.write(f"\r\033[K{msg}")
                    sys.stderr.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        yield _on_progress


def coerce_batch_summary(
    raw_result: Any,
    *,
    study_key: str,
    total_records: int,
    dry_run: bool = False,
    skipped_count: int = 0,
) -> BatchSubmissionSummary:
    """Coerce an SDK batch response (or mocked Job) into a :class:`BatchSubmissionSummary`."""
    if isinstance(raw_result, BatchSubmissionSummary):
        return raw_result
    if isinstance(raw_result, Job):
        return BatchSubmissionSummary(
            study_key=study_key,
            jobs=[raw_result],
            chunk_sizes=[total_records] if total_records else [],
            total_records=total_records,
            skipped_count=skipped_count,
            dry_run=dry_run,
        )
    return BatchSubmissionSummary(
        study_key=study_key,
        jobs=[],
        chunk_sizes=[],
        total_records=total_records,
        skipped_count=skipped_count,
        dry_run=dry_run,
    )


def summary_from_job_failure(
    exc: JobFailedError,
    *,
    study_key: str,
    total_records: int,
    skipped_count: int = 0,
) -> BatchSubmissionSummary:
    """Construct a :class:`BatchSubmissionSummary` from a raised :class:`JobFailedError`."""
    committed_jobs = getattr(exc, "jobs", None)
    if isinstance(committed_jobs, list) and committed_jobs:
        jobs: list[Job] = list(committed_jobs)
    elif exc.status is not None:
        jobs = [exc.status]
    else:
        jobs = []

    raw_chunk_sizes = getattr(exc, "chunk_sizes", None)
    if isinstance(raw_chunk_sizes, list) and raw_chunk_sizes:
        chunk_sizes = list(raw_chunk_sizes)
    elif jobs and total_records:
        chunk_sizes = [total_records]
    else:
        chunk_sizes = []

    fail_key = getattr(exc.status, "batch_id", None) or getattr(exc, "batch_id", None) or "failed"
    results_map = {j.batch_id: j for j in jobs if isinstance(j, JobStatus) and j.batch_id}
    poll_summary = JobPollSummary(
        study_key=study_key,
        results=results_map,
        failures={str(fail_key): exc},
        elapsed_total=0.0,
    )
    return BatchSubmissionSummary(
        study_key=study_key,
        jobs=jobs,
        chunk_sizes=chunk_sizes,
        total_records=total_records,
        skipped_count=skipped_count,
        poll_summary=poll_summary,
    )


def _serialize_job_for_json(job: Job) -> dict[str, Any]:
    """Serialize a :class:`Job` or :class:`JobStatus` including computed record counts."""
    dumped = job.model_dump(mode="json", by_alias=True)
    if isinstance(job, JobStatus):
        dumped["totalRecords"] = job.total_records
        dumped["successfulRecords"] = job.successful_records
        dumped["failedRecords"] = job.failed_records
        if job.result_error is not None:
            dumped["resultError"] = job.result_error
    return dumped


def _summary_to_json_item(summary: BatchSubmissionSummary) -> dict[str, Any]:
    return {
        "studyKey": summary.study_key,
        "dryRun": summary.dry_run,
        "totalRecords": summary.total_records,
        "skippedCount": summary.skipped_count,
        "chunkCount": summary.chunk_count,
        "batchIds": summary.batch_ids,
        "allSuccessful": summary.all_successful,
        "jobs": [_serialize_job_for_json(j) for j in summary.jobs],
        "validationReport": (
            summary.validation_report.to_dict() if summary.validation_report is not None else None
        ),
    }


def _format_failed_summary_error(summary: BatchSubmissionSummary) -> str:
    """Build a concise stderr diagnostic for failed Job runs in *summary*."""
    details: list[str] = []
    for j in summary.jobs:
        failed_cnt = getattr(j, "failed_records", 0)
        res_err = getattr(j, "result_error", None)
        if j.is_failed or failed_cnt > 0 or res_err:
            bid = j.batch_id or j.job_id or "unknown"
            parts = [f"state={j.state}"]
            if failed_cnt > 0:
                parts.append(f"failedRecords={failed_cnt}")
            if res_err:
                parts.append(f"resultError={res_err}")
            details.append(f"Job {bid} ({', '.join(parts)})")
    if details:
        return f"Job Error: {'; '.join(details)}"
    return f"Job Error: one or more jobs failed for study '{summary.study_key}'."


def render_write_summary(
    summary: BatchSubmissionSummary,
    *,
    resource: str,
    output_format: str | None = None,
) -> None:
    """Render a :class:`BatchSubmissionSummary` in human table or JSON mode and exit 1 if failed."""
    resolved_format = resolve_output_format(output_format)
    if summary.validation_report is not None and not summary.validation_report.is_valid:
        if resolved_format == "json":
            emit_json([_summary_to_json_item(summary)], resource)
        else:
            print_error(_redact_secrets(summary.validation_report.format_summary()))
        sys.exit(1)

    if resolved_format == "json":
        emit_json([_summary_to_json_item(summary)], resource)
        if not summary.all_successful:
            print_error(_redact_secrets(_format_failed_summary_error(summary)))
            sys.exit(1)
        return

    if summary.dry_run:
        valid_count = (
            summary.validation_report.valid_records
            if summary.validation_report is not None
            else summary.total_records
        )
        msg = (
            f"Dry run succeeded for study '{summary.study_key}': {valid_count} record(s) validated"
        )
        if summary.skipped_count:
            msg += f" ({summary.skipped_count} skipped)"
        print(f"{msg}. No HTTP write requests were sent.")
        return

    if not summary.jobs:
        if not summary.all_successful:
            print_error(_redact_secrets(_format_failed_summary_error(summary)))
            sys.exit(1)
        msg = f"No records submitted for study '{summary.study_key}'"
        if summary.skipped_count:
            msg += f" ({summary.skipped_count} already-enrolled skipped)"
        print(f"{msg}.")
        return

    rows = [
        {
            "batch_id": j.batch_id,
            "job_id": j.job_id,
            "state": j.state,
            "progress": getattr(j, "progress", None),
        }
        for j in summary.jobs
    ]
    display_list(
        rows,
        resource,
        fields=["batch_id", "job_id", "state", "progress"],
        output_format="table",
        details=summary.jobs,
        interactive=False,
    )
    if not summary.all_successful:
        print_error(_redact_secrets(_format_failed_summary_error(summary)))
        sys.exit(1)


def exit_cli_validation_error(
    exc: Exception,
    *,
    resource: str = "validation_error",
    study_key: str | None = None,
    output_format: str | None = None,
) -> NoReturn:
    """Print a secret-redacted CLI validation error, emit JSON diagnostics in JSON mode, and exit 1."""
    redacted_msg = _redact_secrets(str(exc))
    print_error(f"Validation Error: {redacted_msg}")
    resolved_format = resolve_output_format(output_format)
    if resolved_format == "json":
        val_report = getattr(exc, "validation_report", None)
        diagnostic_item: dict[str, Any] = {
            "studyKey": study_key,
            "allSuccessful": False,
            "error": redacted_msg,
            "validationReport": (
                val_report.to_dict()
                if val_report is not None and hasattr(val_report, "to_dict")
                else None
            ),
        }
        emit_json([diagnostic_item], resource)
    sys.exit(1)
