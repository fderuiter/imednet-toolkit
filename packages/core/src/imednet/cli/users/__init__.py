"""CLI commands for managing study users."""

from __future__ import annotations

import argparse
from typing import Any

from ...sdk import ImednetSDK
from ..decorators import with_sdk
from ..utils import STUDY_KEY_ARG, display_list, fetching_status
from ..utils.args import add_output_format


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


def setup_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Setup the parser for this module."""
    parser = subparsers.add_parser("users", help="Manage users within a study.")
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="List users for a specific study.")
    list_parser.add_argument("study_key", help=STUDY_KEY_ARG)
    list_parser.add_argument(
        "--include-inactive",
        action="store_true",
        default=False,
        help="Include inactive user accounts.",
    )

    add_output_format(list_parser)

    @with_sdk
    def list_users(
        sdk: ImednetSDK,
        study_key: str,
        include_inactive: bool = False,
        output_format: str = "table",
        no_interactive: bool = False,
    ) -> None:
        with fetching_status("users", study_key):
            users_list = list(sdk.users.list(study_key, include_inactive=include_inactive))

        if output_format == "json":
            display_list(users_list, "users", output_format="json")
            return

        view_models = []
        for u in users_list:
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
                active = _get_val(
                    u, "user_active_in_study", "userActiveInStudy", "active", "is_active"
                )
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

            view_models.append(
                {
                    "user_id": _get_val(u, "user_id", "userId", "id"),
                    "name": name_val,
                    "login": _get_val(u, "login", "username"),
                    "email": _get_val(u, "email"),
                    "status": status_val,
                    "roles": roles_str or None,
                }
            )

        display_list(
            view_models,
            "users",
            "No users found matching the criteria.",
            details=users_list,
            interactive=False if no_interactive else None,
        )

    list_parser.set_defaults(
        func=lambda args: list_users(
            study_key=args.study_key,
            include_inactive=args.include_inactive,
            output_format=args.output_format,
            no_interactive=getattr(args, "no_interactive", False),
        )
    )
