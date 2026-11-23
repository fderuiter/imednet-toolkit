"""Unit tests for SyntheticRecordGenerator."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from imednet_workflows.uat.generator import SyntheticRecordGenerator
from imednet_workflows.uat.inspector import StudySnapshot
from imednet_workflows.uat.models import (
    RecordTestType,
    UATFormSpec,
    UATSpecification,
    UATSubjectSpec,
    UATVariableSpec,
    VariableTestStrategy,
)


@pytest.fixture
def mock_snapshot():
    """Fixture for StudySnapshot mock."""
    snapshot = MagicMock(spec=StudySnapshot)
    snapshot.active_sites.return_value = [MagicMock(site_name="Test Site")]
    return snapshot


@pytest.fixture
def basic_spec():
    """Fixture for basic UATSpecification."""
    return UATSpecification(
        study_key="STUDY01",
        study_name="Test Study",
        subject_specs=[
            UATSubjectSpec(site_name="Test Site", subject_count=2, subject_key_prefix="SUBJ-")
        ],
        form_specs=[
            UATFormSpec(
                form_key="F_REG",
                form_name="Registration",
                form_type="Enrollment",
                test_type=RecordTestType.REGISTER_SUBJECT,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="SUBJID",
                        variable_key="V1",
                        variable_type="Text",
                        form_key="F_REG",
                        strategy=VariableTestStrategy.SYNTHETIC,
                        max_length=5,
                    )
                ],
            )
        ],
    )


def test_generate_registration(basic_spec, mock_snapshot):
    """Test subject registration generation."""
    generator = SyntheticRecordGenerator(seed=42)
    results = generator.generate(basic_spec, mock_snapshot)

    assert len(results) == 1
    res = results[0]
    assert res.form_key == "F_REG"
    assert len(res.payloads) == 1
    assert res.payloads[0]["formKey"] == "F_REG"
    assert res.payloads[0]["siteName"] == "Test Site"
    assert res.payloads[0]["keywords"] == [{"keywordName": "UAT", "keywordKey": "UAT"}]
    assert "SUBJID" in res.payloads[0]["data"]
    assert len(res.payloads[0]["data"]["SUBJID"]) == 5


def test_fixed_strategy(mock_snapshot):
    """Test FIXED strategy generation."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="VAR1",
                        variable_key="V1",
                        variable_type="Text",
                        form_key="F1",
                        strategy=VariableTestStrategy.FIXED,
                        fixed_value="FIXED_VAL",
                    )
                ],
            )
        ],
    )
    generator = SyntheticRecordGenerator()
    results = generator.generate(spec, mock_snapshot)
    assert results[0].payloads[0]["data"]["VAR1"] == "FIXED_VAL"


def test_skip_strategy(mock_snapshot):
    """Test SKIP strategy generation."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="VAR1",
                        variable_key="V1",
                        variable_type="Text",
                        form_key="F1",
                        strategy=VariableTestStrategy.SKIP,
                    )
                ],
            )
        ],
    )
    generator = SyntheticRecordGenerator()
    results = generator.generate(spec, mock_snapshot)
    assert "VAR1" not in results[0].payloads[0]["data"]


def test_boundary_strategy(mock_snapshot):
    """Test BOUNDARY strategy generation."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,  # Boundary generates 2 payloads regardless
                variables=[
                    UATVariableSpec(
                        variable_name="NUM",
                        variable_key="V1",
                        variable_type="Number",
                        form_key="F1",
                        strategy=VariableTestStrategy.BOUNDARY,
                        min_value=10,
                        max_value=20,
                    ),
                    UATVariableSpec(
                        variable_name="FLT",
                        variable_key="V2",
                        variable_type="Float",
                        form_key="F1",
                        strategy=VariableTestStrategy.BOUNDARY,
                        min_value=1.5,
                        max_value=9.5,
                    ),
                ],
            )
        ],
    )
    generator = SyntheticRecordGenerator()
    results = generator.generate(spec, mock_snapshot)
    assert len(results[0].payloads) == 2
    assert results[0].payloads[0]["data"]["NUM"] == 10
    assert isinstance(results[0].payloads[0]["data"]["NUM"], int)
    assert results[0].payloads[1]["data"]["NUM"] == 20
    assert isinstance(results[0].payloads[1]["data"]["NUM"], int)
    assert results[0].payloads[0]["data"]["FLT"] == pytest.approx(1.5)
    assert isinstance(results[0].payloads[0]["data"]["FLT"], float)
    assert results[0].payloads[1]["data"]["FLT"] == pytest.approx(9.5)
    assert isinstance(results[0].payloads[1]["data"]["FLT"], float)


