"""Structural SPI protocol and section descriptors for side-panel inspection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class PanelSection:
    """Ordered side-panel section emitted by a domain or workflow detail model.

    Attributes:
        title: Uppercase-ready section heading rendered in the inspection panel.
        content: Mapping, sequence, model, or scalar payload for the section.
        empty_placeholder: Text shown when ``content`` is empty or ``None``.
        preserve_empty_values: When ``True``, render ``None`` and ``""`` mapping
            entries as ``-`` instead of omitting them (used for Form Variables).
    """

    title: str
    content: Any
    empty_placeholder: str = "None"
    preserve_empty_values: bool = False


@runtime_checkable
class PanelSectionsProtocol(Protocol):
    """Structural SPI protocol for detail objects that project ordered panel sections."""

    def to_panel_sections(self) -> Sequence[PanelSection | tuple[str, Any]]:
        """Return ordered :class:`PanelSection` or ``(title, content)`` tuples."""
        ...


__all__ = ["PanelSection", "PanelSectionsProtocol"]
