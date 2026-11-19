"""Interval (visit definition) models for iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class FormSummary(ImednetBaseModel):
    """Minimal form details embedded within an interval definition."""

    form_id: int = Field(0, alias="formId")
    form_key: str = Field("", alias="formKey")
    form_name: str = Field("", alias="formName")


class Interval(ImednetBaseModel):
    """Represents a visit interval or event within the study timeline."""

    forms: list[FormSummary] | None = Field(default=None, alias="forms")


Interval = ModelEngine.get_model('Interval', Interval)

__all__ = ["FormSummary", "Interval"]
