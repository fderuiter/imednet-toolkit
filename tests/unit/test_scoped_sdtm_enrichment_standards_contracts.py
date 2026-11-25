"""Regression tests for unified scoped SDTM, enrichment, and standards contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from imednet.integrations.enrichment import CentralizedMapper, EnrichmentPipeline
from imednet.integrations.sink_base import (
    SinkConfig,
    commit_sdtm_lineage,
    deduplicate_sdtm_rows,
    rehydrate_sdtm_lineage,
)
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.sdtm import (
    _iter_sorted_sdtm_frames,
    _SdtmNormalizer,
    iter_subject_ordered_sdtm_records,
    transform_records_to_sdtm,
)
from imednet.models.standards import (
    NORMALIZATION_TERMINOLOGY,
    BiologicSafetyProfile,
    CategoricalNormalizer,
    DeviceSafetyProfile,
    DrugSafetyProfile,
    StandardsReadinessValidator,
    VaccineSafetyProfile,
    validate_cdash_variable,
)
from imednet.models.study_config import MappingRule, StudyConfiguration
from imednet.models.variables import Variable


@pytest.mark.parametrize("token", ["0", "1", "T", "F", "Y", "N", "YES", "NO", "true", "false"])
def test_categorical_normalizer_preserves_clinical_string_tokens_by_default(token: str) -> None:
    """Req 1 / AC 1: CategoricalNormalizer keeps string tokens on clinical variables unless boolean_fields opts in."""
    normalizer = CategoricalNormalizer()
    record = {
        "vsResult": token,
        "exDose": token,
        "vsTestCd": token,
        "age": token,
        "arm": token,
        "aeSerious": token,
    }
    res = normalizer.normalize_record(record)
    for field in ("vsResult", "exDose", "vsTestCd", "age", "arm", "aeSerious"):
        assert isinstance(res.normalized_record[field], str)
        assert res.normalized_record[field] == token

    explicit_bool = CategoricalNormalizer(boolean_fields={"aeSerious"})
    res_explicit = explicit_bool.normalize_record(record)
    assert isinstance(res_explicit.normalized_record["aeSerious"], bool)
    for field in ("vsResult", "exDose", "vsTestCd", "age", "arm"):
        assert isinstance(res_explicit.normalized_record[field], str)
        assert res_explicit.normalized_record[field] == token

    with pytest.raises(ValueError, match="boolean_fields"):
        CategoricalNormalizer(boolean_fields="aeSerious")


@pytest.mark.parametrize(
    "profile_cls",
    [DrugSafetyProfile, BiologicSafetyProfile, VaccineSafetyProfile, DeviceSafetyProfile],
)
@pytest.mark.parametrize("severity", ["MILD", "MODERATE", "SEVERE", "mild", 1, "5"])
def test_safety_profiles_accept_cdisc_ae_severities_and_grades(
    profile_cls: type[Any],
    severity: Any,
) -> None:
    """Req 2: StandardsProfile presets accept MILD, MODERATE, and SEVERE alongside grades 1..5."""
    profile = profile_cls()
    record = {
        "subjectKey": "SUBJ-01",
        "aeTerm": "Headache",
        "aeSeverity": severity,
        "aeDecod": "HEADACHE",
        "aeRelationship": "RELATED",
        "aeActionTaken": "NONE",
        "aeOutcome": "RECOVERED",
        "aeSerious": "N",
    }
    violations = profile.validate("AE", record)
    assert [v for v in violations if v.severity == "ERROR"] == []
    if profile.value_constraints.get("AE.aeSeverity"):
        bool_violations = profile.validate("AE", {**record, "aeSeverity": True})
        assert any(v.field == "aeSeverity" and v.severity == "ERROR" for v in bool_violations)


@pytest.mark.parametrize("dd_serious", [True, False, "Y", "N", "y", "n"])
def test_standards_readiness_validator_scores_valid_dd_and_ae_at_100_with_terminology(
    dd_serious: Any,
) -> None:
    """Req 2 & Req 3 / AC 2: StandardsReadinessValidator with NORMALIZATION_TERMINOLOGY scores valid DD and AE at 100.0."""
    validator = StandardsReadinessValidator(
        profile=DeviceSafetyProfile(),
        normalizer=CategoricalNormalizer(NORMALIZATION_TERMINOLOGY),
    )
    report = validator.score_records(
        records_by_domain={
            "DD": [
                {
                    "subjectKey": "SUBJ-01",
                    "ddTerm": "Sensor drift",
                    "ddCategory": "HARDWARE",
                    "ddDate": "2026-04-01",
                    "ddSerious": dd_serious,
                    "ddRelationship": "RELATED",
                    "ddActionTaken": "REPLACED",
                }
            ],
            "AE": [
                {
                    "subjectKey": "SUBJ-01",
                    "aeTerm": "Headache",
                    "aeSeverity": "MODERATE",
                }
            ],
        }
    )
    assert report.score == 100.0
    assert report.is_ready is True
    assert report.warnings == []
    assert report.violations == []


def test_cdash_dm_allows_brthdat_under_strict_cdash() -> None:
    """Req 4 / AC 3: validate_cdash_variable('BRTHDAT', 'DM') returns (True, None) and MappingRule accepts it."""
    assert validate_cdash_variable("BRTHDAT", "DM") == (True, None)
    assert validate_cdash_variable("brthdat", "Demographics") == (True, None)

    rule = MappingRule(
        domain="DM",
        targetField="BRTHDAT",
        sourceFormKey="CRF_01",
        sourceVariableName="RAW_DOB",
        strictCdash=True,
    )
    assert rule.target_field == "BRTHDAT"


def test_validate_standards_cli_scores_unredacted_records_applies_lookups_and_masks_phi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 5 & Req 6 / AC 4: validate-standards matches --mask-phi and --no-mask-phi scores/exit codes, applies lookups, infers Form DM, and leaks zero PHI."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    import imednet_workflows.cli as wf_cli
    from imednet import cli as root_cli

    def _invoke_cli(args: list[str]) -> SimpleNamespace:
        out = io.StringIO()
        err = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                root_cli.app(args)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        return SimpleNamespace(exit_code=exit_code, stdout=out.getvalue(), stderr=err.getvalue())

    runner = SimpleNamespace(invoke=lambda _app, args: _invoke_cli(args))
    sdk = MagicMock()
    sdk.forms.list.return_value = [
        Form(formKey="CRF_01", formName="Demographics"),
    ]
    sdk.variables.list.return_value = [
        Variable(variableId=1, formKey="CRF_01", variableName="BRTHDAT"),
        Variable(variableId=2, formKey="CRF_01", variableName="SEX"),
    ]
    # Include PHI in subjectKey (custom phi_fields), birthDate, and dmPatientName/dmContactEmail, plus a raw sex code requiring terminology_lookups
    secret_subject = "PAT-SSN-999-88-7777"  # pragma: allowlist secret
    secret_dob = "1981-07-19"
    secret_name = "Alice ClinicalSecret"  # pragma: allowlist secret
    secret_email = "alice.secret@hospital.example"  # pragma: allowlist secret
    sdk.records.list.return_value = [
        Record(
            recordId=10,
            studyKey="STUDY_AUDIT",
            formKey="CRF_01",
            subjectKey=secret_subject,
            recordData={
                "SEX": "FEMALE_CODE",
                "BRTHDAT": secret_dob,
                "AGE": "44",
                "RACE": "ASIAN",
                "ETHNIC": "NOT HISPANIC OR LATINO",
                "dmPatientName": secret_name,
                "dmContactEmail": secret_email,
            },
        )
    ]

    study_cfg = StudyConfiguration(
        studyKey="STUDY_AUDIT",
        version="1.0.0",
        reportingProfile="drug",
        phiFields=["subjectKey", "dmPatientName", "patientName"],
        terminologyLookups={
            "sex": {"FEMALE_CODE": "F", "MALE_CODE": "M"},
        },
        mappings=[],
    )
    monkeypatch.setenv("IMEDNET_API_KEY", "test-api-key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-security-key")
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda *_a, **_kw: sdk)
    monkeypatch.setattr(wf_cli, "resolve_study_configuration", lambda _sk: study_cfg)
    monkeypatch.setattr(
        "imednet.integrations.enrichment.resolve_study_configuration",
        lambda _sk: study_cfg,
    )

    # 1. Passing study: --mask-phi and --no-mask-phi both score CRF_01 as DM at 100.0 and exit 0
    res_masked_json = runner.invoke(
        root_cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_AUDIT",
            "--profile",
            "drug",
            "--mask-phi",
            "--format",
            "json",
        ],
    )
    res_unmasked_json = runner.invoke(
        root_cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_AUDIT",
            "--profile",
            "drug",
            "--no-mask-phi",
            "--format",
            "json",
        ],
    )
    assert res_masked_json.exit_code == 0, res_masked_json.stdout
    assert res_unmasked_json.exit_code == 0, res_unmasked_json.stdout

    masked_item = json.loads(res_masked_json.stdout)["items"][0]
    unmasked_item = json.loads(res_unmasked_json.stdout)["items"][0]
    assert masked_item["score"] == 100.0
    assert unmasked_item["score"] == 100.0
    assert masked_item["domain_scores"] == {"DM": 100.0}
    assert unmasked_item["domain_scores"] == {"DM": 100.0}
    assert masked_item["is_ready"] == unmasked_item["is_ready"] is True
    assert masked_item["warnings"] == []
    assert unmasked_item["warnings"] == []

    for secret in (secret_subject, secret_dob, secret_name, secret_email):
        assert secret not in res_masked_json.stdout
        assert secret not in getattr(res_masked_json, "stderr", "")

    # 2. Unmapped lookup warning containing PHI subjectKey: identical failing scores/exit codes and zero leaked PHI in text or JSON
    sdk.records.list.return_value = [
        Record(
            recordId=11,
            studyKey="STUDY_AUDIT",
            formKey="CRF_01",
            subjectKey=secret_subject,
            recordData={
                "SEX": "UNMAPPED_SEX_CODE",
                "BRTHDAT": secret_dob,
                "AGE": "44",
                "RACE": "ASIAN",
                "ETHNIC": "NOT HISPANIC OR LATINO",
                "patientName": secret_name,
                "contactEmail": secret_email,
            },
        )
    ]
    fail_masked_json = runner.invoke(
        root_cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_AUDIT",
            "--profile",
            "drug",
            "--mask-phi",
            "--format",
            "json",
        ],
    )
    fail_unmasked_json = runner.invoke(
        root_cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_AUDIT",
            "--profile",
            "drug",
            "--no-mask-phi",
            "--format",
            "json",
        ],
    )
    fail_masked_text = runner.invoke(
        root_cli.app,
        [
            "workflows",
            "validate-standards",
            "STUDY_AUDIT",
            "--profile",
            "drug",
            "--mask-phi",
        ],
    )
    assert fail_masked_json.exit_code == fail_unmasked_json.exit_code == 1
    assert fail_masked_text.exit_code == 1
    assert (
        json.loads(fail_masked_json.stdout)["items"][0]["score"]
        == json.loads(fail_unmasked_json.stdout)["items"][0]["score"]
    )
    for output_blob in (
        fail_masked_json.stdout,
        getattr(fail_masked_json, "stderr", ""),
        fail_masked_text.stdout,
        getattr(fail_masked_text, "stderr", ""),
    ):
        for secret in (secret_subject, secret_dob, secret_name, secret_email):
            assert secret not in output_blob

    # 3. Short custom PHI values do not corrupt English prose/domain names, and SSN/email in subjectKey redact even without phiFields
    no_phi_fields_cfg = StudyConfiguration(
        studyKey="STUDY_AUDIT",
        version="1.0.0",
        reportingProfile="drug",
        phiFields=["shortTag"],
        terminologyLookups={"sex": {"FEMALE_CODE": "F"}},
        mappings=[],
    )
    monkeypatch.setattr(wf_cli, "resolve_study_configuration", lambda _sk: no_phi_fields_cfg)
    monkeypatch.setattr(
        "imednet.integrations.enrichment.resolve_study_configuration",
        lambda _sk: no_phi_fields_cfg,
    )
    sdk.records.list.return_value = [
        Record(
            recordId=12,
            studyKey="STUDY_AUDIT",
            formKey="CRF_01",
            subjectKey="patient.leak@hospital.example",
            recordData={
                "SEX": "UNMAPPED_SEX_CODE",
                "BRTHDAT": secret_dob,
                "AGE": "44",
                "RACE": "ASIAN",
                "ETHNIC": "NOT HISPANIC OR LATINO",
                "shortTag": "in",
            },
        )
    ]
    edge_masked_text = runner.invoke(
        root_cli.app,
        ["workflows", "validate-standards", "STUDY_AUDIT", "--profile", "drug", "--mask-phi"],
    )
    assert edge_masked_text.exit_code == 1
    assert "patient.leak@hospital.example" not in edge_masked_text.stdout
    assert secret_dob not in edge_masked_text.stdout
    assert "DM: " in edge_masked_text.stdout


def test_sdtm_and_centralized_mapper_unpack_camelcase_record_data_and_exclude_envelope_keys() -> (
    None
):
    """Req 7 / AC 5: transform_records_to_sdtm and CentralizedMapper unpack camelCase recordData and exclude envelope metadata."""
    rest_payload = {
        "studyKey": "STUDY_REST",
        "intervalId": 12,
        "formId": 303,
        "formKey": "F_AE",
        "siteId": 5,
        "recordId": 501,
        "recordOid": "REC-OID-501",
        "recordType": "SUBJECT",
        "recordStatus": "Record Complete",
        "programKey": "PRG",
        "deleted": False,
        "dateCreated": "2026-05-01 09:00:00",
        "dateModified": "2026-05-01 09:30:00",
        "lastUpdated": "2026-05-01 09:30:00",
        "subjectId": 88,
        "subjectOid": "SUBJ-OID-88",
        "subjectKey": "SUBJ-88",
        "visitId": 2,
        "parentRecordId": None,
        "keywords": [],
        "recordData": {
            "AETERM": "Migraine",
            "AESEV": "MODERATE",
            "AESTDTC": "2026-05-01",
        },
    }
    model_rec = Record(
        recordId=502,
        studyKey="STUDY_REST",
        formId=303,
        formKey="F_AE",
        subjectKey="SUBJ-88",
        deleted=False,
        dateCreated="2026-05-02 10:00:00",
        dateModified="2026-05-02 10:15:00",
        recordData={
            "AETERM": "Nausea",
            "AESEV": "MILD",
            "AESTDTC": "2026-05-02",
        },
    )
    obj_rec = SimpleNamespace(
        recordId=503,
        studyKey="STUDY_REST",
        formKey="F_AE",
        subjectKey="SUBJ-88",
        deleted=False,
        dateCreated="2026-05-03 11:00:00",
        dateModified="2026-05-03 11:15:00",
        recordData={
            "AETERM": "Fatigue",
            "AESEV": "MILD",
            "AESTDTC": "2026-05-03",
        },
    )

    forbidden_cols = {
        "date_created",
        "dateCreated",
        "date_modified",
        "dateModified",
        "formKey",
        "form_key",
        "deleted",
        "recordData",
        "record_data",
        "intervalId",
        "siteId",
        "recordOid",
        "recordType",
        "subjectId",
        "subjectOid",
        "parentRecordId",
        "lastUpdated",
        "keywords",
    }

    df = transform_records_to_sdtm(
        [rest_payload, model_rec, obj_rec],
        domain="AE",
        study_key="STUDY_REST",
        mask_phi=False,
    )
    assert list(df["AETERM"]) == ["Migraine", "Nausea", "Fatigue"]
    assert list(df["AESEQ"]) == [1, 2, 3]
    assert forbidden_cols.isdisjoint(set(df.columns))
    # Original input dict must not be mutated
    assert "recordData" in rest_payload

    mapper = CentralizedMapper(as_sdtm=True, sdtm_domain="AE", mask_phi=False)
    mapped_single = mapper.map_record(rest_payload, study_key="STUDY_REST")
    assert mapped_single["AETERM"] == "Migraine"
    assert forbidden_cols.isdisjoint(set(mapped_single.keys()))

    mapper_batch = CentralizedMapper(as_sdtm=True, sdtm_domain="AE", mask_phi=False)
    batch_pairs = mapper_batch.map_batch_with_sources(
        [rest_payload, model_rec, obj_rec],
        study_key="STUDY_REST",
    )
    assert [row["AETERM"] for _, row in batch_pairs] == ["Migraine", "Nausea", "Fatigue"]
    for _, row in batch_pairs:
        assert forbidden_cols.isdisjoint(set(row.keys()))


def test_enrichment_pipeline_isolates_form_rules_on_shared_severity_and_applies_fallbacks() -> None:
    """Req 8 / AC 6: EnrichmentPipeline isolates F_AE and F_DV rules on shared SEVERITY keys and populates fallback_value."""
    cfg = StudyConfiguration(
        studyKey="STUDY_RULES",
        version="1.0.0",
        reportingProfile="general",
        mappings=[
            MappingRule(
                sourceFormKey="F_AE",
                sourceVariableName="SEVERITY",
                domain="AE",
                targetField="AESEV",
                fallbackValue="MILD",
            ),
            MappingRule(
                sourceFormKey="F_DV",
                sourceVariableName="SEVERITY",
                domain="DV",
                targetField="DVSEV",
                fallbackValue="MINOR",
            ),
            MappingRule(
                sourceFormKey="F_AE_FOLLOWUP",
                sourceVariableName="SEVERITY",
                domain="AE",
                targetField="AESEV",
                fallbackValue="MODERATE",
            ),
        ],
    )
    pipeline = EnrichmentPipeline(cfg)

    # 1. Shared SEVERITY variable isolated by formKey even without explicit domain arg
    ae_out = pipeline.process({"formKey": "F_AE", "SEVERITY": "SEVERE"}, mask_phi=False)
    dv_out = pipeline.process({"formKey": "F_DV", "SEVERITY": "MAJOR"}, mask_phi=False)
    assert ae_out["AESEV"] == "SEVERE" and "DVSEV" not in ae_out
    assert dv_out["DVSEV"] == "MAJOR" and "AESEV" not in dv_out

    # 2. Missing/empty source values populate each Form's fallback_value without cross-form leakage
    ae_missing = pipeline.process({"formKey": "F_AE"}, mask_phi=False)
    dv_missing = pipeline.process({"formKey": "F_DV"}, mask_phi=False)
    ae_fu_missing = pipeline.process(
        {"formKey": "F_AE_FOLLOWUP"},
        domain="AE",
        mask_phi=False,
    )
    assert ae_missing["AESEV"] == "MILD" and "DVSEV" not in ae_missing
    assert dv_missing["DVSEV"] == "MINOR" and "AESEV" not in dv_missing
    assert ae_fu_missing["AESEV"] == "MODERATE" and "DVSEV" not in ae_fu_missing

    # 3. Envelope dict with nested camelCase recordData applies fallback inside recordData
    envelope_missing = pipeline.process(
        {"formKey": "F_DV", "recordData": {"DVTERM": "Late visit"}},
        mask_phi=False,
    )
    assert envelope_missing["recordData"]["DVSEV"] == "MINOR"
    assert "DVSEV" not in envelope_missing
    assert "AESEV" not in envelope_missing["recordData"]


def test_reassigning_record_to_another_subject_assigns_distinct_seq_without_dropping_prior_events() -> (
    None
):
    """Req 9 / AC 7: Reassigning a Record to another Subject assigns a distinct sequence number without dropping prior Subject events."""
    active_tx: dict[str, Any] = {"metadata": {}}
    fake_sink = SimpleNamespace(
        config=SinkConfig(study_key="STUDY_LIN"),
        _active_tx=active_tx,
        write_batch=lambda *_a, **_k: 0,
    )

    # Run 1: Record 101 belongs to S01 (AESEQ=1) and Record 102 belongs to S02 (AESEQ=1)
    run1_records = [
        Record(
            recordId=101,
            studyKey="STUDY_LIN",
            formKey="F_AE",
            subjectKey="S01",
            recordData={"AETERM": "S01 Event", "AESTDTC": "2026-01-01"},
        ),
        Record(
            recordId=102,
            studyKey="STUDY_LIN",
            formKey="F_AE",
            subjectKey="S02",
            recordData={"AETERM": "S02 Original Event", "AESTDTC": "2026-01-02"},
        ),
    ]
    run1_lineage: dict[str, dict[str, Any]] = {}
    run1_mapper = CentralizedMapper(
        as_sdtm=True,
        sdtm_domain="AE",
        mask_phi=False,
        lineage_out=run1_lineage,
    )
    run1_rows = [row for _, row in run1_mapper.map_batch_with_sources(run1_records, "STUDY_LIN")]
    commit_sdtm_lineage(fake_sink, run1_lineage, study_key="STUDY_LIN")
    assert [(r["USUBJID"], r["AESEQ"], r["AETERM"]) for r in run1_rows] == [
        ("S01", 1, "S01 Event"),
        ("S02", 1, "S02 Original Event"),
    ]

    # Run 2: Record 101 is corrected/reassigned from S01 to S02.
    # Simulate warehouse/document/graph plugin pattern: plain dict updated from rehydrate_sdtm_lineage
    rehydrated_counters, seq_overrides, _ = rehydrate_sdtm_lineage(
        fake_sink,
        study_key="STUDY_LIN",
    )
    assert seq_overrides[("AE", "S01", "101")] == 1
    assert seq_overrides[("AE", "S02", "102")] == 1

    plugin_seq_overrides_ref: dict[str, int] = {}
    plugin_seq_overrides_ref.update(seq_overrides)

    reassigned_rec = Record(
        recordId=101,
        studyKey="STUDY_LIN",
        formKey="F_AE",
        subjectKey="S02",
        recordData={"AETERM": "Reassigned Event", "AESTDTC": "2026-01-01"},
    )
    run2_lineage: dict[str, dict[str, Any]] = {}
    run2_mapper = CentralizedMapper(
        as_sdtm=True,
        sdtm_domain="AE",
        mask_phi=False,
        domain_seq_counters=rehydrated_counters,
        seq_overrides=plugin_seq_overrides_ref,
        lineage_out=run2_lineage,
    )
    run2_rows = [
        row for _, row in run2_mapper.map_batch_with_sources([reassigned_rec], "STUDY_LIN")
    ]
    assert len(run2_rows) == 1
    assert run2_rows[0]["USUBJID"] == "S02"
    assert run2_rows[0]["AESEQ"] == 2

    # Combining prior S01/S02 events with reassigned event in deduplicate_sdtm_rows drops nothing
    deduped = deduplicate_sdtm_rows([*run1_rows, *run2_rows])
    assert len(deduped) == 3
    assert [(r["USUBJID"], r["AESEQ"], r["AETERM"]) for r in deduped] == [
        ("S01", 1, "S01 Event"),
        ("S02", 1, "S02 Original Event"),
        ("S02", 2, "Reassigned Event"),
    ]

    # Legacy unscoped record_seqs in a prior ledger must not override scoped seq_overrides on Subject reassignment
    legacy_record_seqs: dict[str, int] = {"101": 1, "102": 1}
    legacy_mapper = CentralizedMapper(
        as_sdtm=True,
        sdtm_domain="AE",
        mask_phi=False,
        domain_seq_counters={"AE": {"S01": 1, "S02": 1}},
        seq_overrides=plugin_seq_overrides_ref,
        record_seqs=legacy_record_seqs,
    )
    legacy_rows = [
        row for _, row in legacy_mapper.map_batch_with_sources([reassigned_rec], "STUDY_LIN")
    ]
    assert legacy_rows[0]["USUBJID"] == "S02"
    assert legacy_rows[0]["AESEQ"] == 2


def test_evaluate_standards_readiness_supports_sdtm_keyed_terminology_lookups() -> None:
    """Req 6: evaluate_standards_readiness applies terminology_lookups keyed by SDTM variable names without alias shadowing."""
    from imednet.integrations.sink_base import evaluate_standards_readiness

    ae_rec = Record(
        recordId=201,
        studyKey="STUDY_SDTM_LKP",
        formKey="CRF_AE",
        subjectKey="SUBJ-1",
        recordData={
            "AETERM": "Nausea",
            "AEDECOD": "NAUSEA",
            "AESEV": "MOD",
            "AESER": "N",
            "AESTDTC": "2026-06-01",
            "AEOUT": "RECOVERED",
            "AEREL": "POSSIBLE",
            "AEACN": "NONE",
        },
    )
    report = evaluate_standards_readiness(
        [ae_rec],
        study_key="STUDY_SDTM_LKP",
        standards_profile="drug",
        terminology_lookups={"AESEV": {"MOD": "MODERATE"}},
        form_domain_map={"CRF_AE": "AE"},
    )
    assert report.score == 100.0
    assert report.is_ready is True
    assert report.violations == []
    assert report.warnings == []


def test_iter_sorted_sdtm_frames_stages_late_subjects_and_verifies_monotonic_ordered_stream() -> (
    None
):
    """Req 10 / AC 8: _iter_sorted_sdtm_frames assigns chronological AESEQ without splitting Subjects on late arrivals, and verifies monotonic USUBJID when source_is_ordered=True."""
    normalizer = _SdtmNormalizer(
        domain="AE",
        study_key="STUDY_STRM",
        include_deleted=False,
        mask_phi=False,
    )

    def _late_subject_stream() -> Any:
        # First 3 subjects arrive in ascending order (>= chunk_size=2), then a late S01 record with an earlier date arrives!
        yield {
            "recordId": 1,
            "subjectKey": "S00",
            "recordData": {"AETERM": "S00-Only", "AESTDTC": "2026-05-05"},
        }
        yield {
            "recordId": 2,
            "subjectKey": "S01",
            "recordData": {"AETERM": "S01-Later", "AESTDTC": "2026-05-10"},
        }
        yield {
            "recordId": 3,
            "subjectKey": "S02",
            "recordData": {"AETERM": "S02-Only", "AESTDTC": "2026-05-02"},
        }
        yield {
            "recordId": 4,
            "subjectKey": "S03",
            "recordData": {"AETERM": "S03-Only", "AESTDTC": "2026-05-03"},
        }
        yield {
            "recordId": 5,
            "subjectKey": "S01",
            "recordData": {"AETERM": "S01-Earlier", "AESTDTC": "2026-05-01"},
        }

    frames = list(
        _iter_sorted_sdtm_frames(
            _late_subject_stream(),
            normalizer,
            chunk_size=2,
            seq_counters={},
            source_is_ordered=False,
        )
    )
    assert all(1 <= len(df) <= 2 for df in frames)
    for subj in ("S00", "S01", "S02", "S03"):
        containing_chunks = [idx for idx, df in enumerate(frames) if subj in set(df["USUBJID"])]
        assert len(containing_chunks) == 1, (
            f"Subject {subj} was split across chunks {containing_chunks}"
        )

    combined = pd.concat(frames, ignore_index=True)
    assert list(combined[["USUBJID", "AESEQ", "AETERM"]].itertuples(index=False, name=None)) == [
        ("S00", 1, "S00-Only"),
        ("S01", 1, "S01-Earlier"),
        ("S01", 2, "S01-Later"),
        ("S02", 1, "S02-Only"),
        ("S03", 1, "S03-Only"),
    ]

    # When source_is_ordered=True, non-monotonic USUBJID order raises ValueError
    with pytest.raises(ValueError, match="Non-monotonic USUBJID"):
        list(
            _iter_sorted_sdtm_frames(
                _late_subject_stream(),
                normalizer,
                chunk_size=2,
                seq_counters={},
                source_is_ordered=True,
            )
        )

    # iter_subject_ordered_sdtm_records also raises ValueError if non-monotonic USUBJID arrives after yielding
    def _post_yield_violation_stream() -> Any:
        yield Record(
            recordId=1,
            studyKey="STUDY_STRM",
            formKey="F_AE",
            subjectKey="S01",
            dateCreated=datetime(2026, 5, 1, tzinfo=UTC),
            recordData={"AETERM": "First"},
        )
        yield Record(
            recordId=2,
            studyKey="STUDY_STRM",
            formKey="F_AE",
            subjectKey="S02",
            dateCreated=datetime(2026, 5, 1, tzinfo=UTC),
            recordData={"AETERM": "Second"},
        )
        yield Record(
            recordId=3,
            studyKey="STUDY_STRM",
            formKey="F_AE",
            subjectKey="S01",
            dateCreated=datetime(2026, 5, 2, tzinfo=UTC),
            recordData={"AETERM": "Late S01"},
        )

    with pytest.raises(ValueError, match="Non-monotonic USUBJID"):
        list(
            iter_subject_ordered_sdtm_records(
                _post_yield_violation_stream(),
                study_key="STUDY_STRM",
                source_is_ordered=True,
            )
        )
