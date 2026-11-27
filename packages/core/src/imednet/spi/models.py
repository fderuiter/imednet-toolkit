# pylint: disable=duplicate-code
"""SPI for models."""

from __future__ import annotations

from importlib import import_module as _import_module
from typing import TYPE_CHECKING, Any

import imednet.models as _models

if TYPE_CHECKING:
    from imednet.datadict.exchange import *
    from imednet.models import *

_EXCHANGE_EXPORTS: tuple[str, ...] = (
    "DatasetJsonBundle",
    "DatasetJsonColumn",
    "DatasetJsonEnvelope",
    "DatasetJsonItem",
    "DatasetJsonItemType",
    "DatasetJsonScalar",
    "DatasetJsonSourceSystem",
    "DatasetJsonTargetDataType",
    "OdmAdminData",
    "OdmAnnotation",
    "OdmArchive",
    "OdmAuditRecord",
    "OdmClinicalData",
    "OdmFlag",
    "OdmFormData",
    "OdmItemData",
    "OdmItemGroupData",
    "OdmLocation",
    "OdmMetaDataVersionRef",
    "OdmReferenceData",
    "OdmStudyEventData",
    "OdmSubjectData",
    "OdmUser",
)

__all__ = list(_models.__all__)
__all__.extend(_EXCHANGE_EXPORTS)


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily forward SPI model attribute lookups to imednet.models and CDISC exchange."""
        if name in _models.__all__:
            obj = getattr(_models, name)
            globals()[name] = obj
            return obj
        if name in _EXCHANGE_EXPORTS:
            exchange_mod = _import_module("imednet.datadict.exchange")  # nosem
            obj = getattr(exchange_mod, name)
            globals()[name] = obj
            return obj
        raise AttributeError(name)


def __dir__() -> list[str]:
    """Return module attributes including forwarded model exports."""
    return sorted(set(globals()) | set(__all__))
