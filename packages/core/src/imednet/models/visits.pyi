# pylint: disable=duplicate-code
"""Models for subject visits and study events."""

from __future__ import annotations

from typing import Any, overload

from pydantic import Field, model_validator

from imednet.models.base import ImednetBaseModel

class Visit(ImednetBaseModel):
    """A specific instance of a subject visiting a site (or equivalent event)."""

    visit_id: int | None = Field(default=None, alias="visitId")
    study_key: str | None = Field(default=None, alias="studyKey")
    interval_id: int | None = Field(default=None, alias="intervalId")
    interval_name: str | None = Field(default=None, alias="intervalName")
    subject_id: int | None = Field(default=None, alias="subjectId")
    subject_key: str | None = Field(default=None, alias="subjectKey")
    start_date: str | None = Field(default=None, alias="startDate")
    end_date: str | None = Field(default=None, alias="endDate")
    due_date: str | None = Field(default=None, alias="dueDate")
    visit_date: str | None = Field(default=None, alias="visitDate")
    deleted: bool | None = Field(default=None, alias="deleted")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    visit_date_form: str | None = Field(default=None, alias="visitDateForm")
    visit_date_question: str | None = Field(default=None, alias="visitDateQuestion")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    @overload
    def __init__(
        self,
        *,
        visit_id: int | None = None,
        study_key: str | None = None,
        interval_id: int | None = None,
        interval_name: str | None = None,
        subject_id: int | None = None,
        subject_key: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        due_date: str | None = None,
        visit_date: str | None = None,
        deleted: bool | None = None,
        date_created: str | None = None,
        date_modified: str | None = None,
        visit_date_form: str | None = None,
        visit_date_question: str | None = None,
        last_updated: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        visitId: int | None = None,
        studyKey: str | None = None,
        intervalId: int | None = None,
        intervalName: str | None = None,
        subjectId: int | None = None,
        subjectKey: str | None = None,
        startDate: str | None = None,
        endDate: str | None = None,
        dueDate: str | None = None,
        visitDate: str | None = None,
        deleted: bool | None = None,
        dateCreated: str | None = None,
        dateModified: str | None = None,
        visitDateForm: str | None = None,
        visitDateQuestion: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...
    @model_validator(mode="before")
    @classmethod
    def _clean_empty_dates(cls, data: Any) -> Any: ...

__all__ = ["Visit"]
