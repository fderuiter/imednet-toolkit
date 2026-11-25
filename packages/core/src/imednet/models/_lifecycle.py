"""Shared status normalization, transition, and soft-delete lifecycle helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from enum import Enum
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidatorFunctionWrapHandler

StatusEnumT = TypeVar("StatusEnumT", bound=Enum)

_TOKEN_SEPARATOR_RE = re.compile(r"[\s_-]+")


def normalize_status_token(value: str) -> str:
    """Collapse whitespace, underscores, and hyphens into a lowercase token."""
    return _TOKEN_SEPARATOR_RE.sub(" ", value.strip()).lower()


def resolve_status_member(
    enum_cls: type[StatusEnumT],
    value: object,
    *,
    aliases: Mapping[str, StatusEnumT] | None = None,
) -> StatusEnumT | None:
    """Resolve an enum status member case- and separator-insensitively."""
    if not isinstance(value, str):
        return None
    token = normalize_status_token(value)
    if not token:
        return None
    if aliases is not None and token in aliases:
        return aliases[token]
    for member in enum_cls:
        if (
            normalize_status_token(str(member.value)) == token
            or normalize_status_token(member.name) == token
        ):
            return member
    return None


def coerce_status(
    enum_cls: type[StatusEnumT],
    value: object,
) -> StatusEnumT | str | None:
    """Normalize known enum statuses while preserving custom study status strings."""
    if value is None or isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            return value
    return str(value)


def coerce_field_status(
    enum_cls: type[StatusEnumT],
    value: object,
    handler: ValidatorFunctionWrapHandler,
) -> StatusEnumT | str | None:
    """Validate status field preserving verbatim strings unless matching an enum .value."""
    if value is None or isinstance(value, enum_cls):
        return cast("StatusEnumT | None", handler(value))
    text = value if isinstance(value, str) else str(value)
    stripped = text.strip()
    for member in enum_cls:
        if member.value == stripped:
            return member
    return stripped


def build_terminal_transition_map(
    enum_cls: type[StatusEnumT],
    terminal_member: StatusEnumT,
) -> dict[StatusEnumT, set[StatusEnumT]]:
    """Build an all-to-all status transition map with a terminal soft-delete state."""
    non_deleted = {member for member in enum_cls if member != terminal_member}
    return {
        **{status: (non_deleted - {status}) | {terminal_member} for status in non_deleted},
        terminal_member: set(),
    }


def _coerce_validated_transition(
    enum_cls: type[StatusEnumT],
    allowed_transitions: Mapping[StatusEnumT, AbstractSet[StatusEnumT]],
    deleted_member: StatusEnumT,
    entity_label: str,
    from_status: StatusEnumT | str | None,
    to_status: StatusEnumT | str,
    *,
    deleted: bool | None = None,
) -> StatusEnumT | str:
    """Validate an entity status transition and return the normalized target status."""
    status_label = entity_label.lower()
    src = coerce_status(enum_cls, from_status)
    dst = coerce_status(enum_cls, to_status)
    if dst is None or (isinstance(dst, str) and not dst.strip()):
        raise ValueError(f"Invalid target {status_label} status: {to_status!r}")

    is_soft_deleted = deleted if deleted is not None else src == deleted_member
    if is_soft_deleted:
        raise ValueError(
            f"Cannot transition soft-deleted {entity_label} from {deleted_member.value} to {dst!s}."
        )

    if deleted is False and src == deleted_member:
        if dst != deleted_member and from_status == to_status:
            raise ValueError(
                f"Cannot transition {status_label} status from {from_status!s} to {dst!s}."
            )
        return dst

    if isinstance(src, enum_cls) and isinstance(dst, enum_cls):
        permitted = allowed_transitions.get(src, set())
        if dst not in permitted:
            options = sorted(str(p.value) for p in permitted)
            raise ValueError(
                f"Cannot transition {status_label} status from {src.value} to {dst.value}. "
                f"Permitted transitions from {src.value}: {options}"
            )
    elif src is not None and src == dst:
        raise ValueError(f"Cannot transition {status_label} status from {src!s} to {dst!s}.")
    return dst


def validate_status_transition(
    enum_cls: type[StatusEnumT],
    allowed_transitions: Mapping[StatusEnumT, AbstractSet[StatusEnumT]],
    deleted_member: StatusEnumT,
    entity_label: str,
    from_status: StatusEnumT | str | None,
    to_status: StatusEnumT | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Validate an entity status transition, raising ValueError when disallowed."""
    _coerce_validated_transition(
        enum_cls,
        allowed_transitions,
        deleted_member,
        entity_label,
        from_status,
        to_status,
        deleted=deleted,
    )
    return True


