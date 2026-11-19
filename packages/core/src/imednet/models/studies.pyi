"""Study metadata models for iMedNet."""

from __future__ import annotations

from imednet.models.base import ImednetBaseModel

class Study(ImednetBaseModel):
    """Represents a clinical study and its metadata."""

    sponsor_key: str | None
    study_key: str | None
    study_id: int | None
    study_name: str | None
    study_description: str | None
    study_type: str | None
    date_created: str | None
    date_modified: str | None
    last_updated: str | None

__all__ = ["Study"]
