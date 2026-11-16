"""Unit tests for study config."""

import pytest
from pydantic import ValidationError

from imednet.models.study_config import MappingRule, StudyConfiguration, WidgetConfig


def test_study_configuration_roundtrip_json_with_aliases() -> None:
    """Test that study configuration roundtrip json with aliases."""
    config = StudyConfiguration.model_validate(
        {
            "studyKey": " STUDY-001 ",
            "reportingProfile": " drug ",
            "mappings": [
                {
                    "domain": " AE ",
                    "targetField": " aeSeverity ",
                    "sourceFormKey": " FORM_AE ",
                    "sourceVariableName": " AE_SEV ",
                    "fallbackValue": " Unknown ",
                }
            ],
            "terminologyLookups": {"aeSeverity": {"MILD ": " Mild "}},
            "widgets": [
                {
                    "widgetId": " w-1 ",
                    "type": " bar_chart ",
                    "title": " AE Severity ",
                    "domain": " AE ",
                    "xAxis": " aeSeverity ",
                    "yAxis": " count ",
                    "layoutCols": 6,
                }
            ],
        }
    )

    payload_json = config.model_dump_json(by_alias=True)
    parsed = StudyConfiguration.model_validate_json(payload_json)

    assert parsed == config
    assert parsed.study_key == "STUDY-001"
    assert parsed.reporting_profile == "drug"
    assert parsed.mappings[0].domain == "AE"
    assert parsed.widgets[0].title == "AE Severity"
    assert parsed.terminology_lookups["aeSeverity"]["MILD"] == "Mild"
    assert "MILD " not in parsed.terminology_lookups["aeSeverity"]


def test_study_configuration_accepts_snake_case_field_names() -> None:
    """Test that study configuration accepts snake case field names."""
    config = StudyConfiguration(
        study_key="STUDY-002",
        mappings=[
            MappingRule(
                domain="PD",
                target_field="dvSeverity",
                source_form_key="FORM_PD",
                source_variable_name="DV_SEV",
            )
        ],
        widgets=[WidgetConfig(widget_id="kpi-1", type="kpi_card", title="PD", domain="PD")],
    )

    dumped = config.model_dump(by_alias=True)

    assert dumped["studyKey"] == "STUDY-002"
    assert dumped["version"] == "1.0.0"
    assert dumped["reportingProfile"] == "general"
    assert dumped["mappings"][0]["targetField"] == "dvSeverity"
    assert dumped["widgets"][0]["layoutCols"] == 12


def test_study_configuration_rejects_unknown_reporting_profile() -> None:
    """Test that study configuration rejects unknown reporting profile."""
    with pytest.raises(ValidationError, match="reportingProfile must be one of"):
        StudyConfiguration.model_validate(
            {
                "studyKey": "STUDY-003",
                "reportingProfile": "custom",
            }
        )


def test_mapping_rule_rejects_invalid_cdash_target_in_strict_mode() -> None:
    """Test that MappingRule rejects non-CDASH target variables when strict mode is active."""
    # Prefix mismatch: cmMed is not valid in domain AE
    with pytest.raises(ValidationError, match="does not match CDASH domain prefix"):
        MappingRule(
            domain="AE",
            target_field="cmMed",
            source_form_key="FORM_AE",
            source_variable_name="AE_MED",
            strict=True,
        )

    # Completely non-standard variable name
    with pytest.raises(ValidationError, match="does not match CDASH domain prefix"):
        MappingRule.model_validate(
            {
                "domain": "AE",
                "targetField": "customNonStandardField",
                "sourceFormKey": "FORM_AE",
                "sourceVariableName": "RAW_VAR",
                "strictMode": True,
            }
        )


def test_mapping_rule_accepts_valid_cdash_targets_in_strict_mode() -> None:
    """Test that MappingRule accepts valid CDASH target variables in strict mode."""
    # Domain prefix variable
    rule_ae = MappingRule(
        domain="AE",
        target_field="aeSeverity",
        source_form_key="FORM_AE",
        source_variable_name="AE_SEV",
        strict=True,
    )
    assert rule_ae.target_field == "aeSeverity"

    # Uppercase CDASH variable
    rule_ae_upper = MappingRule(
        domain="AE",
        target_field="AETERM",
        source_form_key="FORM_AE",
        source_variable_name="AE_TERM",
        strict_cdash=True,
    )
    assert rule_ae_upper.target_field == "AETERM"

    # Universal CDASH identifier
    rule_id = MappingRule(
        domain="AE",
        target_field="subjectKey",
        source_form_key="FORM_AE",
        source_variable_name="SUBJ_ID",
        strict=True,
    )
    assert rule_id.target_field == "subjectKey"

    # PD domain accepts dv prefix
    rule_pd = MappingRule(
        domain="PD",
        target_field="dvSeverity",
        source_form_key="FORM_PD",
        source_variable_name="DEV_SEV",
        strict=True,
    )
    assert rule_pd.target_field == "dvSeverity"


def test_mapping_rule_permits_non_standard_targets_when_not_strict() -> None:
    """Test that non-standard variables are permitted when strict mode is inactive."""
    rule = MappingRule(
        domain="AE",
        target_field="custom_random_field",
        source_form_key="FORM_AE",
        source_variable_name="RAW_VAL",
        strict=False,
    )
    assert rule.target_field == "custom_random_field"


def test_study_configuration_strict_mode_rejects_invalid_mapping() -> None:
    """Test that StudyConfiguration rejects invalid mappings when strict mode is active."""
    with pytest.raises(ValidationError, match="does not match CDASH domain prefix"):
        StudyConfiguration.model_validate(
            {
                "studyKey": "STUDY-004",
                "strictMode": True,
                "mappings": [
                    {
                        "domain": "AE",
                        "targetField": "arbitrary_var",
                        "sourceFormKey": "FORM_AE",
                        "sourceVariableName": "AE_VAR",
                    }
                ],
            }
        )


def test_study_configuration_obeys_strict_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that IMEDNET_STRICT_CDASH activates the local naming policy."""
    monkeypatch.setenv("IMEDNET_STRICT_CDASH", "1")
    with pytest.raises(ValidationError, match="does not match CDASH domain prefix"):
        StudyConfiguration.model_validate(
            {
                "studyKey": "STUDY-005",
                "mappings": [
                    {
                        "domain": "AE",
                        "targetField": "invalid_var",
                        "sourceFormKey": "FORM_AE",
                        "sourceVariableName": "VAR",
                    }
                ],
            }
        )
