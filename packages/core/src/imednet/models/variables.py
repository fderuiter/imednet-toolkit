"""Models for form variables (data fields) in iMedNet."""

from __future__ import annotations

from pydantic import Field

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class Variable(ImednetBaseModel):
    """Definition of a data field (question) on a form."""

    label: str | None = Field(default=None, alias="label")
    variable_oid: str | None = Field(default=None, alias="variableOid")


Variable = ModelEngine.get_model('Variable', Variable)

__all__ = ["Variable"]
