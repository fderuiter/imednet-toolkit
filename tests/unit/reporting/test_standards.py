"""Unit tests for standards."""

from imednet.models.standards import (
    PROFILE_REGISTRY,
    DeviceSafetyProfile,
    DrugSafetyProfile,
    GeneralClinicalProfile,
    StandardsProfile,
    _canonical_domain,
    is_valid_cdash_variable,
    validate_cdash_variable,
)


def test_profile_registry_contains_general_drug_and_device_profiles() -> None:
    """Test that profile registry contains all five standard safety profiles."""
    assert PROFILE_REGISTRY.list_profiles() == [
        "biologic",
        "device",
        "drug",
        "general",
        "vaccine",
    ]


def test_drug_profile_enforces_ae_decod_relationship_and_ctcae_grades() -> None:
    """Test that drug profile enforces ae decod relationship and ctcae grades."""
    profile = DrugSafetyProfile()
    violations = profile.validate(
        domain="AE",
        data={
            "subjectKey": "S001",
            "aeTerm": "Headache",
            "aeSeverity": "SEVERE",
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
    assert batch1[0]["USUBJID"] == "SUBJ-101"
    assert batch1[0]["AESEQ"] == 1
    assert batch1[0]["AESTDTC"] == "2026-01-10T08:30:00"
    assert batch1[0]["AESER"] == "Y"
    assert batch1[0]["patientName"] == "***MASKED***"

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
