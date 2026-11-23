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

if TYPE_CHECKING:
    from .fake_data import (
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
    from .typed_values import canonical_type, value_for

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "canonical_type": ("imednet.testing.typed_values", "canonical_type"),
    "fake_coding": ("imednet.testing.fake_data", "fake_coding"),
    "fake_form": ("imednet.testing.fake_data", "fake_form"),
    "fake_forms_for_cache": ("imednet.testing.fake_data", "fake_forms_for_cache"),
    "fake_interval": ("imednet.testing.fake_data", "fake_interval"),
    "fake_job": ("imednet.testing.fake_data", "fake_job"),
    "fake_query": ("imednet.testing.fake_data", "fake_query"),
    "fake_record": ("imednet.testing.fake_data", "fake_record"),
    "fake_record_revision": ("imednet.testing.fake_data", "fake_record_revision"),
    "fake_site": ("imednet.testing.fake_data", "fake_site"),
    "fake_study": ("imednet.testing.fake_data", "fake_study"),
    "fake_subject": ("imednet.testing.fake_data", "fake_subject"),
    "fake_user": ("imednet.testing.fake_data", "fake_user"),
    "fake_variable": ("imednet.testing.fake_data", "fake_variable"),
    "fake_variables_for_cache": ("imednet.testing.fake_data", "fake_variables_for_cache"),
    "fake_visit": ("imednet.testing.fake_data", "fake_visit"),
    "value_for": ("imednet.testing.typed_values", "value_for"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve test utility helpers on demand."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy test helpers."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
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
