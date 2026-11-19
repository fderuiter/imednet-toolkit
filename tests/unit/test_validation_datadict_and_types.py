"""Unit and integration tests for unified DataDictionary & SchemaCache validation."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.datadict import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    load_from_redcap,
    parse_logic_xml,
)
from imednet.datadict import (
    Form as DDForm,
)
from imednet.datadict import (
    Variable as DDVariable,
)
from imednet.errors import UnknownVariableTypeError, ValidationError
from imednet.models.variables import Variable as ApiVariable
from imednet.spi.validation import ValidationSchemaProtocol as SpiValidationSchemaProtocol
from imednet.validation import (
    AsyncSchemaCache,
    AsyncSchemaValidator,
    SchemaCache,
    SchemaValidator,
    ValidationSchemaProtocol,
    validate_record_data,
    validate_record_entry,
)
from imednet.validation.cache import calculate_readiness_score


def _sample_data_dictionary() -> DataDictionary:
    return DataDictionary(
        forms=[
            DDForm(
                formKey="F_DM",
                formName="Demographics",
                formId=301,
                formType="Subject",
                variables=[
                    DDVariable(
                        variableName="SUBJID",
                        variableType="Text",
                        required="Yes",
                        extraAttributes={"minLength": 3, "maxLength": 8},
                    ),
                    DDVariable(
                        variableName="AGE",
                        variableType="Integer",
                        required="Yes",
                        extraAttributes={"minValue": 18, "maxValue": 120},
                    ),
                    DDVariable(
                        variableName="SEX",
                        variableType="Radio",
                        required="Yes",
                        choices=[
                            Choice(choiceText="Male", choiceValue="M"),
                            Choice(choiceText="Female", choiceValue="F"),
                        ],
                    ),
                    DDVariable(
                        variableName="RACE",
                        variableType="Multi Select",
                        required="No",
                        choices=[
                            Choice(choiceText="Asian", choiceValue="ASIAN"),
                            Choice(choiceText="Black", choiceValue="BLACK"),
                            Choice(choiceText="White", choiceValue="WHITE"),
                        ],
                    ),
                    DDVariable(
                        variableName="PREG",
                        variableType="Single Select",
                        required="Auto Query",
                        choices=[
                            Choice(choiceText="Yes", choiceValue="Yes"),
                            Choice(choiceText="No", choiceValue="No"),
                        ],
                    ),
                    DDVariable(
                        variableName="BMI",
                        variableType="Float",
                        required="No",
                        extraAttributes={"minValue": 10.0, "maxValue": 80.0},
                    ),
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="BL_1",
                        name="Male Pregnancy Rule",
                        type="Form",
                        status="Active",
                        form="F_DM",
                        logic=parse_logic_xml(
                            "<rule><conditions>"
                            "<condition><variable>SEX</variable><operator>==</operator><value>M</value><true/></condition>"
                            "<condition><variable>PREG</variable><operator>==</operator><value>Yes</value><true/></condition>"
                            "</conditions>"
                            "<actions><action><type>error</type><message>Male subject cannot be pregnant</message></action></actions>"
                            "</rule>"
                        ),
                    ),
                    BusinessLogicRule(
                        id="BL_2",
                        name="High BMI Query",
                        type="Query",
                        status="Active",
                        form="F_DM",
                        logic=parse_logic_xml(
                            "<rule><conditions>"
                            "<condition><variable>BMI</variable><operator>&gt;</operator><value>45</value><true/></condition>"
                            "</conditions>"
                            "<actions><action><type>query</type><message>Confirm high BMI</message></action></actions>"
                            "</rule>"
                        ),
                    ),
                ],
            )
        ]
    )


def test_validation_schema_protocol_conformance() -> None:
    """BaseSchemaCache subclasses and DataDictionary satisfy ValidationSchemaProtocol."""
    assert ValidationSchemaProtocol is SpiValidationSchemaProtocol
    cache = SchemaCache()
    async_cache = AsyncSchemaCache()
    dd = _sample_data_dictionary()

    assert isinstance(cache, ValidationSchemaProtocol)
    assert isinstance(async_cache, ValidationSchemaProtocol)
    assert isinstance(dd, ValidationSchemaProtocol)
    assert dd.form_key_from_id(301) == "F_DM"
    assert dd.form_key_from_id("301") == "F_DM"
    assert dd.form_key_from_id(999) is None


@pytest.mark.parametrize(
    ("var_type", "valid_value", "invalid_value"),
    [
        ("Coded", "M", True),
        ("Coded", 1, 1.5),
        ("Coded", 0, ["M"]),
        ("Coded", "Female", {"code": "F"}),
        ("Radio", "OPT_A", ["OPT_A"]),
        ("Radio", 2, False),
        ("Dropdown", "1", 2.5),
        ("Dropdown", 3, {"opt": 3}),
        ("Single Select", "CHOICE_1", True),
        ("Single Select", 99, ["CHOICE_1"]),
        ("Yes/No", "Yes", True),
        ("Yes/No", 1, False),
        ("Yes/No", 0, 1.5),
        ("Checkbox", "A`B", 123),
        ("Checkbox", ["A", "B"], [1.5]),
        ("Checkbox", [1, 2], [True]),
        ("Checkbox", True, {"a": 1}),
        ("Checkbox", False, 3.14),
        ("Multi Select", "ASIAN`WHITE", 42),
        ("Multi Select", ["ASIAN", "WHITE"], [False]),
        ("Multi Select", [1, 2], {"race": 1}),
        ("Multi Select", True, 2.5),
        ("Memo", "Detailed clinical observation.", 42),
        ("TextArea", "Clinical narrative text.", 100),
        ("Notes", "Investigator follow-up note.", ["note"]),
        ("Date/Time Precision", "2026-03", 202603),
        ("Signature", "Dr. Smith", 1),
        ("File", "consent.pdf", ["consent.pdf"]),
        ("Descriptive", "Section instructions", True),
        ("Calculation", 42.5, True),
        ("Calculation", "42.5", False),
        ("Slider", 50, True),
        ("Slider", 75.5, False),
        ("Slider", "50", [50]),
    ],
)
def test_canonical_variable_type_validators(
    var_type: str,
    valid_value: object,
    invalid_value: object,
) -> None:
    """All canonical iMednet Variable types validate accepted values and reject bad types."""
    cache = SchemaCache()
    cache.populate(
        [
            ApiVariable(
                variable_name="V_TEST",
                variable_type=var_type,
                form_id=10,
                form_key="F_TEST",
            )
        ]
    )
    validate_record_data(cache, "F_TEST", {"V_TEST": valid_value})
    with pytest.raises(ValidationError):
        validate_record_data(cache, "F_TEST", {"V_TEST": invalid_value})


@pytest.mark.parametrize("var_type", ["Integer", "Number", "Float", "Decimal"])
@pytest.mark.parametrize("bool_val", [True, False])
def test_numeric_validators_reject_booleans(var_type: str, bool_val: bool) -> None:
    """validate_record_data rejects True and False on Integer/Number/Float/Decimal Variables."""
    cache = SchemaCache()
    cache.populate(
        [
            ApiVariable(
                variable_name="NUM_VAR",
                variable_type=var_type,
                form_id=1,
                form_key="F1",
            )
        ]
    )
    with pytest.raises(ValidationError):
        validate_record_data(cache, "F1", {"NUM_VAR": bool_val})


def test_cold_cache_form_id_refresh_and_unknown_form_id_sync_and_async() -> None:
    """SchemaValidator and AsyncSchemaValidator refresh cold caches for formId records."""
    var = ApiVariable(variable_name="V_AGE", variable_type="Number", form_id=301, form_key="F_DM")

    sync_sdk = MagicMock()
    sync_sdk.get_variables.return_value = [var]
    validator = SchemaValidator(sync_sdk)

    # Cold cache + formId triggers refresh and catches invalid bool value on numeric variable
    with pytest.raises(ValidationError, match="integer"):
        validator.validate_record("STUDY", {"formId": 301, "data": {"V_AGE": True}})
    sync_sdk.get_variables.assert_called_once_with(study_key="STUDY")

    # Unknown formId raises ValidationError
    with pytest.raises(ValidationError, match="Unknown form ID 999"):
        validator.validate_record("STUDY", {"formId": 999, "data": {"V_AGE": 30}})

    # validate_record_entry also rejects unknown formId directly
    with pytest.raises(ValidationError, match="Unknown form ID 999"):
        validate_record_entry(validator.schema, {"formId": 999, "data": {"V_AGE": 30}})


@pytest.mark.asyncio
async def test_async_cold_cache_form_id_refresh_and_unknown_form_id() -> None:
    """AsyncSchemaValidator refreshes cold caches on formId payloads and rejects unknown formId."""
    var = ApiVariable(variable_name="V_AGE", variable_type="Number", form_id=301, form_key="F_DM")
    async_sdk = MagicMock()
    async_sdk.async_get_variables = AsyncMock(return_value=[var])

    validator = AsyncSchemaValidator(async_sdk)
    await validator.validate_record("STUDY", {"formId": 301, "data": {"V_AGE": 40}})
    async_sdk.async_get_variables.assert_awaited_once_with(study_key="STUDY")

    with pytest.raises(ValidationError, match="Unknown form ID 404"):
        await validator.validate_record("STUDY", {"formId": 404, "data": {"V_AGE": 40}})


def test_simulation_mode_f_dm_coded_submission_and_cold_form_id() -> None:
    """ImednetSDK simulation mode validates F_DM Coded V_GENDER and cold formId=301."""
    sdk = ImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        simulation_mode=True,
    )
    try:
        cache_by_key = SchemaCache()
        cache_by_key.refresh(sdk.forms, sdk.variables, "STUDY")
        job1 = sdk.records.create(
            "STUDY",
            [
                {
                    "formKey": "F_DM",
                    "subjectKey": "SUBJ-001",
                    "data": {"V_AGE": 34, "V_GENDER": "F"},
                }
            ],
            schema=cache_by_key,
        )
        assert job1.batch_id

        # Cold validator using formId=301 from simulation/state.py
        validator = SchemaValidator(sdk)
        validator.validate_record(
            "STUDY",
            {"formId": 301, "subjectKey": "SUBJ-001", "data": {"V_AGE": 35, "V_GENDER": "M"}},
        )
        with pytest.raises(ValidationError, match="Unknown form ID 9999"):
            validator.validate_record(
                "STUDY",
                {"formId": 9999, "subjectKey": "SUBJ-001", "data": {"V_AGE": 35}},
            )
    finally:
        sdk.close()


def test_datadictionary_choices_bounds_lengths_required_and_xml_rules(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DataDictionary enforces choices, numeric bounds, text lengths, Yes vs Auto Query, and XML rules."""
    dd = _sample_data_dictionary()

    # Valid record (omitting PREG triggers Auto Query warning, not ValidationError; BMI > 45 triggers Query warning)
    with caplog.at_level(logging.WARNING):
        validate_record_data(
            dd,
            "F_DM",
            {
                "SUBJID": "SUBJ01",
                "AGE": 35,
                "SEX": "F",
                "RACE": "ASIAN`WHITE",
                "BMI": 48.5,
            },
        )
    assert any("Auto Query" in rec.message for rec in caplog.records)
    assert any("High BMI Query" in rec.message for rec in caplog.records)

    # Missing Required="Yes" raises ValidationError
    with pytest.raises(ValidationError, match="Required variable 'SUBJID'"):
        validate_record_data(dd, "F_DM", {"AGE": 35, "SEX": "F"})

    # Invalid single-select Choice raises ValidationError
    with pytest.raises(ValidationError, match="Invalid choice 'X'"):
        validate_record_data(dd, "F_DM", {"SUBJID": "SUBJ01", "AGE": 35, "SEX": "X"})

    # Invalid multi-select Choice raises ValidationError
    with pytest.raises(ValidationError, match="Invalid choice"):
        validate_record_data(
            dd,
            "F_DM",
            {"SUBJID": "SUBJ01", "AGE": 35, "SEX": "F", "RACE": "ASIAN`UNKNOWN"},
        )

    # Numeric min/max bounds
    with pytest.raises(ValidationError, match="below minimum 18"):
        validate_record_data(dd, "F_DM", {"SUBJID": "SUBJ01", "AGE": 16, "SEX": "F"})
    with pytest.raises(ValidationError, match="exceeds maximum 120"):
        validate_record_data(dd, "F_DM", {"SUBJID": "SUBJ01", "AGE": 130, "SEX": "F"})

    # Text min/max lengths
    with pytest.raises(ValidationError, match="shorter than minimum length 3"):
        validate_record_data(dd, "F_DM", {"SUBJID": "AB", "AGE": 35, "SEX": "F"})
    with pytest.raises(ValidationError, match="exceeds maximum length 8"):
        validate_record_data(dd, "F_DM", {"SUBJID": "ABCDEFGHI", "AGE": 35, "SEX": "F"})

    # XML Form edit check violation raises ValidationError
    with pytest.raises(ValidationError, match="Male subject cannot be pregnant"):
        validate_record_data(
            dd,
            "F_DM",
            {"SUBJID": "SUBJ01", "AGE": 35, "SEX": "M", "PREG": "Yes"},
        )

    # Readiness score surfaces Auto Query & XML rule failures
    score, reasons = calculate_readiness_score(
        dd,
        "F_DM",
        {
            "SUBJID": "SUBJ01",
            "AGE": 35,
            "SEX": "M",
            "RACE": "ASIAN",
            "PREG": "Yes",
            "BMI": 22.0,
        },
    )
    assert score == pytest.approx(100.0)
    assert any("Male Pregnancy Rule" in r for r in reasons)


