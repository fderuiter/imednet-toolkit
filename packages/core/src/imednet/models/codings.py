"""Medical coding models for iMedNet."""

from __future__ import annotations

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class Coding(ImednetBaseModel):
    """Represents a medical coding entry associated with a record."""


Coding = ModelEngine.get_model('Coding', Coding)

__all__ = ["Coding"]
