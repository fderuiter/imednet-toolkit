"""Unit tests for standards."""

import pytest

from imednet.models.standards import (
    PROFILE_REGISTRY,
    BiologicSafetyProfile,
    DeviceSafetyProfile,
    DrugSafetyProfile,
    GeneralClinicalProfile,
    StandardsProfile,
    StandardsReadinessValidator,
    VaccineSafetyProfile,
    _canonical_domain,
    extract_suppqual_rows,
    is_valid_cdash_variable,
    resolve_standards_profile,
    transform_records_to_sdtm,
    transform_records_to_suppqual,
    validate_cdash_variable,
)


def test_profile_registry_contains_general_drug_and_device_profiles() -> None:
    """Test that profile registry contains all five regulatory clinical profiles."""
    assert PROFILE_REGISTRY.list_profiles() == [
        "biologic",
        "device",
        "drug",
        "general",
        "vaccine",
    ]


def test_drug_profile_enforces_ae_decod_relationship_and_ctcae_grades() -> None:
    """Test that drug profile accepts CDISC AESEV terms and grades 1-5 and flags invalid values."""
    profile = DrugSafetyProfile()
    for allowed_sev in ("MILD", "MODERATE", "SEVERE", 1, 2, 3, 4, 5, "1", "2", "3", "4", "5"):
        valid_res = profile.validate(
            domain="AE",
            data={
                "subjectKey": "S001",
                "aeTerm": "Headache",
                "aeSeverity": allowed_sev,
                "aeDecod": "Headache",
                "aeRelationship": "RELATED",
                "aeActionTaken": "NONE",
                "aeOutcome": "RECOVERED",
            },
        )
        assert valid_res == []
        valid_sdtm_res = profile.validate(
            domain="AE",
            data={
                "USUBJID": "S1-S001",
                "AETERM": "Headache",
                "AESEV": allowed_sev,
                "AEDECOD": "Headache",
                "AEREL": "RELATED",
                "AEACN": "NONE",
                "AEOUT": "RECOVERED",
            },
        )
        assert valid_sdtm_res == []

    violations = profile.validate(
        domain="AE",
        data={
            "subjectKey": "S001",
            "aeTerm": "Headache",
            "aeSeverity": "EXTREME",
        },
    )

    assert {(violation.field, violation.severity) for violation in violations} == {
        ("aeDecod", "ERROR"),
        ("aeRelationship", "ERROR"),
        ("aeSeverity", "ERROR"),
        ("aeActionTaken", "WARNING"),
        ("aeOutcome", "WARNING"),
    }


def test_general_profile_only_warns_for_missing_recommended_ae_fields() -> None:
    """Test that general profile only warns for missing recommended ae fields."""
    profile = GeneralClinicalProfile()
    violations = profile.validate(
        domain="AE",
        data={
            "subjectKey": "S001",
            "aeTerm": "Headache",
            "aeSeverity": "SEVERE",
        },
    )

    assert {(violation.field, violation.severity) for violation in violations} == {
        ("aeDecod", "WARNING"),
        ("aeRelationship", "WARNING"),
    }


def test_device_profile_requires_boolean_dd_serious() -> None:
    """Test that device profile requires boolean dd serious."""
    profile = DeviceSafetyProfile()
    violations = profile.validate(
        domain="DD",
        data={
            "subjectKey": "S002",
            "ddTerm": "Battery failed",
            "ddCategory": "HARDWARE",
            "ddDate": "2024-03-15T12:30:00+00:00",
            "ddSerious": "yes",
        },
    )

    assert {(violation.field, violation.severity) for violation in violations} == {
        ("ddActionTaken", "WARNING"),
        ("ddRelationship", "WARNING"),
        ("ddSerious", "ERROR"),
    }


def test_general_profile_validates_demographics_required_and_recommended_fields() -> None:
    """Test that general profile enforces required and recommended fields for DM."""
    profile = GeneralClinicalProfile()
    violations = profile.validate(
        domain="DM",
        data={
            "subjectKey": "S001",
        },
    )

    # Missing required: sex; Missing recommended: birthDate, age, race, ethnicity
    assert {(violation.field, violation.severity) for violation in violations} == {
        ("sex", "ERROR"),
        ("birthDate", "WARNING"),
        ("age", "WARNING"),
        ("race", "WARNING"),
        ("ethnicity", "WARNING"),
    }