def test_redcap_loaded_datadictionary_enforces_both_min_and_max_bounds() -> None:
    """REDCap loader preserves both textValidationMin and textValidationMax for validation."""
    csv_text = (
        "Variable / Field Name,Form Name,Field Type,Field Label,"
        '"Choices, Calculations, or Slider Labels",Text Validation Type OR Show Slider Number,'
        "Text Validation Min,Text Validation Max,Required Field?\n"
        "height_cm,vitals,text,Height,,number,50,250,y\n"
    )
    dd = load_from_redcap(csv_text, study_key="STUDY")
    validate_record_data(dd, "vitals", {"height_cm": 175.0})

    with pytest.raises(ValidationError, match="below minimum 50"):
        validate_record_data(dd, "vitals", {"height_cm": 40.0})

    with pytest.raises(ValidationError, match="exceeds maximum 250"):
        validate_record_data(dd, "vitals", {"height_cm": 260.0})


@pytest.mark.asyncio
async def test_records_endpoint_accepts_datadictionary_sync_and_async() -> None:
    """RecordsEndpoint.create and AsyncRecordsEndpoint.create validate with DataDictionary."""
    dd = _sample_data_dictionary()

    # Sync SDK (simulation mode)
    sdk = ImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        simulation_mode=True,
    )
    try:
        with pytest.raises(ValidationError, match="Invalid choice"):
            sdk.records.create(
                "STUDY",
                [{"formKey": "F_DM", "data": {"SUBJID": "SUB01", "AGE": 30, "SEX": "BAD"}}],
                schema=dd,
            )

        with pytest.raises(ValidationError, match="Required variable 'AGE'"):
            sdk.records.create(
                "STUDY",
                [{"formKey": "F_DM", "data": {"SUBJID": "SUB01", "SEX": "F"}}],
                schema=dd,
            )

        with pytest.raises(ValidationError, match="Male subject cannot be pregnant"):
            sdk.records.create(
                "STUDY",
                [
                    {
                        "formId": 301,
                        "data": {"SUBJID": "SUB01", "AGE": 30, "SEX": "M", "PREG": "Yes"},
                    }
                ],
                schema=dd,
            )

        valid_job = sdk.records.create(
            "STUDY",
            [{"formId": 301, "data": {"SUBJID": "SUB01", "AGE": 30, "SEX": "F", "PREG": "No"}}],
            schema=dd,
        )
        assert valid_job.batch_id
    finally:
        sdk.close()

    # Async SDK (simulation mode)
    async with AsyncImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        simulation_mode=True,
    ) as async_sdk:
        with pytest.raises(ValidationError, match="Invalid choice"):
            await async_sdk.records.create(
                "STUDY",
                [{"formKey": "F_DM", "data": {"SUBJID": "SUB01", "AGE": 30, "SEX": "INVALID"}}],
                schema=dd,
            )

        with pytest.raises(ValidationError, match="Male subject cannot be pregnant"):
            await async_sdk.records.create(
                "STUDY",
                [
                    {
                        "formKey": "F_DM",
                        "data": {"SUBJID": "SUB01", "AGE": 30, "SEX": "M", "PREG": "Yes"},
                    }
                ],
                schema=dd,
            )

        async_job = await async_sdk.records.create(
            "STUDY",
            [{"formKey": "F_DM", "data": {"SUBJID": "SUB01", "AGE": 30, "SEX": "F"}}],
            schema=dd,
        )
        assert async_job.batch_id


