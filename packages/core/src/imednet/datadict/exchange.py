"""Read-only canonical CDISC ODM 1.3.2 and Dataset-JSON v1.1 exchange models and mappers."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from imednet.utils.security import (
    global_sensitivity_registry,
    mask_clinical_phi,
    sanitize_diagnostic_text,
)

from .models import DataDictionary, Form

MASKED_PHI_SENTINEL = "***MASKED***"

DatasetJsonDataType = Literal[
    "string",
    "integer",
    "float",
    "double",
    "decimal",
    "boolean",
    "date",
    "time",
    "datetime",
    "URI",
    "partialDate",
    "partialTime",
    "partialDatetime",
    "durationDatetime",
]

OdmTransactionType = Literal["Insert", "Update", "Remove", "Upsert", "Context"]
OdmEditPointType = Literal["Monitoring", "DataManagement", "DBAudit"]
OdmUserType = Literal["Sponsor", "Investigator", "Lab", "Other"]
OdmLocationType = Literal["Sponsor", "Site", "CRO", "Lab", "Other"]
OdmCommentSourceType = Literal["Sponsor", "Site"]

_XML_ID_INVALID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_OID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def sanitize_odm_oid(prefix: str, raw: Any) -> str:
    """Produce a deterministic, non-empty ODM OID token from ``raw``."""
    text = str(raw).strip() if raw is not None else ""
    cleaned = _OID_SAFE_RE.sub("_", text).strip("_.")
    if not cleaned:
        cleaned = "UNSPECIFIED"
    if prefix:
        return f"{prefix}.{cleaned}" if not cleaned.startswith(f"{prefix}.") else cleaned
    return cleaned


def sanitize_xml_id(prefix: str, raw: Any) -> str:
    """Produce a valid xs:ID token (starting with an ASCII letter or underscore)."""
    text = str(raw).strip() if raw is not None else ""
    cleaned = _XML_ID_INVALID_RE.sub("_", text).strip("_.")
    if not cleaned:
        cleaned = "0"
    if prefix:
        candidate = cleaned if cleaned.startswith(f"{prefix}.") else f"{prefix}.{cleaned}"
    else:
        candidate = cleaned
    if not (candidate[0].isalpha() or candidate[0] == "_"):
        candidate = f"ID_{candidate}"
    return candidate


def _format_iso_timestamp(val: Any, *, fallback: str | None = None) -> str:
    """Convert a datetime, date, or timestamp string into deterministic ISO-8601 format."""
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            return val.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, str) and val.strip():
        stripped = val.strip()
        try:
            parsed = datetime.fromisoformat(stripped)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
            return parsed.isoformat()
        except ValueError:
            return stripped
    if fallback is not None:
        return fallback
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _format_iso_date(val: Any, *, fallback: str = "1970-01-01") -> str:
    """Convert a date/datetime/string into ``YYYY-MM-DD`` for ``EffectiveDate``."""
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, str) and val.strip():
        stripped = val.strip()
        if len(stripped) >= 10 and stripped[4] == "-" and stripped[7] == "-":
            try:
                return date.fromisoformat(stripped[:10]).isoformat()
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(stripped).date().isoformat()
        except ValueError:
            pass
    return fallback


def _get_field(obj: Any, *names: str) -> Any:
    """Extract an attribute or mapping key from ``obj`` across snake_case and camelCase aliases."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return None
    for name in names:
        val = getattr(obj, name, None)
        if val is not None and not callable(val):
            return val
    return None


def _matches_phi_key(key: str, custom_phi_keys: set[str] | None = None) -> bool:
    """Return True when ``key`` is marked sensitive by global or study-specific PHI rules."""
    if not key:
        return False
    if global_sensitivity_registry.is_sensitive(key):
        return True
    if custom_phi_keys:
        norm = _NON_ALNUM_RE.sub("", key.lower())
        for cand in custom_phi_keys:
            if cand and _NON_ALNUM_RE.sub("", str(cand).lower()) == norm:
                return True
    return False


def _mask_scalar_value(
    key: str,
    value: Any,
    *,
    mask_phi: bool,
    custom_phi_keys: set[str] | None = None,
) -> Any:
    """Mask ``value`` when ``mask_phi`` is enabled and ``key`` or content contains PHI."""
    if not mask_phi or value is None:
        return value
    if _matches_phi_key(key, custom_phi_keys):
        return MASKED_PHI_SENTINEL
    if isinstance(value, str):
        return sanitize_diagnostic_text(value)
    if isinstance(value, (Mapping, list, tuple)):
        return mask_clinical_phi(value)
    return value


class _FrozenExchangeModel(BaseModel):
    """Base read-only model for canonical CDISC exchange envelopes."""

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# CDISC ODM 1.3.2 Exchange Models (Read-Only)
# ---------------------------------------------------------------------------


class OdmComment(_FrozenExchangeModel):
    """ODM ``<Comment>`` child inside ``<Annotation>``."""

    text: str = Field(
        ...,
        validation_alias=AliasChoices("text", "value"),
        serialization_alias="text",
    )
    sponsor_or_site: OdmCommentSourceType | None = Field(default=None, alias="sponsorOrSite")

    @property
    def value(self) -> str:
        """Return the comment text value."""
        return self.text


class OdmFlag(_FrozenExchangeModel):
    """ODM ``<Flag>`` child inside ``<Annotation>``."""

    flag_value: str = Field(..., alias="flagValue")
    flag_value_codelist_oid: str = Field(
        default="CL.FLAG_VALUE",
        alias="flagValueCodeListOid",
    )
    flag_type: str | None = Field(default=None, alias="flagType")
    flag_type_codelist_oid: str | None = Field(default=None, alias="flagTypeCodeListOid")


class OdmAnnotation(_FrozenExchangeModel):
    """ODM ``<Annotation>`` element representing an EDC Query or discrepancy comment."""

    seq_num: int = Field(..., alias="seqNum", ge=1)
    annotation_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("id", "annotation_id", "annotationId"),
        serialization_alias="id",
    )
    transaction_type: OdmTransactionType | None = Field(default=None, alias="transactionType")
    comment: OdmComment | None = None
    flags: tuple[OdmFlag, ...] = ()

    @property
    def id(self) -> str | None:
        """Return the optional XML ID of the annotation."""
        return self.annotation_id


class OdmAuditRecord(_FrozenExchangeModel):
    """ODM ``<AuditRecord>`` element representing an iMednet ``RecordRevision``."""

    user_oid: str = Field(..., alias="userOid", min_length=1)
    location_oid: str = Field(..., alias="locationOid", min_length=1)
    date_time_stamp: str = Field(..., alias="dateTimeStamp", min_length=1)
    reason_for_change: str | None = Field(default=None, alias="reasonForChange")
    source_id: str | None = Field(default=None, alias="sourceId")
    audit_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("id", "audit_id", "auditId"),
        serialization_alias="id",
    )
    edit_point: OdmEditPointType | None = Field(default=None, alias="editPoint")
    used_imputation_method: Literal["Yes", "No"] | None = Field(
        default=None, alias="usedImputationMethod"
    )

    @property
    def id(self) -> str | None:
        """Return the optional XML ID of the audit record."""
        return self.audit_id


