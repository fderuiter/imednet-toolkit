"""Models for representing the hierarchical structure of a study."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from imednet.models.base import ImednetBaseModel

# Import existing models needed for type hints and potentially reuse
from .forms import Form
from .intervals import Interval
from .variables import Variable


# Define a structure for Forms within the context of an Interval, including variables
class FormStructure(ImednetBaseModel):
    """Hierarchical representation of a form including its variables."""

    # Key identifying fields
    form_id: int = Field(..., alias="formId")
    form_key: str = Field(..., alias="formKey")
    form_name: str = Field(..., alias="formName")

    # Additional relevant fields from Form
    form_type: str = Field(..., alias="formType")
    revision: int = Field(..., alias="revision")
    disabled: bool | None = Field(default=None, alias="disabled")
    epro_form: bool = Field(..., alias="eproForm")
    allow_copy: bool = Field(..., alias="allowCopy")
    date_created: datetime = Field(..., alias="dateCreated")
    date_modified: datetime = Field(..., alias="dateModified")

    # Nested variables
    variables: list[Variable] = Field(default_factory=list)

    @classmethod
    def from_form(cls, form: Form, variables: list[Variable]) -> FormStructure:
        """Creates FormStructure from a Form model and its associated variables."""
        form_data = form.model_dump(by_alias=True)
        return cls(**form_data, variables=variables)


# Define a structure for Intervals, containing FormStructures
class IntervalStructure(ImednetBaseModel):
    """Hierarchical representation of an interval including its forms."""

    # Key identifying fields
    interval_id: int = Field(..., alias="intervalId")
    interval_name: str = Field(..., alias="intervalName")

    # Additional relevant fields from Interval
    interval_sequence: int = Field(..., alias="intervalSequence")
    interval_description: str = Field(..., alias="intervalDescription")
    interval_group_name: str = Field(..., alias="intervalGroupName")
    disabled: bool | None = Field(default=None, alias="disabled")
    date_created: datetime = Field(..., alias="dateCreated")
    date_modified: datetime = Field(..., alias="dateModified")

    # Nested forms
    forms: list[FormStructure] = Field(default_factory=list)

    @classmethod
    def from_interval(cls, interval: Interval, forms: list[FormStructure]) -> IntervalStructure:
        """Creates IntervalStructure from an Interval model and its associated FormStructures."""
        interval_data = interval.model_dump(by_alias=True)
        # Remove the 'forms' key to avoid multiple values for keyword argument 'forms'
        interval_data.pop("forms", None)
        return cls(**interval_data, forms=forms)


# Define the root StudyStructure model
class StudyStructure(ImednetBaseModel):
    """Hierarchical representation of a full study including intervals and forms."""

    study_key: str = Field(..., alias="studyKey")
    intervals: list[IntervalStructure] = Field(default_factory=list)
    forms: list[FormStructure] = Field(default_factory=list)
