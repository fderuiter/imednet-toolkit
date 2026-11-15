"""Unit tests for EnrichmentPipeline business logic evaluation with restricted AST."""

from __future__ import annotations

import logging

import pytest

from imednet.integrations.enrichment import EnrichmentPipeline
from imednet.models.study_config import MappingRule, StudyConfiguration


def test_enrichment_pipeline_valid_business_logic() -> None:
    """Test that EnrichmentPipeline evaluates valid arithmetic and transformation rules."""
    config = StudyConfiguration(
        study_key="STUDY-ENRICH",
        mappings=[
            MappingRule(
                domain="DM",
                target_field="age_in_months",
                source_form_key="DM_FORM",
                source_variable_name="age",
                business_logic="value * 12",
            ),
            MappingRule(
                domain="DM",
                target_field="cleaned_code",
                source_form_key="DM_FORM",
                source_variable_name="site_code",
                business_logic="str(value).strip().upper()",
            ),
        ],
    )

    pipeline = EnrichmentPipeline(config)
    data = {
        "age": 5,
        "site_code": "  site-01  ",
        "nested": {"age": 10},
    }

    result = pipeline.process(data)
    assert result["age_in_months"] == 60
    assert result["cleaned_code"] == "SITE-01"
    assert result["nested"]["age_in_months"] == 120


def test_enrichment_pipeline_rejects_class_traversal_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that malicious class traversal payload fails safely and returns original value."""
    config = StudyConfiguration(
        study_key="STUDY-EXPLOIT",
        mappings=[
            MappingRule(
                domain="DM",
                target_field="exploited",
                source_form_key="DM_FORM",
                source_variable_name="raw_input",
                business_logic="value.__class__.__bases__[0].__subclasses__()",
            ),
        ],
    )

    pipeline = EnrichmentPipeline(config)
    data = {"raw_input": "safe_string"}

    with caplog.at_level(logging.WARNING):
        result = pipeline.process(data)

    # Exploitation fails safely: field is renamed to target_field with the original value unchanged
    assert result["exploited"] == "safe_string"
    assert any(
        "Security warning" in r.message
        and ("__class__" in r.message or "__subclasses__" in r.message or "forbidden" in r.message)
        for r in caplog.records
    )


def test_enrichment_pipeline_syntax_error_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that invalid expression syntax falls back to original value without error."""
    config = StudyConfiguration(
        study_key="STUDY-ERR",
        mappings=[
            MappingRule(
                domain="DM",
                target_field="target",
                source_form_key="DM_FORM",
                source_variable_name="source",
                business_logic="invalid +++ (((",
            ),
        ],
    )

    pipeline = EnrichmentPipeline(config)
    data = {"source": 99}

    with caplog.at_level(logging.WARNING):
        result = pipeline.process(data)

    assert result["target"] == 99
    assert any("Expression evaluation failed" in r.message for r in caplog.records)
