"""Shared subject enrollment normalization and site eligibility helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

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
    from imednet.validation.cache import (
        BatchValidationReport,
        RecordValidationIssue,
        ValidationSchemaProtocol,
    )


def _make_enrollment_issue(
    *,
    record_index: int,
    form_key: str | None,
    variable_name: str | None,
    message: str,
    rule_id: str | None = None,
) -> RecordValidationIssue:
    from imednet.validation.cache import RecordValidationIssue

    return RecordValidationIssue(
        record_index=record_index,
        form_key=form_key,
        variable_name=variable_name,
        message=message,
        rule_id=rule_id,
    )


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


def _has_form_identifier(mapping: Mapping[str, Any]) -> bool:
    return bool(
        mapping.get("formKey")
        or mapping.get("form_key")
        or mapping.get("formId") is not None
        or mapping.get("form_id") is not None
    )


def _has_site_identifier(mapping: Mapping[str, Any]) -> bool:
    return bool(
        mapping.get("siteName")
        or mapping.get("site_name")
        or mapping.get("siteId") is not None
        or mapping.get("site_id") is not None
    )


def _apply_registration_defaults(
    records: list[JsonDict],
    *,
    default_form_key: str | None = None,
    default_site_name: str | None = None,
) -> list[JsonDict]:
    """Fill missing ``formKey`` / ``siteName`` from optional defaults."""
    if not default_form_key and not default_site_name:
        return records
    resolved: list[JsonDict] = []
    for item in records:
        copied = dict(item)
        if default_form_key and not _has_form_identifier(copied):
            copied["formKey"] = default_form_key
        if default_site_name and not _has_site_identifier(copied):
            copied["siteName"] = default_site_name
        resolved.append(copied)
    return resolved


_INT_FIELD_KEY_ALIASES: dict[str, tuple[str, str]] = {
    "form_id": ("formId", "form_id"),
    "formId": ("formId", "form_id"),
    "site_id": ("siteId", "site_id"),
    "siteId": ("siteId", "site_id"),
    "subject_id": ("subjectId", "subject_id"),
    "subjectId": ("subjectId", "subject_id"),
}


def _validate_request_dict(
    raw_dict: dict[str, Any],
    *,
    record_index: int,
    default_form_key: str | None,
    collect_errors: bool,
    local_issues: list[RecordValidationIssue],
) -> RegisterSubjectRequest:
    """Validate ``raw_dict`` into :class:`RegisterSubjectRequest`, collecting identifier errors if requested."""
    try:
        return RegisterSubjectRequest.model_validate(raw_dict)
    except PydanticValidationError as exc:
        if not collect_errors:
            raise
        hint_fk_raw = raw_dict.get("formKey") or raw_dict.get("form_key") or default_form_key
        hint_fk = str(hint_fk_raw) if hint_fk_raw else None
        sanitized = dict(raw_dict)
        for err in exc.errors():
            loc = err.get("loc", ())
            raw_field = str(loc[0]) if loc else None
            var_name = raw_field
            if raw_field in _INT_FIELD_KEY_ALIASES:
                camel_k, snake_k = _INT_FIELD_KEY_ALIASES[raw_field]
                var_name = camel_k if camel_k in raw_dict else snake_k
                sanitized.pop(camel_k, None)
                sanitized.pop(snake_k, None)
            elif raw_field is not None:
                sanitized.pop(raw_field, None)
            local_issues.append(
                _make_enrollment_issue(
                    record_index=record_index,
                    form_key=hint_fk,
                    variable_name=var_name,
                    message=f"Index {record_index}: {err.get('msg', str(exc))}",
                    rule_id="invalid_identifier",
                )
            )
        if default_form_key and not _has_form_identifier(sanitized):
            sanitized["formKey"] = default_form_key
        return RegisterSubjectRequest.model_validate(sanitized)


def _load_json_subject_file(
    file_path: Path,
    *,
    default_form_key: str | None,
    collect_errors: bool,
    local_issues: list[RecordValidationIssue],
) -> list[JsonDict] | None:
    """Parse a JSON file of subject registration dicts, recording ``malformed_json`` when collecting errors."""
    raw_text = file_path.read_text(encoding="utf-8")
    try:
        raw_json = json.loads(raw_text)
    except ValueError as exc:
        msg = f"Invalid JSON in {file_path}: {exc}"
        if not collect_errors:
            raise ValueError(msg) from exc
        local_issues.append(
            _make_enrollment_issue(
                record_index=0,
                form_key=default_form_key,
                variable_name=None,
                message=msg,
                rule_id="malformed_json",
            )
        )
        return None

    if isinstance(raw_json, Mapping):
        return [dict(raw_json)]
    if isinstance(raw_json, list) and all(isinstance(x, Mapping) for x in raw_json):
        return [dict(x) for x in raw_json]

    msg = f"JSON payload in {file_path} must be a JSON object or array of objects."
    if not collect_errors:
        raise ValueError(msg)
    local_issues.append(
        _make_enrollment_issue(
            record_index=0,
            form_key=default_form_key,
            variable_name=None,
            message=msg,
            rule_id="malformed_json",
        )
    )
    return None


def normalize_subject_registration_inputs(
    subjects: Sequence[RegisterSubjectRequest | dict[str, Any]] | Any,
    *,
    default_form_key: str | None = None,
    default_site_name: str | None = None,
    schema: ValidationSchemaProtocol | None = None,
    study_key: str | None = None,
    collect_errors: bool = False,
    issues: list[RecordValidationIssue] | None = None,
) -> list[tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]]:
    """Normalize registration requests from models, dicts, DataFrame, or CSV/JSON file path."""
    local_issues: list[RecordValidationIssue] = []
    invalid_site_rows: set[int] = set()
    raw_items: Sequence[RegisterSubjectRequest | dict[str, Any]]
    if isinstance(subjects, (str, os.PathLike)):
        file_path = Path(subjects)
        if file_path.suffix.lower() == ".json":
            loaded_records = _load_json_subject_file(
                file_path,
                default_form_key=default_form_key,
                collect_errors=collect_errors,
                local_issues=local_issues,
            )
            if loaded_records is None:
                if issues is not None:
                    issues.extend(local_issues)
                return []
            records = loaded_records
        else:
            records = csv_to_records(
                file_path,
                schema=schema,
                study_key=study_key,
                default_form_key=default_form_key,
                collect_errors=collect_errors,
                issues=local_issues,
            )
        invalid_site_rows = {
            iss.record_index
            for iss in local_issues
            if iss.rule_id == "invalid_identifier" and iss.variable_name in {"siteId", "site_id"}
        }
        raw_items = [
            (
                dict(rec)
                if idx in invalid_site_rows
                else _apply_registration_defaults(
                    [rec],
                    default_form_key=default_form_key,
                    default_site_name=default_site_name,
                )[0]
            )
            for idx, rec in enumerate(records)
        ]
    elif _is_dataframe_instance(subjects):
        records = dataframe_rows_to_records(
            subjects,
            schema=schema,
            study_key=study_key,
            default_form_key=default_form_key,
            collect_errors=collect_errors,
            issues=local_issues,
        )
        invalid_site_rows = {
            iss.record_index
            for iss in local_issues
            if iss.rule_id == "invalid_identifier" and iss.variable_name in {"siteId", "site_id"}
        }
        raw_items = [
            (
                dict(rec)
                if idx in invalid_site_rows
                else _apply_registration_defaults(
                    [rec],
                    default_form_key=default_form_key,
                    default_site_name=default_site_name,
                )[0]
            )
            for idx, rec in enumerate(records)
        ]
    else:
        raw_items = subjects

    pairs: list[tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]] = []
    dumped_for_check: list[JsonDict] = []
    for idx, raw_subj in enumerate(raw_items):
        normalized_raw: RegisterSubjectRequest | dict[str, Any]
        if isinstance(raw_subj, RegisterSubjectRequest):
            model_subj = raw_subj
            needs_form = default_form_key and not model_subj.form_key and model_subj.form_id is None
            needs_site = (
                default_site_name and not model_subj.site_name and model_subj.site_id is None
            )
            if needs_form or needs_site:
                patched = model_subj.model_dump(by_alias=True)
                if needs_form and default_form_key:
                    patched["formKey"] = default_form_key
                if needs_site and default_site_name:
                    patched["siteName"] = default_site_name
                model_subj = RegisterSubjectRequest.model_validate(patched)
            normalized_raw = model_subj
        elif isinstance(raw_subj, Mapping):
            raw_dict = dict(raw_subj)
            if default_form_key and not _has_form_identifier(raw_dict):
                raw_dict["formKey"] = default_form_key
            if (
                default_site_name
                and not _has_site_identifier(raw_dict)
                and idx not in invalid_site_rows
            ):
                raw_dict["siteName"] = default_site_name
            normalized_raw = raw_dict
            model_subj = _validate_request_dict(
                raw_dict,
                record_index=idx,
                default_form_key=default_form_key,
                collect_errors=collect_errors,
                local_issues=local_issues,
            )
        else:
            raise TypeError(
                "Each subject registration item must be a RegisterSubjectRequest or dict; "
                f"got {type(raw_subj).__name__}."
            )
        pairs.append((normalized_raw, model_subj))
        dumped_for_check.append(model_subj.model_dump(by_alias=True))

    sentinel_issues = ensure_no_masked_sentinel(dumped_for_check, collect_errors=collect_errors)
    if issues is not None:
        seen_masked = {
            (iss.record_index, iss.variable_name)
            for iss in local_issues
            if iss.rule_id == "masked_phi"
        }
        issues.extend(local_issues)
        for s_iss in sentinel_issues or ():
            if (s_iss.record_index, s_iss.variable_name) not in seen_masked:
                issues.append(s_iss)
    return pairs


def _extract_site_check_fields(
    entry: tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]
    | RegisterSubjectRequest
    | Mapping[str, Any],
) -> tuple[str | None, str, Any]:
    """Return ``(form_key, site_name, site_id)`` from a pair, model, or record dict."""
    target = entry[1] if isinstance(entry, tuple) else entry
    if isinstance(target, RegisterSubjectRequest):
        return target.form_key or None, target.site_name or "", target.site_id
    if isinstance(target, Mapping):
        raw_fk = target.get("formKey") or target.get("form_key")
        fk = str(raw_fk) if raw_fk else None
        raw_sname = target.get("siteName") or target.get("site_name")
        sname = str(raw_sname).strip() if raw_sname is not None else ""
        raw_sid = (
            target.get("siteId") if target.get("siteId") is not None else target.get("site_id")
        )
        return fk, sname, raw_sid
    return None, "", None


def validate_subject_registration_sites(
    normalized_pairs: Sequence[
        tuple[RegisterSubjectRequest | dict[str, Any], RegisterSubjectRequest]
        | RegisterSubjectRequest
        | Mapping[str, Any]
    ],
    sites: Iterable[Site],
    *,
    collect_errors: bool = False,
    skip_indices: Collection[int] = (),
) -> BatchValidationReport | None:
    """Validate that every registration request targets an existing, enrollment-eligible site."""
    site_list = list(sites)
    sites_by_name = {s.site_name: s for s in site_list if s.site_name}
    sites_by_id = {s.site_id: s for s in site_list if getattr(s, "site_id", None) is not None}
    skipped = set(skip_indices)
    errors: list[str] = []
    site_issues: list[RecordValidationIssue] = []

    def _record_site_error(
        idx: int,
        form_key: str | None,
        variable_name: str,
        msg: str,
        rule_id: str,
    ) -> None:
        errors.append(msg)
        site_issues.append(
            _make_enrollment_issue(
                record_index=idx,
                form_key=form_key,
                variable_name=variable_name,
                message=msg,
                rule_id=rule_id,
            )
        )

    for idx, entry in enumerate(normalized_pairs):
        if idx in skipped:
            continue
        form_key, site_name, site_id = _extract_site_check_fields(entry)
        if site_name:
            if site_name not in sites_by_name:
                _record_site_error(
                    idx,
                    form_key,
                    "siteName",
                    f"Index {idx}: site '{site_name}' not found",
                    "unknown_site",
                )
            else:
                site_obj = sites_by_name[site_name]
                site_status = getattr(site_obj, "site_enrollment_status", None)
                if not is_site_enrollment_eligible(site_status):
                    _record_site_error(
                        idx,
                        form_key,
                        "siteName",
                        f"Index {idx}: site '{site_name}' is not eligible for enrollment "
                        f"(status: {site_status!s})",
                        "ineligible_site",
                    )
        elif site_id is not None and site_id != "":
            if isinstance(site_id, bool) or not isinstance(site_id, int):
                _record_site_error(
                    idx,
                    form_key,
                    "siteId",
                    f"Index {idx}: Identifier 'siteId' must be an integer.",
                    "invalid_identifier",
                )
            elif site_id not in sites_by_id:
                _record_site_error(
                    idx,
                    form_key,
                    "siteId",
                    f"Index {idx}: siteId {site_id} not found",
                    "unknown_site",
                )
            else:
                site_obj = sites_by_id[site_id]
                site_status = getattr(site_obj, "site_enrollment_status", None)
                if not is_site_enrollment_eligible(site_status):
                    _record_site_error(
                        idx,
                        form_key,
                        "siteId",
                        f"Index {idx}: siteId {site_id} is not eligible for enrollment "
                        f"(status: {site_status!s})",
                        "ineligible_site",
                    )
        else:
            _record_site_error(
                idx,
                form_key,
                "siteName",
                f"Index {idx}: siteName is required",
                "missing_site",
            )

    if not collect_errors:
        if errors:
            raise ValueError("; ".join(errors))
        return None

    from imednet.validation.cache import BatchValidationReport

    total = len(normalized_pairs)
    invalid_count = len({iss.record_index for iss in site_issues})
    return BatchValidationReport(
        total_records=total,
        valid_records=max(0, total - invalid_count),
        invalid_records=invalid_count,
        errors=site_issues,
    )


def build_site_validation_hook(
    sites: Iterable[Site],
    *,
    skip_indices: Collection[int] = (),
) -> Callable[..., BatchValidationReport | None]:
    """Build a coordinator ``validate_batch_fn`` hook that validates enrollment sites."""
    cached_sites = list(sites)
    skipped = set(skip_indices)

    def _validate_sites_batch(
        _study_key: str,
        records_data: list[dict[str, Any]],
        *,
        collect_errors: bool = False,
    ) -> BatchValidationReport | None:
        return validate_subject_registration_sites(
            records_data,
            cached_sites,
            collect_errors=collect_errors,
            skip_indices=skipped,
        )

    return _validate_sites_batch


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
    issues: list[RecordValidationIssue] | None = None,
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
    old_to_new: dict[int, int] = {}
    for old_idx, (raw_subj, subj) in enumerate(normalized_pairs):
        dumped = subj.model_dump(by_alias=True)
        if should_filter:
            cand_id = extract_candidate_subject_key(
                raw_subj, payload=dumped, subject_key_field=subject_key_field
            )
            if cand_id and (cand_id in enrolled_keys or cand_id in seen_in_batch):
                skipped_count += 1
                continue
            if cand_id:
                seen_in_batch.add(cand_id)
        old_to_new[old_idx] = len(subjects_data)
        subjects_data.append(dumped)

    if issues is not None and should_filter and skipped_count:
        from imednet.validation.cache import RecordValidationIssue

        issues[:] = [
            RecordValidationIssue(
                record_index=old_to_new[iss.record_index],
                form_key=iss.form_key,
                variable_name=iss.variable_name,
                message=iss.message,
                rule_id=iss.rule_id,
                severity=iss.severity,
            )
            for iss in issues
            if iss.record_index in old_to_new
        ]
    return subjects_data, skipped_count


__all__ = [
    "ELIGIBLE_SITE_ENROLLMENT_STATUSES",
    "build_site_validation_hook",
    "extract_candidate_subject_key",
    "filter_existing_subject_registrations",
    "is_site_enrollment_eligible",
    "normalize_subject_registration_inputs",
    "validate_subject_registration_sites",
]
