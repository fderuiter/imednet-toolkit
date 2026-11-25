"""Unit and integration tests for unified DataDictionary & SchemaCache validation."""

from __future__ import annotations

import logging
from pathlib import Path
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
        ("Multi Select", "ASIAN, WHITE", True),
        ("Multi Select", ("A", "B"), False),
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
    with pytest.raises(ValidationError, match="numeric"):
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
        ("MULTI_SELECT", "A`B"),
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


@pytest.mark.asyncio
async def test_retired_deleted_and_disabled_required_variables_skipped_and_form_id_kept() -> None:
    """SchemaCache, AsyncSchemaCache, SchemaValidator, and AsyncSchemaValidator skip retired Variables."""
    from imednet.validation.cache import AsyncSchemaCache

    active_var = ApiVariable.model_validate(
        {
            "variableName": "ACTIVE_NOTES",
            "variableType": "Text",
            "formId": 401,
            "formKey": "F_MIXED",
            "deleted": False,
            "disabled": False,
        }
    )
    deleted_req = ApiVariable.model_validate(
        {
            "variableName": "DELETED_REQ",
            "variableType": "Text",
            "formId": 401,
            "formKey": "F_MIXED",
            "deleted": True,
            "disabled": False,
            "required": True,
        }
    )
    disabled_req = ApiVariable.model_validate(
        {
            "variableName": "DISABLED_REQ",
            "variableType": "Integer",
            "formId": 401,
            "formKey": "F_MIXED",
            "deleted": False,
            "disabled": True,
            "required": True,
        }
    )
    retired_only_form_var = ApiVariable.model_validate(
        {
            "variableName": "LEGACY_REQ",
            "variableType": "Text",
            "formId": 402,
            "formKey": "F_RETIRED_ONLY",
            "deleted": True,
            "required": True,
        }
    )
    all_vars = [active_var, deleted_req, disabled_req, retired_only_form_var]

    cache = SchemaCache()
    cache.populate(all_vars, study_key="STUDY_RET")
    assert list(cache.variables_for_form("F_MIXED", study_key="STUDY_RET").keys()) == [
        "ACTIVE_NOTES"
    ]
    assert cache.variables_for_form("F_RETIRED_ONLY", study_key="STUDY_RET") == {}
    assert cache.form_key_from_id(401, study_key="STUDY_RET") == "F_MIXED"
    assert cache.form_key_from_id(402, study_key="STUDY_RET") == "F_RETIRED_ONLY"

    validate_record_data(cache, "F_MIXED", {"ACTIVE_NOTES": "ok"}, study_key="STUDY_RET")
    validate_record_data(cache, "F_RETIRED_ONLY", {}, study_key="STUDY_RET")

    sync_sdk = MagicMock()
    sync_sdk.get_variables.return_value = all_vars
    sync_validator = SchemaValidator(sync_sdk)
    sync_validator.validate_record("STUDY_RET", {"formId": 401, "data": {"ACTIVE_NOTES": "ok"}})
    sync_validator.validate_record("STUDY_RET", {"formId": 402, "data": {}})

    async_cache = AsyncSchemaCache()
    async_cache.populate(all_vars, study_key="STUDY_RET")
    assert async_cache.form_key_from_id(402, study_key="STUDY_RET") == "F_RETIRED_ONLY"

    async_sdk = MagicMock()
    async_sdk.async_get_variables = AsyncMock(return_value=all_vars)
    async_validator = AsyncSchemaValidator(async_sdk)
    await async_validator.validate_record(
        "STUDY_RET", {"formId": 401, "data": {"ACTIVE_NOTES": "ok"}}
    )
    await async_validator.validate_record("STUDY_RET", {"formId": 402, "data": {}})


