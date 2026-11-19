"""Unit tests for extraction engine."""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import patch

import pytest

from imednet.models import Record
from imednet.models.study_config import MappingRule, StudyConfiguration
from imednet_workflows.extraction_engine import (
    SubjectContext,
    _compile_business_logic,
    _evaluate_business_logic,
    _extract_rule_value_from_payload,
    _LazyRecordPayload,
    extract_canonical_records,
)


def test_extract_canonical_records_maps_ae_pd_dd_and_collects_row_errors() -> None:
    """Test that extract canonical records maps ae pd dd and collects row errors."""
    config = StudyConfiguration(
        study_key="STUDY-1",
        mappings=[
            MappingRule(
                domain="AE",
                target_field="subjectKey",
                source_form_key="AE_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="AE",
                target_field="aeTerm",
                source_form_key="AE_FORM",
                source_variable_name="recordData.ae.term",
            ),
            MappingRule(
                domain="AE",
                target_field="aeSeverity",
                source_form_key="AE_FORM",
                source_variable_name="recordData.ae.severity",
            ),
            MappingRule(
                domain="PD",
                target_field="subjectKey",
                source_form_key="PD_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="PD",
                target_field="dvTerm",
                source_form_key="PD_FORM",
                source_variable_name="recordData.dv.term",
            ),
            MappingRule(
                domain="PD",
                target_field="dvCategory",
                source_form_key="PD_FORM",
                source_variable_name="recordData.dv.category",
            ),
            MappingRule(
                domain="PD",
                target_field="dvSeverity",
                source_form_key="PD_FORM",
                source_variable_name="recordData.dv.severity",
            ),
            MappingRule(
                domain="PD",
                target_field="dvDate",
                source_form_key="PD_FORM",
                source_variable_name="recordData.dv.date",
            ),
            MappingRule(
                domain="DD",
                target_field="subjectKey",
                source_form_key="DD_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="DD",
                target_field="ddTerm",
                source_form_key="DD_FORM",
                source_variable_name="recordData.dd.term",
            ),
            MappingRule(
                domain="DD",
                target_field="ddCategory",
                source_form_key="DD_FORM",
                source_variable_name="recordData.dd.category",
            ),
            MappingRule(
                domain="DD",
                target_field="ddDate",
                source_form_key="DD_FORM",
                source_variable_name="recordData.dd.date",
            ),
        ],
    )

    records = [
        Record(
            record_id=1,
            form_key="AE_FORM",
            subject_key="SUBJ-001",
            date_created=datetime(2024, 1, 1),
            record_data={"ae": {"term": "Headache", "severity": "MILD"}},
        ),
        Record(
            record_id=2,
            form_key="PD_FORM",
            subject_key="SUBJ-001",
            date_created=datetime(2024, 1, 1),
            record_data={
                "dv": {
                    "term": "Missed visit",
                    "category": "PROCEDURE",
                    "severity": "MAJOR",
                    "date": "invalid-date-format",
                }
            },
        ),
        Record(
            record_id=3,
            form_key="DD_FORM",
            subject_key="SUBJ-002",
            date_created=datetime(2024, 1, 1),
            record_data={
                "dd": {
                    "term": "Battery failed",
                    "category": "HARDWARE",
                    "date": "2024-03-15T12:30:00+00:00",
                }
            },
        ),
        Record(
            record_id=4,
            form_key="OTHER_FORM",
            subject_key="SUBJ-003",
            date_created=datetime(2024, 1, 1),
            record_data={"foo": "bar"},
        ),
    ]

    result = extract_canonical_records(records=records, study_configuration=config)

    assert len(result.adverse_events) == 1
    assert result.adverse_events[0].subject_key == "SUBJ-001"
    assert result.adverse_events[0].ae_term == "Headache"

    assert len(result.protocol_deviations) == 0

    assert len(result.device_deficiencies) == 1
    assert result.device_deficiencies[0].subject_key == "SUBJ-002"
    assert result.device_deficiencies[0].dd_term == "Battery failed"

    assert len(result.validation_errors) == 1
    error = result.validation_errors[0]
    assert error["recordId"] == 2
    assert error["formKey"] == "PD_FORM"
    assert error["domain"] == "PD"
    assert error["payload"]["dvDate"] == "invalid-date-format"


