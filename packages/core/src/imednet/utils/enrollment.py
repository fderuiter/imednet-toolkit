"""Shared subject enrollment normalization and site eligibility helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from imednet.models.records import RegisterSubjectRequest
from imednet.models.sites import (
    ELIGIBLE_SITE_ENROLLMENT_STATUSES,
    Site,
    is_site_enrollment_eligible,
)
from imednet.models.subjects import Subject, SubjectStatus, normalize_subject_status
from imednet.utils.tabular import (
    _is_dataframe_instance,
    csv_to_records,
    dataframe_rows_to_records,
    ensure_no_masked_sentinel,
)
from imednet.utils.typing import JsonDict

if TYPE_CHECKING:
    from imednet.validation.cache import ValidationSchemaProtocol


def extract_candidate_subject_key(
    item: RegisterSubjectRequest | Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
    subject_key_field: str | None = None,
) -> str | None:
    """Extract an optional subject key or OID candidate for enrollment deduplication."""
    for attr in ("subject_key", "subjectKey", "subject_oid", "subjectOid"):
        direct = (item.get(attr) if isinstance(item, Mapping) else getattr(item, attr, None)) or (
            payload.get(attr) if payload is not None else None
        )
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

    source_map = payload if payload is not None else (item if isinstance(item, Mapping) else None)
    data = source_map.get("data") if isinstance(source_map, Mapping) else None
    if isinstance(data, Mapping):
        if subject_key_field and subject_key_field in data and data[subject_key_field] is not None:
            cand = str(data[subject_key_field]).strip()
            return cand or None
        for key in ("subjectKey", "subject_key", "subjectOid", "subject_oid", "SUBJID"):
            if key in data and data[key] is not None:
                cand = str(data[key]).strip()
                if cand:
                    return cand
    return None


def _apply_registration_defaults(
    records: list[JsonDict],
    *,
    default_form_key: str | None = None,
    default_site_name: str | None = None,
    default_site_id: int | None = None,
) -> list[JsonDict]:
    """Fill missing ``formKey`` / ``siteName`` / ``siteId`` from optional defaults."""
    if not default_form_key and not default_site_name and default_site_id is None:
        return records
    resolved: list[JsonDict] = []
    for item in records:
        copied = dict(item)
        if default_form_key and not (copied.get("formKey") or copied.get("form_key")):
            copied["formKey"] = default_form_key
        has_site_name = bool(copied.get("siteName") or copied.get("site_name"))
        has_site_id = copied.get("siteId") is not None or copied.get("site_id") is not None
        if default_site_name and not has_site_name and not has_site_id:
            copied["siteName"] = default_site_name
        elif default_site_id is not None and not has_site_name and not has_site_id:
            copied["siteId"] = default_site_id
        resolved.append(copied)
    return resolved


def normalize_subject_registration_inputs(
    subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | Any,
    *,
    default_form_key: str | None = None,
    default_site_name: str | None = None,
    default_site_id: int | None = None,
    schema: ValidationSchemaProtocol | None = None,
) -> list[tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]]:
    """Normalize registration requests from models, dicts, DataFrame, or CSV/JSON file path."""
    raw_items: Sequence[RegisterSubjectRequest | dict[str, Any]]
    if isinstance(subjects, (str, os.PathLike)):
        file_path = Path(subjects)
        if file_path.suffix.lower() == ".json":
            raw_json = json.loads(file_path.read_text(encoding="utf-8"))
            items = raw_json if isinstance(raw_json, list) else [raw_json]
            records = [dict(x) for x in items]
            ensure_no_masked_sentinel(records)
        else:
            records = csv_to_records(file_path, schema=schema)
        raw_items = _apply_registration_defaults(
            records,
            default_form_key=default_form_key,
            default_site_name=default_site_name,
            default_site_id=default_site_id,
        )
    elif _is_dataframe_instance(subjects):
        records = dataframe_rows_to_records(subjects, schema=schema)
        raw_items = _apply_registration_defaults(
            records,
            default_form_key=default_form_key,
            default_site_name=default_site_name,
            default_site_id=default_site_id,
        )
    else:
        raw_items = subjects

    pairs: list[tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]] = []
    dumped_for_check: list[JsonDict] = []
    for raw_subj in raw_items:
        normalized_raw: RegisterSubjectRequest | dict[str, Any]
        if isinstance(raw_subj, RegisterSubjectRequest):
            model_subj = raw_subj
            needs_form = bool(default_form_key and not model_subj.form_key)
            needs_site = bool(
                (default_site_name or default_site_id is not None)
                and not model_subj.site_name
                and model_subj.site_id is None
            )
            if needs_form or needs_site:
                patched = model_subj.model_dump(by_alias=True, exclude_none=True)
                if needs_form and default_form_key:
                    patched["formKey"] = default_form_key
                if needs_site:
                    if default_site_name:
                        patched["siteName"] = default_site_name
                    elif default_site_id is not None:
                        patched["siteId"] = default_site_id
                model_subj = RegisterSubjectRequest.model_validate(patched)
            normalized_raw = model_subj
        elif isinstance(raw_subj, Mapping):
            raw_dict = dict(raw_subj)
            if default_form_key and not (raw_dict.get("formKey") or raw_dict.get("form_key")):
                raw_dict["formKey"] = default_form_key
            has_site_name = bool(raw_dict.get("siteName") or raw_dict.get("site_name"))
            has_site_id = raw_dict.get("siteId") is not None or raw_dict.get("site_id") is not None
            if default_site_name and not has_site_name and not has_site_id:
                raw_dict["siteName"] = default_site_name
            elif default_site_id is not None and not has_site_name and not has_site_id:
                raw_dict["siteId"] = default_site_id
            normalized_raw = raw_dict
            model_subj = RegisterSubjectRequest.model_validate(raw_dict)
        else:
            raise TypeError(
                "Each subject registration item must be a RegisterSubjectRequest or dict; "
                f"got {type(raw_subj).__name__}."
            )
        pairs.append((normalized_raw, model_subj))
        dumped_for_check.append(model_subj.model_dump(by_alias=True, exclude_none=True))

    ensure_no_masked_sentinel(dumped_for_check)
    return pairs


def validate_subject_registration_sites(
    normalized_pairs: Sequence[
        tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]
    ],
    sites: Iterable[Site] | None,
) -> None:
    """Validate that every registration request targets an existing, enrollment-eligible site.

    When ``sites`` is ``None`` (offline dry-run), only verifies that a site
    identifier (``siteName`` or ``siteId``) is present on each request.
    """
    errors: list[str] = []
    if sites is None:
        for idx, (_raw_subj, subj) in enumerate(normalized_pairs):
            if not subj.site_name and subj.site_id is None:
                errors.append(f"Index {idx}: siteName is required")
        if errors:
            raise ValueError("; ".join(errors))
        return

    materialized_sites = list(sites)
    sites_by_name = {s.site_name: s for s in materialized_sites if s.site_name}
    sites_by_id = {s.site_id: s for s in materialized_sites if s.site_id is not None}
    for idx, (_raw_subj, subj) in enumerate(normalized_pairs):
        if subj.site_name:
            if subj.site_name not in sites_by_name:
                errors.append(f"Index {idx}: site '{subj.site_name}' not found")
            else:
                site_obj = sites_by_name[subj.site_name]
                site_status = getattr(site_obj, "site_enrollment_status", None)
                if not is_site_enrollment_eligible(site_status):
                    errors.append(
                        f"Index {idx}: site '{subj.site_name}' is not eligible for enrollment "
                        f"(status: {site_status!s})"
                    )
        elif subj.site_id is not None:
            if subj.site_id not in sites_by_id:
                errors.append(f"Index {idx}: siteId {subj.site_id} not found")
            else:
                site_obj = sites_by_id[subj.site_id]
                site_status = getattr(site_obj, "site_enrollment_status", None)
                if not is_site_enrollment_eligible(site_status):
                    errors.append(
                        f"Index {idx}: siteId {subj.site_id} is not eligible for enrollment "
                        f"(status: {site_status!s})"
                    )
        else:
            errors.append(f"Index {idx}: siteName is required")
    if errors:
        raise ValueError("; ".join(errors))


def _collect_enrolled_subject_keys(existing_subjects: Iterable[Subject]) -> set[str]:
    """Collect active (non-deleted) subject keys and OIDs."""
    enrolled_keys: set[str] = set()
    for existing in existing_subjects:
        if getattr(existing, "deleted", None) is True:
            continue
        raw_status = getattr(existing, "subject_status", None)
        if isinstance(raw_status, (SubjectStatus, str)) and (
            normalize_subject_status(raw_status) == SubjectStatus.DELETED
        ):
            continue
        for attr in ("subject_key", "subjectKey", "subject_oid", "subjectOid"):
            val = getattr(existing, attr, None)
            if isinstance(val, str) and val.strip():
                enrolled_keys.add(val.strip())
    return enrolled_keys


def filter_existing_subject_registrations(
    normalized_pairs: Sequence[
        tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]
    ],
    existing_subjects: Iterable[Subject] | None = None,
    *,
    should_filter: bool = False,
    subject_key_field: str | None = None,
) -> tuple[list[JsonDict], int]:
    """Dump normalized subject requests and optionally filter already-enrolled/duplicate keys."""
    enrolled_keys = (
        _collect_enrolled_subject_keys(existing_subjects)
        if should_filter and existing_subjects is not None
        else set()
    )
    subjects_data: list[JsonDict] = []
    skipped_count = 0
    seen_in_batch: set[str] = set()
    for raw_subj, subj in normalized_pairs:
        dumped = subj.model_dump(by_alias=True, exclude_none=True)
        if should_filter:
            cand_id = extract_candidate_subject_key(
                raw_subj, payload=dumped, subject_key_field=subject_key_field
            )
            if cand_id and (cand_id in enrolled_keys or cand_id in seen_in_batch):
                skipped_count += 1
                continue
            if cand_id:
                seen_in_batch.add(cand_id)
        subjects_data.append(dumped)
    return subjects_data, skipped_count


__all__ = [
    "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
    "extract_candidate_subject_key",
    "filter_existing_subject_registrations",
    "is_site_enrollment_eligible",
    "normalize_subject_registration_inputs",
    "validate_subject_registration_sites",
]
