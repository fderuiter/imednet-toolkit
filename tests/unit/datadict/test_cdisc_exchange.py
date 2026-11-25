"""Unit tests for CDISC ODM 1.3.2 and CDISC Dataset-JSON v1.1 exchange models, schemas, and loaders."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from imednet.datadict import (
    DATASET_JSON_SCHEMA_PATH,
    ODM_SCHEMA_PATH,
    Choice,
    DataDictionary,
    DatasetJsonClinicalData,
    DatasetJsonDataset,
    Form,
    Metadata,
    OdmAdminData,
    OdmAnnotation,
    OdmArchive,
    OdmAuditRecord,
    OdmClinicalData,
    OdmComment,
    OdmFlag,
    OdmFormData,
    OdmItemData,
    OdmItemGroupData,
    OdmLocation,
    OdmMetaDataVersionRef,
    OdmReferenceData,
    OdmSignatureDef,
    OdmStudyEventData,
    OdmSubjectData,
    OdmUser,
    Variable,
    build_dataset_json_clinical_data,
    build_dataset_json_dataset,
    build_odm_admin_data,
    build_odm_annotations_from_query,
    build_odm_archive,
    build_odm_audit_record_from_revision,
    dataset_json_to_rows,
    load_dataset_json,
    load_from_cdisc_odm,
    load_odm_archive,
    stream_odm_xml,
    to_odm_archive_file,
    to_odm_archive_xml,
    to_odm_xml,
    validate_dataset_json,
    validate_odm_xml,
    write_odm_xml_stream,
)
from imednet.models.queries import Query, QueryComment
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.visits import Visit
from imednet.spi import cdisc as spi_cdisc
from imednet.spi import models as spi_models


def _sample_datadict() -> DataDictionary:
    return DataDictionary(
        metadata=Metadata(
            studyKey="ONCO_01",
            source="api",
            extraAttributes={
                "studyName": "Oncology Phase II",
                "studyDescription": "Open-label study",
                "protocolName": "PROTO-ONCO-01",
            },
        ),
        forms=[
            Form(
                formId=10,
                formKey="DM",
                formName="Demographics",
                extraAttributes={"subjectRecordReport": True},
                variables=[
                    Variable(
                        variableName="SEX",
                        label="Biological Sex",
                        variableType="Radio",
                        sequence=1,
                        required="Yes",
                        choices=[
                            Choice(choiceValue="M", choiceText="Male", position=1),
                            Choice(choiceValue="F", choiceText="Female", position=2),
                        ],
                    ),
                    Variable(
                        variableName="AGE",
                        label="Age in Years",
                        variableType="Integer",
                        sequence=2,
                    ),
                    Variable(
                        variableName="PATIENT_NAME",
                        label="Patient Full Name",
                        variableType="Text",
                        sequence=3,
                    ),
                ],
            ),
            Form(
                formId=20,
                formKey="AE",
                formName="Adverse Events",
                variables=[
                    Variable(
                        variableName="AETERM",
                        label="Reported Term for the Adverse Event",
                        variableType="Text",
                        sequence=1,
                        required="Yes",
                    ),
                    Variable(
                        variableName="AESEV",
                        label="Severity",
                        variableType="Dropdown",
                        sequence=2,
                        choices=[
                            Choice(choiceValue="MILD", choiceText="Mild", position=1),
                            Choice(choiceValue="SEVERE", choiceText="Severe", position=2),
                        ],
                    ),
                ],
            ),
            Form(
                formId=30,
                formKey="REF_RANGES",
                formName="Laboratory Normal Ranges",
                extraAttributes={"isReferenceData": True},
                variables=[
                    Variable(
                        variableName="LBTESTCD",
                        label="Lab Test Code",
                        variableType="Text",
                        sequence=1,
                    ),
                    Variable(
                        variableName="LBORNRLO",
                        label="Normal Range Lower Limit",
                        variableType="Number",
                        sequence=2,
                    ),
                ],
            ),
        ],
    )


def _sample_clinical_fixtures() -> dict[str, list[object]]:
    sites = [
        Site(
            study_key="ONCO_01",
            site_id=101,
            site_name="Memorial Oncology Center",
            site_enrollment_status="Open",
        )
    ]
    users = [
        User(
            user_id="usr-1",
            login="jdoe",
            first_name="Jane",
            last_name="Doe",
            email="jdoe@example.org",
        )
    ]
    subjects = [
        Subject(
            study_key="ONCO_01",
            subject_id=1,
            subject_key="SUBJ-001",
            site_id=101,
            site_name="Memorial Oncology Center",
            subject_status="Enrolled",
        )
    ]
    visits = [
        Visit(
            study_key="ONCO_01",
            visit_id=501,
            interval_id=1,
            interval_name="Screening",
            subject_id=1,
            subject_key="SUBJ-001",
        )
    ]
    records = [
        Record(
            study_key="ONCO_01",
            record_id=1001,
            form_id=10,
            form_key="DM",
            subject_id=1,
            subject_key="SUBJ-001",
            site_id=101,
            visit_id=501,
            interval_name="Screening",
            date_created=datetime(2026, 2, 1, 9, 0, tzinfo=UTC),
            date_modified=datetime(2026, 2, 1, 9, 30, tzinfo=UTC),
            record_data={"SEX": "F", "AGE": 54, "PATIENT_NAME": "Alice Patient"},
        ),
        Record(
            study_key="ONCO_01",
            record_id=1002,
            form_id=20,
            form_key="AE",
            subject_id=1,
            subject_key="SUBJ-001",
            site_id=101,
            visit_id=501,
            interval_name="Screening",
            date_created=datetime(2026, 2, 2, 10, 0, tzinfo=UTC),
            date_modified=datetime(2026, 2, 2, 11, 15, tzinfo=UTC),
            record_data={"AETERM": "Nausea", "AESEV": "MILD"},
        ),
        Record(
            study_key="ONCO_01",
            record_id=2001,
            form_id=30,
            form_key="REF_RANGES",
            site_id=101,
            date_created=datetime(2026, 1, 15, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 1, 15, 8, 0, tzinfo=UTC),
            record_data={"LBTESTCD": "HGB", "LBORNRLO": 12.0},
        ),
    ]
    revisions = [
        RecordRevision(
            study_key="ONCO_01",
            record_revision_id=9001,
            record_id=1002,
            subject_key="SUBJ-001",
            site_id=101,
            form_key="AE",
            interval_name="Screening",
            user="jdoe",
            role="Investigator",
            reason_for_change="Corrected severity from source worksheet",
            date_created=datetime(2026, 2, 2, 11, 15, tzinfo=UTC),
        )
    ]
    queries = [
        Query(
            study_key="ONCO_01",
            query_id=7001,
            record_id=1002,
            subject_key="SUBJ-001",
            form_key="AE",
            variable="AETERM",
            description="Confirm exact MedDRA verbatim term",
            type="Manual",
            date_created=datetime(2026, 2, 2, 12, 0, tzinfo=UTC),
            date_modified=datetime(2026, 2, 2, 13, 0, tzinfo=UTC),
            query_comments=[
                QueryComment(
                    sequence=1,
                    annotation_status="Open",
                    user="jdoe",
                    comment="Verified against chart.",
                    closed=False,
                    date="2026-02-02T12:30:00Z",
                ),
                QueryComment(
                    sequence=2,
                    annotation_status="Closed",
                    user="jdoe",
                    comment="Resolved.",
                    closed=True,
                    date="2026-02-02T13:00:00Z",
                ),
            ],
        )
    ]
    return {
        "sites": sites,
        "users": users,
        "subjects": subjects,
        "visits": visits,
        "records": records,
        "revisions": revisions,
        "queries": queries,
    }


def test_spi_and_core_exchange_models_are_frozen_and_exposed() -> None:
    """Verify read-only exchange models are exposed on SPI and immutable."""
    assert spi_cdisc.OdmArchive is OdmArchive
    assert spi_cdisc.DatasetJsonDataset is DatasetJsonDataset
    assert spi_models.OdmArchive is OdmArchive
    assert spi_models.DatasetJsonClinicalData is DatasetJsonClinicalData
    assert ODM_SCHEMA_PATH.exists()
    assert DATASET_JSON_SCHEMA_PATH.exists()

    item = OdmItemData(item_oid="IT.AE.AETERM", value="Headache")
    with pytest.raises(ValidationError):
        item.value = "Mutated"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        OdmItemData(item_oid="IT.AE.AETERM", value="Headache", unexpected="x")  # type: ignore[call-arg]


def test_odm_132_full_snapshot_round_trip_and_xsd_validation(tmp_path: Path) -> None:
    """Round-trip an ODM 1.3.2 snapshot preserving metadata, AdminData, ReferenceData, ClinicalData, AuditRecord, and Annotation."""
    dd = _sample_datadict()
    fx = _sample_clinical_fixtures()

    archive = build_odm_archive(
        dd,
        sites=fx["sites"],
        users=fx["users"],
        subjects=fx["subjects"],
        visits=fx["visits"],
        records=fx["records"],
        revisions=fx["revisions"],
        queries=fx["queries"],
        creation_date_time="2026-02-03T00:00:00Z",
        mask_phi=True,
    )

    # Default PHI masking masks PATIENT_NAME and user PII while keeping clinical fields intact
    assert len(archive.admin_data) == 1
    assert len(archive.admin_data[0].locations) == 1
    assert len(archive.admin_data[0].users) == 1
    assert archive.admin_data[0].users[0].first_name == "***MASKED***"
    assert archive.admin_data[0].users[0].email == "***MASKED***"

    assert len(archive.reference_data) == 1
    assert len(archive.reference_data[0].item_group_data) == 1

    xml_str = to_odm_archive_xml(archive)
    validate_odm_xml(xml_str)

    # Streamed output matches in-memory serialized output byte-for-byte
    streamed_path = tmp_path / "streamed.odm.xml"
    write_odm_xml_stream(
        streamed_path,
        archive.datadict,
        file_type=archive.file_type,
        file_oid=archive.file_oid,
        creation_date_time=archive.creation_date_time,
        originator=archive.originator,
        source_system=archive.source_system,
        source_system_version=archive.source_system_version,
        odm_version=archive.odm_version,
        admin_data=archive.admin_data,
        reference_data=archive.reference_data,
        clinical_data=archive.clinical_data,
    )
    validate_odm_xml(streamed_path)
    streamed_chunks = "".join(
        stream_odm_xml(
            archive.datadict,
            file_type=archive.file_type,
            file_oid=archive.file_oid,
            creation_date_time=archive.creation_date_time,
            originator=archive.originator,
            source_system=archive.source_system,
            source_system_version=archive.source_system_version,
            odm_version=archive.odm_version,
            admin_data=archive.admin_data,
            reference_data=archive.reference_data,
            clinical_data=archive.clinical_data,
        )
    )
    assert streamed_path.read_text(encoding="utf-8") == streamed_chunks

    archive_file = tmp_path / "archive.odm.xml"
    to_odm_archive_file(archive, archive_file)
    round_tripped = load_odm_archive(archive_file, strict=True)

    # Verify DataDictionary metadata round-trip
    assert round_tripped.datadict.metadata.studyKey == "ONCO_01"
    assert {f.formKey for f in round_tripped.datadict.forms} == {"DM", "AE", "REF_RANGES"}

    # Verify AdminData round-trip
    assert len(round_tripped.admin_data) == 1
    rt_admin = round_tripped.admin_data[0]
    assert len(rt_admin.locations) == 1
    assert rt_admin.locations[0].name == "Memorial Oncology Center"
    assert len(rt_admin.locations[0].metadata_version_refs) == 1
    assert len(rt_admin.users) == 1

    # Verify ReferenceData round-trip
    assert len(round_tripped.reference_data) == 1
    rt_ref_items = {
        it.item_oid: it.value
        for ig in round_tripped.reference_data[0].item_group_data
        for it in ig.items
    }
    assert any("LBTESTCD" in oid and val == "HGB" for oid, val in rt_ref_items.items())

    # Verify ClinicalData, AuditRecord, and Annotation round-trip
    assert len(round_tripped.clinical_data) == 1
    rt_clin = round_tripped.clinical_data[0]
    assert len(rt_clin.subjects) == 1
    rt_subj = rt_clin.subjects[0]
    assert rt_subj.subject_key == "SUBJ-001"
    assert rt_subj.site_ref_oid is not None

    all_items = {
        it.item_oid: it.value
        for ev in rt_subj.study_event_data
        for fd in ev.form_data
        for ig in fd.item_group_data
        for it in ig.items
    }
    assert any("PATIENT_NAME" in oid and val == "***MASKED***" for oid, val in all_items.items())
    assert any("AETERM" in oid and val == "Nausea" for oid, val in all_items.items())

    # AuditRecord preserved with UserRef, LocationRef, DateTimeStamp, ReasonForChange
    all_audits = [
        ig.audit_record
        for ev in rt_subj.study_event_data
        for fd in ev.form_data
        for ig in fd.item_group_data
        if ig.audit_record is not None
    ] + list(rt_clin.audit_records)
    assert len(all_audits) >= 1
    audit = all_audits[0]
    assert audit is not None
    assert audit.user_oid
    assert audit.location_oid
    assert audit.date_time_stamp == "2026-02-02T11:15:00Z"
    assert audit.reason_for_change == "Corrected severity from source worksheet"

    # Annotation preserved from Query + QueryComments
    item_annotations = [
        ann
        for ev in rt_subj.study_event_data
        for fd in ev.form_data
        for ig in fd.item_group_data
        for it in ig.items
        for ann in it.annotations
    ]
    ig_annotations = [
        ann
        for ev in rt_subj.study_event_data
        for fd in ev.form_data
        for ig in fd.item_group_data
        for ann in ig.annotations
    ]
    all_annotations = item_annotations + ig_annotations + list(rt_clin.annotations)
    assert len(all_annotations) >= 2
    comments_text = " | ".join(
        ann.comment.value for ann in all_annotations if ann.comment is not None
    )
    assert "Verified against chart." in comments_text
    assert "Resolved." in comments_text

    # Re-emitting the round-tripped archive also validates against ODM 1.3.2 XSD
    re_emitted_xml = to_odm_archive_xml(round_tripped)
    validate_odm_xml(re_emitted_xml)


def test_load_from_cdisc_odm_strict_raises_on_clinical_and_non_metadata_elements(
    tmp_path: Path,
) -> None:
    """Calling load_from_cdisc_odm with strict=True on an ODM file containing ClinicalData raises ValueError."""
    dd = _sample_datadict()
    fx = _sample_clinical_fixtures()
    archive = build_odm_archive(
        dd,
        sites=fx["sites"],
        users=fx["users"],
        subjects=fx["subjects"],
        visits=fx["visits"],
        records=fx["records"],
        creation_date_time="2026-02-03T00:00:00Z",
    )
    odm_file = tmp_path / "with_clinical.odm.xml"
    to_odm_archive_file(archive, odm_file)

    with pytest.raises(ValueError, match="ClinicalData"):
        load_from_cdisc_odm(odm_file, strict=True)

    # Non-strict mode warns instead of silently dropping non-metadata elements
    loaded_non_strict = load_from_cdisc_odm(odm_file, strict=False)
    joined_warnings = "\n".join(loaded_non_strict.metadata.validationWarnings)
    assert "ClinicalData" in joined_warnings
    assert "AdminData" in joined_warnings
    assert "ReferenceData" in joined_warnings


@pytest.mark.parametrize(
    "injected_element",
    [
        "<AdminData StudyOID='S.1'/>",
        "<ReferenceData StudyOID='S.1' MetaDataVersionOID='MDV.1'/>",
        "<Association StudyOID='S.1' MetaDataVersionOID='MDV.1'/>",
    ],
)
def test_load_from_cdisc_odm_strict_rejects_other_root_non_metadata_containers(
    tmp_path: Path, injected_element: str
) -> None:
    """Verify strict=True rejects AdminData, ReferenceData, and Association containers."""
    xml_doc = f"""<?xml version="1.0" encoding="utf-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     ODMVersion="1.3.2"
     FileType="Snapshot"
     FileOID="F.1"
     CreationDateTime="2026-01-01T00:00:00Z">
  <Study OID="S.1">
    <GlobalVariables>
      <StudyName>S1</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>P1</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV.1" Name="v1"/>
  </Study>
  {injected_element}
