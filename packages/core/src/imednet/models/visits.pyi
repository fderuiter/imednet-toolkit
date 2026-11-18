"""Models for subject visits and study events."""

from __future__ import annotations

from imednet.models.base import ImednetBaseModel

class Visit(ImednetBaseModel):
    """A specific instance of a subject visiting a site (or equivalent event)."""

    visit_id: int | None
    study_key: str | None
    interval_id: int | None
    interval_name: str | None
    subject_id: int | None
    subject_key: str | None
    start_date: str | None
    end_date: str | None
    due_date: str | None
    visit_date: str | None
    deleted: bool | None
    date_created: str | None
    date_modified: str | None
    visit_date_form: str | None
    visit_date_question: str | None
    last_updated: str | None
