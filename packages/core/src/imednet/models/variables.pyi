"""Models for form variables (data fields) in iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class Variable(ImednetBaseModel):
    """Definition of a data field (question) on a form."""

    label: str | None = Field(default=None, alias="label")
    variable_oid: str | None = Field(default=None, alias="variableOid")

    study_key: str | None
    variable_id: int | None
    variable_type: str | None
    variable_name: str | None
    sequence: int | None
    revision: int | None
    date_created: str | None
    date_modified: str | None
    form_id: int | None
    form_key: str | None
    form_name: str | None
    blinded: bool | None
    deleted: bool | None
    disabled: bool | None
    last_updated: str | None
