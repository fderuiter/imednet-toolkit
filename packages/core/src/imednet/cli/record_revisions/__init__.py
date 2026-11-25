"""CLI commands for managing record revisions."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ...utils.dates import normalize_utc_timestamp_bound
from ...utils.filters import REVISION_DATE_ATTRIBUTES
from ..utils import print_error, register_list_command


def _add_revision_date_args(list_parser: argparse.ArgumentParser) -> None:
    """Attach ``--start-date`` and ``--end-date`` options to ``record-revisions list``."""
    list_parser.add_argument(
        "--start-date",
        dest="start_date",
        default=None,
        help="Inclusive lower UTC date or ISO-8601 timestamp bound for dateCreated.",
    )
    list_parser.add_argument(
        "--end-date",
        dest="end_date",
        default=None,
        help="Inclusive upper UTC date or ISO-8601 timestamp bound for dateCreated.",
    )


def _extract_revision_date_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """Validate and extract ``start_date`` and ``end_date`` CLI options."""
    extra: dict[str, Any] = {}
    raw_start = getattr(args, "start_date", None)
    raw_end = getattr(args, "end_date", None)
    try:
        if raw_start is not None:
            extra["start_date"] = normalize_utc_timestamp_bound(raw_start, is_end=False)
        if raw_end is not None:
            extra["end_date"] = normalize_utc_timestamp_bound(raw_end, is_end=True)
    except ValueError as exc:
        print_error(f"Error: {exc}")
        sys.exit(1)
    return extra


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("record-revisions", help="Manage record revisions.")
    register_list_command(
        parser,
        "record_revisions",
        "record revisions",
        requires_study_key=True,
        summary_fields=[
            "record_revision_id",
            "record_id",
            "subject_key",
            "form_key",
            "user",
            "reason_for_change",
            "date_created",
        ],
        multi_bound_attributes=REVISION_DATE_ATTRIBUTES,
        export_filename_prefix="record_revisions",
        extra_args_fn=_add_revision_date_args,
        extra_kwargs_fn=_extract_revision_date_kwargs,
    )
