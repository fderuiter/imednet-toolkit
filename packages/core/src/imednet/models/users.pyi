# pylint: disable=duplicate-code
"""Models for users and roles within iMedNet."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, computed_field

from imednet.models.base import ImednetBaseModel

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

class User(ImednetBaseModel):
    """A user account in the system."""

    roles: list[Role] = Field(default_factory=list, alias="roles")

    @computed_field
    def name(self) -> str: ...

    user_id: str | None
    login: str | None
    first_name: str | None
    last_name: str | None
    email: str | None
    user_active_in_study: bool | None
