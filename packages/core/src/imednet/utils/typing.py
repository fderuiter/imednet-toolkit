"""Type definitions for imednet SDK utilities."""

from typing import Any, TypeAlias, Union

try:
    import pandas as pd

    DataFrame: TypeAlias = pd.DataFrame
except ImportError:
    DataFrame: TypeAlias = Any  # type: ignore

#: Generic JSON object type (mapping of string keys to arbitrary values).
JsonDict = dict[str, Any]

#: Accepted types for an endpoint item-ID parameter (path segment or filter value).
ItemId = Union[str, int]  # noqa: UP007

#: A single filter value accepted by endpoint ``list`` and convenience methods.
#:
#: Supported forms:
#:
#: * A scalar ``str``, ``int``, ``float``, or ``bool`` – implies equality.  # noqa: RUF003
#: * A two-element tuple ``(operator, scalar)`` – e.g. ``(">", 30)``.  # noqa: RUF003
#: * A :class:`list` of two-element ``(operator, scalar)`` tuples – generates ``AND``-joined filters.  # noqa: RUF003
#: * A :class:`list` of scalars – generates ``OR``-joined equality filters.  # noqa: RUF003
FilterScalar = Union[str, int, float, bool, None]  # noqa: UP007
FilterValue = Union[  # noqa: UP007
    FilterScalar,
    tuple[str, FilterScalar],
    list[tuple[str, FilterScalar]],
    list[FilterScalar],
]

__all__ = ["FilterScalar", "FilterValue", "ItemId", "JsonDict"]
