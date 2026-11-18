"""CLI commands for monitoring background jobs."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ...sdk import ImednetSDK
from ...utils.job_ledger import JobLedger
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, fetching_status, format_job_progress, print_error
from ..utils.args import add_output_format
from ..utils.output import display_list

JOB_SUMMARY_FIELDS: list[str] = [
    "batch_id",
    "job_id",
    "state",
    "progress",
    "date_created",
    "date_finished",
]


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("jobs", help="Manage background jobs.")
    sub = parser.add_subparsers(dest="command")

    list_parser = sub.add_parser(
        "list", help="List active and recent jobs persisted in the local study job ledger."
    )
    list_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    list_parser.add_argument(
        "--active",
        action="store_true",
        help="Show only active (non-terminal) jobs.",
    )
    add_output_format(list_parser)

    def job_list(study_key: str, active_only: bool = False, output_format: str = "table") -> None:
        ledger = JobLedger()
        items = ledger.list_jobs(study_key, active_only=active_only)
        display_list(items, "jobs", fields=JOB_SUMMARY_FIELDS, output_format=output_format)

    list_parser.set_defaults(
        func=lambda args: job_list(
            study_key=args.study_key,
            active_only=args.active,
            output_format=args.output_format,
        )
    )

    status_parser = sub.add_parser("status", help="Fetch a job's current status.")
    status_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    status_parser.add_argument("batch_id", help="Job batch ID.")

    add_output_format(status_parser)

    @with_sdk
    def job_status(
        sdk: ImednetSDK, study_key: str, batch_id: str, output_format: str = "table"
    ) -> None:
        with fetching_status("job status", study_key):
            job = sdk.get_job(study_key, batch_id)
        display_list(
            [job],
            "jobs",
            fields=JOB_SUMMARY_FIELDS,
            output_format=output_format,
            details=[job],
        )

    status_parser.set_defaults(
        func=lambda args: job_status(
            study_key=args.study_key, batch_id=args.batch_id, output_format=args.output_format
        )
    )

    wait_parser = sub.add_parser("wait", help="Wait for a job to reach a terminal state.")
    wait_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    wait_parser.add_argument("batch_id", help="Job batch ID.")
    wait_parser.add_argument("--interval", type=int, default=5, help="Polling interval in seconds.")
    wait_parser.add_argument("--timeout", type=int, default=300, help="Maximum time to wait.")

    add_output_format(wait_parser)

    @with_sdk
    def job_wait(
        sdk: ImednetSDK,
        study_key: str,
        batch_id: str,
        interval: int,
        timeout: int,
        output_format: str = "table",
    ) -> None:
        with fetching_status("job result", study_key) as status_ctrl:

            def on_progress(event: Any) -> None:
                msg = format_job_progress(event)
                if status_ctrl is not None and hasattr(status_ctrl, "update"):
                    status_ctrl.update(msg)
                elif sys.stderr.isatty():
                    print_error(f"\r\033[K{msg}", end="", flush=True)

            job = sdk.poll_job(
                study_key,
                batch_id,
                interval=interval,
                timeout=timeout,
                on_progress=on_progress,
            )
        display_list(
            [job],
            "jobs",
            fields=JOB_SUMMARY_FIELDS,
            output_format=output_format,
            details=[job],
        )

    wait_parser.set_defaults(
        func=lambda args: job_wait(
            study_key=args.study_key,
            batch_id=args.batch_id,
            interval=args.interval,
            timeout=args.timeout,
            output_format=args.output_format,
        )
    )