@pytest.mark.parametrize(
    "bad_num",
    [float("nan"), float("inf"), float("-inf"), "nan", "NaN", "inf", "-inf", "Infinity"],
)
@pytest.mark.parametrize("var_type", ["Float", "Decimal", "Calculation", "Slider"])
def test_non_finite_floats_rejected_in_type_and_bounds_validators(
    bad_num: object,
    var_type: str,
) -> None:
    """validate_record_data and _validate_numeric_bounds reject NaN, Inf, and -Inf."""
    from imednet.validation.cache import _validate_numeric_bounds

    with pytest.raises(ValidationError, match="finite"):
        _validate_numeric_bounds("LAB_VAL", "LB", bad_num, 0.0, 100.0)

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="LB",
                variables=[
                    DDVariable(
                        variableName="LAB_VAL",
                        variableType=var_type,
                        extraAttributes={"minValue": 0, "maxValue": 100},
                    )
                ],
            )
        ]
    )
    with pytest.raises(ValidationError):
        validate_record_data(dd, "LB", {"LAB_VAL": bad_num})


def test_temporal_validators_calendar_clock_and_phi_masking() -> None:
    """Date, DateTime, Date/Time Precision, and Time enforce calendar/clock bounds and mask PHI."""
    from imednet.utils.security import global_sensitivity_registry
    from imednet.utils.tabular import MASKED_SENTINEL

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="DM",
                variables=[
                    DDVariable(variableName="VISDAT", variableType="Date"),
                    DDVariable(variableName="DateOfBirth", variableType="Date"),
                    DDVariable(variableName="VSDTC", variableType="DateTime"),
                    DDVariable(variableName="ONSET_PREC", variableType="Date/Time Precision"),
                    DDVariable(variableName="VSTIM", variableType="Time"),
                ],
            )
        ]
    )

    validate_record_data(
        dd,
        "DM",
        {
            "VISDAT": "2024-02-29",
            "DateOfBirth": "02-29-2000",
            "VSDTC": "2026-03-15T14:30:00Z",
            "ONSET_PREC": "2026-03",
            "VSTIM": "23:59:59",
        },
    )

    for bad_date in ("2023-02-29", "2024-02-30", "2024-13-01", "02-30-2026", "not-a-date"):
        with pytest.raises(ValidationError):
            validate_record_data(dd, "DM", {"VISDAT": bad_date})

    for bad_dt in ("2023-02-29T10:00:00", "2024-05-01T25:00:00", "bad-datetime"):
        with pytest.raises(ValidationError):
            validate_record_data(dd, "DM", {"VSDTC": bad_dt})

    for bad_prec in ("2026-13", "2023-02-29", "2026-03-15T25", "bad-prec"):
        with pytest.raises(ValidationError):
            validate_record_data(dd, "DM", {"ONSET_PREC": bad_prec})

    for bad_time in ("24:00:00", "25:00:00", "12:60:00", "12:00:60", "noon"):
        with pytest.raises(ValidationError):
            validate_record_data(dd, "DM", {"VSTIM": bad_time})

    # PHI variable DateOfBirth must mask invalid temporal input in error messages
    raw_phi_dob = "1985-02-29"
    assert global_sensitivity_registry.is_sensitive("DateOfBirth")
    with pytest.raises(ValidationError) as exc_info:
        validate_record_data(dd, "DM", {"DateOfBirth": raw_phi_dob})
    assert MASKED_SENTINEL in str(exc_info.value)
    assert raw_phi_dob not in str(exc_info.value)