def test_general_profile_validates_vital_signs_required_and_recommended_fields() -> None:
    """Test that general profile enforces required and recommended fields for VS."""
    profile = GeneralClinicalProfile()
    violations = profile.validate(
        domain="VS",
        data={
            "subjectKey": "S001",
            "vsTest": "Systolic Blood Pressure",
        },
    )

    # Missing required: vsResult, vsDate; Missing recommended: vsTestCd, vsUnit
    assert {(violation.field, violation.severity) for violation in violations} == {
        ("vsResult", "ERROR"),
        ("vsDate", "ERROR"),
        ("vsTestCd", "WARNING"),
        ("vsUnit", "WARNING"),
    }


def test_drug_profile_enforces_demographic_sex_and_vital_signs_unit_constraints() -> None:
    """Test that DrugSafetyProfile enforces value constraints on DM sex and VS units."""
    profile = DrugSafetyProfile()

    # Invalid sex
    dm_violations = profile.validate(
        domain="DM",
        data={
            "subjectKey": "S001",
            "sex": "INVALID_SEX",
            "birthDate": "1990-01-01",
            "age": 34,
            "race": "White",
            "ethnicity": "Not Hispanic",
        },
    )
    assert {(v.field, v.severity) for v in dm_violations} == {("sex", "ERROR")}
    assert "sex must be one of" in dm_violations[0].message

    # Invalid vsUnit
    vs_violations = profile.validate(
        domain="VS",
        data={
            "subjectKey": "S001",
            "vsTest": "Blood Pressure",
            "vsTestCd": "SYSBP",
            "vsResult": 120,
            "vsUnit": "parsecs",
            "vsDate": "2024-01-01T00:00:00",
        },
    )
    assert {(v.field, v.severity) for v in vs_violations} == {("vsUnit", "ERROR")}
    assert "vsUnit must be one of" in vs_violations[0].message


def test_device_profile_enforces_demographic_sex_and_vital_signs_unit_constraints() -> None:
    """Test that DeviceSafetyProfile enforces value constraints on DM sex and VS units."""
    profile = DeviceSafetyProfile()

    dm_violations = profile.validate(
        domain="DM",
        data={
            "subjectKey": "S002",
            "sex": "NON_STANDARD",
            "birthDate": "1995-01-01",
            "age": 29,
            "race": "Asian",
            "ethnicity": "Not Hispanic",
        },
    )
    assert {(v.field, v.severity) for v in dm_violations} == {("sex", "ERROR")}

    vs_violations = profile.validate(
        domain="VS",
        data={
            "subjectKey": "S002",
            "vsTest": "Pulse",
            "vsTestCd": "PULSE",
            "vsResult": 70,
            "vsUnit": "unknown_unit",
            "vsDate": "2024-01-01T00:00:00",
        },
    )
    assert {(v.field, v.severity) for v in vs_violations} == {("vsUnit", "ERROR")}


def test_domain_canonical_alias_resolution_for_dm_and_vs() -> None:
    """Test that standards profiles resolve natural domain names to canonical 2-letter codes."""
    profile = GeneralClinicalProfile()

    assert profile.expected_fields("Demographics") == profile.expected_fields("DM")
    assert profile.expected_fields("Vital Signs") == profile.expected_fields("VS")
    assert profile.expected_fields("vitals") == profile.expected_fields("VS")

    # Accessing required_fields directly with aliases or canonical keys
    assert "DM" in profile.required_fields
    assert "Demographics" in profile.required_fields
    assert "VS" in profile.required_fields
    assert "Vital Signs" in profile.required_fields


def test_domain_canonical_alias_resolution_for_concomitant_medications() -> None:
    """Test that canonical domain resolution normalizes Concomitant Medications aliases to CM."""
    aliases = [
        "CM",
        "cm",
        "CONCOMITANT MEDICATIONS",
        "CONCOMITANT_MEDICATIONS",
        "CONCOMITANT MEDICATION",
        "CONCOMITANT_MEDICATION",
        "Concomitant Medications",
        "concomitant medications",
        "concomitant-medications",
        "Concomitant Medication",
        "concomitant medication",
        "concomitant-medication",
        "  concomitant medications  ",
    ]
    for alias in aliases:
        assert _canonical_domain(alias) == "CM"


