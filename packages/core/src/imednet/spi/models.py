# pylint: disable=duplicate-code
"""SPI for models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import imednet.models as _models

if TYPE_CHECKING:
    from imednet.models import *

__all__ = list(_models.__all__)


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily forward SPI model attribute lookups to imednet.models."""
        if name in _models.__all__:
            obj = getattr(_models, name)
            globals()[name] = obj
            return obj
        raise AttributeError(name)


def __dir__() -> list[str]:
    """Return module attributes including forwarded model exports."""
    return sorted(set(globals()) | set(__all__))
