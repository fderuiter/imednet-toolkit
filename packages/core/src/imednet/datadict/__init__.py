"""iMednet Data Dictionary module.

Provides canonical modeling, 4-file CSV loading, live SDK extraction,
deterministic JSON emission, structural diffing, and schema validation.
"""

from __future__ import annotations

from .diff import DiffItem, DiffReport, diff
from .emit import sort_data_dictionary, to_file, to_json
from .loaders import load_from_api, load_from_csv, load_from_csv_files
from .logic import parse_logic_xml
from .models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)
from .normalize import to_camel_case, validate_timestamp
from .schema import get_schema, validate

__all__ = [
    "BusinessLogicRule",
    "Choice",
    "DataDictionary",
    "DiffItem",
    "DiffReport",
    "Form",
    "Metadata",
    "Variable",
    "diff",
    "get_schema",
    "load_from_api",
    "load_from_csv",
    "load_from_csv_files",
    "parse_logic_xml",
    "sort_data_dictionary",
    "to_camel_case",
    "to_file",
    "to_json",
    "validate",
    "validate_timestamp",
]