def test_validate_cdash_variable_concomitant_medications_aliases() -> None:
    """Test that validate_cdash_variable accepts CM variables across Concomitant Medications aliases."""
    cm_aliases = [
        "CM",
        "cm",
        "CONCOMITANT MEDICATIONS",
        "CONCOMITANT_MEDICATIONS",
        "CONCOMITANT MEDICATION",
        "CONCOMITANT_MEDICATION",
        "Concomitant Medications",
        "concomitant medications",
        "concomitant-medications",
        "Concomitant Medication",
        "concomitant-medication",
    ]

    for alias in cm_aliases:
        # Valid CM variable
        valid, err = validate_cdash_variable("cmTrt", alias)
        assert valid, f"Expected cmTrt to be valid for alias '{alias}', got error: {err}"
        assert err is None
        assert is_valid_cdash_variable("cmTrt", alias)

        # Universal identifier should also be accepted
        valid, err = validate_cdash_variable("studyId", alias)
        assert valid, f"Expected studyId to be valid for alias '{alias}'"
        assert err is None

        # Invalid prefix for CM domain should be rejected with expected prefix 'cm'
        invalid_valid, invalid_err = validate_cdash_variable("aeTerm", alias)
        assert not invalid_valid
        assert invalid_err is not None
        assert "does not match CDASH domain prefix for CM" in invalid_err
        assert "expected prefix: 'cm'" in invalid_err
        assert not is_valid_cdash_variable("aeTerm", alias)


def test_standards_profile_canonical_alias_resolution_for_cm() -> None:
    """Test that StandardsProfile and _DomainDict correctly resolve Concomitant Medications aliases."""
    profile = StandardsProfile(
        profile_name="conmed_test",
        required_fields={"CM": ["subjectKey", "cmTrt"]},
        recommended_fields={"CM": ["cmDose", "cmDosU"]},
    )

    expected = ["subjectKey", "cmTrt", "cmDose", "cmDosU"]
    assert profile.expected_fields("CM") == expected
    assert profile.expected_fields("Concomitant Medications") == expected
    assert profile.expected_fields("concomitant-medications") == expected
    assert profile.expected_fields("CONCOMITANT_MEDICATION") == expected
    assert profile.expected_fields("concomitant medication") == expected

    assert "CM" in profile.required_fields
    assert "Concomitant Medications" in profile.required_fields
    assert "concomitant-medications" in profile.required_fields
    assert "CONCOMITANT_MEDICATION" in profile.required_fields

    # Validation resolves CM aliases
    violations = profile.validate("Concomitant Medications", {"subjectKey": "S001"})
    assert {(v.field, v.severity) for v in violations} == {
        ("cmTrt", "ERROR"),
        ("cmDose", "WARNING"),
        ("cmDosU", "WARNING"),
    }

    no_violations = profile.validate(
        "concomitant-medications",
        {"subjectKey": "S001", "cmTrt": "Aspirin", "cmDose": 100, "cmDosU": "mg"},
    )
    assert no_violations == []


