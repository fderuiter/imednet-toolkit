"""Canonical read-only exchange models for CDISC ODM 1.3.2 and Dataset-JSON v1.1."""

from __future__ import annotations

from collections.abc import ItemsView, Iterator, KeysView, ValuesView
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from .models import DataDictionary

DatasetJsonScalar = str | int | float | bool | None
DatasetJsonItemType = Literal[
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
]
DatasetJsonTargetDataType = Literal["integer", "decimal"]


class _FrozenExchangeModel(BaseModel):
    """Base immutable Pydantic model for read-only CDISC exchange structures."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")


class OdmUser(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<User>`` administrative definition."""

    oid: str
    user_type: str | None = Field(default=None, alias="userType")
    login_name: str | None = Field(default=None, alias="loginName")
    display_name: str | None = Field(default=None, alias="displayName")
    full_name: str | None = Field(default=None, alias="fullName")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    organization: str | None = None
    email: str | None = None
    phone: str | None = None
    location_refs: tuple[str, ...] = Field(default=(), alias="locationRefs")
    extra_attributes: dict[str, Any] = Field(default_factory=dict, alias="extraAttributes")


class OdmMetaDataVersionRef(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<MetaDataVersionRef>`` child of ``<Location>``."""

    study_oid: str = Field(alias="studyOID")
    metadata_version_oid: str = Field(alias="metaDataVersionOID")
    effective_date: str = Field(default="1970-01-01", alias="effectiveDate")


class OdmLocation(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<Location>`` site/institution definition."""

    oid: str
    name: str
    location_type: str = Field(default="Site", alias="locationType")
    metadata_version_refs: tuple[OdmMetaDataVersionRef, ...] = Field(
        default=(), alias="metaDataVersionRefs"
    )
    extra_attributes: dict[str, Any] = Field(default_factory=dict, alias="extraAttributes")


class OdmAdminData(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<AdminData>`` container for study users and locations."""

    study_oid: str | None = Field(default=None, alias="studyOID")
    users: tuple[OdmUser, ...] = ()
    locations: tuple[OdmLocation, ...] = ()
    extra_attributes: dict[str, Any] = Field(default_factory=dict, alias="extraAttributes")


class OdmAuditRecord(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<AuditRecord>`` element for 21 CFR Part 11 revision trails."""

    id: str | None = None
    edit_point: str | None = Field(default=None, alias="editPoint")
    used_imputation_method: str | None = Field(default=None, alias="usedImputationMethod")
    user_oid: str = Field(alias="userOID")
    location_oid: str = Field(alias="locationOID")
    datetime_stamp: str = Field(alias="dateTimeStamp")
    reason_for_change: str | None = Field(default=None, alias="reasonForChange")
    source_id: str | None = Field(default=None, alias="sourceID")


class OdmFlag(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<Flag>`` child of ``<Annotation>``."""

    flag_value: str = Field(alias="flagValue")
    flag_value_codelist_oid: str = Field(
        default="CL.IMEDNET_FLAG_VALUE", alias="flagValueCodeListOID"
    )
    flag_type: str | None = Field(default=None, alias="flagType")
    flag_type_codelist_oid: str | None = Field(default=None, alias="flagTypeCodeListOID")


class OdmAnnotation(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<Annotation>`` element for EDC queries and query comments."""

    seq_num: int = Field(alias="seqNum")
    id: str | None = None
    transaction_type: str | None = Field(default=None, alias="transactionType")
    comment: str | None = None
    sponsor_or_site: str | None = Field(default=None, alias="sponsorOrSite")
    flags: tuple[OdmFlag, ...] = ()


class OdmItemData(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<ItemData>`` value cell."""

    item_oid: str = Field(alias="itemOID")
    value: str | None = None
    is_null: bool = Field(default=False, alias="isNull")
    transaction_type: str | None = Field(default=None, alias="transactionType")
    audit_record_id: str | None = Field(default=None, alias="auditRecordID")
    annotation_id: str | None = Field(default=None, alias="annotationID")
    measurement_unit_oid: str | None = Field(default=None, alias="measurementUnitOID")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()


class OdmItemGroupData(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<ItemGroupData>`` record container."""

    item_group_oid: str = Field(alias="itemGroupOID")
    item_group_repeat_key: str | None = Field(default=None, alias="itemGroupRepeatKey")
    transaction_type: str | None = Field(default=None, alias="transactionType")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()
    items: tuple[OdmItemData, ...] = ()

    @property
    def audit_records(self) -> tuple[OdmAuditRecord, ...]:
        """Return inline ``AuditRecord`` as a read-only tuple."""
        return (self.audit_record,) if self.audit_record is not None else ()


class OdmReferenceData(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<ReferenceData>`` container for non-subject reference tables."""

    study_oid: str = Field(alias="studyOID")
    metadata_version_oid: str = Field(alias="metaDataVersionOID")
    item_groups: tuple[OdmItemGroupData, ...] = Field(
        default=(),
        alias="itemGroups",
        validation_alias=AliasChoices(
            "itemGroups", "item_groups", "itemGroupData", "item_group_data"
        ),
        serialization_alias="itemGroups",
    )

    @property
    def item_group_data(self) -> tuple[OdmItemGroupData, ...]:
        """Alias for ``item_groups`` matching ODM ``ItemGroupData`` terminology."""
        return self.item_groups


class OdmFormData(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<FormData>`` container."""

    form_oid: str = Field(alias="formOID")
    form_repeat_key: str | None = Field(default=None, alias="formRepeatKey")
    transaction_type: str | None = Field(default=None, alias="transactionType")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()
    item_groups: tuple[OdmItemGroupData, ...] = Field(
        default=(),
        alias="itemGroups",
        validation_alias=AliasChoices(
            "itemGroups", "item_groups", "itemGroupData", "item_group_data"
        ),
        serialization_alias="itemGroups",
    )

    @property
    def item_group_data(self) -> tuple[OdmItemGroupData, ...]:
        """Alias for ``item_groups`` matching ODM ``ItemGroupData`` terminology."""
        return self.item_groups


class OdmStudyEventData(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<StudyEventData>`` scheduled or unscheduled visit container."""

    study_event_oid: str = Field(alias="studyEventOID")
    study_event_repeat_key: str | None = Field(default=None, alias="studyEventRepeatKey")
    transaction_type: str | None = Field(default=None, alias="transactionType")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()
    forms: tuple[OdmFormData, ...] = ()


class OdmSubjectData(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<SubjectData>`` subject clinical hierarchy."""

    subject_key: str = Field(alias="subjectKey")
    transaction_type: str | None = Field(default=None, alias="transactionType")
    investigator_user_oid: str | None = Field(default=None, alias="investigatorUserOID")
    site_location_oid: str | None = Field(default=None, alias="siteLocationOID")
    audit_record: OdmAuditRecord | None = Field(default=None, alias="auditRecord")
    annotations: tuple[OdmAnnotation, ...] = ()
    study_events: tuple[OdmStudyEventData, ...] = Field(default=(), alias="studyEvents")
    forms: tuple[OdmFormData, ...] = ()


class OdmClinicalData(_FrozenExchangeModel):
    """CDISC ODM 1.3.2 ``<ClinicalData>`` container for clinical, audit, and query records."""

    study_oid: str = Field(alias="studyOID")
    metadata_version_oid: str = Field(alias="metaDataVersionOID")
    subjects: tuple[OdmSubjectData, ...] = ()
    audit_records: tuple[OdmAuditRecord, ...] = Field(default=(), alias="auditRecords")
    annotations: tuple[OdmAnnotation, ...] = ()


class OdmArchive(_FrozenExchangeModel):
    """Canonical read-only CDISC ODM 1.3.2 archive snapshot extending DataDictionary."""

    file_oid: str = Field(default="IMEDNET_ODM_EXPORT", alias="fileOID")
    file_type: Literal["Snapshot", "Transactional"] = Field(default="Snapshot", alias="fileType")
    odm_version: str = Field(default="1.3.2", alias="odmVersion")
    creation_datetime: str | None = Field(default=None, alias="creationDateTime")
    as_of_datetime: str | None = Field(default=None, alias="asOfDateTime")
    originator: str | None = None
    source_system: str | None = Field(default=None, alias="sourceSystem")
    source_system_version: str | None = Field(default=None, alias="sourceSystemVersion")
    description: str | None = None
    metadata_version_oid: str = Field(default="MDV.001", alias="metaDataVersionOID")
    metadata_version_name: str | None = Field(default=None, alias="metaDataVersionName")
    data_dictionary: DataDictionary = Field(alias="dataDictionary")
    admin_data: tuple[OdmAdminData, ...] = Field(default=(), alias="adminData")
    reference_data: tuple[OdmReferenceData, ...] = Field(default=(), alias="referenceData")
    clinical_data: tuple[OdmClinicalData, ...] = Field(default=(), alias="clinicalData")
    validation_warnings: tuple[str, ...] = Field(default=(), alias="validationWarnings")


class DatasetJsonSourceSystem(_FrozenExchangeModel):
    """CDISC Dataset-JSON v1.1 ``sourceSystem`` metadata descriptor."""

    name: str
    version: str


class DatasetJsonColumn(_FrozenExchangeModel):
    """CDISC Dataset-JSON v1.1 column definition inside top-level ``columns``."""

    item_oid: str = Field(
        alias="itemOID",
        validation_alias=AliasChoices("itemOID", "item_oid", "OID", "oid"),
        serialization_alias="itemOID",
    )
    name: str
    label: str
    data_type: DatasetJsonItemType = Field(
        alias="dataType",
        validation_alias=AliasChoices("dataType", "data_type", "type"),
        serialization_alias="dataType",
    )
    target_data_type: DatasetJsonTargetDataType | None = Field(default=None, alias="targetDataType")
    length: int | None = Field(default=None, ge=1)
    display_format: str | None = Field(default=None, alias="displayFormat")
    key_sequence: int | None = Field(default=None, alias="keySequence", ge=1)

    @property
    def oid(self) -> str:
        """Return ``item_oid`` for ergonomic Python inspection."""
        return self.item_oid

    @property
    def type(self) -> DatasetJsonItemType:
        """Return ``data_type`` for ergonomic Python inspection."""
        return self.data_type


DatasetJsonItem = DatasetJsonColumn


class DatasetJsonEnvelope(_FrozenExchangeModel):
    """Canonical read-only flat CDISC Dataset-JSON v1.1 single-dataset envelope."""

    dataset_json_creation_datetime: str = Field(
        alias="datasetJSONCreationDateTime",
        serialization_alias="datasetJSONCreationDateTime",
    )
    dataset_json_version: Literal["1.1.0"] = Field(
        default="1.1.0",
        alias="datasetJSONVersion",
        serialization_alias="datasetJSONVersion",
    )
    file_oid: str | None = Field(default=None, alias="fileOID")
    db_last_modified_datetime: str | None = Field(default=None, alias="dbLastModifiedDateTime")
    originator: str | None = None
    source_system: DatasetJsonSourceSystem | None = Field(default=None, alias="sourceSystem")
    study_oid: str | None = Field(default=None, alias="studyOID")
    metadata_version_oid: str | None = Field(default=None, alias="metaDataVersionOID")
    metadata_ref: str | None = Field(default=None, alias="metaDataRef")
    item_group_oid: str = Field(alias="itemGroupOID")
    is_reference_data: bool | None = Field(default=None, alias="isReferenceData")
    records: int = Field(ge=0)
    name: str
    label: str
    columns: tuple[DatasetJsonColumn, ...] = ()
    rows: tuple[tuple[DatasetJsonScalar, ...], ...] = ()

    @model_validator(mode="after")
    def _validate_records_and_arity(self) -> DatasetJsonEnvelope:  # noqa: N804
        if self.records != len(self.rows):
            raise ValueError(
                f"Dataset-JSON dataset '{self.name}' records ({self.records}) "
                f"does not match rows length ({len(self.rows)})"
            )
        expected_cols = len(self.columns)
        for row_idx, row in enumerate(self.rows):
            if len(row) != expected_cols:
                raise ValueError(
                    f"Dataset-JSON dataset '{self.name}' row {row_idx} length ({len(row)}) "
                    f"does not match columns count ({expected_cols})"
                )
        return self

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize to a clean CDISC Dataset-JSON v1.1 dictionary omitting null optional keys."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class DatasetJsonBundle(_FrozenExchangeModel):
    """Typed multi-dataset Python bundle mapping ``itemGroupOID`` keys to v1.1 envelopes."""

    study_oid: str = Field(alias="studyOID")
    metadata_version_oid: str = Field(default="MDV.001", alias="metaDataVersionOID")
    dataset_json_creation_datetime: str = Field(alias="datasetJSONCreationDateTime")
    datasets: dict[str, DatasetJsonEnvelope] = Field(default_factory=dict)

    def _resolve_key(self, key: str) -> str | None:
        if key in self.datasets:
            return key
        prefixed = f"IG.{key}"
        if prefixed in self.datasets:
            return prefixed
        upper_prefixed = f"IG.{key.upper()}"
        if upper_prefixed in self.datasets:
            return upper_prefixed
        for k, env in self.datasets.items():
            if env.name == key or env.name.upper() == key.upper():
                return k
        return None

    def __getitem__(self, key: str) -> DatasetJsonEnvelope:
        """Look up a dataset envelope by ``itemGroupOID`` (``IG.DM``) or dataset name (``DM``)."""
        resolved = self._resolve_key(key)
        if resolved is None:
            raise KeyError(key)
        return self.datasets[resolved]

    def get(
        self, key: str, default: DatasetJsonEnvelope | None = None
    ) -> DatasetJsonEnvelope | None:
        """Return dataset envelope for *key* if present, else *default*."""
        resolved = self._resolve_key(key)
        return self.datasets[resolved] if resolved is not None else default

    def __contains__(self, key: object) -> bool:
        """Return True when *key* matches an ``itemGroupOID`` or dataset name in the bundle."""
        return isinstance(key, str) and self._resolve_key(key) is not None

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        """Iterate over ``itemGroupOID`` keys in deterministic insertion order."""
        return iter(self.datasets)

    def __len__(self) -> int:
        """Return the number of dataset envelopes in the bundle."""
        return len(self.datasets)

    def keys(self) -> KeysView[str]:
        """Return view of ``itemGroupOID`` keys."""
        return self.datasets.keys()

    def values(self) -> ValuesView[DatasetJsonEnvelope]:
        """Return view of :class:`DatasetJsonEnvelope` values."""
        return self.datasets.values()

    def items(self) -> ItemsView[str, DatasetJsonEnvelope]:
        """Return view of ``(itemGroupOID, DatasetJsonEnvelope)`` pairs."""
        return self.datasets.items()


__all__ = [
    "DatasetJsonBundle",
    "DatasetJsonColumn",
    "DatasetJsonEnvelope",
    "DatasetJsonItem",
    "DatasetJsonItemType",
    "DatasetJsonScalar",
    "DatasetJsonSourceSystem",
    "DatasetJsonTargetDataType",
    "OdmAdminData",
    "OdmAnnotation",
    "OdmArchive",
    "OdmAuditRecord",
    "OdmClinicalData",
    "OdmFlag",
    "OdmFormData",
    "OdmItemData",
    "OdmItemGroupData",
    "OdmLocation",
    "OdmMetaDataVersionRef",
    "OdmReferenceData",
    "OdmStudyEventData",
    "OdmSubjectData",
    "OdmUser",
]
