"""Study metadata models for iMedNet."""

from __future__ import annotations

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class Study(ImednetBaseModel):
    """Represents a clinical study and its metadata."""


Study = ModelEngine.get_model('Study', Study)

__all__ = ["Study"]
