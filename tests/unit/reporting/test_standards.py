"""Unit tests for standards."""

from imednet.models.standards import (
    PROFILE_REGISTRY,
    DeviceSafetyProfile,
    DrugSafetyProfile,
    GeneralClinicalProfile,
)


def test_profile_registry_contains_general_drug_and_device_profiles() -> None:
    """Test that profile registry contains general drug and device profiles."""
    assert PROFILE_REGISTRY.list_profiles() == ["device", "drug", "general"]


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
