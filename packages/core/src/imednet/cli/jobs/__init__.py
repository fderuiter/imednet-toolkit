"""CLI commands for monitoring background jobs."""

from __future__ import annotations

import argparse

from ...sdk import ImednetSDK
from ..decorators import validate_cli_path_segment, with_sdk
from ..utils import (
    fetching_status,
    simulation_mode_context,
)
from ..utils.args import (
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    resolve_output_format,
    resolve_study_key,
)
from ..utils.output import display_list
from ..utils.write_helpers import make_job_progress_callback

JOB_SUMMARY_FIELDS: list[str] = [
    "batch_id",
    "job_id",
    "state",
    "date_created",
    "date_started",
    "date_finished",
]


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("jobs", help="Manage background jobs.")
    sub = parser.add_subparsers(dest="command")

    status_parser = sub.add_parser("status", help="Fetch a job's current status.")
    add_study_key_args(status_parser)
    status_parser.add_argument("batch_id", help="Job batch ID.")

    add_output_format(status_parser)
    add_simulation_arg(status_parser)

    @with_sdk
    def job_status(
        sdk: ImednetSDK, study_key: str, batch_id: str, output_format: str | None = None
    ) -> None:
        resolved_format = resolve_output_format(output_format)
        with fetching_status("job status", study_key):
            job = sdk.get_job(study_key, batch_id)
        display_list(
            [job],
            "jobs",
            fields=JOB_SUMMARY_FIELDS,
            output_format=resolved_format,
            details=[job],
            interactive=False,
        )

    def _dispatch_job_status(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(status_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        validate_cli_path_segment(args.batch_id, name="batch_id")
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            job_status(
                study_key=study_key,
                batch_id=args.batch_id,
                output_format=resolve_output_format(args.output_format),
            )

    status_parser.set_defaults(func=_dispatch_job_status)

    wait_parser = sub.add_parser("wait", help="Wait for a job to reach a terminal state.")
    add_study_key_args(wait_parser)
    wait_parser.add_argument("batch_id", help="Job batch ID.")
    wait_parser.add_argument("--interval", type=int, default=5, help="Polling interval in seconds.")
    wait_parser.add_argument("--timeout", type=int, default=300, help="Maximum time to wait.")

    add_output_format(wait_parser)
    add_simulation_arg(wait_parser)

    @with_sdk
    def job_wait(
        sdk: ImednetSDK,
        study_key: str,
        batch_id: str,
        interval: int,
        timeout: int,
        output_format: str | None = None,
    ) -> None:
        resolved_format = resolve_output_format(output_format)
        with fetching_status("job result", study_key) as status_ctrl:
            job = sdk.poll_job(
                study_key,
                batch_id,
                interval=interval,
                timeout=timeout,
                on_progress=make_job_progress_callback(status_ctrl),
            )
        display_list(
            [job],
            "jobs",
            fields=JOB_SUMMARY_FIELDS,
            output_format=resolved_format,
            details=[job],
            interactive=False,
        )

    def _dispatch_job_wait(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(wait_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        validate_cli_path_segment(args.batch_id, name="batch_id")
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            job_wait(
                study_key=study_key,
                batch_id=args.batch_id,
                interval=args.interval,
                timeout=args.timeout,
                output_format=resolve_output_format(args.output_format),
            )

    wait_parser.set_defaults(func=_dispatch_job_wait)
