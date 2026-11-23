"""Shared CLI helpers for write commands (records create/import and subjects register)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from imednet.datadict import load_from_cdisc_odm, load_from_csv, load_from_redcap
from imednet.datadict.models import DataDictionary
from imednet.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    ValidationError,
)
from imednet.models.jobs import Job
from imednet.sdk import ImednetSDK
from imednet.utils.batch_coordinator import DEFAULT_BATCH_CHUNK_SIZE, BatchSubmissionSummary
from imednet.utils.job_poller import JobFailedError, JobProgressHandler, JobTimeoutError
from imednet.utils.tabular import (
    MASKED_SENTINEL,
    _coerce_clinical_value,
    _contains_masked_sentinel,
    _resolve_expected_var_type,
    csv_text_to_records,
    csv_to_records,
    ensure_no_masked_sentinel,
)
from imednet.validation.cache import SchemaCache, ValidationSchemaProtocol

from ..decorators import _redact_secrets
from .args import add_output_format, add_simulation_arg, resolve_output_format
from .context import fetching_status
from .output import display_list, emit_json, format_job_progress, print_error
from .terminal import is_json_output_mode, is_non_interactive, is_tty, supports_live_status

_WRITE_RESOURCE_LABELS: dict[str, str] = {
    "records_create": "record submission jobs",
    "records_import": "record import jobs",
    "subjects_register": "subject registration jobs",
}

_WRITE_JOB_TABLE_FIELDS: list[str] = [
    "batch_id",
    "job_id",
    "state",
    "progress",
    "total_records",
    "failed_records",
]


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


def guard_stdin_dash_tty(file_arg: str | None) -> None:
    """Exit 1 before SDK initialization if ``'-'`` is requested on an interactive TTY stdin."""
    if file_arg == "-" and is_tty(sys.stdin):
        print_error(
            "Error: Cannot read '-' from an interactive terminal (TTY) stdin; "
            "pipe JSON or CSV data into stdin or pass a file path."
        )
        sys.exit(1)


def load_offline_schema(schema_source: str, *, study_key: str) -> ValidationSchemaProtocol:
    """Load an offline :class:`DataDictionary` from JSON, ODM XML, REDCap CSV, or CSV dir/zip."""
    path = Path(schema_source)
    if not path.exists():
        raise ValueError(f"Schema path not found: '{schema_source}'")
    if path.is_dir():
        return load_from_csv(path, study_key=study_key)
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return load_from_csv(path, study_key=study_key)
    if suffix == ".json":
        return DataDictionary.model_validate_json(path.read_text(encoding="utf-8"))
    if suffix == ".xml":
        return load_from_cdisc_odm(path, study_key=study_key)
    if suffix == ".csv":
        return load_from_redcap(path, study_key=study_key)
    raise ValueError(
        f"Unsupported schema file format '{path.suffix}'. Expected .json, .xml, .csv, .zip, or directory."
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


def _unquote_double_quoted_literal(raw_val: str) -> str | None:
    """Return unescaped string if ``raw_val`` is wrapped in double quotes, else ``None``."""
    stripped = raw_val.strip()
    if len(stripped) < 2 or stripped[0] != '"' or stripped[-1] != '"':
        return None
    try:
        loaded = json.loads(stripped)
        if isinstance(loaded, str):
            return loaded
    except ValueError:
        pass
    return stripped[1:-1].replace('\\"', '"').replace("\\\\", "\\")


def parse_key_value_pairs(
    pairs: Sequence[str] | None,
    *,
    schema: ValidationSchemaProtocol | None = None,
    form_key: str | None = None,
    form_id: int | None = None,
    study_key: str | None = None,
) -> dict[str, Any]:
    """Parse repeatable ``KEY=VALUE`` pairs from ``-d/--data`` flags with optional schema types."""
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
        unquoted = _unquote_double_quoted_literal(raw_val)
        if unquoted is not None:
            if _contains_masked_sentinel(unquoted):
                raise ValueError(
                    f"Variable '{key}' contains masked PHI sentinel '{MASKED_SENTINEL}' and cannot be submitted."
                )
            parsed[key] = unquoted
            continue

        expected_type = _resolve_expected_var_type(
            schema,
            form_key,
            form_id,
            key,
            study_key=study_key,
        )
        val_stripped = raw_val.strip()
        if expected_type is None and val_stripped.lower() in {"true", "false"}:
            parsed[key] = val_stripped.lower() == "true"
        else:
            parsed[key] = _coerce_clinical_value(
                raw_val,
                from_csv_str=True,
                expected_var_type=expected_type,
            )
    return parsed


def parse_json_records_text(raw_text: str, *, source_label: str) -> list[dict[str, Any]]:
    """Parse a JSON object or JSON array of record payload dicts."""
    try:
        loaded = json.loads(raw_text)
    except ValueError as exc:
        raise ValueError(f"Invalid JSON in {source_label}: {exc}") from exc

    if isinstance(loaded, dict):
        items = [loaded]
    elif isinstance(loaded, list) and all(isinstance(x, dict) for x in loaded):
        items = list(loaded)
    else:
        raise ValueError(
            f"JSON payload in {source_label} must be a JSON object or array of objects."
        )
    ensure_no_masked_sentinel(items)
    return items


def parse_stdin_records_text(
    raw_text: str,
    *,
    schema: ValidationSchemaProtocol | None = None,
) -> list[dict[str, Any]]:
    """Auto-detect and parse piped JSON or CSV record payloads from stdin text."""
    cleaned = raw_text.lstrip("\ufeff")
    stripped = cleaned.lstrip()
    if not stripped:
        raise ValueError("Empty input received from stdin ('-').")
    if stripped[0] in {"{", "["}:
        return parse_json_records_text(cleaned, source_label="stdin")
    if "\n" not in stripped.rstrip():
        try:
            json.loads(stripped)
        except ValueError:
            pass
        else:
            raise ValueError("JSON payload in stdin must be a JSON object or array of objects.")
    return csv_text_to_records(cleaned, schema=schema)


def load_records_from_path_or_stdin(
    file_arg: str,
    *,
    schema: ValidationSchemaProtocol | None = None,
) -> list[dict[str, Any]]:
    """Load record payloads from ``'-'`` (piped stdin JSON or CSV), ``.json``, or ``.csv``."""
    if file_arg == "-":
        if is_tty(sys.stdin):
            raise ValueError(
                "Cannot read '-' from an interactive terminal (TTY) stdin; "
                "pipe JSON or CSV data into stdin or pass a file path."
            )
        return parse_stdin_records_text(sys.stdin.read(), schema=schema)
    file_path = Path(file_arg)
    if not file_path.exists():
        raise ValueError(f"Input file not found: '{file_arg}'")
    if file_path.suffix.lower() == ".json":
        return parse_json_records_text(
            file_path.read_text(encoding="utf-8"), source_label=str(file_path)
        )
    return csv_to_records(file_path, schema=schema)


def _extract_subject_identifier(rec: Mapping[str, Any]) -> str | None:
    for key in (
        "subjectKey",
        "subject_key",
        "subjectOid",
        "subject_oid",
        "subjectId",
        "subject_id",
    ):
        val = rec.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            if val.strip():
                return val.strip()
        else:
            return str(val)
    return None


def _coerce_optional_form_id(rec: Mapping[str, Any]) -> int | None:
    raw_fid = rec.get("formId")
    if raw_fid is None:
        raw_fid = rec.get("form_id")
    if raw_fid is None or isinstance(raw_fid, bool):
        return None
    if isinstance(raw_fid, int):
        return raw_fid
    if isinstance(raw_fid, str) and raw_fid.strip().lstrip("-").isdigit():
        return int(raw_fid.strip())
    return None


def assemble_cli_write_records(
    args: argparse.Namespace,
    *,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    is_subject_registration: bool = False,
) -> list[dict[str, Any]]:
    """Unify CLI payload assembly for ``records create`` and ``subjects register``."""
    if args.input_file and args.json_payload:
        raise ValueError("Cannot combine --file and --json.")

    data_pairs: Sequence[str] | None = getattr(args, "data_pairs", None)
    # Validate syntax and masked sentinels in -d flags up front even if input file is empty.
    if data_pairs:
        parse_key_value_pairs(data_pairs)

    form_id_arg: int | None = getattr(args, "form_id", None)
    records: list[dict[str, Any]]
    if args.input_file:
        records = load_records_from_path_or_stdin(args.input_file, schema=schema)
    elif args.json_payload:
        records = parse_json_records_text(args.json_payload, source_label="--json")
    elif is_subject_registration:
        if (not args.form_key and form_id_arg is None) or not args.site_name:
            raise ValueError(
                "Provide --file, --json, or both --form-key and --site-name to register a subject."
            )
        records = [{"data": {}}]
    else:
        if not args.form_key and form_id_arg is None:
            raise ValueError(
                "Provide --file, --json, or at least --form-key / --form-id to create a record."
            )
        records = [{"data": {}}]

    subject_key_arg: str | None = getattr(args, "subject_key", None)
    subject_oid_arg: str | None = getattr(args, "subject_oid", None)
    subject_id_arg: int | None = getattr(args, "subject_id", None)
    site_name_arg: str | None = getattr(args, "site_name", None)
    site_id_arg: int | None = getattr(args, "site_id", None)
    interval_name_arg: str | None = getattr(args, "interval_name", None)
    interval_id_arg: int | None = getattr(args, "interval_id", None)

    if subject_key_arg and len(records) > 1:
        existing_subjects = {
            sid for r in records if (sid := _extract_subject_identifier(r)) is not None
        }
        missing_count = sum(1 for r in records if _extract_subject_identifier(r) is None)
        if missing_count > 0 and (is_subject_registration or len(existing_subjects) > 1):
            raise ValueError(
                "Cannot apply --subject-key across multi-Subject inputs lacking subject keys."
            )

    resolved_study_key = study_key or getattr(args, "study_key", None)
    resolved: list[dict[str, Any]] = []
    for item in records:
        rec = dict(item)
        if args.form_key and not (rec.get("formKey") or rec.get("form_key")):
            rec["formKey"] = args.form_key
        elif form_id_arg is not None and rec.get("formId") is None and rec.get("form_id") is None:
            rec["formId"] = form_id_arg

        if _extract_subject_identifier(rec) is None:
            if subject_key_arg:
                rec["subjectKey"] = subject_key_arg
            elif subject_oid_arg:
                rec["subjectOid"] = subject_oid_arg
            elif subject_id_arg is not None:
                rec["subjectId"] = subject_id_arg

        if site_name_arg and not (rec.get("siteName") or rec.get("site_name")):
            rec["siteName"] = site_name_arg
        elif site_id_arg is not None and rec.get("siteId") is None and rec.get("site_id") is None:
            rec["siteId"] = site_id_arg

        if interval_name_arg and not (rec.get("intervalName") or rec.get("interval_name")):
            rec["intervalName"] = interval_name_arg
        elif (
            interval_id_arg is not None
            and rec.get("intervalId") is None
            and rec.get("interval_id") is None
        ):
            rec["intervalId"] = interval_id_arg

        if data_pairs:
            raw_fk = rec.get("formKey") or rec.get("form_key")
            rec_form_key = (
                str(raw_fk).strip() if isinstance(raw_fk, str) and raw_fk.strip() else None
            )
            rec_form_id = _coerce_optional_form_id(rec)
            coerced_inline = parse_key_value_pairs(
                data_pairs,
                schema=schema,
                form_key=rec_form_key,
                form_id=rec_form_id,
                study_key=resolved_study_key,
            )
            existing_data = dict(rec.get("data") or {})
            existing_data.update(coerced_inline)
            rec["data"] = existing_data

        resolved.append(rec)

    ensure_no_masked_sentinel(resolved)
    return resolved


@contextmanager
def write_progress_context(
    action_label: str,
    study_key: str,
    *,
    no_interactive: bool = False,
    output_format: str | None = None,
    dry_run: bool = False,
) -> Iterator[JobProgressHandler | None]:
    """Manage spinner and polling progress callbacks while honoring dry-run/non-interactive/JSON modes."""
    if dry_run:
        yield None
        return
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


def submit_cli_records_batch(
    sdk: ImednetSDK,
    args: argparse.Namespace,
    payload: list[dict[str, Any]],
    *,
    action_label: str,
    schema: ValidationSchemaProtocol | None,
    skipped_count: int = 0,
) -> BatchSubmissionSummary:
    """Submit or dry-run a prepared record payload batch via ``sdk.records.create_batch``."""
    no_interactive = getattr(args, "no_interactive", False)
    email_notify = parse_email_notify(args.email_notify)
    try:
        with write_progress_context(
            action_label,
            args.study_key,
            no_interactive=no_interactive,
            output_format=args.output_format,
            dry_run=args.dry_run,
        ) as on_progress:
            raw_summary = sdk.records.create_batch(
                args.study_key,
                payload,
                email_notify=email_notify,
                schema=schema,
                chunk_size=args.chunk_size,
                wait_for_completion=args.wait,
                timeout=args.timeout,
                interval=args.interval,
                on_progress=on_progress,
                dry_run=args.dry_run,
                collect_errors=True,
            )
    except Exception as exc:
        attached = getattr(exc, "batch_summary", None)
        if isinstance(attached, BatchSubmissionSummary) and skipped_count:
            attached.skipped_count = skipped_count
        raise

    summary = coerce_batch_summary(
        raw_summary,
        study_key=args.study_key,
        total_records=len(payload),
        dry_run=args.dry_run,
        skipped_count=skipped_count,
    )
    if skipped_count:
        summary.skipped_count = skipped_count
    return summary


def _summary_to_json_item(summary: BatchSubmissionSummary) -> dict[str, Any]:
    return {
        "studyKey": summary.study_key,
        "dryRun": summary.dry_run,
        "totalRecords": summary.total_records,
        "skippedCount": summary.skipped_count,
        "chunkCount": summary.chunk_count,
        "batchIds": summary.batch_ids,
        "allSuccessful": summary.all_successful,
        "jobs": [j.model_dump(mode="json", by_alias=True) for j in summary.jobs],
        "validationReport": (
            summary.validation_report.to_dict() if summary.validation_report is not None else None
        ),
    }


def _render_committed_jobs_table(summary: BatchSubmissionSummary, *, resource: str) -> None:
    rows = [
        {
            "batch_id": j.batch_id,
            "job_id": j.job_id,
            "state": j.state,
            "progress": getattr(j, "progress", None),
            "total_records": getattr(j, "total_records", None),
            "failed_records": getattr(j, "failed_records", None),
        }
        for j in summary.jobs
    ]
    human_label = _WRITE_RESOURCE_LABELS.get(resource, resource.replace("_", " "))
    display_list(
        rows,
        human_label,
        fields=_WRITE_JOB_TABLE_FIELDS,
        output_format="table",
        details=summary.jobs,
        interactive=False,
    )


def _format_cli_write_exception(exc: Exception) -> str:
    msg = _redact_secrets(str(exc))
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return f"Validation Error: {msg}"
    if isinstance(exc, (AuthenticationError, UnauthorizedError)):
        return f"Authentication Failed: {msg}"
    if isinstance(exc, (AuthorizationError, ForbiddenError)):
        return f"Permission Denied: {msg}"
    if isinstance(exc, RateLimitError):
        return f"Rate Limit Exceeded: {msg}"
    if isinstance(exc, NotFoundError):
        return f"Not Found: {msg}"
    if isinstance(exc, (JobFailedError, JobTimeoutError)):
        return f"Job Error: {msg}"
    if isinstance(exc, ApiError):
        return f"API Error: {msg}"
    return f"Error: {msg}"


def render_write_summary(
    summary: BatchSubmissionSummary,
    *,
    resource: str,
    output_format: str | None = None,
) -> None:
    """Render a :class:`BatchSubmissionSummary` in human table or JSON mode and exit 1 if invalid."""
    resolved_format = resolve_output_format(output_format)
    if summary.validation_report is not None and not summary.validation_report.is_valid:
        if resolved_format == "json":
            emit_json([_summary_to_json_item(summary)], resource, status="failed")
        print_error(_redact_secrets(summary.validation_report.format_summary()))
        sys.exit(1)

    if not summary.all_successful:
        if resolved_format == "json":
            emit_json([_summary_to_json_item(summary)], resource, status="failed")
        elif summary.jobs:
            _render_committed_jobs_table(summary, resource=resource)
        print_error(f"Error: Write batch for study '{summary.study_key}' completed with failures.")
        sys.exit(1)

    if resolved_format == "json":
        emit_json([_summary_to_json_item(summary)], resource, status="complete")
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
        msg = f"No records submitted for study '{summary.study_key}'"
        if summary.skipped_count:
            msg += f" ({summary.skipped_count} already-enrolled skipped)"
        print(f"{msg}.")
        return

    _render_committed_jobs_table(summary, resource=resource)


def _extract_failure_summary(
    exc: Exception,
    *,
    study_key: str,
    dry_run: bool,
    skipped_count: int = 0,
    total_records: int = 0,
) -> BatchSubmissionSummary | None:
    """Extract or synthesize a partial :class:`BatchSubmissionSummary` from ``exc`` if present."""
    attached = getattr(exc, "batch_summary", None)
    if isinstance(attached, BatchSubmissionSummary):
        if skipped_count and not attached.skipped_count:
            attached.skipped_count = skipped_count
        return attached

    val_report = getattr(exc, "validation_report", None)
    if val_report is not None:
        return BatchSubmissionSummary(
            study_key=study_key,
            jobs=[],
            chunk_sizes=[],
            total_records=getattr(val_report, "total_records", total_records),
            skipped_count=skipped_count,
            dry_run=dry_run,
            validation_report=val_report,
            has_error=True,
        )

    failed_status = getattr(exc, "status", None)
    if isinstance(exc, JobFailedError) and isinstance(failed_status, Job):
        return BatchSubmissionSummary(
            study_key=study_key,
            jobs=[failed_status],
            chunk_sizes=[total_records] if total_records else [],
            total_records=total_records,
            skipped_count=skipped_count,
            dry_run=False,
            has_error=True,
        )

    return None


def execute_cli_write(
    operation: Callable[[], BatchSubmissionSummary],
    *,
    study_key: str,
    resource: str,
    output_format: str | None = None,
    dry_run: bool = False,
) -> None:
    """Orchestrate a CLI write command, emitting structured partial/validation failures before exit 1."""
    resolved_format = resolve_output_format(output_format)
    try:
        summary = operation()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        partial = _extract_failure_summary(exc, study_key=study_key, dry_run=dry_run)
        if partial is not None and (
            partial.validation_report is not None and not partial.validation_report.is_valid
        ):
            if resolved_format == "json":
                emit_json([_summary_to_json_item(partial)], resource, status="failed")
            print_error(_redact_secrets(partial.validation_report.format_summary()))
            sys.exit(1)

        if partial is not None and partial.jobs:
            if resolved_format == "json":
                emit_json([_summary_to_json_item(partial)], resource, status="failed")
            else:
                _render_committed_jobs_table(partial, resource=resource)
            print_error(_format_cli_write_exception(exc))
            sys.exit(1)

        if isinstance(exc, (ValidationError, ValueError, TypeError)):
            exit_cli_validation_error(exc)

        if isinstance(exc, (JobFailedError, JobTimeoutError)):
            print_error(_format_cli_write_exception(exc))
            sys.exit(1)

        raise

    render_write_summary(summary, resource=resource, output_format=resolved_format)


def exit_cli_validation_error(exc: Exception) -> None:
    """Print a secret-redacted CLI validation error to stderr and exit with status 1."""
    print_error(f"Validation Error: {_redact_secrets(str(exc))}")
    sys.exit(1)
