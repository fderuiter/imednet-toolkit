# pylint: disable=duplicate-code
"""Models for subject visits and study events."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationInfo, model_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class Visit(ImednetBaseModel):
    """A specific instance of a subject visiting a site (or equivalent event)."""

    @model_validator(mode="before")
    @classmethod
    def _clean_empty_dates(cls, data: Any, info: ValidationInfo) -> Any:
        """Coerce empty strings in date fields to None before validation."""
        if isinstance(data, dict):
            is_wire = isinstance(info.context, dict) and bool(info.context.get("wire"))
            keys = (
                ("startDate", "endDate", "dueDate", "visitDate")
                if is_wire
                else (
                    "start_date",
                    "end_date",
                    "due_date",
                    "visit_date",
                    "startDate",
                    "endDate",
                    "dueDate",
                    "visitDate",
                )
            )
            if any(data.get(key) == "" for key in keys):
                cleaned = dict(data)
                for key in keys:
                    if cleaned.get(key) == "":
                        cleaned[key] = None
                return cleaned
        return data


Visit = ModelEngine.get_model('Visit', Visit)

__all__ = ["Visit"]
