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
from imednet.models.jobs import Job
from imednet.sdk import ImednetSDK
from imednet.utils.batch_coordinator import DEFAULT_BATCH_CHUNK_SIZE, BatchSubmissionSummary
from imednet.utils.job_poller import JobProgressHandler
from imednet.utils.tabular import (
    _TEXT_LIKE_TYPES,
    MASKED_SENTINEL,
    _coerce_clinical_value,
    _contains_masked_sentinel,
    _resolve_expected_var_type,
    csv_to_records,
    ensure_no_masked_sentinel,
)
from imednet.validation.cache import BaseSchemaCache, SchemaCache, ValidationSchemaProtocol

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
                    return re.sub(r"[\s_-]+", " ", raw.strip().lower())
            return None
    else:
        direct = _resolve_expected_var_type(schema, form_key, form_id, var_name)
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
                matched_types.add(re.sub(r"[\s_-]+", " ", raw.strip().lower()))
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
        if val_stripped.lower() in {"true", "false"}:
            parsed[key] = val_stripped.lower() == "true"
        else:
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
    return csv_to_records(file_path, schema=schema)


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
) -> None:
    """Render a :class:`BatchSubmissionSummary` in human table or JSON mode and exit 1 if invalid."""
    resolved_format = resolve_output_format(output_format)
    if summary.validation_report is not None and not summary.validation_report.is_valid:
        if resolved_format == "json":
            emit_json([_summary_to_json_item(summary)], resource)
        else:
            print_error(_redact_secrets(summary.validation_report.format_summary()))
        sys.exit(1)

    if resolved_format == "json":
        emit_json([_summary_to_json_item(summary)], resource)
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


def exit_cli_validation_error(exc: Exception) -> None:
    """Print a secret-redacted CLI validation error to stderr and exit with status 1."""
    print_error(f"Validation Error: {_redact_secrets(str(exc))}")
    sys.exit(1)