def test_extract_canonical_records_uses_fallback_values() -> None:
    """Test that extract canonical records uses fallback values."""
    config = StudyConfiguration(
        study_key="STUDY-1",
        mappings=[
            MappingRule(
                domain="AE",
                target_field="subjectKey",
                source_form_key="AE_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="AE",
                target_field="aeTerm",
                source_form_key="AE_FORM",
                source_variable_name="recordData.term",
                fallback_value="Unknown event",
            ),
            MappingRule(
                domain="AE",
                target_field="aeSeverity",
                source_form_key="AE_FORM",
                source_variable_name="recordData.severity",
                fallback_value="MILD",
            ),
        ],
    )

    record = Record(
        record_id=5,
        form_key="AE_FORM",
        subject_key="SUBJ-010",
        date_created=datetime(2024, 1, 1),
        record_data={},
    )

    result = extract_canonical_records(records=[record], study_configuration=config)

    assert len(result.adverse_events) == 1
    assert result.adverse_events[0].ae_term == "Unknown event"
    assert result.adverse_events[0].ae_severity == "MILD"
    assert result.validation_errors == []


def test_extract_subject_centric_analysis_datasets() -> None:
    """Test that extract subject centric analysis datasets."""
    config = StudyConfiguration(
        study_key="STUDY-2",
        mappings=[
            # ADSL Mapping
            MappingRule(
                domain="ADSL",
                target_field="subjectKey",
                source_form_key="DM_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="ADSL",
                target_field="age",
                source_form_key="DM_FORM",
                source_variable_name="recordData.dm.age",
            ),
            MappingRule(
                domain="ADSL",
                target_field="firstDoseDate",
                source_form_key="EX_FORM",
                source_variable_name="recordData.ex.startDate",
                businessLogic="value",
            ),
            # ADAE Mapping
            MappingRule(
                domain="ADAE",
                target_field="subjectKey",
                source_form_key="AE_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="ADAE",
                target_field="aeTerm",
                source_form_key="AE_FORM",
                source_variable_name="recordData.ae.term",
            ),
            MappingRule(
                domain="ADAE",
                target_field="treatmentEmergent",
                source_form_key="AE_FORM",
                source_variable_name="",
                businessLogic="record.record_data.get('ae', {}).get('startDate', '') >= state.get('ADSL.firstDoseDate', '9999-99-99')",
            ),
            # ADLB Mapping - Baseline and Change
            MappingRule(
                domain="ADLB",
                target_field="subjectKey",
                source_form_key="LB_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="ADLB",
                target_field="labTest",
                source_form_key="LB_FORM",
                source_variable_name="recordData.lb.test",
            ),
            MappingRule(
                domain="ADLB",
                target_field="result",
                source_form_key="LB_FORM",
                source_variable_name="recordData.lb.result",
                isBaseline=True,
                businessLogic="value if record.record_data.get('lb', {}).get('visit') == 'Baseline' else value",
            ),
            MappingRule(
                domain="ADLB",
                target_field="changeFromBaseline",
                source_form_key="LB_FORM",
                source_variable_name="",
                businessLogic="float(payload.get('result', 0)) - float(baseline.get('ADLB.result', 0)) if 'ADLB.result' in baseline and record.record_data.get('lb', {}).get('visit') != 'Baseline' else None",
            ),
        ],
    )

    records = [
        Record(
            record_id=1,
            form_key="DM_FORM",
            subject_key="SUBJ-001",
            date_created=datetime(2024, 1, 1),
            record_data={"dm": {"age": 45}},
        ),
        Record(
            record_id=2,
            form_key="EX_FORM",
            subject_key="SUBJ-001",
            date_created=datetime(2024, 1, 2),
            record_data={"ex": {"startDate": "2024-02-01"}},
        ),
        Record(
            record_id=3,
            form_key="AE_FORM",
            subject_key="SUBJ-001",
            date_created=datetime(2024, 1, 3),
            record_data={"ae": {"term": "Nausea", "startDate": "2024-02-05"}},
        ),
        Record(
            record_id=4,
            form_key="AE_FORM",
            subject_key="SUBJ-001",
            date_created=datetime(2024, 1, 4),
            record_data={
                "ae": {"term": "Headache", "startDate": "2024-01-15"}
            },  # before first dose
        ),
        Record(
            record_id=5,
            form_key="LB_FORM",
            subject_key="SUBJ-001",
            date_created=datetime(2024, 1, 5),
            record_data={"lb": {"test": "ALT", "result": 40.0, "visit": "Baseline"}},
        ),
        Record(
            record_id=6,
            form_key="LB_FORM",
            subject_key="SUBJ-001",
            date_created=datetime(2024, 1, 6),
            record_data={"lb": {"test": "ALT", "result": 55.0, "visit": "Visit 2"}},
        ),
    ]

    result = extract_canonical_records(records=records, study_configuration=config)

    assert len(result.adsl_records) == 1
    adsl = result.adsl_records[0].model_dump(by_alias=True)
    assert adsl["subjectKey"] == "SUBJ-001"
    assert adsl["age"] == 45
    assert adsl["firstDoseDate"] == "2024-02-01"

    assert len(result.adae_records) == 2
    adae1 = result.adae_records[0].model_dump(by_alias=True)
    assert adae1["aeTerm"] == "Nausea"
    assert adae1["treatmentEmergent"] is True

    adae2 = result.adae_records[1].model_dump(by_alias=True)
    assert adae2["aeTerm"] == "Headache"
    assert adae2["treatmentEmergent"] is False

    assert len(result.adlb_records) == 2
    adlb1 = result.adlb_records[0].model_dump(by_alias=True)
    assert adlb1["labTest"] == "ALT"
    assert adlb1["result"] == 40.0
    assert adlb1["changeFromBaseline"] is None

    adlb2 = result.adlb_records[1].model_dump(by_alias=True)
    assert adlb2["labTest"] == "ALT"
    assert adlb2["result"] == 55.0
    assert adlb2["changeFromBaseline"] == 15.0


