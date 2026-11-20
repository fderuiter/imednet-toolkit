"""Study metadata models for iMedNet."""

from __future__ import annotations

from imednet.models.base import ImednetBaseModel

class Study(ImednetBaseModel):
    """Represents a clinical study and its metadata."""

    sponsor_key: str | None = None
    study_key: str | None = None
    study_id: int | None = None
    study_name: str | None = None
    study_description: str | None = None
    study_type: str | None = None
    date_created: str | None = None
    date_modified: str | None = None
    last_updated: str | None = None

__all__ = ["Study"]
