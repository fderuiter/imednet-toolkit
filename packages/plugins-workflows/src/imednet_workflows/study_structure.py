"""Workflow for retrieving and aggregating study structural metadata."""

import asyncio
from typing import TYPE_CHECKING, Any

from imednet.spi.errors import ImednetError
from imednet.spi.models import (
    Form,
    Interval,
    StudyStructure,
    Variable,
)
from imednet.spi.tabular_mapper import build_core_study_structure

# Use TYPE_CHECKING to avoid circular import at runtime
if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade


def _build_study_structure(
    study_key: str,
    intervals: list[Interval],
    forms: list[Form],
    variables: list[Variable],
) -> StudyStructure:
    """Helper to assemble the StudyStructure from its component parts."""
    return build_core_study_structure(study_key, intervals, forms, variables)


def get_study_structure(sdk: "ImednetFacade", study_key: str) -> StudyStructure:
    """Fetches and aggregates study structure information (intervals, forms, variables).

    Args:
        sdk: An initialized ImednetSDK instance.
        study_key: The key of the study to fetch structure for.

    Returns:
        A StudyStructure object containing nested intervals, forms, and variables.

    Raises:
        ImednetError: If fetching any part of the structure fails.
    """
    try:
        # Fetch all components concurrently (if async were used) or sequentially
        intervals: list[Interval] = list(sdk.get_intervals(study_key))
        forms: list[Form] = list(sdk.get_forms(study_key))
        variables: list[Variable] = list(sdk.get_variables(study_key))

        return _build_study_structure(study_key, intervals, forms, variables)

    except Exception as e:
        # Catch potential API errors or processing errors
        raise ImednetError(
            f"Failed to retrieve or process study structure for {study_key}: {e}"
        ) from e


async def async_get_study_structure(sdk: "AsyncImednetFacade", study_key: str) -> StudyStructure:
    """Asynchronous variant of :func:`get_study_structure`."""
    try:

        async def fetch_intervals() -> list[Any]:
            """Asynchronously fetch visit intervals."""
            return await sdk.async_get_intervals(study_key)

        async def fetch_forms() -> list[Any]:
            """Asynchronously fetch form definitions."""
            return await sdk.async_get_forms(study_key)

        async def fetch_variables() -> list[Any]:
            """Asynchronously fetch variable definitions."""
            return await sdk.async_get_variables(study_key)

        intervals, forms, variables = await asyncio.gather(
            fetch_intervals(),
            fetch_forms(),
            fetch_variables(),
        )

        return _build_study_structure(study_key, intervals, forms, variables)

    except Exception as e:  # pragma: no cover - unexpected
        raise ImednetError(
            f"Failed to retrieve or process study structure for {study_key}: {e}"
        ) from e