def test_compilation_cache_reuses_compiled_business_logic() -> None:
    """Test that business logic compilation is cached across multiple evaluations."""
    _compile_business_logic.cache_clear()

    config = StudyConfiguration(
        study_key="STUDY-CACHE",
        mappings=[
            MappingRule(
                domain="AE",
                target_field="subjectKey",
                source_form_key="AE_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="AE",
                target_field="aeTerm",
                source_form_key="AE_FORM",
                source_variable_name="recordData.term",
                businessLogic="str(value).strip().upper()",
            ),
            MappingRule(
                domain="AE",
                target_field="aeSeverity",
                source_form_key="AE_FORM",
                source_variable_name="",
                fallback_value="MILD",
            ),
        ],
    )

    records = [
        Record(
            record_id=i,
            form_key="AE_FORM",
            subject_key=f"SUBJ-{i:03d}",
            date_created=datetime(2024, 1, 1),
            record_data={"term": f" symptom {i} "},
        )
        for i in range(1, 10)
    ]

    result = extract_canonical_records(records=records, study_configuration=config)
    assert len(result.adverse_events) == 9
    assert result.adverse_events[0].ae_term == "SYMPTOM 1"
    assert result.adverse_events[8].ae_term == "SYMPTOM 9"

    cache_info = _compile_business_logic.cache_info()
    assert cache_info.currsize >= 1
    # First record compiles (miss), next 8 records hit the LRU cache
    assert cache_info.hits >= 8


