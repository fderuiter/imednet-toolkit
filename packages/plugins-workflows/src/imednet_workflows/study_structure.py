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
from imednet.spi.utils import (
    SyncToAsyncFacadeAdapter,
    call_async_facade_method,
    is_async_facade,
    run_sync_over_async,
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

    all_form_structures: list[FormStructure] = [
        FormStructure.from_form(f, variables_by_form_id.get(f.form_id, []))
        for f in forms
        if f.form_id is not None
    ]

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

    return StudyStructure(
        studyKey=study_key,
        intervals=interval_structures,
        forms=all_form_structures,
    )


async def async_get_study_structure(sdk: "AsyncImednetFacade", study_key: str) -> StudyStructure:
    """Canonical asynchronous implementation for retrieving study structural metadata."""
    try:

        async def fetch_intervals() -> list[Any]:
            """Asynchronously fetch visit intervals."""
            return list(await call_async_facade_method(sdk, "get_intervals", study_key))

        async def fetch_forms() -> list[Any]:
            """Asynchronously fetch form definitions."""
            return list(await call_async_facade_method(sdk, "get_forms", study_key))

        async def fetch_variables() -> list[Any]:
            """Asynchronously fetch variable definitions."""
            return list(await call_async_facade_method(sdk, "get_variables", study_key))

        intervals, forms, variables = await asyncio.gather(
            fetch_intervals(),
            fetch_forms(),
            fetch_variables(),
        )

        return _build_study_structure(study_key, intervals, forms, variables)

    except ImednetError:
        raise
    except Exception as e:
        raise ImednetError(
            f"Failed to retrieve or process study structure for {study_key}: {e}"
        ) from e


def get_study_structure(sdk: "ImednetFacade", study_key: str) -> StudyStructure:
    """Fetches and aggregates study structure information (intervals, forms, variables).

    Args:
        sdk: An initialized synchronous ImednetSDK instance.
        study_key: The key of the study to fetch structure for.

    Returns:
        A StudyStructure object containing nested intervals, forms, and variables.

    Raises:
        TypeError: If an asynchronous SDK is passed.
        ImednetError: If fetching any part of the structure fails.
    """
    if is_async_facade(sdk):
        raise TypeError(
            "get_study_structure() requires a synchronous ImednetSDK (ImednetFacade). "
            "With AsyncImednetSDK, await async_get_study_structure(...) instead."
        )
    adapter = SyncToAsyncFacadeAdapter(sdk)
    return run_sync_over_async(lambda: async_get_study_structure(adapter, study_key))
