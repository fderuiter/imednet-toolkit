"""Utils init module."""

from typing import Any

"""Re-exports utility functions for easier access."""

from importlib import import_module

from .dates import (
    format_iso_datetime,
    normalize_iso_date,
    normalize_utc_timestamp_bound,
    parse_iso_datetime,
)
from .filters import (
    ALLOWED_OPERATORS,
    ORDERED_FILTER_OPERATORS,
    build_filter_string,
    parse_filter_expression,
    parse_filter_expressions,
    validate_connector_exclusivity,
    validate_filter_key,
    validate_filter_operator,
    validate_filter_syntax,
    validate_record_filter,
)
from .json_logging import configure_json_logging
from .typing import FilterValue, ItemId, JsonDict

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "flatten": ("imednet.utils.serialization", "flatten"),
    "to_arrow_table": ("imednet.utils.arrow", "to_arrow_table"),
    "check_pandas": ("imednet.utils.pandas", "check_pandas"),
    "queries_to_dataframe": ("imednet.utils.pandas", "queries_to_dataframe"),
    "subjects_to_dataframe": ("imednet.utils.pandas", "subjects_to_dataframe"),
    "forms_to_dataframe": ("imednet.utils.pandas", "forms_to_dataframe"),
    "visits_to_dataframe": ("imednet.utils.pandas", "visits_to_dataframe"),
    "intervals_to_dataframe": ("imednet.utils.pandas", "intervals_to_dataframe"),
    "sites_to_dataframe": ("imednet.utils.pandas", "sites_to_dataframe"),
    "variables_to_dataframe": ("imednet.utils.pandas", "variables_to_dataframe"),
    "records_to_dataframe": ("imednet.utils.pandas", "records_to_dataframe"),
    "users_to_dataframe": ("imednet.utils.pandas", "users_to_dataframe"),
    "export_records_csv": ("imednet.utils.pandas", "export_records_csv"),
    "parse_bool": ("imednet.utils.validators", "parse_bool"),
    "parse_datetime": ("imednet.utils.validators", "parse_datetime"),
    "parse_int_or_default": ("imednet.utils.validators", "parse_int_or_default"),
    "parse_str_or_default": ("imednet.utils.validators", "parse_str_or_default"),
    "parse_list_or_default": ("imednet.utils.validators", "parse_list_or_default"),
    "parse_dict_or_default": ("imednet.utils.validators", "parse_dict_or_default"),
    "sanitize_base_url": ("imednet.utils.url", "sanitize_base_url"),
    "build_safe_path": ("imednet.utils.url", "build_safe_path"),
    "sanitize_csv_formula": ("imednet.utils.security", "sanitize_csv_formula"),
    "validate_partition_key": ("imednet.utils.security", "validate_partition_key"),
    "RestrictedAstEvaluator": ("imednet.utils.evaluator", "RestrictedAstEvaluator"),
    "evaluate_expression": ("imednet.utils.evaluator", "evaluate_expression"),
    "parse_expression": ("imednet.utils.evaluator", "parse_expression"),
}


def __getattr__(name: str) -> Any:
    """Lazy load utility functions from their respective modules."""
    try:
        module_path, obj_name = _LAZY_ATTRS[name]
    except KeyError:
        raise AttributeError(name) from None
    mod = import_module(module_path)  # nosem
    obj = getattr(mod, obj_name)
    globals()[name] = obj
    return obj


__all__ = [
    "ALLOWED_OPERATORS",
    "ORDERED_FILTER_OPERATORS",
    "FilterValue",
    "ItemId",
    "JsonDict",
    "RestrictedAstEvaluator",
    "build_filter_string",
    "build_safe_path",
    "check_pandas",
    "configure_json_logging",
    "evaluate_expression",
    "export_records_csv",
    "flatten",
    "format_iso_datetime",
    "forms_to_dataframe",
    "intervals_to_dataframe",
    "normalize_iso_date",
    "normalize_utc_timestamp_bound",
    "parse_bool",
    "parse_datetime",
    "parse_dict_or_default",
    "parse_expression",
    "parse_filter_expression",
    "parse_filter_expressions",
    "parse_int_or_default",
    "parse_iso_datetime",
    "parse_list_or_default",
    "parse_str_or_default",
    "queries_to_dataframe",
    "records_to_dataframe",
    "sanitize_base_url",
    "sanitize_csv_formula",
    "sites_to_dataframe",
    "subjects_to_dataframe",
    "to_arrow_table",
    "users_to_dataframe",
    "validate_connector_exclusivity",
    "validate_filter_key",
    "validate_filter_operator",
    "validate_filter_syntax",
    "validate_partition_key",
    "validate_record_filter",
    "variables_to_dataframe",
    "visits_to_dataframe",
]