def test_coded_all_strategy(mock_snapshot):
    """Test CODED_ALL strategy generation."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="CODED",
                        variable_key="V1",
                        variable_type="Coded",
                        form_key="F1",
                        strategy=VariableTestStrategy.CODED_ALL,
                        coded_values=["A", "B", "C"],
                    )
                ],
            )
        ],
    )
    generator = SyntheticRecordGenerator()
    results = generator.generate(spec, mock_snapshot)
    assert len(results[0].payloads) == 3
    assert results[0].payloads[0]["data"]["CODED"] == "A"
    assert results[0].payloads[1]["data"]["CODED"] == "B"
    assert results[0].payloads[2]["data"]["CODED"] == "C"


def test_coded_all_warning(mock_snapshot):
    """Test CODED_ALL warning path."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="C1",
                        variable_key="V1",
                        variable_type="Coded",
                        form_key="F1",
                        strategy=VariableTestStrategy.CODED_ALL,
                        coded_values=["A", "B"],
                    ),
                    UATVariableSpec(
                        variable_name="C2",
                        variable_key="V2",
                        variable_type="Coded",
                        form_key="F1",
                        strategy=VariableTestStrategy.CODED_ALL,
                        coded_values=["X", "Y"],
                    ),
                ],
            )
        ],
    )
    generator = SyntheticRecordGenerator()
    results = generator.generate(spec, mock_snapshot)
    assert len(results[0].warnings) > 0
    assert len(results[0].payloads) == 1  # Fallback to 1 subject


def test_seed_reproducibility(basic_spec, mock_snapshot):
    """Test reproducibility with random seed."""
    gen1 = SyntheticRecordGenerator(seed=123)
    res1 = gen1.generate(basic_spec, mock_snapshot)

    gen2 = SyntheticRecordGenerator(seed=123)
    res2 = gen2.generate(basic_spec, mock_snapshot)

    assert res1[0].payloads[0]["data"] == res2[0].payloads[0]["data"]


def test_various_types(mock_snapshot):
    """Test generation across various variable types."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        global_date_value=date(2023, 1, 1),
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="N", variable_key="V1", variable_type="Number", form_key="F1"
                    ),
                    UATVariableSpec(
                        variable_name="F", variable_key="V2", variable_type="Float", form_key="F1"
                    ),
                    UATVariableSpec(
                        variable_name="D", variable_key="V3", variable_type="Date", form_key="F1"
                    ),
                    UATVariableSpec(
                        variable_name="DT",
                        variable_key="V4",
                        variable_type="DateTime",
                        form_key="F1",
                    ),
                    UATVariableSpec(
                        variable_name="B", variable_key="V5", variable_type="Boolean", form_key="F1"
                    ),
                    UATVariableSpec(
                        variable_name="CB",
                        variable_key="V6",
                        variable_type="Checkbox",
                        form_key="F1",
                        coded_values=["X", "Y"],
                    ),
                    UATVariableSpec(
                        variable_name="TA",
                        variable_key="V7",
                        variable_type="TextArea",
                        form_key="F1",
                    ),
                ],
            )
        ],
    )
    generator = SyntheticRecordGenerator(seed=42)
    results = generator.generate(spec, mock_snapshot)
    data = results[0].payloads[0]["data"]

    assert isinstance(data["N"], int) and not isinstance(data["N"], bool)
    assert isinstance(data["F"], float)
    assert data["D"] == "2023-01-01"
    assert "T" in data["DT"]
    assert isinstance(data["B"], bool)
    assert any(c in data["CB"] for c in ("X", "Y"))
    assert len(data["TA"]) > 0


def test_subject_pool_logic(mock_snapshot):
    """Test subject pooling and wrap-around logic."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        subject_specs=[UATSubjectSpec(site_name="S1", subject_count=3, subject_key_prefix="TEST-")],
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.UPDATE_SCHEDULED_RECORD,
                subject_count=5,  # Should wrap around pool
                variables=[],
            )
        ],
    )
    generator = SyntheticRecordGenerator()
    results = generator.generate(spec, mock_snapshot)
    res = results[0]
    assert res.subject_keys == ["TEST-001", "TEST-002", "TEST-003", "TEST-001", "TEST-002"]
    assert res.payloads[0]["subjectKey"] == "TEST-001"
    assert res.payloads[4]["subjectKey"] == "TEST-002"


