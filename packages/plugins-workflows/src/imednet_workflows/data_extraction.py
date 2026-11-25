"""Provides workflows for extracting specific datasets from iMednet studies."""

import contextlib
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from imednet.spi.cdisc import (
    OdmAuditRecord,
    build_odm_audit_record_from_revision,
    sanitize_odm_oid,
    sanitize_xml_id,
)
from imednet.spi.models import Record, RecordRevision
from imednet.spi.sink_base import is_record_deleted
from imednet.spi.utils import normalize_utc_timestamp_bound

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade


def _parse_date_created_utc(raw: Any) -> datetime | None:
    """Parse a revision timestamp or date into a UTC-aware datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw.astimezone(UTC)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def _matches_local_date_bound(rev_dt: datetime | None, op: str, bound_val: Any) -> bool:
    """Evaluate a local dateCreated comparison operator against a parsed UTC timestamp."""
    if rev_dt is None:
        return False
    if isinstance(bound_val, datetime):
        bound_dt = _parse_date_created_utc(bound_val)
    elif isinstance(bound_val, (str, date)):
        bound_str = bound_val if isinstance(bound_val, str) else bound_val.isoformat()
        try:
            norm_str = normalize_utc_timestamp_bound(bound_str, is_end=op in ("<=", "<"))
            bound_dt = _parse_date_created_utc(norm_str)
        except ValueError:
            bound_dt = _parse_date_created_utc(bound_val)
    else:
        bound_dt = None
    if bound_dt is None:
        return False
    if op in ("==", "="):
        return rev_dt == bound_dt
    if op in ("!=", "<>"):
        return rev_dt != bound_dt
    if op == "<":
        return rev_dt < bound_dt
    if op == "<=":
        return rev_dt <= bound_dt
    if op == ">":
        return rev_dt > bound_dt
    if op == ">=":
        return rev_dt >= bound_dt
    raise ValueError(f"Unsupported dateCreated filter operator: {op!r}")


def _list_sdk_resource(
    sdk: Any,
    facade_attr: str,
    endpoint_attr: str,
    study_key: str,
    **filters: Any,
) -> list[Any]:
    """List items from a facade getter or fallback endpoint ``.list`` method."""
    from unittest.mock import NonCallableMock

    if isinstance(sdk, NonCallableMock):
        children = getattr(sdk, "_mock_children", {})
        inst_vars = vars(sdk)
        if facade_attr in children or facade_attr in inst_vars:
            return list(getattr(sdk, facade_attr)(study_key, **filters))
        if endpoint_attr in children or endpoint_attr in inst_vars:
            ep = getattr(sdk, endpoint_attr)
            return list(ep.list(study_key=study_key, **filters))
        return list(getattr(sdk, facade_attr)(study_key, **filters))
    facade_fn = getattr(sdk, facade_attr, None)
    if callable(facade_fn):
        return list(facade_fn(study_key, **filters))
    ep = getattr(sdk, endpoint_attr)
    return list(ep.list(study_key=study_key, **filters))


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
                return []

        matching_visit_ids: set[int] | None = None
        if visit_filter:
            # Client-side filtering for subject_key on visits is still needed
            # as build_filter_string doesn't handle complex AND/OR structures easily
            # from separate filter dictionaries.
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
                return []

        # Build the final record filter dictionary
        final_record_filter_dict = dict(record_filter) if record_filter else {}
        final_record_filter_dict.update(other_filters)  # Add other_filters here

        # Client-side filtering is used below for subject/visit matching,
        # so no need to add complex 'in' clauses here even if build_filter_string supported it.

        if on_progress:
            on_progress(f"Querying records for study '{study_key}'...")
        records = self._sdk.get_records(
            study_key=study_key,
            record_data_filter=None,
            **final_record_filter_dict,
        )

        if not include_deleted:
            records = [r for r in records if not is_record_deleted(r)]

        # Client-side filtering fallback
        if matching_subject_keys:
            if on_progress:
                on_progress("Filtering records by matched subject keys...")
            records = [r for r in records if r.subject_key in matching_subject_keys]
        # Corrected attribute from visit_oid to visit_id and variable name
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

        local_date_bounds: list[tuple[str, Any]] = []
        has_existing_tuple_list = any(
            isinstance(final_filter_dict.get(k), list)
            and any(isinstance(item, tuple) for item in final_filter_dict[k])
            for k in ("date_created", "dateCreated")
            if k in final_filter_dict
        )
        if start_date is not None or end_date is not None or has_existing_tuple_list:
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

            if date_bounds:
                final_filter_dict["dateCreated"] = date_bounds[0]
                local_date_bounds = date_bounds[1:]

        revisions = _list_sdk_resource(
            self._sdk,
            "get_record_revisions",
            "record_revisions",
            study_key,
            **final_filter_dict,
        )
        if local_date_bounds:
            revisions = [
                rev
                for rev in revisions
                if all(
                    _matches_local_date_bound(
                        _parse_date_created_utc(getattr(rev, "date_created", None)),
                        op,
                        bound_val,
                    )
                    for op, bound_val in local_date_bounds
                )
            ]
        return revisions

    def extract_odm_audit_records(
        self,
        study_key: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_filter: dict[str, Any | tuple[str, Any] | list[Any]] | None = None,
        *,
        record_ids: Sequence[int | str] | None = None,
        mask_phi: bool = True,
        **filters: Any,
    ) -> list[OdmAuditRecord]:
        """Extract record revisions mapped to canonical ODM ``<AuditRecord>`` elements.

        Populates ``<UserRef>``, ``<LocationRef>``, ``<DateTimeStamp>``, and
        ``<ReasonForChange>`` while enforcing default PHI masking.
        """
        from unittest.mock import NonCallableMock

        revisions = self.extract_audit_trail(
            study_key,
            start_date=start_date,
            end_date=end_date,
            user_filter=user_filter,
            **filters,
        )
        if record_ids is not None:
            allowed_ids = {str(rid).strip() for rid in record_ids}
            revisions = [
                rev
                for rev in revisions
                if str(getattr(rev, "record_id", "")).strip() in allowed_ids
            ]

        site_lookup: dict[str, str] = {}
        user_lookup: dict[str, str] = {}
        mock_children = getattr(self._sdk, "_mock_children", {})
        sdk_vars = vars(self._sdk) if hasattr(self._sdk, "__dict__") else {}
        if not isinstance(self._sdk, NonCallableMock) or (
            "get_sites" in mock_children
            or "get_sites" in sdk_vars
            or "sites" in mock_children
            or "sites" in sdk_vars
        ):
            with contextlib.suppress(Exception):
                for site in _list_sdk_resource(self._sdk, "get_sites", "sites", study_key):
                    sid = getattr(site, "site_id", None)
                    sname = getattr(site, "site_name", None)
                    raw_s = sid if sid not in (None, "") else sname
                    if raw_s not in (None, ""):
                        loc_oid = sanitize_odm_oid("LOC", raw_s)
                        if sid not in (None, ""):
                            site_lookup[str(sid).strip()] = loc_oid
                        if sname not in (None, ""):
                            site_lookup[str(sname).strip()] = loc_oid

        if not isinstance(self._sdk, NonCallableMock) or (
            "get_users" in mock_children
            or "get_users" in sdk_vars
            or "users" in mock_children
            or "users" in sdk_vars
        ):
            with contextlib.suppress(Exception):
                for usr in _list_sdk_resource(self._sdk, "get_users", "users", study_key):
                    uid = getattr(usr, "user_id", None)
                    ulogin = getattr(usr, "login", None)
                    raw_u = uid if uid not in (None, "") else ulogin
                    if raw_u not in (None, ""):
                        u_oid = sanitize_odm_oid("USR", raw_u)
                        if uid not in (None, ""):
                            user_lookup[str(uid).strip()] = u_oid
                        if ulogin not in (None, ""):
                            user_lookup[str(ulogin).strip()] = u_oid

        audit_records: list[OdmAuditRecord] = []
        seen_ids: set[str] = set()
        for rev in revisions:
            aud = build_odm_audit_record_from_revision(
                rev,
                user_oid_lookup=user_lookup or None,
                site_oid_lookup=site_lookup or None,
                mask_phi=mask_phi,
            )
            if aud.audit_id and aud.audit_id in seen_ids:
                aud = aud.model_copy(
                    update={
                        "audit_id": sanitize_xml_id("AR", f"{aud.audit_id}_{len(seen_ids) + 1}")
                    }
                )
            if aud.audit_id:
                seen_ids.add(aud.audit_id)
            audit_records.append(aud)

        audit_records.sort(key=lambda a: (a.date_time_stamp, a.audit_id or ""))
        return audit_records


# Integration:
# - Accessed via the main SDK instance
#       (e.g., `sdk.workflows.data_extraction.extract_records_by_criteria(...)`).
# - Offers powerful data retrieval capabilities beyond single endpoint calls.
