"""Provides workflows for extracting specific datasets from iMednet studies."""

from collections import deque
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from imednet.sdk import ImednetSDK
from imednet.spi.models import Record, RecordRevision
from imednet.spi.sink_base import is_record_deleted
from imednet.spi.utils import normalize_utc_timestamp_bound

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


class _FilteredRecordIterator(Iterator[Record]):
    """Lazy, pagination-aware iterator over filtered :class:`Record` items."""

    def __init__(
        self,
        source: Any,
        predicate: Callable[[Record], bool],
    ) -> None:
        self._source = source
        self._predicate = predicate
        self._buffer: deque[Record] = deque()
        self._step_fn = getattr(source, "next_page", None)
        self._has_step_api = callable(self._step_fn) and isinstance(
            getattr(source, "has_more", None), bool
        )
        self._started_stepping = False
        self._iter: Iterator[Record] | None = None if self._has_step_api else iter(source)
        self._exhausted = False

    @property
    def has_more(self) -> bool:
        """Return True if buffered matching records or unfetched pages remain."""
        if self._buffer:
            return True
        if self._has_step_api:
            return bool(getattr(self._source, "has_more", False))
        return not self._exhausted

    def next_page(self) -> list[Record]:
        """Return buffered matching records first, or filter the next upstream page."""
        if self._has_step_api and callable(self._step_fn):
            if self._buffer:
                buffered = list(self._buffer)
                self._buffer.clear()
                return buffered
            self._started_stepping = True
            raw_page = self._step_fn()
            return [r for r in raw_page if self._predicate(r)]

        matched: list[Record] = []
        while self._buffer and len(matched) < 100:
            matched.append(self._buffer.popleft())

        if not self._exhausted and self._iter is not None:
            while len(matched) < 100:
                try:
                    candidate = next(self._iter)
                except StopIteration:
                    self._exhausted = True
                    break
                if self._predicate(candidate):
                    matched.append(candidate)

        if not self._exhausted and not self._buffer and self._iter is not None:
            while True:
                try:
                    probe = next(self._iter)
                except StopIteration:
                    self._exhausted = True
                    break
                if self._predicate(probe):
                    self._buffer.append(probe)
                    break
        return matched

    def __iter__(self) -> "_FilteredRecordIterator":
        return self

    def __next__(self) -> Record:
        if self._has_step_api and callable(self._step_fn):
            empty_pages = 0
            while not self._buffer:
                if self._started_stepping and not bool(getattr(self._source, "has_more", False)):
                    raise StopIteration
                self._started_stepping = True
                raw_page = self._step_fn()
                if not raw_page:
                    empty_pages += 1
                    if not bool(getattr(self._source, "has_more", False)) or empty_pages >= 10000:
                        raise StopIteration
                    continue
                empty_pages = 0
                self._buffer.extend(r for r in raw_page if self._predicate(r))
            return self._buffer.popleft()

        while not self._buffer:
            if self._exhausted or self._iter is None:
                raise StopIteration
            try:
                candidate = next(self._iter)
            except StopIteration:
                self._exhausted = True
                raise
            if self._predicate(candidate):
                return candidate
        return self._buffer.popleft()