def test_unrecognized_type(mock_snapshot, caplog):
    """Test handling of unrecognized variable types."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="UNK",
                        variable_key="V1",
                        variable_type="UNKNOWN",
                        form_key="F1",
                    ),
                    UATVariableSpec(
                        variable_name="SIG",
                        variable_key="V2",
                        variable_type="Signature",
                        form_key="F1",
                    ),
                ],
            )
        ],
    )
    generator = SyntheticRecordGenerator()
    results = generator.generate(spec, mock_snapshot)
    assert "UNK" in results[0].payloads[0]["data"]
    assert results[0].payloads[0]["data"]["SIG"] == ""


def test_no_subjects_pool_default(mock_snapshot):
    """Test default subject pool when none provided."""
    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        subject_specs=[],
        form_specs=[
            UATFormSpec(
                form_key="F1",
                form_name="F1",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[],
            )
        ],
    )
    generator = SyntheticRecordGenerator()
    results = generator.generate(spec, mock_snapshot)
    assert results[0].subject_keys == ["UAT-TEST-001"]


def test_canonical_variable_types_and_datadictionary_enrichment(mock_snapshot):
    """SyntheticRecordGenerator emits valid values for Coded, Radio, Single Select, Checkbox, Multi Select, etc."""
    from imednet.datadict import Choice, DataDictionary
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVariable

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="F_CANON",
                variables=[
                    DDVariable(
                        variableName="CODED_V",
                        variableType="Coded",
                        choices=[Choice(choiceText="One", choiceValue="C1")],
                    ),
                    DDVariable(
                        variableName="RADIO_V",
                        variableType="Radio",
                        choices=[
                            Choice(choiceText="Alpha", choiceValue="A"),
                            Choice(choiceText="Beta", choiceValue="B"),
                        ],
                    ),
                    DDVariable(
                        variableName="SINGLE_V",
                        variableType="Single Select",
                        choices=[Choice(choiceText="Option 1", choiceValue="OPT1")],
                    ),
                    DDVariable(
                        variableName="CHECK_V",
                        variableType="Checkbox",
                        choices=[
                            Choice(choiceText="K1", choiceValue="K1"),
                            Choice(choiceText="K2", choiceValue="K2"),
                        ],
                    ),
                    DDVariable(
                        variableName="MULTI_V",
                        variableType="Multi Select",
                        choices=[
                            Choice(choiceText="M1", choiceValue="M1"),
                            Choice(choiceText="M2", choiceValue="M2"),
                        ],
                    ),
                ],
            )
        ]
    )

    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        global_date_value=date(2026, 2, 15),
        form_specs=[
            UATFormSpec(
                form_key="F_CANON",
                form_name="Canonical",
                form_type="Standard",
                test_type=RecordTestType.CREATE_NEW_RECORD,
                subject_count=1,
                variables=[
                    UATVariableSpec(
                        variable_name="CODED_V",
                        variable_key="V1",
                        variable_type="Coded",
                        form_key="F_CANON",
                    ),
                    UATVariableSpec(
                        variable_name="RADIO_V",
                        variable_key="V2",
                        variable_type="Radio",
                        form_key="F_CANON",
                    ),
                    UATVariableSpec(
                        variable_name="SINGLE_V",
                        variable_key="V3",
                        variable_type="Single Select",
                        form_key="F_CANON",
                    ),
                    UATVariableSpec(
                        variable_name="CHECK_V",
                        variable_key="V4",
                        variable_type="Checkbox",
                        form_key="F_CANON",
                    ),
                    UATVariableSpec(
                        variable_name="MULTI_V",
                        variable_key="V5",
                        variable_type="Multi Select",
                        form_key="F_CANON",
                    ),
                    UATVariableSpec(
                        variable_name="DTP_V",
                        variable_key="V6",
                        variable_type="Date/Time Precision",
                        form_key="F_CANON",
                    ),
                    UATVariableSpec(
                        variable_name="CALC_V",
                        variable_key="V7",
                        variable_type="Calculation",
                        form_key="F_CANON",
                        min_value=10,
                        max_value=20,
                    ),
                    UATVariableSpec(
                        variable_name="YESNO_V",
                        variable_key="V8",
                        variable_type="Yes/No",
                        form_key="F_CANON",
                    ),
                ],
            )
        ],
    )

    generator = SyntheticRecordGenerator(seed=99, data_dictionary=dd)
    results = generator.generate(spec, mock_snapshot)
    data = results[0].payloads[0]["data"]

    assert data["CODED_V"] == "C1"
    assert data["RADIO_V"] in {"A", "B"}
    assert data["SINGLE_V"] == "OPT1"
    assert set(data["CHECK_V"].split("`")).issubset({"K1", "K2"})
    assert set(data["MULTI_V"].split("`")).issubset({"M1", "M2"})
    assert data["DTP_V"] == "2026-02-15"
    assert isinstance(data["CALC_V"], int)
    assert 10 <= data["CALC_V"] <= 20
    assert data["YESNO_V"] in {"Yes", "No"}


def test_generator_enriches_odm_range_checks_and_iso_temporal_bounds(
    mock_snapshot,
    caplog,
) -> None:
    """SYNTHETIC and BOUNDARY UAT payloads honor ODM Hard/Soft RangeChecks and ISO date/time limits."""
    import logging

    from imednet.datadict import load_from_cdisc_odm
    from imednet.spi.validation import RangeCheckConstraint, VariableConstraints
    from imednet.validation import validate_record_data, validate_record_entry

    assert RangeCheckConstraint is not None
    assert VariableConstraints is not None

    odm_xml = """<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" ODMVersion="1.3.2" FileType="Snapshot" FileOID="F.UAT" CreationDateTime="2026-01-01T00:00:00Z">
  <Study OID="S.UAT">
    <GlobalVariables>
      <StudyName>UAT</StudyName>
      <StudyDescription>UAT</StudyDescription>
      <ProtocolName>UAT-1</ProtocolName>
    </GlobalVariables>
    <MetaDataVersion OID="MDV.1" Name="v1">
      <FormDef OID="VS" Name="Vital Signs" Repeating="No">
        <ItemGroupRef ItemGroupOID="IG.VS" Mandatory="Yes"/>
      </FormDef>
      <ItemGroupDef OID="IG.VS" Name="Vitals" Repeating="No">
        <ItemRef ItemOID="IT.VS.SYSBP" Mandatory="Yes"/>
        <ItemRef ItemOID="IT.VS.TEMP" Mandatory="Yes"/>
        <ItemRef ItemOID="IT.VS.ARM" Mandatory="Yes"/>
        <ItemRef ItemOID="IT.VS.VISDAT" Mandatory="Yes"/>
        <ItemRef ItemOID="IT.VS.VISTIM" Mandatory="Yes"/>
      </ItemGroupDef>
      <ItemDef OID="IT.VS.SYSBP" Name="SYSBP" DataType="integer">
        <RangeCheck Comparator="GT" SoftHard="Hard"><CheckValue>80</CheckValue></RangeCheck>
        <RangeCheck Comparator="LT" SoftHard="Hard"><CheckValue>180</CheckValue></RangeCheck>
      </ItemDef>
      <ItemDef OID="IT.VS.TEMP" Name="TEMP" DataType="float" SignificantDigits="1">
        <RangeCheck Comparator="GE" SoftHard="Soft"><CheckValue>36.0</CheckValue></RangeCheck>
        <RangeCheck Comparator="LE" SoftHard="Soft"><CheckValue>37.5</CheckValue></RangeCheck>
      </ItemDef>
      <ItemDef OID="IT.VS.ARM" Name="ARM" DataType="text">
        <RangeCheck Comparator="IN" SoftHard="Hard">
          <CheckValue>COHORT_A</CheckValue>
          <CheckValue>COHORT_B</CheckValue>
        </RangeCheck>
      </ItemDef>
      <ItemDef OID="IT.VS.VISDAT" Name="VISDAT" DataType="date">
        <RangeCheck Comparator="GE" SoftHard="Hard"><CheckValue>2025-03-01</CheckValue></RangeCheck>
        <RangeCheck Comparator="LE" SoftHard="Hard"><CheckValue>2025-03-31</CheckValue></RangeCheck>
      </ItemDef>
      <ItemDef OID="IT.VS.VISTIM" Name="VISTIM" DataType="time">
        <RangeCheck Comparator="GE" SoftHard="Hard"><CheckValue>08:00:00</CheckValue></RangeCheck>
        <RangeCheck Comparator="LE" SoftHard="Hard"><CheckValue>17:00:00</CheckValue></RangeCheck>
      </ItemDef>
    </MetaDataVersion>
  </Study>
