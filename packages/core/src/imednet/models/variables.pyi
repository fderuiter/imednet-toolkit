"""Models for form variables (data fields) in iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class Variable(ImednetBaseModel):
    """Definition of a data field (question) on a form."""

    label: str | None = Field(default=None, alias="label")
    variable_oid: str | None = Field(default=None, alias="variableOid")
    required: bool | str | None = Field(default=None, alias="required")
    study_key: str | None = None
    variable_id: int | None = None
    variable_type: str | None = None
    variable_name: str | None = None
    sequence: int | None = None
    revision: int | None = None
    date_created: str | None = None
    date_modified: str | None = None
    form_id: int | None = None
    form_key: str | None = None
    form_name: str | None = None
    blinded: bool | None = None
    deleted: bool | None = None
    disabled: bool | None = None
    last_updated: str | None = None

__all__ = ["Variable"]
