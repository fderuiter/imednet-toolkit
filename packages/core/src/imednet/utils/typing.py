"""Type definitions for imednet SDK utilities."""

import os
from typing import TYPE_CHECKING, Any, TypeAlias, Union

#: Generic JSON object type (mapping of string keys to arbitrary values).
JsonDict = dict[str, Any]

if TYPE_CHECKING:
    import pandas as pd

    DataFrame: TypeAlias = pd.DataFrame
    RecordsInput: TypeAlias = list[JsonDict] | pd.DataFrame | str | os.PathLike[str]
else:
    try:
        import pandas as _runtime_pd

        DataFrame = _runtime_pd.DataFrame
        RecordsInput = list[JsonDict] | _runtime_pd.DataFrame | str | os.PathLike[str]
    except ImportError:
        DataFrame = Any
        RecordsInput = list[JsonDict] | str | os.PathLike[str]

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

__all__ = ["FilterScalar", "FilterValue", "ItemId", "JsonDict", "RecordsInput"]