def test_core_sdtm_transform_enforces_pre_usubjid_phi_masking_and_cross_batch_seq() -> None:
    """Core SDTM transformation masks sensitive USUBJID/PHI before output and increments --SEQ across batches."""
    from imednet.models.records import Record
    from imednet.models.standards import (
        SDTM_DOMAIN_SPECS,
        CategoricalNormalizer,
        StandardsReadinessValidator,
        transform_records_to_sdtm_rows,
    )
    from imednet.utils.security import SensitivityRegistry

    assert {"DM", "AE", "LB", "VS", "CM", "EX", "MH"}.issubset(SDTM_DOMAIN_SPECS.keys())
    norm = CategoricalNormalizer(cdisc_ct={"sex": {"F", "M"}}).normalize_record(
        {"sex": "female"},
        terminology_lookups={"sex": {"FEMALE": "F"}},
    )
    assert norm.normalized_record["sex"] == "F"
    assert norm.warnings == []

    # Upstream Record instances must never be mutated in place
    rec1 = Record(
        record_id=1,
        study_key="ONCO1",
        form_key="AE",
        subject_key="SUBJ-101",
        record_data={
            "aeTerm": "Nausea",
            "aeDecod": "NAUSEA",
            "aeStartDate": "2026-01-10 08:30:00",
            "aeSerious": "yes",
            "aeSeverity": "Grade 2",
            "aeRelationship": "POSSIBLE",
            "patientName": "Alice Secret",
        },
    )
    rec2 = Record(
        record_id=2,
        study_key="ONCO1",
        form_key="AE",
        subject_key="SUBJ-101",
        record_data={
            "ae_term": "Fatigue",
            "AEDECOD": "FATIGUE",
            "AESTDTC": "2026-01-12",
            "AESER": False,
            "AESEV": "Grade 1",
            "AEREL": "UNLIKELY",
        },
    )
    original_dump_1 = rec1.model_dump()

    seq_counters: dict[str, int] = {}
    batch1 = transform_records_to_sdtm_rows(
        [rec1], domain="AE", study_key="ONCO1", mask_phi=True, seq_counters=seq_counters
    )
    batch2 = transform_records_to_sdtm_rows(
        [rec2], domain="AE", study_key="ONCO1", mask_phi=True, seq_counters=seq_counters
    )
    assert rec1.model_dump() == original_dump_1
    assert batch1[0]["STUDYID"] == "ONCO1"
    assert batch1[0]["DOMAIN"] == "AE"
    assert batch1[0]["USUBJID"] == "ONCO1-SUBJ-101"
    assert batch1[0]["AESEQ"] == 1
    assert batch1[0]["AESTDTC"] == "2026-01-10T08:30:00"
    assert batch1[0]["AESER"] == "Y"
    assert "patientName" not in batch1[0]
    assert "date_created" not in batch1[0]

    batch1_with_unmapped = transform_records_to_sdtm_rows(
        [rec1],
        domain="AE",
        study_key="ONCO1",
        mask_phi=True,
        include_unmapped=True,
    )
    assert batch1_with_unmapped[0]["patientName"] == "***MASKED***"

    assert batch2[0]["AESEQ"] == 2
    assert batch2[0]["AETERM"] == "Fatigue"
    assert batch2[0]["AEDECOD"] == "FATIGUE"
    assert batch2[0]["AESER"] == "N"

    # When subject_key is registered as sensitive in SensitivityRegistry or custom phi_keys,
    # USUBJID itself is masked BEFORE SDTM rows are emitted.
    custom_reg = SensitivityRegistry()
    custom_reg.remove_exempt_key("subject_key")
    custom_reg.add_sensitive_key("subject_key")
    masked_usubjid_rows = transform_records_to_sdtm_rows(
        [rec1],
        domain="AE",
        study_key="ONCO1",
        mask_phi=True,
        registry=custom_reg,
    )
    assert masked_usubjid_rows[0]["USUBJID"] == "***MASKED***"

    validator = StandardsReadinessValidator(profile="drug")
    ready_report = validator.score_records(
        records_by_domain={
            "AE": [
                {
                    "subjectKey": "SUBJ-101",
                    "aeTerm": "Nausea",
                    "aeDecod": "NAUSEA",
                    "aeSeverity": "2",
                    "aeRelationship": "POSSIBLE",
                    "aeActionTaken": "NONE",
                    "aeOutcome": "RECOVERED",
                }
            ]
        }
    )
    assert ready_report.score == 100.0
    assert ready_report.is_ready is True


