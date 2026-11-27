"""Builders, PHI redactors, and schema validators for CDISC ODM 1.3.2 and Dataset-JSON v1.1."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from imednet.models.sdtm import (
    deduplicate_sdtm_rows,
    format_iso8601_dtc,
    format_nested_variable_name,
    get_sdtm_domain_variables,
    prefix_colliding_variable_name,
    transform_records_to_sdtm_rows,
)
from imednet.models.standards import (
    format_variable_name,
    normalize_naming_convention,
    resolve_sdtm_domain,
)
from imednet.utils.dates import normalize_iso_date
from imednet.utils.security import (
    SensitivityRegistry,
    build_study_sensitivity_registry,
    global_sensitivity_registry,
    mask_clinical_phi,
)
from imednet.utils.tabular import MASKED_SENTINEL

from .emit import ODM_NS, _allocate_item_oids, _scrub_clinical_free_text
from .exchange import (
    DatasetJsonClinicalData,
    DatasetJsonEnvelope,
    DatasetJsonItem,
    DatasetJsonItemGroup,
    DatasetJsonItemType,
    DatasetJsonScalar,
    OdmAdminData,
    OdmAnnotation,
    OdmArchive,
    OdmAuditRecord,
    OdmClinicalData,
    OdmFlag,
    OdmFormData,
    OdmItemData,
    OdmItemGroupData,
    OdmLocation,
    OdmMetaDataVersionRef,
    OdmReferenceData,
    OdmStudyEventData,
    OdmSubjectData,
    OdmUser,
)
from .models import Choice, DataDictionary, Form, Metadata, Variable
from .normalize import _is_file_target, _safe_is_file, normalize_variable_type
from .schema import _parse_json_validation_source

ODM_132_XSD_PATH = Path(__file__).parent / "resources" / "cdisc-odm-1.3.2.xsd"
DATASET_JSON_110_SCHEMA_PATH = (
    Path(__file__).parent / "resources" / "dataset-json-1.1.0.schema.json"
)
UNSCHEDULED_EVENT_OID = "SE.UNSCHEDULED"
DEFAULT_EPOCH_DATETIME = "1970-01-01T00:00:00Z"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_NCNAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SDTM_BIRTH_DATE_TOKENS = frozenset(
    {"brthdtc", "brthdat", "birthdate", "dob", "dateofbirth", "birthdt"}
)


def _attr_or_key(obj: Any, *names: str) -> Any:
    """Read the first non-None attribute or mapping key from *obj*."""
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return None
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            return val
    return None


def make_oid_slug(raw: Any, fallback: str = "UNKNOWN") -> str:
    """Normalize an arbitrary token into a deterministic CDISC OID segment."""
    if raw is None:
        return fallback
    cleaned = _SLUG_RE.sub("_", str(raw).strip()).strip("_.")
    return cleaned or fallback


def make_xml_ncname(prefix: str, *parts: Any) -> str:
    """Construct a valid XML ``xs:ID`` NCName token starting with an ASCII letter."""
    joined = "_".join(
        _NCNAME_CLEAN_RE.sub("_", str(p).strip()).strip("_.")
        for p in parts
        if p is not None and str(p).strip()
    )
    token = f"{prefix}_{joined}" if joined else prefix
    if not token[0].isalpha() and token[0] != "_":
        token = f"ID_{token}"
    return token


def make_user_oid(user_token: Any) -> str:
    """Return a deterministic ODM ``UserOID`` for *user_token*."""
    slug = make_oid_slug(user_token, fallback="SYSTEM")
    return slug if slug.startswith("USR.") else f"USR.{slug}"


def make_location_oid(site_token: Any) -> str:
    """Return a deterministic ODM ``LocationOID`` for *site_token*."""
    slug = make_oid_slug(site_token, fallback="DEFAULT_SITE")
    return slug if slug.startswith("LOC.") else f"LOC.{slug}"


def make_study_event_oid(visit_token: Any) -> str:
    """Return a deterministic ODM ``StudyEventOID`` for *visit_token*."""
    if visit_token is None or str(visit_token).strip() in {"", "0", "-1"}:
        return UNSCHEDULED_EVENT_OID
    raw_str = str(visit_token).strip()
    if raw_str.startswith("SE."):
        return raw_str
    return f"SE.{make_oid_slug(raw_str, fallback='VISIT')}"


def normalize_odm_datetime(value: Any, *, fallback: str = DEFAULT_EPOCH_DATETIME) -> str:
    """Format temporal scalars or EDC date strings into deterministic ISO-8601 timestamps."""
    if value is None:
        return fallback
    if isinstance(value, datetime):
        dt_utc = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    raw = str(value).strip()
    if not raw:
        return fallback
    iso_candidate = raw.replace(" ", "T") if " " in raw and "T" not in raw else raw
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        dt_utc = parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    for fmt in ("%m-%d-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=UTC)
            return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    norm_date = normalize_iso_date(raw)
    if norm_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", norm_date):
        return f"{norm_date}T00:00:00Z"
    return iso_candidate


def _collect_max_snapshot_datetime(candidates: Iterable[Any]) -> str:
    """Select the maximum normalized ISO-8601 timestamp across *candidates* deterministically."""
    valid_stamps: list[str] = []
    for cand in candidates:
        if cand is None or (isinstance(cand, str) and not cand.strip()):
            continue
        norm = normalize_odm_datetime(cand, fallback="")
        if norm and norm != DEFAULT_EPOCH_DATETIME and re.match(r"^\d{4}-\d{2}-\d{2}T", norm):
            valid_stamps.append(norm)
    return max(valid_stamps) if valid_stamps else DEFAULT_EPOCH_DATETIME


def _is_birth_variable(key: str) -> bool:
    cleaned = re.sub(r"[^a-z0-9]", "", key.lower())
    return bool(cleaned) and (cleaned in _SDTM_BIRTH_DATE_TOKENS or cleaned.endswith("brthdtc"))


def _mask_scalar_if_sensitive(
    value: Any,
    *,
    var_name: str,
    label: str | None = None,
    mask_phi: bool = True,
    registry: SensitivityRegistry | None = None,
    as_sdtm: bool = False,
) -> Any:
    """Apply ``SensitivityRegistry`` masking to a clinical variable value when ``mask_phi=True``."""
    if not mask_phi or value is None:
        return value
    active_reg = global_sensitivity_registry if registry is None else registry
    if as_sdtm and _is_birth_variable(var_name) and not active_reg.is_exempt(var_name):
        return format_iso8601_dtc(value, mask_phi=True)
    is_sens = active_reg.is_sensitive(var_name) or (
        bool(label) and not active_reg.is_exempt(var_name) and active_reg.is_sensitive(label)
    )
    if is_sens:
        return MASKED_SENTINEL
    if isinstance(value, (dict, list, tuple)):
        return mask_clinical_phi(value, registry=active_reg)
    return value


def build_datadict_from_sdk_metadata(
    study_key: str,
    *,
    forms: Sequence[Any] = (),
    variables: Sequence[Any] = (),
    intervals: Sequence[Any] = (),
    study_name: str | None = None,
    study_description: str | None = None,
    protocol_name: str | None = None,
) -> DataDictionary:
    """Construct a canonical :class:`DataDictionary` from SDK Form/Variable/Interval objects."""
    vars_by_form_key: dict[str, list[Variable]] = {}
    vars_by_form_id: dict[int, list[Variable]] = {}

    for raw_var in variables:
        v_name = str(_attr_or_key(raw_var, "variable_name", "variableName", "name") or "").strip()
        if not v_name:
            continue
        v_type = _attr_or_key(raw_var, "variable_type", "variableType")
        v_label = _attr_or_key(raw_var, "label", "description")
        v_seq = _attr_or_key(raw_var, "sequence", "order")
        v_req = _attr_or_key(raw_var, "required")
        raw_choices = _attr_or_key(raw_var, "choices") or ()
        choices_list: list[Choice] = []
        if isinstance(raw_choices, Sequence) and not isinstance(raw_choices, (str, bytes)):
            for idx, ch in enumerate(raw_choices, 1):
                c_val = _attr_or_key(ch, "choice_value", "choiceValue", "value")
                c_txt = _attr_or_key(ch, "choice_text", "choiceText", "label", "text")
                if c_val is not None:
                    choices_list.append(
                        Choice(
                            choiceValue=str(c_val),
                            choiceText=str(c_txt if c_txt is not None else c_val),
                            position=_attr_or_key(ch, "position") or idx,
                        )
                    )
        var_model = Variable(
            variableName=v_name,
            variableType=normalize_variable_type(str(v_type) if v_type is not None else None)
            or "Text",
            label=str(v_label) if v_label is not None else v_name,
            sequence=int(v_seq) if isinstance(v_seq, int) and not isinstance(v_seq, bool) else None,
            required="Yes" if v_req in (True, "Yes", "yes", "true", "1") else "No",
            choices=choices_list,
        )
        fk = _attr_or_key(raw_var, "form_key", "formKey")
        fid = _attr_or_key(raw_var, "form_id", "formId")
        if fk:
            vars_by_form_key.setdefault(str(fk), []).append(var_model)
        elif isinstance(fid, int) and not isinstance(fid, bool):
            vars_by_form_id.setdefault(fid, []).append(var_model)

    dd_forms: list[Form] = []
    seen_form_keys: set[str] = set()
    for raw_form in forms:
        fk_raw = _attr_or_key(raw_form, "form_key", "formKey")
        fid_raw = _attr_or_key(raw_form, "form_id", "formId")
        fk_str = str(fk_raw).strip() if fk_raw else (f"FORM_{fid_raw}" if fid_raw else "")
        if not fk_str or fk_str in seen_form_keys:
            continue
        seen_form_keys.add(fk_str)
        f_vars = list(vars_by_form_key.get(fk_str, []))
        if not f_vars and isinstance(fid_raw, int) and not isinstance(fid_raw, bool):
            f_vars = list(vars_by_form_id.get(fid_raw, []))
        f_name = _attr_or_key(raw_form, "form_name", "formName")
        f_type = _attr_or_key(raw_form, "form_type", "formType")
        dd_forms.append(
            Form(
                formId=fid_raw if isinstance(fid_raw, (int, str)) else None,
                formKey=fk_str,
                formName=str(f_name) if f_name else fk_str,
                formType=str(f_type) if f_type else None,
                extraAttributes={"repeating": "Yes"},
                variables=f_vars,
            )
        )

    for fk_str, orphan_vars in sorted(vars_by_form_key.items()):
        if fk_str not in seen_form_keys:
            seen_form_keys.add(fk_str)
            dd_forms.append(
                Form(
                    formKey=fk_str,
                    formName=fk_str,
                    extraAttributes={"repeating": "Yes"},
                    variables=orphan_vars,
                )
            )

    visits_meta: list[dict[str, Any]] = []
    proto_refs: list[dict[str, Any]] = []
    seen_visit_oids: set[str] = set()
    for idx, iv in enumerate(intervals, 1):
        iv_name = _attr_or_key(iv, "interval_name", "intervalName", "name")
        iv_id = _attr_or_key(iv, "interval_id", "intervalId")
        token = iv_name or iv_id
        if token is None:
            continue
        se_oid = make_study_event_oid(token)
        if se_oid in seen_visit_oids:
            continue
        seen_visit_oids.add(se_oid)
        iv_forms_raw = _attr_or_key(iv, "forms") or ()
        iv_form_refs: list[dict[str, Any]] = []
        for f_idx, f_summary in enumerate(iv_forms_raw, 1):
            sf_key = _attr_or_key(f_summary, "form_key", "formKey")
            if sf_key and str(sf_key) in seen_form_keys:
                iv_form_refs.append(
                    {"formOid": str(sf_key), "orderNumber": f_idx, "mandatory": "No"}
                )
        visits_meta.append(
            {
                "oid": se_oid,
                "name": str(iv_name or se_oid),
                "repeating": "Yes",
                "type": "Scheduled",
                "forms": iv_form_refs,
            }
        )
        proto_refs.append(
            {
                "studyEventOid": se_oid,
                "orderNumber": idx,
                "mandatory": "No",
            }
        )

    extra_meta: dict[str, Any] = {
        "studyName": study_name or study_key,
        "studyDescription": study_description or f"Clinical Archive for {study_key}",
        "protocolName": protocol_name or study_key,
    }
    if visits_meta:
        extra_meta["visits"] = visits_meta
        extra_meta["protocol"] = {"studyEventRefs": proto_refs}

    return DataDictionary(
        metadata=Metadata(studyKey=study_key, source="imednet_sdk", extraAttributes=extra_meta),
        forms=dd_forms,
    )


def map_sites_and_users_to_admin_data(
    study_key: str,
    *,
    sites: Iterable[Any] = (),
    users: Iterable[Any] = (),
    inferred_site_tokens: Iterable[tuple[Any, str | None]] = (),
    inferred_user_tokens: Iterable[Any] = (),
    metadata_version_oid: str = "MDV.001",
    effective_date: str = "1970-01-01",
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
) -> OdmAdminData:
    """Map iMednet ``Site`` and ``User`` metadata into a deterministic ``OdmAdminData`` model."""
    reg = build_study_sensitivity_registry(custom_phi_keys)
    locations_by_oid: dict[str, OdmLocation] = {}
    site_id_to_oid: dict[str, str] = {}

    for site in sites:
        site_id = _attr_or_key(site, "site_id", "siteId")
        site_name = _attr_or_key(site, "site_name", "siteName", "name")
        token = site_id if site_id is not None else site_name
        if token is None:
            continue
        loc_oid = make_location_oid(token)
        eff_raw = _attr_or_key(site, "date_created", "dateCreated")
        eff_date = (
            normalize_odm_datetime(eff_raw, fallback=f"{effective_date}T00:00:00Z")[:10]
            if eff_raw
            else effective_date
        )
        loc_name = str(site_name) if site_name else str(token)
        locations_by_oid[loc_oid] = OdmLocation(
            oid=loc_oid,
            name=loc_name,
            locationType="Site",
            metaDataVersionRefs=(
                OdmMetaDataVersionRef(
                    studyOID=study_key,
                    metaDataVersionOID=metadata_version_oid,
                    effectiveDate=eff_date,
                ),
            ),
        )
        if site_id is not None:
            site_id_to_oid[str(site_id)] = loc_oid
        if site_name:
            site_id_to_oid[str(site_name)] = loc_oid

    for site_tok, site_label in inferred_site_tokens:
        if site_tok is None and not site_label:
            continue
        lookup_key = str(site_tok) if site_tok is not None else str(site_label)
        if lookup_key in site_id_to_oid:
            continue
        loc_oid = make_location_oid(site_tok if site_tok is not None else site_label)
        if loc_oid not in locations_by_oid:
            locations_by_oid[loc_oid] = OdmLocation(
                oid=loc_oid,
                name=str(site_label or site_tok),
                locationType="Site",
                metaDataVersionRefs=(
                    OdmMetaDataVersionRef(
                        studyOID=study_key,
                        metaDataVersionOID=metadata_version_oid,
                        effectiveDate=effective_date,
                    ),
                ),
            )
        site_id_to_oid[lookup_key] = loc_oid

    if not locations_by_oid:
        default_loc_oid = make_location_oid("DEFAULT_SITE")
        locations_by_oid[default_loc_oid] = OdmLocation(
            oid=default_loc_oid,
            name="Default Study Site",
            locationType="Site",
            metaDataVersionRefs=(
                OdmMetaDataVersionRef(
                    studyOID=study_key,
                    metaDataVersionOID=metadata_version_oid,
                    effectiveDate=effective_date,
                ),
            ),
        )

    users_by_oid: dict[str, OdmUser] = {}
    for user in users:
        login = _attr_or_key(user, "user_name", "userName", "login_name", "loginName", "login")
        uid = _attr_or_key(user, "user_id", "userId")
        token = login or uid
        if token is None:
            continue
        u_oid = make_user_oid(token)
        first_name = _attr_or_key(user, "first_name", "firstName")
        last_name = _attr_or_key(user, "last_name", "lastName")
        email_raw = _attr_or_key(user, "email")
        email_val: str | None = None
        if email_raw is not None and str(email_raw).strip():
            email_val = (
                MASKED_SENTINEL
                if (mask_phi and reg.is_sensitive("email"))
                else str(email_raw).strip()
            )
        full_parts = [str(p).strip() for p in (first_name, last_name) if p and str(p).strip()]
        full_name = " ".join(full_parts) if full_parts else None
        roles = _attr_or_key(user, "roles") or ()
        user_type = "Investigator"
        loc_refs: list[str] = []
        if isinstance(roles, Sequence) and not isinstance(roles, (str, bytes)):
            for role in roles:
                r_name = str(_attr_or_key(role, "name", "role_id", "roleId") or "").lower()
                if "monitor" in r_name or "sponsor" in r_name or "data" in r_name:
                    user_type = "Sponsor"
                r_sites = _attr_or_key(role, "sites") or ()
                if isinstance(r_sites, Sequence) and not isinstance(r_sites, (str, bytes)):
                    for rs in r_sites:
                        cand_oid = site_id_to_oid.get(str(rs)) or make_location_oid(rs)
                        if cand_oid in locations_by_oid and cand_oid not in loc_refs:
                            loc_refs.append(cand_oid)
        users_by_oid[u_oid] = OdmUser(
            oid=u_oid,
            userType=user_type,
            loginName=str(login) if login else str(token),
            displayName=full_name or (str(login) if login else str(token)),
            fullName=full_name,
            firstName=str(first_name) if first_name else None,
            lastName=str(last_name) if last_name else None,
            email=email_val,
            locationRefs=tuple(sorted(loc_refs)),
        )

    for u_tok in inferred_user_tokens:
        if u_tok is None or not str(u_tok).strip():
            continue
        u_oid = make_user_oid(u_tok)
        if u_oid not in users_by_oid:
            users_by_oid[u_oid] = OdmUser(
                oid=u_oid,
                userType="Other",
                loginName=str(u_tok).strip(),
                displayName=str(u_tok).strip(),
            )

    if not users_by_oid:
        sys_oid = make_user_oid("SYSTEM")
        users_by_oid[sys_oid] = OdmUser(
            oid=sys_oid,
            userType="Sponsor",
            loginName="SYSTEM",
            displayName="System Export User",
        )

    return OdmAdminData(
        studyOID=study_key,
        users=tuple(users_by_oid[k] for k in sorted(users_by_oid)),
        locations=tuple(locations_by_oid[k] for k in sorted(locations_by_oid)),
    )


def map_revisions_to_audit_records(
    revisions: Iterable[Any],
    *,
    default_location_oid: str | None = None,
    site_oid_by_record_id: Mapping[int, str] | None = None,
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
) -> tuple[dict[int, list[OdmAuditRecord]], list[OdmAuditRecord]]:
    """Map iMednet ``RecordRevision`` items into ODM ``<AuditRecord>`` objects.

    Returns ``(by_record_id, all_ordered_audit_records)``.
    """
    reg = build_study_sensitivity_registry(custom_phi_keys)
    fallback_loc = default_location_oid or make_location_oid("DEFAULT_SITE")
    by_record: dict[int, list[tuple[int, int, OdmAuditRecord]]] = {}
    unkeyed: list[OdmAuditRecord] = []

    for idx, rev in enumerate(revisions, 1):
        rec_id_raw = _attr_or_key(rev, "record_id", "recordId")
        rec_id = (
            int(rec_id_raw)
            if isinstance(rec_id_raw, int) and not isinstance(rec_id_raw, bool)
            else None
        )
        rev_num_raw = _attr_or_key(rev, "record_revision", "recordRevision")
        rev_num = (
            int(rev_num_raw)
            if isinstance(rev_num_raw, int) and not isinstance(rev_num_raw, bool)
            else idx
        )
        rev_id_raw = _attr_or_key(rev, "record_revision_id", "recordRevisionId")
        user_raw = _attr_or_key(rev, "user", "role") or "SYSTEM"
        user_oid = make_user_oid(user_raw)

        site_raw = _attr_or_key(rev, "site_id", "siteId", "site_name", "siteName")
        if site_raw is not None:
            loc_oid = make_location_oid(site_raw)
        elif rec_id is not None and site_oid_by_record_id and rec_id in site_oid_by_record_id:
            loc_oid = site_oid_by_record_id[rec_id]
        else:
            loc_oid = fallback_loc

        ts_raw = _attr_or_key(rev, "date_created", "dateCreated")
        ts_str = normalize_odm_datetime(ts_raw)

        reason_raw = _attr_or_key(rev, "reason_for_change", "reasonForChange")
        reason_str: str | None = None
        if reason_raw is not None and str(reason_raw).strip():
            reason_str = (
                _scrub_clinical_free_text(str(reason_raw), registry=reg)
                if mask_phi
                else str(reason_raw)
            )
        else:
            status_raw = _attr_or_key(rev, "record_status", "recordStatus")
            if status_raw is not None and str(status_raw).strip():
                reason_str = f"Status: {status_raw}"

        ar_id = (
            make_xml_ncname("AR", rec_id, rev_num)
            if rec_id is not None
            else make_xml_ncname("AR_REV", rev_id_raw or idx)
        )
        source_id = (
            str(rev_id_raw)
            if rev_id_raw is not None
            else (f"{rec_id}:{rev_num}" if rec_id is not None else str(idx))
        )
        audit_rec = OdmAuditRecord(
            id=ar_id,
            editPoint="DBAudit",
            usedImputationMethod="No",
            userOID=user_oid,
            locationOID=loc_oid,
            dateTimeStamp=ts_str,
            reasonForChange=reason_str,
            sourceID=source_id,
        )
        if rec_id is not None:
            by_record.setdefault(rec_id, []).append((rev_num, idx, audit_rec))
        else:
            unkeyed.append(audit_rec)

    grouped: dict[int, list[OdmAuditRecord]] = {}
    all_ordered: list[OdmAuditRecord] = []
    for rid in sorted(by_record):
        ordered_chain = [item[2] for item in sorted(by_record[rid], key=lambda t: (t[0], t[1]))]
        grouped[rid] = ordered_chain
        all_ordered.extend(ordered_chain)
    all_ordered.extend(unkeyed)
    return grouped, all_ordered


def map_queries_to_annotations(
    queries: Iterable[Any],
    *,
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
) -> tuple[
    dict[tuple[int, str], list[OdmAnnotation]],
    dict[int, list[OdmAnnotation]],
    dict[str, list[OdmAnnotation]],
    list[OdmAnnotation],
]:
    """Map iMednet ``Query`` and ``QueryComment`` histories into ODM ``<Annotation>`` objects.

    Returns ``(by_record_and_var, by_record_only, by_subject_only, unattached_annotations)``.
    """
    reg = build_study_sensitivity_registry(custom_phi_keys)
    by_rec_var: dict[tuple[int, str], list[OdmAnnotation]] = {}
    by_rec: dict[int, list[OdmAnnotation]] = {}
    by_subj: dict[str, list[OdmAnnotation]] = {}
    unattached: list[OdmAnnotation] = []

    seq_counter = 0
    for q_idx, query in enumerate(queries, 1):
        ann_id_raw = _attr_or_key(query, "annotation_id", "annotationId") or q_idx
        ann_type_raw = _attr_or_key(query, "annotation_type", "annotationType")
        rec_id_raw = _attr_or_key(query, "record_id", "recordId")
        rec_id = (
            int(rec_id_raw)
            if isinstance(rec_id_raw, int) and not isinstance(rec_id_raw, bool)
            else None
        )
        var_raw = _attr_or_key(query, "variable")
        var_name = str(var_raw).strip() if var_raw is not None and str(var_raw).strip() else None
        subj_raw = _attr_or_key(query, "subject_key", "subjectKey")
        subj_key = str(subj_raw).strip() if subj_raw is not None and str(subj_raw).strip() else None
        desc_raw = _attr_or_key(query, "description")
        q_status_raw = _attr_or_key(query, "status")
        q_status_str = (
            str(q_status_raw.value if hasattr(q_status_raw, "value") else q_status_raw).strip()
            if q_status_raw is not None and str(q_status_raw).strip()
            else None
        )

        comments_raw = _attr_or_key(query, "query_comments", "queryComments") or ()
        comments_list = (
            list(comments_raw)
            if isinstance(comments_raw, Sequence) and not isinstance(comments_raw, (str, bytes))
            else []
        )

        emitted_for_query: list[OdmAnnotation] = []
        if comments_list:
            sorted_comments = sorted(
                enumerate(comments_list, 1),
                key=lambda pair: (
                    _attr_or_key(pair[1], "sequence")
                    if isinstance(_attr_or_key(pair[1], "sequence"), int)
                    else pair[0]
                ),
            )
            for c_idx, comment_obj in sorted_comments:
                seq_counter += 1
                c_seq = _attr_or_key(comment_obj, "sequence") or c_idx
                c_text_raw = _attr_or_key(comment_obj, "comment")
                c_status = _attr_or_key(comment_obj, "annotation_status", "annotationStatus")
                c_closed = _attr_or_key(comment_obj, "closed")
                parts: list[str] = []
                if c_idx == 1 and desc_raw and str(desc_raw).strip():
                    parts.append(str(desc_raw).strip())
                if c_text_raw and str(c_text_raw).strip():
                    parts.append(str(c_text_raw).strip())
                raw_combined = " - ".join(parts) if parts else f"Query {ann_id_raw}"
                comment_str = (
                    _scrub_clinical_free_text(raw_combined, registry=reg)
                    if mask_phi
                    else raw_combined
                )
                sponsor_or_site = (
                    "Site" if ann_type_raw and "site" in str(ann_type_raw).lower() else "Sponsor"
                )
                status_token = (
                    str(c_status).strip()
                    if c_status and str(c_status).strip()
                    else (
                        q_status_str if q_status_str else ("Closed" if c_closed is True else "Open")
                    )
                )
                flags = [
                    OdmFlag(
                        flagValue=status_token,
                        flagValueCodeListOID="CL.IMEDNET_QUERY_STATUS",
                        flagType=str(ann_type_raw).strip() if ann_type_raw else None,
                        flagTypeCodeListOID=("CL.IMEDNET_QUERY_TYPE" if ann_type_raw else None),
                    )
                ]
                emitted_for_query.append(
                    OdmAnnotation(
                        seqNum=seq_counter,
                        id=make_xml_ncname("ANN", ann_id_raw, c_seq),
                        comment=comment_str,
                        sponsorOrSite=sponsor_or_site,
                        flags=tuple(flags),
                    )
                )
        else:
            seq_counter += 1
            raw_desc = (
                str(desc_raw).strip()
                if desc_raw and str(desc_raw).strip()
                else f"Query {ann_id_raw}"
            )
            comment_str = (
                _scrub_clinical_free_text(raw_desc, registry=reg) if mask_phi else raw_desc
            )
            flags = [
                OdmFlag(
                    flagValue=q_status_str or "Open",
                    flagValueCodeListOID="CL.IMEDNET_QUERY_STATUS",
                    flagType=str(ann_type_raw).strip() if ann_type_raw else None,
                    flagTypeCodeListOID="CL.IMEDNET_QUERY_TYPE" if ann_type_raw else None,
                )
            ]
            emitted_for_query.append(
                OdmAnnotation(
                    seqNum=seq_counter,
                    id=make_xml_ncname("ANN", ann_id_raw, 1),
                    comment=comment_str,
                    sponsorOrSite="Sponsor",
                    flags=tuple(flags),
                )
            )

        if rec_id is not None and var_name is not None:
            by_rec_var.setdefault((rec_id, var_name), []).extend(emitted_for_query)
        elif rec_id is not None:
            by_rec.setdefault(rec_id, []).extend(emitted_for_query)
        elif subj_key is not None:
            by_subj.setdefault(subj_key, []).extend(emitted_for_query)
        else:
            unattached.extend(emitted_for_query)

    return by_rec_var, by_rec, by_subj, unattached


def _format_odm_item_value(val: Any) -> tuple[str | None, bool]:
    """Convert a Python scalar into an ODM ``<ItemData>`` ``(Value, IsNull)`` pair."""
    if val is None:
        return None, True
    if isinstance(val, bool):
        return ("true" if val else "false"), False
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None, True
    if isinstance(val, datetime):
        return normalize_odm_datetime(val), False
    if isinstance(val, date):
        return val.isoformat(), False
    if isinstance(val, (dict, list, tuple)):
        return json.dumps(val, sort_keys=True, ensure_ascii=False, default=str), False
    text = str(val)
    if text == "":
        return None, True
    return text, False


def _run_enrichment_record_map(
    enrichment_pipeline: Any,
    rdata: dict[str, Any],
    *,
    mask_phi: bool,
    form_key: str | None = None,
) -> dict[str, Any]:
    """Run an enrichment pipeline on a single record payload dict with graceful signature fallback."""
    if enrichment_pipeline is None or not callable(getattr(enrichment_pipeline, "process", None)):
        return rdata
    try:
        processed = enrichment_pipeline.process(rdata, mask_phi=mask_phi, form_key=form_key)
    except TypeError:
        try:
            processed = enrichment_pipeline.process(rdata, mask_phi=mask_phi)
        except TypeError:
            processed = enrichment_pipeline.process(rdata)
    return dict(processed) if isinstance(processed, Mapping) else rdata


def _collect_renamed_variable_pairs(
    enrichment_pipeline: Any,
    *,
    form_key: str | None = None,
) -> dict[str, str]:
    """Return ``{source_var: target_field}`` pairs from *enrichment_pipeline* applicable to *form_key*."""
    if enrichment_pipeline is None:
        return {}
    raw_rules = getattr(enrichment_pipeline, "all_mappings", None)
    if not isinstance(raw_rules, Sequence):
        raw_map = getattr(enrichment_pipeline, "mappings", None)
        raw_rules = list(raw_map.values()) if isinstance(raw_map, Mapping) else []
    norm_fk = re.sub(r"[^a-z0-9]", "", (form_key or "").lower())
    renamed: dict[str, str] = {}
    for rule in raw_rules:
        src = getattr(rule, "source_variable_name", None)
        tgt = getattr(rule, "target_field", None)
        if not src or not tgt or str(src) == str(tgt):
            continue
        rule_fk = getattr(rule, "source_form_key", None)
        if rule_fk and norm_fk and re.sub(r"[^a-z0-9]", "", str(rule_fk).lower()) != norm_fk:
            continue
        renamed[str(src)] = str(tgt)
    return renamed


def build_odm_archive(
    study_key: str,
    *,
    study: Any = None,
    data_dictionary: DataDictionary | None = None,
    forms: Sequence[Any] = (),
    variables: Sequence[Any] = (),
    intervals: Sequence[Any] = (),
    subjects: Iterable[Any] = (),
    visits: Iterable[Any] = (),
    records: Iterable[Any] = (),
    revisions: Iterable[Any] = (),
    queries: Iterable[Any] = (),
    sites: Iterable[Any] = (),
    users: Iterable[Any] = (),
    reference_data: Sequence[OdmReferenceData] = (),
    metadata_version_oid: str = "MDV.001",
    metadata_version_name: str | None = None,
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
    enrichment_pipeline: Any = None,
    include_deleted: bool = False,
) -> OdmArchive:
    """Build a complete, deterministic CDISC ODM 1.3.2 :class:`OdmArchive` snapshot."""
    merged_phi_keys: set[str] = set(custom_phi_keys or ())
    pipe_phi = getattr(enrichment_pipeline, "phi_keys", None)
    if isinstance(pipe_phi, Iterable) and not isinstance(pipe_phi, (str, bytes)):
        merged_phi_keys.update(str(k) for k in pipe_phi if k)
    effective_phi_keys: Collection[str] | None = merged_phi_keys or None
    reg = build_study_sensitivity_registry(effective_phi_keys)
    records_list = [
        rec for rec in records if include_deleted or not bool(_attr_or_key(rec, "deleted"))
    ]
    revisions_list = list(revisions)
    queries_list = list(queries)
    sites_list = list(sites)
    users_list = list(users)
    subjects_list = list(subjects)
    visits_list = list(visits)

    s_name = _attr_or_key(study, "study_name", "studyName", "name") if study is not None else None
    s_desc = (
        _attr_or_key(study, "study_description", "studyDescription", "description")
        if study is not None
        else None
    )
    dd = data_dictionary or build_datadict_from_sdk_metadata(
        study_key,
        forms=forms,
        variables=variables,
        intervals=intervals,
        study_name=str(s_name) if s_name else None,
        study_description=str(s_desc) if s_desc else None,
    )
    if metadata_version_oid == "MDV.001":
        dd_mdv_oid = dd.metadata.extraAttributes.get("metaDataVersionOID")
        if isinstance(dd_mdv_oid, str) and dd_mdv_oid.strip():
            metadata_version_oid = dd_mdv_oid.strip()

    # Ensure any Form referenced solely by Records exists in DataDictionary
    existing_form_keys = {f.formKey: f for f in dd.forms}
    form_id_to_key: dict[int, str] = {}
    for f_obj in dd.forms:
        if isinstance(f_obj.formId, int) and not isinstance(f_obj.formId, bool):
            form_id_to_key[f_obj.formId] = f_obj.formKey
    for raw_f in forms:
        fid = _attr_or_key(raw_f, "form_id", "formId")
        fk = _attr_or_key(raw_f, "form_key", "formKey")
        if isinstance(fid, int) and not isinstance(fid, bool) and fk:
            form_id_to_key[fid] = str(fk)

    extra_forms: list[Form] = []
    discovered_vars_by_fk: dict[str, set[str]] = {}
    enriched_rdata_by_rec_id: dict[int, dict[str, Any]] = {}
    inherited_sensitive_by_rec_id: dict[int, set[str]] = {}
    dd_var_labels: dict[tuple[str, str], str | None] = {
        (f_obj.formKey, v_obj.variableName): v_obj.label
        for f_obj in dd.forms
        for v_obj in f_obj.variables
    }
    for rec in records_list:
        rfk = _attr_or_key(rec, "form_key", "formKey")
        rfid = _attr_or_key(rec, "form_id", "formId")
        fk_resolved = (
            str(rfk).strip()
            if rfk
            else (form_id_to_key.get(rfid, f"FORM_{rfid}") if rfid is not None else "FORM_DEFAULT")
        )
        rec_data = _attr_or_key(rec, "record_data", "recordData")
        raw_map = dict(rec_data) if isinstance(rec_data, Mapping) else {}
        renamed_pairs = _collect_renamed_variable_pairs(enrichment_pipeline, form_key=fk_resolved)
        inherited_sens: set[str] = set()
        if mask_phi and renamed_pairs:
            for src_var, tgt_var in renamed_pairs.items():
                src_lbl = dd_var_labels.get((fk_resolved, src_var))
                if reg.is_sensitive(src_var) or (
                    bool(src_lbl) and not reg.is_exempt(src_var) and reg.is_sensitive(src_lbl)
                ):
                    inherited_sens.add(tgt_var)
        enriched_map = _run_enrichment_record_map(
            enrichment_pipeline,
            raw_map,
            mask_phi=mask_phi,
            form_key=fk_resolved,
        )
        enriched_rdata_by_rec_id[id(rec)] = enriched_map
        inherited_sensitive_by_rec_id[id(rec)] = inherited_sens
        if enriched_map:
            discovered_vars_by_fk.setdefault(fk_resolved, set()).update(
                str(k) for k in enriched_map if str(k).strip()
            )
        if fk_resolved not in existing_form_keys:
            existing_form_keys[fk_resolved] = Form(
                formId=rfid if isinstance(rfid, int) else None,
                formKey=fk_resolved,
                formName=fk_resolved,
                extraAttributes={"repeating": "Yes"},
                variables=[],
            )
            extra_forms.append(existing_form_keys[fk_resolved])

    updated_dd_forms: list[Form] = []
    for f_model in [*dd.forms, *extra_forms]:
        known_vnames = {v.variableName for v in f_model.variables}
        missing_vnames = sorted(discovered_vars_by_fk.get(f_model.formKey, set()) - known_vnames)
        if missing_vnames:
            merged_vars = [
                *f_model.variables,
                *[
                    Variable(
                        variableName=vn,
                        variableType="Text",
                        label=vn,
                        sequence=len(f_model.variables) + idx,
                    )
                    for idx, vn in enumerate(missing_vnames, 1)
                ],
            ]
            updated_dd_forms.append(f_model.model_copy(update={"variables": merged_vars}))
        else:
            updated_dd_forms.append(f_model)
    dd = dd.model_copy(update={"forms": updated_dd_forms})

    sorted_forms = sorted(dd.forms, key=lambda f: f.formKey or "")
    sorted_vars_by_form, oid_by_occ, _ = _allocate_item_oids(sorted_forms)
    item_oid_lookup: dict[tuple[str, str], tuple[str, str | None]] = {}
    for f_idx, f_model in enumerate(sorted_forms):
        for v_idx, v_model in enumerate(sorted_vars_by_form[f_idx]):
            item_oid_lookup[(f_model.formKey, v_model.variableName)] = (
                oid_by_occ[(f_idx, v_idx)],
                v_model.label,
            )

    # Visit ID -> StudyEventOID lookup
    visit_oid_by_id: dict[int, tuple[str, str | None]] = {}
    for vis in visits_list:
        vid = _attr_or_key(vis, "visit_id", "visitId")
        iv_name = _attr_or_key(vis, "interval_name", "intervalName")
        iv_id = _attr_or_key(vis, "interval_id", "intervalId")
        if isinstance(vid, int) and not isinstance(vid, bool):
            visit_oid_by_id[vid] = (
                make_study_event_oid(iv_name or iv_id or vid),
                str(vid),
            )

    # Inferred site & user tokens
    inferred_sites: list[tuple[Any, str | None]] = []
    site_oid_by_record_id: dict[int, str] = {}
    subj_site_oid: dict[str, str] = {}
    for subj in subjects_list:
        sk = _attr_or_key(subj, "subject_key", "subjectKey")
        sid = _attr_or_key(subj, "site_id", "siteId")
        sname = _attr_or_key(subj, "site_name", "siteName")
        if sid is not None or sname:
            inferred_sites.append(
                (sid if sid is not None else sname, str(sname) if sname else None)
            )
            if sk:
                subj_site_oid[str(sk)] = make_location_oid(sid if sid is not None else sname)

    for rec in records_list:
        sid = _attr_or_key(rec, "site_id", "siteId")
        sname = _attr_or_key(rec, "site_name", "siteName")
        rid = _attr_or_key(rec, "record_id", "recordId")
        sk = _attr_or_key(rec, "subject_key", "subjectKey")
        if sid is not None or sname:
            tok = sid if sid is not None else sname
            inferred_sites.append((tok, str(sname) if sname else None))
            loc_oid = make_location_oid(tok)
            if isinstance(rid, int) and not isinstance(rid, bool):
                site_oid_by_record_id[rid] = loc_oid
            if sk and str(sk) not in subj_site_oid:
                subj_site_oid[str(sk)] = loc_oid

    inferred_users: list[Any] = []
    for rev in revisions_list:
        u = _attr_or_key(rev, "user", "role")
        if u:
            inferred_users.append(u)
        rsite = _attr_or_key(rev, "site_id", "siteId", "site_name", "siteName")
        if rsite is not None:
            inferred_sites.append((rsite, None))
    for q in queries_list:
        qcs = _attr_or_key(q, "query_comments", "queryComments") or ()
        if isinstance(qcs, Sequence) and not isinstance(qcs, (str, bytes)):
            for qc in qcs:
                qu = _attr_or_key(qc, "user")
                if qu:
                    inferred_users.append(qu)

    admin_data = map_sites_and_users_to_admin_data(
        study_key,
        sites=sites_list,
        users=users_list,
        inferred_site_tokens=inferred_sites,
        inferred_user_tokens=inferred_users,
        metadata_version_oid=metadata_version_oid,
        mask_phi=mask_phi,
        custom_phi_keys=effective_phi_keys,
    )
    default_loc_oid = admin_data.locations[0].oid

    audits_by_rec, all_audit_records = map_revisions_to_audit_records(
        revisions_list,
        default_location_oid=default_loc_oid,
        site_oid_by_record_id=site_oid_by_record_id,
        mask_phi=mask_phi,
        custom_phi_keys=effective_phi_keys,
    )
    ann_by_rec_var, ann_by_rec, ann_by_subj, unattached_anns = map_queries_to_annotations(
        queries_list,
        mask_phi=mask_phi,
        custom_phi_keys=effective_phi_keys,
    )

    # Group records deterministically:
    # - Scheduled / visit records: Subject -> (StudyEventOID, RepeatKey) -> FormOID -> [ItemGroupData]
    # - Subject-level (visitless) records: Subject -> FormOID -> [ItemGroupData]
    # - Unassigned (subjectless) records: ReferenceData -> [ItemGroupData]
    grouped_clinical: dict[
        str, dict[tuple[str, str | None], dict[str, list[OdmItemGroupData]]]
    ] = {}
    grouped_subject_forms: dict[str, dict[str, list[OdmItemGroupData]]] = {}
    unassigned_ref_item_groups: list[OdmItemGroupData] = []
    referenced_event_oids: set[str] = set()

    def _resolve_clean_subject_key(r: Any) -> str | None:
        raw_sk = _attr_or_key(r, "subject_key", "subjectKey", "subject_id", "subjectId")
        if raw_sk is None or isinstance(raw_sk, bool):
            return None
        cleaned = str(raw_sk).strip()
        return cleaned or None

    def _has_event_binding(vid_val: Any, iv_val: Any) -> bool:
        if isinstance(vid_val, int) and not isinstance(vid_val, bool) and vid_val > 0:
            return True
        return iv_val is not None and str(iv_val).strip() not in {"", "0", "-1"}

    def _rec_sort_key(r: Any) -> tuple[int, str, str, str, int, str]:
        sk_opt = _resolve_clean_subject_key(r)
        vid_v = _attr_or_key(r, "visit_id", "visitId")
        iv_v = _attr_or_key(r, "interval_name", "intervalName", "interval_id", "intervalId")
        if sk_opt is not None and _has_event_binding(vid_v, iv_v):
            se_v = (
                visit_oid_by_id[vid_v][0]
                if isinstance(vid_v, int) and vid_v in visit_oid_by_id
                else make_study_event_oid(iv_v if iv_v is not None else vid_v)
            )
        else:
            se_v = ""
        fk_v = str(_attr_or_key(r, "form_key", "formKey") or "")
        rid_v = _attr_or_key(r, "record_id", "recordId")
        rid_int = int(rid_v) if isinstance(rid_v, int) and not isinstance(rid_v, bool) else 0
        roid_v = str(_attr_or_key(r, "record_oid", "recordOid") or "")
        return (0 if sk_opt is not None else 1, sk_opt or "", se_v, fk_v, rid_int, roid_v)

    for rec in sorted(records_list, key=_rec_sort_key):
        subj_key = _resolve_clean_subject_key(rec)
        vid = _attr_or_key(rec, "visit_id", "visitId")
        iv_tok = _attr_or_key(rec, "interval_name", "intervalName", "interval_id", "intervalId")
        has_event = subj_key is not None and _has_event_binding(vid, iv_tok)
        se_oid: str | None = None
        se_rep: str | None = None
        if has_event:
            if isinstance(vid, int) and not isinstance(vid, bool) and vid > 0:
                if vid in visit_oid_by_id:
                    se_oid, se_rep = visit_oid_by_id[vid]
                else:
                    se_oid = make_study_event_oid(iv_tok if iv_tok is not None else vid)
                    se_rep = str(vid)
            else:
                se_oid = make_study_event_oid(iv_tok)
                se_rep = None
            referenced_event_oids.add(se_oid)

        rfk = _attr_or_key(rec, "form_key", "formKey")
        rfid = _attr_or_key(rec, "form_id", "formId")
        form_oid = (
            str(rfk).strip()
            if rfk
            else (form_id_to_key.get(rfid, f"FORM_{rfid}") if rfid is not None else "FORM_DEFAULT")
        )
        ig_oid = f"IG_{form_oid}"
        rid_raw = _attr_or_key(rec, "record_id", "recordId")
        rid_int = (
            int(rid_raw) if isinstance(rid_raw, int) and not isinstance(rid_raw, bool) else None
        )
        rid_str = (
            str(rid_int)
            if rid_int is not None
            else (str(rid_raw).strip() if isinstance(rid_raw, str) and rid_raw.strip() else None)
        )
        roid_raw = _attr_or_key(rec, "record_oid", "recordOid")
        ig_repeat_key = rid_str if rid_str is not None else (str(roid_raw) if roid_raw else None)
        tx_type = "Remove" if bool(_attr_or_key(rec, "deleted")) else None

        rec_data_map = enriched_rdata_by_rec_id.get(id(rec), {})
        inherited_sens_vars = inherited_sensitive_by_rec_id.get(id(rec), set())

        # Also include any queried variable on this record as IsNull="Yes" if absent from rec_data_map
        queried_vars_for_rec = (
            {vn for (q_rid, vn) in ann_by_rec_var if q_rid == rid_int}
            if rid_int is not None
            else set()
        )
        all_var_names = sorted(set(rec_data_map.keys()) | queried_vars_for_rec)

        item_objs: list[OdmItemData] = []
        for var_name in all_var_names:
            item_oid, var_label = item_oid_lookup.get((form_oid, var_name), (var_name, None))
            raw_val = rec_data_map.get(var_name)
            if mask_phi and raw_val is not None and var_name in inherited_sens_vars:
                masked_val: Any = MASKED_SENTINEL
            else:
                masked_val = _mask_scalar_if_sensitive(
                    raw_val,
                    var_name=var_name,
                    label=var_label,
                    mask_phi=mask_phi,
                    registry=reg,
                    as_sdtm=False,
                )
            val_str, is_null = _format_odm_item_value(masked_val)
            var_anns = (
                tuple(
                    ann.model_copy(update={"id": None})
                    for ann in ann_by_rec_var.get((rid_int, var_name), ())
                )
                if rid_int is not None
                else ()
            )
            item_objs.append(
                OdmItemData(
                    itemOID=item_oid,
                    value=val_str,
                    isNull=is_null,
                    annotations=var_anns,
                )
            )

        rec_audits = audits_by_rec.get(rid_int, []) if rid_int is not None else []
        inline_audit = rec_audits[-1].model_copy(update={"id": None}) if rec_audits else None
        rec_anns = (
            tuple(ann.model_copy(update={"id": None}) for ann in ann_by_rec.get(rid_int, ()))
            if rid_int is not None
            else ()
        )

        ig_data = OdmItemGroupData(
            itemGroupOID=ig_oid,
            itemGroupRepeatKey=ig_repeat_key,
            transactionType=tx_type,
            auditRecord=inline_audit,
            annotations=rec_anns,
            items=tuple(item_objs),
        )
        if subj_key is None:
            unassigned_ref_item_groups.append(ig_data)
            continue
        grouped_clinical.setdefault(subj_key, {})
        if se_oid is not None:
            grouped_clinical[subj_key].setdefault((se_oid, se_rep), {}).setdefault(
                form_oid, []
            ).append(ig_data)
        else:
            grouped_subject_forms.setdefault(subj_key, {}).setdefault(form_oid, []).append(ig_data)

    # Ensure subjects without records (e.g. subject-level queries or enrolled subjects) appear
    for sk_only in sorted(
        {
            str(_attr_or_key(s, "subject_key", "subjectKey"))
            for s in subjects_list
            if _attr_or_key(s, "subject_key", "subjectKey")
        }
        | set(ann_by_subj.keys())
    ):
        grouped_clinical.setdefault(sk_only, {})

    # Ensure StudyEventDef entries exist in DataDictionary for all referenced StudyEventOIDs
    visits_extra = list(dd.metadata.extraAttributes.get("visits") or [])
    known_se_oids = {
        str(v.get("oid")) for v in visits_extra if isinstance(v, dict) and v.get("oid")
    }
    all_form_refs = [
        {"formOid": f.formKey, "orderNumber": idx, "mandatory": "No"}
        for idx, f in enumerate(sorted_forms, 1)
    ]
    added_se = False
    for se_oid in sorted(referenced_event_oids):
        if se_oid not in known_se_oids:
            known_se_oids.add(se_oid)
            added_se = True
            is_unsched = se_oid == UNSCHEDULED_EVENT_OID
            visits_extra.append(
                {
                    "oid": se_oid,
                    "name": "Unscheduled" if is_unsched else se_oid,
                    "repeating": "Yes",
                    "type": "Unscheduled" if is_unsched else "Scheduled",
                    "forms": all_form_refs,
                }
            )
    if added_se:
        new_extra = dict(dd.metadata.extraAttributes)
        new_extra["visits"] = visits_extra
        proto_dict = dict(new_extra.get("protocol") or {})
        existing_proto_refs = list(proto_dict.get("studyEventRefs") or [])
        known_proto_oids = {
            str(r.get("studyEventOid")) for r in existing_proto_refs if isinstance(r, dict)
        }
        for se_oid in sorted(known_se_oids):
            if se_oid not in known_proto_oids:
                existing_proto_refs.append(
                    {
                        "studyEventOid": se_oid,
                        "orderNumber": len(existing_proto_refs) + 1,
                        "mandatory": "No",
                    }
                )
        proto_dict["studyEventRefs"] = existing_proto_refs
        new_extra["protocol"] = proto_dict
        dd = dd.model_copy(
            update={"metadata": dd.metadata.model_copy(update={"extraAttributes": new_extra})}
        )

    subject_models: list[OdmSubjectData] = []
    for subj_key in sorted(grouped_clinical):
        events_map = grouped_clinical[subj_key]
        se_models: list[OdmStudyEventData] = []
        for se_oid, se_rep in sorted(events_map, key=lambda k: (k[0], k[1] or "")):
            forms_map = events_map[(se_oid, se_rep)]
            form_models_out: list[OdmFormData] = []
            for form_oid in sorted(forms_map):
                ig_list = forms_map[form_oid]
                form_models_out.append(
                    OdmFormData(
                        formOID=form_oid,
                        itemGroups=tuple(ig_list),
                    )
                )
            se_models.append(
                OdmStudyEventData(
                    studyEventOID=se_oid,
                    studyEventRepeatKey=se_rep,
                    forms=tuple(form_models_out),
                )
            )
        subj_forms_map = grouped_subject_forms.get(subj_key, {})
        subj_form_models: list[OdmFormData] = []
        for form_oid in sorted(subj_forms_map):
            subj_form_models.append(
                OdmFormData(
                    formOID=form_oid,
                    itemGroups=tuple(subj_forms_map[form_oid]),
                )
            )
        subj_anns = tuple(
            ann.model_copy(update={"id": None}) for ann in ann_by_subj.get(subj_key, ())
        )
        subject_models.append(
            OdmSubjectData(
                subjectKey=subj_key,
                siteLocationOID=subj_site_oid.get(subj_key, default_loc_oid),
                annotations=subj_anns,
                studyEvents=tuple(se_models),
                forms=tuple(subj_form_models),
            )
        )

    all_global_anns: list[OdmAnnotation] = []
    for (_, _), anns in sorted(ann_by_rec_var.items()):
        all_global_anns.extend(anns)
    for _, anns in sorted(ann_by_rec.items()):
        all_global_anns.extend(anns)
    for _, anns in sorted(ann_by_subj.items()):
        all_global_anns.extend(anns)
    all_global_anns.extend(unattached_anns)
    all_global_anns.sort(key=lambda a: a.seq_num)

    clinical_container = OdmClinicalData(
        studyOID=study_key,
        metaDataVersionOID=metadata_version_oid,
        subjects=tuple(subject_models),
        auditRecords=tuple(all_audit_records),
        annotations=tuple(all_global_anns),
    )

    timestamp_candidates: list[Any] = [dd.metadata.generatedAt]
    for rec in records_list:
        timestamp_candidates.append(_attr_or_key(rec, "date_modified", "dateModified"))
        timestamp_candidates.append(_attr_or_key(rec, "date_created", "dateCreated"))
    for rev in revisions_list:
        timestamp_candidates.append(_attr_or_key(rev, "date_created", "dateCreated"))
    resolved_creation_dt = (
        normalize_odm_datetime(creation_datetime)
        if creation_datetime
        else _collect_max_snapshot_datetime(timestamp_candidates)
    )

    final_reference_data: list[OdmReferenceData] = list(reference_data)
    if unassigned_ref_item_groups:
        merged_into_existing = False
        for ref_idx, ref_entry in enumerate(final_reference_data):
            if (
                ref_entry.study_oid == study_key
                and ref_entry.metadata_version_oid == metadata_version_oid
            ):
                final_reference_data[ref_idx] = ref_entry.model_copy(
                    update={"item_groups": (*ref_entry.item_groups, *unassigned_ref_item_groups)}
                )
                merged_into_existing = True
                break
        if not merged_into_existing:
            final_reference_data.append(
                OdmReferenceData(
                    studyOID=study_key,
                    metaDataVersionOID=metadata_version_oid,
                    itemGroups=tuple(unassigned_ref_item_groups),
                )
            )

    return OdmArchive(
        fileOID=file_oid or f"ODM.{make_oid_slug(study_key)}",
        fileType="Snapshot",
        odmVersion="1.3.2",
        creationDateTime=resolved_creation_dt,
        originator="imednet-toolkit",
        sourceSystem="imednet",
        metaDataVersionOID=metadata_version_oid,
        metaDataVersionName=metadata_version_name or "Study Metadata and Clinical Archive",
        dataDictionary=dd,
        adminData=(admin_data,),
        referenceData=tuple(final_reference_data),
        clinicalData=(clinical_container,),
    )


def _infer_dataset_json_type_from_variable_type(var_type: str | None) -> DatasetJsonItemType:
    vt = (normalize_variable_type(var_type) or "").lower()
    if vt == "integer":
        return "integer"
    if vt == "float":
        return "float"
    if vt == "boolean":
        return "boolean"
    return "string"


def _infer_dataset_json_type_from_values(values: Sequence[Any]) -> DatasetJsonItemType:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "string"
    if all(isinstance(v, bool) for v in non_null):
        return "boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
        return "integer"
    if all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in non_null):
        return "float"
    return "string"


def _coerce_dataset_json_cell(val: Any, target_type: DatasetJsonItemType) -> DatasetJsonScalar:
    """Coerce a cell scalar to match the declared Dataset-JSON column ``type``."""
    if val is None:
        return None
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    if val == MASKED_SENTINEL:
        return MASKED_SENTINEL if target_type == "string" else None

    if target_type == "boolean":
        if isinstance(val, bool):
            return val
        low = str(val).strip().lower()
        if low in {"true", "1", "yes", "y"}:
            return True
        if low in {"false", "0", "no", "n"}:
            return False
        return None

    if target_type == "integer":
        if isinstance(val, bool):
            return int(val)
        if isinstance(val, int):
            return val
        if isinstance(val, float) and val.is_integer():
            return int(val)
        try:
            return int(str(val).strip())
        except ValueError:
            return None

    if target_type in {"float", "double", "decimal"}:
        if isinstance(val, bool):
            return float(int(val))
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, Decimal):
            return float(val)
        try:
            return float(str(val).strip())
        except ValueError:
            return None

    # target_type == "string"
    if isinstance(val, datetime):
        return normalize_odm_datetime(val)
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, (dict, list, tuple)):
        return json.dumps(val, sort_keys=True, ensure_ascii=False, default=str)
    return str(val)


def build_dataset_json_envelope(
    study_key: str,
    *,
    records: Iterable[Any] = (),
    forms: Sequence[Any] = (),
    variables: Sequence[Any] = (),
    visits: Iterable[Any] = (),
    data_dictionary: DataDictionary | None = None,
    as_sdtm: bool | None = None,
    sdtm_domain: str | None = None,
    use_labels_as_columns: bool = False,
    variable_whitelist: Collection[str] | None = None,
    form_whitelist: Collection[int] | None = None,
    naming_convention: str | None = None,
    preserve_record_id: bool = False,
    include_deleted: bool = False,
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
    enrichment_pipeline: Any = None,
    metadata_version_oid: str = "MDV.001",
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    source_system_version: str | None = None,
) -> DatasetJsonEnvelope:
    """Build a deterministic CDISC Dataset-JSON v1.1 envelope for CDASH Forms or SDTM domains."""
    merged_phi_keys: set[str] = set(custom_phi_keys or ())
    pipe_phi = getattr(enrichment_pipeline, "phi_keys", None)
    if isinstance(pipe_phi, Iterable) and not isinstance(pipe_phi, (str, bytes)):
        merged_phi_keys.update(str(k) for k in pipe_phi if k)
    effective_phi_keys: Collection[str] | None = merged_phi_keys or None
    reg = build_study_sensitivity_registry(effective_phi_keys)
    effective_sdtm = bool(sdtm_domain) if as_sdtm is None else bool(as_sdtm)
    canonical_naming_conv = normalize_naming_convention(naming_convention)
    allowed_form_ids = set(form_whitelist) if form_whitelist is not None else None
    allowed_vars = set(variable_whitelist) if variable_whitelist is not None else None

    if data_dictionary is not None:
        if not forms:
            forms = list(data_dictionary.forms)
        if not variables:
            dd_vars: list[Any] = []
            for df in data_dictionary.forms:
                for dv in df.variables:
                    dd_vars.append(
                        {
                            "variableName": dv.variableName,
                            "label": dv.label,
                            "variableType": dv.variableType,
                            "sequence": dv.sequence,
                            "formKey": df.formKey,
                            "formId": df.formId,
                        }
                    )
            variables = dd_vars

    visit_meta_by_id: dict[int, tuple[Any, Any]] = {}
    for vis in visits:
        vid = _attr_or_key(vis, "visit_id", "visitId")
        if isinstance(vid, int) and not isinstance(vid, bool):
            visit_meta_by_id[vid] = (
                _attr_or_key(vis, "interval_id", "intervalId"),
                _attr_or_key(vis, "interval_name", "intervalName"),
            )

    filtered_records: list[Any] = []
    timestamp_candidates: list[Any] = []
    for rec in records:
        if not include_deleted and bool(_attr_or_key(rec, "deleted")):
            continue
        rfid = _attr_or_key(rec, "form_id", "formId")
        if allowed_form_ids is not None and rfid not in allowed_form_ids:
            continue
        norm_rec = rec
        rvid = _attr_or_key(norm_rec, "visit_id", "visitId")
        if (
            isinstance(rvid, int)
            and rvid in visit_meta_by_id
            and _attr_or_key(norm_rec, "interval_name", "intervalName") is None
            and hasattr(norm_rec, "model_copy")
        ):
            iv_id, iv_name = visit_meta_by_id[rvid]
            updates: dict[str, Any] = {}
            if iv_id is not None and getattr(norm_rec, "interval_id", None) is None:
                updates["interval_id"] = iv_id
            if iv_name is not None:
                updates["interval_name"] = iv_name
            if updates:
                norm_rec = norm_rec.model_copy(update=updates)
        filtered_records.append(norm_rec)
        timestamp_candidates.append(_attr_or_key(norm_rec, "date_modified", "dateModified"))
        timestamp_candidates.append(_attr_or_key(norm_rec, "date_created", "dateCreated"))

    resolved_creation_dt = (
        normalize_odm_datetime(creation_datetime)
        if creation_datetime
        else _collect_max_snapshot_datetime(timestamp_candidates)
    )

    form_name_by_key: dict[str, str] = {}
    form_key_by_id: dict[int, str] = {}
    for raw_f in forms:
        fk = _attr_or_key(raw_f, "form_key", "formKey")
        fid = _attr_or_key(raw_f, "form_id", "formId")
        fname = _attr_or_key(raw_f, "form_name", "formName")
        if fk:
            fk_s = str(fk).strip()
            form_name_by_key[fk_s] = str(fname).strip() if fname else fk_s
            if isinstance(fid, int) and not isinstance(fid, bool):
                form_key_by_id[fid] = fk_s

    var_meta_by_form: dict[str, dict[str, tuple[str, DatasetJsonItemType, int | None]]] = {}
    global_var_meta: dict[str, tuple[str, DatasetJsonItemType, int | None]] = {}
    for raw_v in variables:
        vn = _attr_or_key(raw_v, "variable_name", "variableName", "name")
        if not vn:
            continue
        vn_s = str(vn).strip()
        vlbl = str(_attr_or_key(raw_v, "label", "description") or vn_s)
        vtype = _infer_dataset_json_type_from_variable_type(
            _attr_or_key(raw_v, "variable_type", "variableType")
        )
        vseq_raw = _attr_or_key(raw_v, "sequence", "order")
        vseq = (
            int(vseq_raw) if isinstance(vseq_raw, int) and not isinstance(vseq_raw, bool) else None
        )
        vfk = _attr_or_key(raw_v, "form_key", "formKey")
        vfid = _attr_or_key(raw_v, "form_id", "formId")
        fk_resolved = (
            str(vfk).strip()
            if vfk
            else (form_key_by_id.get(vfid) if isinstance(vfid, int) else None)
        )
        info = (vlbl, vtype, vseq)
        global_var_meta.setdefault(vn_s, info)
        if fk_resolved:
            var_meta_by_form.setdefault(fk_resolved, {})[vn_s] = info

    item_groups: dict[str, DatasetJsonItemGroup] = {}

    if effective_sdtm:
        target_domains: list[str] = []
        records_by_domain: dict[str, list[Any]] = {}
        if sdtm_domain:
            dom_code = resolve_sdtm_domain(sdtm_domain) or sdtm_domain.strip().upper()
            target_domains.append(dom_code)
            for rec in filtered_records:
                rfk = _attr_or_key(rec, "form_key", "formKey")
                rfid = _attr_or_key(rec, "form_id", "formId")
                fk_cand = str(rfk) if rfk else form_key_by_id.get(rfid, "")  # type: ignore[arg-type]
                rec_dom = resolve_sdtm_domain(fk_cand) if fk_cand else None
                if rec_dom is None or rec_dom == dom_code:
                    records_by_domain.setdefault(dom_code, []).append(rec)
        else:
            for rec in filtered_records:
                rfk = _attr_or_key(rec, "form_key", "formKey")
                rfid = _attr_or_key(rec, "form_id", "formId")
                fk_cand = str(rfk) if rfk else form_key_by_id.get(rfid, "SUPP")  # type: ignore[arg-type]
                dom_code = resolve_sdtm_domain(fk_cand) or make_oid_slug(fk_cand, "DM").upper()
                records_by_domain.setdefault(dom_code, []).append(rec)
            target_domains = sorted(records_by_domain)
            if not target_domains:
                for fk_s in sorted(form_name_by_key):
                    dom_code = resolve_sdtm_domain(fk_s) or fk_s.upper()
                    if dom_code not in target_domains:
                        target_domains.append(dom_code)
                if not target_domains:
                    target_domains = ["DM"]

        for dom in sorted(dict.fromkeys(target_domains)):
            dom_recs = records_by_domain.get(dom, [])
            sdtm_rows = deduplicate_sdtm_rows(
                transform_records_to_sdtm_rows(
                    dom_recs,
                    domain=dom,
                    study_key=study_key,
                    include_deleted=include_deleted,
                    mask_phi=mask_phi,
                    phi_keys=effective_phi_keys,
                    registry=reg,
                    enrichment_pipeline=enrichment_pipeline,
                    preserve_record_id=preserve_record_id,
                )
            )
            columns: list[str] = list(get_sdtm_domain_variables(dom))
            if preserve_record_id and "RECORD_ID" not in columns:
                columns.append("RECORD_ID")
            seen_cols = set(columns)
            for r_dict in sdtm_rows:
                for k in r_dict:
                    if k not in seen_cols:
                        seen_cols.add(k)
                        columns.append(k)
            if allowed_vars is not None:
                columns = [c for c in columns if c in allowed_vars]

            # Apply SensitivityRegistry masking across SDTM rows
            processed_rows: list[dict[str, Any]] = []
            for r_dict in sdtm_rows:
                clean_row: dict[str, Any] = {}
                for col in columns:
                    clean_row[col] = _mask_scalar_if_sensitive(
                        r_dict.get(col),
                        var_name=col,
                        mask_phi=mask_phi,
                        registry=reg,
                        as_sdtm=True,
                    )
                processed_rows.append(clean_row)
            processed_rows = deduplicate_sdtm_rows(processed_rows)

            # Deterministic sort by USUBJID, *SEQ / RECORD_ID
            seq_col = f"{dom}SEQ"
            processed_rows.sort(
                key=lambda r: (
                    0 if r.get("USUBJID") else 1,
                    str(r.get("USUBJID") or ""),
                    int(r[seq_col]) if isinstance(r.get(seq_col), int) else 0,
                    str(r.get("RECORD_ID") or ""),
                )
            )

            seq_item = DatasetJsonItem(
                OID="ITEMGROUPDATASEQ",
                name="ITEMGROUPDATASEQ",
                label="Record Identifier",
                type="integer",
            )
            col_items: list[DatasetJsonItem] = [seq_item]
            key_seq = 0
            key_candidates = (
                {"STUDYID", "DOMAIN", "USUBJID"}
                if dom == "DM"
                else {"STUDYID", "USUBJID", seq_col, "RECORD_ID"}
            )
            sdtm_meta_candidates = {"STUDYID", "DOMAIN", "USUBJID", seq_col, "RECORD_ID"}
            used_sdtm_col_lower: set[str] = {"itemgroupdataseq"}
            used_sdtm_oids: set[str] = {"ITEMGROUPDATASEQ"}
            sdtm_col_lookup: list[tuple[str, DatasetJsonItem]] = []
            for col in columns:
                if col == "ITEMGROUPDATASEQ":
                    continue
                col_vals = [r.get(col) for r in processed_rows]
                col_type: DatasetJsonItemType = (
                    "integer"
                    if col in {seq_col, "AGE"}
                    and all(
                        v is None or (isinstance(v, int) and not isinstance(v, bool))
                        for v in col_vals
                    )
                    else _infer_dataset_json_type_from_values(col_vals)
                )
                if mask_phi and any(v == MASKED_SENTINEL for v in col_vals):
                    col_type = "string"
                k_seq: int | None = None
                if col in key_candidates:
                    key_seq += 1
                    k_seq = key_seq
                is_sdtm_meta = col in sdtm_meta_candidates
                if canonical_naming_conv is not None and canonical_naming_conv != "sdtm":
                    raw_out_name = format_variable_name(
                        col,
                        canonical_naming_conv,
                        domain=dom,
                        is_metadata=is_sdtm_meta,
                    )
                else:
                    raw_out_name = col
                if is_sdtm_meta and raw_out_name.lower() not in used_sdtm_col_lower:
                    used_sdtm_col_lower.add(raw_out_name.lower())
                    out_col_name = raw_out_name
                else:
                    out_col_name = prefix_colliding_variable_name(
                        raw_out_name,
                        convention=canonical_naming_conv or "sdtm",
                        used_lower=used_sdtm_col_lower,
                    )
                base_item_oid = f"IT.{dom}.{make_oid_slug(out_col_name, col)}"
                item_oid = base_item_oid
                oid_idx = 2
                while item_oid in used_sdtm_oids:
                    item_oid = f"{base_item_oid}_{oid_idx}"
                    oid_idx += 1
                used_sdtm_oids.add(item_oid)
                item_obj = DatasetJsonItem(
                    OID=item_oid,
                    name=out_col_name,
                    label=col if use_labels_as_columns else out_col_name,
                    type=col_type,
                    keySequence=k_seq,
                )
                col_items.append(item_obj)
                sdtm_col_lookup.append((col, item_obj))

            tuple_rows = tuple(
                (
                    row_idx,
                    *(
                        _coerce_dataset_json_cell(r.get(src_col), item_def.type)
                        for src_col, item_def in sdtm_col_lookup
                    ),
                )
                for row_idx, r in enumerate(processed_rows, 1)
            )
            ig_oid = f"IG.{dom}"
            item_groups[ig_oid] = DatasetJsonItemGroup(
                records=len(tuple_rows),
                name=dom,
                label=form_name_by_key.get(dom, f"{dom} Domain"),
                items=tuple(col_items),
                itemData=tuple_rows,
            )
    else:
        # CDASH per-Form itemGroupData datasets
        recs_by_form: dict[str, list[Any]] = {}
        for rec in filtered_records:
            rfk = _attr_or_key(rec, "form_key", "formKey")
            rfid = _attr_or_key(rec, "form_id", "formId")
            fk_s = (
                str(rfk).strip()
                if rfk
                else (
                    form_key_by_id.get(rfid, f"FORM_{rfid}")
                    if isinstance(rfid, int)
                    else "FORM_DEFAULT"
                )
            )
            recs_by_form.setdefault(fk_s, []).append(rec)

        all_form_keys = sorted(
            set(recs_by_form.keys())
            | (
                {
                    fk_s
                    for fid, fk_s in form_key_by_id.items()
                    if allowed_form_ids is None or fid in allowed_form_ids
                }
                if form_key_by_id
                else set(form_name_by_key.keys())
            )
        )

        def _fmt_meta_col(raw_col: str) -> str:
            if naming_convention:
                return format_variable_name(raw_col, naming_convention, is_metadata=True)
            return raw_col

        meta_defs = [
            (_fmt_meta_col("studyKey"), "Study Key", "string", 1),
            (_fmt_meta_col("subjectKey"), "Subject Key", "string", 2),
            (_fmt_meta_col("recordId"), "Record ID", "integer", 3),
            (_fmt_meta_col("visitId"), "Visit ID", "integer", None),
            (_fmt_meta_col("formKey"), "Form Key", "string", None),
        ]

        for fk_s in all_form_keys:
            form_recs = sorted(
                recs_by_form.get(fk_s, []),
                key=lambda r: (
                    str(_attr_or_key(r, "subject_key", "subjectKey") or ""),
                    int(_attr_or_key(r, "record_id", "recordId") or 0)
                    if isinstance(_attr_or_key(r, "record_id", "recordId"), int)
                    else 0,
                ),
            )
            form_vmeta = dict(var_meta_by_form.get(fk_s, {}))
            renamed_pairs = _collect_renamed_variable_pairs(enrichment_pipeline, form_key=fk_s)
            inherited_sens_targets: set[str] = set()
            for src_var, tgt_var in renamed_pairs.items():
                src_info = form_vmeta.get(src_var) or global_var_meta.get(src_var)
                if src_info is not None and tgt_var not in form_vmeta:
                    form_vmeta[tgt_var] = (tgt_var, src_info[1], src_info[2])
                if mask_phi:
                    src_lbl = src_info[0] if src_info is not None else None
                    if reg.is_sensitive(src_var) or (
                        bool(src_lbl) and not reg.is_exempt(src_var) and reg.is_sensitive(src_lbl)
                    ):
                        inherited_sens_targets.add(tgt_var)

            enriched_form_recs: list[tuple[Any, dict[str, Any]]] = []
            present_enriched_keys: set[str] = set()
            for rec in form_recs:
                rdata_raw = _attr_or_key(rec, "record_data", "recordData")
                rdata_in = dict(rdata_raw) if isinstance(rdata_raw, Mapping) else {}
                rdata_out = _run_enrichment_record_map(
                    enrichment_pipeline,
                    rdata_in,
                    mask_phi=mask_phi,
                    form_key=fk_s,
                )
                present_enriched_keys.update(str(k) for k in rdata_out)
                enriched_form_recs.append((rec, rdata_out))

            replaced_sources = {src for src in renamed_pairs if src not in present_enriched_keys}
            discovered_vars: list[str] = sorted(
                (vn for vn in form_vmeta if vn not in replaced_sources),
                key=lambda vn: (
                    0 if form_vmeta[vn][2] is not None else 1,
                    form_vmeta[vn][2] or 0,
                    vn,
                ),
            )
            seen_vars = set(discovered_vars)
            for _rec, rdata in enriched_form_recs:
                for vk in sorted(rdata.keys(), key=str):
                    vk_s = str(vk)
                    if vk_s not in seen_vars:
                        seen_vars.add(vk_s)
                        discovered_vars.append(vk_s)

            if allowed_vars is not None:
                discovered_vars = [
                    vn
                    for vn in discovered_vars
                    if vn in allowed_vars
                    or (
                        naming_convention
                        and format_nested_variable_name(vn, naming_convention, domain=sdtm_domain)
                        in allowed_vars
                    )
                ]

            active_meta_defs: list[tuple[str, str, str, int | None]] = []
            for m_col, m_lbl, m_type, m_kseq in meta_defs:
                if (
                    allowed_vars is not None
                    and m_col not in allowed_vars
                    and m_col
                    not in {
                        meta_defs[0][0],
                        meta_defs[1][0],
                        meta_defs[2][0],
                    }
                ):
                    continue
                active_meta_defs.append((m_col, m_lbl, m_type, m_kseq))

            metadata_lower: set[str] = {"itemgroupdataseq"} | {
                m_col.lower() for m_col, _, _, _ in meta_defs
            }
            used_col_lower: set[str] = {"itemgroupdataseq"} | {
                m_col.lower() for m_col, _, _, _ in active_meta_defs
            }

            # Build normalized row dicts with disambiguated column names
            row_dicts: list[dict[str, Any]] = []
            col_name_for_var: dict[str, str] = {}
            for vn in discovered_vars:
                lbl = (form_vmeta.get(vn) or global_var_meta.get(vn) or (vn, "string", None))[0]
                if use_labels_as_columns and lbl:
                    raw_col_cand = lbl
                elif naming_convention:
                    raw_col_cand = format_nested_variable_name(
                        vn, naming_convention, domain=sdtm_domain
                    )
                else:
                    raw_col_cand = vn
                col_name_for_var[vn] = prefix_colliding_variable_name(
                    raw_col_cand,
                    metadata_lower=metadata_lower,
                    convention=naming_convention,
                    used_lower=used_col_lower,
                )

            for rec, rdata in enriched_form_recs:
                row_map: dict[str, Any] = {
                    meta_defs[0][0]: _attr_or_key(rec, "study_key", "studyKey") or study_key,
                    meta_defs[1][0]: _attr_or_key(rec, "subject_key", "subjectKey"),
                    meta_defs[2][0]: _attr_or_key(rec, "record_id", "recordId"),
                    meta_defs[3][0]: _attr_or_key(rec, "visit_id", "visitId"),
                    meta_defs[4][0]: fk_s,
                }
                for vn in discovered_vars:
                    lbl = (form_vmeta.get(vn) or global_var_meta.get(vn) or (vn, "string", None))[0]
                    target_col = col_name_for_var[vn]
                    raw_val = rdata.get(vn)
                    if mask_phi and raw_val is not None and vn in inherited_sens_targets:
                        row_map[target_col] = MASKED_SENTINEL
                    else:
                        row_map[target_col] = _mask_scalar_if_sensitive(
                            raw_val,
                            var_name=vn,
                            label=lbl,
                            mask_phi=mask_phi,
                            registry=reg,
                        )
                row_dicts.append(row_map)

            items_out: list[DatasetJsonItem] = [
                DatasetJsonItem(
                    OID="ITEMGROUPDATASEQ",
                    name="ITEMGROUPDATASEQ",
                    label="Record Identifier",
                    type="integer",
                )
            ]
            metadata_oids_lower: set[str] = {"itemgroupdataseq"} | {
                f"IT.{fk_s}.{make_oid_slug(m_col)}".lower() for m_col, _, _, _ in meta_defs
            }
            used_oids_lower: set[str] = {"itemgroupdataseq"}
            used_labels_lower: set[str] = {"record identifier"}
            for m_col, m_lbl, m_type, m_kseq in active_meta_defs:
                m_oid = f"IT.{fk_s}.{make_oid_slug(m_col)}"
                used_oids_lower.add(m_oid.lower())
                used_labels_lower.add(m_lbl.lower())
                items_out.append(
                    DatasetJsonItem(
                        OID=m_oid,
                        name=m_col,
                        label=m_lbl,
                        type=m_type,  # type: ignore[arg-type]
                        keySequence=m_kseq,
                    )
                )

            for vn in discovered_vars:
                target_col = col_name_for_var[vn]
                v_info = form_vmeta.get(vn) or global_var_meta.get(vn)
                raw_lbl = v_info[0] if v_info else vn
                item_lbl = raw_lbl
                if item_lbl.lower() in used_labels_lower:
                    cand_lbl = f"{raw_lbl} ({vn})" if vn and vn != raw_lbl else f"{raw_lbl}_2"
                    lbl_idx = 2
                    while cand_lbl.lower() in used_labels_lower:
                        lbl_idx += 1
                        cand_lbl = f"{raw_lbl}_{lbl_idx}"
                    item_lbl = cand_lbl
                used_labels_lower.add(item_lbl.lower())

                slug = make_oid_slug(vn)
                base_oid = f"IT.{fk_s}.{slug}"
                if base_oid.lower() in metadata_oids_lower:
                    var_prefix = "VAR_" if naming_convention == "sdtm" else "var_"
                    base_oid = f"IT.{fk_s}.{var_prefix}{slug}"
                item_oid = base_oid
                oid_idx = 2
                while item_oid.lower() in used_oids_lower:
                    item_oid = f"{base_oid}_{oid_idx}"
                    oid_idx += 1
                used_oids_lower.add(item_oid.lower())

                col_vals = [r.get(target_col) for r in row_dicts]
                declared_type: DatasetJsonItemType = (
                    v_info[1] if v_info else _infer_dataset_json_type_from_values(col_vals)
                )
                if any(v == MASKED_SENTINEL for v in col_vals):
                    declared_type = "string"
                elif row_dicts and declared_type in {"integer", "float", "boolean"}:
                    # Fallback to string if actual values are non-coercible strings
                    inferred = _infer_dataset_json_type_from_values(col_vals)
                    if inferred == "string" and any(v is not None for v in col_vals):
                        declared_type = "string"
                items_out.append(
                    DatasetJsonItem(
                        OID=item_oid,
                        name=target_col,
                        label=item_lbl,
                        type=declared_type,
                    )
                )

            tuple_rows = tuple(
                (
                    row_idx,
                    *(_coerce_dataset_json_cell(r.get(it.name), it.type) for it in items_out[1:]),
                )
                for row_idx, r in enumerate(row_dicts, 1)
            )
            ig_oid = f"IG.{make_oid_slug(fk_s)}"
            item_groups[ig_oid] = DatasetJsonItemGroup(
                records=len(tuple_rows),
                name=fk_s,
                label=form_name_by_key.get(fk_s, fk_s),
                items=tuple(items_out),
                itemData=tuple_rows,
            )

    return DatasetJsonEnvelope(
        creationDateTime=resolved_creation_dt,
        datasetJSONVersion="1.1.0",
        fileOID=file_oid or f"DSJ.{make_oid_slug(study_key)}",
        originator="imednet-toolkit",
        sourceSystem="imednet",
        sourceSystemVersion=source_system_version,
        clinicalData=DatasetJsonClinicalData(
            studyOID=study_key,
            metaDataVersionOID=metadata_version_oid,
            itemGroupData=dict(sorted(item_groups.items())),
        ),
    )


def to_dataset_json(envelope: DatasetJsonEnvelope, *, indent: int = 2) -> str:
    """Serialize a :class:`DatasetJsonEnvelope` to deterministic UTF-8 JSON text."""
    return json.dumps(envelope.to_json_dict(), indent=indent, ensure_ascii=False) + "\n"


def to_dataset_json_file(
    envelope: DatasetJsonEnvelope,
    file_path: Path | str,
    *,
    indent: int = 2,
) -> None:
    """Write a :class:`DatasetJsonEnvelope` deterministically to *file_path*."""
    dest = Path(file_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(to_dataset_json(envelope, indent=indent), encoding="utf-8")


def load_dataset_json(source: str | Path | Mapping[str, Any]) -> DatasetJsonEnvelope:
    """Load and validate a CDISC Dataset-JSON v1.1 envelope from JSON text, path, or dict."""
    if isinstance(source, Mapping):
        return DatasetJsonEnvelope.model_validate(dict(source))
    p = Path(source)
    if _is_file_target(source):
        if not _safe_is_file(p):
            raise FileNotFoundError(f"Target file '{source}' does not exist")
        raw_obj = json.loads(p.read_text(encoding="utf-8"))
    elif _safe_is_file(p):
        raw_obj = json.loads(p.read_text(encoding="utf-8"))
    else:
        raw_obj = json.loads(str(source))
    return DatasetJsonEnvelope.model_validate(raw_obj)


def get_dataset_json_schema() -> dict[str, Any]:
    """Load the bundled official CDISC Dataset-JSON v1.1 JSON Schema."""
    return dict(json.loads(DATASET_JSON_110_SCHEMA_PATH.read_text(encoding="utf-8")))


def _cell_matches_dataset_json_type(val: Any, item_type: str) -> bool:
    if val is None:
        return True
    if item_type == "string":
        return isinstance(val, str)
    if item_type == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if item_type in {"float", "double", "decimal"}:
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if item_type == "boolean":
        return isinstance(val, bool)
    return False


def validate_dataset_json(
    target: DatasetJsonEnvelope | Mapping[str, Any] | str | Path,
) -> list[str]:
    """Validate a CDISC Dataset-JSON v1.1 document against JSON Schema and tuple invariants."""
    errors: list[str] = []
    data: Any
    if isinstance(target, DatasetJsonEnvelope):
        data = target.to_json_dict()
    elif isinstance(target, Mapping):
        data = dict(target)
    elif isinstance(target, (str, Path)):
        data, load_err = _parse_json_validation_source(target)
        if load_err is not None:
            return [load_err]
    else:
        return [f"Unsupported target type: {type(target)}"]

    if not isinstance(data, Mapping):
        return ["Dataset-JSON root must be a JSON object"]

    schema = get_dataset_json_schema()
    try:
        import jsonschema

        validator = jsonschema.Draft202012Validator(schema)
        for err in validator.iter_errors(data):
            loc = ".".join(str(part) for part in err.absolute_path)
            errors.append(f"[{loc}] {err.message}" if loc else err.message)
    except ImportError:
        pass

    try:
        envelope = DatasetJsonEnvelope.model_validate(data)
    except ValidationError as val_err:
        for verr in val_err.errors():
            loc = ".".join(str(part) for part in verr.get("loc", ()))
            errors.append(f"[{loc}] {verr.get('msg', 'Validation error')}")
        return errors

    for section_name, container in (
        ("clinicalData", envelope.clinical_data),
        ("referenceData", envelope.reference_data),
    ):
        if container is None:
            continue
        for ig_oid, ig in container.item_group_data.items():
            if ig.items and (
                ig.items[0].name != "ITEMGROUPDATASEQ" or ig.items[0].type != "integer"
            ):
                errors.append(
                    f"[{section_name}.itemGroupData.{ig_oid}.items.0] "
                    "First item in Dataset-JSON v1.1 must be integer 'ITEMGROUPDATASEQ'"
                )
            for r_idx, row in enumerate(ig.item_data):
                for c_idx, (item_def, cell) in enumerate(zip(ig.items, row, strict=True)):
                    if not _cell_matches_dataset_json_type(cell, item_def.type):
                        errors.append(
                            f"[{section_name}.itemGroupData.{ig_oid}.itemData.{r_idx}.{c_idx}] "
                            f"Value {cell!r} does not match declared type '{item_def.type}'"
                        )
    return errors


def validate_odm_xml(source: str | bytes | Path) -> list[str]:
    """Validate an ODM 1.3.2 XML document safely using ``defusedxml`` and ``cdisc-odm-1.3.2.xsd``."""
    xml_bytes: bytes
    if isinstance(source, Path) or (isinstance(source, str) and _is_file_target(source)):
        p = Path(source)
        if not _safe_is_file(p):
            return [f"FileNotFoundError: Target file '{source}' does not exist"]
        xml_bytes = p.read_bytes()
    elif isinstance(source, bytes):
        xml_bytes = source
    elif isinstance(source, str):
        p = Path(source)
        xml_bytes = p.read_bytes() if _safe_is_file(p) else source.encode("utf-8")
    else:
        return [f"Unsupported XML source type: {type(source)}"]

    # Always parse with defusedxml first to block XXE / entity expansion attacks
    try:
        root = DefusedET.fromstring(xml_bytes)
    except DefusedXmlException as exc:
        return [f"DefusedXmlException ({type(exc).__name__}): {exc}"]
    except DefusedET.ParseError as exc:
        return [f"Invalid XML syntax: {exc}"]

    errors: list[str] = []
    if root.tag not in {f"{{{ODM_NS}}}ODM", "ODM"}:
        errors.append(f"Root element must be ODM, got '{root.tag}'")
        return errors

    try:
        from lxml import etree as lxml_etree  # type: ignore[import-untyped]  # nosemgrep

        parser = lxml_etree.XMLParser(resolve_entities=False, no_network=True)
        xsd_doc = lxml_etree.parse(str(ODM_132_XSD_PATH), parser=parser)  # nosemgrep
        xml_schema = lxml_etree.XMLSchema(xsd_doc)
        doc = lxml_etree.fromstring(xml_bytes, parser=parser)  # nosemgrep
        if not xml_schema.validate(doc):
            for log_entry in xml_schema.error_log:
                errors.append(f"[line {log_entry.line}] {log_entry.message}")
    except ImportError:
        pass

    return errors


__all__ = [
    "DATASET_JSON_110_SCHEMA_PATH",
    "DEFAULT_EPOCH_DATETIME",
    "ODM_132_XSD_PATH",
    "UNSCHEDULED_EVENT_OID",
    "build_datadict_from_sdk_metadata",
    "build_dataset_json_envelope",
    "build_odm_archive",
    "get_dataset_json_schema",
    "load_dataset_json",
    "make_location_oid",
    "make_oid_slug",
    "make_study_event_oid",
    "make_user_oid",
    "make_xml_ncname",
    "map_queries_to_annotations",
    "map_revisions_to_audit_records",
    "map_sites_and_users_to_admin_data",
    "normalize_odm_datetime",
    "to_dataset_json",
    "to_dataset_json_file",
    "validate_dataset_json",
    "validate_odm_xml",
]
