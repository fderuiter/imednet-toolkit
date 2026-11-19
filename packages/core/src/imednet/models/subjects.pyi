"""Models for subjects and participant keywords."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class SubjectKeyword(ImednetBaseModel):
    """A keyword or tag associated with a subject."""

    keyword_name: str = Field("", alias="keywordName")
    keyword_key: str = Field("", alias="keywordKey")
    keyword_id: int = Field(0, alias="keywordId")
    date_added: datetime = Field(default_factory=datetime.now, alias="dateAdded")

class Subject(ImednetBaseModel):
    """A subject (participant) in a study, with status and site info."""

    keywords: list[SubjectKeyword] = Field(default_factory=list, alias="keywords")
    study_key: str | None
    subject_id: int | None
    subject_key: str | None
    subject_status: str | None
    site_id: int | None
    site_name: str | None
    deleted: bool | None
    date_created: str | None
    date_modified: str | None
    enrollment_start_date: str | None
    subject_oid: str | None
    last_updated: str | None