def test_datadict_validation_explicit_or_group_rule() -> None:
    """validate_record_data enforces explicit <or> XML rules on both one-true, both-true, and neither-true payloads."""
    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="VS",
                formId="10",
                variables=[
                    DDVariable(variableName="SYS_BP", variableType="Integer"),
                    DDVariable(variableName="DIA_BP", variableType="Integer"),
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="VS-OR-1",
                        name="Critical BP Range",
                        type="Form",
                        status="Active",
                        form="VS",
                        logic=parse_logic_xml(
                            "<rule><conditions><or>"
                            "<condition><variable>SYS_BP</variable><operator>&gt;</operator><value>220</value></condition>"
                            "<condition><variable>DIA_BP</variable><operator>&lt;</operator><value>40</value></condition>"
                            "</or></conditions>"
                            "<actions><action><type>error</type><message>Critical BP</message></action></actions></rule>"
                        ),
                    )
                ],
            )
        ]
    )

    with pytest.raises(ValidationError, match="Critical BP"):
        validate_record_data(dd, "VS", {"SYS_BP": 230, "DIA_BP": 80})

    with pytest.raises(ValidationError, match="Critical BP"):
        validate_record_data(dd, "VS", {"SYS_BP": 120, "DIA_BP": 35})

    with pytest.raises(ValidationError, match="Critical BP"):
        validate_record_data(dd, "VS", {"SYS_BP": 230, "DIA_BP": 35})

    validate_record_data(dd, "VS", {"SYS_BP": 120, "DIA_BP": 80})


