"""Builders, PHI redactors, and schema validators for CDISC ODM 1.3.2 and Dataset-JSON v1.1."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, Literal

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from imednet.models.sdtm import (
    _UNSET_PIPELINE,
    build_form_sdtm_domain_map,
    format_iso8601_dtc,
    get_sdtm_domain_variables,
    resolve_form_sdtm_domain,
    transform_records_to_sdtm_rows,
)
from imednet.models.standards import format_variable_name, resolve_sdtm_domain
from imednet.utils.dates import normalize_iso_date
from imednet.utils.security import (
    SensitivityRegistry,
    build_study_sensitivity_registry,
    global_sensitivity_registry,
    mask_clinical_phi,
)
from imednet.utils.tabular import MASKED_SENTINEL

from .emit import (
    ODM_NS,
    _allocate_item_oids,
    _is_cdisc_identifier_sensitive,
    _scrub_clinical_free_text,
)
from .exchange import (
    DatasetJsonBundle,
    DatasetJsonColumn,
    DatasetJsonEnvelope,
    DatasetJsonItemType,
    DatasetJsonScalar,
    DatasetJsonSourceSystem,
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


def _resolve_toolkit_version() -> str:
    """Resolve the installed ``imednet`` package version with a deterministic fallback."""
    try:
        return _pkg_version("imednet")
    except PackageNotFoundError:
        return "0.1.0"


def _mask_scalar_if_sensitive(
    value: Any,
    *,
    var_name: str,
    item_oid: str | None = None,
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
    if _is_cdisc_identifier_sensitive(active_reg, var_name, item_oid, label=label):
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
        var_is_sensitive = bool(
            mask_phi and var_name and _is_cdisc_identifier_sensitive(reg, var_name)
        )
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
                if var_is_sensitive:
                    comment_str = MASKED_SENTINEL
                elif mask_phi:
                    comment_str = _scrub_clinical_free_text(raw_combined, registry=reg)
                else:
                    comment_str = raw_combined
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
            if var_is_sensitive:
                comment_str = MASKED_SENTINEL
            elif mask_phi:
                comment_str = _scrub_clinical_free_text(raw_desc, registry=reg)
            else:
                comment_str = raw_desc
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
    file_type: Literal["Snapshot", "Transactional"] = "Snapshot",
    creation_datetime: str | None = None,
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
    include_deleted: bool = False,
) -> OdmArchive:
    """Build a complete, deterministic CDISC ODM 1.3.2 :class:`OdmArchive` snapshot."""
    reg = build_study_sensitivity_registry(custom_phi_keys)
    allow_remove = file_type == "Transactional"
    records_list = [
        rec
        for rec in records
        if (include_deleted and allow_remove) or not bool(_attr_or_key(rec, "deleted"))
    ]
    forms_list = list(forms)
    variables_list = list(variables)
    intervals_list = list(intervals)
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
        forms=forms_list,
        variables=variables_list,
        intervals=intervals_list,
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
    for raw_f in forms_list:
        fid = _attr_or_key(raw_f, "form_id", "formId")
        fk = _attr_or_key(raw_f, "form_key", "formKey")
        if isinstance(fid, int) and not isinstance(fid, bool) and fk:
            form_id_to_key[fid] = str(fk)

    extra_forms: list[Form] = []
    discovered_vars_by_fk: dict[str, set[str]] = {}
    for rec in records_list:
        rfk = _attr_or_key(rec, "form_key", "formKey")
        rfid = _attr_or_key(rec, "form_id", "formId")
        fk_resolved = (
            str(rfk).strip()
            if rfk
            else (form_id_to_key.get(rfid, f"FORM_{rfid}") if rfid is not None else "FORM_DEFAULT")
        )
        rec_data = _attr_or_key(rec, "record_data", "recordData")
        if isinstance(rec_data, Mapping):
            discovered_vars_by_fk.setdefault(fk_resolved, set()).update(
                str(k) for k in rec_data if str(k).strip()
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
    ig_oid_by_form_key: dict[str, str] = {}
    for f_idx, f_model in enumerate(sorted_forms):
        preserved_ig = f_model.extraAttributes.get("itemGroupOID")
        ig_oid_by_form_key[f_model.formKey] = (
            preserved_ig.strip()
            if isinstance(preserved_ig, str) and preserved_ig.strip()
            else f"IG_{f_model.formKey}"
        )
        for v_idx, v_model in enumerate(sorted_vars_by_form[f_idx]):
            item_oid_lookup[(f_model.formKey, v_model.variableName)] = (
                oid_by_occ[(f_idx, v_idx)],
                v_model.label,
            )

    # Interval ID & Visit ID -> StudyEventOID lookup
    interval_oid_by_id: dict[int, str] = {}
    for iv in intervals_list:
        iv_id = _attr_or_key(iv, "interval_id", "intervalId")
        iv_name = _attr_or_key(iv, "interval_name", "intervalName")
        if isinstance(iv_id, int) and not isinstance(iv_id, bool):
            interval_oid_by_id[iv_id] = make_study_event_oid(iv_name or iv_id)

    visit_oid_by_id: dict[int, tuple[str, str | None]] = {}
    for vis in visits_list:
        vid = _attr_or_key(vis, "visit_id", "visitId")
        iv_name = _attr_or_key(vis, "interval_name", "intervalName")
        iv_id = _attr_or_key(vis, "interval_id", "intervalId")
        if isinstance(vid, int) and not isinstance(vid, bool):
            resolved_se = (
                make_study_event_oid(iv_name)
                if iv_name
                else (
                    interval_oid_by_id[iv_id]
                    if isinstance(iv_id, int) and iv_id in interval_oid_by_id
                    else make_study_event_oid(iv_id or vid)
                )
            )
            visit_oid_by_id[vid] = (resolved_se, str(vid))

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
        custom_phi_keys=custom_phi_keys,
    )
    default_loc_oid = admin_data.locations[0].oid

    audits_by_rec, all_audit_records = map_revisions_to_audit_records(
        revisions_list,
        default_location_oid=default_loc_oid,
        site_oid_by_record_id=site_oid_by_record_id,
        mask_phi=mask_phi,
        custom_phi_keys=custom_phi_keys,
    )
    ann_by_rec_var, ann_by_rec, ann_by_subj, unattached_anns = map_queries_to_annotations(
        queries_list,
        mask_phi=mask_phi,
        custom_phi_keys=custom_phi_keys,
    )

    # Group records deterministically under StudyEventData (wrapping visitless forms in SE.UNSCHEDULED):
    # Subject -> (StudyEventOID, RepeatKey) -> FormOID -> [ItemGroupData]
    grouped_clinical: dict[
        str, dict[tuple[str, str | None], dict[str, list[OdmItemGroupData]]]
    ] = {}
    referenced_event_oids: set[str] = set()

    def _has_event_binding(vid_val: Any, iv_val: Any) -> bool:
        if isinstance(vid_val, int) and not isinstance(vid_val, bool) and vid_val > 0:
            return True
        return iv_val is not None and str(iv_val).strip() not in {"", "0", "-1"}

    def _resolve_interval_event_oid(r: Any) -> str | None:
        iv_name_v = _attr_or_key(r, "interval_name", "intervalName")
        if iv_name_v is not None and str(iv_name_v).strip():
            return make_study_event_oid(iv_name_v)
        iv_id_v = _attr_or_key(r, "interval_id", "intervalId")
        if isinstance(iv_id_v, int) and not isinstance(iv_id_v, bool) and iv_id_v > 0:
            return interval_oid_by_id.get(iv_id_v) or make_study_event_oid(iv_id_v)
        if iv_id_v is not None and str(iv_id_v).strip() not in {"", "0", "-1"}:
            return make_study_event_oid(iv_id_v)
        return None

    def _rec_sort_key(r: Any) -> tuple[str, str, str, int, str]:
        sk_v = str(
            _attr_or_key(r, "subject_key", "subjectKey", "subject_id", "subjectId") or "UNKNOWN"
        )
        vid_v = _attr_or_key(r, "visit_id", "visitId")
        iv_v = _attr_or_key(r, "interval_name", "intervalName", "interval_id", "intervalId")
        explicit_se_v = _attr_or_key(r, "study_event_oid", "studyEventOid")
        if explicit_se_v and str(explicit_se_v).strip():
            se_v = str(explicit_se_v).strip()
        elif _has_event_binding(vid_v, iv_v):
            if isinstance(vid_v, int) and not isinstance(vid_v, bool) and vid_v in visit_oid_by_id:
                se_v = visit_oid_by_id[vid_v][0]
            else:
                se_v = _resolve_interval_event_oid(r) or make_study_event_oid(vid_v)
        else:
            se_v = UNSCHEDULED_EVENT_OID
        fk_v = str(_attr_or_key(r, "form_key", "formKey") or "")
        rid_v = _attr_or_key(r, "record_id", "recordId")
        rid_int = int(rid_v) if isinstance(rid_v, int) and not isinstance(rid_v, bool) else 0
        roid_v = str(_attr_or_key(r, "record_oid", "recordOid") or "")
        return (sk_v, se_v, fk_v, rid_int, roid_v)

    for rec in sorted(records_list, key=_rec_sort_key):
        subj_key = str(
            _attr_or_key(rec, "subject_key", "subjectKey", "subject_id", "subjectId") or "UNKNOWN"
        )
        vid = _attr_or_key(rec, "visit_id", "visitId")
        iv_tok = _attr_or_key(rec, "interval_name", "intervalName", "interval_id", "intervalId")
        explicit_se_oid = _attr_or_key(rec, "study_event_oid", "studyEventOid")
        explicit_se_rep = _attr_or_key(rec, "study_event_repeat_key", "studyEventRepeatKey")
        has_event = _has_event_binding(vid, iv_tok)
        se_rep: str | None = None
        if explicit_se_oid and str(explicit_se_oid).strip():
            se_oid = str(explicit_se_oid).strip()
            if explicit_se_rep is not None and str(explicit_se_rep).strip():
                se_rep = str(explicit_se_rep).strip()
            elif isinstance(vid, int) and not isinstance(vid, bool) and vid > 0:
                se_rep = visit_oid_by_id[vid][1] if vid in visit_oid_by_id else str(vid)
        elif has_event:
            if isinstance(vid, int) and not isinstance(vid, bool) and vid > 0:
                if vid in visit_oid_by_id:
                    se_oid, se_rep = visit_oid_by_id[vid]
                else:
                    se_oid = _resolve_interval_event_oid(rec) or make_study_event_oid(vid)
                    se_rep = str(vid)
            else:
                se_oid = _resolve_interval_event_oid(rec) or make_study_event_oid(iv_tok)
                se_rep = None
        else:
            se_oid = UNSCHEDULED_EVENT_OID
            se_rep = None
        referenced_event_oids.add(se_oid)

        rfk = _attr_or_key(rec, "form_key", "formKey")
        rfid = _attr_or_key(rec, "form_id", "formId")
        form_oid = (
            str(rfk).strip()
            if rfk
            else (form_id_to_key.get(rfid, f"FORM_{rfid}") if rfid is not None else "FORM_DEFAULT")
        )
        ig_oid = ig_oid_by_form_key.get(form_oid, f"IG_{form_oid}")
        rid_raw = _attr_or_key(rec, "record_id", "recordId")
        rid_int = (
            int(rid_raw) if isinstance(rid_raw, int) and not isinstance(rid_raw, bool) else None
        )
        roid_raw = _attr_or_key(rec, "record_oid", "recordOid")
        ig_repeat_key = (
            str(rid_int) if rid_int is not None else (str(roid_raw) if roid_raw else None)
        )
        tx_type = "Remove" if (allow_remove and bool(_attr_or_key(rec, "deleted"))) else None

        rec_data_raw = _attr_or_key(rec, "record_data", "recordData")
        rec_data_map = dict(rec_data_raw) if isinstance(rec_data_raw, Mapping) else {}

        # Also include any queried variable on this record as IsNull="Yes" if absent from rec_data_map
        queried_vars_for_rec = (
            {vn for (q_rid, vn) in ann_by_rec_var if q_rid == rid_int}
            if rid_int is not None
            else set()
        )
        all_var_names = sorted(set(rec_data_map.keys()) | queried_vars_for_rec)

        rec_audits = audits_by_rec.get(rid_int, []) if rid_int is not None else []
        latest_audit_id = rec_audits[-1].id if rec_audits else None

        item_objs: list[OdmItemData] = []
        for var_name in all_var_names:
            item_oid, var_label = item_oid_lookup.get((form_oid, var_name), (var_name, None))
            raw_val = rec_data_map.get(var_name)
            masked_val = _mask_scalar_if_sensitive(
                raw_val,
                var_name=var_name,
                item_oid=item_oid,
                label=var_label,
                mask_phi=mask_phi,
                registry=reg,
            )
            val_str, is_null = _format_odm_item_value(masked_val)
            raw_var_anns = (
                ann_by_rec_var.get((rid_int, var_name), ()) if rid_int is not None else ()
            )
            latest_ann_id = raw_var_anns[-1].id if raw_var_anns else None
            var_anns = tuple(ann.model_copy(update={"id": None}) for ann in raw_var_anns)
            item_objs.append(
                OdmItemData(
                    itemOID=item_oid,
                    value=val_str,
                    isNull=is_null,
                    auditRecordID=latest_audit_id,
                    annotationID=latest_ann_id,
                    annotations=var_anns,
                )
            )

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
        grouped_clinical.setdefault(subj_key, {}).setdefault((se_oid, se_rep), {}).setdefault(
            form_oid, []
        ).append(ig_data)

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
        subj_anns = tuple(
            ann.model_copy(update={"id": None}) for ann in ann_by_subj.get(subj_key, ())
        )
        subject_models.append(
            OdmSubjectData(
                subjectKey=subj_key,
                siteLocationOID=subj_site_oid.get(subj_key, default_loc_oid),
                annotations=subj_anns,
                studyEvents=tuple(se_models),
                forms=(),
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

    return OdmArchive(
        fileOID=file_oid or f"ODM.{make_oid_slug(study_key)}",
        fileType=file_type,
        odmVersion="1.3.2",
        creationDateTime=resolved_creation_dt,
        originator="imednet-toolkit",
        sourceSystem="imednet",
        metaDataVersionOID=metadata_version_oid,
        metaDataVersionName=metadata_version_name or "Study Metadata and Clinical Archive",
        dataDictionary=dd,
        adminData=(admin_data,),
        referenceData=tuple(reference_data),
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
        return (
            MASKED_SENTINEL
            if target_type in {"string", "date", "time", "datetime", "URI"}
            else None
        )

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


def _run_enrichment_on_record_data(
    pipeline: Any,
    rdata: dict[str, Any],
    *,
    mask_phi: bool,
    form_key: str | None = None,
) -> dict[str, Any]:
    """Invoke ``pipeline.process`` with form context while supporting simple custom pipelines."""
    if pipeline is None or not callable(getattr(pipeline, "process", None)):
        return rdata
    try:
        processed = pipeline.process(rdata, mask_phi=mask_phi, form_key=form_key)
    except TypeError:
        try:
            processed = pipeline.process(rdata, mask_phi=mask_phi)
        except TypeError:
            processed = pipeline.process(rdata)
    return dict(processed) if isinstance(processed, Mapping) else rdata


def build_dataset_json_bundle(
    study_key: str,
    *,
    records: Iterable[Any] = (),
    forms: Sequence[Any] = (),
    variables: Sequence[Any] = (),
    visits: Iterable[Any] = (),
    data_dictionary: DataDictionary | None = None,
    as_sdtm: bool = False,
    sdtm_domain: str | None = None,
    form_key: str | None = None,
    use_labels_as_columns: bool = False,
    variable_whitelist: Collection[str] | None = None,
    form_whitelist: Collection[int] | None = None,
    naming_convention: str | None = None,
    preserve_record_id: bool = False,
    include_deleted: bool = False,
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
    enrichment_pipeline: Any = _UNSET_PIPELINE,
    metadata_version_oid: str = "MDV.001",
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    source_system_version: str | None = None,
    seq_counters: dict[str, int] | None = None,
    domain_seq_counters: dict[str, dict[str, int]] | None = None,
    seq_overrides: Mapping[str, int] | None = None,
    lineage_out: dict[str, dict[str, Any]] | None = None,
) -> DatasetJsonBundle:
    """Build a multi-dataset :class:`DatasetJsonBundle` of flat CDISC Dataset-JSON v1.1 envelopes."""
    if enrichment_pipeline is _UNSET_PIPELINE:
        from imednet.integrations.enrichment import resolve_enrichment_pipeline

        effective_pipeline = resolve_enrichment_pipeline(study_key)
    else:
        effective_pipeline = enrichment_pipeline

    merged_phi: set[str] = set(custom_phi_keys) if custom_phi_keys is not None else set()
    pipe_phi = getattr(effective_pipeline, "phi_keys", None)
    if isinstance(pipe_phi, Iterable) and not isinstance(pipe_phi, (str, bytes)):
        merged_phi.update(str(k) for k in pipe_phi)
    reg = build_study_sensitivity_registry(merged_phi or None)

    effective_sdtm = bool(as_sdtm or sdtm_domain)
    allowed_form_ids = set(form_whitelist) if form_whitelist is not None else None
    allowed_vars = set(variable_whitelist) if variable_whitelist is not None else None
    target_form_key = form_key.strip() if isinstance(form_key, str) and form_key.strip() else None

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
                            "itemOID": dv.extraAttributes.get("itemOID"),
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
    src_system = DatasetJsonSourceSystem(
        name="imednet",
        version=source_system_version or _resolve_toolkit_version(),
    )

    form_name_by_key: dict[str, str] = {}
    form_key_by_id: dict[int, str] = {}
    form_obj_by_key: dict[str, Any] = {}
    form_ig_oid_by_key: dict[str, str] = {}
    for raw_f in forms:
        fk = _attr_or_key(raw_f, "form_key", "formKey")
        fid = _attr_or_key(raw_f, "form_id", "formId")
        fname = _attr_or_key(raw_f, "form_name", "formName")
        f_extra = _attr_or_key(raw_f, "extraAttributes", "extra_attributes")
        if fk:
            fk_s = str(fk).strip()
            form_obj_by_key[fk_s] = raw_f
            form_name_by_key[fk_s] = str(fname).strip() if fname else fk_s
            if isinstance(f_extra, Mapping):
                pres_ig = f_extra.get("itemGroupOID")
                if isinstance(pres_ig, str) and pres_ig.strip():
                    form_ig_oid_by_key[fk_s] = pres_ig.strip()
            if isinstance(fid, int) and not isinstance(fid, bool):
                form_key_by_id[fid] = fk_s

    var_meta_by_form: dict[
        str, dict[str, tuple[str, DatasetJsonItemType, int | None, str | None]]
    ] = {}
    global_var_meta: dict[str, tuple[str, DatasetJsonItemType, int | None, str | None]] = {}
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
        raw_item_oid = _attr_or_key(raw_v, "itemOID", "item_oid")
        if raw_item_oid is None:
            v_extra = _attr_or_key(raw_v, "extraAttributes", "extra_attributes")
            if isinstance(v_extra, Mapping):
                raw_item_oid = v_extra.get("itemOID")
        pres_item_oid = (
            str(raw_item_oid).strip()
            if isinstance(raw_item_oid, str) and raw_item_oid.strip()
            else None
        )
        info = (vlbl, vtype, vseq, pres_item_oid)
        global_var_meta.setdefault(vn_s, info)
        if fk_resolved:
            var_meta_by_form.setdefault(fk_resolved, {})[vn_s] = info

    datasets: dict[str, DatasetJsonEnvelope] = {}

    if effective_sdtm:
        study_cfg = getattr(effective_pipeline, "config", None)
        form_domain_map = build_form_sdtm_domain_map(
            forms,
            study_config=study_cfg,
            study_key=study_key,
        )

        def _resolve_record_sdtm_domain(rec_item: Any, *, default_dom: str) -> str:
            rfk_v = _attr_or_key(rec_item, "form_key", "formKey")
            rfid_v = _attr_or_key(rec_item, "form_id", "formId")
            fk_cand = (
                str(rfk_v).strip()
                if rfk_v
                else (form_key_by_id.get(rfid_v, "") if isinstance(rfid_v, int) else "")
            )
            form_ref = form_obj_by_key.get(fk_cand)
            if form_ref is None and (fk_cand or rfid_v is not None):
                form_ref = {
                    "formKey": fk_cand or None,
                    "formId": rfid_v,
                    "formName": form_name_by_key.get(fk_cand),
                }
            if form_ref is None:
                return default_dom
            resolved_dom = resolve_form_sdtm_domain(
                form_ref,
                study_config=study_cfg,
                study_key=study_key,
                form_domain_map=form_domain_map,
                fallback="",
            )
            if resolved_dom:
                return resolved_dom
            return make_oid_slug(fk_cand, default_dom).upper() if fk_cand else default_dom

        target_domains: list[str] = []
        records_by_domain: dict[str, list[Any]] = {}
        if sdtm_domain:
            dom_code = resolve_sdtm_domain(sdtm_domain) or sdtm_domain.strip().upper()
            target_domains.append(dom_code)
            for rec in filtered_records:
                rec_dom = _resolve_record_sdtm_domain(rec, default_dom="")
                if not rec_dom or rec_dom == dom_code:
                    records_by_domain.setdefault(dom_code, []).append(rec)
        else:
            for rec in filtered_records:
                dom_code = _resolve_record_sdtm_domain(rec, default_dom="DM")
                records_by_domain.setdefault(dom_code, []).append(rec)
            target_domains = sorted(records_by_domain)
            if not target_domains:
                for fk_s, raw_f in sorted(form_obj_by_key.items()):
                    dom_code = (
                        resolve_form_sdtm_domain(
                            raw_f,
                            study_config=study_cfg,
                            study_key=study_key,
                            form_domain_map=form_domain_map,
                            fallback="",
                        )
                        or make_oid_slug(fk_s, "DM").upper()
                    )
                    if dom_code not in target_domains:
                        target_domains.append(dom_code)
                if not target_domains:
                    target_domains = ["DM"]

        ordered_domains = sorted(dict.fromkeys(target_domains))
        multi_ds = len(ordered_domains) > 1
        for dom in ordered_domains:
            dom_recs = records_by_domain.get(dom, [])
            dom_counters = (
                domain_seq_counters.setdefault(dom, {})
                if domain_seq_counters is not None
                else seq_counters
            )
            sdtm_rows = transform_records_to_sdtm_rows(
                dom_recs,
                domain=dom,
                study_key=study_key,
                include_deleted=include_deleted,
                mask_phi=mask_phi,
                seq_counters=dom_counters,
                seq_overrides=seq_overrides,
                lineage_out=lineage_out,
                phi_keys=merged_phi or None,
                registry=reg,
                enrichment_pipeline=effective_pipeline,
                preserve_record_id=preserve_record_id,
            )
            columns: list[str] = list(get_sdtm_domain_variables(dom))
            if preserve_record_id and "RECORD_ID" not in columns:
                columns.append("RECORD_ID")
            seen_cols = set(columns)
            for r_dict in sdtm_rows:
                for k in r_dict:
                    if k not in seen_cols and k != "ITEMGROUPDATASEQ":
                        seen_cols.add(k)
                        columns.append(k)
            if allowed_vars is not None:
                columns = [c for c in columns if c in allowed_vars]

            processed_rows: list[dict[str, Any]] = []
            for r_dict in sdtm_rows:
                clean_row: dict[str, Any] = {}
                for col in columns:
                    clean_row[col] = _mask_scalar_if_sensitive(
                        r_dict.get(col),
                        var_name=col,
                        item_oid=f"IT.{dom}.{col}",
                        mask_phi=mask_phi,
                        registry=reg,
                        as_sdtm=True,
                    )
                processed_rows.append(clean_row)

            seq_col = f"{dom}SEQ"
            processed_rows.sort(
                key=lambda r: (
                    str(r.get("USUBJID") or ""),
                    int(r[seq_col]) if isinstance(r.get(seq_col), int) else 0,
                    str(r.get("RECORD_ID") or ""),
                )
            )

            col_items: list[DatasetJsonColumn] = []
            key_seq = 0
            key_candidates = {"STUDYID", "USUBJID", seq_col, "RECORD_ID"}
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
                col_items.append(
                    DatasetJsonColumn(
                        itemOID=f"IT.{dom}.{col}",
                        name=col,
                        label=col,
                        dataType=col_type,
                        keySequence=k_seq,
                    )
                )

            tuple_rows = tuple(
                tuple(
                    _coerce_dataset_json_cell(r.get(item_def.name), item_def.data_type)
                    for item_def in col_items
                )
                for r in processed_rows
            )
            ig_oid = f"IG.{dom}"
            dom_label = form_name_by_key.get(dom)
            if not dom_label:
                for fk_s, fn_s in form_name_by_key.items():
                    if form_domain_map.get(fk_s) == dom:
                        dom_label = fn_s
                        break
            if not dom_label:
                dom_label = f"{dom} Domain"

            dom_file_oid = (
                (f"{file_oid}.{dom}" if multi_ds else file_oid)
                if file_oid
                else f"DSJ.{make_oid_slug(study_key)}.{dom}"
            )
            datasets[ig_oid] = DatasetJsonEnvelope(
                datasetJSONCreationDateTime=resolved_creation_dt,
                datasetJSONVersion="1.1.0",
                fileOID=dom_file_oid,
                originator="imednet-toolkit",
                sourceSystem=src_system,
                studyOID=study_key,
                metaDataVersionOID=metadata_version_oid,
                itemGroupOID=ig_oid,
                records=len(tuple_rows),
                name=dom,
                label=dom_label,
                columns=tuple(col_items),
                rows=tuple_rows,
            )
    else:
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
            if target_form_key is not None and fk_s != target_form_key:
                continue
            recs_by_form.setdefault(fk_s, []).append(rec)

        if target_form_key is not None:
            all_form_keys = [target_form_key]
        else:
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
            if not all_form_keys:
                all_form_keys = ["FORM_DEFAULT"]

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

        multi_ds = len(all_form_keys) > 1
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
            form_vmeta = var_meta_by_form.get(fk_s, {})

            # Run EnrichmentPipeline BEFORE discovering CDASH variables so mapped target variables appear
            enriched_records: list[tuple[Any, dict[str, Any]]] = []
            mapped_away_sources: set[str] = set()
            for rec in form_recs:
                rdata_raw = _attr_or_key(rec, "record_data", "recordData")
                pre_keys = {str(k) for k in rdata_raw} if isinstance(rdata_raw, Mapping) else set()
                rdata = dict(rdata_raw) if isinstance(rdata_raw, Mapping) else {}
                if effective_pipeline is not None:
                    rdata = _run_enrichment_on_record_data(
                        effective_pipeline,
                        rdata,
                        mask_phi=mask_phi,
                        form_key=fk_s,
                    )
                    post_keys = {str(k) for k in rdata}
                    mapped_away_sources.update(pre_keys - post_keys)
                enriched_records.append((rec, rdata))

            present_post_keys = {
                str(vk) for _, rdata in enriched_records for vk in rdata if str(vk).strip()
            }
            discovered_vars: list[str] = [
                vn
                for vn in sorted(
                    form_vmeta.keys(),
                    key=lambda vn: (
                        0 if form_vmeta[vn][2] is not None else 1,
                        form_vmeta[vn][2] or 0,
                        vn,
                    ),
                )
                if vn not in mapped_away_sources or vn in present_post_keys
            ]
            seen_vars = set(discovered_vars)
            for _, rdata in enriched_records:
                for vk in sorted(rdata.keys(), key=str):
                    vk_s = str(vk)
                    if vk_s and vk_s != "ITEMGROUPDATASEQ" and vk_s not in seen_vars:
                        seen_vars.add(vk_s)
                        discovered_vars.append(vk_s)

            if allowed_vars is not None:
                discovered_vars = [
                    vn
                    for vn in discovered_vars
                    if vn in allowed_vars
                    or (
                        naming_convention
                        and format_variable_name(vn, naming_convention) in allowed_vars
                    )
                ]

            row_dicts: list[dict[str, Any]] = []
            col_name_for_var: dict[str, str] = {}
            for vn in discovered_vars:
                v_info = form_vmeta.get(vn) or global_var_meta.get(vn)
                lbl = v_info[0] if v_info else vn
                if use_labels_as_columns and lbl:
                    col_name_for_var[vn] = lbl
                elif naming_convention:
                    col_name_for_var[vn] = format_variable_name(vn, naming_convention)
                else:
                    col_name_for_var[vn] = vn

            for rec, rdata in enriched_records:
                row_map: dict[str, Any] = {
                    meta_defs[0][0]: _attr_or_key(rec, "study_key", "studyKey") or study_key,
                    meta_defs[1][0]: _attr_or_key(rec, "subject_key", "subjectKey"),
                    meta_defs[2][0]: _attr_or_key(rec, "record_id", "recordId"),
                    meta_defs[3][0]: _attr_or_key(rec, "visit_id", "visitId"),
                    meta_defs[4][0]: fk_s,
                }
                for vn in discovered_vars:
                    v_info = form_vmeta.get(vn) or global_var_meta.get(vn)
                    lbl = v_info[0] if v_info else vn
                    pres_oid = v_info[3] if v_info else None
                    item_oid = pres_oid or f"IT.{fk_s}.{make_oid_slug(vn)}"
                    target_col = col_name_for_var[vn]
                    raw_val = rdata.get(vn)
                    row_map[target_col] = _mask_scalar_if_sensitive(
                        raw_val,
                        var_name=vn,
                        item_oid=item_oid,
                        label=lbl,
                        mask_phi=mask_phi,
                        registry=reg,
                    )
                row_dicts.append(row_map)

            items_out: list[DatasetJsonColumn] = []
            used_col_names: set[str] = set()
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
                used_col_names.add(m_col)
                items_out.append(
                    DatasetJsonColumn(
                        itemOID=f"IT.{fk_s}.{make_oid_slug(m_col)}",
                        name=m_col,
                        label=m_lbl,
                        dataType=m_type,  # type: ignore[arg-type]
                        keySequence=m_kseq,
                    )
                )

            for vn in discovered_vars:
                target_col = col_name_for_var[vn]
                if target_col in used_col_names or target_col == "ITEMGROUPDATASEQ":
                    continue
                used_col_names.add(target_col)
                v_info = form_vmeta.get(vn) or global_var_meta.get(vn)
                lbl = v_info[0] if v_info else vn
                pres_oid = v_info[3] if v_info else None
                col_vals = [r.get(target_col) for r in row_dicts]
                declared_type: DatasetJsonItemType = (
                    v_info[1] if v_info else _infer_dataset_json_type_from_values(col_vals)
                )
                if any(v == MASKED_SENTINEL for v in col_vals):
                    declared_type = "string"
                elif row_dicts and declared_type in {"integer", "float", "boolean"}:
                    inferred = _infer_dataset_json_type_from_values(col_vals)
                    if inferred == "string" and any(v is not None for v in col_vals):
                        declared_type = "string"
                items_out.append(
                    DatasetJsonColumn(
                        itemOID=pres_oid or f"IT.{fk_s}.{make_oid_slug(vn)}",
                        name=target_col,
                        label=lbl,
                        dataType=declared_type,
                    )
                )

            tuple_rows = tuple(
                tuple(_coerce_dataset_json_cell(r.get(it.name), it.data_type) for it in items_out)
                for r in row_dicts
            )
            ig_oid = form_ig_oid_by_key.get(fk_s, f"IG.{make_oid_slug(fk_s)}")
            form_slug = make_oid_slug(fk_s)
            form_file_oid = (
                (f"{file_oid}.{form_slug}" if multi_ds else file_oid)
                if file_oid
                else f"DSJ.{make_oid_slug(study_key)}.{form_slug}"
            )
            datasets[ig_oid] = DatasetJsonEnvelope(
                datasetJSONCreationDateTime=resolved_creation_dt,
                datasetJSONVersion="1.1.0",
                fileOID=form_file_oid,
                originator="imednet-toolkit",
                sourceSystem=src_system,
                studyOID=study_key,
                metaDataVersionOID=metadata_version_oid,
                itemGroupOID=ig_oid,
                records=len(tuple_rows),
                name=fk_s,
                label=form_name_by_key.get(fk_s, fk_s),
                columns=tuple(items_out),
                rows=tuple_rows,
            )

    return DatasetJsonBundle(
        studyOID=study_key,
        metaDataVersionOID=metadata_version_oid,
        datasetJSONCreationDateTime=resolved_creation_dt,
        datasets=dict(sorted(datasets.items())),
    )


def build_dataset_json_envelope(
    study_key: str,
    *,
    records: Iterable[Any] = (),
    forms: Sequence[Any] = (),
    variables: Sequence[Any] = (),
    visits: Iterable[Any] = (),
    data_dictionary: DataDictionary | None = None,
    as_sdtm: bool = False,
    sdtm_domain: str | None = None,
    form_key: str | None = None,
    use_labels_as_columns: bool = False,
    variable_whitelist: Collection[str] | None = None,
    form_whitelist: Collection[int] | None = None,
    naming_convention: str | None = None,
    preserve_record_id: bool = False,
    include_deleted: bool = False,
    mask_phi: bool = True,
    custom_phi_keys: Collection[str] | None = None,
    enrichment_pipeline: Any = _UNSET_PIPELINE,
    metadata_version_oid: str = "MDV.001",
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    source_system_version: str | None = None,
    seq_counters: dict[str, int] | None = None,
    domain_seq_counters: dict[str, dict[str, int]] | None = None,
    seq_overrides: Mapping[str, int] | None = None,
    lineage_out: dict[str, dict[str, Any]] | None = None,
) -> DatasetJsonEnvelope:
    """Build a flat CDISC Dataset-JSON v1.1 single-dataset envelope."""
    bundle = build_dataset_json_bundle(
        study_key,
        records=records,
        forms=forms,
        variables=variables,
        visits=visits,
        data_dictionary=data_dictionary,
        as_sdtm=as_sdtm,
        sdtm_domain=sdtm_domain,
        form_key=form_key,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        naming_convention=naming_convention,
        preserve_record_id=preserve_record_id,
        include_deleted=include_deleted,
        mask_phi=mask_phi,
        custom_phi_keys=custom_phi_keys,
        enrichment_pipeline=enrichment_pipeline,
        metadata_version_oid=metadata_version_oid,
        file_oid=file_oid,
        creation_datetime=creation_datetime,
        source_system_version=source_system_version,
        seq_counters=seq_counters,
        domain_seq_counters=domain_seq_counters,
        seq_overrides=seq_overrides,
        lineage_out=lineage_out,
    )
    if sdtm_domain:
        dom_code = resolve_sdtm_domain(sdtm_domain) or sdtm_domain.strip().upper()
        if dom_code in bundle:
            selected = bundle[dom_code]
            return selected.model_copy(update={"file_oid": file_oid}) if file_oid else selected
    if form_key and form_key in bundle:
        selected = bundle[form_key]
        return selected.model_copy(update={"file_oid": file_oid}) if file_oid else selected

    non_empty = [env for env in bundle.values() if env.records > 0]
    selected = non_empty[0] if len(non_empty) == 1 else next(iter(bundle.values()))
    if file_oid and selected.file_oid != file_oid:
        return selected.model_copy(update={"file_oid": file_oid})
    return selected


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
    if item_type in {"string", "date", "time", "datetime", "URI"}:
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
    """Validate a flat CDISC Dataset-JSON v1.1 document against JSON Schema and tuple invariants."""
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

    if "clinicalData" in data or "referenceData" in data:
        errors.append(
            "Legacy Dataset-JSON v1.0 'clinicalData'/'referenceData' nesting is not valid in flat Dataset-JSON v1.1"
        )

    raw_cols = data.get("columns")
    if isinstance(raw_cols, Sequence) and not isinstance(raw_cols, (str, bytes)):
        for c_idx, raw_col in enumerate(raw_cols):
            if isinstance(raw_col, Mapping) and ("OID" in raw_col or "type" in raw_col):
                errors.append(
                    f"[columns.{c_idx}] Legacy v1.0 column keys ('OID'/'type') must use v1.1 'itemOID' and 'dataType'"
                )

    schema = get_dataset_json_schema()
    try:
        import jsonschema

        validator = jsonschema.Draft202012Validator(schema)
        for err in validator.iter_errors(data):
            loc = ".".join(str(part) for part in err.absolute_path)
            errors.append(f"[{loc}] {err.message}" if loc else err.message)
    except ImportError:
        pass

    if errors:
        return errors

    try:
        envelope = DatasetJsonEnvelope.model_validate(data)
    except ValidationError as val_err:
        for verr in val_err.errors():
            loc = ".".join(str(part) for part in verr.get("loc", ()))
            errors.append(f"[{loc}] {verr.get('msg', 'Validation error')}")
        return errors

    seen_col_names: set[str] = set()
    seen_item_oids: set[str] = set()
    for c_idx, col_def in enumerate(envelope.columns):
        if col_def.name in seen_col_names:
            errors.append(f"[columns.{c_idx}.name] Duplicate column name '{col_def.name}'")
        seen_col_names.add(col_def.name)
        if col_def.item_oid in seen_item_oids:
            errors.append(f"[columns.{c_idx}.itemOID] Duplicate itemOID '{col_def.item_oid}'")
        seen_item_oids.add(col_def.item_oid)

    for r_idx, row in enumerate(envelope.rows):
        for c_idx, (col_def, cell) in enumerate(zip(envelope.columns, row, strict=True)):
            if not _cell_matches_dataset_json_type(cell, col_def.data_type):
                errors.append(
                    f"[rows.{r_idx}.{c_idx}] "
                    f"Value {cell!r} does not match declared dataType '{col_def.data_type}'"
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

    odm_file_type = root.attrib.get("FileType", "Snapshot")
    if odm_file_type == "Snapshot":
        for elem in root.iter():
            if elem.attrib.get("TransactionType") == "Remove":
                local_name = elem.tag.rsplit("}", 1)[-1]
                errors.append(
                    f'TransactionType="Remove" on <{local_name}> is not permitted when ODM FileType="Snapshot"'
                )

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
    "build_dataset_json_bundle",
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
