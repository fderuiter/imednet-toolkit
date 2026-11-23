"""Models for representing the hierarchical structure of a study."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from imednet.models.base import ImednetBaseModel

from .forms import Form
from .intervals import Interval
from .variables import Variable

class FormStructure(ImednetBaseModel):
    """Hierarchical representation of a form including its variables."""

    form_id: int = Field(..., alias="formId")
    form_key: str = Field(..., alias="formKey")
    form_name: str = Field(..., alias="formName")
    form_type: str = Field(..., alias="formType")
    revision: int = Field(..., alias="revision")
    disabled: bool | None = Field(default=None, alias="disabled")
    epro_form: bool = Field(..., alias="eproForm")
    allow_copy: bool = Field(..., alias="allowCopy")
    date_created: datetime = Field(..., alias="dateCreated")
    date_modified: datetime = Field(..., alias="dateModified")
    variables: list[Variable] = Field(default_factory=list)

    def __init__(
        self,
        *,
        form_id: int = ...,
        formId: int = ...,
        form_key: str = ...,
        formKey: str = ...,
        form_name: str = ...,
        formName: str = ...,
        form_type: str = ...,
        formType: str = ...,
        revision: int,
        disabled: bool | None = None,
        epro_form: bool = ...,
        eproForm: bool = ...,
        allow_copy: bool = ...,
        allowCopy: bool = ...,
        date_created: datetime = ...,
        dateCreated: datetime = ...,
        date_modified: datetime = ...,
        dateModified: datetime = ...,
        variables: list[Variable] = ...,
    ) -> None: ...
    @classmethod
    def from_form(cls, form: Form, variables: list[Variable]) -> FormStructure: ...

class IntervalStructure(ImednetBaseModel):
    """Hierarchical representation of an interval including its forms."""

    interval_id: int = Field(..., alias="intervalId")
    interval_name: str = Field(..., alias="intervalName")
    interval_sequence: int = Field(..., alias="intervalSequence")
    interval_description: str = Field(..., alias="intervalDescription")
    interval_group_name: str = Field(..., alias="intervalGroupName")
    disabled: bool | None = Field(default=None, alias="disabled")
    date_created: datetime = Field(..., alias="dateCreated")
    date_modified: datetime = Field(..., alias="dateModified")
    forms: list[FormStructure] = Field(default_factory=list)

    def __init__(
        self,
        *,
        interval_id: int = ...,
        intervalId: int = ...,
        interval_name: str = ...,
        intervalName: str = ...,
        interval_sequence: int = ...,
        intervalSequence: int = ...,
        interval_description: str = ...,
        intervalDescription: str = ...,
        interval_group_name: str = ...,
        intervalGroupName: str = ...,
        disabled: bool | None = None,
        date_created: datetime = ...,
        dateCreated: datetime = ...,
        date_modified: datetime = ...,
        dateModified: datetime = ...,
        forms: list[FormStructure] = ...,
    ) -> None: ...
    @classmethod
    def from_interval(cls, interval: Interval, forms: list[FormStructure]) -> IntervalStructure: ...

class StudyStructure(ImednetBaseModel):
    """Hierarchical representation of a full study including intervals and forms."""

    study_key: str = Field(..., alias="studyKey")
    intervals: list[IntervalStructure] = Field(default_factory=list)
    forms: list[FormStructure] = Field(default_factory=list)

    def __init__(
        self,
        *,
        study_key: str = ...,
        studyKey: str = ...,
        intervals: list[IntervalStructure] = ...,
        forms: list[FormStructure] = ...,
    ) -> None: ...
