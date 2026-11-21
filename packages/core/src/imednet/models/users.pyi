# pylint: disable=duplicate-code
"""Models for users and roles within iMedNet."""

from __future__ import annotations

from datetime import datetime
from typing import Any, overload

from pydantic import Field, computed_field, field_validator

from imednet.models.base import ImednetBaseModel

def _normalize_role_timestamp(v: Any) -> Any: ...

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

    @overload
    def __init__(
        self,
        *,
        role_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        level: int | None = None,
        type: str | None = None,
        inactive: bool | None = None,
        community_id: int | None = None,
        date_created: datetime | None = None,
        date_modified: datetime | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        roleId: str | None = None,
        name: str | None = None,
        description: str | None = None,
        level: int | None = None,
        type: str | None = None,
        inactive: bool | None = None,
        communityId: int | None = None,
        dateCreated: datetime | None = None,
        dateModified: datetime | None = None,
    ) -> None: ...
    @field_validator("date_created", "date_modified", mode="before")
    @classmethod
    def _parse_timestamp_array(cls, v: Any) -> Any: ...

class User(ImednetBaseModel):
    """A user account in the system."""

    roles: list[Role] = Field(default_factory=list, alias="roles")
    user_id: str | None = Field(default=None, alias="userId")
    login: str | None = Field(default=None, alias="login")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    email: str | None = Field(default=None, alias="email")
    user_active_in_study: bool | None = Field(default=None, alias="userActiveInStudy")

    @overload
    def __init__(
        self,
        *,
        roles: list[Role] = ...,
        user_id: str | None = None,
        login: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        user_active_in_study: bool | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        roles: list[Role] = ...,
        userId: str | None = None,
        login: str | None = None,
        firstName: str | None = None,
        lastName: str | None = None,
        email: str | None = None,
        userActiveInStudy: bool | None = None,
    ) -> None: ...
    @computed_field
    def name(self) -> str: ...

__all__ = ["Role", "User"]
