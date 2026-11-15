"""Form metadata models for iMedNet."""

from __future__ import annotations

from typing import Any

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
    embedded_log: Any
    enforce_ownership: Any
    other_forms: Any
    user_agreement: Any
    last_updated: str | None