@pytest.mark.parametrize(
    ("raw_type", "valid_value"),
    [
        ("  single   select  ", "OPT_1"),
        ("SINGLE_SELECT", 1),
        ("single-select", "OPT_2"),
        ("SingleSelect", 2),
        ("  multi\tselect\n", ["A", 1]),
        ("MULTI_SELECT", True),
        ("Date / Time   Precision", "2026-03"),
        ("date_time_precision", "2026-03-15"),
        ("Yes / No", 1),
        ("YES_NO", "No"),
        ("Text   Area", "free text"),
        ("Drop   Down", "A"),
        ("Check   Box", (1, "B")),
        ("  MEMO  ", "clinical note"),
        ("  NOTES ", "visit note"),
        ("  SLIDER ", "75"),
    ],
)
def test_type_matching_normalizes_casing_and_whitespace_and_rejects_unknown(
    raw_type: str, valid_value: object
) -> None:
    """Type lookup normalizes casing/whitespace across multi-word types and rejects unknown names."""
    cache = SchemaCache()
    cache.populate(
        [
            ApiVariable(
                variable_name="V_NORM",
                variable_type=raw_type,
                form_id=11,
                form_key="F_NORM",
            )
        ]
    )
    validate_record_data(cache, "F_NORM", {"V_NORM": valid_value})
    validate_record_data(cache, "F_NORM", {"V_NORM": None})
    validate_record_data(cache, "F_NORM", {"V_NORM": ""})

    bad_cache = SchemaCache()
    bad_cache.populate(
        [
            ApiVariable(
                variable_name="V_BAD",
                variable_type="Unrecognized Clinical Widget",
                form_id=12,
                form_key="F_BAD",
            )
        ]
    )
    with pytest.raises(UnknownVariableTypeError, match="Unrecognized Clinical Widget"):
        validate_record_data(bad_cache, "F_BAD", {"V_BAD": "val"})
    with pytest.raises(UnknownVariableTypeError, match="Unrecognized Clinical Widget"):
        validate_record_data(bad_cache, "F_BAD", {"V_BAD": ""})


@pytest.mark.asyncio
async def test_seeded_simulation_state_f_dm_validates_sync_and_async() -> None:
    """Seeded SimulationState F_DM records validate cleanly in both sync and async clients."""
    from imednet.simulation.state import SimulationState

    state = SimulationState()
    seeded_f_dm = [r for r in state.records if r.get("formKey") == "F_DM"]
    assert seeded_f_dm

    with ImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        simulation_mode=True,
    ) as sync_sdk:
        sync_validator = SchemaValidator(sync_sdk)
        sync_validator.validate_batch("STUDY", seeded_f_dm)
        score, reasons = calculate_readiness_score(
            sync_validator.schema, "F_DM", seeded_f_dm[0]["recordData"]
        )
        assert score == pytest.approx(100.0)
        assert reasons == []

    async with AsyncImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        simulation_mode=True,
    ) as async_sdk:
        async_validator = AsyncSchemaValidator(async_sdk)
        await async_validator.validate_batch("STUDY", seeded_f_dm)
        async_score, async_reasons = calculate_readiness_score(
            async_validator.schema, "F_DM", seeded_f_dm[0]["recordData"]
        )
        assert async_score == pytest.approx(100.0)
        assert async_reasons == []