def test_uncompilable_business_logic_logs_warning_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that invalid business logic syntax logs a warning and returns None without raising."""
    context = SubjectContext("SUBJ-ERR")
    record = Record(
        record_id=1,
        form_key="AE_FORM",
        subject_key="SUBJ-ERR",
        date_created=datetime(2024, 1, 1),
        record_data={"term": "Rash"},
    )

    with caplog.at_level(logging.WARNING):
        result = _evaluate_business_logic(
            logic="invalid syntax +++ (((",
            record=record,
            payload={},
            context=context,
            value="Rash",
        )

    assert result is None
    assert any(
        "Derivation logic failed for SUBJ-ERR" in record.message for record in caplog.records
    )


def test_record_model_dump_not_invoked_during_standard_extraction() -> None:
    """Test that model_dump is not called in the inner extraction loop for standard mappings."""
    config = StudyConfiguration(
        study_key="STUDY-NO-DUMP",
        mappings=[
            MappingRule(
                domain="AE",
                target_field="subjectKey",
                source_form_key="AE_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="AE",
                target_field="aeTerm",
                source_form_key="AE_FORM",
                source_variable_name="recordData.term",
            ),
            MappingRule(
                domain="AE",
                target_field="aeSeverity",
                source_form_key="AE_FORM",
                source_variable_name="",
                fallback_value="MILD",
            ),
        ],
    )

    records = [
        Record(
            record_id=101,
            form_key="AE_FORM",
            subject_key="SUBJ-101",
            date_created=datetime(2024, 1, 1),
            record_data={"term": "Fatigue"},
        ),
        Record(
            record_id=102,
            form_key="AE_FORM",
            subject_key="SUBJ-101",
            date_created=datetime(2024, 1, 2),
            record_data={"term": "Fever"},
        ),
    ]

    with patch.object(Record, "model_dump", wraps=Record.model_dump) as mock_dump:
        result = extract_canonical_records(records=records, study_configuration=config)
        assert len(result.adverse_events) == 2
        # Normal extraction loop must not call model_dump on the raw records
        assert mock_dump.call_count == 0


def test_top_level_alias_resolution_and_fallback() -> None:
    """Test that top-level aliases are resolved directly from Record and fallback operates correctly."""
    record = Record(
        record_id=99,
        form_key="AE_FORM",
        subject_key="SUBJ-999",
        date_created=datetime(2024, 5, 10, 12, 0, 0),
        record_data={"unprefixed_key": "raw_val"},
    )

    # 1. Alias directly on Record (not in record_data)
    rule_subject = MappingRule(
        domain="AE",
        target_field="subjectKey",
        source_form_key="AE_FORM",
        source_variable_name="subjectKey",
    )
    val_subject = _extract_rule_value_from_payload(record, rule_subject)
    assert val_subject == "SUBJ-999"

    # 2. Snake case field on Record
    rule_rec_id = MappingRule(
        domain="AE",
        target_field="subjectKey",
        source_form_key="AE_FORM",
        source_variable_name="record_id",
    )
    val_rec_id = _extract_rule_value_from_payload(record, rule_rec_id)
    assert val_rec_id == 99

    # 3. CamelCase alias recordId
    rule_rec_id_alias = MappingRule(
        domain="AE",
        target_field="subjectKey",
        source_form_key="AE_FORM",
        source_variable_name="recordId",
    )
    val_rec_id_alias = _extract_rule_value_from_payload(record, rule_rec_id_alias)
    assert val_rec_id_alias == 99

    # 4. Unprefixed key in record_data
    rule_raw = MappingRule(
        domain="AE",
        target_field="aeTerm",
        source_form_key="AE_FORM",
        source_variable_name="unprefixed_key",
    )
    val_raw = _extract_rule_value_from_payload(record, rule_raw)
    assert val_raw == "raw_val"

    # 5. Non-existent path returns None
    rule_missing = MappingRule(
        domain="AE",
        target_field="aeTerm",
        source_form_key="AE_FORM",
        source_variable_name="nonexistent.field",
    )
    val_missing = _extract_rule_value_from_payload(record, rule_missing)
    assert val_missing is None


def test_lazy_record_payload_initializes_on_demand() -> None:
    """Test that _LazyRecordPayload populates only when dictionary methods are accessed."""
    record = Record(
        record_id=42,
        form_key="TEST_FORM",
        subject_key="SUBJ-42",
        date_created=datetime(2024, 1, 1),
        record_data={"foo": "bar"},
    )
    lazy = _LazyRecordPayload(record)
    assert not lazy._populated
    assert len(lazy) > 0  # Triggers __len__ -> _ensure_populated
    assert lazy._populated
    assert lazy["subjectKey"] == "SUBJ-42"
    assert lazy["subject_key"] == "SUBJ-42"
    assert lazy.get("recordId") == 42
    assert "formKey" in lazy
    assert list(lazy.keys())
    assert list(lazy.values())
    assert list(lazy.items())
    assert iter(lazy)


def test_extraction_engine_rejects_class_traversal_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that malicious class traversal payload in extraction engine fails safely."""
    config = StudyConfiguration(
        study_key="STUDY-EXPLOIT",
        mappings=[
            MappingRule(
                domain="AE",
                target_field="subjectKey",
                source_form_key="AE_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="AE",
                target_field="aeTerm",
                source_form_key="AE_FORM",
                source_variable_name="recordData.term",
                businessLogic="value.__class__.__bases__[0].__subclasses__()",
            ),
            MappingRule(
                domain="AE",
                target_field="aeSeverity",
                source_form_key="AE_FORM",
                source_variable_name="",
                fallback_value="MILD",
            ),
        ],
    )

    record = Record(
        record_id=1,
        form_key="AE_FORM",
        subject_key="SUBJ-001",
        date_created=datetime(2024, 1, 1),
        record_data={"term": "Headache"},
    )

    with caplog.at_level(logging.WARNING):
        result = extract_canonical_records(records=[record], study_configuration=config)

    assert len(result.adverse_events) == 1
    # The derived business logic failed safely, keeping the initial extracted value "Headache"
    assert result.adverse_events[0].ae_term == "Headache"
    assert any("Derivation logic failed for SUBJ-001" in r.message for r in caplog.records)