</ODM>
"""
    target = tmp_path / "non_meta.xml"
    target.write_text(xml_doc, encoding="utf-8")
    with pytest.raises(ValueError, match="non-metadata|Unsupported|unparsed"):
        load_from_cdisc_odm(target, strict=True)


def test_load_odm_archive_blocks_xxe_payloads(tmp_path: Path) -> None:
    """Verify defusedxml blocks XXE entity expansion in load_odm_archive and load_from_cdisc_odm."""
    xxe_xml = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE ODM [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     ODMVersion="1.3.2"
     FileType="Snapshot"
     FileOID="F.XXE"
     CreationDateTime="2026-01-01T00:00:00Z">
  <Study OID="S.1">
    <GlobalVariables>
      <StudyName>&xxe;</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>P1</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV.1" Name="v1"/>
  </Study>
</ODM>
"""
    xxe_path = tmp_path / "xxe.odm.xml"
    xxe_path.write_text(xxe_xml, encoding="utf-8")

    with pytest.raises(DefusedXmlException):
        load_odm_archive(xxe_path)
    with pytest.raises(DefusedXmlException):
        load_from_cdisc_odm(xxe_path)


def test_dataset_json_v11_cdash_and_sdtm_schema_validation_and_round_trip(tmp_path: Path) -> None:
    """Verify CDASH Form and SDTM domain Dataset-JSON v1.1 generation, PHI masking, and official schema validation."""
    dd = _sample_datadict()
    fx = _sample_clinical_fixtures()

    # 1. CDASH clinicalData bundle across Forms
    clinical_bundle = build_dataset_json_clinical_data(
        fx["records"],
        study_key="ONCO_01",
        datadict=dd,
        as_sdtm=False,
        mask_phi=True,
        creation_date_time="2026-02-03T00:00:00Z",
    )
    assert len(clinical_bundle.item_group_data) == 3
    for ds in clinical_bundle.item_group_data:
        payload = ds.to_json_dict()
        validate_dataset_json(payload)
        assert payload["datasetJSONVersion"] == "1.1.0"
        # Ensure zero Veeva Vault proprietary envelope keys
        assert "subjects" not in payload
        assert "visits" not in payload
        assert "forms" not in payload

    dm_ds = next(ds for ds in clinical_bundle.item_group_data if ds.name == "DM")
    dm_rows = dataset_json_to_rows(dm_ds)
    assert len(dm_rows) == 1
    assert dm_rows[0]["PATIENT_NAME"] == "***MASKED***"
    assert dm_rows[0]["AGE"] == 54

    # Round-trip via file
    dm_path = tmp_path / "dm.dataset.json"
    dm_path.write_text(__import__("json").dumps(dm_ds.to_json_dict(), indent=2), encoding="utf-8")
    loaded_dm = load_dataset_json(dm_path)
    assert loaded_dm.records == 1
    assert dataset_json_to_rows(loaded_dm)[0]["SEX"] == "F"

    # 2. SDTM Domain Dataset-JSON v1.1
    ae_sdtm_ds = build_dataset_json_dataset(
        [
            {
                "STUDYID": "ONCO_01",
                "DOMAIN": "AE",
                "USUBJID": "ONCO_01-SUBJ-001",
                "AESEQ": 1,
                "AETERM": "Nausea",
                "AESEV": "MILD",
            }
        ],
        study_key="ONCO_01",
        name="AE",
        label="Adverse Events",
        datadict=dd,
        as_sdtm=True,
        sdtm_domain="AE",
        creation_date_time="2026-02-03T00:00:00Z",
    )
    ae_payload = ae_sdtm_ds.to_json_dict()
    validate_dataset_json(ae_payload)
    assert ae_payload["itemGroupOID"] == "IG.AE"
    assert ae_payload["records"] == 1
    col_types = {c["name"]: c["dataType"] for c in ae_payload["columns"]}
    assert col_types["AESEQ"] == "integer"
    assert col_types["AETERM"] == "string"