</ODM>"""
    dd = load_from_cdisc_odm(odm_xml, strict=True)

    def _make_spec(strategy: VariableTestStrategy) -> UATSpecification:
        return UATSpecification(
            study_key="S.UAT",
            study_name="UAT",
            form_specs=[
                UATFormSpec(
                    form_key="VS",
                    form_name="Vital Signs",
                    form_type="Subject",
                    test_type=RecordTestType.CREATE_NEW_RECORD,
                    subject_count=3,
                    variables=[
                        UATVariableSpec(
                            variable_name="SYSBP",
                            variable_key="V1",
                            variable_type="Integer",
                            form_key="VS",
                            strategy=strategy,
                        ),
                        UATVariableSpec(
                            variable_name="TEMP",
                            variable_key="V2",
                            variable_type="Float",
                            form_key="VS",
                            strategy=strategy,
                        ),
                        UATVariableSpec(
                            variable_name="ARM",
                            variable_key="V3",
                            variable_type="Text",
                            form_key="VS",
                            strategy=VariableTestStrategy.SYNTHETIC,
                        ),
                        UATVariableSpec(
                            variable_name="VISDAT",
                            variable_key="V4",
                            variable_type="Date",
                            form_key="VS",
                            strategy=strategy,
                        ),
                        UATVariableSpec(
                            variable_name="VISTIM",
                            variable_key="V5",
                            variable_type="Time",
                            form_key="VS",
                            strategy=strategy,
                        ),
                    ],
                )
            ],
        )

    generator = SyntheticRecordGenerator(seed=42, data_dictionary=dd)

    # 1. SYNTHETIC payloads pass preflight with zero errors and zero Soft warnings
    synth_sets = generator.generate(_make_spec(VariableTestStrategy.SYNTHETIC), mock_snapshot)
    assert len(synth_sets[0].payloads) == 3
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        for payload in synth_sets[0].payloads:
            validate_record_data(dd, "VS", payload["data"])
            validate_record_entry(dd, payload)
            assert isinstance(payload["data"]["SYSBP"], int)
            assert isinstance(payload["data"]["TEMP"], float)
            assert payload["data"]["ARM"] in {"COHORT_A", "COHORT_B"}
            assert "2025-03-01" <= payload["data"]["VISDAT"] <= "2025-03-31"
            assert "08:00:00" <= payload["data"]["VISTIM"] <= "17:00:00"
        assert caplog.records == []

    # 2. BOUNDARY payloads step inside strict GT/LT bounds and match inclusive date/time bounds
    bound_sets = generator.generate(_make_spec(VariableTestStrategy.BOUNDARY), mock_snapshot)
    assert len(bound_sets[0].payloads) == 2
    min_data = bound_sets[0].payloads[0]["data"]
    max_data = bound_sets[0].payloads[1]["data"]

    assert min_data["SYSBP"] == 81
    assert max_data["SYSBP"] == 179
    assert min_data["TEMP"] == pytest.approx(36.0)
    assert max_data["TEMP"] == pytest.approx(37.5)
    assert min_data["VISDAT"] == "2025-03-01"
    assert max_data["VISDAT"] == "2025-03-31"
    assert min_data["VISTIM"] == "08:00:00"
    assert max_data["VISTIM"] == "17:00:00"

    with caplog.at_level(logging.WARNING):
        caplog.clear()
        for payload in bound_sets[0].payloads:
            validate_record_data(dd, "VS", payload["data"])
            validate_record_entry(dd, payload)
        assert caplog.records == []

    # 3. UATSpecificationBuilder also populates enriched RangeCheck/ISO bounds directly
    from imednet.models.forms import Form as SnapForm
    from imednet.models.sites import Site as SnapSite
    from imednet.models.variables import Variable as SnapVar
    from imednet_workflows.uat.inspector import StudySnapshot
    from imednet_workflows.uat.orchestrator import UATSpecificationBuilder

    snap = StudySnapshot(
        study_key="STUDY",
        forms=[SnapForm(form_key="VS", form_name="Vital Signs", form_type="Standard")],
        variables=[
            SnapVar(variable_name="SYSBP", variable_type="Integer", form_key="VS"),
            SnapVar(variable_name="TEMP", variable_type="Float", form_key="VS"),
            SnapVar(variable_name="ARM", variable_type="Text", form_key="VS"),
            SnapVar(variable_name="VISDAT", variable_type="Date", form_key="VS"),
        ],
        variables_by_form={
            "VS": [
                SnapVar(variable_name="SYSBP", variable_type="Integer", form_key="VS"),
                SnapVar(variable_name="TEMP", variable_type="Float", form_key="VS"),
                SnapVar(variable_name="ARM", variable_type="Text", form_key="VS"),
                SnapVar(variable_name="VISDAT", variable_type="Date", form_key="VS"),
            ]
        },
        intervals=[],
        sites=[SnapSite(site_name="SITE-1", site_enrollment_status="Enrollment Open")],
    )
    built_spec = UATSpecificationBuilder(data_dictionary=dd).build(snap, subject_count=2)
    vs_vars = {v.variable_name: v for v in built_spec.form_specs[0].variables}
    assert vs_vars["SYSBP"].min_value == pytest.approx(80.0)
    assert vs_vars["SYSBP"].max_value == pytest.approx(180.0)
    assert vs_vars["SYSBP"].min_exclusive is True
    assert vs_vars["SYSBP"].max_exclusive is True
    assert vs_vars["TEMP"].min_value == pytest.approx(36.0)
    assert vs_vars["TEMP"].max_value == pytest.approx(37.5)
    assert vs_vars["ARM"].coded_values == ["COHORT_A", "COHORT_B"]
    assert vs_vars["VISDAT"].min_date == "2025-03-01"
    assert vs_vars["VISDAT"].max_date == "2025-03-31"


def test_dropdown_memo_keywords_and_schema_cache_and_datadict_validation(mock_snapshot):
    """Generated records attach keyword_tag and pass validate_record_entry on SchemaCache and DataDictionary."""
    from imednet.datadict import Choice, DataDictionary
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVariable
    from imednet.models.variables import Variable as APIVariable
    from imednet.validation import SchemaCache, validate_record_entry

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="F_REG",
                variables=[
                    DDVariable(
                        variableName="AGE",
                        variableType="Integer",
                        required="Yes",
                        extraAttributes={"minValue": 18, "maxValue": 65},
                    ),
                    DDVariable(
                        variableName="SCORE",
                        variableType="Integer",
                        required="Yes",
                    ),
                    DDVariable(
                        variableName="WEIGHT",
                        variableType="Float",
                        required="Yes",
                    ),
                    DDVariable(
                        variableName="ACTIVE",
                        variableType="Boolean",
                        required="Yes",
                    ),
                    DDVariable(
                        variableName="CONSENT",
                        variableType="Yes/No",
                        required="Yes",
                        choices=[
                            Choice(choiceText="Yes", choiceValue="Yes"),
                            Choice(choiceText="No", choiceValue="No"),
                        ],
                    ),
                    DDVariable(
                        variableName="COUNTRY",
                        variableType="Dropdown",
                        required="Yes",
                        choices=[
                            Choice(choiceText="Netherlands", choiceValue="NL"),
                            Choice(choiceText="Germany", choiceValue="DE"),
                        ],
                    ),
                    DDVariable(
                        variableName="NOTES",
                        variableType="Memo",
                        required="No",
                    ),
                ],
            )
        ]
    )

    cache = SchemaCache()
    cache.populate(
        [
            APIVariable(form_key="F_REG", variable_name="AGE", variable_type="Integer"),
            APIVariable(form_key="F_REG", variable_name="SCORE", variable_type="Number"),
            APIVariable(form_key="F_REG", variable_name="WEIGHT", variable_type="Float"),
            APIVariable(form_key="F_REG", variable_name="ACTIVE", variable_type="Boolean"),
            APIVariable(form_key="F_REG", variable_name="CONSENT", variable_type="Yes/No"),
            APIVariable(form_key="F_REG", variable_name="COUNTRY", variable_type="Dropdown"),
            APIVariable(form_key="F_REG", variable_name="NOTES", variable_type="Memo"),
        ]
    )

    spec = UATSpecification(
        study_key="S1",
        study_name="S1",
        subject_specs=[
            UATSubjectSpec(
                site_name="Test Site",
                subject_count=2,
                subject_key_prefix="UAT-TEST-",
                keyword_tag="UAT-PRELIVE",
            )
        ],
        form_specs=[
            UATFormSpec(
                form_key="F_REG",
                form_name="Registration",
                form_type="Subject",
                test_type=RecordTestType.REGISTER_SUBJECT,
                subject_count=2,
                variables=[
                    UATVariableSpec(
                        variable_name="AGE",
                        variable_key="V1",
                        variable_type="Integer",
                        form_key="F_REG",
                        strategy=VariableTestStrategy.BOUNDARY,
                        min_value=18.0,
                        max_value=65.0,
                    ),
                    UATVariableSpec(
                        variable_name="SCORE",
                        variable_key="V2",
                        variable_type="Number",
                        form_key="F_REG",
                        strategy=VariableTestStrategy.FIXED,
                        fixed_value="42.0",
                    ),
                    UATVariableSpec(
                        variable_name="WEIGHT",
                        variable_key="V3",
                        variable_type="Float",
                        form_key="F_REG",
                    ),
                    UATVariableSpec(
                        variable_name="ACTIVE",
                        variable_key="V4",
                        variable_type="Boolean",
                        form_key="F_REG",
                    ),
                    UATVariableSpec(
                        variable_name="CONSENT",
                        variable_key="V5",
                        variable_type="Yes/No",
                        form_key="F_REG",
                    ),
                    UATVariableSpec(
                        variable_name="COUNTRY",
                        variable_key="V6",
                        variable_type="Dropdown",
                        form_key="F_REG",
                    ),
                    UATVariableSpec(
                        variable_name="NOTES",
                        variable_key="V7",
                        variable_type="Memo",
                        form_key="F_REG",
                        max_length=24,
                    ),
                ],
            )
        ],
    )

    generator = SyntheticRecordGenerator(seed=17, data_dictionary=dd)
    record_sets = generator.generate(spec, mock_snapshot)
    assert len(record_sets) == 1
    assert len(record_sets[0].payloads) == 2

    for idx, payload in enumerate(record_sets[0].payloads):
        assert payload["keywords"] == [{"keywordName": "UAT-PRELIVE", "keywordKey": "UAT-PRELIVE"}]
        data = payload["data"]
        assert data["AGE"] == (18 if idx == 0 else 65)
        assert isinstance(data["AGE"], int)
        assert data["SCORE"] == 42
        assert isinstance(data["SCORE"], int)
        assert isinstance(data["WEIGHT"], float)
        assert isinstance(data["ACTIVE"], bool)
        assert data["CONSENT"] in {"Yes", "No"}
        assert data["COUNTRY"] in {"NL", "DE"}
        assert isinstance(data["NOTES"], str) and 1 <= len(data["NOTES"]) <= 24
        validate_record_entry(cache, payload)
        validate_record_entry(dd, payload)