class DataExtractionWorkflow:
    """Provides methods for complex data extraction tasks involving multiple iMednet endpoints.

    Args:
        sdk: An instance of the ImednetSDK.
    """

    def __init__(self, sdk: "ImednetFacade"):
        """Initialize the data extraction workflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
        """
        self._sdk = sdk

    def _resolve_subject_and_visit_bounds(
        self,
        study_key: str,
        subject_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None,
        visit_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None,
        on_progress: Callable[[str], None] | None,
    ) -> tuple[set[str] | None, set[int] | None, bool]:
        """Resolve subject and visit filter allowlists; return ``aborted=True`` if empty."""
        matching_subject_keys: set[str] | None = None
        if subject_filter:
            if on_progress:
                on_progress("Querying subjects matching filter criteria...")
            subjects = self._sdk.get_subjects(study_key, **subject_filter)
            matching_subject_keys = {s.subject_key for s in subjects}  # type: ignore
            if on_progress:
                on_progress(f"Retrieved {len(matching_subject_keys)} matching subjects.")
            if not matching_subject_keys:
                if on_progress:
                    on_progress("No subjects matched criteria; returning 0 records.")
                return None, None, True

        matching_visit_ids: set[int] | None = None
        if visit_filter:
            if on_progress:
                on_progress("Querying visits matching filter criteria...")
            visits = self._sdk.get_visits(study_key, **visit_filter)
            if matching_subject_keys:
                visits = [v for v in visits if v.subject_key in matching_subject_keys]
            matching_visit_ids = {v.visit_id for v in visits}  # type: ignore
            if on_progress:
                on_progress(f"Retrieved {len(matching_visit_ids)} matching visits.")
            if not matching_visit_ids:
                if on_progress:
                    on_progress("No visits matched criteria; returning 0 records.")
                return matching_subject_keys, None, True

        return matching_subject_keys, matching_visit_ids, False

    @staticmethod
    def _build_record_predicate(
        *,
        include_deleted: bool,
        matching_subject_keys: set[str] | None,
        matching_visit_ids: set[int] | None,
    ) -> Callable[[Record], bool]:
        """Create a client-side predicate for deletion, subject, and visit constraints."""

        def _matches(record: Record) -> bool:
            if not include_deleted and is_record_deleted(record):
                return False
            if (
                matching_subject_keys is not None
                and record.subject_key not in matching_subject_keys
            ):
                return False
            return not (
                matching_visit_ids is not None and record.visit_id not in matching_visit_ids
            )

        return _matches

    def iter_records_by_criteria(
        self,
        study_key: str,
        record_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        subject_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        visit_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        include_deleted: bool = False,
        **other_filters: Any,
    ) -> Iterator[Record]:
        """Lazily iterate over matching :class:`Record` items page-by-page."""
        matching_subject_keys, matching_visit_ids, aborted = self._resolve_subject_and_visit_bounds(
            study_key,
            subject_filter,
            visit_filter,
            on_progress,
        )
        if aborted:
            return _FilteredRecordIterator((), lambda _r: True)

        final_record_filter_dict = dict(record_filter) if record_filter else {}
        final_record_filter_dict.update(other_filters)

        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")

        sdk_dict = getattr(self._sdk, "__dict__", {})
        mock_children = getattr(self._sdk, "_mock_children", None)
        has_records_endpoint = (
            isinstance(self._sdk, ImednetSDK)
            or getattr(type(self._sdk), "records", None) is not None
            or "records" in sdk_dict
            or (isinstance(mock_children, dict) and "records" in mock_children)
        )
        if has_records_endpoint:
            source: Any = self._sdk.records.list(  # type: ignore[attr-defined]
                study_key,
                record_data_filter=None,
                **final_record_filter_dict,
            )
        else:
            source = self._sdk.get_records(
                study_key=study_key,
                record_data_filter=None,
                **final_record_filter_dict,
            )

        predicate = self._build_record_predicate(
            include_deleted=include_deleted,
            matching_subject_keys=matching_subject_keys,
            matching_visit_ids=matching_visit_ids,
        )
        return _FilteredRecordIterator(source, predicate)

    def extract_records_by_criteria(
        self,
        study_key: str,
        record_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        subject_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        visit_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        include_deleted: bool = False,
        **other_filters: Any,
    ) -> list[Record]:
        """Extracts records based on criteria spanning subjects, visits, and records.

        Args:
            study_key: The key identifying the study.
            record_filter: Dictionary of conditions for the records endpoint.
            subject_filter: Dictionary of conditions for the subjects endpoint.
            visit_filter: Dictionary of conditions for the visits endpoint.
            on_progress: Optional callback invoked with progress status strings.
            include_deleted: When True, include voided (deleted=True) records.
            **other_filters: Additional keyword arguments passed as filters to the
                             records endpoint `list` method.

        Returns:
            A list of Record objects matching all specified criteria.
        """
        matching_subject_keys, matching_visit_ids, aborted = self._resolve_subject_and_visit_bounds(
            study_key,
            subject_filter,
            visit_filter,
            on_progress,
        )
        if aborted:
            return []

        final_record_filter_dict = dict(record_filter) if record_filter else {}
        final_record_filter_dict.update(other_filters)

        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")
        records = self._sdk.get_records(
            study_key=study_key,
            record_data_filter=None,
            **final_record_filter_dict,
        )

        if not include_deleted:
            records = [r for r in records if not is_record_deleted(r)]

        if matching_subject_keys:
            if on_progress:
                on_progress("Filtering records by matched subject keys...")
            records = [r for r in records if r.subject_key in matching_subject_keys]
        if matching_visit_ids:
            if on_progress:
                on_progress("Filtering records by matched visit IDs...")
            records = [r for r in records if r.visit_id in matching_visit_ids]

        result_records = list(records)
        if on_progress:
            on_progress(f"Extraction complete: found {len(result_records)} matching records.")

        return result_records

    def extract_audit_trail(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **filters: Any,
    ) -> list[RecordRevision]:
        """Extracts the audit trail (record revisions) based on specified filters.

        Args:
            study_key: The key identifying the study.
            start_date: Optional inclusive lower UTC date/timestamp bound for ``dateCreated``.
            end_date: Optional inclusive upper UTC date/timestamp bound for ``dateCreated``.
            user_filter: Optional dictionary of base filter conditions.
            **filters: Additional key-value pairs to be added as equality filters.

        Returns:
            A list of RecordRevision objects matching the criteria.
        """
        final_filter_dict: dict[str, Any] = dict(user_filter) if user_filter else {}
        final_filter_dict.update(filters)

        if start_date is not None or end_date is not None:
            date_bounds: list[tuple[str, Any]] = []
            for key_variant in ("date_created", "dateCreated"):
                if key_variant in final_filter_dict:
                    existing = final_filter_dict.pop(key_variant)
                    if isinstance(existing, list):
                        for item in existing:
                            date_bounds.append(item if isinstance(item, tuple) else ("==", item))
                    elif isinstance(existing, tuple):
                        date_bounds.append(existing)
                    else:
                        date_bounds.append(("==", existing))

            if start_date is not None:
                start_utc = normalize_utc_timestamp_bound(start_date, is_end=False)
                date_bounds.append((">=", start_utc))
            if end_date is not None:
                end_utc = normalize_utc_timestamp_bound(end_date, is_end=True)
                date_bounds.append(("<=", end_utc))

            final_filter_dict["dateCreated"] = (
                date_bounds[0] if len(date_bounds) == 1 else date_bounds
            )

        revisions = self._sdk.get_record_revisions(
            study_key,
            **final_filter_dict,
        )
        return list(revisions)


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
