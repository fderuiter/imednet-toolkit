"""Batch orchestration and preflight helpers for CLI write commands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from imednet.sdk import ImednetSDK
from imednet.utils.batch_coordinator import BatchSubmissionSummary
from imednet.validation.cache import ValidationSchemaProtocol

from ..decorators import validate_cli_path_segment
from . import write_helpers as _wh
from .args import resolve_study_key
from .output import print_error

BatchPayloadBuilder = Callable[[ValidationSchemaProtocol | None], list[dict[str, Any]]]
BatchFilteredBuilder = Callable[[ValidationSchemaProtocol | None], tuple[list[dict[str, Any]], int]]
BatchProgressRunner = Callable[[ValidationSchemaProtocol | None, dict[str, Any]], tuple[Any, int]]


def prepare_write_cli_preflight(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    file_arg: str | None = None,
    allow_stdin_dash: bool = True,
) -> str:
    """Resolve and validate ``study_key``, numeric batch flags, and optional input file path."""
    study_key = resolve_study_key(parser, args, required=True)
    assert study_key is not None
    validate_cli_path_segment(study_key, name="study_key")
    _wh.validate_write_numeric_flags(
        chunk_size=args.chunk_size,
        timeout=args.timeout,
        interval=args.interval,
    )
    resolved_file = file_arg if file_arg is not None else getattr(args, "input_file", None)
    if resolved_file:
        if allow_stdin_dash:
            _wh.guard_stdin_dash_tty(resolved_file)
        if (not allow_stdin_dash or resolved_file != "-") and not Path(resolved_file).exists():
            print_error(f"Error: Input file not found: '{resolved_file}'")
            sys.exit(1)
    return study_key


def run_cli_batch_write(
    sdk: ImednetSDK,
    args: argparse.Namespace,
    *,
    resource: str,
    action_label: str,
    prepare_payload: BatchPayloadBuilder | None = None,
    prepare_batch: BatchFilteredBuilder | None = None,
    submit_in_progress: BatchProgressRunner | None = None,
) -> None:
    """Resolve schema, run batch write with progress reporting, handle errors, and render summary."""
    no_interactive = bool(getattr(args, "no_interactive", False))
    total_records = 0
    skipped_count = 0

    def _operation() -> BatchSubmissionSummary:
        nonlocal total_records, skipped_count
        resolved_schema = _wh.resolve_cli_schema(
            sdk,
            args.study_key,
            args.schema_source,
            dry_run=args.dry_run,
        )
        payload: list[dict[str, Any]] | None = None
        if prepare_batch is not None:
            payload, skipped_count = prepare_batch(resolved_schema)
            total_records = len(payload)
        elif prepare_payload is not None:
            payload = prepare_payload(resolved_schema)
            total_records = len(payload)
        elif submit_in_progress is None:
            raise ValueError(
                "One of prepare_payload, prepare_batch, or submit_in_progress is required."
            )

        email_notify = _wh.parse_email_notify(args.email_notify)
        try:
            with _wh.write_progress_context(
                action_label,
                args.study_key,
                no_interactive=no_interactive,
                output_format=args.output_format,
                dry_run=args.dry_run,
            ) as on_progress:
                batch_kwargs: dict[str, Any] = {
                    "email_notify": email_notify,
                    "schema": resolved_schema,
                    "chunk_size": args.chunk_size,
                    "wait_for_completion": args.wait,
                    "timeout": args.timeout,
                    "interval": args.interval,
                    "on_progress": on_progress,
                    "dry_run": args.dry_run,
                    "collect_errors": True,
                    "resolve_results": bool(getattr(args, "resolve_results", True)),
                }
                if submit_in_progress is not None:
                    raw_summary, total_records = submit_in_progress(resolved_schema, batch_kwargs)
                else:
                    assert payload is not None
                    raw_summary = sdk.records.create_batch(
                        args.study_key,
                        payload,
                        **batch_kwargs,
                    )
        except Exception as exc:
            if skipped_count:
                for attr in ("batch_summary", "partial_summary"):
                    attached = getattr(exc, attr, None)
                    if isinstance(attached, BatchSubmissionSummary):
                        attached.skipped_count = skipped_count
            raise

        summary = _wh.coerce_batch_summary(
            raw_summary,
            study_key=args.study_key,
            total_records=total_records,
            dry_run=args.dry_run,
            skipped_count=skipped_count,
            wait_for_completion=args.wait,
        )
        if skipped_count:
            summary.skipped_count = skipped_count
        return summary

    _wh.execute_cli_write(
        _operation,
        study_key=args.study_key,
        resource=resource,
        output_format=args.output_format,
        dry_run=args.dry_run,
        skipped_count=lambda: skipped_count,
        total_records=lambda: total_records,
    )
