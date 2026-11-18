# pylint: disable=duplicate-code
"""Models for users and roles within iMedNet."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field, computed_field, field_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


def _normalize_role_timestamp(v: Any) -> Any:
    """Normalize 7-element numeric timestamp arrays into UTC datetime objects."""
    if isinstance(v, (list, tuple)):
        if len(v) != 7 or any(isinstance(item, bool) or not isinstance(item, int) for item in v):
            raise ValueError(
                f"Expected 7-element integer timestamp array [Y, M, D, h, m, s, ns], got {v!r}"
            )
        year, month, day, hour, minute, second, nanosecond = v
        if not (0 <= nanosecond < 1_000_000_000):
            raise ValueError(f"Nanosecond component out of range in Role timestamp: {nanosecond}")
        return datetime(year, month, day, hour, minute, second, nanosecond // 1000, tzinfo=UTC)
    return v


class Role(ImednetBaseModel):
    """A role assigned to a user within a study or community."""

    role_id: str | None = Field(default=None, alias="roleId")
    name: str | None = Field(default=None, alias="name")
    description: str | None = Field(default=None, alias="description")
    level: int | None = Field(default=None, alias="level")
    type: str | None = Field(default=None, alias="type")
    inactive: bool | None = Field(default=None, alias="inactive")
    community_id: int | None = Field(default=None, alias="communityId")
    date_created: datetime | None = Field(default=None, alias="dateCreated")
    date_modified: datetime | None = Field(default=None, alias="dateModified")

    @field_validator("date_created", "date_modified", mode="before")
    @classmethod
    def _parse_timestamp_array(cls, v: Any) -> Any:
        """Normalize 7-element numeric arrays before base datetime normalization."""
        return _normalize_role_timestamp(v)


Role = ModelEngine.get_model('Role', Role)


class User(ImednetBaseModel):
    """A user account in the system."""

    roles: list[Role] = Field(default_factory=list, alias="roles")

    @computed_field
    def name(self) -> str:
        """A convenience full-name property so you can do `user.name`.

        instead of f"{user.first_name} {user.last_name}" everywhere.
        """
        # will strip extra spaces if either is empty
        return " ".join(filter(None, (self.first_name, self.last_name)))


User = ModelEngine.get_model('User', User)
