"""Shared CLI helpers for write commands (records create/import and subjects register)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from imednet.datadict import load_from_api, load_from_cdisc_odm, load_from_csv, load_from_redcap
from imednet.datadict.loaders.api import can_load_from_api
from imednet.datadict.models import DataDictionary
from imednet.errors import ValidationError
from imednet.models.jobs import Job, JobStatus
from imednet.sdk import ImednetSDK
from imednet.utils.batch_coordinator import DEFAULT_BATCH_CHUNK_SIZE, BatchSubmissionSummary
from imednet.utils.job_poller import JobFailedError, JobPollSummary, JobProgressHandler
from imednet.utils.tabular import (
    _BOOL_LIKE_TYPES,
    _SLASH_SPACE_RE,
    _TEXT_LIKE_TYPES,
    MASKED_SENTINEL,
    _coerce_clinical_value,
    _contains_masked_sentinel,
    _resolve_expected_var_type,
    csv_to_records,
    dataframe_rows_to_records,
    ensure_no_masked_sentinel,
    normalize_records_input,
    reject_nonfinite_json_constant,
)
from imednet.validation.cache import (
    BaseSchemaCache,
    BatchValidationReport,
    RecordValidationIssue,
    SchemaCache,
    ValidationSchemaProtocol,
)

from ..decorators import _redact_secrets
from .args import add_output_format, add_simulation_arg, resolve_output_format
from .context import fetching_status
from .output import display_list, emit_json, format_job_progress, print_error
from .terminal import is_json_output_mode, is_non_interactive, supports_live_status

SUPPORTED_RECORD_EXTENSIONS: tuple[str, ...] = (
    ".csv",
    ".json",
    ".jsonl",
    ".ndjson",
    ".parquet",
    ".xlsx",
)

_NONFINITE_TOKENS: frozenset[str] = frozenset(
    {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
)


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


def parse_email_notify(raw_email_notify: str | bool | None) -> bool | str | None:
    """Normalize and validate ``--email-notify`` value into bool or header-safe email string."""
    if raw_email_notify is None:
        return None
    if isinstance(raw_email_notify, bool):
        return raw_email_notify
    if "\r" in raw_email_notify or "\n" in raw_email_notify or "\x00" in raw_email_notify:
        raise ValueError("Invalid --email-notify value: header value must not contain newlines.")
    cleaned = raw_email_notify.strip()
    if not cleaned:
        raise ValueError("Invalid --email-notify value: cannot be empty.")
    low = cleaned.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if any(ch.isspace() for ch in cleaned):
        raise ValueError(
            f"Invalid --email-notify value '{cleaned}': expected 'true', 'false', or a valid email address."
        )
    local, sep, domain = cleaned.partition("@")
    if (
        sep != "@"
        or not local
        or not domain
        or "@" in domain
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
    ):
        raise ValueError(
            f"Invalid --email-notify value '{cleaned}': expected 'true', 'false', or a valid email address."
        )
    return cleaned


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
    if suffix == ".zip":
        return load_from_csv(path)
    raise ValueError(
        f"Unsupported schema file format '{path.suffix}'. Expected .json, .xml, .csv, or directory."
    )


def resolve_cli_schema(
    sdk: ImednetSDK,
    study_key: str,
    schema_source: str | None,
    *,
    dry_run: bool = False,
) -> ValidationSchemaProtocol | None:
    """Resolve ``--schema`` from disk or live study API variables."""
    if not schema_source:
        if dry_run and can_load_from_api(sdk):
            return load_from_api(sdk, study_key)
        return None
    if schema_source.strip().lower() == "api":
        cache = SchemaCache()
        cache.refresh(sdk.forms, sdk.variables, study_key=study_key)
        return cache
    return load_offline_schema(schema_source, study_key=study_key)


def validate_record_file_arg(file_arg: str) -> None:
    """Validate that *file_arg* is ``'-'`` or an existing file with a supported record extension."""
    if file_arg == "-":
        return
    file_path = Path(file_arg)
    if not file_path.exists():
        raise ValueError(f"Input file not found: '{file_arg}'")
    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_RECORD_EXTENSIONS:
        supported = ", ".join(SUPPORTED_RECORD_EXTENSIONS)
        raise ValueError(
            f"Unsupported file format '{file_path.suffix or '<none>'}' for '{file_arg}'. "
            f"Supported extensions: {supported}."
        )


def _normalize_var_type_label(raw: str) -> str:
    collapsed = re.sub(r"[\s_-]+", " ", raw.strip().lower())
    return _SLASH_SPACE_RE.sub("/", collapsed)


def _resolve_cli_var_type(
    schema: ValidationSchemaProtocol | None,
    *,
    form_key: str | None,
    form_id: int | None,
    var_name: str,
    study_key: str | None = None,
) -> str | None:
    """Resolve normalized variable type from *schema* and optional form context."""
    if schema is None:
        return None
    if isinstance(schema, BaseSchemaCache) and study_key:
        resolved_fk = form_key
        if not resolved_fk and form_id is not None:
            resolved_fk = schema.form_key_from_id(form_id, study_key=study_key)
        if resolved_fk:
            vars_map = schema.variables_for_form(resolved_fk, study_key=study_key)
            if vars_map and var_name in vars_map:
                raw = getattr(vars_map[var_name], "variable_type", None) or getattr(
                    vars_map[var_name], "variableType", None
                )
                if isinstance(raw, str):
                    return _normalize_var_type_label(raw)
            return None
    else:
        direct = _resolve_expected_var_type(
            schema, form_key, form_id, var_name, study_key=study_key
        )
        if direct is not None:
            return direct
        if form_key or form_id is not None:
            return None

    # Form-less update fallback: check if variable has a single unambiguous type across forms
    candidate_fks: Sequence[str]
    if isinstance(schema, BaseSchemaCache):
        candidate_fks = list(schema.forms_for_study(study_key) if study_key else schema.forms)
    else:
        forms_attr = getattr(schema, "forms", None)
        if isinstance(forms_attr, Sequence):
            candidate_fks = [
                str(getattr(f, "form_key", None) or getattr(f, "formKey", ""))
                for f in forms_attr
                if getattr(f, "form_key", None) or getattr(f, "formKey", None)
            ]
        else:
            candidate_fks = []

    matched_types: set[str] = set()
    for fk in candidate_fks:
        form_vars: Mapping[str, Any] = (
            schema.variables_for_form(fk, study_key=study_key)
            if isinstance(schema, BaseSchemaCache)
            else schema.variables_for_form(fk)
        )
        if form_vars and var_name in form_vars:
            raw = getattr(form_vars[var_name], "variable_type", None) or getattr(
                form_vars[var_name], "variableType", None
            )
            if isinstance(raw, str) and raw.strip():
                matched_types.add(_normalize_var_type_label(raw))
    if len(matched_types) == 1:
        return next(iter(matched_types))
    return None


def parse_key_value_pairs(
    pairs: Sequence[str] | None,
    *,
    schema: ValidationSchemaProtocol | None = None,
    form_key: str | None = None,
    form_id: int | None = None,
    study_key: str | None = None,
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
        expected_var_type = _resolve_cli_var_type(
            schema,
            form_key=form_key,
            form_id=form_id,
            var_name=key,
            study_key=study_key,
        )
        if expected_var_type in _TEXT_LIKE_TYPES:
            parsed[key] = raw_val
            continue
        val_stripped = raw_val.strip()
        val_low = val_stripped.lower()
        if val_low in _NONFINITE_TOKENS:
            raise ValidationError(
                f"Variable '{key}' contains invalid numeric constant '{val_stripped}': "
                "NaN and Infinity are not allowed."
            )
        if (expected_var_type in _BOOL_LIKE_TYPES or expected_var_type is None) and val_low in {
            "true",
            "false",
        }:
            parsed[key] = val_low == "true"
        else:
            parsed[key] = _coerce_clinical_value(
                raw_val,
                from_csv_str=True,
                expected_var_type=expected_var_type,
            )
    return parsed


def _unwrap_json_records_payload(loaded: Any, *, source_label: str) -> list[dict[str, Any]]:
    """Unwrap v1 CLI ``items`` envelopes, REST API ``data`` list envelopes, or record objects."""
    if isinstance(loaded, dict):
        if isinstance(loaded.get("items"), list) and all(
            isinstance(x, dict) for x in loaded["items"]
        ):
            items = list(loaded["items"])
        elif isinstance(loaded.get("data"), list) and all(
            isinstance(x, dict) for x in loaded["data"]
        ):
            items = list(loaded["data"])
        else:
            items = [loaded]
    elif isinstance(loaded, list) and all(isinstance(x, dict) for x in loaded):
        items = list(loaded)
    else:
        raise ValueError(
            f"JSON payload in {source_label} must be a JSON object or array of objects."
        )
    ensure_no_masked_sentinel(items)
    return items


def parse_jsonl_records_text(
    raw_text: str,
    *,
    source_label: str,
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON (``.jsonl`` / ``.ndjson``) record objects."""
    items: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped, parse_constant=reject_nonfinite_json_constant)
        except ValidationError:
            raise
        except ValueError as exc:
            raise ValueError(f"Invalid JSON on line {line_no} of {source_label}: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"JSONL line {line_no} in {source_label} must be a JSON object.")
        items.append(obj)
    ensure_no_masked_sentinel(items)
    return normalize_records_input(
        items,
        schema=schema,
        create_only=create_only,
        study_key=study_key,
    )


def parse_json_records_text(
    raw_text: str,
    *,
    source_label: str,
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
    allow_jsonl_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Parse a JSON object, envelope, or JSON array of record payload dicts."""
    try:
        loaded = json.loads(raw_text, parse_constant=reject_nonfinite_json_constant)
    except ValidationError:
        raise
    except ValueError as exc:
        if allow_jsonl_fallback:
            non_empty_lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
            if len(non_empty_lines) > 1:
                return parse_jsonl_records_text(
                    raw_text,
                    source_label=source_label,
                    schema=schema,
                    create_only=create_only,
                    study_key=study_key,
                )
        raise ValueError(f"Invalid JSON in {source_label}: {exc}") from exc

    items = _unwrap_json_records_payload(loaded, source_label=source_label)
    return normalize_records_input(
        items,
        schema=schema,
        create_only=create_only,
        study_key=study_key,
    )


def _load_parquet_records(
    file_path: Path,
    *,
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
) -> list[dict[str, Any]]:
    """Load records from a Parquet file, hinting ``imednet[export]`` when missing."""
    try:
        import pandas as pd
        import pyarrow as _pa  # noqa: F401
    except ImportError as exc:
        raise ValueError(
            f"Reading Parquet file '{file_path}' requires optional dependencies "
            "(pandas and pyarrow). Install them with: pip install 'imednet[export]'"
        ) from exc
    try:
        df = pd.read_parquet(file_path)
    except ImportError as exc:
        raise ValueError(
            f"Reading Parquet file '{file_path}' requires pyarrow. "
            "Install it with: pip install 'imednet[export]'"
        ) from exc
    return dataframe_rows_to_records(
        df,
        schema=schema,
        create_only=create_only,
        study_key=study_key,
    )


def _load_excel_records(
    file_path: Path,
    *,
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
) -> list[dict[str, Any]]:
    """Load records from an Excel ``.xlsx`` workbook, hinting ``imednet[export]`` when missing."""
    try:
        import openpyxl as _oxl  # noqa: F401
        import pandas as pd
    except ImportError as exc:
        raise ValueError(
            f"Reading Excel file '{file_path}' requires optional dependencies "
            "(pandas and openpyxl). Install them with: pip install 'imednet[export]'"
        ) from exc
    try:
        df = pd.read_excel(file_path, engine="openpyxl")
    except ImportError as exc:
        raise ValueError(
            f"Reading Excel file '{file_path}' requires openpyxl. "
            "Install it with: pip install 'imednet[export]'"
        ) from exc
    return dataframe_rows_to_records(
        df,
        schema=schema,
        create_only=create_only,
        study_key=study_key,
    )


def load_records_from_path_or_stdin(
    file_arg: str,
    *,
    schema: ValidationSchemaProtocol | None = None,
    create_only: bool = False,
    study_key: str | None = None,
) -> list[dict[str, Any]]:
    """Load record payloads from ``'-'`` (stdin) or supported tabular/JSON files."""
    if file_arg == "-":
        return parse_json_records_text(
            sys.stdin.read(),
            source_label="stdin",
            schema=schema,
            create_only=create_only,
            study_key=study_key,
            allow_jsonl_fallback=True,
        )
    validate_record_file_arg(file_arg)
    file_path = Path(file_arg)
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        return csv_to_records(
            file_path,
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    if suffix == ".json":
        return parse_json_records_text(
            file_path.read_text(encoding="utf-8-sig"),
            source_label=str(file_path),
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    if suffix in {".jsonl", ".ndjson"}:
        return parse_jsonl_records_text(
            file_path.read_text(encoding="utf-8-sig"),
            source_label=str(file_path),
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    if suffix == ".parquet":
        return _load_parquet_records(
            file_path,
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    if suffix == ".xlsx":
        return _load_excel_records(
            file_path,
            schema=schema,
            create_only=create_only,
            study_key=study_key,
        )
    supported = ", ".join(SUPPORTED_RECORD_EXTENSIONS)
    raise ValueError(
        f"Unsupported file format '{file_path.suffix or '<none>'}' for '{file_arg}'. "
        f"Supported extensions: {supported}."
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


def render_write_summary(
    summary: BatchSubmissionSummary,
    *,
    resource: str,
    output_format: str | None = None,
    failure_message: str | None = None,
) -> None:
    """Render a :class:`BatchSubmissionSummary` in human table or JSON mode and exit 1 on failure."""
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
            err_msg = failure_message or "Background job(s) ended in a failed state."
            print_error(f"Error: {_redact_secrets(err_msg)}")
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
        if failure_message:
            print_error(f"Error: {_redact_secrets(failure_message)}")
        else:
            failed_ids = [
                str(j.batch_id or j.job_id or "<unknown>")
                for j in summary.jobs
                if getattr(j, "is_failed", False)
            ]
            suffix = f" ({', '.join(failed_ids)})" if failed_ids else ""
            print_error(f"Error: Background job(s) ended in a failed state{suffix}.")
        sys.exit(1)


def handle_job_failed_error(
    exc: JobFailedError,
    *,
    study_key: str,
    total_records: int,
    resource: str,
    output_format: str | None = None,
    skipped_count: int = 0,
) -> None:
    """Render a failed polled job summary in JSON or table mode and exit 1."""
    status = getattr(exc, "status", None)
    jobs = [status] if isinstance(status, Job) else []
    batch_key = str(getattr(status, "batch_id", None) or "failed")
    poll_summary = JobPollSummary(
        study_key=study_key,
        results={batch_key: status} if isinstance(status, JobStatus) else {},
        failures={batch_key: exc},
        elapsed_total=0.0,
    )
    summary = BatchSubmissionSummary(
        study_key=study_key,
        jobs=jobs,
        chunk_sizes=[total_records] if total_records else [],
        total_records=total_records,
        skipped_count=skipped_count,
        poll_summary=poll_summary,
        dry_run=False,
    )
    render_write_summary(
        summary,
        resource=resource,
        output_format=output_format,
        failure_message=str(exc),
    )


def handle_cli_write_validation_error(
    exc: Exception,
    *,
    study_key: str,
    resource: str,
    dry_run: bool = False,
    output_format: str | None = None,
) -> None:
    """Emit structured ``isValid: false`` report on dry-run payload errors or exit 1 on stderr."""
    if dry_run and isinstance(exc, ValidationError) and MASKED_SENTINEL not in str(exc):
        report = getattr(exc, "validation_report", None)
        if not isinstance(report, BatchValidationReport):
            report = BatchValidationReport(
                total_records=1,
                valid_records=0,
                invalid_records=1,
                errors=[
                    RecordValidationIssue(
                        record_index=0,
                        form_key=None,
                        variable_name=None,
                        message=_redact_secrets(str(exc)),
                        rule_id="invalid_payload",
                    )
                ],
            )
        summary = BatchSubmissionSummary(
            study_key=study_key,
            jobs=[],
            chunk_sizes=[],
            total_records=report.total_records,
            dry_run=True,
            validation_report=report,
        )
        render_write_summary(
            summary,
            resource=resource,
            output_format=output_format,
        )
    exit_cli_validation_error(exc)


def exit_cli_validation_error(exc: Exception) -> None:
    """Print a secret-redacted CLI validation error to stderr and exit with status 1."""
    print_error(f"Validation Error: {_redact_secrets(str(exc))}")
    sys.exit(1)
