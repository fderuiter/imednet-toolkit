"""Interval (visit definition) models for iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class FormSummary(ImednetBaseModel):
    """Minimal form details embedded within an interval definition."""

    form_id: int | None = Field(default=None, alias="formId")
    form_key: str | None = Field(default=None, alias="formKey")
    form_name: str | None = Field(default=None, alias="formName")

    def __init__(
        self,
        *,
        form_id: int | None = None,
        formId: int | None = None,
        form_key: str | None = None,
        formKey: str | None = None,
        form_name: str | None = None,
        formName: str | None = None,
    ) -> None: ...

class Interval(ImednetBaseModel):
    """Represents a visit interval or event within the study timeline."""

    forms: list[FormSummary] | None = Field(default=None, alias="forms")
    study_key: str | None = Field(default=None, alias="studyKey")
    interval_id: int | None = Field(default=None, alias="intervalId")
    interval_name: str | None = Field(default=None, alias="intervalName")
    interval_description: str | None = Field(default=None, alias="intervalDescription")
    interval_sequence: int | None = Field(default=None, alias="intervalSequence")
    interval_group_id: int | None = Field(default=None, alias="intervalGroupId")
    interval_group_name: str | None = Field(default=None, alias="intervalGroupName")
    disabled: bool | None = Field(default=None, alias="disabled")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    actual_date: str | None = Field(default=None, alias="actualDate")
    actual_date_form: str | None = Field(default=None, alias="actualDateForm")
    defined_using_interval: str | None = Field(default=None, alias="definedUsingInterval")
    due_date_will_be_in: int | None = Field(default=None, alias="dueDateWillBeIn")
    epro_grace_period: int | None = Field(default=None, alias="eproGracePeriod")
    negative_slack: int | None = Field(default=None, alias="negativeSlack")
    positive_slack: int | None = Field(default=None, alias="positiveSlack")
    timeline: str | None = Field(default=None, alias="timeline")
    window_calculation_date: str | None = Field(default=None, alias="windowCalculationDate")
    window_calculation_form: str | None = Field(default=None, alias="windowCalculationForm")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def __init__(
        self,
        *,
        forms: list[FormSummary] | None = None,
        study_key: str | None = None,
        studyKey: str | None = None,
        interval_id: int | None = None,
        intervalId: int | None = None,
        interval_name: str | None = None,
        intervalName: str | None = None,
        interval_description: str | None = None,
        intervalDescription: str | None = None,
        interval_sequence: int | None = None,
        intervalSequence: int | None = None,
        interval_group_id: int | None = None,
        intervalGroupId: int | None = None,
        interval_group_name: str | None = None,
        intervalGroupName: str | None = None,
        disabled: bool | None = None,
        date_created: str | None = None,
        dateCreated: str | None = None,
        date_modified: str | None = None,
        dateModified: str | None = None,
        actual_date: str | None = None,
        actualDate: str | None = None,
        actual_date_form: str | None = None,
        actualDateForm: str | None = None,
        defined_using_interval: str | None = None,
        definedUsingInterval: str | None = None,
        due_date_will_be_in: int | None = None,
        dueDateWillBeIn: int | None = None,
        epro_grace_period: int | None = None,
        eproGracePeriod: int | None = None,
        negative_slack: int | None = None,
        negativeSlack: int | None = None,
        positive_slack: int | None = None,
        positiveSlack: int | None = None,
        timeline: str | None = None,
        window_calculation_date: str | None = None,
        windowCalculationDate: str | None = None,
        window_calculation_form: str | None = None,
        windowCalculationForm: str | None = None,
        last_updated: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...

__all__ = ["FormSummary", "Interval"]
