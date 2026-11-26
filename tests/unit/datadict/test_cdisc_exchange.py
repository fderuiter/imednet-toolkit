"""Unit tests for unified CDISC ODM 1.3.2 and Dataset-JSON v1.1 exchange."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from imednet import spi
from imednet.datadict import (
    DatasetJsonEnvelope,
    DatasetJsonItem,
    OdmArchive,
    OdmItemData,
    OdmItemGroupData,
    OdmReferenceData,
    build_dataset_json_envelope,
    build_odm_archive,
    get_dataset_json_schema,
    load_cdisc_odm_archive,
    load_dataset_json,
    load_from_cdisc_odm,
    load_odm_archive,
    to_dataset_json,
    to_dataset_json_file,
    to_odm_file,
    to_odm_xml,
    validate_dataset_json,
    validate_odm_xml,
    write_odm_xml_stream,
)
from imednet.datadict.models import (
    Choice,
    DataDictionary,
    Metadata,
)
from imednet.datadict.models import (
    Form as DictForm,
)
from imednet.datadict.models import (
    Variable as DictVar,
)
from imednet.models.forms import Form
from imednet.models.queries import Query, QueryComment, QueryStatus
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.users import Role, User
from imednet.models.variables import Variable
from imednet.models.visits import Visit


def _sample_clinical_bundle() -> dict[str, object]:
    """Create a rich, deterministic clinical trial fixture covering all ODM 1.3.2 domains."""
    study = Study(
        study_key="ONCO_01",
        study_name="Oncology Phase II",
        study_description="Multicenter open-label trial",
    )
    sites = [
        Site(site_id=10, site_name="Memorial Site 01", site_enrollment_status="Active"),
    ]
    users = [
        User(
            user_id="usr-101",
            login="jinvestigator",
            first_name="Jane",
            last_name="Investigator",
            email="jane.investigator@hospital.example",
            roles=[Role(role_id="PI", name="Principal Investigator")],
        )
    ]
    forms = [
        Form(form_id=301, form_key="DM", form_name="Demographics", form_type="Subject"),
        Form(form_id=302, form_key="VS", form_name="Vital Signs", form_type="Visit"),
    ]
    variables = [
        Variable(
            variable_id=1,
            variable_name="SUBJID",
            label="Subject Identifier",
            variable_type="text",
            form_id=301,
            form_key="DM",
            sequence=1,
        ),
        Variable(
            variable_id=2,
            variable_name="patientName",
            label="Patient Full Name",
            variable_type="text",
            form_id=301,
            form_key="DM",
            sequence=2,
        ),
        Variable(
            variable_id=3,
            variable_name="BRTHDAT",
            label="Birth Date",
            variable_type="date",
            form_id=301,
            form_key="DM",
            sequence=3,
        ),
        Variable(
            variable_id=4,
            variable_name="SEX",
            label="Biological Sex",
            variable_type="radio",
            form_id=301,
            form_key="DM",
            sequence=4,
        ),
        Variable(
            variable_id=5,
            variable_name="SYSBP",
            label="Systolic Blood Pressure",
            variable_type="int",
            form_id=302,
            form_key="VS",
            sequence=1,
        ),
    ]
    subjects = [
        Subject(
            subject_id=1001,
            subject_key="SUBJ-001",
            site_id=10,
            site_name="Memorial Site 01",
            subject_status="Enrolled",
        )
    ]
    visits = [
        Visit(
            visit_id=501,
            interval_id=1,
            interval_name="Screening",
            subject_id=1001,
            subject_key="SUBJ-001",
        )
    ]
    records = [
        Record(
            record_id=9001,
            study_key="ONCO_01",
            subject_id=1001,
            subject_key="SUBJ-001",
            site_id=10,
            site_name="Memorial Site 01",
            form_id=301,
            form_key="DM",
            visit_id=None,
            date_modified="2026-03-01T12:00:00Z",
            record_data={
                "SUBJID": "001",
                "patientName": "Alice Example",
                "BRTHDAT": "1980-07-15",
                "SEX": "F",
            },
        ),
        Record(
            record_id=9002,
            study_key="ONCO_01",
            subject_id=1001,
            subject_key="SUBJ-001",
            site_id=10,
            site_name="Memorial Site 01",
            form_id=302,
            form_key="VS",
            visit_id=501,
            interval_id=1,
            interval_name="Screening",
            date_modified="2026-03-02T09:30:00Z",
            record_data={"SYSBP": 122},
        ),
    ]
    revisions = [
        RecordRevision(
            record_revision_id=7001,
            record_id=9002,
            record_revision=1,
            data_revision=1,
            subject_key="SUBJ-001",
            site_id=10,
            user="jinvestigator",
            role="Principal Investigator",
            reason_for_change="Corrected cuff reading per source chart jane@clinic.example",
            date_created="2026-03-02T09:35:00Z",
        )
    ]
    queries = [
        Query(
            annotation_id=8001,
            annotation_type="Monitor Query",
            record_id=9002,
            subject_key="SUBJ-001",
            variable="SYSBP",
            description="Confirm resting blood pressure measurement",
            status=QueryStatus.OPEN,
            query_comments=[
                QueryComment(
                    sequence=1,
                    user="jinvestigator",
                    comment="Verified against nurse vitals sheet john.doe@clinic.example",
                    closed=False,
                    date="2026-03-02T10:00:00Z",
                )
            ],
        )
    ]
    reference_data = [
        OdmReferenceData(
            study_oid="ONCO_01",
            metadata_version_oid="MDV.ONCO_01",
            item_group_data=[
                OdmItemGroupData(
                    item_group_oid="IG.REF_RANGES",
                    item_group_repeat_key="1",
                    items=[
                        OdmItemData(item_oid="IT.REF_HIGH", value="140"),
                        OdmItemData(item_oid="IT.REF_LOW", value="90"),
                    ],
                )
            ],
        )
    ]
    data_dict = DataDictionary(
        metadata=Metadata(
            studyKey="ONCO_01",
            extraAttributes={
                "studyName": "Oncology Phase II",
                "studyDescription": "Multicenter open-label trial",
                "protocolName": "ONCO-01",
                "metaDataVersionOID": "MDV.ONCO_01",
            },
        ),
        forms=[
            DictForm(
                formKey="DM",
                formName="Demographics",
                variables=[
                    DictVar(
                        variableName="SUBJID",
                        label="Subject Identifier",
                        variableType="Text",
                        sequence=1,
                    ),
                    DictVar(
                        variableName="patientName",
                        label="Patient Full Name",
                        variableType="Text",
                        sequence=2,
                    ),
                    DictVar(
                        variableName="BRTHDAT", label="Birth Date", variableType="Date", sequence=3
                    ),
                    DictVar(
                        variableName="SEX",
                        label="Biological Sex",
                        variableType="Radio",
                        sequence=4,
                        choices=[
                            Choice(choiceValue="F", choiceText="Female", position=1),
                            Choice(choiceValue="M", choiceText="Male", position=2),
                        ],
                    ),
                ],
            ),
            DictForm(
                formKey="VS",
                formName="Vital Signs",
                variables=[
                    DictVar(
                        variableName="SYSBP",
                        label="Systolic Blood Pressure",
                        variableType="Integer",
                        sequence=1,
                    ),
                ],
            ),
            DictForm(
                formKey="REF_RANGES",
                formName="Reference Ranges",
                extraAttributes={"itemGroupOID": "IG.REF_RANGES"},
                variables=[
                    DictVar(
                        variableName="REF_HIGH",
                        label="Reference High",
                        variableType="Integer",
                        sequence=1,
                        extraAttributes={"itemOID": "IT.REF_HIGH"},
                    ),
                    DictVar(
                        variableName="REF_LOW",
                        label="Reference Low",
                        variableType="Integer",
                        sequence=2,
                        extraAttributes={"itemOID": "IT.REF_LOW"},
                    ),
                ],
            ),
        ],
    )
    return {
        "study": study,
        "sites": sites,
        "users": users,
        "forms": forms,
        "variables": variables,
        "subjects": subjects,
        "visits": visits,
        "records": records,
        "revisions": revisions,
        "queries": queries,
        "reference_data": reference_data,
        "data_dictionary": data_dict,
    }


def test_exchange_models_are_frozen_and_exposed_on_spi() -> None:
    """Exchange models enforce read-only immutability, forbid extra keys, and resolve via imednet.spi."""
    assert spi.cdisc.OdmArchive is OdmArchive
    assert spi.cdisc.DatasetJsonEnvelope is DatasetJsonEnvelope
    assert spi.models.OdmArchive is OdmArchive
    assert spi.models.DatasetJsonEnvelope is DatasetJsonEnvelope

    item = OdmItemData(item_oid="IT.SYSBP", value="120")
    with pytest.raises(ValidationError):
        item.value = "130"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        OdmItemData(item_oid="IT.SYSBP", value="120", unexpected_field="x")  # type: ignore[call-arg]

    dj_item = DatasetJsonItem(
        OID="IT.SYSBP",
        name="SYSBP",
        label="Systolic BP",
        dataType="integer",
    )
    with pytest.raises(ValidationError):
        dj_item.name = "DIABP"  # type: ignore[misc]

    # DatasetJsonItemGroup arity & records invariants
    from imednet.datadict import DatasetJsonClinicalData, DatasetJsonItemGroup

    with pytest.raises(ValidationError, match="records"):
        DatasetJsonItemGroup(
            records=2,
            name="VS",
            label="Vitals",
            items=(dj_item,),
            itemData=((120,),),
        )

    with pytest.raises(ValidationError, match="row 0 length"):
        DatasetJsonItemGroup(
            records=1,
            name="VS",
            label="Vitals",
            items=(dj_item,),
            itemData=((120, 80),),
        )

    with pytest.raises(ValidationError, match="clinicalData or referenceData"):
        DatasetJsonEnvelope(creationDateTime="2026-01-01T00:00:00Z")

    # Validator error branches on missing files, malformed payloads, and cell type mismatches
    assert validate_odm_xml(Path("/nonexistent/missing.xml"))
    assert validate_odm_xml(b"<NotODM/>")
    assert validate_dataset_json(Path("/nonexistent/missing.json"))
    assert validate_dataset_json("[]")
    mismatch_dict = DatasetJsonEnvelope(
        creationDateTime="2026-01-01T00:00:00Z",
        clinicalData=DatasetJsonClinicalData(
            studyOID="S1",
            metaDataVersionOID="MDV.1",
            itemGroupData={
                "IG.VS": DatasetJsonItemGroup(
                    records=1,
                    name="VS",
                    label="Vitals",
                    items=(
                        DatasetJsonItem(
                            OID="ITEMGROUPDATASEQ",
                            name="ITEMGROUPDATASEQ",
                            label="Seq",
                            type="integer",
                        ),
                        dj_item,
                    ),
                    itemData=((1, "not_an_int"),),
                )
            },
        ),
    ).to_json_dict()
    assert any("does not match declared type" in e for e in validate_dataset_json(mismatch_dict))


def test_odm_132_round_trip_and_xsd_validation(tmp_path: Path) -> None:
    """Round-tripping an ODM 1.3.2 snapshot preserves metadata, AdminData, ReferenceData, ClinicalData, AuditRecords, and Annotations."""
    bundle = _sample_clinical_bundle()
    archive = build_odm_archive(
        study_key="ONCO_01",
        study=bundle["study"],  # type: ignore[arg-type]
        data_dictionary=bundle["data_dictionary"],  # type: ignore[arg-type]
        forms=bundle["forms"],  # type: ignore[arg-type]
        variables=bundle["variables"],  # type: ignore[arg-type]
        sites=bundle["sites"],  # type: ignore[arg-type]
        users=bundle["users"],  # type: ignore[arg-type]
        subjects=bundle["subjects"],  # type: ignore[arg-type]
        visits=bundle["visits"],  # type: ignore[arg-type]
        records=bundle["records"],  # type: ignore[arg-type]
        revisions=bundle["revisions"],  # type: ignore[arg-type]
        queries=bundle["queries"],  # type: ignore[arg-type]
        reference_data=bundle["reference_data"],  # type: ignore[arg-type]
        file_oid="OID.ONCO_01.SNAPSHOT",
        creation_datetime="2026-03-05T00:00:00Z",
        mask_phi=False,
    )

    xml_str = to_odm_xml(archive, mask_phi=False)
    assert 'ODMVersion="1.3.2"' in xml_str
    assert "<AdminData" in xml_str
    assert "<ReferenceData" in xml_str
    assert "<ClinicalData" in xml_str
    assert "<AuditRecord" in xml_str
    assert "<Annotation" in xml_str
    assert "vault" not in xml_str.lower()

    # Validate against bundled official CDISC ODM 1.3.2 XSD
    xsd_errors = validate_odm_xml(xml_str)
    assert xsd_errors == []

    # Test streaming writer produces identical schema-valid XML
    buf = io.StringIO()
    write_odm_xml_stream(archive, buf, mask_phi=False)
    assert buf.getvalue() == xml_str

    odm_path = tmp_path / "snapshot.xml"
    to_odm_file(archive, odm_path, mask_phi=False)
    assert validate_odm_xml(odm_path) == []

    # Full archive round-trip
    reloaded = load_odm_archive(odm_path, strict=True)
    assert load_cdisc_odm_archive(xml_str, strict=True) == reloaded
    assert reloaded.file_oid == "OID.ONCO_01.SNAPSHOT"
    assert reloaded.odm_version == "1.3.2"
    assert reloaded.data_dictionary.metadata.studyKey == "ONCO_01"
    assert {f.formKey for f in reloaded.data_dictionary.forms} == {"DM", "VS", "REF_RANGES"}

    # AdminData preserved
    assert len(reloaded.admin_data) == 1
    admin = reloaded.admin_data[0]
    assert len(admin.users) == 1
    assert admin.users[0].login_name == "jinvestigator"
    assert admin.users[0].first_name == "Jane"
    assert admin.users[0].last_name == "Investigator"
    assert len(admin.locations) == 1
    assert admin.locations[0].name == "Memorial Site 01"
    assert len(admin.locations[0].metadata_version_refs) == 1
    assert admin.locations[0].metadata_version_refs[0].metadata_version_oid == "MDV.ONCO_01"

    # ReferenceData preserved
    assert len(reloaded.reference_data) == 1
    ref_ig = reloaded.reference_data[0].item_group_data[0]
    assert ref_ig.item_group_oid == "IG.REF_RANGES"
    assert {it.item_oid: it.value for it in ref_ig.items} == {
        "IT.REF_HIGH": "140",
        "IT.REF_LOW": "90",
    }

    # ClinicalData, AuditRecords, and Annotations preserved
    assert len(reloaded.clinical_data) == 1
    clin = reloaded.clinical_data[0]
    assert len(clin.subjects) == 1
    subj = clin.subjects[0]
    assert subj.subject_key == "SUBJ-001"
    assert subj.site_location_oid == admin.locations[0].oid

    # Unscheduled / subject-level DM form and scheduled Screening VS form
    assert len(subj.forms) == 1
    assert subj.forms[0].form_oid == "DM"
    dm_items = {it.item_oid: it.value for it in subj.forms[0].item_group_data[0].items}
    assert dm_items["BRTHDAT"] == "1980-07-15"
    assert dm_items["patientName"] == "Alice Example"

    assert len(subj.study_events) == 1
    vs_form = subj.study_events[0].forms[0]
    assert vs_form.form_oid == "VS"
    vs_ig = vs_form.item_group_data[0]
    assert vs_ig.items[0].value == "122"

    # AuditRecord preserved (either nested or in AuditRecords pool)
    all_audits = list(clin.audit_records) + list(vs_ig.audit_records)
    if vs_ig.items[0].audit_record is not None:
        all_audits.append(vs_ig.items[0].audit_record)
    assert len(all_audits) >= 1
    audit = all_audits[0]
    assert audit.user_oid == admin.users[0].oid
    assert audit.location_oid == admin.locations[0].oid
    assert audit.datetime_stamp == "2026-03-02T09:35:00Z"
    assert "Corrected cuff reading" in (audit.reason_for_change or "")

    # Annotation preserved
    all_annots = list(clin.annotations) + list(vs_ig.annotations) + list(vs_ig.items[0].annotations)
    assert len(all_annots) >= 1
    annot = all_annots[0]
    assert "Confirm resting blood pressure" in (annot.comment or "")
    assert any(fl.flag_value == "OPEN" for fl in annot.flags)

    # Re-serializing reloaded archive is deterministic
    re_emitted = to_odm_xml(reloaded, mask_phi=False)
    assert validate_odm_xml(re_emitted) == []
    assert load_odm_archive(re_emitted, strict=True) == reloaded


@pytest.mark.parametrize(
    "extra_xml,expected_label",
    [
        (
            '<ClinicalData StudyOID="S1" MetaDataVersionOID="MDV1">'
            '<SubjectData SubjectKey="SUBJ1"/></ClinicalData>',
            "ClinicalData",
        ),
        (
            '<AdminData StudyOID="S1"><User OID="USR.1"><LoginName>u1</LoginName></User></AdminData>',
            "AdminData",
        ),
        (
            '<ReferenceData StudyOID="S1" MetaDataVersionOID="MDV1"/>',
            "ReferenceData",
        ),
        (
            "<Association/>",
            "Association",
        ),
    ],
)
def test_load_from_cdisc_odm_strict_rejects_unparsed_containers(
    extra_xml: str, expected_label: str
) -> None:
    """Calling load_from_cdisc_odm with strict=True raises ValueError on ClinicalData/AdminData/ReferenceData."""
    odm_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2" FileType="Snapshot" FileOID="F1" CreationDateTime="2026-01-01T00:00:00Z">
  <Study OID="S1">
    <GlobalVariables>
      <StudyName>Study 1</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>P1</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV1" Name="v1">
      <FormDef OID="F_DM" Name="Demographics" Repeating="No"/>
    </MetaDataVersion>
  </Study>
  {extra_xml}
</ODM>
"""
    with pytest.raises(ValueError, match=expected_label):
        load_from_cdisc_odm(odm_doc, strict=True)

    # Non-strict mode records an explicit warning instead of silently dropping containers
    with pytest.warns(UserWarning, match=expected_label):
        dd = load_from_cdisc_odm(odm_doc, strict=False)
    warnings_list = dd.metadata.validationWarnings
    assert any(expected_label in str(w) for w in warnings_list)
    assert any("load_odm_archive" in str(w) for w in warnings_list)


