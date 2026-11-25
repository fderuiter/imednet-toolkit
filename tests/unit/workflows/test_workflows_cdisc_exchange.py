"""Unit tests for CDISC ODM 1.3.2 and Dataset-JSON v1.1 methods in imednet_workflows."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imednet.spi.cdisc import (
    DatasetJsonClinicalData,
    DatasetJsonDataset,
    OdmAnnotation,
    OdmAuditRecord,
    validate_dataset_json,
)
from imednet.spi.models import (
    Form as SdkForm,
)
from imednet.spi.models import (
    Query,
    QueryComment,
    Record,
    RecordRevision,
    Site,
    User,
)
from imednet.spi.models import (
    Variable as SdkVariable,
)
from imednet_workflows import DataExtractionWorkflow, QueryManagementWorkflow, RecordMapper
from imednet_workflows.data_extraction import (
    _list_sdk_resource,
    _matches_local_date_bound,
    _parse_date_created_utc,
)


def test_data_extraction_date_helpers_and_resource_listing() -> None:
    """Verify UTC date parsing, comparison bounds, and facade vs endpoint resource listing."""
    assert _parse_date_created_utc(None) is None
    assert _parse_date_created_utc("   ") is None
    assert _parse_date_created_utc("not-a-date") is None
    assert _parse_date_created_utc(12345) is None
    assert _parse_date_created_utc(date(2026, 4, 1)) == datetime(2026, 4, 1, tzinfo=UTC)
    naive_dt = datetime(2026, 4, 1, 8, 30)
    assert _parse_date_created_utc(naive_dt) == datetime(2026, 4, 1, 8, 30, tzinfo=UTC)
    assert _parse_date_created_utc("2026-04-01T08:30:00") == datetime(2026, 4, 1, 8, 30, tzinfo=UTC)

    sample_dt = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
    assert not _matches_local_date_bound(None, "==", "2026-04-02")
    assert not _matches_local_date_bound(sample_dt, "==", 999)
    assert not _matches_local_date_bound(sample_dt, "==", "invalid-bound")
    assert _matches_local_date_bound(sample_dt, "==", sample_dt)
    assert _matches_local_date_bound(sample_dt, "!=", date(2026, 4, 1))
    assert _matches_local_date_bound(sample_dt, "<", "2026-04-03")
    assert _matches_local_date_bound(sample_dt, "<=", "2026-04-02")
    assert _matches_local_date_bound(sample_dt, ">", "2026-04-01")
    assert _matches_local_date_bound(sample_dt, ">=", "2026-04-02")
    with pytest.raises(ValueError, match="Unsupported dateCreated filter operator"):
        _matches_local_date_bound(sample_dt, "~=", "2026-04-02")

    # Non-mock facade vs non-mock raw endpoint SDK
    facade_obj = SimpleNamespace(get_sites=lambda study_key, **_kw: [f"facade:{study_key}"])
    assert _list_sdk_resource(facade_obj, "get_sites", "sites", "S1") == ["facade:S1"]

    raw_ep_sdk = SimpleNamespace(
        sites=SimpleNamespace(list=lambda *, study_key, **_kw: [f"ep:{study_key}"])
    )
    assert _list_sdk_resource(raw_ep_sdk, "get_sites", "sites", "S1") == ["ep:S1"]


def test_data_extraction_odm_audit_records_facade_and_endpoint_variants() -> None:
    """Verify extract_audit_trail and extract_odm_audit_records across facade, raw SDK, and duplicate IDs."""
    revisions = [
        RecordRevision(
            study_key="ST_1",
            record_revision_id=501,
            record_id=10,
            site_id=1,
            user="investigator1",
            role="Investigator",
            reason_for_change="Initial save",
            date_created=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        ),
        # Duplicate revision ID to exercise collision disambiguation
        RecordRevision(
            study_key="ST_1",
            record_revision_id=501,
            record_id=10,
            site_name="Central Clinic",
            user="cra@example.org",
            role="Monitor",
            reason_for_change="Patient John Doe DOB verified",
            date_created=datetime(2026, 4, 2, 11, 0, tzinfo=UTC),
        ),
        RecordRevision(
            study_key="ST_1",
            record_revision_id=502,
            record_id=99,
            site_id=2,
            user="dm_user",
            role="Data Manager",
            reason_for_change="Out of window",
            date_created=datetime(2026, 4, 5, 15, 0, tzinfo=UTC),
        ),
    ]
    sites = [Site(study_key="ST_1", site_id=1, site_name="Central Clinic")]
    users = [
        User(
            user_id="u-1",
            login="investigator1",
            first_name="Alice",
            last_name="Investigator",
            email="alice@clinic.org",
        )
    ]

    captured_kwargs: dict[str, object] = {}

    def _list_revisions(**kw: object) -> list[RecordRevision]:
        captured_kwargs.update(kw)
        return list(revisions)

    raw_sdk = SimpleNamespace(
        record_revisions=SimpleNamespace(list=_list_revisions),
        sites=SimpleNamespace(list=lambda **_kw: list(sites)),
        users=SimpleNamespace(list=lambda **_kw: list(users)),
    )
    wf = DataExtractionWorkflow(raw_sdk)  # type: ignore[arg-type]

    # Exercise date bounds + existing scalar dateCreated filter (first bound sent upstream, remaining evaluated locally)
    filtered_trail = wf.extract_audit_trail(
        "ST_1",
        start_date="2026-04-01",
        end_date="2026-04-03",
        user_filter={"dateCreated": "2026-04-01T10:00:00Z"},
    )
    assert captured_kwargs["dateCreated"] == ("==", "2026-04-01T10:00:00Z")
    assert len(filtered_trail) == 2

    audits = wf.extract_odm_audit_records(
        "ST_1",
        start_date="2026-04-01",
        end_date="2026-04-04",
        record_ids=[10],
        mask_phi=True,
    )
    assert len(audits) == 2
    assert all(isinstance(a, OdmAuditRecord) for a in audits)
    assert audits[0].id != audits[1].id
    assert audits[0].user_oid == "USR.u-1"
    assert audits[0].location_oid == "LOC.1"
    # Second revision used an email user not in user_lookup -> hashed USR.<digest> under mask_phi=True
    assert audits[1].user_oid.startswith("USR.")
    assert "@" not in audits[1].user_oid

    # Also exercise MagicMock facade path on DataExtractionWorkflow
    mock_facade = MagicMock()
    mock_facade.get_record_revisions.return_value = list(revisions)
    mock_facade.get_sites.return_value = list(sites)
    mock_facade.get_users.return_value = list(users)
    wf_mock = DataExtractionWorkflow(mock_facade)
    mock_audits = wf_mock.extract_odm_audit_records("ST_1", mask_phi=False)
    assert len(mock_audits) == 3


def test_query_management_extract_odm_annotations_filters_and_sdk_variants() -> None:
    """Verify QueryManagementWorkflow.extract_odm_annotations with unresolved_only, record_ids, and state_filter."""
    queries = [
        Query(
            study_key="ST_1",
            query_id=101,
            record_id=10,
            subject_key="SUBJ-1",
            form_key="AE",
            variable="AETERM",
            description="Clarify onset",
            type="Manual",
            date_created=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
            query_comments=[
                QueryComment(
                    sequence=1,
                    annotation_status="Open",
                    user="cra_user",
                    comment="Please confirm onset time.",
                    closed=False,
                    date="2026-04-01T09:00:00Z",
                )
            ],
        ),
        Query(
            study_key="ST_1",
            query_id=102,
            record_id=10,
            subject_key="SUBJ-1",
            form_key="DM",
            variable="PATIENT_NAME",
            description="Sensitive name check",
            type="Auto",
            date_created=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
            query_comments=[
                QueryComment(
                    sequence=1,
                    annotation_status="Closed",
                    user="site_coord",
                    comment="Alice Secret Name",
                    closed=True,
                    date="2026-04-01T11:00:00Z",
                )
            ],
        ),
        Query(
            study_key="ST_1",
            query_id=103,
            record_id=99,
            subject_key="SUBJ-2",
            form_key="AE",
            variable="AESEV",
            description="Other record query",
            type="Manual",
            date_created=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
            query_comments=[],
        ),
    ]

    # Non-mock endpoint SDK fallback
    ep_sdk = SimpleNamespace(queries=SimpleNamespace(list=lambda **_kw: list(queries)))
    qwf = QueryManagementWorkflow(ep_sdk)  # type: ignore[arg-type]

    open_only = qwf.extract_odm_annotations("ST_1", unresolved_only=True, record_ids=[10])
    assert len(open_only) == 1
    assert isinstance(open_only[0], OdmAnnotation)
    assert open_only[0].comment is not None
    assert "Please confirm onset time." in open_only[0].comment.value

    closed_masked = qwf.extract_odm_annotations(
        "ST_1",
        record_ids=[10],
        state_filter="Closed",
        mask_phi=True,
    )
    assert len(closed_masked) == 1
    assert closed_masked[0].comment is not None
    assert "***MASKED***" in closed_masked[0].comment.value
    assert "Alice Secret Name" not in closed_masked[0].comment.value

    # Also exercise MagicMock facade path
    mock_facade = MagicMock()
    mock_facade.get_queries.return_value = list(queries)
    qwf_mock = QueryManagementWorkflow(mock_facade)
    all_anns = qwf_mock.extract_odm_annotations("ST_1")
    assert len(all_anns) == 3
    assert [a.seq_num for a in all_anns] == [1, 2, 3]


def test_record_mapper_dataset_json_and_clinical_data_cdash_and_sdtm() -> None:
    """Verify RecordMapper.dataset_json and dataset_json_clinical_data across CDASH and SDTM modes."""
    records = [
        Record(
            study_key="ST_1",
            record_id=1,
            form_id=10,
            form_key="DM",
            subject_id=1,
            subject_key="SUBJ-01",
            site_id=50,
            visit_id=100,
            date_created=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
            record_data={"SEX": "F", "AGE": 41, "PATIENT_NAME": "Jane Doe"},
        ),
        Record(
            study_key="ST_1",
            record_id=2,
            form_id=20,
            form_key="AE",
            subject_id=1,
            subject_key="SUBJ-01",
            site_id=50,
            visit_id=100,
            date_created=datetime(2026, 4, 2, 9, 0, tzinfo=UTC),
            date_modified=datetime(2026, 4, 2, 9, 0, tzinfo=UTC),
            record_data={"AETERM": "Fatigue", "AESEV": "MILD"},
        ),
    ]

    sdk = MagicMock()
    sdk.forms.list.return_value = [
        SdkForm(study_key="ST_1", form_id=10, form_key="DM", form_name="Demographics"),
        {"formId": 20, "formKey": "AE", "formName": "Adverse Events", "formType": "Subject"},
        {"formId": 30, "formName": "MissingKeyForm"},
    ]
    sdk.variables.list.return_value = [
        SdkVariable(
            study_key="ST_1",
            variable_id=1,
            variable_name="SEX",
            label="Sex",
            variable_type="Text",
            form_id=10,
            form_key="DM",
            sequence=1,
        ),
        SdkVariable(
            study_key="ST_1",
            variable_id=2,
            variable_name="AGE",
            label="Age",
            variable_type="Integer",
            form_id=10,
            form_key="DM",
            sequence=2,
        ),
        SdkVariable(
            study_key="ST_1",
            variable_id=3,
            variable_name="PATIENT_NAME",
            label="Patient Name",
            variable_type="Text",
            form_id=10,
            form_key="DM",
            sequence=3,
        ),
        SdkVariable(
            study_key="ST_1",
            variable_id=4,
            variable_name="AETERM",
            label="Adverse Event Term",
            variable_type="Text",
            form_id=20,
            form_key="AE",
            sequence=1,
        ),
        # Variable with unmapped form to exercise fallback form creation in _try_load_datadict
        SdkVariable(
            study_key="ST_1",
            variable_id=5,
            variable_name="ORPHAN_VAR",
            label="Orphan Variable",
            variable_type="Text",
            form_id=999,
            form_key="ORPHAN",
            sequence=1,
        ),
    ]
    sdk.records.iter_records.side_effect = lambda **_kw: list(records)
    sdk.records.list.side_effect = lambda **_kw: list(records)

    mapper = RecordMapper(sdk)

    # 1. CDASH single dataset with form_whitelist & variable_whitelist
    dm_ds = mapper.dataset_json(
        "ST_1",
        name="DM",
        form_whitelist=[10],
        variable_whitelist=["SEX", "PATIENT_NAME"],
        creation_date_time="2026-04-03T00:00:00Z",
    )
    assert isinstance(dm_ds, DatasetJsonDataset)
    validate_dataset_json(dm_ds)
    assert dm_ds.name == "DM"

    # 2. SDTM single dataset with variable_whitelist
    ae_sdtm = mapper.dataset_json(
        "ST_1",
        as_sdtm=True,
        domain="AE",
        variable_whitelist=["AETERM"],
        creation_date_time="2026-04-03T00:00:00Z",
    )
    validate_dataset_json(ae_sdtm)
    assert ae_sdtm.item_group_oid == "IG.AE"

    # 3. CDASH clinicalData bundle with whitelists
    cdash_bundle = mapper.dataset_json_clinical_data(
        "ST_1",
        form_whitelist=[10, 20],
        variable_whitelist=["SEX", "AETERM"],
        creation_date_time="2026-04-03T00:00:00Z",
    )
    assert isinstance(cdash_bundle, DatasetJsonClinicalData)
    assert "IG_DM" in cdash_bundle.item_groups_by_oid
    assert "IG_AE" in cdash_bundle.item_groups_by_oid

    # 4. SDTM clinicalData bundle across default domains and explicit domains
    sdtm_bundle = mapper.dataset_json_clinical_data(
        "ST_1",
        as_sdtm=True,
        domains=["DM", "AE"],
        creation_date_time="2026-04-03T00:00:00Z",
    )
    assert set(sdtm_bundle.item_groups_by_oid) == {"IG.DM", "IG.AE"}
    for ds in sdtm_bundle.item_group_data:
        validate_dataset_json(ds)

    # 5. Empty metadata fallback in _try_load_datadict
    empty_sdk = MagicMock()
    empty_sdk.forms.list.return_value = []
    empty_sdk.variables.list.return_value = []
    empty_sdk.records.iter_records.return_value = []
    empty_mapper = RecordMapper(empty_sdk)
    assert empty_mapper._try_load_datadict("ST_EMPTY") is None
    empty_ds = empty_mapper.dataset_json("ST_EMPTY", creation_date_time="2026-04-03T00:00:00Z")
    validate_dataset_json(empty_ds)


def test_record_mapper_expanded_sdtm_domains_and_aliases() -> None:
    """Verify RecordMapper Dataset-JSON resolves expanded SDTM domains (CM, EX, MH, DV/PD)."""
    sdk = MagicMock()
    sdk.forms.list.return_value = [
        SimpleNamespace(
            form_id=30, form_key="CONMEDS", form_name="Concomitant Meds", form_type="Subject"
        ),
        SimpleNamespace(form_id=40, form_key="EXPOSURE", form_name="Exposure", form_type="Subject"),
        SimpleNamespace(
            form_id=50, form_key="MEDHIST", form_name="Medical History", form_type="Subject"
        ),
        SimpleNamespace(
            form_id=60, form_key="PD", form_name="Protocol Deviations", form_type="Subject"
        ),
    ]
    sdk.variables.list.return_value = []
    records = [
        Record(
            study_key="ST_EXP",
            record_id=301,
            form_id=30,
            form_key="CONMEDS",
            subject_id=1,
            subject_key="SUBJ-01",
            record_data={
                "cmTerm": "Aspirin",
                "cmDose": 81,
                "cmDoseUnit": "mg",
                "cmStartDate": "2026-02-01",
            },
        ),
        Record(
            study_key="ST_EXP",
            record_id=401,
            form_id=40,
            form_key="EXPOSURE",
            subject_id=1,
            subject_key="SUBJ-01",
            record_data={"exTrt": "DrugX", "exDose": 100, "exStartDate": "2026-02-02"},
        ),
        Record(
            study_key="ST_EXP",
            record_id=501,
            form_id=50,
            form_key="MEDHIST",
            subject_id=1,
            subject_key="SUBJ-01",
            record_data={"mhTerm": "Hypertension", "mhStartDate": "2020-01-01"},
        ),
        Record(
            study_key="ST_EXP",
            record_id=601,
            form_id=60,
            form_key="PD",
            subject_id=1,
            subject_key="SUBJ-01",
            record_data={"dvTerm": "Out of window visit", "dvDate": "2026-03-01"},
        ),
    ]
    sdk.records.list.side_effect = lambda *_a, **_kw: list(records)
    sdk.records.iter.side_effect = lambda *_a, **_kw: iter(records)

    mapper = RecordMapper(sdk)

    # Alias resolution ("CONMEDS" -> "CM", "PD" -> "DV")
    cm_ds = mapper.dataset_json(
        "ST_EXP",
        as_sdtm=True,
        domain="CONMEDS",
        creation_date_time="2026-05-01T00:00:00Z",
    )
    validate_dataset_json(cm_ds)
    assert cm_ds.name == "CM"
    assert cm_ds.item_group_oid == "IG.CM"
    assert cm_ds.records == 1

    dv_ds = mapper.dataset_json(
        "ST_EXP",
        as_sdtm=True,
        domain="PD",
        creation_date_time="2026-05-01T00:00:00Z",
    )
    validate_dataset_json(dv_ds)
    assert dv_ds.name == "DV"
    assert dv_ds.item_group_oid == "IG.DV"
    assert dv_ds.records == 1

    # Automatic discovery across expanded SDTM_DOMAIN_SPECS when domains=None
    bundle = mapper.dataset_json_clinical_data(
        "ST_EXP",
        as_sdtm=True,
        creation_date_time="2026-05-01T00:00:00Z",
    )
    assert set(bundle.item_groups_by_oid) == {"IG.CM", "IG.EX", "IG.MH", "IG.DV"}
    for ds in bundle.item_group_data:
        validate_dataset_json(ds)
