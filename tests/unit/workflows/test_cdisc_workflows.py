"""Workflow unit tests for CDISC Dataset-JSON v1.1, ODM AuditRecord, and ODM Annotation mappers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from imednet.cli import app
from imednet.models.forms import Form
from imednet.models.queries import Query, QueryComment, QueryStatus
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet.spi.cdisc import load_odm_archive, validate_dataset_json, validate_odm_xml
from imednet_workflows import transform_records_to_dataset_json
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.query_management import QueryManagementWorkflow
from imednet_workflows.record_mapper import RecordMapper


def _build_workflow_sdk() -> tuple[MagicMock, list[Record]]:
    forms = [
        Form(form_id=101, form_key="DM", form_name="Demographics", form_type="Subject"),
        Form(form_id=102, form_key="VS", form_name="Vital Signs", form_type="Visit"),
    ]
    variables = [
        Variable(
            variable_id=1,
            variable_name="SUBJID",
            label="Subject ID",
            variable_type="text",
            form_id=101,
            form_key="DM",
            sequence=1,
        ),
        Variable(
            variable_id=2,
            variable_name="patientName",
            label="Patient Name",
            variable_type="text",
            form_id=101,
            form_key="DM",
            sequence=2,
        ),
        Variable(
            variable_id=3,
            variable_name="SYSBP",
            label="Systolic BP",
            variable_type="int",
            form_id=102,
            form_key="VS",
            sequence=1,
        ),
    ]
    records = [
        Record(
            record_id=1,
            study_key="CDISC_WF",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=101,
            form_key="DM",
            date_modified="2026-02-01T10:00:00Z",
            record_data={"SUBJID": "100", "patientName": "Secret Name", "SEX": "M"},
        ),
        Record(
            record_id=2,
            study_key="CDISC_WF",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=102,
            form_key="VS",
            visit_id=600,
            interval_id=201,
            interval_name="Visit 1",
            date_modified="2026-02-01T10:05:00Z",
            record_data={"SYSBP": 124},
        ),
    ]
    revisions = [
        RecordRevision(
            record_revision_id=901,
            record_id=2,
            record_revision=1,
            data_revision=1,
            subject_key="SUBJ-100",
            site_id=1,
            user="cra_user",
            reason_for_change="Initial vitals entry verified by cra@sponsor.example",
            date_created="2026-02-01T10:05:00Z",
        )
    ]
    queries = [
        Query(
            annotation_id=950,
            annotation_type="Monitor Query",
            record_id=2,
            subject_key="SUBJ-100",
            variable="SYSBP",
            description="Verify systolic BP units",
            status=QueryStatus.OPEN,
            query_comments=[
                QueryComment(
                    sequence=1,
                    user="cra_user",
                    comment="Confirmed mmHg with nurse@site.example",
                    closed=False,
                    date="2026-02-01T11:00:00Z",
                )
            ],
        ),
        Query(
            annotation_id=951,
            annotation_type="AutoCheck",
            record_id=1,
            subject_key="SUBJ-100",
            variable="SUBJID",
            description="Closed check",
            status=QueryStatus.CLOSED,
            query_comments=[
                QueryComment(
                    sequence=1,
                    user="cra_user",
                    comment="Closed",
                    closed=True,
                    date="2026-02-01T12:00:00Z",
                )
            ],
        ),
    ]

    sdk = MagicMock()
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.records.iter.side_effect = lambda *_a, **_k: iter(
        [r.model_copy(deep=True) for r in records]
    )
    sdk.records.list.side_effect = lambda *_a, **_k: [r.model_copy(deep=True) for r in records]
    sdk.get_records.side_effect = lambda *_a, **_k: [r.model_copy(deep=True) for r in records]
    sdk.record_revisions.list.return_value = revisions
    sdk.get_record_revisions.return_value = revisions
    sdk.queries.list.return_value = queries
    sdk.get_queries.return_value = queries
    return sdk, records


def test_workflows_record_mapper_audit_and_query_extractors() -> None:
    """Workflow plugins expose Dataset-JSON v1.1, ODM AuditRecord, and ODM Annotation mappers via imednet.spi."""
    sdk, records = _build_workflow_sdk()

    mapper = RecordMapper(sdk)
    cdash_dj = mapper.to_dataset_json(
        "CDISC_WF",
        form_whitelist=[101, 102],
        creation_datetime="2026-02-10T00:00:00Z",
    )
    assert validate_dataset_json(cdash_dj) == []
    assert "IG.DM" in cdash_dj.clinical_data.item_group_data

    sdtm_dj = mapper.sdtm_dataset_json(
        "CDISC_WF",
        domain="VS",
        creation_datetime="2026-02-10T00:00:00Z",
    )
    assert validate_dataset_json(sdtm_dj) == []
    assert "IG.VS" in sdtm_dj.clinical_data.item_group_data

    direct_dj = transform_records_to_dataset_json(
        records,
        study_key="CDISC_WF",
        forms=sdk.forms.list(),
        variables=sdk.variables.list(),
        phi_keys=["SYSBP"],
        creation_datetime="2026-02-10T00:00:00Z",
    )
    assert validate_dataset_json(direct_dj) == []

    data_wf = DataExtractionWorkflow(sdk)
    odm_audits = data_wf.extract_odm_audit_records("CDISC_WF")
    assert len(odm_audits) == 1
    assert odm_audits[0].user_oid == "USR.cra_user"
    assert odm_audits[0].location_oid == "LOC.1"
    assert "***MASKED***" in (odm_audits[0].reason_for_change or "")

    query_wf = QueryManagementWorkflow(sdk)
    all_annots = query_wf.extract_odm_annotations("CDISC_WF")
    assert len(all_annots) == 2
    assert "***MASKED***" in (all_annots[0].comment or "")

    open_annots = query_wf.extract_odm_annotations("CDISC_WF", open_only=True)
    assert len(open_annots) == 1
    assert any(fl.flag_value == "OPEN" for fl in open_annots[0].flags)


def test_workflow_audit_cli_odm_and_xml_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """workflows extract-audit-trail supports both explicit .xml paths and 'odm' target format."""
    monkeypatch.chdir(tmp_path)
    sdk, _ = _build_workflow_sdk()

    audit_xml_path = tmp_path / "custom_audit.xml"
    with patch("imednet.cli.utils.context.get_sdk", return_value=sdk):
        app(
            [
                "workflows",
                "extract-audit-trail",
                "--study-key",
                "CDISC_WF",
                "-o",
                str(audit_xml_path),
            ]
        )
        app(
            [
                "workflows",
                "extract-audit-trail",
                "--study-key",
                "CDISC_WF",
                "-o",
                "odm",
            ]
        )

    assert audit_xml_path.is_file()
    assert validate_odm_xml(audit_xml_path) == []
    audit_archive = load_odm_archive(audit_xml_path)
    assert len(audit_archive.clinical_data[0].audit_records) == 1
    assert "cra@sponsor.example" not in audit_xml_path.read_text(encoding="utf-8")

    default_odm = tmp_path / "audit_trail.xml"
    assert default_odm.is_file()
    assert validate_odm_xml(default_odm) == []

    # Invalid output format still exits with code 1
    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=sdk),
        pytest.raises(SystemExit) as exc_info,
    ):
        app(
            [
                "workflows",
                "extract-audit-trail",
                "--study-key",
                "CDISC_WF",
                "-o",
                "unsupported.txt",
            ]
        )
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_record_mapper_and_async_record_mapper_naming_and_sdtm_contracts() -> None:
    """RecordMapper and AsyncRecordMapper honor naming_convention, preserve_record_id, DM deduplication, and explicit as_sdtm=False."""
    from unittest.mock import AsyncMock

    from imednet_workflows.record_mapper import AsyncRecordMapper

    sdk, records = _build_workflow_sdk()
    extra_vars = [
        *sdk.variables.list(),
        Variable(
            variable_id=4,
            variable_name="form_key",
            label="Form Key Var",
            variable_type="text",
            form_id=101,
            form_key="DM",
            sequence=3,
        ),
    ]
    sdk.variables.list.return_value = extra_vars
    sdk.get_variables.return_value = extra_vars
    records.append(
        Record(
            record_id=3,
            study_key="CDISC_WF",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=101,
            form_key="DM",
            date_modified="2026-02-02T10:00:00Z",
            record_data={"SUBJID": "100", "SEX": "F", "form_key": "nested_collision"},
        )
    )
    sdk.get_intervals.return_value = []
    sdk.get_subjects.return_value = []
    sdk.get_visits.return_value = []
    sdk._async_sdk = None

    mapper = RecordMapper(sdk)
    camel_df = mapper.dataframe(
        "CDISC_WF",
        form_whitelist=[101],
        use_labels_as_columns=False,
        naming_convention="camelCase",
    )
    assert "recordId" in camel_df.columns
    assert "var_formKey" in camel_df.columns

    non_sdtm_filtered = mapper.dataframe(
        "CDISC_WF",
        as_sdtm=False,
        domain="DM",
        use_labels_as_columns=False,
        naming_convention="snake_case",
    )
    assert "record_id" in non_sdtm_filtered.columns
    assert "USUBJID" not in non_sdtm_filtered.columns

    dm_sdtm = mapper.sdtm_dataframe("CDISC_WF", domain="DM", preserve_record_id=True)
    assert len(dm_sdtm) == 1
    assert dm_sdtm["SEX"].iloc[0] == "F"
    assert int(dm_sdtm["RECORD_ID"].iloc[0]) == 3

    hier = mapper.build_hierarchy("CDISC_WF", naming_convention="camelCase")
    assert hier[0]["subjectKey"] == "SUBJ-100"

    async_sdk = MagicMock()
    async_sdk.get_forms = AsyncMock(return_value=sdk.forms.list())
    async_sdk.get_variables = AsyncMock(return_value=sdk.variables.list())
    async_sdk.get_records = AsyncMock(return_value=[r.model_copy(deep=True) for r in records])
    async_sdk.get_intervals = AsyncMock(return_value=[])
    async_sdk.get_subjects = AsyncMock(return_value=[])
    async_sdk.get_visits = AsyncMock(return_value=[])

    amapper = AsyncRecordMapper(async_sdk)
    adm_df = await amapper.sdtm_dataframe("CDISC_WF", domain="DM", preserve_record_id=True)
    assert len(adm_df) == 1
    assert int(adm_df["RECORD_ID"].iloc[0]) == 3

    ahier = await amapper.build_hierarchy("CDISC_WF", naming_convention="sdtm")
    assert ahier[0]["USUBJID"] == "SUBJ-100"

    adj = await amapper.sdtm_dataset_json("CDISC_WF", domain="DM", preserve_record_id=True)
    assert validate_dataset_json(adj) == []
