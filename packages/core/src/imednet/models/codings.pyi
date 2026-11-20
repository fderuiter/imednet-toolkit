"""Medical coding models for iMedNet."""

from __future__ import annotations

from imednet.models.base import ImednetBaseModel

class Coding(ImednetBaseModel):
    """Represents a medical coding entry associated with a record."""

    study_key: str | None = None
    site_name: str | None = None
    site_id: int | None = None
    subject_id: int | None = None
    subject_key: str | None = None
    form_id: int | None = None
    form_name: str | None = None
    form_key: str | None = None
    record_id: int | None = None
    variable: str | None = None
    value: str | None = None
    coding_id: int | None = None
    code: str | None = None
    coded_by: str | None = None
    dictionary_name: str | None = None
    dictionary_version: str | None = None
    date_coded: str | None = None
    reason: str | None = None
    revision: int | None = None
    last_updated: str | None = None

__all__ = ["Coding"]
