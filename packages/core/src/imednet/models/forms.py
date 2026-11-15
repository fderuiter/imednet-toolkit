"""Form metadata models for iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class Form(ImednetBaseModel):
    """Configuration and metadata for a CRF (Case Report Form)."""

    subject_record_report: bool | None = Field(default=None, alias="subjectRecordReport")
    unscheduled_visit: bool | None = Field(default=None, alias="unscheduledVisit")
    disabled: bool | None = Field(default=None, alias="disabled")


Form = ModelEngine.get_model('Form', Form)
