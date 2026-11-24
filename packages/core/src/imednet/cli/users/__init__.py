"""CLI commands for managing study users."""

from __future__ import annotations

import argparse
from typing import Any

from ..utils import get_model_value, register_list_command


def _user_view_model(u: Any) -> dict[str, Any]:
    roles = get_model_value(u, "roles", default=[])
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

    status_val = get_model_value(u, "status")
    if status_val is None:
        active = get_model_value(
            u, "user_active_in_study", "userActiveInStudy", "active", "is_active"
        )
        if active is not None:
            status_val = "active" if active else "inactive"

    first = get_model_value(u, "first_name", "firstName")
    last = get_model_value(u, "last_name", "lastName")
    name_val = get_model_value(u, "name")
    if not isinstance(name_val, str):
        name_val = None
    if not name_val and (first or last):
        name_parts = [p for p in (first, last) if isinstance(p, str)]
        name_val = " ".join(name_parts) if name_parts else None

    return {
        "user_id": get_model_value(u, "user_id", "userId", "id"),
        "name": name_val,
        "login": get_model_value(u, "login", "username"),
        "email": get_model_value(u, "email"),
        "status": status_val,
        "roles": roles_str or None,
    }


def _add_users_list_args(list_parser: argparse.ArgumentParser) -> None:
    list_parser.add_argument(
        "--include-inactive",
        action="store_true",
        default=False,
        help="Include inactive user accounts.",
    )


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("users", help="Manage users within a study.")
    register_list_command(
        parser,
        "users",
        "users",
        requires_study_key=True,
        list_help="List users for a specific study.",
        empty_msg="No users found matching the criteria.",
        row_mapper=_user_view_model,
        include_filter=False,
        extra_args_fn=_add_users_list_args,
        extra_kwargs_fn=lambda args: {"include_inactive": args.include_inactive},
        preflight_query=True,
    )