class OdmUser(_FrozenExchangeModel):
    """ODM ``<User>`` administrative metadata element."""

    oid: str = Field(..., min_length=1)
    user_type: OdmUserType | None = Field(default=None, alias="userType")
    login_name: str | None = Field(default=None, alias="loginName")
    display_name: str | None = Field(default=None, alias="displayName")
    full_name: str | None = Field(default=None, alias="fullName")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    organization: str | None = None
    email: str | None = None
    phone: str | None = None
    location_oids: tuple[str, ...] = Field(default=(), alias="locationOids")


class OdmMetaDataVersionRef(_FrozenExchangeModel):
    """ODM ``<MetaDataVersionRef>`` child of ``<Location>``."""

    study_oid: str = Field(..., alias="studyOid", min_length=1)
    metadata_version_oid: str = Field(..., alias="metaDataVersionOid", min_length=1)
    effective_date: str = Field(..., alias="effectiveDate", min_length=1)


class OdmLocation(_FrozenExchangeModel):
    """ODM ``<Location>`` administrative metadata element representing a Study Site."""

    oid: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    location_type: OdmLocationType = Field(default="Site", alias="locationType")
    metadata_version_refs: tuple[OdmMetaDataVersionRef, ...] = Field(
        default=(), alias="metaDataVersionRefs"
    )


class OdmSignatureDef(_FrozenExchangeModel):
    """ODM ``<SignatureDef>`` administrative element."""

    oid: str = Field(..., min_length=1)
    meaning: str = Field(..., min_length=1)
    legal_reason: str = Field(..., alias="legalReason", min_length=1)
    methodology: Literal["Digital", "Electronic"] | None = None


class OdmAdminData(_FrozenExchangeModel):
    """ODM ``<AdminData>`` envelope containing ``<User>`` and ``<Location>`` definitions."""

    study_oid: str | None = Field(default=None, alias="studyOid")
    users: tuple[OdmUser, ...] = ()
    locations: tuple[OdmLocation, ...] = ()
    signature_defs: tuple[OdmSignatureDef, ...] = Field(default=(), alias="signatureDefs")


class OdmItemData(_FrozenExchangeModel):
    """ODM ``<ItemData>`` leaf value element."""

    item_oid: str = Field(..., alias="itemOid", min_length=1)
    value: str | None = None
    is_null: bool = Field(default=False, alias="isNull")
    transaction_type: OdmTransactionType | None = Field(default=None, alias="transactionType")
    measurement_unit_oid: str | None = Field(default=None, alias="measurementUnitOid")
    audit_record_id: str | None = Field(default=None, alias="auditRecordId")
    annotation_id: str | None = Field(default=None, alias="annotationId")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()


class OdmItemGroupData(_FrozenExchangeModel):
    """ODM ``<ItemGroupData>`` container for clinical or reference item tuples."""

    item_group_oid: str = Field(..., alias="itemGroupOid", min_length=1)
    item_group_repeat_key: str | None = Field(default=None, alias="itemGroupRepeatKey")
    transaction_type: OdmTransactionType | None = Field(default=None, alias="transactionType")
    audit_record_id: str | None = Field(default=None, alias="auditRecordId")
    annotation_id: str | None = Field(default=None, alias="annotationId")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()
    items: tuple[OdmItemData, ...] = ()


class OdmReferenceData(_FrozenExchangeModel):
    """ODM ``<ReferenceData>`` envelope for study-level / non-subject reference datasets."""

    study_oid: str = Field(..., alias="studyOid", min_length=1)
    metadata_version_oid: str = Field(..., alias="metaDataVersionOid", min_length=1)
    item_group_data: tuple[OdmItemGroupData, ...] = Field(default=(), alias="itemGroupData")


class OdmFormData(_FrozenExchangeModel):
    """ODM ``<FormData>`` container within ``<StudyEventData>``."""

    form_oid: str = Field(..., alias="formOid", min_length=1)
    form_repeat_key: str | None = Field(default=None, alias="formRepeatKey")
    transaction_type: OdmTransactionType | None = Field(default=None, alias="transactionType")
    audit_record_id: str | None = Field(default=None, alias="auditRecordId")
    annotation_id: str | None = Field(default=None, alias="annotationId")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()
    item_group_data: tuple[OdmItemGroupData, ...] = Field(default=(), alias="itemGroupData")


class OdmStudyEventData(_FrozenExchangeModel):
    """ODM ``<StudyEventData>`` container within ``<SubjectData>``."""

    study_event_oid: str = Field(..., alias="studyEventOid", min_length=1)
    study_event_repeat_key: str | None = Field(default=None, alias="studyEventRepeatKey")
    transaction_type: OdmTransactionType | None = Field(default=None, alias="transactionType")
    audit_record_id: str | None = Field(default=None, alias="auditRecordId")
    annotation_id: str | None = Field(default=None, alias="annotationId")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()
    form_data: tuple[OdmFormData, ...] = Field(default=(), alias="formData")


class OdmSubjectData(_FrozenExchangeModel):
    """ODM ``<SubjectData>`` container within ``<ClinicalData>``."""

    subject_key: str = Field(..., alias="subjectKey", min_length=1)
    transaction_type: OdmTransactionType | None = Field(default=None, alias="transactionType")
    site_location_oid: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "siteLocationOid",
            "site_location_oid",
            "siteRefOid",
            "site_ref_oid",
        ),
        serialization_alias="siteLocationOid",
    )
    investigator_user_oid: str | None = Field(default=None, alias="investigatorUserOid")
    audit_record_id: str | None = Field(default=None, alias="auditRecordId")
    annotation_id: str | None = Field(default=None, alias="annotationId")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()
    study_event_data: tuple[OdmStudyEventData, ...] = Field(default=(), alias="studyEventData")

    @property
    def site_ref_oid(self) -> str | None:
        """Return the ``<SiteRef LocationOID='...'/>`` value if present."""
        return self.site_location_oid


class OdmClinicalData(_FrozenExchangeModel):
    """ODM ``<ClinicalData>`` container holding SubjectData, AuditRecords, and Annotations."""

    study_oid: str = Field(..., alias="studyOid", min_length=1)
    metadata_version_oid: str = Field(..., alias="metaDataVersionOid", min_length=1)
    subject_data: tuple[OdmSubjectData, ...] = Field(
        default=(),
        validation_alias=AliasChoices("subjectData", "subject_data", "subjects"),
        serialization_alias="subjectData",
    )
    audit_records: tuple[OdmAuditRecord, ...] = Field(default=(), alias="auditRecords")
    annotations: tuple[OdmAnnotation, ...] = ()

    @property
    def subjects(self) -> tuple[OdmSubjectData, ...]:
        """Return the tuple of ``<SubjectData>`` records."""
        return self.subject_data