def test_transform_records_to_sdtm_scores_100_percent_in_readiness_validator() -> None:
    """SDTM outputs across AE, DM, VS, DV, CM, DD, LB, EX, and MH score 100.0% with is_ready=True."""
    from imednet.models.records import Record

    study = "TRIAL01"
    dm_df = transform_records_to_sdtm(
        [
            Record(
                record_id=1,
                study_key=study,
                site_id=101,
                subject_key="S01",
                form_key="DM",
                record_data={
                    "subjid": "S01",
                    "sex": "F",
                    "birthDate": "1985-06-15",
                    "age": 40,
                    "race": "ASIAN",
                    "ethnicity": "NOT HISPANIC OR LATINO",
                },
            )
        ],
        domain="DM",
        study_key=study,
    )
    assert list(dm_df["USUBJID"]) == ["TRIAL01-S01"]
    assert list(dm_df["SITEID"]) == ["101"]
    assert list(dm_df["BRTHDTC"]) == ["1985"]

    ae_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "S01",
                "aeTerm": "Nausea",
                "aeDecod": "Nausea",
                "aeSeverity": "MODERATE",
                "aeSerious": False,
                "aeRelationship": "RELATED",
                "aeActionTaken": "NONE",
                "aeOutcome": "RECOVERED",
                "aeStartDate": "2026-02-01",
                "aeEndDate": "2026-02-03",
            }
        ],
        domain="AE",
        study_key=study,
    )
    vs_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "S01",
                "vsTest": "Heart Rate",
                "vsTestCd": "HR",
                "vsResult": 72,
                "vsUnit": "bpm",
                "vsDate": "2026-02-01",
            }
        ],
        domain="VS",
        study_key=study,
    )
    cm_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "S01",
                "cmTerm": "Ibuprofen",
                "cmDose": 200,
                "cmDoseUnit": "mg",
                "cmRoute": "ORAL",
                "cmStartDate": "2026-02-01",
                "cmEndDate": "2026-02-02",
                "cmIndication": "Headache",
            }
        ],
        domain="CM",
        study_key=study,
    )
    lb_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "S01",
                "lbTest": "Hemoglobin",
                "lbTestCd": "HGB",
                "lbCategory": "HEMATOLOGY",
                "lbResult": 13.8,
                "lbUnit": "g/dL",
                "lbDate": "2026-02-01",
            }
        ],
        domain="LB",
        study_key=study,
    )
    ex_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "S01",
                "exTrt": "IMP-100",
                "exDose": 50,
                "exDoseUnit": "mg",
                "exDoseForm": "INJECTION",
                "exRoute": "SUBCUTANEOUS",
                "exStartDate": "2026-02-01",
                "exEndDate": "2026-02-01",
            }
        ],
        domain="EX",
        study_key=study,
    )
    mh_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "S01",
                "mhTerm": "Seasonal Allergy",
                "mhDecod": "Seasonal allergy",
                "mhCategory": "GENERAL",
                "mhStartDate": "2020-04-01",
            }
        ],
        domain="MH",
        study_key=study,
    )
    dv_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "S01",
                "dvTerm": "Visit out of window",
                "dvCategory": "VISIT SCHEDULE",
                "dvSeverity": "MINOR",
                "dvDate": "2026-02-04",
                "dvStatus": "RESOLVED",
            }
        ],
        domain="DV",
        study_key=study,
    )
    dd_df = transform_records_to_sdtm(
        [
            {
                "subjectKey": "S01",
                "ddTerm": "Battery depleted",
                "ddCategory": "BATTERY",
                "ddSerious": False,
                "ddDate": "2026-02-05",
                "ddRelationship": "NOT RELATED",
                "ddActionTaken": "REPLACED",
            }
        ],
        domain="DD",
        study_key=study,
    )
    assert "DDREL" in dd_df.columns
    assert "DDACN" in dd_df.columns

    sdtm_batches = {
        "DM": dm_df.to_dict(orient="records"),
        "AE": ae_df.to_dict(orient="records"),
        "VS": vs_df.to_dict(orient="records"),
        "DV": dv_df.to_dict(orient="records"),
        "CM": cm_df.to_dict(orient="records"),
        "DD": dd_df.to_dict(orient="records"),
        "LB": lb_df.to_dict(orient="records"),
        "EX": ex_df.to_dict(orient="records"),
        "MH": mh_df.to_dict(orient="records"),
    }

    for profile_name in ("general", "drug", "biologic", "vaccine", "device"):
        validator = StandardsReadinessValidator(profile=profile_name)
        report = validator.score_records(records_by_domain=sdtm_batches)
        assert report.score == 100.0, f"Profile {profile_name} failed: {report.violations}"
        assert report.is_ready is True
        assert report.violations == []


@pytest.mark.parametrize("domain", ["CM", "LB", "EX", "MH"])
@pytest.mark.parametrize("profile_name", ["general", "drug", "biologic", "vaccine", "device"])
def test_empty_cm_lb_ex_mh_records_return_zero_readiness(domain: str, profile_name: str) -> None:
    """Scoring empty CM, LB, EX, or MH records returns 0.0% readiness with is_ready=False."""
    prof = resolve_standards_profile(profile_name)
    assert prof.required_fields.get(domain), f"{profile_name} missing required {domain}"
    assert prof.recommended_fields.get(domain), f"{profile_name} missing recommended {domain}"

    validator = StandardsReadinessValidator(profile=prof)
    report = validator.score_records(records_by_domain={domain: [{}]})
    assert report.score == 0.0
    assert report.domain_scores[domain] == 0.0
    assert report.is_ready is False
    assert any(v.severity == "ERROR" for v in report.violations)


