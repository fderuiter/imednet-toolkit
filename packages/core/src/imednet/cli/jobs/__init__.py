"""CLI commands for monitoring background jobs."""

from __future__ import annotations

import argparse

from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, fetching_status
from ..utils.args import add_output_format
from ..utils.output import emit_json


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("jobs", help="Manage background jobs.")
    sub = parser.add_subparsers(dest="command")

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
        if output_format == "json":
            emit_json([job], "jobs")
        else:
            print(job.model_dump())

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
        with fetching_status("job result", study_key):
            job = sdk.poll_job(study_key, batch_id, interval=interval, timeout=timeout)
        if output_format == "json":
            emit_json([job], "jobs")
        else:
            print(job.model_dump())

    wait_parser.set_defaults(
        func=lambda args: job_wait(
            study_key=args.study_key,
            batch_id=args.batch_id,
            interval=args.interval,
            timeout=args.timeout,
            output_format=args.output_format,
        )
    )
