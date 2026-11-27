"""Type definitions for imednet SDK utilities."""

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, Union, runtime_checkable

#: Generic JSON object type (mapping of string keys to arbitrary values).
JsonDict = dict[str, Any]


@runtime_checkable
class _TabularDataFrameLike(Protocol):
    """Runtime structural protocol for tabular DataFrame inputs without importing pandas."""

    @property
    def columns(self) -> Any:
        """Return tabular column labels."""

    def to_dict(self, *args: Any, **kwargs: Any) -> Any:
        """Convert tabular rows into Python dictionaries."""


if TYPE_CHECKING:
    import pandas as pd

    from imednet.models.records import BaseRecordRequest

    DataFrame: TypeAlias = pd.DataFrame
    RecordsInput: TypeAlias = (
        Sequence[JsonDict | BaseRecordRequest] | pd.DataFrame | str | os.PathLike[str]
    )
else:
    RecordsInput = Sequence[Any] | _TabularDataFrameLike | str | os.PathLike[str]
    globals().pop("DataFrame", None)

    def __getattr__(name: str) -> Any:
        """Lazily resolve the runtime DataFrame alias only when explicitly requested."""
        if name == "DataFrame":
            try:
                import pandas as _runtime_pd

                df_type: Any = _runtime_pd.DataFrame
            except ImportError:
                df_type = Any
            globals()["DataFrame"] = df_type
            return df_type
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