def test_extraction_engine_rejects_mutator_methods_and_defends_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that expressions invoking mutating methods fail safely and cannot mutate state/payload."""
    context = SubjectContext("SUBJ-001")
    context.state["baseline_score"] = 50
    record = Record(
        record_id=1,
        form_key="AE_FORM",
        subject_key="SUBJ-001",
        date_created=datetime(2024, 1, 1),
        record_data={"term": "Fever"},
    )
    payload = {"aeTerm": "Fever", "site": "SiteA"}

    # Attempt payload.clear()
    with caplog.at_level(logging.WARNING):
        res = _evaluate_business_logic("payload.clear()", record, payload, context, value="Fever")

    assert res is None
    assert payload == {"aeTerm": "Fever", "site": "SiteA"}
    assert context.state["baseline_score"] == 50
    assert any("Derivation logic failed for SUBJ-001" in r.message for r in caplog.records)


def test_extract_canonical_records_blocks_payload_clear(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that malicious mapping business logic cannot clear payload or corrupt extraction."""
    config = StudyConfiguration(
        study_key="STUDY-CLEAR",
        mappings=[
            MappingRule(
                domain="AE",
                target_field="subjectKey",
                source_form_key="AE_FORM",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="AE",
                target_field="aeTerm",
                source_form_key="AE_FORM",
                source_variable_name="recordData.term",
                business_logic="payload.clear()",
            ),
            MappingRule(
                domain="AE",
                target_field="aeSeverity",
                source_form_key="AE_FORM",
                source_variable_name="",
                fallback_value="MILD",
            ),
        ],
    )

    record = Record(
        record_id=1,
        form_key="AE_FORM",
        subject_key="SUBJ-001",
        date_created=datetime(2024, 1, 1),
        record_data={"term": "Headache"},
    )

    with caplog.at_level(logging.WARNING):
        result = extract_canonical_records(records=[record], study_configuration=config)

    assert len(result.adverse_events) == 1
    # Derived logic failed safely, keeping initial extracted value
    assert result.adverse_events[0].ae_term == "Headache"
    assert any("Derivation logic failed for SUBJ-001" in r.message for r in caplog.records)


