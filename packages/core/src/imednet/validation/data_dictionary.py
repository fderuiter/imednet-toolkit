"""Loading and container for iMednet Data Dictionary files (legacy shim).

This module is retained for backward compatibility. New code should use
`imednet.datadict`.
"""

from __future__ import annotations

import csv
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TextIO

from imednet.datadict import load_from_csv, load_from_csv_files
from imednet.datadict.models import DataDictionary

__all__ = ["DataDictionary", "DataDictionaryLoader"]


class DataDictionaryLoader:
    """Load data dictionary files from various sources (legacy shim)."""

    REQUIRED_FILES = {  # noqa: RUF012
        "BUSINESS_LOGIC.csv": "business_logic",
        "CHOICES.csv": "choices",
        "FORMS.csv": "forms",
        "QUESTIONS.csv": "questions",
    }

    @staticmethod
    @contextmanager
    def _open_text(source: Path | TextIO) -> Iterator[TextIO]:
        """Provide a text stream for the given source."""
        if isinstance(source, str | Path):
            with open(source, encoding="utf-8", newline="") as f:
                yield f
        else:
            yield source

    @classmethod
    def _load_csv(cls, source: Path | TextIO) -> list[dict[str, str]]:
        """Load a CSV file into a list of dictionaries."""
        with cls._open_text(source) as f:
            reader = csv.DictReader(f)
            return list(reader)

    @classmethod
    def from_files(
        cls,
        *,
        business_logic: Path | TextIO,
        choices: Path | TextIO,
        forms: Path | TextIO,
        questions: Path | TextIO,
    ) -> DataDictionary:
        """Load a data dictionary from individual CSV files."""
        warnings.warn(
            "DataDictionaryLoader is deprecated and will be removed in a future release. "
            "Use imednet.datadict.load_from_csv_files instead.",
            FutureWarning,
            stacklevel=2,
        )
        return load_from_csv_files(
            forms_file=forms,
            questions_file=questions,
            choices_file=choices,
            business_logic_file=business_logic,
        )

    @classmethod
    def from_directory(cls, directory: Path | str) -> DataDictionary:
        """Load all required CSV files from ``directory``."""
        warnings.warn(
            "DataDictionaryLoader is deprecated and will be removed in a future release. "
            "Use imednet.datadict.load_from_csv instead.",
            FutureWarning,
            stacklevel=2,
        )
        return load_from_csv(directory)

    @classmethod
    def from_zip(cls, source: Path | BinaryIO) -> DataDictionary:
        """Load a data dictionary from a ZIP archive containing the required CSVs."""
        warnings.warn(
            "DataDictionaryLoader is deprecated and will be removed in a future release. "
            "Use imednet.datadict.load_from_csv instead.",
            FutureWarning,
            stacklevel=2,
        )
        return load_from_csv(source)
