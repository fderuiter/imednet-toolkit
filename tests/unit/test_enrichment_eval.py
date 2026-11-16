"""Unit tests for EnrichmentPipeline business logic evaluation with restricted AST."""

from __future__ import annotations

import logging

import pytest

from imednet.integrations.enrichment import EnrichmentPipeline, _to_snake_case
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


def test_to_snake_case_edge_cases() -> None:
    """Test _to_snake_case with edge cases including empty strings, non-alphanumeric chars, and snake_case."""
    # Empty string
    assert _to_snake_case("") == ""

    # Already snake_case
    assert _to_snake_case("already_snake_case") == "already_snake_case"
    assert _to_snake_case("date_created") == "date_created"
    assert _to_snake_case("record_id") == "record_id"

    # CamelCase and PascalCase
    assert _to_snake_case("camelCase") == "camel_case"
    assert _to_snake_case("PascalCase") == "pascal_case"
    assert _to_snake_case("HTMLParser") == "html_parser"
    assert _to_snake_case("dateCreated") == "date_created"

    # Alphanumeric with numbers
    assert _to_snake_case("field123Value") == "field123_value"
    assert _to_snake_case("v1Alpha2") == "v1_alpha2"

    # Non-alphanumeric characters
    assert _to_snake_case("item-code") == "item-code"
    assert _to_snake_case("field.subfield") == "field.subfield"
    assert _to_snake_case("param#1") == "param#1"


def test_to_snake_case_memoization() -> None:
    """Test that _to_snake_case caches results with bounded lru_cache."""
    _to_snake_case.cache_clear()
    info = _to_snake_case.cache_info()
    assert info.hits == 0
    assert info.misses == 0
    assert info.maxsize == 1024

    res1 = _to_snake_case("cachedIdentifier")
    assert res1 == "cached_identifier"
    assert _to_snake_case.cache_info().misses == 1
    assert _to_snake_case.cache_info().hits == 0

    res2 = _to_snake_case("cachedIdentifier")
    assert res2 == "cached_identifier"
    assert _to_snake_case.cache_info().hits == 1
    assert _to_snake_case.cache_info().misses == 1
