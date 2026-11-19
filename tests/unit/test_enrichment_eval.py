"""Unit tests for EnrichmentPipeline business logic evaluation with restricted AST."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imednet.integrations.enrichment import EnrichmentPipeline, _to_snake_case
from imednet.models.records import Record
from imednet.models.study_config import MappingRule, StudyConfiguration
from imednet.testing.fake_data import fake_record
from imednet_sinks.warehouse import _records_to_arrow_table


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


def test_enrichment_pipeline_empty_rules_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that EnrichmentPipeline detects empty rules and returns input records directly."""
    # Default empty configuration
    config_default = StudyConfiguration(study_key="STUDY-EMPTY")
    pipeline_default = EnrichmentPipeline(config_default)
    assert pipeline_default._has_rules is False

    # Explicit empty collections
    config_explicit = StudyConfiguration(
        study_key="STUDY-EMPTY-EXPLICIT",
        mappings=[],
        terminology_lookups={},
        phi_fields=[],
    )
    pipeline_explicit = EnrichmentPipeline(config_explicit)
    assert pipeline_explicit._has_rules is False

    # Mapping with empty source_variable_name evaluates to no active rules
    config_no_source = StudyConfiguration(
        study_key="STUDY-NO-SRC",
        mappings=[
            MappingRule(
                domain="DM",
                target_field="target",
                source_form_key="DM_FORM",
                source_variable_name="",
            )
        ],
    )
    pipeline_no_source = EnrichmentPipeline(config_no_source)
    assert pipeline_no_source._has_rules is False

    # Verify input record is returned directly without recursive traversal
    data = {
        "record_id": 1001,
        "subject_key": "SUBJ-001",
        "nested": {"site": "SITE-01", "details": [1, 2, {"key": "val"}]},
    }

    spy = MagicMock(side_effect=pipeline_default._transform_recursive)
    monkeypatch.setattr(pipeline_default, "_transform_recursive", spy)

    result = pipeline_default.process(data)
    spy.assert_not_called()
    assert result is data


def test_enrichment_pipeline_active_rules_preservation() -> None:
    """Test that active rules trigger full recursive traversal across nested structures."""
    # PHI only
    config_phi = StudyConfiguration(
        study_key="STUDY-PHI",
        phi_fields=["patient_name"],
    )
    pipeline_phi = EnrichmentPipeline(config_phi)
    assert pipeline_phi._has_rules is True

    # Terminology only
    config_term = StudyConfiguration(
        study_key="STUDY-TERM",
        terminology_lookups={"gender": {"1": "Male", "2": "Female"}},
    )
    pipeline_term = EnrichmentPipeline(config_term)
    assert pipeline_term._has_rules is True

    # Combined configuration
    config_all = StudyConfiguration(
        study_key="STUDY-ALL",
        phi_fields=["patient_name"],
        terminology_lookups={"gender": {"1": "Male", "2": "Female"}},
        mappings=[
            MappingRule(
                domain="DM",
                target_field="age_in_months",
                source_form_key="DM_FORM",
                source_variable_name="age",
                business_logic="value * 12",
            )
        ],
    )
    pipeline_all = EnrichmentPipeline(config_all)
    assert pipeline_all._has_rules is True

    data = {
        "patient_name": "Alice",
        "gender": 1,
        "age": 5,
        "nested_dict": {
            "patient_name": "Bob",
            "gender": 2,
            "age": 10,
        },
        "nested_list": [
            {"patient_name": "Charlie", "gender": 1, "age": 2},
        ],
        "nested_tuple": ({"patient_name": "David", "gender": 2, "age": 3},),
    }

    result = pipeline_all.process(data)
    assert result is not data

    # Root level checks
    assert result["patient_name"] == "[MASKED]"
    assert result["gender"] == "Male"
    assert result["age_in_months"] == 60

    # Nested dict checks
    assert result["nested_dict"]["patient_name"] == "[MASKED]"
    assert result["nested_dict"]["gender"] == "Female"
    assert result["nested_dict"]["age_in_months"] == 120

    # Nested list checks
    assert result["nested_list"][0]["patient_name"] == "[MASKED]"
    assert result["nested_list"][0]["gender"] == "Male"
    assert result["nested_list"][0]["age_in_months"] == 24

    # Nested tuple checks
    assert isinstance(result["nested_tuple"], tuple)
    assert result["nested_tuple"][0]["patient_name"] == "[MASKED]"
    assert result["nested_tuple"][0]["gender"] == "Female"
    assert result["nested_tuple"][0]["age_in_months"] == 36


