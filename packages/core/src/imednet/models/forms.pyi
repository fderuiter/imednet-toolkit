"""Form metadata models for iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class Form(ImednetBaseModel):
    """Configuration and metadata for a CRF (Case Report Form)."""

    subject_record_report: bool | None = Field(default=None, alias="subjectRecordReport")
    unscheduled_visit: bool | None = Field(default=None, alias="unscheduledVisit")
    disabled: bool | None = Field(default=None, alias="disabled")
    study_key: str | None = None
    form_id: int | None = None
    form_key: str | None = None
    form_name: str | None = None
    form_type: str | None = None
    revision: int | None = None
    date_created: str | None = None
    date_modified: str | None = None
    epro_form: bool | None = None
    allow_copy: bool | None = None
    embedded_log: bool | None = None
    enforce_ownership: bool | None = None
    other_forms: bool | None = None
    user_agreement: bool | None = None
    last_updated: str | None = None

__all__ = ["Form"]