def test_multi_select_rejects_booleans_and_blank_delimiters_while_checkbox_keeps_bool() -> None:
    """Multi Select rejects bool and blank delimiters; Checkbox preserves bool support."""
    cache = SchemaCache()
    cache.populate(
        [
            ApiVariable(
                variable_name="MS_VAR",
                variable_type="Multi Select",
                form_id=10,
                form_key="F_CHOICE",
            ),
            ApiVariable(
                variable_name="CB_VAR",
                variable_type="Checkbox",
                form_id=10,
                form_key="F_CHOICE",
            ),
        ]
    )

    validate_record_data(cache, "F_CHOICE", {"CB_VAR": True, "MS_VAR": "A`B"})
    validate_record_data(cache, "F_CHOICE", {"CB_VAR": False, "MS_VAR": "A, B"})

    for bad_ms in (True, False, " , , ", " ` ` ", "   "):
        with pytest.raises(ValidationError):
            validate_record_data(cache, "F_CHOICE", {"MS_VAR": bad_ms})

    # Also verify DataDictionary Choice validation rejects bool/blank delimiters and masks PHI
    from imednet.utils.tabular import MASKED_SENTINEL

    dd_choice = DataDictionary(
        forms=[
            DDForm(
                formKey="F_CHOICE",
                variables=[
                    DDVariable(
                        variableName="MS_VAR",
                        variableType="Multi Select",
                        choices=[
                            Choice(choiceValue="A", choiceText="Alpha"),
                            Choice(choiceValue="B", choiceText="Beta"),
                        ],
                    ),
                    DDVariable(
                        variableName="DateOfBirth",
                        variableType="Multi Select",
                        choices=[Choice(choiceValue="1990", choiceText="1990")],
                    ),
                ],
            )
        ]
    )
    for bad_ms in (True, False, " , , ", " ` ` "):
        with pytest.raises(ValidationError):
            validate_record_data(dd_choice, "F_CHOICE", {"MS_VAR": bad_ms})
    with pytest.raises(ValidationError) as exc_info:
        validate_record_data(dd_choice, "F_CHOICE", {"DateOfBirth": "1985-04-12"})
    assert MASKED_SENTINEL in str(exc_info.value)
    assert "1985-04-12" not in str(exc_info.value)


def test_odm_range_checks_comparators_softhard_and_phi_masking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """extraAttributes['rangeChecks'] enforces all 8 ODM comparators, SoftHard, and PHI masking."""
    from imednet.datadict.logic import extract_variable_constraints
    from imednet.utils.tabular import MASKED_SENTINEL

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="VS",
                variables=[
                    DDVariable(
                        variableName="HR",
                        variableType="Integer",
                        extraAttributes={
                            "rangeChecks": [
                                {"comparator": "GT", "softHard": "Hard", "checkValue": "30"},
                                {"comparator": "LT", "softHard": "Hard", "checkValue": "220"},
                                {"comparator": "NE", "softHard": "Hard", "checkValue": "999"},
                                {
                                    "comparator": "LE",
                                    "softHard": "Soft",
                                    "checkValue": "100",
                                    "errorMessage": "Tachycardia warning",
                                },
                            ]
                        },
                    ),
                    DDVariable(
                        variableName="DOSE_COHORT",
                        variableType="Integer",
                        extraAttributes={
                            "rangeChecks": [
                                {
                                    "comparator": "IN",
                                    "softHard": "Hard",
                                    "checkValues": ["10", "20", "40"],
                                },
                            ]
                        },
                    ),
                    DDVariable(
                        variableName="VISIT_NUM",
                        variableType="Integer",
                        extraAttributes={
                            "rangeChecks": [
                                {
                                    "comparator": "NOTIN",
                                    "softHard": "Hard",
                                    "checkValues": ["13", "99"],
                                },
                                {"comparator": "GE", "softHard": "Hard", "checkValue": "1"},
                            ]
                        },
                    ),
                    DDVariable(
                        variableName="PROTO_VER",
                        variableType="Integer",
                        extraAttributes={
                            "rangeChecks": [
                                {"comparator": "EQ", "softHard": "Hard", "checkValue": "3"},
                            ]
                        },
                    ),
                    DDVariable(
                        variableName="DateOfBirth",
                        variableType="Date",
                        extraAttributes={
                            "rangeChecks": [
                                {
                                    "comparator": "GE",
                                    "softHard": "Hard",
                                    "checkValue": "1920-01-01",
                                }
                            ]
                        },
                    ),
                ],
            )
        ]
    )

    hr_constraints = extract_variable_constraints(dd.forms[0].variables[0])
    assert [rc.comparator for rc in hr_constraints.range_checks] == ["GT", "LT", "NE", "LE"]
    assert [rc.soft_hard for rc in hr_constraints.range_checks] == ["Hard", "Hard", "Hard", "Soft"]

    # Valid payload with Soft RangeCheck warning (HR=110 > 100)
    with caplog.at_level(logging.WARNING):
        report = validate_record_data(
            dd,
            "VS",
            {
                "HR": 110,
                "DOSE_COHORT": 20,
                "VISIT_NUM": 2,
                "PROTO_VER": 3,
                "DateOfBirth": "1990-05-15",
            },
            collect_errors=True,
        )
    assert report is not None
    assert report.errors == []
    assert len(report.warnings) == 1
    assert "Tachycardia warning" in report.warnings[0].message

    # Hard RangeCheck violations raise ValidationError
    with pytest.raises(ValidationError, match="GT 30"):
        validate_record_data(dd, "VS", {"HR": 30})
    with pytest.raises(ValidationError, match="LT 220"):
        validate_record_data(dd, "VS", {"HR": 220})
    with pytest.raises(ValidationError, match="IN"):
        validate_record_data(dd, "VS", {"DOSE_COHORT": 30})
    with pytest.raises(ValidationError, match="NOTIN"):
        validate_record_data(dd, "VS", {"VISIT_NUM": 13})
    with pytest.raises(ValidationError, match="GE 1"):
        validate_record_data(dd, "VS", {"VISIT_NUM": 0})
    with pytest.raises(ValidationError, match="EQ 3"):
        validate_record_data(dd, "VS", {"PROTO_VER": 4})

    # PHI variable masks violated value in RangeCheck error message
    secret_dob = "1905-06-12"
    with pytest.raises(ValidationError) as exc_info:
        validate_record_data(dd, "VS", {"DateOfBirth": secret_dob})
    assert MASKED_SENTINEL in str(exc_info.value)
    assert secret_dob not in str(exc_info.value)