def test_schema_validators_reject_invalid_odm_and_dataset_json() -> None:
    """Verify validate_odm_xml and validate_dataset_json raise ValueError on schema violations."""
    bad_odm = """<?xml version="1.0" encoding="utf-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2">
  <UnknownElement/>
</ODM>
"""
    with pytest.raises(ValueError, match="ODM 1.3.2 XSD validation failed"):
        validate_odm_xml(bad_odm)

    bad_ds_json = {
        "datasetJSONCreationDateTime": "not-a-timestamp",
        "datasetJSONVersion": "1.1.0",
        "records": 0,
        "name": "AE",
        "label": "Adverse Events",
        "columns": [],
        "rows": [],
    }
    with pytest.raises(ValueError, match="Dataset-JSON v1.1.0 schema validation failed"):
        validate_dataset_json(bad_ds_json)


def test_builders_for_admin_audit_and_annotations_direct() -> None:
    """Verify direct builder helpers for AdminData, AuditRecord, and Annotation."""
    fx = _sample_clinical_fixtures()
    admin = build_odm_admin_data(
        "S.ONCO_01",
        sites=fx["sites"],
        users=fx["users"],
        metadata_version_oid="MDV.ONCO_01",
        mask_phi=False,
    )
    assert isinstance(admin, OdmAdminData)
    assert admin.users[0].first_name == "Jane"
    assert admin.users[0].email == "jdoe@example.org"

    audit = build_odm_audit_record_from_revision(fx["revisions"][0], mask_phi=False)
    assert isinstance(audit, OdmAuditRecord)
    assert audit.user_oid == "USR.jdoe"
    assert audit.location_oid == "LOC.101"
    assert audit.reason_for_change == "Corrected severity from source worksheet"

    annotations = build_odm_annotations_from_query(fx["queries"][0], mask_phi=False)
    assert len(annotations) == 2
    assert all(isinstance(a, OdmAnnotation) for a in annotations)
    assert annotations[0].seq_num == 1
    assert annotations[1].seq_num == 2

    # Explicit nested manual model construction & to_odm_xml integration
    manual_clin = OdmClinicalData(
        study_oid="S.ONCO_01",
        metadata_version_oid="MDV.1",
        subjects=(
            OdmSubjectData(
                subject_key="SUBJ-99",
                site_ref_oid="LOC.101",
                study_event_data=(
                    OdmStudyEventData(
                        study_event_oid="SE.SCREENING",
                        form_data=(
                            OdmFormData(
                                form_oid="F.AE",
                                item_group_data=(
                                    OdmItemGroupData(
                                        item_group_oid="IG.AE",
                                        item_group_repeat_key="1",
                                        items=(
                                            OdmItemData(
                                                item_oid="IT.AE.AETERM",
                                                value="Fatigue",
                                            ),
                                        ),
                                        audit_record=audit,
                                        annotations=(
                                            OdmAnnotation(
                                                seq_num=1,
                                                id="ANN.1",
                                                comment=OdmComment(value="Check onset"),
                                                flags=(
                                                    OdmFlag(
                                                        flag_value="Open",
                                                        flag_type="AnnotationStatus",
                                                    ),
                                                ),
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    manual_admin = OdmAdminData(
        study_oid="S.ONCO_01",
        users=(OdmUser(oid="USR.jdoe", login_name="jdoe"),),
        locations=(
            OdmLocation(
                oid="LOC.101",
                name="Site 101",
                location_type="Site",
                metadata_version_refs=(
                    OdmMetaDataVersionRef(
                        study_oid="S.ONCO_01",
                        metadata_version_oid="MDV.1",
                        effective_date="2026-01-01",
                    ),
                ),
            ),
        ),
        signature_defs=(
            OdmSignatureDef(
                oid="SIG.INV",
                methodology="Electronic",
                meaning="Investigator Approval",
                legal_reason="21 CFR Part 11",
            ),
        ),
    )
    manual_ref = OdmReferenceData(
        study_oid="S.ONCO_01",
        metadata_version_oid="MDV.1",
        item_group_data=(
            OdmItemGroupData(
                item_group_oid="IG.REF_RANGES",
                item_group_repeat_key="1",
                items=(OdmItemData(item_oid="IT.REF_RANGES.LBTESTCD", value="WBC"),),
            ),
        ),
    )
    xml_out = to_odm_xml(
        _sample_datadict(),
        creation_date_time="2026-02-03T00:00:00Z",
        admin_data=(manual_admin,),
        reference_data=(manual_ref,),
        clinical_data=(manual_clin,),
    )
    validate_odm_xml(xml_out)


def test_dataset_json_type_coercions_sdtm_bundles_and_loader_variants(tmp_path: Path) -> None:
    """Verify Dataset-JSON scalar type coercion, SDTM clinical bundles, and load_dataset_json input variants."""
    import json
    from datetime import date

    from imednet.datadict.models import Form as DDForm
    from imednet.datadict.models import Metadata
    from imednet.datadict.models import Variable as DDVar

    typed_form = DDForm(
        formId=99,
        formKey="VS",
        formName="Vital Signs",
        variables=[
            DDVar(variableName="SYSBP", label="Systolic BP", variableType="Integer", sequence=1),
            DDVar(variableName="TEMP", label="Temperature", variableType="Number", sequence=2),
            DDVar(variableName="FASTING", label="Fasting", variableType="Checkbox", sequence=3),
            DDVar(variableName="VSDAT", label="Visit Date", variableType="Date", sequence=4),
        ],
    )
    dd = DataDictionary(metadata=Metadata(studyKey="ONCO_01", source="api"), forms=[typed_form])
    raw_rows = [
        {
            "studyKey": "ONCO_01",
            "subjectKey": "SUBJ-01",
            "siteId": 101,
            "visitId": 500,
            "formId": 99,
            "formKey": "VS",
            "recordId": 11,
            "recordData": {
                "SYSBP": "120.0",
                "TEMP": "36.8",
                "FASTING": "yes",
                "VSDAT": date(2026, 2, 1),
                "NESTED": {"unit": "mmHg"},
            },
        },
        {
            "study_key": "ONCO_01",
            "subject_key": "SUBJ-02",
            "form_id": 99,
            "form_key": "VS",
            "record_id": 12,
            "SYSBP": True,
            "TEMP": float("nan"),
            "FASTING": "no",
            "VSDAT": datetime(2026, 2, 2, 9, 30, tzinfo=UTC),
            "EXTRA_FLOAT": 42.5,
            "EXTRA_INT": 7,
            "visits": ["proprietary"],
        },
    ]
    ds = build_dataset_json_dataset(
        "ONCO_01",
        raw_rows,
        dataset_name="VS",
        dataset_label="Vital Signs",
        form=typed_form,
        as_sdtm=False,
        mask_phi=False,
        creation_date_time="2026-02-03T00:00:00Z",
    )
    json_str = ds.to_json_str()
    validate_dataset_json(json.loads(json_str))
    out_file = tmp_path / "vs.dataset.json"
    out_file.write_text(json_str, encoding="utf-8")

    # Test load_dataset_json from dict, JSON string, str path, and invalid type
    assert load_dataset_json(ds.to_json_dict()).records == 2
    assert load_dataset_json(json_str).records == 2
    assert load_dataset_json(str(out_file)).records == 2
    with pytest.raises(TypeError, match="Unsupported Dataset-JSON source type"):
        load_dataset_json(12345)  # type: ignore[arg-type]

    # SDTM clinical data bundle (both explicit domain and inferred multi-domain)
    sdtm_bundle = build_dataset_json_clinical_data(
        "ONCO_01",
        [
            {
                "study_key": "ONCO_01",
                "subject_key": "SUBJ-01",
                "form_key": "AE",
                "record_id": 1,
                "record_data": {"AETERM": "Nausea", "BRTHDTC": "1980-05-12"},
            },
            {
                "study_key": "ONCO_01",
                "subject_key": "SUBJ-01",
                "form_key": "VS",
                "record_id": 2,
                "record_data": {"VSTESTCD": "SYSBP", "VSORRES": "120"},
            },
        ],
        data_dictionary=dd,
        as_sdtm=True,
        creation_date_time="2026-02-03T00:00:00Z",
    )
    assert "IG.AE" in sdtm_bundle.item_groups_by_oid
    assert "IG.VS" in sdtm_bundle.item_groups_by_oid
    single_dom_bundle = build_dataset_json_clinical_data(
        "ONCO_01",
        [{"study_key": "ONCO_01", "subject_key": "SUBJ-01", "record_data": {"AETERM": "Rash"}}],
        as_sdtm=True,
        sdtm_domain="AE",
        creation_date_time="2026-02-03T00:00:00Z",
    )
    assert len(single_dom_bundle.item_group_data) == 1


def test_odm_admin_roles_mapping_records_and_schema_non_raising() -> None:
    """Verify user role inference, unmasked AdminData, dict-based ODM archive building, and non-raising schema checks."""
    from imednet.datadict.exchange import build_odm_admin_data

    admin = build_odm_admin_data(
        "S.ONCO_01",
        sites=[
            {"site_id": 10, "site_name": "Site A", "date_created": "2026-01-15"},
            {"site_id": 10, "site_name": "Site A"},  # duplicate skipped
            {},  # empty skipped
        ],
        users=[
            {
                "user_id": "u1",
                "login": "inv1",
                "first_name": "Alice",
                "last_name": "Jones",
                "email": "alice@site.org",
                "roles": [{"name": "Principal Investigator", "sites": ["10", "Site A"]}],
            },
            {"user_id": "u2", "login": "cra1", "roles": [{"name": "Sponsor Monitor"}]},
            {"user_id": "u3", "login": "lab1", "roles": [{"name": "Central Lab"}]},
            {"user_id": "u4", "login": "other1", "roles": [{"name": "ReadOnly"}]},
            {"user_id": "u1", "login": "inv1"},  # duplicate skipped
            {},  # empty skipped
        ],
        mask_phi=False,
    )
    roles_by_oid = {u.oid: u.user_type for u in admin.users}
    assert roles_by_oid["USR.u1"] == "Investigator"
    assert roles_by_oid["USR.u2"] == "Sponsor"
    assert roles_by_oid["USR.u3"] == "Lab"
    assert roles_by_oid["USR.u4"] == "Other"
    inv_user = next(u for u in admin.users if u.oid == "USR.u1")
    assert inv_user.first_name == "Alice"
    assert inv_user.location_oids == ("LOC.10",)

    # Dict-based records without record_data + interval_id fallback + custom_phi_keys
    dd = _sample_datadict()
    dd.metadata.extraAttributes["visits"] = [
        {"oid": "SE.5", "name": "Cycle 1", "repeating": "No", "type": "Scheduled", "forms": []}
    ]
    archive = build_odm_archive(
        dd,
        records=[
            {
                "record_id": 900,
                "subject_key": "SUBJ-99",
                "site_id": 77,
                "interval_id": 5,
                "form_id": 888,
                "CUSTOM_PHI_FIELD": "sensitive-patient-value",
                "EXTRA_METRIC": 3.14,
            }
        ],
        subjects=[{"subject_key": "SUBJ-EMPTY"}],
        custom_phi_keys={"CUSTOM_PHI_FIELD"},
        creation_date_time="2026-02-03T00:00:00Z",
    )
    xml_out = to_odm_archive_xml(archive)
    assert "sensitive-patient-value" not in xml_out
    assert "***MASKED***" in xml_out
    assert validate_odm_xml(xml_out, raise_on_error=False) == []
    assert validate_odm_xml("<ODM/>", raise_on_error=False) != []
    assert validate_dataset_json({"datasetJSONVersion": "1.1.0"}, raise_on_error=False) != []


def test_exchange_dataset_json_and_schema_edge_branches(tmp_path: Path) -> None:
    """Exercise coercion, validation error branches, commentless queries, and schema validator inputs."""
    from datetime import date

    from imednet.datadict.exchange import (
        DatasetJsonColumn,
        _format_iso_date,
        _format_iso_timestamp,
        sanitize_odm_oid,
        sanitize_xml_id,
    )

    assert sanitize_odm_oid("LOC", "") == "LOC.UNSPECIFIED"
    assert sanitize_odm_oid("", "IT.AE.TERM") == "IT.AE.TERM"
    assert sanitize_xml_id("", "99bad").startswith("ID_")
    assert sanitize_xml_id("AR", "").startswith("AR.")
    assert _format_iso_timestamp(date(2026, 5, 1)) == "2026-05-01"
    assert _format_iso_timestamp("not-iso") == "not-iso"
    assert _format_iso_timestamp("2026-05-01T10:00:00") == "2026-05-01T10:00:00"
    assert _format_iso_timestamp(None).endswith("Z")
    assert _format_iso_date(datetime(2026, 5, 1, 12, 0, tzinfo=UTC)) == "2026-05-01"
    assert _format_iso_date(date(2026, 5, 2)) == "2026-05-02"
    assert _format_iso_date("2026-05-03T08:00:00Z") == "2026-05-03"
    assert _format_iso_date("bad-date", fallback="2020-01-01") == "2020-01-01"

    # DatasetJsonDataset row validation errors & DatasetJsonClinicalData mapping coercion
    col = DatasetJsonColumn(itemOID="IT.1", name="C1", label="C1", dataType="integer")
    with pytest.raises(ValidationError, match="sequences of positional scalar values"):
        DatasetJsonDataset(
            datasetJSONCreationDateTime="2026-01-01T00:00:00Z",
            itemGroupOID="IG.1",
            records=1,
            name="DS",
            columns=(col,),
            rows=("not-a-tuple",),  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError, match="expected 1 matching columns"):
        DatasetJsonDataset(
            datasetJSONCreationDateTime="2026-01-01T00:00:00Z",
            itemGroupOID="IG.1",
            records=1,
            name="DS",
            columns=(col,),
            rows=((1, 2),),
        )
    with pytest.raises(ValidationError, match="does not match len\\(rows\\)"):
        DatasetJsonDataset(
            datasetJSONCreationDateTime="2026-01-01T00:00:00Z",
            itemGroupOID="IG.1",
            records=5,
            name="DS",
            columns=(col,),
            rows=((1,),),
        )
    valid_ds = DatasetJsonDataset(
        datasetJSONCreationDateTime="2026-01-01T00:00:00Z",
        itemGroupOID="IG.1",
        records=1,
        name="DS",
        columns=(col,),
        rows=((float("nan"),),),
    )
    assert valid_ds.rows == ((None,),)
    clin_from_dict = DatasetJsonClinicalData(
        studyOID="S1",
        itemGroupData={"IG.1": valid_ds},  # type: ignore[arg-type]
    )
    assert len(clin_from_dict.item_group_data) == 1

    # Rich variable types + non-numeric widening + boolean/float/date/time + length
    rich_form = Form(
        formId=77,
        formKey="RICH",
        formName="Rich Types",
        variables=[
            Variable(
                variableName="INT_WIDEN",
                label="Widened Int",
                variableType="Integer",
                sequence=1,
                extraAttributes={"length": "12"},
            ),
            Variable(
                variableName="FLT_VAL",
                label="Float Val",
                variableType="Float",
                sequence=2,
                extraAttributes={"length": 8},
            ),
            Variable(
                variableName="BOOL_VAL",
                label="Bool Val",
                variableType="Boolean",
                sequence=3,
            ),
            Variable(
                variableName="DT_VAL",
                label="Datetime Val",
                variableType="Datetime",
                sequence=4,
            ),
            Variable(
                variableName="TM_VAL",
                label="Time Val",
                variableType="Time",
                sequence=5,
            ),
        ],
    )
    ds_rich = build_dataset_json_dataset(
        [
            {
                "INT_WIDEN": "non-numeric",
                "FLT_VAL": "12.5",
                "BOOL_VAL": "true",
                "DT_VAL": datetime(2026, 5, 1, 8, 0, tzinfo=UTC),
                "TM_VAL": "08:15:00",
            },
            {
                "INT_WIDEN": 42,
                "FLT_VAL": True,
                "BOOL_VAL": 0,
                "DT_VAL": None,
                "TM_VAL": "",
            },
        ],
        study_key="S1",
        name="RICH",
        form=rich_form,
        mask_phi=False,
        use_labels_as_names=True,
        creation_date_time="2026-05-01T00:00:00Z",
    )
    validate_dataset_json(ds_rich)
    col_map = {c.label: c for c in ds_rich.columns}
    assert col_map["Widened Int"].data_type == "string"
    assert col_map["Widened Int"].length == 12
    assert col_map["Float Val"].data_type == "float"
    assert col_map["Bool Val"].data_type == "boolean"

    # Empty dataset fallback columns
    ds_empty = build_dataset_json_dataset(
        [], study_key="S1", name="EMPTY", creation_date_time="2026-05-01T00:00:00Z"
    )
    validate_dataset_json(ds_empty)

    # Commentless query, sensitive commentless query, record-level and subject-level queries, deleted record
    q_no_comments = Query(
        study_key="ONCO_01",
        query_id=801,
        record_id=1001,
        description="Missing lab attachment",
        type="Manual",
        status="Open",
        query_comments=[],
    )
    q_subj_level = Query(
        study_key="ONCO_01",
        query_id=802,
        subject_key="SUBJ-001",
        variable="PATIENT_NAME",
        description="Alice Real Name",
        type="Auto",
        query_comments=[],
    )
    del_rec = Record(
        study_key="ONCO_01",
        record_id=1001,
        form_id=10,
        form_key="DM",
        subject_id=1,
        subject_key="SUBJ-001",
        deleted=True,
        record_data={
            "SEX": "",
            "AGE": None,
            "BOOL_F": True,
            "NAN_F": float("nan"),
            "DICT_F": {"a": 1},
            "DATE_F": date(2026, 5, 1),
        },
    )
    rev_no_revid = {"record_id": 1001, "record_revision": 2, "role": "Data Manager"}
    arch_del = build_odm_archive(
        _sample_datadict(),
        records=[del_rec],
        revisions=[rev_no_revid],
        queries=[q_no_comments, q_subj_level],
        include_deleted=True,
        creation_date_time="2026-05-01T00:00:00Z",
    )
    xml_del = to_odm_archive_xml(arch_del)
    validate_odm_xml(xml_del.encode("utf-8"))
    assert 'TransactionType="Remove"' in xml_del
    assert "Alice Real Name" not in xml_del

    # Schema validator error paths (missing files, invalid JSON, non-ODM root, unsupported types)
    missing_json = tmp_path / "missing.json"
    assert validate_dataset_json(missing_json, raise_on_error=False) != []
    assert validate_dataset_json(str(missing_json), raise_on_error=False) != []
    assert validate_dataset_json("{invalid json", raise_on_error=False) != []
    assert validate_dataset_json(123, raise_on_error=False) != []  # type: ignore[arg-type]
    bad_json_file = tmp_path / "bad.json"
    bad_json_file.write_text("{broken", encoding="utf-8")
    assert validate_dataset_json(bad_json_file, raise_on_error=False) != []
    assert validate_dataset_json(str(bad_json_file), raise_on_error=False) != []

    missing_xml = tmp_path / "missing.xml"
    assert validate_odm_xml(missing_xml, raise_on_error=False) != []
    assert validate_odm_xml(str(missing_xml), raise_on_error=False) != []
    assert validate_odm_xml("<NotODM/>", raise_on_error=False) != []
    assert validate_odm_xml("<unclosed", raise_on_error=False) != []
    assert validate_odm_xml(123, raise_on_error=False) != []  # type: ignore[arg-type]


def test_upstream_main_strict_schema_and_unified_phi_alignment() -> None:
    """Verify alignment with #2115 strict schemas and #2119 unified diagnostic PHI redaction."""
    from pydantic import ValidationError

    from imednet.datadict.schema import validate as validate_dd

    # 1. Malformed imednet:logicRawXml on ODM ConditionDef records malformedLogicXml for validate()
    malformed_odm = """<?xml version="1.0" encoding="utf-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     xmlns:imednet="http://www.imednet.com/ns/odm/metadata"
     FileOID="F1" FileType="Snapshot" CreationDateTime="2026-05-01T00:00:00Z" ODMVersion="1.3.2">
  <Study OID="ST_STRICT">
    <GlobalVariables>
      <StudyName>Strict Study</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>Proto</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV.1" Name="v1">
      <FormDef OID="DM" Name="Demographics" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG.DM" Mandatory="No"/>
      </FormDef>
      <ItemGroupDef OID="IG.DM" Name="Demographics" Repeating="No">
        <ItemRef ItemOID="IT.SEX" Mandatory="No"/>
      </ItemGroupDef>
      <ItemDef OID="IT.SEX" Name="SEX" DataType="text"/>
      <ConditionDef OID="COND.BAD" Name="Broken Rule" imednet:RuleSource="imednet" imednet:ContainerForm="DM">
        <Description><TranslatedText>Broken</TranslatedText></Description>
        <FormalExpression Context="imednet:logicRawXml">&lt;unclosed</FormalExpression>
      </ConditionDef>
    </MetaDataVersion>
  </Study>
</ODM>
"""
    dd = load_from_cdisc_odm(malformed_odm, strict=False)
    assert len(dd.forms[0].businessLogic) == 1
    assert "malformedLogicXml" in dd.forms[0].businessLogic[0].extraAttributes
    schema_errors = validate_dd(dd)
    assert any("logic" in err for err in schema_errors)

    # 2. Both DataDictionary and ODM exchange models enforce extra="forbid"
    with pytest.raises(ValidationError):
        Metadata(studyKey="ST_1", undeclaredField="nope")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        OdmItemData(item_oid="IT.SEX", value="M", undeclaredField="nope")  # type: ignore[call-arg]

    # 3. Unified PHI sanitizer (#2119) scrubs multiline JSON and URL query PHI in ODM comments/reasons
    rev = RecordRevision(
        study_key="ONCO_01",
        record_id=1001,
        record_revision=3,
        user="auditor@clinic.org",
        reason_for_change='Corrected payload {"birthDate": "1980-04-12"} via /records?patientName=JaneDoe',
        date_created=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
    )
    aud = build_odm_audit_record_from_revision(rev, mask_phi=True)
    assert aud.reason_for_change is not None
    assert "1980-04-12" not in aud.reason_for_change
    assert "JaneDoe" not in aud.reason_for_change
    assert "***MASKED***" in aud.reason_for_change

    q = Query(
        study_key="ONCO_01",
        annotation_id=901,
        record_id=1001,
        variable="SEX",
        description="Check subject record",
        query_comments=[
            QueryComment(
                sequence=1,
                user="cra@clinic.org",
                comment='Verify {\n  "patientName": "Alice",\n  "sex": "F"\n}',
                annotation_status="Open",
            )
        ],
    )
    anns = build_odm_annotations_from_query(q, mask_phi=True)
    assert len(anns) == 1
    assert anns[0].comment is not None
    assert "Alice" not in anns[0].comment.text
    assert "***MASKED***" in anns[0].comment.text
