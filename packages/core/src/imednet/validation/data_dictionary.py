"""Loading and container for iMednet Data Dictionary files (legacy shim).

This module is retained for backward compatibility. New code should use
`imednet.datadict`.
"""

from __future__ import annotations

from imednet.datadict.models import DataDictionary

__all__ = ["DataDictionary"]
