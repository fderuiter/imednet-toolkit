"""Medical coding models for iMedNet."""

from __future__ import annotations

from typing import overload

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class Coding(ImednetBaseModel):
    """Represents a medical coding entry associated with a record."""

    study_key: str | None = Field(default=None, alias="studyKey")
    site_name: str | None = Field(default=None, alias="siteName")
    site_id: int | None = Field(default=None, alias="siteId")
    subject_id: int | None = Field(default=None, alias="subjectId")
    subject_key: str | None = Field(default=None, alias="subjectKey")
    form_id: int | None = Field(default=None, alias="formId")
    form_name: str | None = Field(default=None, alias="formName")
    form_key: str | None = Field(default=None, alias="formKey")
    record_id: int | None = Field(default=None, alias="recordId")
    variable: str | None = Field(default=None, alias="variable")
    value: str | None = Field(default=None, alias="value")
    coding_id: int | None = Field(default=None, alias="codingId")
    code: str | None = Field(default=None, alias="code")
    coded_by: str | None = Field(default=None, alias="codedBy")
    dictionary_name: str | None = Field(default=None, alias="dictionaryName")
    dictionary_version: str | None = Field(default=None, alias="dictionaryVersion")
    date_coded: str | None = Field(default=None, alias="dateCoded")
    reason: str | None = Field(default=None, alias="reason")
    revision: int | None = Field(default=None, alias="revision")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    @overload
    def __init__(
        self,
        *,
        study_key: str | None = None,
        site_name: str | None = None,
        site_id: int | None = None,
        subject_id: int | None = None,
        subject_key: str | None = None,
        form_id: int | None = None,
        form_name: str | None = None,
        form_key: str | None = None,
        record_id: int | None = None,
        variable: str | None = None,
        value: str | None = None,
        coding_id: int | None = None,
        code: str | None = None,
        coded_by: str | None = None,
        dictionary_name: str | None = None,
        dictionary_version: str | None = None,
        date_coded: str | None = None,
        reason: str | None = None,
        revision: int | None = None,
        last_updated: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        studyKey: str | None = None,
        siteName: str | None = None,
        siteId: int | None = None,
        subjectId: int | None = None,
        subjectKey: str | None = None,
        formId: int | None = None,
        formName: str | None = None,
        formKey: str | None = None,
        recordId: int | None = None,
        variable: str | None = None,
        value: str | None = None,
        codingId: int | None = None,
        code: str | None = None,
        codedBy: str | None = None,
        dictionaryName: str | None = None,
        dictionaryVersion: str | None = None,
        dateCoded: str | None = None,
        reason: str | None = None,
        revision: int | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...

__all__ = ["Coding"]
