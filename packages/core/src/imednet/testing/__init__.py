# pylint: disable=duplicate-code
"""Test utilities for the iMedNet SDK.

.. warning::

   This package provides helpers for writing tests against the SDK.
   Its interface is **unstable** and may change between minor releases.
   Do not rely on it in production code.

"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from imednet.testing.typed_values import canonical_type, value_for

if TYPE_CHECKING:
    from imednet.testing.fake_data import (
        fake_coding,
        fake_form,
        fake_forms_for_cache,
        fake_interval,
        fake_job,
        fake_query,
        fake_record,
        fake_record_revision,
        fake_site,
        fake_study,
        fake_subject,
        fake_user,
        fake_variable,
        fake_variables_for_cache,
        fake_visit,
    )

_FAKE_DATA_EXPORTS: frozenset[str] = frozenset(
    {
        "fake_coding",
        "fake_form",
        "fake_forms_for_cache",
        "fake_interval",
        "fake_job",
        "fake_query",
        "fake_record",
        "fake_record_revision",
        "fake_site",
        "fake_study",
        "fake_subject",
        "fake_user",
        "fake_variable",
        "fake_variables_for_cache",
        "fake_visit",
    }
)


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve ``fake_*`` helpers from ``imednet.testing.fake_data`` on demand."""
        if name not in _FAKE_DATA_EXPORTS:
            raise AttributeError(name)
        mod = import_module("imednet.testing.fake_data")
        obj = getattr(mod, name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy exports."""
    return sorted(set(globals()) | set(__all__))


__all__: list[str] = [
    "canonical_type",
    "fake_coding",
    "fake_form",
    "fake_forms_for_cache",
    "fake_interval",
    "fake_job",
    "fake_query",
    "fake_record",
    "fake_record_revision",
    "fake_site",
    "fake_study",
    "fake_subject",
    "fake_user",
    "fake_variable",
    "fake_variables_for_cache",
    "fake_visit",
    "value_for",
]
