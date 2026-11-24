"""Models for form variables (data fields) in iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class Variable(ImednetBaseModel):
    """Definition of a data field (question) on a form."""

    label: str | None = Field(default=None, alias="label")
    variable_oid: str | None = Field(default=None, alias="variableOid")
    required: bool | str | None = Field(default=None, alias="required")
    study_key: str | None = Field(default=None, alias="studyKey")
    variable_id: int | None = Field(default=None, alias="variableId")
    variable_type: str | None = Field(default=None, alias="variableType")
    variable_name: str | None = Field(default=None, alias="variableName")
    sequence: int | None = Field(default=None, alias="sequence")
    revision: int | None = Field(default=None, alias="revision")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    form_id: int | None = Field(default=None, alias="formId")
    form_key: str | None = Field(default=None, alias="formKey")
    form_name: str | None = Field(default=None, alias="formName")
    blinded: bool | None = Field(default=None, alias="blinded")
    deleted: bool | None = Field(default=None, alias="deleted")
    disabled: bool | None = Field(default=None, alias="disabled")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def __init__(
        self,
        *,
        label: str | None = None,
        variable_oid: str | None = None,
        variableOid: str | None = None,
        required: bool | str | None = None,
        study_key: str | None = None,
        studyKey: str | None = None,
        variable_id: int | None = None,
        variableId: int | None = None,
        variable_type: str | None = None,
        variableType: str | None = None,
        variable_name: str | None = None,
        variableName: str | None = None,
        sequence: int | None = None,
        revision: int | None = None,
        date_created: str | None = None,
        dateCreated: str | None = None,
        date_modified: str | None = None,
        dateModified: str | None = None,
        form_id: int | None = None,
        formId: int | None = None,
        form_key: str | None = None,
        formKey: str | None = None,
        form_name: str | None = None,
        formName: str | None = None,
        blinded: bool | None = None,
        deleted: bool | None = None,
        disabled: bool | None = None,
        last_updated: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...

__all__ = ["Variable"]