def test_load_odm_archive_strict_catches_broken_oid_references() -> None:
    """load_odm_archive(strict=True) rejects broken FormOID / UserOID references."""
    broken_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2" FileType="Snapshot" FileOID="F1" CreationDateTime="2026-01-01T00:00:00Z">
  <Study OID="S1">
    <GlobalVariables>
      <StudyName>Study 1</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>P1</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV1" Name="v1">
      <FormDef OID="F_DM" Name="Demographics" Repeating="No"/>
    </MetaDataVersion>
  </Study>
  <ClinicalData StudyOID="S1" MetaDataVersionOID="MDV1">
    <SubjectData SubjectKey="SUBJ1">
      <FormData FormOID="UNKNOWN_FORM"/>
    </SubjectData>
  </ClinicalData>
</ODM>
"""
    with pytest.raises(ValueError, match="UNKNOWN_FORM"):
        load_odm_archive(broken_xml, strict=True)


def test_defusedxml_rejects_xxe_across_odm_loaders_and_validator() -> None:
    """All inbound ODM XML entry points enforce defusedxml and block entity expansion / XXE."""
    xxe_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ODM [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2" FileType="Snapshot" FileOID="F1" CreationDateTime="2026-01-01T00:00:00Z">
  <Study OID="S1">
    <GlobalVariables>
      <StudyName>&xxe;</StudyName>
      <StudyDescription>Desc</StudyDescription>
      <ProtocolName>P1</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV1" Name="v1"/>
  </Study>
</ODM>
"""
    with pytest.raises(DefusedXmlException):
        load_from_cdisc_odm(xxe_payload)

    with pytest.raises(DefusedXmlException):
        load_odm_archive(xxe_payload)

    errors = validate_odm_xml(xxe_payload)
    assert errors and any(
        "DefusedXml" in err or "EntitiesForbidden" in err or "DTDForbidden" in err for err in errors
    )


