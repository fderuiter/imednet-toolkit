"""Study structure module."""

from typing import Any

"""Workflow for retrieving and aggregating study structural metadata."""

import asyncio
from typing import TYPE_CHECKING, Any

# Import potential exceptions
from imednet.spi.errors import ImednetError

# Import the models we need
from imednet.spi.models import (
    Form,
    FormStructure,
    Interval,
    IntervalStructure,
    StudyStructure,
    Variable,
)

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
    forms_by_id: dict[int, Form] = {f.form_id: f for f in forms if f.form_id is not None}
    variables_by_form_id: dict[int, list[Variable]] = {}
    for var in variables:
        if var.form_id is not None:
            variables_by_form_id.setdefault(var.form_id, []).append(var)

    interval_structures: list[IntervalStructure] = []
    for interval in intervals:
        form_structures: list[FormStructure] = []
        for form_summary in getattr(interval, "forms", None) or []:
            full_form: Form | None = None
            if form_summary.form_id is not None:
                full_form = forms_by_id.get(form_summary.form_id)
            if full_form and full_form.form_id is not None:
                form_vars = variables_by_form_id.get(full_form.form_id, [])
                form_structures.append(FormStructure.from_form(full_form, form_vars))

        interval_structures.append(IntervalStructure.from_interval(interval, form_structures))

    return StudyStructure(studyKey=study_key, intervals=interval_structures)


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
