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

    study_key: str | None
    interval_id: int | None
    interval_name: str | None
    interval_description: str | None
    interval_sequence: int | None
    interval_group_id: int | None
    interval_group_name: str | None
    disabled: bool | None
    date_created: str | None
    date_modified: str | None
    actual_date: str | None
    actual_date_form: str | None
    defined_using_interval: str | None
    due_date_will_be_in: int | None
    epro_grace_period: int | None
    negative_slack: int | None
    positive_slack: int | None
    timeline: str | None
    window_calculation_date: str | None
    window_calculation_form: str | None
    last_updated: str | None
