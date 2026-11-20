# pylint: disable=duplicate-code
"""Models for subject visits and study events."""

from __future__ import annotations

from typing import Any

from pydantic import model_validator

from imednet.models.base import ImednetBaseModel

class Visit(ImednetBaseModel):
    """A specific instance of a subject visiting a site (or equivalent event)."""

    visit_id: int | None = None
    study_key: str | None = None
    interval_id: int | None = None
    interval_name: str | None = None
    subject_id: int | None = None
    subject_key: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    due_date: str | None = None
    visit_date: str | None = None
    deleted: bool | None = None
    date_created: str | None = None
    date_modified: str | None = None
    visit_date_form: str | None = None
    visit_date_question: str | None = None
    last_updated: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _clean_empty_dates(cls, data: Any) -> Any: ...

__all__ = ["Visit"]
