"""Provides workflows for managing queries within iMednet studies."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

from imednet.spi.models import (
    Query,
    QueryStatus,
    TriageAnnotation,
    TriageItem,
    TriageStatus,
    calculate_legacy_status,
)
from imednet.spi.utils import normalize_iso_date, parse_iso_datetime

from .triage_store import TriageStore

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade, TriageStoreProtocol


class QueryStateCounts(dict[str, int]):
    """State counts dictionary supporting case-insensitive and enum key access."""

    def __getitem__(self, key: Any) -> int:
        """Retrieve state count supporting case-insensitive and enum keys."""
        if isinstance(key, str) and key not in self:
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
        if hasattr(key, "value") and key.value in self:
            return self[key.value]
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = 0) -> Any:  # type: ignore[override]
        """Get state count with fallback default on missing keys."""
        try:
            return self[key]
        except KeyError:
            return default


class QueryManagementWorkflow:
    """Provides methods for common query management tasks.

    Args:
        sdk: An instance of the ImednetSDK.
    """

    def __init__(self, sdk: ImednetFacade):
        """Initialize the query management workflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
        """
        self._sdk = sdk

    @staticmethod
    def _resolve_query_status(query: Any) -> QueryStatus:
        """Resolve a query's lifecycle status from explicit attribute or comments."""
        status = getattr(query, "status", None)
        if isinstance(status, QueryStatus):
            return status
        if status is not None:
            try:
                return QueryStatus(str(status).strip().upper())
            except ValueError:
                return QueryStatus.OPEN
        comments = getattr(query, "query_comments", None)
        if comments is None:
            comments = getattr(query, "queryComments", None)
        return calculate_legacy_status(comments)

    @classmethod
    def _is_query_open(cls, query: Query, *, include_answered: bool = False) -> bool:
        """Determines if a query is open (or unresolved when include_answered is True)."""
        resolved = cls._resolve_query_status(query)
        if include_answered:
            return resolved in (QueryStatus.OPEN, QueryStatus.ANSWERED)
        return resolved == QueryStatus.OPEN

    def get_open_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        include_answered: bool = False,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves open queries for a given study, optionally including answered queries.

        Note:
            This method fetches queries based on ``additional_filter`` and then
            filters for ``OPEN`` (and ``ANSWERED`` when ``include_answered=True``)
            client-side.

        Args:
            study_key: The key identifying the study.
            additional_filter: An optional dictionary of conditions to apply via the API.
            include_answered: When True, include ``ANSWERED`` queries alongside ``OPEN`` queries.
            **kwargs: Additional keyword arguments passed directly to ``sdk.queries.list``.

        Returns:
            A list of matching Query objects.
        """
        filters = dict(additional_filter) if additional_filter else {}
        all_matching_queries = self._sdk.get_queries(study_key, **filters, **kwargs)
        return [
            query
            for query in all_matching_queries
            if self._is_query_open(query, include_answered=include_answered)
        ]

    def get_unresolved_queries(
        self,
        study_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all unresolved (``OPEN`` and ``ANSWERED``) queries for a given study.

        Args:
            study_key: The key identifying the study.
            additional_filter: An optional dictionary of conditions to apply via the API.
            **kwargs: Additional keyword arguments passed directly to ``sdk.queries.list``.

        Returns:
            A list of ``OPEN`` and ``ANSWERED`` Query objects matching the criteria.
        """
        return self.get_open_queries(
            study_key,
            additional_filter=additional_filter,
            include_answered=True,
            **kwargs,
        )

    def get_queries_for_subject(
        self,
        study_key: str,
        subject_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all queries for a specific subject within a study.

        Args:
            study_key: The key identifying the study.
            subject_key: The key identifying the subject.
            additional_filter: An optional dictionary of conditions to combine
                with the subject filter.
            **kwargs: Additional keyword arguments passed directly to `sdk.queries.list`.

        Returns:
            A list of Query objects for the specified subject.
        """
        # Build filter dictionary
        final_filter_dict: dict[str, Any] = {"subject_key": subject_key}
        if additional_filter:
            final_filter_dict.update(additional_filter)

        return list(self._sdk.get_queries(study_key, **final_filter_dict, **kwargs))

    def get_queries_by_site(
        self,
        study_key: str,
        site_key: str,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        **kwargs: Any,
    ) -> list[Query]:
        """Retrieves all queries for active, keyed subjects at a specific site within a study.

        Args:
            study_key: The key identifying the study.
            site_key: The name of the site.
            additional_filter: Extra conditions to combine with the subject filter.
            **kwargs: Additional keyword arguments passed directly to `sdk.queries.list`.

        Returns:
            A list of Query objects for the specified site.
        """
        subjects = self._sdk.get_subjects(study_key, site_name=site_key)
        subject_keys = [
            key
            for s in subjects
            if getattr(s, "is_deleted", False) is not True
            and isinstance(s.subject_key, str)
            and (key := s.subject_key.strip())
        ]
        if not subject_keys:
            return []

        final_filter_dict: dict[str, Any] = {"subject_key": subject_keys}
        if additional_filter:
            final_filter_dict.update(additional_filter)

        return list(self._sdk.get_queries(study_key, **final_filter_dict, **kwargs))

    def get_query_state_counts(self, study_key: str, **kwargs: Any) -> dict[str, int]:
        """Counts queries grouped by their current QueryStatus values.

        The discrepancy lifecycle state is evaluated in linear time directly from
        each Query's status attribute, with fallback calculation for legacy payloads.

        Args:
            study_key: The key identifying the study.
            **kwargs: Additional keyword arguments passed directly to `sdk.queries.list`
                      (e.g., for initial filtering before counting).

        Returns:
            A dictionary with QueryStatus values as keys and their respective counts.
        """
        all_queries = self._sdk.get_queries(study_key, **kwargs)
        state_counts = QueryStateCounts({s.value: 0 for s in QueryStatus})

        for query in all_queries:
            status_key = self._resolve_query_status(query).value
            state_counts[status_key] += 1

        return state_counts

    @staticmethod
    def _resolve_triage_store(
        store: TriageStore | TriageStoreProtocol | str | Path | None,
    ) -> TriageStore | TriageStoreProtocol:
        """Resolve a TriageStore instance from argument or imednet.stores entry point."""
        if store is not None and not isinstance(store, (str, Path)):
            return store
        eps = list(entry_points(group="imednet.stores", name="TriageStore"))
        store_cls: Any = eps[0].load() if eps else TriageStore
        if store is None:
            return store_cls()  # type: ignore[no-any-return]
        return store_cls(store)  # type: ignore[no-any-return]

    @staticmethod
    def _parse_comment_timestamp(raw_date: Any) -> datetime:
        """Parse a query comment timestamp into a timezone-aware UTC datetime."""
        if isinstance(raw_date, datetime):
            return raw_date if raw_date.tzinfo is not None else raw_date.replace(tzinfo=UTC)
        if isinstance(raw_date, str) and raw_date.strip():
            normalized = normalize_iso_date(raw_date.strip())
            candidate = normalized if isinstance(normalized, str) else raw_date.strip()
            if len(candidate) == 10 and candidate[4:5] == "-" and candidate[7:8] == "-":
                candidate = f"{candidate}T00:00:00Z"
            try:
                dt = parse_iso_datetime(candidate)
                return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
            except ValueError:
                pass
        return datetime.now(UTC)

    @staticmethod
    def _build_triage_item_id(query: Any, study_key: str, index: int) -> str:
        """Construct a deterministic triage item identifier for a clinical query."""
        ann_id = getattr(query, "annotation_id", None)
        if ann_id is not None and str(ann_id).strip():
            return f"{study_key}:{str(ann_id).strip()}"
        subj_key = getattr(query, "subject_key", None)
        rec_id = getattr(query, "record_id", None)
        var_name = getattr(query, "variable", None)
        subj_part = (
            subj_key.strip() if isinstance(subj_key, str) and subj_key.strip() else "unkeyed"
        )
        rec_part = str(rec_id) if rec_id is not None else str(index)
        var_part = (
            var_name.strip() if isinstance(var_name, str) and var_name.strip() else f"q{index}"
        )
        return f"{study_key}/{subj_part}/{rec_part}/{var_part}"

    @classmethod
    def _query_to_triage_item(
        cls,
        query: Any,
        *,
        study_key: str,
        index: int,
        existing: TriageItem | None = None,
        default_severity: str = "medium",
    ) -> TriageItem:
        """Map a clinical Query model into a TriageItem."""
        item_id = cls._build_triage_item_id(query, study_key, index)
        query_status = cls._resolve_query_status(query)
        if query_status == QueryStatus.ANSWERED:
            target_status = TriageStatus.UNDER_REVIEW
        elif query_status in (QueryStatus.CLOSED, QueryStatus.CANCELLED):
            target_status = TriageStatus.RESOLVED
        elif existing is not None and existing.status in (
            TriageStatus.UNDER_REVIEW,
            TriageStatus.RESOLVED,
        ):
            target_status = TriageStatus.UNDER_REVIEW
        else:
            target_status = TriageStatus.NEW

        comments = getattr(query, "query_comments", None)
        if comments is None:
            comments = getattr(query, "queryComments", None)
        annotations: list[TriageAnnotation] = []
        seen_ids: set[str] = set()
        for c_idx, comment_obj in enumerate(comments or [], start=1):
            if isinstance(comment_obj, dict):
                seq = comment_obj.get("sequence")
                raw_user = comment_obj.get("user")
                raw_text = comment_obj.get("comment")
                raw_ann_status = comment_obj.get("annotation_status") or comment_obj.get(
                    "annotationStatus"
                )
                raw_date = comment_obj.get("date")
            else:
                seq = getattr(comment_obj, "sequence", None)
                raw_user = getattr(comment_obj, "user", None)
                raw_text = getattr(comment_obj, "comment", None)
                raw_ann_status = getattr(comment_obj, "annotation_status", None) or getattr(
                    comment_obj, "annotationStatus", None
                )
                raw_date = getattr(comment_obj, "date", None)

            text = (
                raw_text.strip()
                if isinstance(raw_text, str) and raw_text.strip()
                else (
                    raw_ann_status.strip()
                    if isinstance(raw_ann_status, str) and raw_ann_status.strip()
                    else ""
                )
            )
            if not text:
                continue

            base_id = f"comment-{seq}" if seq is not None else f"comment-idx-{c_idx}"
            ann_id_str = base_id if base_id not in seen_ids else f"{base_id}-{c_idx}"
            seen_ids.add(ann_id_str)

            user_id = (
                raw_user.strip() if isinstance(raw_user, str) and raw_user.strip() else "system"
            )
            annotations.append(
                TriageAnnotation(
                    annotation_id=ann_id_str,
                    user_id=user_id,
                    comment=text,
                    timestamp=cls._parse_comment_timestamp(raw_date),
                )
            )

        if not annotations:
            raw_desc = getattr(query, "description", None)
            if isinstance(raw_desc, str) and raw_desc.strip():
                annotations.append(
                    TriageAnnotation(
                        annotation_id="description",
                        user_id="system",
                        comment=raw_desc.strip(),
                        timestamp=cls._parse_comment_timestamp(
                            getattr(query, "date_created", None)
                        ),
                    )
                )

        return TriageItem(
            item_id=item_id,
            study_key=study_key,
            status=target_status,
            assignee=existing.assignee if existing is not None else None,
            severity=existing.severity if existing is not None else default_severity,
            annotations=annotations,
        )

    def sync_queries_to_triage(
        self,
        study_key: str,
        store: TriageStore | TriageStoreProtocol | str | Path | None = None,
        *,
        additional_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        unresolved_only: bool = False,
        default_severity: str = "medium",
        **kwargs: Any,
    ) -> list[TriageItem]:
        """Synchronize clinical queries for a study into a local TriageStore queue.

        Args:
            study_key: The key identifying the study.
            store: Target ``TriageStore`` instance, SQLite database path, or ``None`` to
                instantiate via the registered ``imednet.stores`` ``TriageStore`` entry point.
            additional_filter: Optional API filter conditions passed to query retrieval.
            unresolved_only: When True, sync only ``OPEN`` and ``ANSWERED`` queries.
            default_severity: Severity assigned to newly discovered triage items.
            **kwargs: Additional keyword arguments forwarded to ``sdk.get_queries``.

        Returns:
            The synchronized list of ``TriageItem`` entries queued for ``study_key``.
        """
        target_store = self._resolve_triage_store(store)
        if unresolved_only:
            queries = self.get_unresolved_queries(
                study_key,
                additional_filter=additional_filter,
                **kwargs,
            )
        else:
            filters = dict(additional_filter) if additional_filter else {}
            queries = list(self._sdk.get_queries(study_key, **filters, **kwargs))

        for idx, query in enumerate(queries, start=1):
            item_id = self._build_triage_item_id(query, study_key, idx)
            existing = target_store.get_triage_item(item_id)
            triage_item = self._query_to_triage_item(
                query,
                study_key=study_key,
                index=idx,
                existing=existing,
                default_severity=default_severity,
            )
            target_store.upsert_item(triage_item)

        return target_store.get_queue(study_key)


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.query_management.get_open_queries(...)`).
# - Provides convenient ways to access query information without manually constructing
#   complex filters.
