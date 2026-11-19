# pylint: disable=duplicate-code
"""Public Service Provider Interface for tabular record mapping."""

from imednet.integrations.tabular_mapper import (
    DEFAULT_TABULAR_CHUNK_SIZE,
    BaseTabularRecordMapper,
    build_core_study_structure,
)

__all__ = [
    "DEFAULT_TABULAR_CHUNK_SIZE",
    "BaseTabularRecordMapper",
    "build_core_study_structure",
]
