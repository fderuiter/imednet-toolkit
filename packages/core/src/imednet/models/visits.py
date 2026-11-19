# pylint: disable=duplicate-code
"""Models for subject visits and study events."""

from __future__ import annotations

from typing import Any

from pydantic import model_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class Visit(ImednetBaseModel):
    """A specific instance of a subject visiting a site (or equivalent event)."""

    @model_validator(mode="before")
    @classmethod
    def _clean_empty_dates(cls, data: Any) -> Any:
        """Coerce empty strings in date fields to None before validation."""
        if isinstance(data, dict):
            for key in [
                "start_date",
                "end_date",
                "due_date",
                "visit_date",
                "startDate",
                "endDate",
                "dueDate",
                "visitDate",
            ]:
                if data.get(key) == "":
                    data[key] = None
        return data


Visit = ModelEngine.get_model('Visit', Visit)

__all__ = ["Visit"]
