"""Utils init module."""

from typing import Any

"""Re-exports utility functions for easier access."""

from importlib import import_module

from .dates import format_iso_datetime, parse_iso_datetime
from .filters import build_filter_string
from .json_logging import configure_json_logging
from .typing import FilterValue, ItemId, JsonDict

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "flatten": ("imednet.utils.serialization", "flatten"),
    "to_arrow_table": ("imednet.utils.arrow", "to_arrow_table"),
    "subjects_to_dataframe": ("imednet.utils.pandas", "subjects_to_dataframe"),
    "records_to_dataframe": ("imednet.utils.pandas", "records_to_dataframe"),
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
    "FilterValue",
    "ItemId",
    "JsonDict",
    "RestrictedAstEvaluator",
    "build_filter_string",
    "build_safe_path",
    "configure_json_logging",
    "evaluate_expression",
    "export_records_csv",
    "flatten",
    "format_iso_datetime",
    "parse_bool",
    "parse_datetime",
    "parse_dict_or_default",
    "parse_expression",
    "parse_int_or_default",
    "parse_iso_datetime",
    "parse_list_or_default",
    "parse_str_or_default",
    "records_to_dataframe",
    "sanitize_base_url",
    "sanitize_csv_formula",
    "subjects_to_dataframe",
    "to_arrow_table",
    "validate_partition_key",
]