def test_arrow_conversion_speed_and_bypass() -> None:
    """Test that empty rule bypass eliminates traversal delay and speeds up Arrow conversion."""
    pipeline_empty = EnrichmentPipeline(StudyConfiguration(study_key="STUDY-BENCH"))
    assert pipeline_empty._has_rules is False

    test_record = {
        "record_id": 1,
        "subject_key": "SUBJ-001",
        "study_key": "STUDY-BENCH",
        "form_id": 10,
        "visit_id": 20,
        "deleted": False,
        "record_data": {
            "unvnum": "1",
            "aeterm": "Headache",
            "aeser": "No",
            "sub_dict": {"k1": "v1", "k2": "v2"},
        },
    }

    # 5,000 iterations of process() with empty rules should complete almost instantaneously
    start = time.perf_counter()
    for _ in range(5000):
        res = pipeline_empty.process(test_record)
        assert res is test_record
    elapsed_empty = time.perf_counter() - start

    # Empty bypass avoids the ~0.187s CPU reconstruction overhead across 5,000 records
    assert elapsed_empty < 0.05, (
        f"Expected empty pipeline process < 0.05s, took {elapsed_empty:.4f}s"
    )

    # Verify warehouse _records_to_arrow_table conversion speed with fake records
    records = [Record.model_validate(fake_record()) for _ in range(500)]
    start = time.perf_counter()
    table = _records_to_arrow_table(records)
    elapsed_arrow = time.perf_counter() - start

    assert table.num_rows == 500
    assert "record_id" in table.column_names
    assert elapsed_arrow < 1.0, (
        f"Expected 500-record Arrow conversion < 1.0s, took {elapsed_arrow:.4f}s"
    )


def test_centralized_mapper_cached_fields_and_shared_pipeline() -> None:
    """Test that CentralizedMapper caches field metadata and shares default pipeline."""
    from imednet.integrations.enrichment import _DEFAULT_PIPELINE, CentralizedMapper

    mapper = CentralizedMapper(mode="tabular")
    assert mapper._cached_fields is not None
    assert len(mapper._cached_fields) > 0

    record = Record.model_validate(fake_record())
    mapped = mapper.map_record(record, study_key="STUDY-UNCONFIGURED")

    # Unconfigured study key should point to shared default pipeline instance
    assert "STUDY-UNCONFIGURED" in mapper._pipeline_cache
    assert mapper._pipeline_cache["STUDY-UNCONFIGURED"] is _DEFAULT_PIPELINE
    assert mapped["study_key"] == "STUDY-UNCONFIGURED"

    # Custom study pipeline configured prior to mapping is preserved and executed
    custom_config = StudyConfiguration(
        study_key="STUDY-CUSTOM",
        phi_fields=["patient_name"],
    )
    custom_pipeline = EnrichmentPipeline(custom_config)
    mapper._pipeline_cache["STUDY-CUSTOM"] = custom_pipeline

    custom_record = {
        "record_id": 999,
        "study_key": "STUDY-CUSTOM",
        "record_data": {"patient_name": "John Doe", "vitalSign": 120},
    }
    mapped_custom = mapper.map_record(SimpleNamespace(**custom_record), study_key="STUDY-CUSTOM")
    assert mapper._pipeline_cache["STUDY-CUSTOM"] is custom_pipeline
    assert mapped_custom["record_data"]["patient_name"] == "[MASKED]"
    # Dynamic field snake_case conversion is preserved
    assert "vital_sign" in mapped_custom["record_data"]

    # Document mode, lazy cache initialization, metadata defaults, and post_processor
    orig_fields = CentralizedMapper._cached_fields
    orig_pairs = CentralizedMapper._cached_field_pairs
    try:
        CentralizedMapper._cached_fields = None
        CentralizedMapper._cached_field_pairs = None
        doc_mapper = CentralizedMapper(
            mode="document",
            post_processor=lambda d: {**d, "post_processed": True},
        )
        assert doc_mapper._cached_fields is not None

        CentralizedMapper._cached_fields = None
        sparse_record = SimpleNamespace(
            record_id=1000,
            deleted=None,
            date_modified=None,
            study_key=None,
            record_data={"nestedObj": {"innerVal": 1}},
        )
        mapped_sparse = doc_mapper.map_record(sparse_record, study_key=None)
        assert doc_mapper._cached_fields is not None
        assert mapped_sparse["deleted"] is False
        assert isinstance(mapped_sparse["date_modified"], str)
        assert mapped_sparse["study_key"] is None
        assert doc_mapper._pipeline_cache["UNKNOWN"] is _DEFAULT_PIPELINE
        assert mapped_sparse["record_data"]["nested_obj"] == {"innerVal": 1}
        assert mapped_sparse["post_processed"] is True
    finally:
        CentralizedMapper._cached_fields = orig_fields
        CentralizedMapper._cached_field_pairs = orig_pairs


