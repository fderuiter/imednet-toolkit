"""Endpoint for managing studies in the iMedNet system."""

from typing import Any

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.operations.get import PathGetOperation
from imednet.models.studies import Study
from imednet.utils.typing import ItemId


class StudiesOperationDef:
    """Definition for Study operations."""

    PATH = ""
    MODEL = Study
    _id_param = "studyKey"
    requires_study_key: bool = False

    @execute_operation  # type: ignore
    def get(
        self,
        study_key: str | None = None,
        item_id: ItemId | None = None,
        *args: Any,
    ) -> PathGetOperation[Study]:
        """Retrieve a single study by key via direct path.

        Supports both the preferred single-argument signature ``get("STUDY")``
        and the legacy two-argument signature ``get(None, "STUDY")``.

        Args:
            study_key: The study key (or None if passed in legacy format).
            item_id: The study key when passed as the second argument in legacy format.
            *args: Additional positional arguments (disallowed).

        Returns:
            The retrieved study model.

        Raises:
            TypeError: If no study key is provided or too many arguments are given.
        """
        if args:
            raise TypeError(f"get() takes at most 2 positional arguments ({2 + len(args)} given)")

        resolved_key = item_id if item_id is not None else study_key
        if not resolved_key:
            raise TypeError("Missing required argument: study_key")

        path = self._get_endpoint_path(None, resolved_key)  # type: ignore[attr-defined]
        return PathGetOperation[Study](
            path=path,
            parse_func=self._parse_item,  # type: ignore[attr-defined]
            not_found_func=lambda: self._raise_not_found(None, resolved_key),  # type: ignore[attr-defined]
        )


class StudiesEndpoint(StudiesOperationDef, EdcSyncListGetEndpoint[Study]):  # type: ignore[misc]
    """Synchronous endpoint for managing Studies."""


class AsyncStudiesEndpoint(StudiesOperationDef, EdcAsyncListGetEndpoint[Study]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Studies."""