def test_unprofiled_and_zero_matching_domain_batches_emit_error_and_zero_score() -> None:
    """Non-empty domain batches matching zero profile variables return 0.0 and an ERROR violation."""
    validator = StandardsReadinessValidator(profile="general")
    unprofiled_report = validator.score_records(
        records_by_domain={"FA": [{"USUBJID": "S1-01", "FATEST": "Pain"}]}
    )
    assert unprofiled_report.score == 0.0
    assert unprofiled_report.domain_scores["FA"] == 0.0
    assert unprofiled_report.is_ready is False
    assert any(v.field == "DOMAIN" and v.severity == "ERROR" for v in unprofiled_report.violations)

    rec_only_profile = StandardsProfile(
        profile_name="rec_only",
        recommended_fields={"CM": ["cmDose", "cmRoute"]},
    )
    zero_match_report = StandardsReadinessValidator(profile=rec_only_profile).score_records(
        records_by_domain={"CM": [{"unrelatedKey": "foo"}]}
    )
    assert zero_match_report.score == 0.0
    assert zero_match_report.is_ready is False
    assert any(v.severity == "ERROR" for v in zero_match_report.violations)

    empty_batch_report = validator.score_records(records_by_domain={})
    assert empty_batch_report.score == 0.0
    assert empty_batch_report.is_ready is False


def test_biologic_and_vaccine_profile_resolution_and_rules() -> None:
    """BiologicSafetyProfile and VaccineSafetyProfile resolve by name/alias and enforce rules."""
    assert isinstance(resolve_standards_profile("biologic"), BiologicSafetyProfile)
    assert isinstance(resolve_standards_profile("biologic_safety"), BiologicSafetyProfile)
    assert isinstance(resolve_standards_profile("vaccine"), VaccineSafetyProfile)
    assert isinstance(resolve_standards_profile("vaccine_safety"), VaccineSafetyProfile)

    bio_violations = BiologicSafetyProfile().validate(
        "EX",
        {"USUBJID": "S1-01", "EXTRT": "MAB-1", "EXDOSE": 100, "EXSTDTC": "2026-01-01"},
    )
    assert ("exRoute", "ERROR") in {(v.field, v.severity) for v in bio_violations}

    vac_violations = VaccineSafetyProfile().validate(
        "AE",
        {"USUBJID": "S1-01", "AETERM": "Injection site pain", "AESEV": "MILD"},
    )
    assert ("aeSerious", "ERROR") in {(v.field, v.severity) for v in vac_violations}
    assert ("aeStartDate", "ERROR") in {(v.field, v.severity) for v in vac_violations}


def test_chronological_stdtc_endtc_and_duplicate_dm_usubjid_validation() -> None:
    """StandardsReadinessValidator flags --STDTC > --ENDTC and duplicate DM USUBJIDs as ERRORs."""
    validator = StandardsReadinessValidator(profile="general")

    # Inverted SDTM dates and duplicate DM USUBJIDs
    bad_report = validator.score_records(
        records_by_domain={
            "AE": [
                {
                    "USUBJID": "S1-001",
                    "AETERM": "Headache",
                    "AEDECOD": "Headache",
                    "AESEV": "MILD",
                    "AEREL": "NOT RELATED",
                    "AESTDTC": "2026-03-10",
                    "AEENDTC": "2026-03-05",
                }
            ],
            "DM": [
                {
                    "USUBJID": "S1-001",
                    "SEX": "F",
                    "BRTHDTC": "1990",
                    "AGE": 35,
                    "RACE": "WHITE",
                    "ETHNIC": "NOT HISPANIC OR LATINO",
                },
                {
                    "USUBJID": "S1-001",
                    "SEX": "F",
                    "BRTHDTC": "1990",
                    "AGE": 35,
                    "RACE": "WHITE",
                    "ETHNIC": "NOT HISPANIC OR LATINO",
                },
            ],
        }
    )
    assert bad_report.is_ready is False
    assert bad_report.score < 100.0
    error_fields = {(v.field, v.severity) for v in bad_report.violations}
    assert ("AEENDTC", "ERROR") in error_fields
    assert ("USUBJID", "ERROR") in error_fields

    # Equal dates and partial ISO 8601 end dates with matching prefix do NOT false-positive
    ok_report = validator.score_records(
        records_by_domain={
            "AE": [
                {
                    "USUBJID": "S1-001",
                    "AETERM": "Headache",
                    "AEDECOD": "Headache",
                    "AESEV": "MILD",
                    "AEREL": "NOT RELATED",
                    "AESTDTC": "2026-03-15",
                    "AEENDTC": "2026-03",
                },
                {
                    "USUBJID": "S1-002",
                    "AETERM": "Nausea",
                    "AEDECOD": "Nausea",
                    "AESEV": "MODERATE",
                    "AEREL": "POSSIBLE",
                    "AESTDTC": "2026-03-15T12:00:00+05:00",
                    "AEENDTC": "2026-03-15T09:00:00Z",
                },
            ]
        }
    )
    assert ok_report.is_ready is True
    assert ok_report.score == 100.0
    assert ok_report.violations == []


