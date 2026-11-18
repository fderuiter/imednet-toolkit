"""CLI commands for managing study subjects."""

from __future__ import annotations

import argparse

from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, display_list, fetching_status
from ..utils.args import (
    add_list_query_args,
    add_output_format,
    collect_list_items,
    prepare_list_query,
)


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("subjects", help="Manage subjects within a study.")
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="List subjects for a specific study.")
    list_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    add_list_query_args(
        list_parser,
        filter_dest="subject_filter",
        filter_help="Filter criteria (e.g., 'subject_status=Screened' or 'subjectStatus==Screened'). Repeat for multiple filters.",
    )

    add_output_format(list_parser)

    @with_sdk
    def list_subjects(
        sdk: ImednetSDK,
        study_key: str,
        subject_filter: list[str] | None = None,
        output_format: str = "table",
        sort: str | None = None,
        limit: int | None = None,
        fetch_all: bool = False,
    ) -> None:
        req_kwargs, effective_limit = prepare_list_query(
            filter_args=subject_filter,
            sort=sort,
            limit=limit,
            fetch_all=fetch_all,
            output_format=output_format,
        )

        with fetching_status("subjects", study_key):
            subjects_list, truncated = collect_list_items(
                sdk.subjects.list(study_key, **req_kwargs),
                effective_limit,
            )

        if output_format == "json":
            display_list(subjects_list, "subjects", output_format="json")
            return
        view_models = []
        for s in subjects_list:
            keywords_str = ", ".join(k.keyword_name for k in s.keywords)
            view_models.append(
                {
                    "subject_key": s.subject_key,
                    "status": s.subject_status,
                    "site": s.site_name,
                    "enrollment_date": s.enrollment_start_date,
                    "keywords": keywords_str,
                }
            )

        display_list(
            view_models,
            "subjects",
            "No subjects found matching the criteria.",
            details=subjects_list,
            truncated=truncated,
        )

    list_parser.set_defaults(
        func=lambda args: list_subjects(
            study_key=args.study_key,
            subject_filter=args.subject_filter,
            output_format=args.output_format,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
        )
    )
