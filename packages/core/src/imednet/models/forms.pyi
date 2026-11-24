"""Form metadata models for iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel

class Form(ImednetBaseModel):
    """Configuration and metadata for a CRF (Case Report Form)."""

    subject_record_report: bool | None = Field(default=None, alias="subjectRecordReport")
    unscheduled_visit: bool | None = Field(default=None, alias="unscheduledVisit")
    disabled: bool | None = Field(default=None, alias="disabled")
    study_key: str | None = Field(default=None, alias="studyKey")
    form_id: int | None = Field(default=None, alias="formId")
    form_key: str | None = Field(default=None, alias="formKey")
    form_name: str | None = Field(default=None, alias="formName")
    form_type: str | None = Field(default=None, alias="formType")
    revision: int | None = Field(default=None, alias="revision")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    epro_form: bool | None = Field(default=None, alias="eproForm")
    allow_copy: bool | None = Field(default=None, alias="allowCopy")
    embedded_log: bool | None = Field(default=None, alias="embeddedLog")
    enforce_ownership: bool | None = Field(default=None, alias="enforceOwnership")
    other_forms: bool | None = Field(default=None, alias="otherForms")
    user_agreement: bool | None = Field(default=None, alias="userAgreement")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def __init__(
        self,
        *,
        subject_record_report: bool | None = None,
        subjectRecordReport: bool | None = None,
        unscheduled_visit: bool | None = None,
        unscheduledVisit: bool | None = None,
        disabled: bool | None = None,
        study_key: str | None = None,
        studyKey: str | None = None,
        form_id: int | None = None,
        formId: int | None = None,
        form_key: str | None = None,
        formKey: str | None = None,
        form_name: str | None = None,
        formName: str | None = None,
        form_type: str | None = None,
        formType: str | None = None,
        revision: int | None = None,
        date_created: str | None = None,
        dateCreated: str | None = None,
        date_modified: str | None = None,
        dateModified: str | None = None,
        epro_form: bool | None = None,
        eproForm: bool | None = None,
        allow_copy: bool | None = None,
        allowCopy: bool | None = None,
        embedded_log: bool | None = None,
        embeddedLog: bool | None = None,
        enforce_ownership: bool | None = None,
        enforceOwnership: bool | None = None,
        other_forms: bool | None = None,
        otherForms: bool | None = None,
        user_agreement: bool | None = None,
        userAgreement: bool | None = None,
        last_updated: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...

__all__ = ["Form"]