def test_enrichment_pipeline_evaluator_exception_and_scalar_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test fallback when evaluate_expression raises unexpectedly and on non-container values."""
    config = StudyConfiguration(
        study_key="STUDY-FALLBACK",
        mappings=[
            MappingRule(
                domain="DM",
                target_field="computed",
                source_form_key="DM_FORM",
                source_variable_name="raw_val",
                business_logic="value + 1",
            )
        ],
    )
    pipeline = EnrichmentPipeline(config)

    # Scalar and primitive elements in list/tuple hit the base return path in _transform_recursive
    assert pipeline.process("scalar_value") == "scalar_value"
    list_tuple_res = pipeline.process({"items": ["a", 1], "pair": ("x", 2)})
    assert list_tuple_res == {"items": ["a", 1], "pair": ("x", 2)}

    monkeypatch.setattr(
        "imednet.integrations.enrichment.evaluate_expression",
        MagicMock(side_effect=RuntimeError("unexpected evaluator error")),
    )
    with caplog.at_level(logging.WARNING):
        res = pipeline.process({"raw_val": 42})

    assert res["computed"] == 42
    assert any("Business logic evaluation failed" in r.message for r in caplog.records)


def test_centralized_mapper_masks_default_phi_and_supports_opt_out() -> None:
    """Verify CentralizedMapper masks registered PHI to ***MASKED*** by default and honors mask_phi=False."""
    from imednet.integrations.enrichment import CentralizedMapper
    from imednet.utils.security import global_sensitivity_registry

    sensitive_snapshot = set(global_sensitivity_registry._sensitive_keys)
    exempt_snapshot = set(global_sensitivity_registry._exempt_keys)

    raw_payload = {
        "patientName": "Jane Doe",
        "DOB": "1980-04-12",
        "SSN": "123-45-6789",
        "email": "jane@clinical.example",
        "systolicBp": 118,
        "contacts": {"phone": "555-0199", "clinicCode": "C1"},
    }
    record = Record(
        record_id=501,
        study_key="STUDY1",
        subject_key="SUBJ-501",
        visit_id=1,
        form_id=10,
        record_data=dict(raw_payload),
    )

    doc_mapper = CentralizedMapper(mode="document")
    mapped_doc = doc_mapper.map_record(record, study_key="STUDY1")
    assert mapped_doc["study_key"] == "STUDY1"
    assert mapped_doc["subject_key"] == "SUBJ-501"
    assert mapped_doc["record_id"] == 501
    assert mapped_doc["record_data"]["patient_name"] == "***MASKED***"
    assert mapped_doc["record_data"]["dob"] == "***MASKED***"
    assert mapped_doc["record_data"]["ssn"] == "***MASKED***"
    assert mapped_doc["record_data"]["email"] == "***MASKED***"
    assert mapped_doc["record_data"]["contacts"]["phone"] == "***MASKED***"
    assert mapped_doc["record_data"]["contacts"]["clinicCode"] == "C1"
    assert mapped_doc["record_data"]["systolic_bp"] == 118
    # Input record must remain unmutated
    assert record.record_data["patientName"] == "Jane Doe"

    tab_mapper = CentralizedMapper(mode="tabular")
    mapped_tab = tab_mapper.map_record(record, study_key="STUDY1")
    assert mapped_tab["record_data"]["patient_name"] == "***MASKED***"
    assert mapped_tab["record_data"]["dob"] == "***MASKED***"
    assert mapped_tab["record_data"]["ssn"] == "***MASKED***"
    assert mapped_tab["record_data"]["email"] == "***MASKED***"
    assert mapped_tab["record_data"]["contacts.phone"] == "***MASKED***"
    assert mapped_tab["record_data"]["contacts.clinic_code"] == "C1"
    assert mapped_tab["record_data"]["systolic_bp"] == 118

    unmasked_mapper = CentralizedMapper(mode="tabular", mask_phi=False)
    mapped_unmasked = unmasked_mapper.map_record(record, study_key="STUDY1")
    assert mapped_unmasked["record_data"]["patient_name"] == "Jane Doe"
    assert mapped_unmasked["record_data"]["dob"] == "1980-04-12"
    assert mapped_unmasked["record_data"]["ssn"] == "123-45-6789"
    assert mapped_unmasked["record_data"]["email"] == "jane@clinical.example"
    assert global_sensitivity_registry._sensitive_keys == sensitive_snapshot
    assert global_sensitivity_registry._exempt_keys == exempt_snapshot


def test_study_configuration_phi_fields_case_insensitive_in_document_and_tabular_modes() -> None:
    """Verify StudyConfiguration(phiFields=...) matches snake_case keys across document and tabular modes."""
    from imednet.integrations.enrichment import CentralizedMapper

    study_cfg = StudyConfiguration(
        studyKey="STUDY1",
        phiFields=["patientName", "DOB", "SSN", "medicalRecordNumber", "subjectKey", "recordId"],
        terminology_lookups={"gender": {"1": "Female"}},
    )

    record = SimpleNamespace(
        record_id=777,
        study_key="STUDY1",
        subject_key="SUBJ-777",
        visit_id=2,
        form_id=5,
        record_data={
            "patientName": "John Roe",
            "DOB": "1975-09-21",
            "SSN": "987-65-4321",
            "medicalRecordNumber": "MRN-42",
            "gender": "1",
            "heartRate": 72,
            "demographics": {"medicalRecordNumber": "MRN-NESTED"},
        },
    )

    for mode in ("document", "tabular"):
        mapper = CentralizedMapper(mode=mode, study_config=study_cfg)
        mapped = mapper.map_record(record, study_key="STUDY1")

        # Exempt relational keys remain unmasked even if listed in phiFields
        assert mapped["study_key"] == "STUDY1"
        assert mapped["subject_key"] == "SUBJ-777"
        assert mapped["record_id"] == 777

        rd = mapped["record_data"]
        assert rd["patient_name"] in {"***MASKED***", "[MASKED]"}
        assert rd["dob"] in {"***MASKED***", "[MASKED]"}
        assert rd["ssn"] in {"***MASKED***", "[MASKED]"}
        assert rd["medical_record_number"] == "[MASKED]"
        assert rd["gender"] == "Female"
        assert rd["heart_rate"] == 72
        if mode == "document":
            assert rd["demographics"]["medicalRecordNumber"] == "[MASKED]"
        else:
            assert rd["demographics.medical_record_number"] == "[MASKED]"

    # Explicit opt-out skips PHI masking while keeping terminology enrichment active
    opt_out_mapper = CentralizedMapper(mode="document", mask_phi=False, study_config=study_cfg)
    unmasked = opt_out_mapper.map_record(record, study_key="STUDY1")
    assert unmasked["record_data"]["patient_name"] == "John Roe"
    assert unmasked["record_data"]["medical_record_number"] == "MRN-42"
    assert unmasked["record_data"]["gender"] == "Female"
