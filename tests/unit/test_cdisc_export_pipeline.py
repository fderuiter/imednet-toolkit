"""Integration tests for CDISC ODM 1.3.2 and Dataset-JSON v1.1 export pipelines, CLI, workflows, and Airflow."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.utils.airflow_mocks import setup_dummy_airflow

from imednet.cli import app as cli_app
from imednet.datadict import (
    dataset_json_to_rows,
    load_dataset_json,
    load_odm_archive,
    validate_dataset_json,
    validate_odm_xml,
)
from imednet.integrations import (
    export as unified_export,
)
from imednet.integrations import (
    export_to_dataset_json,
    export_to_odm,
)
from imednet.integrations.dispatcher import _registry as export_registry
from imednet.models.forms import Form as SdkForm
from imednet.models.intervals import Interval
from imednet.models.queries import Query, QueryComment
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.variables import Variable as SdkVariable
from imednet.models.visits import Visit
from imednet_workflows import (
    DataExtractionWorkflow,
    ExtractionStateLedger,
    QueryManagementWorkflow,
    RecordMapper,
)


def _build_mock_sdk(
    records: list[Record] | None = None,
    revisions: list[RecordRevision] | None = None,
    queries: list[Query] | None = None,
) -> MagicMock:
    sdk = MagicMock()
    sdk.studies.get.return_value = Study(
        study_key="TRIAL_1",
        study_name="Trial 1",
        study_description="Phase III Trial",
    )
    sdk.forms.list.return_value = [
        SdkForm(
            study_key="TRIAL_1",
            form_id=10,
            form_key="AE",
            form_name="Adverse Events",
            form_type="CRF",
        ),
        SdkForm(
            study_key="TRIAL_1",
            form_id=20,
            form_key="DM",
            form_name="Demographics",
            form_type="CRF",
            subject_record_report=True,
        ),
    ]
    sdk.variables.list.return_value = [
        SdkVariable(
            study_key="TRIAL_1",
            variable_id=101,
            variable_name="AETERM",
            label="Reported Term for the Adverse Event",
            variable_type="text",
            form_id=10,
            form_key="AE",
            form_name="Adverse Events",
            sequence=1,
        ),
        SdkVariable(
            study_key="TRIAL_1",
            variable_id=102,
            variable_name="AESEV",
            label="Severity",
            variable_type="text",
            form_id=10,
            form_key="AE",
            form_name="Adverse Events",
            sequence=2,
        ),
        SdkVariable(
            study_key="TRIAL_1",
            variable_id=201,
            variable_name="SEX",
            label="Sex",
            variable_type="text",
            form_id=20,
            form_key="DM",
            form_name="Demographics",
            sequence=1,
        ),
        SdkVariable(
            study_key="TRIAL_1",
            variable_id=202,
            variable_name="PATIENT_NAME",
            label="Patient Name",
            variable_type="text",
            form_id=20,
            form_key="DM",
            form_name="Demographics",
            sequence=2,
        ),
    ]
    sdk.intervals.list.return_value = [
        Interval(
            study_key="TRIAL_1",
            interval_id=1,
            interval_name="Baseline",
            interval_sequence=1,
        )
    ]
    sdk.sites.list.return_value = [
        Site(
            study_key="TRIAL_1",
            site_id=50,
            site_name="Boston General",
            site_enrollment_status="Active",
        )
    ]
    sdk.users.list.return_value = [
        User(
            user_id="u-10",
            login="drsmith",
            first_name="John",
            last_name="Smith",
            email="jsmith@hospital.org",
        )
    ]
    sdk.subjects.list.return_value = [
        Subject(
            study_key="TRIAL_1",
            subject_id=1,
            subject_key="SUBJ-01",
            site_id=50,
            site_name="Boston General",
            subject_status="Enrolled",
        )
    ]
    sdk.visits.list.return_value = [
        Visit(
            study_key="TRIAL_1",
            visit_id=700,
            interval_id=1,
            interval_name="Baseline",
            subject_id=1,
            subject_key="SUBJ-01",
        )
    ]

    default_records = (
        records
        if records is not None
        else [
            Record(
                study_key="TRIAL_1",
                record_id=1,
                form_id=20,
                form_key="DM",
                subject_id=1,
                subject_key="SUBJ-01",
                site_id=50,
                visit_id=700,
                interval_name="Baseline",
                date_created=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
                date_modified=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
                record_data={"SEX": "M", "PATIENT_NAME": "John Patient"},
            ),
            Record(
                study_key="TRIAL_1",
                record_id=2,
                form_id=10,
                form_key="AE",
                subject_id=1,
                subject_key="SUBJ-01",
                site_id=50,
                visit_id=700,
                interval_name="Baseline",
                date_created=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
                date_modified=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
                record_data={"AETERM": "Headache", "AESEV": "MILD"},
            ),
        ]
    )
    sdk.records.list.side_effect = lambda **_kw: list(default_records)

    sdk.record_revisions.list.return_value = (
        revisions
        if revisions is not None
        else [
            RecordRevision(
                study_key="TRIAL_1",
                record_revision_id=301,
                record_id=2,
                subject_key="SUBJ-01",
                site_id=50,
                form_key="AE",
                interval_name="Baseline",
                user="drsmith",
                reason_for_change="Initial entry verified",
                date_created=datetime(2026, 3, 2, 12, 5, tzinfo=UTC),
            )
        ]
    )
    sdk.queries.list.return_value = (
        queries
        if queries is not None
        else [
            Query(
                study_key="TRIAL_1",
                query_id=401,
                record_id=2,
                subject_key="SUBJ-01",
                form_key="AE",
                variable="AETERM",
                description="Clarify headache subtype",
                type="Manual",
                date_created=datetime(2026, 3, 2, 12, 10, tzinfo=UTC),
                date_modified=datetime(2026, 3, 2, 12, 20, tzinfo=UTC),
                query_comments=[
                    QueryComment(
                        sequence=1,
                        annotation_status="Open",
                        user="drsmith",
                        comment="Tension headache confirmed.",
                        closed=False,
                        date="2026-03-02T12:20:00Z",
                    )
                ],
            )
        ]
    )
    return sdk


def test_export_registry_resolves_odm_and_dataset_json(tmp_path: Path) -> None:
    """Verify ExportRegistry resolves odm and dataset_json targets with PHI masking enabled by default."""
    assert export_registry.get_tabular("odm") is not None
    assert export_registry.get_tabular("dataset_json") is not None

    sdk = _build_mock_sdk()
    odm_out = tmp_path / "registry.odm.xml"
    dsj_out = tmp_path / "registry.dataset.json"

    unified_export("odm", sdk, "TRIAL_1", path=odm_out, validate_schema=True)
    unified_export("dataset_json", sdk, "TRIAL_1", path=dsj_out, validate_schema=True)

    validate_odm_xml(odm_out)
    dsj_payload = json.loads(dsj_out.read_text(encoding="utf-8"))
    validate_dataset_json(dsj_payload)

    # Verify default PHI masking
    assert "***MASKED***" in odm_out.read_text(encoding="utf-8")
    assert "John Patient" not in odm_out.read_text(encoding="utf-8")
    assert "***MASKED***" in dsj_out.read_text(encoding="utf-8")
    assert "John Patient" not in dsj_out.read_text(encoding="utf-8")


def test_odm_incremental_ledger_checkpoint_upsert_and_voided_reconciliation(
    tmp_path: Path,
) -> None:
    """Verify export_to_odm advances ledger watermarks, upserts updated records, and prunes voided records."""
    ledger_file = tmp_path / "odm_ledger.json"
    odm_out = tmp_path / "incremental.odm.xml"

    # Run 1: Initial two records
    sdk_run1 = _build_mock_sdk()
    export_to_odm(
        sdk_run1,
        "TRIAL_1",
        odm_out,
        incremental=True,
        ledger_path=ledger_file,
        stream_name="odm_sync",
        validate_schema=True,
    )
    validate_odm_xml(odm_out)
    ledger = ExtractionStateLedger(str(ledger_file))
    ts1 = ledger.get_last_timestamp("TRIAL_1", "odm_sync")
    assert ts1 is not None
    assert "2026-03-02" in ts1.isoformat()

    # Run 2: Record 2 updated to SEVERE, Record 1 voided (deleted=True)
    delta_records = [
        Record(
            study_key="TRIAL_1",
            record_id=1,
            form_id=20,
            form_key="DM",
            subject_id=1,
            subject_key="SUBJ-01",
            site_id=50,
            visit_id=700,
            interval_name="Baseline",
            deleted=True,
            date_created=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
            date_modified=datetime(2026, 3, 5, 8, 0, tzinfo=UTC),
            record_data={"SEX": "M", "PATIENT_NAME": "John Patient"},
        ),
        Record(
            study_key="TRIAL_1",
            record_id=2,
            form_id=10,
            form_key="AE",
            subject_id=1,
            subject_key="SUBJ-01",
            site_id=50,
            visit_id=700,
            interval_name="Baseline",
            deleted=False,
            date_created=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
            date_modified=datetime(2026, 3, 5, 9, 0, tzinfo=UTC),
            record_data={"AETERM": "Headache", "AESEV": "SEVERE"},
        ),
    ]
    sdk_run2 = _build_mock_sdk(records=delta_records)
    export_to_odm(
        sdk_run2,
        "TRIAL_1",
        odm_out,
        incremental=True,
        ledger_path=ledger_file,
        stream_name="odm_sync",
        validate_schema=True,
    )
    validate_odm_xml(odm_out)
    merged_archive = load_odm_archive(odm_out, strict=True)
    repeat_keys = [
        ig.item_group_repeat_key
        for subj in merged_archive.clinical_data[0].subjects
        for ev in subj.study_event_data
        for fd in ev.form_data
        for ig in fd.item_group_data
    ]
    assert "1" not in repeat_keys  # Voided record 1 pruned
    assert "2" in repeat_keys
    values = [
        it.value
        for subj in merged_archive.clinical_data[0].subjects
        for ev in subj.study_event_data
        for fd in ev.form_data
        for ig in fd.item_group_data
        for it in ig.items
    ]
    assert "SEVERE" in values
    assert "MILD" not in values

    ts2 = ExtractionStateLedger(str(ledger_file)).get_last_timestamp("TRIAL_1", "odm_sync")
    assert ts2 is not None
    assert "2026-03-05T09:00:00" in ts2.isoformat()


def test_dataset_json_incremental_sdtm_and_cdash_per_form_exports(tmp_path: Path) -> None:
    """Verify Dataset-JSON v1.1 CDASH per-form and SDTM incremental exports with ledger checkpoints."""
    sdk = _build_mock_sdk()

    # 1. Per-form CDASH directory export
    cdash_dir = tmp_path / "cdash_datasets"
    export_to_dataset_json(
        sdk,
        "TRIAL_1",
        cdash_dir,
        per_form=True,
        validate_schema=True,
    )
    ae_file = cdash_dir / "AE.json"
    dm_file = cdash_dir / "DM.json"
    assert ae_file.exists()
    assert dm_file.exists()
    validate_dataset_json(json.loads(ae_file.read_text(encoding="utf-8")))
    validate_dataset_json(json.loads(dm_file.read_text(encoding="utf-8")))

    # 2. Incremental SDTM AE export with stateful AESEQ counter across checkpoints
    sdtm_file = tmp_path / "ae_sdtm.json"
    ledger_path = tmp_path / "dsj_ledger.json"
    export_to_dataset_json(
        sdk,
        "TRIAL_1",
        sdtm_file,
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        ledger_path=ledger_path,
        stream_name="ae_dataset_json",
        validate_schema=True,
    )
    ds1 = load_dataset_json(sdtm_file)
    rows1 = dataset_json_to_rows(ds1)
    assert len(rows1) == 1
    assert rows1[0]["AESEQ"] == 1
    assert rows1[0]["AETERM"] == "Headache"

    # Incremental batch 2 adds a second AE record for SUBJ-01 -> gets AESEQ=2
    sdk_batch2 = _build_mock_sdk(
        records=[
            Record(
                study_key="TRIAL_1",
                record_id=3,
                form_id=10,
                form_key="AE",
                subject_id=1,
                subject_key="SUBJ-01",
                site_id=50,
                visit_id=700,
                interval_name="Baseline",
                date_created=datetime(2026, 3, 6, 14, 0, tzinfo=UTC),
                date_modified=datetime(2026, 3, 6, 14, 0, tzinfo=UTC),
                record_data={"AETERM": "Dizziness", "AESEV": "MODERATE"},
            )
        ]
    )
    export_to_dataset_json(
        sdk_batch2,
        "TRIAL_1",
        sdtm_file,
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        ledger_path=ledger_path,
        stream_name="ae_dataset_json",
        validate_schema=True,
    )
    validate_dataset_json(json.loads(sdtm_file.read_text(encoding="utf-8")))
    ds2 = load_dataset_json(sdtm_file)
    rows2 = dataset_json_to_rows(ds2)
    assert len(rows2) == 2
    assert [r["AESEQ"] for r in rows2] == [1, 2]
    assert {r["AETERM"] for r in rows2} == {"Headache", "Dizziness"}


def test_workflows_audit_annotations_and_record_mapper_dataset_json() -> None:
    """Verify DataExtractionWorkflow, QueryManagementWorkflow, and RecordMapper CDISC exchange methods."""
    sdk = _build_mock_sdk()

    extraction_wf = DataExtractionWorkflow(sdk)
    audits = extraction_wf.extract_odm_audit_records("TRIAL_1", record_ids=[2], mask_phi=True)
    assert len(audits) == 1
    assert audits[0].id == "AR.301"
    assert audits[0].location_oid == "LOC.50"
    assert audits[0].reason_for_change == "Initial entry verified"

    query_wf = QueryManagementWorkflow(sdk)
    annotations = query_wf.extract_odm_annotations(
        "TRIAL_1", record_ids=[2], state_filter="Open", mask_phi=True
    )
    assert len(annotations) == 1
    assert annotations[0].comment is not None
    assert "Tension headache confirmed." in annotations[0].comment.value

    mapper = RecordMapper(sdk)
    mapper.sdk.records.iter_records.return_value = sdk.records.list()
    ae_ds = mapper.dataset_json("TRIAL_1", as_sdtm=True, sdtm_domain="AE")
    validate_dataset_json(ae_ds.to_json_dict())
    assert ae_ds.name == "AE"

    bundle = mapper.dataset_json_clinical_data("TRIAL_1", as_sdtm=False)
    assert len(bundle.item_group_data) >= 1
    for ds in bundle.item_group_data:
        validate_dataset_json(ds.to_json_dict())


def test_cli_export_odm_and_dataset_json_deterministic_and_schema_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running `imednet export odm` and `imednet export dataset-json` writes deterministic schema-valid outputs."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test-key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-secret")

    odm_a = tmp_path / "run_a.odm.xml"
    odm_b = tmp_path / "run_b.odm.xml"
    dsj_a = tmp_path / "run_a.dataset.json"
    dsj_b = tmp_path / "run_b.dataset.json"

    with patch("imednet.cli.utils.context.get_sdk", side_effect=lambda **_kw: _build_mock_sdk()):
        cli_app(["export", "odm", "TRIAL_1", str(odm_a), "--validate-schema", "--no-interactive"])
        cli_app(["export", "odm", "TRIAL_1", str(odm_b), "--validate-schema", "--no-interactive"])
        cli_app(
            [
                "export",
                "dataset-json",
                "TRIAL_1",
                str(dsj_a),
                "--sdtm-domain",
                "AE",
                "--validate-schema",
                "--no-interactive",
            ]
        )
        cli_app(
            [
                "export",
                "dataset-json",
                "TRIAL_1",
                str(dsj_b),
                "--sdtm-domain",
                "AE",
                "--validate-schema",
                "--no-interactive",
            ]
        )

    odm_bytes_a = odm_a.read_bytes()
    odm_bytes_b = odm_b.read_bytes()
    assert odm_bytes_a == odm_bytes_b
    validate_odm_xml(odm_a)

    dsj_bytes_a = dsj_a.read_bytes()
    dsj_bytes_b = dsj_b.read_bytes()
    assert dsj_bytes_a == dsj_bytes_b
    validate_dataset_json(json.loads(dsj_a.read_text(encoding="utf-8")))


def test_cli_datadict_from_odm_strict_exits_nonzero_on_clinical_data(
    tmp_path: Path,
) -> None:
    """Running `imednet datadict from-odm --strict` exits non-zero when non-metadata containers exist."""
    odm_out = tmp_path / "full_snapshot.odm.xml"
    export_to_odm(_build_mock_sdk(), "TRIAL_1", odm_out, validate_schema=True)
    json_out = tmp_path / "datadict.json"

    with pytest.raises(SystemExit) as exc_info:
        cli_app(["datadict", "from-odm", str(odm_out), "-o", str(json_out), "--strict"])
    assert exc_info.value.code == 1
    assert not json_out.exists()


def test_airflow_operator_exports_odm_and_dataset_json_with_ledger_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ImednetExportOperator executes odm and dataset_json exports with ledger checkpoints."""
    setup_dummy_airflow(monkeypatch)
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    odm_path = tmp_path / "airflow_study.odm.xml"
    dsj_path = tmp_path / "airflow_ae.dataset.json"
    ledger_path = tmp_path / "airflow_ledger.json"

    sdk = _build_mock_sdk()
    with patch.object(ImednetExportOperator, "_get_sdk", return_value=sdk):
        op_odm = ImednetExportOperator(
            task_id="export_odm_task",
            study_key="TRIAL_1",
            destination="odm",
            output_path=str(odm_path),
            incremental=True,
            ledger_path=str(ledger_path),
            stream_name="af_odm",
            export_kwargs={"validate_schema": True},
        )
        ret_odm = op_odm.execute(context={})
        assert ret_odm == str(odm_path)
        validate_odm_xml(odm_path)

        op_dsj = ImednetExportOperator(
            task_id="export_dsj_task",
            study_key="TRIAL_1",
            destination="dataset-json",
            output_path=str(dsj_path),
            sdtm_domain="AE",
            incremental=True,
            ledger_path=str(ledger_path),
            stream_name="af_dsj",
            export_kwargs={"validate_schema": True},
        )
        ret_dsj = op_dsj.execute(context={})
        assert ret_dsj == str(dsj_path)
        validate_dataset_json(json.loads(dsj_path.read_text(encoding="utf-8")))

    ledger = ExtractionStateLedger(str(ledger_path))
    assert ledger.get_last_timestamp("TRIAL_1", "af_odm") is not None
    assert ledger.get_last_timestamp("TRIAL_1", "af_dsj") is not None


