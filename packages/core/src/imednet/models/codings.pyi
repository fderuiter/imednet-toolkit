"""Medical coding models for iMedNet."""

from __future__ import annotations

from typing import Any

from imednet.models.base import ImednetBaseModel

class Coding(ImednetBaseModel):
    """Represents a medical coding entry associated with a record."""

    study_key: str | None
    site_name: str | None
    site_id: int | None
    subject_id: int | None
    subject_key: str | None
    form_id: int | None
    form_name: str | None
    form_key: str | None
    record_id: int | None
    variable: str | None
    value: str | None
    coding_id: str | None
    code: str | None
    coded_by: str | None
    dictionary_name: str | None
    dictionary_version: str | None
    date_coded: str | None
    last_updated: str | None
    reason: Any
    revision: Any
