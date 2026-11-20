"""CLI commands for managing study subjects."""

from __future__ import annotations

import argparse
from typing import Any

from ...endpoints.subjects import SubjectsEndpoint
from ...models.subjects import Subject
from ...sdk import ImednetSDK
from ..decorators import validate_cli_path_segment, with_sdk
from ..utils import display_list, fetching_status
from ..utils.args import (
    add_list_query_args,
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    collect_list_items_with_remainder,
    prepare_list_query,
    resolve_study_key,
)


def _subject_view_model(s: Any) -> dict[str, Any]:
    keywords_str = ", ".join(k.keyword_name for k in s.keywords)
    return {
        "subject_key": s.subject_key,
        "status": s.subject_status,
        "site": s.site_name,
        "enrollment_date": s.enrollment_start_date,
        "keywords": keywords_str,
    }


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("subjects", help="Manage subjects within a study.")
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="List subjects for a specific study.")
    add_study_key_args(list_parser)
    add_list_query_args(
        list_parser,
        filter_dest="subject_filter",
        filter_help="Filter criteria (e.g., 'subject_status=Screened' or 'subjectStatus==Screened'). Repeat for multiple filters.",
    )

    add_output_format(list_parser)
    add_simulation_arg(list_parser)

    @with_sdk
    def list_subjects(
        sdk: ImednetSDK,
        study_key: str,
        subject_filter: list[str] | None = None,
        output_format: str = "table",
        sort: str | None = None,
        limit: int | None = None,
        fetch_all: bool = False,
        no_interactive: bool = False,
        _prepared_query: tuple[dict[str, Any], int | None] | None = None,
    ) -> None:
        req_kwargs, effective_limit = (
            _prepared_query
            if _prepared_query is not None
            else prepare_list_query(
                filter_args=subject_filter,
                sort=sort,
                limit=limit,
                fetch_all=fetch_all,
                output_format=output_format,
                no_interactive=no_interactive,
                model=Subject,
                allowed_attributes=SubjectsEndpoint.FILTER_ATTRIBUTES,
                disallow_study_key=True,
            )
        )

        with fetching_status("subjects", study_key):
            subjects_list, truncated, load_remaining = collect_list_items_with_remainder(
                sdk.subjects.list(study_key, **req_kwargs),
                effective_limit,
            )

        if output_format == "json":
            display_list(subjects_list, "subjects", output_format="json")
            return
        view_models = [_subject_view_model(s) for s in subjects_list]

        def _load_more_subjects() -> tuple[list[dict[str, Any]], list[Any]]:
            more = load_remaining() if load_remaining is not None else []
            return [_subject_view_model(s) for s in more], more

        display_list(
            view_models,
            "subjects",
            "No subjects found matching the criteria.",
            details=subjects_list,
            truncated=truncated,
            interactive=False if no_interactive else None,
            on_load_all=_load_more_subjects if load_remaining is not None else None,
        )

    def _dispatch_list_subjects(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(list_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        no_interactive = getattr(args, "no_interactive", False)
        prepared = prepare_list_query(
            filter_args=args.subject_filter,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
            output_format=args.output_format,
            no_interactive=no_interactive,
            model=Subject,
            allowed_attributes=SubjectsEndpoint.FILTER_ATTRIBUTES,
            disallow_study_key=True,
        )
        list_subjects(
            study_key=study_key,
            subject_filter=args.subject_filter,
            output_format=args.output_format,
            sort=args.sort,
            limit=args.limit,
            fetch_all=args.fetch_all,
            no_interactive=no_interactive,
            _prepared_query=prepared,
        )

    list_parser.set_defaults(func=_dispatch_list_subjects)