def test_validate_odm_xml_detects_schema_violations() -> None:
    """validate_odm_xml reports XSD violations on malformed ODM 1.3.2 structures."""
    invalid_odm = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2" FileType="InvalidType" FileOID="F1" CreationDateTime="2026-01-01T00:00:00Z">
  <UnknownElement/>
</ODM>
"""
    errors = validate_odm_xml(invalid_odm)
    assert len(errors) >= 1


def test_dataset_json_v11_cdash_and_sdtm_schema_validation_and_roundtrip(tmp_path: Path) -> None:
    """Dataset-JSON v1.1 envelopes validate against official schema for both CDASH Forms and SDTM domains."""
    bundle = _sample_clinical_bundle()
    schema = get_dataset_json_schema()
    assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12")

    # 1. CDASH Dataset-JSON envelope
    cdash_env = build_dataset_json_envelope(
        study_key="ONCO_01",
        records=bundle["records"],  # type: ignore[arg-type]
        forms=bundle["forms"],  # type: ignore[arg-type]
        variables=bundle["variables"],  # type: ignore[arg-type]
        data_dictionary=bundle["data_dictionary"],  # type: ignore[arg-type]
        as_sdtm=False,
        mask_phi=True,
        creation_datetime="2026-03-05T00:00:00Z",
        file_oid="OID.DJ.CDASH",
    )
    assert cdash_env.dataset_json_version == "1.1.0"
    assert validate_dataset_json(cdash_env) == []

    json_str = to_dataset_json(cdash_env)
    assert validate_dataset_json(json_str) == []
    assert "vault" not in json_str.lower()

    dj_file = tmp_path / "cdash.dataset.json"
    to_dataset_json_file(cdash_env, dj_file)
    assert validate_dataset_json(dj_file) == []

    reloaded_env = load_dataset_json(dj_file)
    assert reloaded_env == cdash_env
    assert "IG.DM" in reloaded_env.clinical_data.item_group_data
    dm_group = reloaded_env.clinical_data.item_group_data["IG.DM"]
    assert dm_group.records == 1
    assert dm_group.items[0].name == "ITEMGROUPDATASEQ"
    # PHI masked by default in CDASH Dataset-JSON
    item_names = [it.name for it in dm_group.items]
    pname_idx = item_names.index("patientName")
    brth_idx = item_names.index("BRTHDAT")
    assert dm_group.item_data[0][pname_idx] == "***MASKED***"
    assert dm_group.item_data[0][brth_idx] == "***MASKED***"

    # 2. SDTM Dataset-JSON envelope (DM and VS)
    sdtm_dm = build_dataset_json_envelope(
        study_key="ONCO_01",
        records=bundle["records"],  # type: ignore[arg-type]
        forms=bundle["forms"],  # type: ignore[arg-type]
        variables=bundle["variables"],  # type: ignore[arg-type]
        visits=bundle["visits"],  # type: ignore[arg-type]
        as_sdtm=True,
        sdtm_domain="DM",
        mask_phi=True,
        creation_datetime="2026-03-05T00:00:00Z",
    )
    assert validate_dataset_json(sdtm_dm) == []
    assert "IG.DM" in sdtm_dm.clinical_data.item_group_data
    sdtm_dm_group = sdtm_dm.clinical_data.item_group_data["IG.DM"]
    sdtm_cols = [it.name for it in sdtm_dm_group.items]
    assert "STUDYID" in sdtm_cols
    assert "DOMAIN" in sdtm_cols
    assert "USUBJID" in sdtm_cols
    if "BRTHDTC" in sdtm_cols:
        brthdtc_val = sdtm_dm_group.item_data[0][sdtm_cols.index("BRTHDTC")]
        # SDTM BRTHDTC truncates birth date to 4-digit year under PHI masking
        assert brthdtc_val in ("1980", "***MASKED***")

    # Negative schema validation check: missing ITEMGROUPDATASEQ first column
    bad_payload = json.loads(json_str)
    bad_payload["clinicalData"]["itemGroupData"]["IG.DM"]["items"][0]["name"] = "NOT_SEQ"
    schema_errs = validate_dataset_json(bad_payload)
    assert len(schema_errs) >= 1


def test_default_phi_masking_across_odm_clinical_audit_annotation_and_admin() -> None:
    """Default mask_phi=True scrubs PHI across ODM ItemData, AuditRecord reasons, Annotations, and AdminData."""
    bundle = _sample_clinical_bundle()
    masked_archive = build_odm_archive(
        study_key="ONCO_01",
        study=bundle["study"],  # type: ignore[arg-type]
        data_dictionary=bundle["data_dictionary"],  # type: ignore[arg-type]
        forms=bundle["forms"],  # type: ignore[arg-type]
        variables=bundle["variables"],  # type: ignore[arg-type]
        sites=bundle["sites"],  # type: ignore[arg-type]
        users=bundle["users"],  # type: ignore[arg-type]
        subjects=bundle["subjects"],  # type: ignore[arg-type]
        visits=bundle["visits"],  # type: ignore[arg-type]
        records=bundle["records"],  # type: ignore[arg-type]
        revisions=bundle["revisions"],  # type: ignore[arg-type]
        queries=bundle["queries"],  # type: ignore[arg-type]
        file_oid="OID.PHI.MASKED",
        creation_datetime="2026-03-05T00:00:00Z",
    )
    masked_xml = to_odm_xml(masked_archive)
    assert validate_odm_xml(masked_xml) == []
    assert "Alice Example" not in masked_xml
    assert "1980-07-15" not in masked_xml
    assert "jane.investigator@hospital.example" not in masked_xml
    assert "jane@clinic.example" not in masked_xml
    assert "john.doe@clinic.example" not in masked_xml
    assert "***MASKED***" in masked_xml


def test_cdisc_builder_edge_cases_coercions_and_validators(tmp_path: Path) -> None:
    """Exercise OID/datetime helpers, fallback metadata, commentless queries, coercions, and validator error paths."""
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from imednet.datadict.cdisc_builders import (
        UNSCHEDULED_EVENT_OID,
        _coerce_dataset_json_cell,
        build_datadict_from_sdk_metadata,
        make_oid_slug,
        make_study_event_oid,
        make_xml_ncname,
        map_queries_to_annotations,
        map_sites_and_users_to_admin_data,
        normalize_odm_datetime,
    )

    assert make_oid_slug(None, fallback="DEF") == "DEF"
    assert make_xml_ncname("9BAD", "part:1") == "ID_9BAD_part_1"
    assert make_study_event_oid(None) == UNSCHEDULED_EVENT_OID
    assert make_study_event_oid("0") == UNSCHEDULED_EVENT_OID
    assert make_study_event_oid("SE.CUSTOM") == "SE.CUSTOM"

    assert normalize_odm_datetime(None, fallback="EPOCH") == "EPOCH"
    assert normalize_odm_datetime("   ", fallback="EPOCH") == "EPOCH"
    assert normalize_odm_datetime(date(2026, 5, 1)) == "2026-05-01T00:00:00Z"
    assert normalize_odm_datetime("05-13-2026 14:06:50") == "2026-05-13T14:06:50Z"
    assert normalize_odm_datetime("2026-05-13") == "2026-05-13T00:00:00Z"
    assert normalize_odm_datetime("not-a-timestamp") == "not-a-timestamp"

    # Metadata builder with choices, formId-only variables, and orphan formKey variables
    dd = build_datadict_from_sdk_metadata(
        "EDGE_STUDY",
        forms=[
            {"formId": 10, "formName": "IdOnlyForm"},
            {"formKey": "", "formId": None},
            {"formKey": "FORM_10", "formId": 10},
        ],
        variables=[
            {"variableName": ""},
            {
                "variableName": "STATUS_CD",
                "variableType": " integer ",
                "formId": 10,
                "choices": [
                    {"value": 1, "label": "Active"},
                    {"value": 2},
                ],
            },
            {
                "variableName": "ORPHAN_VAR",
                "variableType": "float",
                "formKey": "ORPHAN_FORM",
            },
        ],
        intervals=[
            {"intervalName": None, "intervalId": None},
            {"intervalName": "Screening", "forms": [{"formKey": "FORM_10"}]},
            {"intervalName": "Screening", "forms": []},
        ],
    )
    form_keys = {f.formKey for f in dd.forms}
    assert {"FORM_10", "ORPHAN_FORM"} <= form_keys

    # AdminData default fallback when no sites/users provided, plus inferred tokens
    empty_admin = map_sites_and_users_to_admin_data("EDGE_STUDY")
    assert empty_admin.locations[0].oid == "LOC.DEFAULT_SITE"
    assert empty_admin.users[0].oid == "USR.SYSTEM"

    inferred_admin = map_sites_and_users_to_admin_data(
        "EDGE_STUDY",
        sites=[{"siteId": None, "siteName": None}],
        users=[{"login": None, "userId": None}],
        inferred_site_tokens=[(None, None), ("SITE_99", "Site Ninety-Nine")],
        inferred_user_tokens=[None, "   ", "auditor_bot"],
    )
    assert any(loc.oid == "LOC.SITE_99" for loc in inferred_admin.locations)
    assert any(u.oid == "USR.auditor_bot" for u in inferred_admin.users)

    # Queries without comments across record+var, record-only, subject-only, and unattached scopes
    by_rv, by_r, by_s, unattached = map_queries_to_annotations(
        [
            {"annotationId": 1, "recordId": 10, "variable": "V1", "description": "Check V1"},
            {"annotationId": 2, "recordId": 10, "description": "Record query"},
            {"annotationId": 3, "subjectKey": "SUBJ-1", "description": ""},
            {"annotationId": 4, "description": "Study-level note", "type": "Manual"},
        ],
        mask_phi=True,
    )
    assert (10, "V1") in by_rv
    assert 10 in by_r
    assert "SUBJ-1" in by_s
    assert len(unattached) == 1

    # Archive with undeclared forms/vars, scalar formatting, and include_deleted=True
    edge_archive = build_odm_archive(
        "EDGE_STUDY",
        records=[
            {
                "recordId": 10,
                "subjectKey": "SUBJ-1",
                "formKey": "NEW_FORM",
                "visitId": 77,
                "deleted": True,
                "recordData": {
                    "BOOL_F": False,
                    "NAN_F": float("nan"),
                    "DT_F": datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
                    "DATE_F": date(2026, 3, 1),
                    "OBJ_F": ["a", "b"],
                    "EMPTY_F": "",
                },
            }
        ],
        queries=[
            {"annotationId": 11, "recordId": 10, "description": "Record level"},
            {"annotationId": 12, "subjectKey": "SUBJ-2", "description": "Subject level"},
        ],
        include_deleted=True,
        mask_phi=False,
    )
    xml_bytes = to_odm_xml(edge_archive).encode("utf-8")
    assert validate_odm_xml(xml_bytes) == []

    # Coercion helpers across boolean/integer/float/string edge cases
    assert _coerce_dataset_json_cell("yes", "boolean") is True
    assert _coerce_dataset_json_cell("no", "boolean") is False
    assert _coerce_dataset_json_cell("unknown", "boolean") is None
    assert _coerce_dataset_json_cell(True, "integer") == 1
    assert _coerce_dataset_json_cell(4.0, "integer") == 4
    assert _coerce_dataset_json_cell("42", "integer") == 42
    assert _coerce_dataset_json_cell("bad", "integer") is None
    assert _coerce_dataset_json_cell(True, "float") == 1.0
    assert _coerce_dataset_json_cell(Decimal("2.5"), "float") == 2.5
    assert _coerce_dataset_json_cell("3.25", "float") == 3.25
    assert _coerce_dataset_json_cell("bad", "float") is None

    # Auto-domain SDTM Dataset-JSON (with records and empty-records fallback) + labels/enrichment CDASH
    auto_sdtm = build_dataset_json_envelope(
        "EDGE_STUDY",
        records=[
            {
                "recordId": 1,
                "subjectKey": "SUBJ-1",
                "formKey": "VS",
                "recordData": {"VSTESTCD": "HR", "VSORRES": "72"},
            }
        ],
        as_sdtm=True,
        preserve_record_id=True,
        variable_whitelist=["STUDYID", "DOMAIN", "USUBJID", "VSSEQ", "RECORD_ID"],
    )
    assert validate_dataset_json(auto_sdtm) == []

    empty_sdtm = build_dataset_json_envelope("EDGE_STUDY", records=[], as_sdtm=True)
    assert "IG.DM" in empty_sdtm.clinical_data.item_group_data

    class _DummyPipeline:
        def process(self, data: dict[str, object], *, mask_phi: bool = True) -> dict[str, object]:
            return {**data, "DERIVED_COL": "ok" if mask_phi else "raw"}

    labeled_cdash = build_dataset_json_envelope(
        "EDGE_STUDY",
        data_dictionary=dd,
        records=[
            {
                "recordId": 5,
                "subjectKey": "SUBJ-1",
                "formKey": "FORM_10",
                "recordData": {"STATUS_CD": "non_int_fallback"},
            }
        ],
        use_labels_as_columns=True,
        enrichment_pipeline=_DummyPipeline(),
    )
    assert validate_dataset_json(labeled_cdash) == []

    # Negative validation paths for validate_dataset_json and validate_odm_xml
    assert validate_dataset_json(tmp_path / "missing.json")
    assert validate_dataset_json("[1, 2, 3]") == ["Dataset-JSON root must be a JSON object"]
    assert validate_dataset_json(12345)  # type: ignore[arg-type]
    assert validate_dataset_json({"datasetJSONVersion": "1.1.0"})

    mismatch_dict = auto_sdtm.to_json_dict()
    mismatch_dict["clinicalData"]["itemGroupData"]["IG.VS"]["itemData"][0][0] = "not-an-int"
    assert any("does not match declared type" in e for e in validate_dataset_json(mismatch_dict))

    assert validate_odm_xml(tmp_path / "missing.xml")
    assert validate_odm_xml("<<<not xml")
    assert validate_odm_xml("<WrongRoot/>") == ["Root element must be ODM, got 'WrongRoot'"]
    assert validate_odm_xml(12345)  # type: ignore[arg-type]
