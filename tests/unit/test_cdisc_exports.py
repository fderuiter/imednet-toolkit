"""Core unit and integration tests for CDISC ODM 1.3.2 and Dataset-JSON v1.1 exports and CLI."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, NonCallableMock, patch

import pytest

from imednet.cli import app
from imednet.datadict import (
    DatasetJsonClinicalData,
    DatasetJsonEnvelope,
    DatasetJsonItem,
    DatasetJsonItemGroup,
    OdmItemData,
    OdmItemGroupData,
    OdmReferenceData,
    build_dataset_json_envelope,
    load_dataset_json,
    load_odm_archive,
    to_dataset_json_file,
    validate_dataset_json,
    validate_odm_xml,
)
from imednet.integrations import SinkConfig, export, export_to_dataset_json, export_to_odm
from imednet.integrations.cdisc_export import _safe_fetch_sdk_items
from imednet.integrations.sink_base import extract_record_timestamp, normalize_watermark_datetime
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.queries import Query, QueryComment, QueryStatus
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.users import Role, User
from imednet.models.variables import Variable
from imednet.models.visits import Visit


def _build_stateful_cdisc_sdk(
    upstream_records: dict[int, Record],
    *,
    study_key: str = "CDISC_STUDY",
) -> MagicMock:
    """Create a stateful read-only mock SDK supporting incremental watermarks and voided records."""
    study = Study(
        study_key=study_key,
        study_name="Unified CDISC Study",
        study_description="ODM and Dataset-JSON validation study",
    )
    sites = [Site(site_id=1, site_name="Site Alpha", site_enrollment_status="Active")]
    users = [
        User(
            user_id="u-1",
            login="cra_user",
            first_name="Clinical",
            last_name="Monitor",
            email="cra@sponsor.example",
            roles=[Role(role_id="CRA", name="Clinical Research Associate", sites=[1])],
        )
    ]
    forms = [
        Form(form_id=101, form_key="DM", form_name="Demographics", form_type="Subject"),
        Form(form_id=102, form_key="VS", form_name="Vital Signs", form_type="Visit"),
    ]
    intervals = [
        Interval(
            interval_id=201,
            interval_name="Visit 1",
            interval_sequence=1,
            forms=[FormSummary(form_id=102, form_key="VS", form_name="Vital Signs")],
        )
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
            variable_name="SEX",
            label="Sex",
            variable_type="text",
            form_id=101,
            form_key="DM",
            sequence=3,
        ),
        Variable(
            variable_id=4,
            variable_name="SYSBP",
            label="Systolic BP",
            variable_type="int",
            form_id=102,
            form_key="VS",
            sequence=1,
        ),
    ]
    subjects = [
        Subject(
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            site_name="Site Alpha",
            subject_status="Active",
        )
    ]
    visits = [
        Visit(
            visit_id=600,
            interval_id=201,
            interval_name="Visit 1",
            subject_id=500,
            subject_key="SUBJ-100",
        )
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
            annotation_type="AutoCheck",
            record_id=2,
            subject_key="SUBJ-100",
            variable="SYSBP",
            description="Verify systolic BP units",
            status=QueryStatus.ANSWERED,
            query_comments=[
                QueryComment(
                    sequence=1,
                    user="cra_user",
                    comment="Confirmed mmHg with nurse@site.example",
                    closed=False,
                    date="2026-02-01T11:00:00Z",
                )
            ],
        )
    ]

    def _iter_records(*_args: Any, **kw: Any) -> Any:
        flt = str(kw.get("filter", "") or "")
        items = list(upstream_records.values())
        if kw.get("deleted") is False:
            items = [r for r in items if not r.deleted]
        elif kw.get("deleted") is True:
            items = [r for r in items if r.deleted]

        if kw.get("dateModified") is not None:
            dm = kw["dateModified"]
            if isinstance(dm, tuple) and len(dm) == 2:
                op_str, raw_ts = dm
                wm_dt = normalize_watermark_datetime(raw_ts)
                if wm_dt is not None:
                    if op_str == ">=":
                        items = [
                            r
                            for r in items
                            if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                        ]
                    elif op_str == ">":
                        items = [
                            r
                            for r in items
                            if (ts := extract_record_timestamp(r)) is not None and ts > wm_dt
                        ]
        for clause in (c.strip() for c in flt.split(";") if c.strip()):
            if clause == "deleted==false":
                items = [r for r in items if not r.deleted]
            elif clause == "deleted==true":
                items = [r for r in items if r.deleted]
            elif clause.startswith("dateModified>="):
                wm_dt = normalize_watermark_datetime(clause.split(">=", 1)[1])
                if wm_dt is not None:
                    items = [
                        r
                        for r in items
                        if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                    ]
        return iter([r.model_copy(deep=True) for r in items])

    sdk = MagicMock()
    sdk.studies.get.return_value = study
    sdk.studies.list.return_value = [study]
    sdk.get_studies.return_value = [study]
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.intervals.list.return_value = intervals
    sdk.get_intervals.return_value = intervals
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.sites.list.return_value = sites
    sdk.get_sites.return_value = sites
    sdk.users.list.return_value = users
    sdk.get_users.return_value = users
    sdk.subjects.list.return_value = subjects
    sdk.get_subjects.return_value = subjects
    sdk.visits.list.return_value = visits
    sdk.get_visits.return_value = visits
    sdk.record_revisions.list.return_value = revisions
    sdk.get_record_revisions.return_value = revisions
    sdk.queries.list.return_value = queries
    sdk.get_queries.return_value = queries
    sdk.records.iter.side_effect = _iter_records
    sdk.records.list.side_effect = lambda *a, **k: list(_iter_records(*a, **k))
    sdk.get_records.side_effect = lambda *a, **k: list(_iter_records(*a, **k))
    return sdk


def test_export_registry_odm_and_dataset_json_deterministic_incremental_and_phi_masked(
    tmp_path: Path,
) -> None:
    """ExportRegistry resolves odm and dataset_json with deterministic outputs, PHI masking, and incremental --since checkpoints."""
    records: dict[int, Record] = {
        1: Record(
            record_id=1,
            study_key="CDISC_STUDY",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=101,
            form_key="DM",
            date_modified="2026-02-01T10:00:00Z",
            record_data={"SUBJID": "100", "patientName": "John Secret", "SEX": "M"},
        ),
        2: Record(
            record_id=2,
            study_key="CDISC_STUDY",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=102,
            form_key="VS",
            visit_id=600,
            interval_id=201,
            interval_name="Visit 1",
            date_modified="2026-02-01T10:05:00Z",
            record_data={"SYSBP": 118},
        ),
    }
    sdk = _build_stateful_cdisc_sdk(records)

    odm_file = tmp_path / "export_1.xml"
    odm_file_repeat = tmp_path / "export_2.xml"
    ledger_path = tmp_path / "odm_ledger.json"
    manifest_path = tmp_path / "manifest.jsonl"

    ref_data = [
        OdmReferenceData(
            study_oid="CDISC_STUDY",
            metadata_version_oid="MDV.001",
            item_group_data=[
                OdmItemGroupData(
                    item_group_oid="IG.REF",
                    item_group_repeat_key="1",
                    items=[OdmItemData(item_oid="IT.REF_VAL", value="100")],
                )
            ],
        )
    ]

    # Initial incremental export via ExportRegistry
    export(
        "odm",
        sdk,
        "CDISC_STUDY",
        path=str(odm_file),
        incremental=True,
        ledger_path=ledger_path,
        stream_name="odm_stream",
        file_oid="OID.DET.01",
        creation_datetime="2026-02-02T00:00:00Z",
        reference_data=ref_data,
        manifest_path=str(manifest_path),
    )
    export_to_odm(
        sdk,
        "CDISC_STUDY",
        str(odm_file_repeat),
        file_oid="OID.DET.01",
        creation_datetime="2026-02-02T00:00:00Z",
        reference_data=ref_data,
    )

    xml_text = odm_file.read_text(encoding="utf-8")
    assert xml_text == odm_file_repeat.read_text(encoding="utf-8")
    assert validate_odm_xml(xml_text) == []
    assert manifest_path.is_file()
    assert "John Secret" not in xml_text
    assert "cra@sponsor.example" not in xml_text
    assert "nurse@site.example" not in xml_text
    assert "***MASKED***" in xml_text

    # Repeat incremental run when no records changed preserves existing archive deterministically
    export(
        "odm",
        sdk,
        "CDISC_STUDY",
        path=str(odm_file),
        incremental=True,
        ledger_path=ledger_path,
        stream_name="odm_stream",
        file_oid="OID.DET.01",
        creation_datetime="2026-02-02T00:00:00Z",
    )
    assert odm_file.read_text(encoding="utf-8") == xml_text

    # Mutate upstream: update Record 2 (SYSBP 118 -> 126) and void Record 1 (deleted=True)
    records[2] = records[2].model_copy(
        update={
            "date_modified": "2026-02-03T12:00:00Z",
            "record_data": {"SYSBP": 126},
        }
    )
    records[1] = records[1].model_copy(
        update={
            "deleted": True,
            "date_modified": "2026-02-03T12:05:00Z",
        }
    )

    export(
        "odm",
        sdk,
        "CDISC_STUDY",
        path=str(odm_file),
        incremental=True,
        ledger_path=ledger_path,
        stream_name="odm_stream",
        file_oid="OID.DET.01",
        creation_datetime="2026-02-04T00:00:00Z",
    )
    assert validate_odm_xml(odm_file) == []
    updated_archive = load_odm_archive(odm_file)
    subj = updated_archive.clinical_data[0].subjects[0]
    # Voided DM record 1 was pruned; ReferenceData & updated VS record 2 preserved
    assert subj.forms == ()
    assert len(updated_archive.reference_data) == 1
    vs_igs = subj.study_events[0].forms[0].item_group_data
    assert len(vs_igs) == 1
    assert vs_igs[0].items[0].value == "126"

    # Dataset-JSON v1.1 directory export via ExportRegistry
    records[1] = records[1].model_copy(
        update={"deleted": False, "date_modified": "2026-02-05T08:00:00Z"}
    )
    dj_dir = tmp_path / "dataset_json_out"
    dj_ledger = tmp_path / "dj_ledger.json"
    export(
        "dataset-json",
        sdk,
        "CDISC_STUDY",
        path=str(dj_dir),
        incremental=True,
        ledger_path=dj_ledger,
        stream_name="dj_stream",
        creation_datetime="2026-02-05T09:00:00Z",
        manifest_path=str(manifest_path),
    )
    dm_path = dj_dir / "dm.json"
    vs_path = dj_dir / "vs.json"
    assert dm_path.is_file()
    assert vs_path.is_file()
    assert validate_dataset_json(dm_path) == []
    assert validate_dataset_json(vs_path) == []
    assert "John Secret" not in dm_path.read_text(encoding="utf-8")

    # Void record 1 and update record 2 in directory Dataset-JSON incremental export
    records[1] = records[1].model_copy(
        update={"deleted": True, "date_modified": "2026-02-06T08:00:00Z"}
    )
    records[2] = records[2].model_copy(
        update={"date_modified": "2026-02-06T08:10:00Z", "record_data": {"SYSBP": 131}}
    )
    export(
        "dataset_json",
        sdk,
        "CDISC_STUDY",
        path=str(dj_dir),
        incremental=True,
        ledger_path=dj_ledger,
        stream_name="dj_stream",
        creation_datetime="2026-02-06T09:00:00Z",
    )
    assert validate_dataset_json(vs_path) == []
    vs_env = load_dataset_json(vs_path)
    vs_group = vs_env.clinical_data.item_group_data["IG.VS"]
    assert vs_group.records == 1
    sysbp_idx = [it.name for it in vs_group.items].index("SYSBP")
    assert vs_group.item_data[0][sysbp_idx] == 131

    # Confirm zero write calls to upstream EDC endpoints
    sdk.records.create.assert_not_called()
    sdk.records.update.assert_not_called()
    sdk.queries.create.assert_not_called()


def test_single_file_dataset_json_sdtm_incremental_merge_and_sink_config_overrides(
    tmp_path: Path,
) -> None:
    """Single-file Dataset-JSON export supports SinkConfig overrides, SDTM composite-key pruning, and column widening."""
    records: dict[int, Record] = {
        10: Record(
            record_id=10,
            study_key="CDISC_STUDY",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=101,
            form_key="DM",
            date_modified="2026-02-01T10:00:00Z",
            record_data={"SUBJID": "100", "SEX": "M", "AGE": 45},
        ),
        20: Record(
            record_id=20,
            study_key="CDISC_STUDY",
            subject_id=501,
            subject_key="SUBJ-101",
            site_id=1,
            form_id=101,
            form_key="DM",
            date_modified="2026-02-01T10:05:00Z",
            record_data={"SUBJID": "101", "SEX": "F", "AGE": 52},
        ),
    }
    sdk = _build_stateful_cdisc_sdk(records)
    single_dj = tmp_path / "dm_sdtm.json"
    sdtm_ledger = tmp_path / "sdtm_ledger.json"

    base_cfg = SinkConfig(
        study_key="CDISC_STUDY",
        incremental=True,
        ledger_path=str(sdtm_ledger),
        stream_name="sdtm_dm_stream",
    )
    export_to_dataset_json(
        sdk,
        "CDISC_STUDY",
        single_dj,
        config=base_cfg,
        as_sdtm=True,
        sdtm_domain="DM",
        mask_phi=False,
        preserve_record_id=True,
        naming_convention="sdtm",
        custom_tag="v1",
    )
    assert validate_dataset_json(single_dj) == []
    env1 = load_dataset_json(single_dj)
    assert env1.clinical_data.item_group_data["IG.DM"].records == 2

    # Void SUBJ-100 DM record and update SUBJ-101 DM record with a new column + widened type
    records[10] = records[10].model_copy(
        update={"deleted": True, "date_modified": "2026-02-02T10:00:00Z"}
    )
    records[20] = records[20].model_copy(
        update={
            "date_modified": "2026-02-02T10:05:00Z",
            "record_data": {"SUBJID": "101", "SEX": "F", "AGE": "UNKNOWN", "RACE": "ASIAN"},
        }
    )
    export_to_dataset_json(
        sdk,
        "CDISC_STUDY",
        single_dj,
        config=base_cfg,
        as_sdtm=True,
        sdtm_domain="DM",
        mask_phi=False,
        preserve_record_id=True,
    )
    assert validate_dataset_json(single_dj) == []
    env2 = load_dataset_json(single_dj)
    dm_grp = env2.clinical_data.item_group_data["IG.DM"]
    assert dm_grp.records == 1
    col_names = [it.name for it in dm_grp.items]
    assert dm_grp.item_data[0][col_names.index("USUBJID")].endswith("SUBJ-101")


def test_whitelists_default_paths_and_safe_sdk_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ODM and Dataset-JSON exports respect form/variable whitelists, default filenames, and unmocked/non-mock SDK fallbacks."""
    monkeypatch.chdir(tmp_path)
    records = {
        1: Record(
            record_id=1,
            study_key="CDISC_STUDY",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=101,
            form_key="DM",
            date_modified="2026-02-01T10:00:00Z",
            record_data={"SUBJID": "100", "patientName": "John", "SEX": "M"},
        ),
        2: Record(
            record_id=2,
            study_key="CDISC_STUDY",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=102,
            form_key="VS",
            visit_id=600,
            date_modified="2026-02-01T10:05:00Z",
            record_data={"SYSBP": 120, "DIABP": 80},
        ),
    }
    sdk = _build_stateful_cdisc_sdk(records)

    # Export with path=None (uses deterministic default path in cwd) + whitelists + no audit/annotations
    export_to_odm(
        sdk,
        "CDISC_STUDY",
        None,
        form_whitelist=[102],
        variable_whitelist=["SYSBP"],
        include_audit_trail=False,
        include_annotations=False,
    )
    default_odm = tmp_path / "cdisc_study.odm.xml"
    assert default_odm.is_file()
    assert validate_odm_xml(default_odm) == []
    odm_loaded = load_odm_archive(default_odm)
    assert {f.formKey for f in odm_loaded.data_dictionary.forms} == {"VS"}

    export_to_dataset_json(
        sdk,
        "CDISC_STUDY",
        None,
        form_whitelist=[101],
        variable_whitelist=["SUBJID", "SEX"],
        use_labels_as_columns=False,
        naming_convention="snake_case",
    )
    default_dj = tmp_path / "cdisc_study.dataset.json"
    assert default_dj.is_file()
    assert validate_dataset_json(default_dj) == []

    # Verify _safe_fetch_sdk_items graceful fallbacks on bare objects, failing endpoints, and NonCallableMock
    bare_mock = NonCallableMock()
    assert _safe_fetch_sdk_items(bare_mock, "S1", "forms", "get_forms") == []

    class _FacadeOnlySDK:
        def get_forms(self, study_key: str) -> list[dict[str, Any]]:
            return [{"formKey": f"F_{study_key}", "formId": 1}]

        def get_sites(self, study_key: str) -> list[Any]:
            raise RuntimeError("endpoint unavailable")

    facade_sdk = _FacadeOnlySDK()
    assert len(_safe_fetch_sdk_items(facade_sdk, "S1", "forms", "get_forms")) == 1
    assert _safe_fetch_sdk_items(facade_sdk, "S1", "sites", "get_sites") == []
    assert _safe_fetch_sdk_items(object(), "S1", "forms", "get_forms") == []

    # Scalar & collection coercions in Dataset-JSON builder
    coercion_env = build_dataset_json_envelope(
        "COERCE_STUDY",
        records=[
            {
                "recordId": 1,
                "subjectKey": "S-1",
                "formKey": "LAB",
                "dateModified": datetime(2026, 4, 1, 8, 30, tzinfo=UTC),
                "recordData": {
                    "FLAG_BOOL": True,
                    "DEC_VAL": Decimal("12.5"),
                    "NAN_VAL": float("nan"),
                    "COLLECT_DT": date(2026, 4, 1),
                    "STAMP_DT": datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
                    "META_OBJ": {"unit": "mg/dL"},
                },
            }
        ],
        mask_phi=False,
    )
    assert validate_dataset_json(coercion_env) == []
    tmp_dsj = tmp_path / "coerce.json"
    to_dataset_json_file(coercion_env, tmp_dsj)
    assert load_dataset_json(json.loads(tmp_dsj.read_text(encoding="utf-8"))) == coercion_env

    # Corrupt prior file fallback during incremental merge logs warning and writes fresh valid file
    corrupt_dj = tmp_path / "corrupt.dataset.json"
    corrupt_dj.write_text("{not valid json", encoding="utf-8")
    export_to_dataset_json(
        sdk,
        "CDISC_STUDY",
        corrupt_dj,
        incremental=True,
        ledger_path=tmp_path / "corrupt_ledger.json",
    )
    assert validate_dataset_json(corrupt_dj) == []

    # Manual DatasetJsonItemGroup validation via dict
    manual_env = DatasetJsonEnvelope(
        creationDateTime="2026-01-01T00:00:00Z",
        clinicalData=DatasetJsonClinicalData(
            studyOID="S1",
            metaDataVersionOID="MDV.1",
            itemGroupData={
                "IG.TEST": DatasetJsonItemGroup(
                    records=1,
                    name="TEST",
                    label="Test",
                    items=(
                        DatasetJsonItem(
                            OID="ITEMGROUPDATASEQ",
                            name="ITEMGROUPDATASEQ",
                            label="Seq",
                            type="integer",
                        ),
                    ),
                    itemData=((1,),),
                )
            },
        ),
    )
    assert validate_dataset_json(manual_env.to_json_dict()) == []