def test_extract_canonical_records_supports_all_registered_clinical_domains() -> None:
    """Verify extract_canonical_records groups and populates DM, VS, CM, LB, EX, MH, DS, EG, PE, IE."""
    rule_specs = [
        ("DM", "DM_F", [("sex", "sex"), ("age", "age")]),
        ("VS", "VS_F", [("vsTest", "vsTest"), ("vsResult", "vsResult"), ("vsDate", "vsDate")]),
        ("CM", "CM_F", [("cmTrt", "cmTrt"), ("cmDose", "cmDose")]),
        ("LB", "LB_F", [("lbTest", "lbTest"), ("lbResult", "lbResult")]),
        ("EX", "EX_F", [("exTrt", "exTrt"), ("exDose", "exDose")]),
        ("MH", "MH_F", [("mhTerm", "mhTerm")]),
        ("DS", "DS_F", [("dsTerm", "dsTerm")]),
        ("EG", "EG_F", [("egTest", "egTest"), ("egResult", "egResult")]),
        ("PE", "PE_F", [("peTest", "peTest"), ("peResult", "peResult")]),
        (
            "IE",
            "IE_F",
            [
                ("ieTest", "ieTest"),
                ("ieTestCd", "ieTestCd"),
                ("ieCat", "ieCat"),
                ("ieResult", "ieResult"),
            ],
        ),
    ]
    mappings: list[MappingRule] = []
    for domain, form_key, fields in rule_specs:
        mappings.append(
            MappingRule(
                domain=domain,
                target_field="subjectKey",
                source_form_key=form_key,
                source_variable_name="subjectKey",
            )
        )
        for target_field, src_var in fields:
            mappings.append(
                MappingRule(
                    domain=domain,
                    target_field=target_field,
                    source_form_key=form_key,
                    source_variable_name=src_var,
                )
            )

    config = StudyConfiguration(study_key="STUDY-ALL", mappings=mappings)
    records = [
        Record(
            record_id=1,
            form_key="DM_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 1),
            record_data={"sex": "F", "age": 30},
        ),
        Record(
            record_id=2,
            form_key="VS_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 2),
            record_data={"vsTest": "Pulse", "vsResult": "72", "vsDate": "2025-01-02T08:00:00"},
        ),
        Record(
            record_id=3,
            form_key="CM_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 3),
            record_data={"cmTrt": "Paracetamol", "cmDose": "500"},
        ),
        Record(
            record_id=4,
            form_key="LB_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 4),
            record_data={"lbTest": "ALT", "lbResult": "24.5"},
        ),
        Record(
            record_id=5,
            form_key="EX_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 5),
            record_data={"exTrt": "Investigational Drug", "exDose": "100"},
        ),
        Record(
            record_id=6,
            form_key="MH_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 6),
            record_data={"mhTerm": "Seasonal allergies"},
        ),
        Record(
            record_id=7,
            form_key="DS_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 7),
            record_data={"dsTerm": "Completed"},
        ),
        Record(
            record_id=8,
            form_key="EG_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 8),
            record_data={"egTest": "PR Interval", "egResult": "160"},
        ),
        Record(
            record_id=9,
            form_key="PE_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 9),
            record_data={"peTest": "Abdomen", "peResult": "Soft, non-tender"},
        ),
        Record(
            record_id=10,
            form_key="IE_F",
            subject_key="S01",
            date_created=datetime(2025, 1, 10),
            record_data={
                "ieTest": "Prior therapy",
                "ieTestCd": "EXCL02",
                "ieCat": "EXCLUSION",
                "ieResult": "Y",
            },
        ),
    ]

    extracted = extract_canonical_records(records=records, study_configuration=config)
    assert extracted.validation_errors == []
    assert len(extracted.demographics) == 1 and extracted.demographics[0].sex == "F"
    assert len(extracted.vital_signs) == 1 and extracted.vital_signs[0].vs_result == 72.0
    assert (
        len(extracted.concomitant_medications) == 1
        and extracted.concomitant_medications[0].cm_trt == "Paracetamol"
    )
    assert (
        len(extracted.laboratory_results) == 1 and extracted.laboratory_results[0].lb_result == 24.5
    )
    assert len(extracted.exposures) == 1 and extracted.exposures[0].ex_dose == 100.0
    assert (
        len(extracted.medical_history) == 1
        and extracted.medical_history[0].mh_term == "Seasonal allergies"
    )
    assert len(extracted.disposition) == 1 and extracted.disposition[0].ds_term == "Completed"
    assert len(extracted.ecg_results) == 1 and extracted.ecg_results[0].eg_result == 160.0
    assert len(extracted.physical_exams) == 1 and extracted.physical_exams[0].pe_test == "Abdomen"
    assert (
        len(extracted.inclusion_exclusion) == 1
        and extracted.inclusion_exclusion[0].ie_test_cd == "EXCL02"
    )