def is_valid_status_transition(
    enum_cls: type[StatusEnumT],
    allowed_transitions: Mapping[StatusEnumT, AbstractSet[StatusEnumT]],
    deleted_member: StatusEnumT,
    entity_label: str,
    from_status: StatusEnumT | str | None,
    to_status: StatusEnumT | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Return True when transitioning *entity_label* from *from_status* to *to_status* is valid."""
    try:
        return validate_status_transition(
            enum_cls,
            allowed_transitions,
            deleted_member,
            entity_label,
            from_status,
            to_status,
            deleted=deleted,
        )
    except ValueError:
        return False


def is_entity_deleted(
    deleted: bool | None,
    status: object,
    deleted_member: StatusEnumT,
) -> bool:
    """Return True when ``deleted=True`` or when ``deleted is None`` and status resolves to DELETED."""
    if deleted is not None:
        return deleted
    return coerce_status(type(deleted_member), status) == deleted_member


def resolve_effective_status(
    deleted: bool | None,
    status: StatusEnumT | str | None,
    deleted_member: StatusEnumT,
) -> StatusEnumT | str | None:
    """Return *deleted_member* when soft-deleted, otherwise the current status."""
    if is_entity_deleted(deleted, status, deleted_member):
        return deleted_member
    return status


def apply_entity_transition(
    instance: BaseModel,
    *,
    enum_cls: type[StatusEnumT],
    allowed_transitions: Mapping[StatusEnumT, AbstractSet[StatusEnumT]],
    deleted_member: StatusEnumT,
    entity_label: str,
    status_field: str,
    target_status: StatusEnumT | str,
) -> StatusEnumT | str:
    """Validate and apply a status transition on *instance*, enforcing soft-delete invariants."""
    current_status: StatusEnumT | str | None = getattr(instance, status_field)
    raw_deleted: bool | None = getattr(instance, "deleted", None)
    normalized = _coerce_validated_transition(
        enum_cls,
        allowed_transitions,
        deleted_member,
        entity_label,
        current_status,
        target_status,
        deleted=raw_deleted,
    )
    BaseModel.__setattr__(instance, status_field, normalized)
    if normalized == deleted_member:
        BaseModel.__setattr__(instance, "deleted", True)
    return normalized


def setattr_with_soft_delete_guard(
    instance: BaseModel,
    name: str,
    value: Any,
    *,
    enum_cls: type[StatusEnumT],
    deleted_member: StatusEnumT,
    entity_label: str,
    status_field: str,
) -> None:
    """Enforce terminal soft-delete invariants on post-initialization attribute mutation."""
    state = instance.__dict__
    initialized = "deleted" in state or status_field in state
    currently_deleted = is_entity_deleted(
        state.get("deleted"),
        state.get(status_field),
        deleted_member,
    )
    if initialized and currently_deleted:
        if name == "deleted" and value is not True:
            raise ValueError(f"Cannot restore a soft-deleted {entity_label} by clearing deleted.")
        if name == status_field:
            normalized = coerce_status(enum_cls, value)
            if normalized != deleted_member:
                raise ValueError(
                    f"Cannot mutate soft-deleted {entity_label} to active status {value!r}."
                )
            BaseModel.__setattr__(instance, name, normalized)
            return
    if initialized and name == status_field:
        normalized = coerce_status(enum_cls, value)
        BaseModel.__setattr__(instance, name, normalized)
        if normalized == deleted_member:
            BaseModel.__setattr__(instance, "deleted", True)
        return
    BaseModel.__setattr__(instance, name, value)


__all__ = [
    "StatusEnumT",
    "apply_entity_transition",
    "build_terminal_transition_map",
    "coerce_field_status",
    "coerce_status",
    "is_entity_deleted",
    "is_valid_status_transition",
    "normalize_status_token",
    "resolve_effective_status",
    "resolve_status_member",
    "setattr_with_soft_delete_guard",
    "validate_status_transition",
]
