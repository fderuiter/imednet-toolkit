"""Data serialization and flattening utilities."""

from typing import Any


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """Recursively flatten a nested dict/list into dot-notation keys."""
    result: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                result.update(flatten(value, full_key))
            else:
                result[full_key] = value
    elif isinstance(data, list):
        for index, item in enumerate(data):
            full_key = f"{prefix}[{index}]"
            if isinstance(item, (dict, list)):
                result.update(flatten(item, full_key))
            else:
                result[full_key] = item
    else:
        result[prefix] = data
    return result


__all__ = ["flatten"]
