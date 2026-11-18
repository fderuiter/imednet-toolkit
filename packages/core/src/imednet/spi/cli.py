"""Service Provider Interface (SPI) for CLI extensions."""

from imednet.cli.decorators import with_sdk
from imednet.cli.utils import (
    STUDY_KEY_ARG,
    Console,
    Status,
    clear_line,
    console,
    fetching_status,
    format_job_progress,
    parse_filter_args,
    print_error,
)
from imednet.cli.utils.args import add_output_format
from imednet.cli.utils.output import display_list, emit_json

__all__ = [
    "STUDY_KEY_ARG",
    "Console",
    "Status",
    "add_output_format",
    "clear_line",
    "console",
    "display_list",
    "emit_json",
    "fetching_status",
    "format_job_progress",
    "parse_filter_args",
    "print_error",
    "with_sdk",
]
