"""iMednet Data Dictionary module.

Provides canonical modeling, 4-file CSV loading, live SDK extraction,
deterministic JSON emission, structural diffing, and schema validation.
"""

from __future__ import annotations

from .diff import DiffItem, DiffReport, diff
from .emit import sort_data_dictionary, to_file, to_json, to_odm_file, to_odm_xml
from .loaders import (
    load_from_api,
    load_from_cdisc_odm,
    load_from_csv,
    load_from_csv_files,
    load_from_redcap,
)
from .logic import (
    ExtractedCondition,
    ExtractedRuleMetadata,
    LogicEvaluationReport,
    LogicTreeEvaluator,
    RuleEvaluationResult,
    VariableConstraints,
    extract_variable_constraints,
    parse_logic_xml,
)
from .models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)
from .normalize import (
    normalize_form_id,
    normalize_form_type,
    normalize_measurement_units,
    normalize_variable_bounds,
    normalize_variable_type,
    to_camel_case,
    validate_timestamp,
)
from .schema import get_schema, validate

__all__ = [
    "BusinessLogicRule",
    "Choice",
    "DataDictionary",
    "DiffItem",
    "DiffReport",
    "ExtractedCondition",
    "ExtractedRuleMetadata",
    "Form",
    "LogicEvaluationReport",
    "LogicTreeEvaluator",
    "Metadata",
    "RuleEvaluationResult",
    "Variable",
    "VariableConstraints",
    "diff",
    "extract_variable_constraints",
    "get_schema",
    "load_from_api",
    "load_from_cdisc_odm",
    "load_from_csv",
    "load_from_csv_files",
    "load_from_redcap",
    "normalize_form_id",
    "normalize_form_type",
    "normalize_measurement_units",
    "normalize_variable_bounds",
    "normalize_variable_type",
    "parse_logic_xml",
    "sort_data_dictionary",
    "to_camel_case",
    "to_file",
    "to_json",
    "to_odm_file",
    "to_odm_xml",
    "validate",
    "validate_timestamp",
]
