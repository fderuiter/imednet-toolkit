"""Endpoint for managing studies in the iMedNet system."""

from typing import Any

from imednet.core.endpoint.dispatch import execute_get
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.operations.filter_get import FilterGetOperation
from imednet.models.studies import Study
from imednet.utils.typing import ItemId
from imednet.utils.url import validate_path_segment


class StudiesOperationDef:
    """Definition for Study operations."""

    PATH = ""
    MODEL = Study
    _id_param = "studyKey"
    requires_study_key: bool = False
    FILTER_ATTRIBUTES = frozenset(
        {
            "sponsorKey",
            "studyKey",
            "studyId",
            "studyName",
            "studyDescription",
            "studyType",
            "dateCreated",
            "dateModified",
            "lastUpdated",
        }
    )

    @execute_get
    def get(
        self,
        study_key: str | None = None,
        item_id: ItemId | None = None,
        *args: Any,
    ) -> FilterGetOperation[Study]:
        """Retrieve a single study by key via ``GET /api/v1/edc/studies?filter=studyKey==<key>``.

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
        if resolved_key is None:
            raise TypeError("Missing required argument: study_key")

        validated_key = validate_path_segment(resolved_key, name="study_key")
        return FilterGetOperation[Study](
            study_key=None,
            item_id=validated_key,
            filters={self._id_param: validated_key},
            validate_func=self._validate_get_result,  # type: ignore[attr-defined]
            list_sync_func=self._list_sync,  # type: ignore[attr-defined]
            list_async_func=self._list_async,  # type: ignore[attr-defined]
        )


class StudiesEndpoint(StudiesOperationDef, EdcSyncListGetEndpoint[Study]):  # type: ignore[misc]
    """Synchronous endpoint for managing Studies."""


class AsyncStudiesEndpoint(StudiesOperationDef, EdcAsyncListGetEndpoint[Study]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Studies."""
