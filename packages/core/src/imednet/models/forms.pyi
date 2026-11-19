"""Form metadata models for iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class Form(ImednetBaseModel):
    """Configuration and metadata for a CRF (Case Report Form)."""

    subject_record_report: bool | None = Field(default=None, alias="subjectRecordReport")
    unscheduled_visit: bool | None = Field(default=None, alias="unscheduledVisit")
    disabled: bool | None = Field(default=None, alias="disabled")
    study_key: str | None
    form_id: int | None
    form_key: str | None
    form_name: str | None
    form_type: str | None
    revision: int | None
    date_created: str | None
    date_modified: str | None
    epro_form: bool | None
    allow_copy: bool | None
    embedded_log: bool | None
    enforce_ownership: bool | None
    other_forms: bool | None
    user_agreement: bool | None
    last_updated: str | None