def test_cdisc_sinks_cdash_and_sdtm_per_form_voiding_and_whitelists(tmp_path: Path) -> None:
    """Verify CDASH/SDTM incremental merges, voided record pruning, SinkConfig passthrough, and whitelists."""
    from imednet.integrations import SinkConfig

    # 1. ODM export with explicit SinkConfig, form/variable whitelists, and unmapped typed fields
    extra_records = [
        Record(
            study_key="TRIAL_1",
            record_id=10,
            form_id=10,
            form_key="AE",
            subject_id=1,
            subject_key="SUBJ-01",
            site_id=50,
            visit_id=700,
            interval_name="Baseline",
            date_created=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
            date_modified=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
            record_data={
                "AETERM": "Fever",
                "AESEV": "MODERATE",
                "IS_SERIOUS": False,
                "DAYS_ACTIVE": 3,
                "PEAK_TEMP": 38.9,
            },
        ),
        Record(
            study_key="TRIAL_1",
            record_id=11,
            form_id=20,
            form_key="DM",
            subject_id=1,
            subject_key="SUBJ-01",
            site_id=50,
            visit_id=700,
            date_created=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
            date_modified=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
            record_data={"SEX": "F"},
        ),
    ]
    odm_filtered = tmp_path / "filtered.odm.xml"
    export_to_odm(
        _build_mock_sdk(records=extra_records),
        "TRIAL_1",
        odm_filtered,
        config=SinkConfig(study_key="TRIAL_1", mask_phi=True),
        form_whitelist=[10],
        variable_whitelist=["AETERM", "PEAK_TEMP"],
        creation_date_time=datetime(2026, 3, 10, 0, 0, tzinfo=UTC),
        validate_schema=True,
    )
    filtered_arch = load_odm_archive(odm_filtered, strict=True)
    emitted_oids = {
        it.item_oid
        for s in filtered_arch.clinical_data[0].subjects
        for e in s.study_event_data
        for f in e.form_data
        for ig in f.item_group_data
        for it in ig.items
    }
    assert any("AETERM" in oid for oid in emitted_oids)
    assert not any("AESEV" in oid for oid in emitted_oids)

    # 2. Single-file and per-form CDASH incremental merge + voided pruning
    cdash_single = tmp_path / "cdash_inc.json"
    cdash_per_form = tmp_path / "cdash_pf_inc"
    cdash_ledger = tmp_path / "cdash_ledger.json"

    export_to_dataset_json(
        _build_mock_sdk(),
        "TRIAL_1",
        cdash_single,
        incremental=True,
        ledger_path=cdash_ledger,
        stream_name="cdash_single",
        validate_schema=True,
    )
    export_to_dataset_json(
        _build_mock_sdk(),
        "TRIAL_1",
        cdash_per_form,
        per_form=True,
        incremental=True,
        ledger_path=cdash_ledger,
        stream_name="cdash_pf",
        validate_schema=True,
    )
    # Run 2: Void record 1 (DM) and update record 2 (AE)
    delta = [
        Record(
            study_key="TRIAL_1",
            record_id=1,
            form_id=20,
            form_key="DM",
            subject_id=1,
            subject_key="SUBJ-01",
            deleted=True,
            date_modified=datetime(2026, 3, 8, 10, 0, tzinfo=UTC),
            record_data={"SEX": "M"},
        ),
        Record(
            study_key="TRIAL_1",
            record_id=2,
            form_id=10,
            form_key="AE",
            subject_id=1,
            subject_key="SUBJ-01",
            deleted=False,
            date_modified=datetime(2026, 3, 8, 11, 0, tzinfo=UTC),
            record_data={"AETERM": "Migraine", "AESEV": "SEVERE"},
        ),
    ]
    export_to_dataset_json(
        _build_mock_sdk(records=delta),
        "TRIAL_1",
        cdash_single,
        incremental=True,
        ledger_path=cdash_ledger,
        stream_name="cdash_single",
        validate_schema=True,
    )
    export_to_dataset_json(
        _build_mock_sdk(records=delta),
        "TRIAL_1",
        cdash_per_form,
        per_form=True,
        incremental=True,
        ledger_path=cdash_ledger,
        stream_name="cdash_pf",
        validate_schema=True,
    )
    single_rows = dataset_json_to_rows(load_dataset_json(cdash_single))
    assert len(single_rows) == 1
    assert single_rows[0]["AETERM"] == "Migraine"
    dm_rows_after_void = dataset_json_to_rows(load_dataset_json(cdash_per_form / "DM.json"))
    assert dm_rows_after_void == []

    # 3. Per-form SDTM multi-domain incremental export with variable_whitelist & void reconciliation
    sdtm_pf_dir = tmp_path / "sdtm_pf"
    export_to_dataset_json(
        _build_mock_sdk(),
        "TRIAL_1",
        sdtm_pf_dir,
        as_sdtm=True,
        per_form=True,
        variable_whitelist=["AETERM", "SEX"],
        incremental=True,
        ledger_path=cdash_ledger,
        stream_name="sdtm_pf",
        validate_schema=True,
    )
    assert (sdtm_pf_dir / "AE.json").exists()
    assert (sdtm_pf_dir / "DM.json").exists()
    export_to_dataset_json(
        _build_mock_sdk(records=delta),
        "TRIAL_1",
        sdtm_pf_dir,
        as_sdtm=True,
        per_form=True,
        variable_whitelist=["AETERM", "SEX"],
        incremental=True,
        ledger_path=cdash_ledger,
        stream_name="sdtm_pf",
        validate_schema=True,
    )
    validate_dataset_json(json.loads((sdtm_pf_dir / "AE.json").read_text(encoding="utf-8")))

    # 4. Zero-delta incremental runs preserve existing files untouched, multi-domain single-file SDTM, and guardrails
    with pytest.raises(ValueError, match="'path' is required"):
        export_to_odm(_build_mock_sdk(), "TRIAL_1", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="'path' is required"):
        export_to_dataset_json(_build_mock_sdk(), "TRIAL_1", None)  # type: ignore[arg-type]

    # Zero-delta incremental ODM and Dataset-JSON runs
    empty_delta_sdk = _build_mock_sdk(records=[])
    export_to_odm(
        empty_delta_sdk,
        "TRIAL_1",
        odm_filtered,
        incremental=True,
        ledger_path=cdash_ledger,
        stream_name="odm_zero_delta",
        include_admin_data=False,
        include_reference_data=False,
        include_audit_records=False,
        include_annotations=False,
    )
    export_to_dataset_json(
        empty_delta_sdk,
        "TRIAL_1",
        cdash_single,
        incremental=True,
        ledger_path=cdash_ledger,
        stream_name="cdash_single",
    )

    # Multi-domain single-file SDTM export (combines AE + DM when sdtm_domain is omitted and per_form=False)
    multi_sdtm_file = tmp_path / "multi_sdtm.json"
    export_to_dataset_json(
        _build_mock_sdk(),
        "TRIAL_1",
        multi_sdtm_file,
        as_sdtm=True,
        per_form=False,
        validate_schema=True,
    )
    multi_ds = load_dataset_json(multi_sdtm_file)
    assert multi_ds.name == "SDTM"
    assert multi_ds.records == 2
