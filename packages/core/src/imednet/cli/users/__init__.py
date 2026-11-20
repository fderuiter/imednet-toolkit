"""CLI commands for managing study users."""

from __future__ import annotations

import argparse
from typing import Any

from ...sdk import ImednetSDK
from ..decorators import validate_cli_path_segment, with_sdk
from ..utils import display_list, fetching_status, simulation_mode_context
from ..utils.args import (
    add_list_query_args,
    add_output_format,
    add_simulation_arg,
    add_study_key_args,
    collect_list_items_with_remainder,
    prepare_list_query,
    resolve_output_format,
    resolve_study_key,
)


def _get_val(obj: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(obj, dict):
            if k in obj and obj[k] is not None:
                return obj[k]
        else:
            val = getattr(obj, k, None)
            if val is not None:
                return val
    return default


def _user_view_model(u: Any) -> dict[str, Any]:
    roles = _get_val(u, "roles", default=[])
    if isinstance(roles, list):
        role_names: list[str] = []
        for r in roles:
            r_name = getattr(r, "name", None)
            if isinstance(r_name, str):
                role_names.append(r_name)
            elif isinstance(r, dict) and isinstance(r.get("name"), str):
                role_names.append(r["name"])
            elif isinstance(r, str):
                role_names.append(r)
            else:
                role_names.append(str(r))
        roles_str = ", ".join(role_names)
    else:
        roles_str = str(roles) if roles else ""

    status_val = _get_val(u, "status")
    if status_val is None:
        active = _get_val(u, "user_active_in_study", "userActiveInStudy", "active", "is_active")
        if active is not None:
            status_val = "active" if active else "inactive"

    first = _get_val(u, "first_name", "firstName")
    last = _get_val(u, "last_name", "lastName")
    name_val = _get_val(u, "name")
    if not isinstance(name_val, str):
        name_val = None
    if not name_val and (first or last):
        name_parts = [p for p in (first, last) if isinstance(p, str)]
        name_val = " ".join(name_parts) if name_parts else None

    return {
        "user_id": _get_val(u, "user_id", "userId", "id"),
        "name": name_val,
        "login": _get_val(u, "login", "username"),
        "email": _get_val(u, "email"),
        "status": status_val,
        "roles": roles_str or None,
    }


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("users", help="Manage users within a study.")
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="List users for a specific study.")
    add_study_key_args(list_parser)
    list_parser.add_argument(
        "--include-inactive",
        action="store_true",
        default=False,
        help="Include inactive user accounts.",
    )
    add_list_query_args(list_parser, include_filter=False)

    add_output_format(list_parser)
    add_simulation_arg(list_parser)

    @with_sdk
    def list_users(
        sdk: ImednetSDK,
        study_key: str,
        include_inactive: bool = False,
        output_format: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        fetch_all: bool = False,
        no_interactive: bool = False,
    ) -> None:
        resolved_format = resolve_output_format(output_format)
        req_kwargs, effective_limit = prepare_list_query(
            sort=sort,
            limit=limit,
            fetch_all=fetch_all,
            output_format=resolved_format,
            no_interactive=no_interactive,
        )

        with fetching_status("users", study_key):
            users_list, truncated, load_remaining = collect_list_items_with_remainder(
                sdk.users.list(study_key, include_inactive=include_inactive, **req_kwargs),
                effective_limit,
            )

        if resolved_format == "json":
            display_list(users_list, "users", output_format="json")
            return

        view_models = [_user_view_model(u) for u in users_list]

        def _load_more_users() -> tuple[list[dict[str, Any]], list[Any]]:
            more = load_remaining() if load_remaining is not None else []
            return [_user_view_model(u) for u in more], more

        display_list(
            view_models,
            "users",
            "No users found matching the criteria.",
            details=users_list,
            truncated=truncated,
            interactive=False if no_interactive else None,
            output_format=resolved_format,
            on_load_all=_load_more_users if load_remaining is not None else None,
        )

    def _dispatch_list_users(args: argparse.Namespace) -> None:
        study_key = resolve_study_key(list_parser, args, required=True)
        assert study_key is not None
        validate_cli_path_segment(study_key, name="study_key")
        with simulation_mode_context(bool(getattr(args, "simulation", False))):
            list_users(
                study_key=study_key,
                include_inactive=args.include_inactive,
                output_format=resolve_output_format(args.output_format),
                sort=args.sort,
                limit=args.limit,
                fetch_all=args.fetch_all,
                no_interactive=getattr(args, "no_interactive", False),
            )

    list_parser.set_defaults(func=_dispatch_list_users)
