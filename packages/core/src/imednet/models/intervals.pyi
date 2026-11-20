"""Interval (visit definition) models for iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class FormSummary(ImednetBaseModel):
    """Minimal form details embedded within an interval definition."""

    form_id: int = Field(0, alias="formId")
    form_key: str = Field("", alias="formKey")
    form_name: str = Field("", alias="formName")

class Interval(ImednetBaseModel):
    """Represents a visit interval or event within the study timeline."""

    forms: list[FormSummary] | None = Field(default=None, alias="forms")
    study_key: str | None = None
    interval_id: int | None = None
    interval_name: str | None = None
    interval_description: str | None = None
    interval_sequence: int | None = None
    interval_group_id: int | None = None
    interval_group_name: str | None = None
    disabled: bool | None = None
    date_created: str | None = None
    date_modified: str | None = None
    actual_date: str | None = None
    actual_date_form: str | None = None
    defined_using_interval: str | None = None
    due_date_will_be_in: int | None = None
    epro_grace_period: int | None = None
    negative_slack: int | None = None
    positive_slack: int | None = None
    timeline: str | None = None
    window_calculation_date: str | None = None
    window_calculation_form: str | None = None
    last_updated: str | None = None

__all__ = ["FormSummary", "Interval"]