def test_load_offline_schema_supports_zip_archive_and_is_file_target(tmp_path: Path) -> None:
    """load_offline_schema loads .zip Data Dictionary archives and _is_file_target matches .zip."""
    import zipfile

    from imednet.cli.utils.write_helpers import load_offline_schema
    from imednet.datadict.normalize import _is_file_target

    assert _is_file_target("study_datadict.zip") is True
    assert _is_file_target(Path("exports/archive.zip")) is True

    fixtures_dir = Path(__file__).parent.parent / "fixtures" / "data_dictionary"
    zip_path = tmp_path / "study_dd.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for csv_name in ("FORMS.csv", "QUESTIONS.csv", "CHOICES.csv", "BUSINESS_LOGIC.csv"):
            zf.write(fixtures_dir / csv_name, arcname=csv_name)

    dd = load_offline_schema(zip_path, study_key="ZIP_STUDY")
    assert isinstance(dd, DataDictionary)
    assert dd.metadata.studyKey == "ZIP_STUDY"
    assert {f.formKey for f in dd.forms} == {"AE", "AEACTEL", "AESDC"}


@pytest.mark.parametrize("bad_float", [float("nan"), float("inf"), float("-inf")])
def test_untyped_required_variable_rejects_non_finite_floats_in_validation_and_readiness(
    bad_float: float,
) -> None:
    """AC 3: Untyped required Variable with NaN/Inf/-Inf fails validation and scores < 100.0."""
    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="F_UNTYPED",
                variables=[
                    DDVariable(variableName="RAW_METRIC", variableType=None, required="Yes")
                ],
            )
        ]
    )
    cache = SchemaCache()
    cache.populate(
        [
            ApiVariable(
                variable_name="RAW_METRIC",
                variable_type="",
                form_id=1,
                form_key="F_UNTYPED",
            )
        ]
    )

    for schema_obj in (dd, cache):
        with pytest.raises(ValidationError, match="finite"):
            validate_record_data(schema_obj, "F_UNTYPED", {"RAW_METRIC": bad_float})
        with pytest.raises(ValidationError, match="finite"):
            validate_record_entry(
                schema_obj,
                {"formKey": "F_UNTYPED", "data": {"RAW_METRIC": bad_float}},
            )
        report = validate_record_entry(
            schema_obj,
            {"formKey": "F_UNTYPED", "data": {"RAW_METRIC": bad_float}},
            collect_errors=True,
        )
        assert report is not None and not report.is_valid

    score, reasons = calculate_readiness_score(dd, "F_UNTYPED", {"RAW_METRIC": bad_float})
    assert score < 100.0
    assert score == pytest.approx(0.0)
    assert any("finite" in r for r in reasons)

    cache_score, cache_reasons = calculate_readiness_score(
        cache, "F_UNTYPED", {"RAW_METRIC": bad_float}
    )
    assert cache_score < 100.0
    assert any("finite" in r for r in cache_reasons)