class OdmArchive(_FrozenExchangeModel):
    """Canonical read-only CDISC ODM 1.3.2 snapshot/transactional archive envelope."""

    file_oid: str = Field(..., alias="fileOid", min_length=1)
    file_type: Literal["Snapshot", "Transactional"] = Field(default="Snapshot", alias="fileType")
    odm_version: Literal["1.2", "1.3", "1.3.1", "1.3.2"] = Field(
        default="1.3.2", alias="odmVersion"
    )
    creation_date_time: str = Field(..., alias="creationDateTime", min_length=1)
    description: str | None = None
    granularity: (
        Literal[
            "All",
            "Metadata",
            "AdminData",
            "ReferenceData",
            "AllClinicalData",
            "SingleSite",
            "SingleSubject",
        ]
        | None
    ) = None
    archival: Literal["Yes"] | None = None
    prior_file_oid: str | None = Field(default=None, alias="priorFileOid")
    as_of_date_time: str | None = Field(default=None, alias="asOfDateTime")
    originator: str | None = None
    source_system: str | None = Field(default="imednet-toolkit", alias="sourceSystem")
    source_system_version: str | None = Field(default=None, alias="sourceSystemVersion")
    archive_id: str | None = Field(default=None, alias="id")
    data_dictionary: DataDictionary | None = Field(
        default=None,
        validation_alias=AliasChoices("dataDictionary", "data_dictionary", "datadict"),
        serialization_alias="dataDictionary",
    )
    admin_data: tuple[OdmAdminData, ...] = Field(default=(), alias="adminData")
    reference_data: tuple[OdmReferenceData, ...] = Field(default=(), alias="referenceData")
    clinical_data: tuple[OdmClinicalData, ...] = Field(default=(), alias="clinicalData")

    @property
    def datadict(self) -> DataDictionary | None:
        """Return the embedded ``DataDictionary`` metadata if present."""
        return self.data_dictionary


# ---------------------------------------------------------------------------
# CDISC Dataset-JSON v1.1 Exchange Models (Read-Only)
# ---------------------------------------------------------------------------


class DatasetJsonSourceSystem(_FrozenExchangeModel):
    """CDISC Dataset-JSON v1.1 ``sourceSystem`` metadata block."""

    name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)


class DatasetJsonColumn(_FrozenExchangeModel):
    """CDISC Dataset-JSON v1.1 column definition."""

    item_oid: str = Field(..., alias="itemOID", min_length=1)
    name: str = Field(..., min_length=1)
    label: str = ""
    data_type: DatasetJsonDataType = Field(..., alias="dataType")
    target_data_type: Literal["integer", "decimal"] | None = Field(
        default=None, alias="targetDataType"
    )
    length: int | None = Field(default=None, ge=1)
    display_format: str | None = Field(default=None, alias="displayFormat")
    key_sequence: int | None = Field(default=None, alias="keySequence", ge=1)