def test_cli_export_odm_and_dataset_json_and_datadict_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI commands imednet export odm, imednet export dataset-json, and datadict --archive produce valid CDISC outputs."""
    records = {
        1: Record(
            record_id=1,
            study_key="CDISC_STUDY",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=101,
            form_key="DM",
            date_modified="2026-02-01T10:00:00Z",
            record_data={"SUBJID": "100", "patientName": "Jane Patient", "SEX": "F"},
        ),
        2: Record(
            record_id=2,
            study_key="CDISC_STUDY",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=102,
            form_key="VS",
            visit_id=600,
            interval_id=201,
            interval_name="Visit 1",
            date_modified="2026-02-01T10:05:00Z",
            record_data={"SYSBP": 120},
        ),
    }
    sdk = _build_stateful_cdisc_sdk(records)

    odm_cli_out = tmp_path / "cli_study.xml"
    dj_cli_out = tmp_path / "cli_dj_sdtm.json"
    ledger_cli = tmp_path / "cli_state.json"

    with patch("imednet.cli.utils.context.get_sdk", return_value=sdk):
        app(
            [
                "export",
                "odm",
                "CDISC_STUDY",
                str(odm_cli_out),
                "--since",
                "2026-01-01T00:00:00Z",
                "--incremental",
                "--ledger-path",
                str(ledger_cli),
                "--file-oid",
                "OID.CLI.ODM",
                "--creation-datetime",
                "2026-02-10T00:00:00Z",
            ]
        )
        app(
            [
                "export",
                "dataset-json",
                "CDISC_STUDY",
                str(dj_cli_out),
                "--as-sdtm",
                "--sdtm-domain",
                "VS",
                "--since",
                "2026-01-01T00:00:00Z",
                "--file-oid",
                "OID.CLI.DJ",
                "--creation-datetime",
                "2026-02-10T00:00:00Z",
            ]
        )

    assert odm_cli_out.is_file()
    assert validate_odm_xml(odm_cli_out) == []
    assert "Jane Patient" not in odm_cli_out.read_text(encoding="utf-8")

    assert dj_cli_out.is_file()
    assert validate_dataset_json(dj_cli_out) == []
    sdtm_vs = load_dataset_json(dj_cli_out)
    assert "IG.VS" in sdtm_vs.clinical_data.item_group_data

    # datadict from-odm --strict fails on ClinicalData
    with pytest.raises(SystemExit) as exc_info:
        app(["datadict", "from-odm", str(odm_cli_out), "--strict"])
    assert exc_info.value.code == 1
    assert "ClinicalData" in capsys.readouterr().err

    # datadict from-odm --archive + datadict to-odm round-trip
    archive_json_path = tmp_path / "archive.json"
    roundtrip_xml_path = tmp_path / "roundtrip.xml"
    app(["datadict", "from-odm", str(odm_cli_out), "--archive", "-o", str(archive_json_path)])
    assert archive_json_path.is_file()
    app(["datadict", "to-odm", str(archive_json_path), "-o", str(roundtrip_xml_path)])
    assert validate_odm_xml(roundtrip_xml_path) == []
    assert load_odm_archive(roundtrip_xml_path) == load_odm_archive(odm_cli_out)


def test_cdisc_sink_config_manifests_corrupt_recovery_and_schema_widening(tmp_path: Path) -> None:
    """Verify SinkConfig overrides, JSONL checksum manifests, corrupt archive recovery, and Dataset-JSON column widening."""
    from imednet.integrations.cdisc_export import (
        _build_cdisc_sink_config,
        _DatasetJsonSink,
        _merge_dataset_json_item_groups,
        _OdmArchiveSink,
    )

    base_cfg = SinkConfig(study_key="S1", mask_phi=True)
    overridden = _build_cdisc_sink_config(
        "S1",
        config=base_cfg,
        mask_phi=False,
        as_sdtm=True,
        sdtm_domain="AE",
        naming_convention="snake_case",
        preserve_record_id=True,
        kwargs={"batch_size": 25, "custom_tag": "v1"},
    )
    assert overridden.mask_phi is False
    assert overridden.as_sdtm is True
    assert overridden.sdtm_domain == "AE"
    assert overridden.naming_convention == "snake_case"
    assert overridden.preserve_record_id is True
    assert overridden.batch_size == 25
    assert overridden.extra["custom_tag"] == "v1"

    fresh_cfg = _build_cdisc_sink_config(
        "S1",
        config=None,
        mask_phi=True,
        as_sdtm=True,
        sdtm_domain="DM",
        naming_convention="camelCase",
        preserve_record_id=False,
        kwargs={"extra": {"seed": 1}, "another_opt": 2},
    )
    assert fresh_cfg.extra == {"seed": 1, "another_opt": 2}

    records = {
        1: Record(
            record_id=1,
            study_key="CDISC_STUDY",
            subject_id=500,
            subject_key="SUBJ-100",
            site_id=1,
            form_id=102,
            form_key="VS",
            visit_id=600,
            date_modified="2026-02-01T10:05:00Z",
            record_data={"SYSBP": 120, "DIABP": 80},
        )
    }
    sdk = _build_stateful_cdisc_sdk(records)

    # Corrupt ODM file recovery + manifest emission + dict/unkeyed batch staging + idempotent flush
    corrupt_odm = tmp_path / "corrupt.odm.xml"
    corrupt_odm.write_text("<<<corrupt xml", encoding="utf-8")
    manifest_jsonl = tmp_path / "manifests" / "cdisc.jsonl"

    inc_cfg = SinkConfig(study_key="CDISC_STUDY", incremental=True, idempotent=True)
    odm_sink = _OdmArchiveSink(
        sdk,
        "CDISC_STUDY",
        corrupt_odm,
        inc_cfg,
        variable_whitelist=["SYSBP"],
        manifest_path=str(manifest_jsonl),
    )
    assert (
        odm_sink.write_batch(
            [
                {
                    "recordId": 1,
                    "subjectKey": "SUBJ-100",
                    "formKey": "VS",
                    "recordData": {"SYSBP": 120, "DIABP": 80},
                },
                {
                    "subjectKey": "SUBJ-100",
                    "formKey": "VS",
                    "record_data": {"SYSBP": 118, "DIABP": 76},
                },
            ],
            batch_id="b1",
        )
        == 2
    )
    odm_sink.flush()
    odm_sink.flush()  # Second flush is a safe no-op
    odm_sink.close()
    assert validate_odm_xml(corrupt_odm) == []
    assert manifest_jsonl.is_file()

    # Empty incremental flush on existing ODM and Dataset-JSON files returns cleanly without rewriting
    noop_odm_sink = _OdmArchiveSink(sdk, "CDISC_STUDY", corrupt_odm, inc_cfg)
    noop_odm_sink.flush()

    dj_dir = tmp_path / "dj_domains"
    dj_dir.mkdir()
    (dj_dir / "broken.json").write_text("{not json", encoding="utf-8")
    dj_sink = _DatasetJsonSink(
        sdk,
        "CDISC_STUDY",
        dj_dir,
        inc_cfg,
        manifest_path=str(manifest_jsonl),
    )
    dj_sink.write_batch(
        [{"subjectKey": "SUBJ-100", "formKey": "VS", "recordData": {"SYSBP": 121}}],
        batch_id="dj-b1",
    )
    dj_sink.flush()
    dj_sink.flush()
    dj_sink.close()
    assert validate_dataset_json(dj_dir / "vs.json") == []

    noop_dj_sink = _DatasetJsonSink(sdk, "CDISC_STUDY", dj_dir / "vs.json", inc_cfg)
    noop_dj_sink.flush()

    # Verify _merge_dataset_json_item_groups widens mismatched column types to string and appends new columns
    prior_grp = DatasetJsonItemGroup(
        records=1,
        name="VS",
        label="Vital Signs",
        items=(
            DatasetJsonItem(
                OID="ITEMGROUPDATASEQ", name="ITEMGROUPDATASEQ", label="Seq", type="integer"
            ),
            DatasetJsonItem(
                OID="IT.VS.recordId", name="recordId", label="ID", type="integer", keySequence=1
            ),
            DatasetJsonItem(OID="IT.VS.VAL", name="VAL", label="Val", type="integer"),
        ),
        itemData=((1, 10, 120),),
    )
    delta_grp = DatasetJsonItemGroup(
        records=1,
        name="VS",
        label="Vital Signs",
        items=(
            DatasetJsonItem(
                OID="ITEMGROUPDATASEQ", name="ITEMGROUPDATASEQ", label="Seq", type="integer"
            ),
            DatasetJsonItem(
                OID="IT.VS.recordId", name="recordId", label="ID", type="integer", keySequence=1
            ),
            DatasetJsonItem(OID="IT.VS.VAL", name="VAL", label="Val", type="string"),
            DatasetJsonItem(OID="IT.VS.NEW_COL", name="NEW_COL", label="New", type="string"),
        ),
        itemData=((1, 11, "125+", "added"),),
    )
    merged_grp = _merge_dataset_json_item_groups(
        prior_grp,
        delta_grp,
        voided_record_ids=set(),
        voided_sdtm_keys=set(),
    )
    merged_types = {it.name: it.type for it in merged_grp.items}
    assert merged_types["VAL"] == "string"
    assert "NEW_COL" in merged_types
    assert merged_grp.records == 2