def test_dm_deduplication_siteid_unmapped_stripping_and_suppqual_helpers() -> None:
    """DM outputs populate SITEID, deduplicate USUBJID, strip unmapped/date_created, and emit SUPP--."""
    from datetime import datetime

    from imednet.models.records import Record

    dm_records = [
        Record(
            record_id=1,
            study_key="STUDY99",
            site_id=42,
            subject_key="001",
            form_key="DM",
            date_created=datetime(2026, 1, 10, 9, 0),
            record_data={
                "subjid": "001",
                "sex": "M",
                "birthDate": "1978-11-22",
                "customSponsorCode": "SP-A",
                "patientName": "John Doe",
            },
        ),
        Record(
            record_id=2,
            study_key="STUDY99",
            site_id=42,
            subject_key="001",
            form_key="DM",
            date_created=datetime(2026, 1, 11, 9, 0),
            record_data={
                "subjid": "001",
                "race": "WHITE",
                "arm": "ARM A",
                "customSponsorCode": "SP-B",
            },
        ),
    ]

    dm_df = transform_records_to_sdtm(dm_records, domain="DM", study_key="STUDY99")
    assert len(dm_df) == 1
    assert dm_df.iloc[0]["USUBJID"] == "STUDY99-001"
    assert dm_df.iloc[0]["SUBJID"] == "001"
    assert dm_df.iloc[0]["SITEID"] == "42"
    assert dm_df.iloc[0]["BRTHDTC"] == "1978"
    assert dm_df.iloc[0]["SEX"] == "M"
    assert dm_df.iloc[0]["RACE"] == "WHITE"
    assert dm_df.iloc[0]["ARM"] == "ARM A"
    assert "DMSEQ" not in dm_df.columns
    assert "date_created" not in dm_df.columns
    assert "dateCreated" not in dm_df.columns
    assert "customSponsorCode" not in dm_df.columns
    assert "patientName" not in dm_df.columns

    supp_dm = transform_records_to_suppqual(dm_records, domain="DM", study_key="STUDY99")
    assert list(supp_dm["RDOMAIN"].unique()) == ["DM"]
    assert list(supp_dm["USUBJID"].unique()) == ["STUDY99-001"]
    qval_by_qlabel = dict(zip(supp_dm["QLABEL"], supp_dm["QVAL"], strict=True))
    assert qval_by_qlabel["customSponsorCode"] == "SP-B"
    assert qval_by_qlabel["patientName"] == "***MASKED***"

    ae_records = [
        Record(
            record_id=10,
            study_key="STUDY99",
            site_id=42,
            subject_key="001",
            form_key="AE",
            date_created=datetime(2026, 2, 1, 12, 0),
            record_data={
                "aeTerm": "Rash",
                "aeStartDate": "2026-02-01",
                "customGradeScale": "CTCAE-v5",
            },
        )
    ]
    supp_ae_rows = extract_suppqual_rows(ae_records, domain="AE", study_key="STUDY99")
    assert len(supp_ae_rows) == 1
    assert supp_ae_rows[0]["RDOMAIN"] == "AE"
    assert supp_ae_rows[0]["USUBJID"] == "STUDY99-001"
    assert supp_ae_rows[0]["IDVAR"] == "AESEQ"
    assert supp_ae_rows[0]["IDVARVAL"] == "1"
    assert supp_ae_rows[0]["QNAM"] == "CUSTOMGR"
    assert supp_ae_rows[0]["QLABEL"] == "customGradeScale"
    assert supp_ae_rows[0]["QVAL"] == "CTCAE-v5"