class DatasetJsonDataset(_FrozenExchangeModel):
    """CDISC Dataset-JSON v1.1.0 single-dataset document (for an ``itemGroupOID``)."""

    dataset_json_creation_date_time: str = Field(
        ..., alias="datasetJSONCreationDateTime", min_length=1
    )
    dataset_json_version: Literal["1.1.0"] = Field(default="1.1.0", alias="datasetJSONVersion")
    file_oid: str | None = Field(default=None, alias="fileOID")
    db_last_modified_date_time: str | None = Field(default=None, alias="dbLastModifiedDateTime")
    originator: str | None = None
    source_system: DatasetJsonSourceSystem | None = Field(default=None, alias="sourceSystem")
    study_oid: str | None = Field(default=None, alias="studyOID")
    metadata_version_oid: str | None = Field(default=None, alias="metaDataVersionOID")
    metadata_ref: str | None = Field(default=None, alias="metaDataRef")
    item_group_oid: str = Field(..., alias="itemGroupOID", min_length=1)
    is_reference_data: bool | None = Field(default=None, alias="isReferenceData")
    records: int = Field(..., ge=0)
    name: str = Field(..., min_length=1)
    label: str = ""
    columns: tuple[DatasetJsonColumn, ...] = Field(..., min_length=1)
    rows: tuple[tuple[str | int | float | bool | None, ...], ...] = ()

    @field_validator("rows", mode="before")
    @classmethod
    def _coerce_rows(cls, v: Any) -> tuple[tuple[Any, ...], ...]:
        if v is None:
            return ()
        out: list[tuple[Any, ...]] = []
        for row in v:
            if isinstance(row, (list, tuple)):
                normalized_cells: list[Any] = []
                for cell in row:
                    if isinstance(cell, float) and (math.isnan(cell) or math.isinf(cell)):
                        normalized_cells.append(None)
                    else:
                        normalized_cells.append(cell)
                out.append(tuple(normalized_cells))
            else:
                raise ValueError("Dataset-JSON rows must be sequences of positional scalar values")
        return tuple(out)

    @field_validator("rows")
    @classmethod
    def _validate_row_widths_and_count(
        cls,
        rows: tuple[tuple[Any, ...], ...],
        info: Any,
    ) -> tuple[tuple[Any, ...], ...]:
        cols = info.data.get("columns")
        if cols is not None:
            expected_len = len(cols)
            for idx, row in enumerate(rows):
                if len(row) != expected_len:
                    raise ValueError(
                        f"Row {idx} has {len(row)} values, expected {expected_len} matching columns"
                    )
        rec_count = info.data.get("records")
        if rec_count is not None and rec_count != len(rows):
            raise ValueError(f"records={rec_count} does not match len(rows)={len(rows)}")
        return rows

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize to an official CDISC Dataset-JSON v1.1.0 dictionary."""
        payload = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload["rows"] = [list(r) for r in self.rows]
        return payload

    def to_json_str(self, *, indent: int | None = 2) -> str:
        """Serialize to a deterministic CDISC Dataset-JSON v1.1.0 JSON string."""
        return json.dumps(self.to_json_dict(), indent=indent, ensure_ascii=False)


DatasetJsonItemGroupData = DatasetJsonDataset


class DatasetJsonClinicalData(_FrozenExchangeModel):
    """Read-only study collection of CDISC Dataset-JSON v1.1 ``itemGroupData`` datasets."""

    study_oid: str = Field(..., alias="studyOID", min_length=1)
    metadata_version_oid: str = Field(default="MDV.001", alias="metaDataVersionOID", min_length=1)
    item_group_data: tuple[DatasetJsonDataset, ...] = Field(default=(), alias="itemGroupData")
    reference_data: tuple[DatasetJsonDataset, ...] = Field(default=(), alias="referenceData")

    @field_validator("item_group_data", "reference_data", mode="before")
    @classmethod
    def _coerce_dataset_collection(cls, v: Any) -> tuple[Any, ...]:
        if v is None:
            return ()
        if isinstance(v, Mapping):
            return tuple(v.values())
        if isinstance(v, Iterable):
            return tuple(v)
        raise ValueError("Dataset collection must be a sequence or mapping of DatasetJsonDataset")

    @property
    def item_groups_by_oid(self) -> dict[str, DatasetJsonDataset]:
        """Return ``item_group_data`` indexed by ``item_group_oid``."""
        return {ds.item_group_oid: ds for ds in self.item_group_data}


# ---------------------------------------------------------------------------
# Canonical Mappers from iMednet Models -> Read-Only ODM 1.3.2 Exchange Models
# ---------------------------------------------------------------------------


def _infer_user_type(role_text: str | None) -> OdmUserType:
    if not role_text:
        return "Other"
    lowered = role_text.lower()
    if "investigator" in lowered or "site" in lowered or "coordinator" in lowered:
        return "Investigator"
    if (
        "sponsor" in lowered
        or "monitor" in lowered
        or "cra" in lowered
        or "data manager" in lowered
    ):
        return "Sponsor"
    if "lab" in lowered:
        return "Lab"
    return "Other"


def build_odm_admin_data(
    study_oid: str,
    *,
    sites: Iterable[Any] = (),
    users: Iterable[Any] = (),
    metadata_version_oid: str = "MDV.001",
    effective_date: str | date | datetime | None = None,
    mask_phi: bool = True,
    custom_phi_keys: set[str] | None = None,
) -> OdmAdminData:
    """Build ``<AdminData>`` from iMednet ``Site`` and ``User`` records with PHI protection."""
    resolved_eff_date = _format_iso_date(effective_date, fallback="1970-01-01")
    site_name_to_oid: dict[str, str] = {}
    site_id_to_oid: dict[str, str] = {}
    locations: list[OdmLocation] = []
    seen_loc_oids: set[str] = set()

    for site in sites:
        site_id = _get_field(site, "site_id", "siteId", "id")
        site_name = _get_field(site, "site_name", "siteName", "name")
        raw_key = site_id if site_id not in (None, "") else site_name
        if raw_key in (None, ""):
            continue
        loc_oid = sanitize_odm_oid("LOC", raw_key)
        if loc_oid in seen_loc_oids:
            continue
        seen_loc_oids.add(loc_oid)
        loc_name = str(site_name).strip() if site_name not in (None, "") else str(raw_key)
        if mask_phi:
            loc_name = str(
                _mask_scalar_value(
                    "site_name", loc_name, mask_phi=mask_phi, custom_phi_keys=custom_phi_keys
                )
            )
        site_created = _get_field(site, "date_created", "dateCreated")
        eff = _format_iso_date(site_created, fallback=resolved_eff_date)
        locations.append(
            OdmLocation(
                oid=loc_oid,
                name=loc_name,
                locationType="Site",
                metaDataVersionRefs=(
                    OdmMetaDataVersionRef(
                        studyOid=study_oid,
                        metaDataVersionOid=metadata_version_oid,
                        effectiveDate=eff,
                    ),
                ),
            )
        )
        if site_id not in (None, ""):
            site_id_to_oid[str(site_id).strip()] = loc_oid
        if site_name not in (None, ""):
            site_name_to_oid[str(site_name).strip()] = loc_oid

    locations.sort(key=lambda loc: loc.oid)

    odm_users: list[OdmUser] = []
    seen_user_oids: set[str] = set()
    for user in users:
        user_id = _get_field(user, "user_id", "userId", "id")
        login = _get_field(user, "login", "login_name", "loginName", "username")
        raw_uid = user_id if user_id not in (None, "") else login
        if raw_uid in (None, ""):
            continue
        u_oid = sanitize_odm_oid("USR", raw_uid)
        if u_oid in seen_user_oids:
            continue
        seen_user_oids.add(u_oid)

        first_name = _get_field(user, "first_name", "firstName")
        last_name = _get_field(user, "last_name", "lastName")
        email = _get_field(user, "email")
        org = _get_field(user, "organization")
        roles = _get_field(user, "roles")

        role_desc: str | None = None
        user_loc_oids: list[str] = []
        if isinstance(roles, Iterable) and not isinstance(roles, (str, bytes, Mapping)):
            for role in roles:
                r_name = _get_field(role, "name", "role_id", "roleId")
                if r_name and role_desc is None:
                    role_desc = str(r_name)
                r_sites = _get_field(role, "sites")
                if isinstance(r_sites, Iterable) and not isinstance(r_sites, (str, bytes)):
                    for rs in r_sites:
                        rs_str = str(rs).strip()
                        matched_loc = site_id_to_oid.get(rs_str) or site_name_to_oid.get(rs_str)
                        if matched_loc and matched_loc not in user_loc_oids:
                            user_loc_oids.append(matched_loc)

        raw_full = " ".join(
            p for p in (str(first_name or "").strip(), str(last_name or "").strip()) if p
        )
        full_name_val: str | None = raw_full if raw_full else None
        first_val = str(first_name).strip() if first_name not in (None, "") else None
        last_val = str(last_name).strip() if last_name not in (None, "") else None
        email_val = str(email).strip() if email not in (None, "") else None
        login_val = str(login).strip() if login not in (None, "") else None

        if mask_phi:
            if first_val is not None:
                first_val = MASKED_PHI_SENTINEL
            if last_val is not None:
                last_val = MASKED_PHI_SENTINEL
            if full_name_val is not None:
                full_name_val = MASKED_PHI_SENTINEL
            if email_val is not None:
                email_val = MASKED_PHI_SENTINEL
            if login_val is not None and (
                "@" in login_val or _matches_phi_key("login_name", custom_phi_keys)
            ):
                login_val = MASKED_PHI_SENTINEL

        odm_users.append(
            OdmUser(
                oid=u_oid,
                userType=_infer_user_type(role_desc),
                loginName=login_val,
                displayName=login_val if login_val else u_oid,
                fullName=full_name_val,
                firstName=first_val,
                lastName=last_val,
                organization=str(org).strip() if org not in (None, "") else None,
                email=email_val,
                locationOids=tuple(sorted(user_loc_oids)),
            )
        )

    odm_users.sort(key=lambda usr: usr.oid)
    return OdmAdminData(
        studyOid=study_oid,
        users=tuple(odm_users),
        locations=tuple(locations),
    )


def build_odm_audit_record_from_revision(
    revision: Any,
    *,
    user_oid_lookup: Mapping[str, str] | None = None,
    site_oid_lookup: Mapping[str, str] | None = None,
    default_location_oid: str = "LOC.STUDY",
    default_user_oid: str = "USR.SYSTEM",
    mask_phi: bool = True,
    custom_phi_keys: set[str] | None = None,
) -> OdmAuditRecord:
    """Map an iMednet ``RecordRevision`` item into a canonical ``OdmAuditRecord``."""
    rev_id = _get_field(revision, "record_revision_id", "recordRevisionId", "id")
    rec_id = _get_field(revision, "record_id", "recordId")
    rec_rev = _get_field(revision, "record_revision", "recordRevision")
    raw_user = _get_field(revision, "user", "user_id", "userId")
    raw_role = _get_field(revision, "role")
    raw_site_id = _get_field(revision, "site_id", "siteId")
    raw_site_name = _get_field(revision, "site_name", "siteName")
    raw_dt = _get_field(revision, "date_created", "dateCreated", "timestamp")
    raw_reason = _get_field(revision, "reason_for_change", "reasonForChange")

    user_str = str(raw_user).strip() if raw_user not in (None, "") else ""
    if user_str and user_oid_lookup and user_str in user_oid_lookup:
        resolved_user_oid = user_oid_lookup[user_str]
    elif user_str:
        if mask_phi and ("@" in user_str or " " in user_str):
            import hashlib

            digest = hashlib.sha256(user_str.encode("utf-8")).hexdigest()[:10]
            resolved_user_oid = f"USR.{digest}"
        else:
            resolved_user_oid = sanitize_odm_oid("USR", user_str)
    else:
        resolved_user_oid = default_user_oid

    site_id_str = str(raw_site_id).strip() if raw_site_id not in (None, "") else ""
    site_name_str = str(raw_site_name).strip() if raw_site_name not in (None, "") else ""
    if site_id_str and site_oid_lookup and site_id_str in site_oid_lookup:
        resolved_loc_oid = site_oid_lookup[site_id_str]
    elif site_name_str and site_oid_lookup and site_name_str in site_oid_lookup:
        resolved_loc_oid = site_oid_lookup[site_name_str]
    elif site_id_str:
        resolved_loc_oid = sanitize_odm_oid("LOC", site_id_str)
    elif site_name_str:
        resolved_loc_oid = sanitize_odm_oid("LOC", site_name_str)
    else:
        resolved_loc_oid = default_location_oid

    edit_point: OdmEditPointType | None = None
    if raw_role:
        role_low = str(raw_role).lower()
        if "monitor" in role_low or "cra" in role_low:
            edit_point = "Monitoring"
        elif "data" in role_low or "dm" in role_low:
            edit_point = "DataManagement"
        else:
            edit_point = "DBAudit"

    reason_str: str | None = None
    if raw_reason not in (None, ""):
        reason_str = str(raw_reason).strip()
        if mask_phi and reason_str:
            reason_str = str(
                _mask_scalar_value(
                    "reason_for_change",
                    reason_str,
                    mask_phi=True,
                    custom_phi_keys=custom_phi_keys,
                )
            )

    if rev_id not in (None, ""):
        audit_id = sanitize_xml_id("AR", rev_id)
        source_id = str(rev_id)
    elif rec_id not in (None, ""):
        suffix = f"{rec_id}_{rec_rev}" if rec_rev not in (None, "") else str(rec_id)
        audit_id = sanitize_xml_id("AR", suffix)
        source_id = suffix
    else:
        audit_id = sanitize_xml_id("AR", "1")
        source_id = None

    return OdmAuditRecord(
        audit_id=audit_id,
        editPoint=edit_point,
        usedImputationMethod="No",
        userOid=resolved_user_oid,
        locationOid=resolved_loc_oid,
        dateTimeStamp=_format_iso_timestamp(raw_dt, fallback="1970-01-01T00:00:00Z"),
        reasonForChange=reason_str or None,
        sourceId=source_id,
    )


def _infer_comment_source(user_or_type: str | None) -> OdmCommentSourceType | None:
    if not user_or_type:
        return None
    low = user_or_type.lower()
    if "site" in low or "investigator" in low or "coordinator" in low:
        return "Site"
    if "sponsor" in low or "monitor" in low or "data" in low or "system" in low or "auto" in low:
        return "Sponsor"
    return None


def build_odm_annotations_from_query(
    query: Any,
    *,
    base_seq_num: int = 1,
    mask_phi: bool = True,
    custom_phi_keys: set[str] | None = None,
) -> list[OdmAnnotation]:
    """Map an iMednet ``Query`` and its ``QueryComment`` items to ``OdmAnnotation`` elements."""
    query_id = _get_field(query, "annotation_id", "annotationId", "query_id", "queryId", "id")
    description = _get_field(query, "description")
    query_type = _get_field(
        query, "annotation_type", "annotationType", "type", "query_type", "queryType"
    )
    query_status = _get_field(query, "status")
    variable_name = str(_get_field(query, "variable") or "").strip()
    comments = _get_field(query, "query_comments", "queryComments") or ()

    is_target_var_sensitive = bool(
        mask_phi and variable_name and _matches_phi_key(variable_name, custom_phi_keys)
    )

    annotations: list[OdmAnnotation] = []
    seq = max(1, base_seq_num)

    desc_str = str(description).strip() if description not in (None, "") else ""
    qtype_str = str(query_type).strip() if query_type not in (None, "") else ""
    qstatus_str = str(query_status).strip() if query_status not in (None, "") else ""

    comment_items = (
        list(comments)
        if isinstance(comments, Iterable) and not isinstance(comments, (str, bytes, Mapping))
        else []
    )

    # Sort comments deterministically by sequence when present
    comment_items.sort(key=lambda c: int(_get_field(c, "sequence") or 0))

    for c_idx, q_comment in enumerate(comment_items, start=1):
        c_text = str(_get_field(q_comment, "comment") or "").strip()
        c_status = str(
            _get_field(
                q_comment,
                "annotation_status",
                "annotationStatus",
                "query_status",
                "queryStatus",
                "status",
            )
            or ""
        ).strip()
        c_user = str(_get_field(q_comment, "user") or "").strip()
        effective_text = c_text or (desc_str if c_idx == 1 else "")
        if not effective_text and not c_status:
            continue

        if is_target_var_sensitive:
            rendered_comment = (
                f"[{c_status}] {MASKED_PHI_SENTINEL}" if c_status else MASKED_PHI_SENTINEL
            )
        else:
            body = (
                sanitize_diagnostic_text(effective_text)
                if (mask_phi and effective_text)
                else effective_text
            )
            rendered_comment = f"[{c_status}] {body}" if c_status and body else body or c_status

        c_flags: list[OdmFlag] = []
        if c_status:
            c_flags.append(
                OdmFlag(
                    flagValue=c_status,
                    flagValueCodeListOid="CL.QUERY_STATUS",
                )
            )
        if qtype_str:
            c_flags.append(
                OdmFlag(
                    flagValue=qtype_str,
                    flagValueCodeListOid="CL.QUERY_TYPE",
                )
            )
        c_id = sanitize_xml_id(
            "ANN",
            f"{query_id}_C{c_idx}" if query_id not in (None, "") else f"C{seq}",
        )
        annotations.append(
            OdmAnnotation(
                seqNum=seq,
                annotation_id=c_id,
                comment=OdmComment(
                    text=rendered_comment,
                    sponsorOrSite=_infer_comment_source(c_user or qtype_str),
                ),
                flags=tuple(c_flags),
            )
        )
        seq += 1

    if not annotations:
        if is_target_var_sensitive:
            masked_desc = MASKED_PHI_SENTINEL if desc_str else "Query on sensitive field"
        elif mask_phi and desc_str:
            masked_desc = sanitize_diagnostic_text(desc_str)
        else:
            masked_desc = desc_str or qtype_str or "Query"

        flags: list[OdmFlag] = []
        if qstatus_str:
            flags.append(
                OdmFlag(
                    flagValue=qstatus_str,
                    flagValueCodeListOid="CL.QUERY_STATUS",
                )
            )
        if qtype_str:
            flags.append(
                OdmFlag(
                    flagValue=qtype_str,
                    flagValueCodeListOid="CL.QUERY_TYPE",
                )
            )
        ann_id = sanitize_xml_id(
            "ANN", f"{query_id}_{seq}" if query_id not in (None, "") else str(seq)
        )
        annotations.append(
            OdmAnnotation(
                seqNum=seq,
                annotation_id=ann_id,
                comment=OdmComment(
                    text=masked_desc,
                    sponsorOrSite=_infer_comment_source(qtype_str),
                ),
                flags=tuple(flags),
            )
        )

    return annotations


def _is_reference_form(form: Form) -> bool:
    """Return True when a Form represents study-level non-subject ReferenceData."""
    if str(form.extraAttributes.get("isReferenceData", "")).lower() in ("yes", "true", "1"):
        return True
    ft = (form.formType or "").strip().lower()
    return ft in {"reference", "study", "non-subject", "nonsubject"}


def _serialize_item_scalar(
    var_name: str,
    raw_value: Any,
    *,
    mask_phi: bool,
    custom_phi_keys: set[str] | None = None,
) -> tuple[str | None, bool]:
    """Return ``(serialized_value, is_null)`` for an ODM ``<ItemData>`` value."""
    if raw_value is None:
        return None, True
    if isinstance(raw_value, float) and (math.isnan(raw_value) or math.isinf(raw_value)):
        return None, True
    if isinstance(raw_value, str) and raw_value == "":
        return None, True

    masked = _mask_scalar_value(
        var_name,
        raw_value,
        mask_phi=mask_phi,
        custom_phi_keys=custom_phi_keys,
    )
    if masked is None:
        return None, True
    if isinstance(masked, bool):
        return ("true" if masked else "false"), False
    if isinstance(masked, (datetime, date)):
        return _format_iso_timestamp(masked) if isinstance(
            masked, datetime
        ) else masked.isoformat(), False
    if isinstance(masked, (Mapping, list, tuple)):
        return json.dumps(masked, sort_keys=True, ensure_ascii=False), False
    return str(masked), False


def build_odm_archive(
    study_key: str | DataDictionary,
    *,
    data_dictionary: DataDictionary | None = None,
    datadict: DataDictionary | None = None,
    records: Iterable[Any] = (),
    sites: Iterable[Any] = (),
    users: Iterable[Any] = (),
    subjects: Iterable[Any] = (),
    visits: Iterable[Any] = (),
    revisions: Iterable[Any] = (),
    queries: Iterable[Any] = (),
    metadata_version_oid: str = "MDV.001",
    file_oid: str | None = None,
    creation_date_time: str | datetime | None = None,
    file_type: Literal["Snapshot", "Transactional"] = "Snapshot",
    mask_phi: bool = True,
    include_deleted: bool = False,
    custom_phi_keys: set[str] | None = None,
    source_system_version: str | None = None,
) -> OdmArchive:
    """Assemble a complete, deterministic ``OdmArchive`` from iMednet study models."""
    resolved_dd_input = data_dictionary if data_dictionary is not None else datadict
    if isinstance(study_key, DataDictionary):
        if resolved_dd_input is None:
            resolved_dd_input = study_key
        resolved_study_key = study_key.metadata.studyKey or "STUDY_001"
    else:
        resolved_study_key = str(study_key)
    study_key = resolved_study_key

    created_iso = _format_iso_timestamp(creation_date_time, fallback="1970-01-01T00:00:00Z")
    eff_date = _format_iso_date(created_iso, fallback="1970-01-01")

    dd = resolved_dd_input.model_copy(deep=True) if resolved_dd_input is not None else None
    forms_by_key: dict[str, Form] = {}
    forms_by_id: dict[int, Form] = {}
    ref_form_keys: set[str] = set()
    allocated_item_oids: dict[tuple[str, str], str] = {}
    if dd is not None:
        from .emit import resolve_form_item_oids

        allocated_item_oids = resolve_form_item_oids(dd)
        for f in dd.forms:
            forms_by_key[f.formKey] = f
            if isinstance(f.formId, int):
                forms_by_id[f.formId] = f
            if _is_reference_form(f):
                ref_form_keys.add(f.formKey)

    # AdminData (Sites & Users)
    sites_list = list(sites)
    users_list = list(users)
    admin = build_odm_admin_data(
        study_key,
        sites=sites_list,
        users=users_list,
        metadata_version_oid=metadata_version_oid,
        effective_date=eff_date,
        mask_phi=mask_phi,
        custom_phi_keys=custom_phi_keys,
    )

    site_lookup: dict[str, str] = {}
    for s in sites_list:
        sid = _get_field(s, "site_id", "siteId", "id")
        sname = _get_field(s, "site_name", "siteName", "name")
        raw_k = sid if sid not in (None, "") else sname
        if raw_k not in (None, ""):
            loid = sanitize_odm_oid("LOC", raw_k)
            if sid not in (None, ""):
                site_lookup[str(sid).strip()] = loid
            if sname not in (None, ""):
                site_lookup[str(sname).strip()] = loid

    user_lookup: dict[str, str] = {}
    for u in users_list:
        uid = _get_field(u, "user_id", "userId", "id")
        ulogin = _get_field(u, "login", "login_name", "loginName", "username")
        raw_u = uid if uid not in (None, "") else ulogin
        if raw_u not in (None, ""):
            uoid = sanitize_odm_oid("USR", raw_u)
            if uid not in (None, ""):
                user_lookup[str(uid).strip()] = uoid
            if ulogin not in (None, ""):
                user_lookup[str(ulogin).strip()] = uoid

    # Subject -> Site lookup
    subject_site_oid: dict[str, str] = {}
    for subj in subjects:
        skey = _get_field(subj, "subject_key", "subjectKey")
        if skey in (None, ""):
            continue
        ssite_id = _get_field(subj, "site_id", "siteId")
        ssite_name = _get_field(subj, "site_name", "siteName")
        loid_cand: str | None = None
        if ssite_id not in (None, "") and str(ssite_id).strip() in site_lookup:
            loid_cand = site_lookup[str(ssite_id).strip()]
        elif ssite_name not in (None, "") and str(ssite_name).strip() in site_lookup:
            loid_cand = site_lookup[str(ssite_name).strip()]
        elif ssite_id not in (None, ""):
            loid_cand = sanitize_odm_oid("LOC", ssite_id)
        if loid_cand:
            subject_site_oid[str(skey).strip()] = loid_cand

    # Visit ID -> StudyEventOID lookup
    visit_oid_by_id: dict[int, tuple[str, str | None]] = {}
    dd_visit_oids: set[str] = set()
    if dd is not None:
        raw_dd_visits = dd.metadata.extraAttributes.get("visits")
        if isinstance(raw_dd_visits, list):
            for v_entry in raw_dd_visits:
                if isinstance(v_entry, Mapping) and v_entry.get("oid"):
                    dd_visit_oids.add(str(v_entry["oid"]))

    for vis in visits:
        vid = _get_field(vis, "visit_id", "visitId", "id")
        iv_name = _get_field(vis, "interval_name", "intervalName", "name")
        iv_id = _get_field(vis, "interval_id", "intervalId")
        inst = _get_field(vis, "instance")
        if isinstance(vid, int) and not isinstance(vid, bool):
            if iv_name not in (None, "") and str(iv_name).strip() in dd_visit_oids:
                se_oid = str(iv_name).strip()
            elif iv_name not in (None, ""):
                se_oid = sanitize_odm_oid("SE", iv_name)
            elif iv_id not in (None, ""):
                se_oid = sanitize_odm_oid("SE", iv_id)
            else:
                se_oid = sanitize_odm_oid("SE", vid)
            rep = str(inst) if inst not in (None, "") else str(vid)
            visit_oid_by_id[vid] = (se_oid, rep)

    # Index AuditRecords by record_id and collect archive-level AuditRecords
    revisions_by_record: dict[str, list[OdmAuditRecord]] = {}
    all_audit_records: list[OdmAuditRecord] = []
    seen_audit_ids: set[str] = set()
    for rev in revisions:
        aud = build_odm_audit_record_from_revision(
            rev,
            user_oid_lookup=user_lookup,
            site_oid_lookup=site_lookup,
            mask_phi=mask_phi,
            custom_phi_keys=custom_phi_keys,
        )
        if aud.audit_id and aud.audit_id in seen_audit_ids:
            aud = aud.model_copy(
                update={
                    "audit_id": sanitize_xml_id("AR", f"{aud.audit_id}_{len(seen_audit_ids) + 1}")
                }
            )
        if aud.audit_id:
            seen_audit_ids.add(aud.audit_id)
        all_audit_records.append(aud)
        rec_id = _get_field(rev, "record_id", "recordId")
        if rec_id not in (None, ""):
            revisions_by_record.setdefault(str(rec_id).strip(), []).append(aud)

    for rec_auds in revisions_by_record.values():
        rec_auds.sort(key=lambda a: (a.date_time_stamp, a.audit_id or ""))
    all_audit_records.sort(key=lambda a: (a.date_time_stamp, a.audit_id or ""))

    # Index Annotations by (record_id, variable) or (subject_key, form_key, variable)
    annotations_by_rec_var: dict[tuple[str, str], list[OdmAnnotation]] = {}
    annotations_by_rec: dict[str, list[OdmAnnotation]] = {}
    annotations_by_subj: dict[str, list[OdmAnnotation]] = {}
    all_annotations: list[OdmAnnotation] = []
    next_seq = 1

    sorted_queries = sorted(
        queries,
        key=lambda q: (
            str(_get_field(q, "annotation_id", "annotationId", "query_id", "queryId", "id") or ""),
            str(_get_field(q, "date_created", "dateCreated") or ""),
        ),
    )
    for query in sorted_queries:
        built_anns = build_odm_annotations_from_query(
            query,
            base_seq_num=next_seq,
            mask_phi=mask_phi,
            custom_phi_keys=custom_phi_keys,
        )
        if not built_anns:
            continue
        next_seq += len(built_anns)
        all_annotations.extend(built_anns)

        q_rec_id = _get_field(query, "record_id", "recordId")
        q_var = str(_get_field(query, "variable") or "").strip()
        q_subj = str(_get_field(query, "subject_key", "subjectKey") or "").strip()
        if q_rec_id not in (None, ""):
            rid_str = str(q_rec_id).strip()
            if q_var:
                annotations_by_rec_var.setdefault((rid_str, q_var), []).extend(built_anns)
            else:
                annotations_by_rec.setdefault(rid_str, []).extend(built_anns)
        elif q_subj:
            annotations_by_subj.setdefault(q_subj, []).extend(built_anns)

    # Group Records into ReferenceData vs ClinicalData (Subject -> StudyEvent -> Form -> ItemGroup -> ItemData)
    ref_item_groups: list[OdmItemGroupData] = []
    subj_events: dict[str, dict[tuple[str, str], list[OdmFormData]]] = {}
    discovered_events: dict[str, set[str]] = {}

    for rec in records:
        is_del_raw = _get_field(rec, "deleted")
        is_del = (
            bool(is_del_raw)
            if isinstance(is_del_raw, bool)
            else (
                str(is_del_raw).strip().lower() in {"true", "1", "yes"}
                if is_del_raw is not None
                else False
            )
        )
        if is_del and not include_deleted:
            continue
        tx_type: OdmTransactionType | None = "Remove" if is_del else None

        rec_id = _get_field(rec, "record_id", "recordId")
        rec_id_str = str(rec_id).strip() if rec_id not in (None, "") else None
        raw_form_key = _get_field(rec, "form_key", "formKey")
        raw_form_id = _get_field(rec, "form_id", "formId")

        matched_form: Form | None = None
        if raw_form_key not in (None, "") and str(raw_form_key) in forms_by_key:
            matched_form = forms_by_key[str(raw_form_key)]
        elif isinstance(raw_form_id, int) and raw_form_id in forms_by_id:
            matched_form = forms_by_id[raw_form_id]

        form_oid = (
            matched_form.formKey
            if matched_form is not None
            else (
                str(raw_form_key).strip()
                if raw_form_key not in (None, "")
                else f"FORM_{raw_form_id or 'UNKNOWN'}"
            )
        )
        ig_oid = f"IG_{form_oid}"

        raw_payload = _get_field(rec, "record_data", "recordData")
        if isinstance(raw_payload, Mapping):
            payload = dict(raw_payload)
        elif isinstance(rec, Mapping):
            excluded = {
                "record_id",
                "recordId",
                "subject_key",
                "subjectKey",
                "study_key",
                "studyKey",
                "visit_id",
                "visitId",
                "form_id",
                "formId",
                "form_key",
                "formKey",
                "deleted",
                "date_created",
                "dateCreated",
                "date_modified",
                "dateModified",
            }
            payload = {k: v for k, v in rec.items() if k not in excluded}
        else:
            payload = {}

        # Determine ordered variable keys (metadata order first, then remaining sorted)
        ordered_var_names: list[str] = []
        seen_vars: set[str] = set()
        if matched_form is not None:
            for v in matched_form.variables:
                if v.variableName in payload:
                    ordered_var_names.append(v.variableName)
                    seen_vars.add(v.variableName)
        for k in sorted(payload.keys(), key=str):
            ks = str(k)
            if ks not in seen_vars:
                ordered_var_names.append(ks)
                seen_vars.add(ks)

        rec_audits = revisions_by_record.get(rec_id_str, []) if rec_id_str else []
        latest_audit = rec_audits[-1] if rec_audits else None
        rec_anns = list(annotations_by_rec.get(rec_id_str, ())) if rec_id_str else []

        item_data_list: list[OdmItemData] = []
        for var_name in ordered_var_names:
            item_oid = allocated_item_oids.get((form_oid, var_name), f"{form_oid}.{var_name}")
            set_val, is_null = _serialize_item_scalar(
                var_name,
                payload.get(var_name),
                mask_phi=mask_phi,
                custom_phi_keys=custom_phi_keys,
            )
            var_anns = (
                tuple(
                    a.model_copy(update={"annotation_id": None})
                    for a in annotations_by_rec_var.get((rec_id_str, var_name), ())
                )
                if rec_id_str
                else ()
            )
            var_ann_ref = (
                annotations_by_rec_var[(rec_id_str, var_name)][-1].annotation_id
                if rec_id_str and (rec_id_str, var_name) in annotations_by_rec_var
                else None
            )
            item_data_list.append(
                OdmItemData(
                    itemOid=item_oid,
                    value=set_val,
                    isNull=is_null,
                    transactionType=tx_type,
                    auditRecordId=latest_audit.audit_id if latest_audit else None,
                    annotationId=var_ann_ref,
                    annotations=var_anns,
                )
            )

        rec_inline_audit = (
            latest_audit.model_copy(update={"audit_id": None}) if latest_audit else None
        )
        rec_inline_anns = tuple(a.model_copy(update={"annotation_id": None}) for a in rec_anns)

        ig_data = OdmItemGroupData(
            itemGroupOid=ig_oid,
            itemGroupRepeatKey=rec_id_str,
            transactionType=tx_type,
            auditRecordId=latest_audit.audit_id if latest_audit else None,
            annotationId=rec_anns[-1].annotation_id if rec_anns else None,
            auditRecord=rec_inline_audit,
            annotations=rec_inline_anns,
            items=tuple(item_data_list),
        )

        subj_key_raw = _get_field(rec, "subject_key", "subjectKey")
        subj_key = str(subj_key_raw).strip() if subj_key_raw not in (None, "") else ""

        if form_oid in ref_form_keys or not subj_key:
            ref_item_groups.append(ig_data)
            continue

        rec_site_id = _get_field(rec, "site_id", "siteId")
        if subj_key not in subject_site_oid and rec_site_id not in (None, ""):
            sid_s = str(rec_site_id).strip()
            subject_site_oid[subj_key] = site_lookup.get(sid_s, sanitize_odm_oid("LOC", sid_s))

        vis_id_raw = _get_field(rec, "visit_id", "visitId")
        se_oid = "SE.COMMON"
        se_repeat = "1"
        if isinstance(vis_id_raw, int) and not isinstance(vis_id_raw, bool) and vis_id_raw > 0:
            if vis_id_raw in visit_oid_by_id:
                se_oid, rep_cand = visit_oid_by_id[vis_id_raw]
                se_repeat = rep_cand or str(vis_id_raw)
            else:
                se_oid = sanitize_odm_oid("SE", vis_id_raw)
                se_repeat = str(vis_id_raw)
        else:
            iv_raw = _get_field(rec, "interval_id", "intervalId")
            if iv_raw not in (None, "", 0):
                se_oid = sanitize_odm_oid("SE", iv_raw)

        discovered_events.setdefault(se_oid, set()).add(form_oid)
        form_data_obj = OdmFormData(
            formOid=form_oid,
            formRepeatKey=rec_id_str,
            transactionType=tx_type,
            itemGroupData=(ig_data,),
        )
        subj_events.setdefault(subj_key, {}).setdefault((se_oid, se_repeat), []).append(
            form_data_obj
        )

    # Ensure subjects without records (if explicitly supplied) are also represented
    for subj in subjects:
        sk = _get_field(subj, "subject_key", "subjectKey")
        if sk not in (None, ""):
            subj_events.setdefault(str(sk).strip(), {})

    # Ensure StudyEventDefs exist in DataDictionary when dd is supplied and events were discovered
    if dd is not None and discovered_events:
        extra_attrs = dict(dd.metadata.extraAttributes)
        existing_visits = list(extra_attrs.get("visits") or [])
        by_oid: dict[str, dict[str, Any]] = {}
        for v_item in existing_visits:
            if isinstance(v_item, Mapping) and v_item.get("oid"):
                copied = dict(v_item)
                copied["forms"] = list(v_item.get("forms") or [])
                by_oid[str(v_item["oid"])] = copied

        for se_oid in sorted(discovered_events):
            forms_for_se = sorted(discovered_events[se_oid])
            if se_oid not in by_oid:
                ev_type = "Common" if se_oid == "SE.COMMON" else "Scheduled"
                by_oid[se_oid] = {
                    "oid": se_oid,
                    "name": "Common / Unscheduled" if se_oid == "SE.COMMON" else se_oid,
                    "repeating": "Yes",
                    "type": ev_type,
                    "forms": [
                        {"formOid": fk, "orderNumber": idx, "mandatory": "No"}
                        for idx, fk in enumerate(forms_for_se, start=1)
                    ],
                }
            else:
                known_fks = {
                    str(f_ref.get("formOid"))
                    for f_ref in by_oid[se_oid]["forms"]
                    if isinstance(f_ref, Mapping)
                }
                for fk in forms_for_se:
                    if fk not in known_fks:
                        by_oid[se_oid]["forms"].append(
                            {
                                "formOid": fk,
                                "orderNumber": len(by_oid[se_oid]["forms"]) + 1,
                                "mandatory": "No",
                            }
                        )
        extra_attrs["visits"] = list(by_oid.values())
        dd.metadata.extraAttributes = extra_attrs

    subject_data_list: list[OdmSubjectData] = []
    for subj_key in sorted(subj_events.keys()):
        events_map = subj_events[subj_key]
        se_list: list[OdmStudyEventData] = []
        for se_oid, se_rep in sorted(events_map.keys()):
            forms_in_event = sorted(
                events_map[(se_oid, se_rep)],
                key=lambda fd: (fd.form_oid, fd.form_repeat_key or ""),
            )
            se_list.append(
                OdmStudyEventData(
                    studyEventOid=se_oid,
                    studyEventRepeatKey=se_rep,
                    formData=tuple(forms_in_event),
                )
            )
        subj_anns = tuple(
            a.model_copy(update={"annotation_id": None})
            for a in annotations_by_subj.get(subj_key, ())
        )
        subject_data_list.append(
            OdmSubjectData(
                subjectKey=subj_key,
                site_location_oid=subject_site_oid.get(subj_key),
                annotations=subj_anns,
                studyEventData=tuple(se_list),
            )
        )

    admin_tuple = (admin,) if (admin.users or admin.locations or admin.signature_defs) else ()
    ref_tuple = (
        (
            OdmReferenceData(
                studyOid=study_key,
                metaDataVersionOid=metadata_version_oid,
                itemGroupData=tuple(
                    sorted(
                        ref_item_groups,
                        key=lambda ig: (ig.item_group_oid, ig.item_group_repeat_key or ""),
                    )
                ),
            ),
        )
        if ref_item_groups
        else ()
    )
    clinical_tuple = (
        (
            OdmClinicalData(
                studyOid=study_key,
                metaDataVersionOid=metadata_version_oid,
                subject_data=tuple(subject_data_list),
                auditRecords=tuple(all_audit_records),
                annotations=tuple(all_annotations),
            ),
        )
        if (subject_data_list or all_audit_records or all_annotations)
        else ()
    )

    return OdmArchive(
        fileOid=file_oid or f"ODM.{study_key}",
        fileType=file_type,
        odmVersion="1.3.2",
        creationDateTime=created_iso,
        data_dictionary=dd,
        adminData=admin_tuple,
        referenceData=ref_tuple,
        clinicalData=clinical_tuple,
        sourceSystem="imednet-toolkit",
        sourceSystemVersion=source_system_version,
    )


__all__ = [
    "MASKED_PHI_SENTINEL",
    "DatasetJsonClinicalData",
    "DatasetJsonColumn",
    "DatasetJsonDataType",
    "DatasetJsonDataset",
    "DatasetJsonItemGroupData",
    "DatasetJsonSourceSystem",
    "OdmAdminData",
    "OdmAnnotation",
    "OdmArchive",
    "OdmAuditRecord",
    "OdmClinicalData",
    "OdmComment",
    "OdmCommentSourceType",
    "OdmEditPointType",
    "OdmFlag",
    "OdmFormData",
    "OdmItemData",
    "OdmItemGroupData",
    "OdmLocation",
    "OdmLocationType",
    "OdmMetaDataVersionRef",
    "OdmReferenceData",
    "OdmSignatureDef",
    "OdmStudyEventData",
    "OdmSubjectData",
    "OdmTransactionType",
    "OdmUser",
    "OdmUserType",
    "build_odm_admin_data",
    "build_odm_annotations_from_query",
    "build_odm_archive",
    "build_odm_audit_record_from_revision",
    "sanitize_odm_oid",
    "sanitize_xml_id",
]
