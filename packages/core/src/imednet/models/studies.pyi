"""Study metadata models for iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class Study(ImednetBaseModel):
    """Represents a clinical study and its metadata."""

    sponsor_key: str | None = Field(default=None, alias="sponsorKey")
    study_key: str | None = Field(default=None, alias="studyKey")
    study_id: int | None = Field(default=None, alias="studyId")
    study_name: str | None = Field(default=None, alias="studyName")
    study_description: str | None = Field(default=None, alias="studyDescription")
    study_type: str | None = Field(default=None, alias="studyType")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def __init__(
        self,
        *,
        sponsor_key: str | None = None,
        sponsorKey: str | None = None,
        study_key: str | None = None,
        studyKey: str | None = None,
        study_id: int | None = None,
        studyId: int | None = None,
        study_name: str | None = None,
        studyName: str | None = None,
        study_description: str | None = None,
        studyDescription: str | None = None,
        study_type: str | None = None,
        studyType: str | None = None,
        date_created: str | None = None,
        dateCreated: str | None = None,
        date_modified: str | None = None,
        dateModified: str | None = None,
        last_updated: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...

__all__ = ["Study"]