def test_number_variable_accepts_finite_decimals_across_caches_and_datadictionary() -> None:
    """AC 4: Number Variables accept finite decimals like 68.5 while rejecting non-finite floats."""
    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="VS",
                variables=[DDVariable(variableName="WEIGHT", variableType="Number")],
            )
        ]
    )
    cache = SchemaCache()
    cache.populate(
        [ApiVariable(variable_name="WEIGHT", variable_type="Number", form_id=10, form_key="VS")],
        study_key="STUDY",
    )

    for schema_obj in (dd, cache):
        validate_record_data(schema_obj, "VS", {"WEIGHT": 68.5}, study_key="STUDY")
        validate_record_data(schema_obj, "VS", {"WEIGHT": 68}, study_key="STUDY")
        with pytest.raises(ValidationError, match="numeric"):
            validate_record_data(schema_obj, "VS", {"WEIGHT": "68.5"}, study_key="STUDY")
        with pytest.raises(ValidationError, match="finite"):
            validate_record_data(schema_obj, "VS", {"WEIGHT": float("nan")}, study_key="STUDY")


def test_targeted_record_updates_enforce_constraints_and_phi_masking_in_all_modes() -> None:
    """AC 2 & Req 3: Targeted updates enforce unknown vars, types, choices, bounds, rangeChecks, lengths, and PHI masking."""
    from imednet.utils.tabular import MASKED_SENTINEL

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="F_DM",
                formId=301,
                variables=[
                    DDVariable(
                        variableName="SUBJID",
                        variableType="Text",
                        required="Yes",
                        extraAttributes={"minLength": 3, "maxLength": 6},
                    ),
                    DDVariable(
                        variableName="WEIGHT",
                        variableType="Number",
                        extraAttributes={"minValue": 20.0, "maxValue": 250.0},
                    ),
                    DDVariable(
                        variableName="SEX",
                        variableType="Radio",
                        choices=[
                            Choice(choiceValue="M", choiceText="Male"),
                            Choice(choiceValue="F", choiceText="Female"),
                        ],
                    ),
                    DDVariable(
                        variableName="HR",
                        variableType="Integer",
                        extraAttributes={
                            "rangeChecks": [
                                {"comparator": "GT", "softHard": "Hard", "checkValue": "30"},
                                {
                                    "comparator": "LE",
                                    "softHard": "Soft",
                                    "checkValue": "100",
                                    "errorMessage": "Elevated HR",
                                },
                            ]
                        },
                    ),
                    DDVariable(
                        variableName="DateOfBirth",
                        variableType="Date",
                        extraAttributes={
                            "rangeChecks": [
                                {
                                    "comparator": "GE",
                                    "softHard": "Hard",
                                    "checkValue": "1920-01-01",
                                }
                            ]
                        },
                    ),
                    DDVariable(variableName="RAW_NOTE", variableType=None),
                ],
            )
        ]
    )

    cache = SchemaCache()
    cache.populate(
        [
            ApiVariable(variable_name="SUBJID", variable_type="Text", form_id=301, form_key="F_DM"),
            ApiVariable(
                variable_name="WEIGHT", variable_type="Number", form_id=301, form_key="F_DM"
            ),
            ApiVariable(variable_name="SEX", variable_type="Radio", form_id=301, form_key="F_DM"),
            ApiVariable(variable_name="HR", variable_type="Integer", form_id=301, form_key="F_DM"),
            ApiVariable(
                variable_name="DateOfBirth", variable_type="Date", form_id=301, form_key="F_DM"
            ),
            ApiVariable(variable_name="RAW_NOTE", variable_type="", form_id=301, form_key="F_DM"),
        ],
        study_key="STUDY",
    )

    # Valid partial targeted update omits required SUBJID without error and records Soft warning
    ok_report = validate_record_entry(
        dd,
        {"recordId": 99, "data": {"WEIGHT": 68.5, "SEX": "F", "HR": 110}},
        collect_errors=True,
    )
    assert ok_report is not None
    assert ok_report.is_valid
    assert len(ok_report.warnings) == 1
    assert "Elevated HR" in ok_report.warnings[0].message

    validate_record_entry(
        cache,
        {"visitId": 12, "data": {"WEIGHT": 68.5, "SEX": "M"}},
        study_key="STUDY",
    )

    invalid_cases = [
        ({"NO_SUCH": 1}, "Unknown variable NO_SUCH"),
        ({"HR": "bad"}, "integer"),
        ({"RAW_NOTE": float("inf")}, "finite"),
        ({"DateOfBirth": MASKED_SENTINEL}, "masked PHI sentinel"),
    ]
    for bad_data, expected_msg in invalid_cases:
        for schema_obj in (dd, cache):
            with pytest.raises(ValidationError, match=expected_msg):
                validate_record_entry(
                    schema_obj, {"recordId": 99, "data": bad_data}, study_key="STUDY"
                )
            rep = validate_record_entry(
                schema_obj,
                {"recordOid": "OID-99", "data": bad_data},
                study_key="STUDY",
                collect_errors=True,
            )
            assert rep is not None and not rep.is_valid

    # DataDictionary-specific unambiguous constraints on targeted updates (choices, bounds, rangeChecks, lengths)
    dd_constraint_cases = [
        ({"SEX": "X"}, "Invalid choice"),
        ({"WEIGHT": 10.0}, "below minimum"),
        ({"WEIGHT": 300.0}, "exceeds maximum"),
        ({"HR": 25}, "GT 30"),
        ({"SUBJID": "AB"}, "minimum length 3"),
        ({"SUBJID": "ABCDEFG"}, "maximum length 6"),
    ]
    for bad_data, expected_msg in dd_constraint_cases:
        with pytest.raises(ValidationError, match=expected_msg):
            validate_record_entry(dd, {"recordId": 99, "data": bad_data})
        rep = validate_record_entry(dd, {"recordId": 99, "data": bad_data}, collect_errors=True)
        assert rep is not None and not rep.is_valid

    # PHI redaction preserved on targeted updates across fail-fast and collect_errors modes
    secret_dob = "1905-04-12"
    with pytest.raises(ValidationError) as exc_info:
        validate_record_entry(dd, {"recordId": 99, "data": {"DateOfBirth": secret_dob}})
    assert MASKED_SENTINEL in str(exc_info.value)
    assert secret_dob not in str(exc_info.value)

    phi_report = validate_record_entry(
        dd,
        {"recordId": 99, "data": {"DateOfBirth": secret_dob}},
        collect_errors=True,
    )
    assert phi_report is not None and not phi_report.is_valid
    assert all(secret_dob not in err.message for err in phi_report.errors)
    assert any(MASKED_SENTINEL in err.message for err in phi_report.errors)
