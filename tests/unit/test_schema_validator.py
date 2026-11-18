"""Unit tests for schema validator."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet.errors import ValidationError
from imednet.models.variables import Variable
from imednet.validation.cache import AsyncSchemaValidator, SchemaValidator


def _build_sdk(variable: Variable, async_mode: bool) -> MagicMock:
    """Helper function to  build sdk."""
    sdk = MagicMock()
    if async_mode:
        sdk.async_get_variables = AsyncMock(return_value=[variable])
    else:
        sdk.get_variables.return_value = [variable]
    return sdk


@pytest.mark.parametrize("async_mode", [False, True])
def test_validate_record_unknown_variable(async_mode: bool) -> None:
    """Test that validate record unknown variable."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode)
    if async_mode:
        validator = AsyncSchemaValidator(sdk)  # type: ignore[assignment]
    else:
        validator = SchemaValidator(sdk)  # type: ignore[assignment]

    with pytest.raises(ValidationError):
        if async_mode:
            asyncio.run(validator.validate_record("STUDY", {"formKey": "F1", "data": {"bad": 1}}))
        else:
            validator.validate_record("STUDY", {"formKey": "F1", "data": {"bad": 1}})

    if async_mode:
        sdk.async_get_variables.assert_awaited_once_with(study_key="STUDY")
    else:
        sdk.get_variables.assert_called_once_with(study_key="STUDY")


@pytest.mark.parametrize("async_mode", [False, True])
def test_validate_record_wrong_type(async_mode: bool) -> None:
    """Test that validate record wrong type."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode)
    if async_mode:
        validator = AsyncSchemaValidator(sdk)  # type: ignore[assignment]
    else:
        validator = SchemaValidator(sdk)  # type: ignore[assignment]

    with pytest.raises(ValidationError):
        if async_mode:
            asyncio.run(validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": "x"}}))
        else:
            validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": "x"}})

    if async_mode:
        sdk.async_get_variables.assert_awaited_once_with(study_key="STUDY")
    else:
        sdk.get_variables.assert_called_once_with(study_key="STUDY")


@pytest.mark.parametrize("async_mode", [False, True])
def test_validate_record_unknown_form(async_mode: bool) -> None:
    """Test that validate record unknown form."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode)
    if async_mode:
        validator = AsyncSchemaValidator(sdk)  # type: ignore[assignment]
    else:
        validator = SchemaValidator(sdk)  # type: ignore[assignment]

    with pytest.raises(ValidationError, match="Unknown form BAD"):
        if async_mode:
            asyncio.run(validator.validate_record("STUDY", {"formKey": "BAD", "data": {}}))
        else:
            validator.validate_record("STUDY", {"formKey": "BAD", "data": {}})

    if async_mode:
        sdk.async_get_variables.assert_awaited_once_with(study_key="STUDY")
    else:
        sdk.get_variables.assert_called_once_with(study_key="STUDY")


@pytest.mark.parametrize("async_mode", [False, True])
def test_refresh_called_when_form_not_cached(async_mode: bool) -> None:
    """Test that refresh called when form not cached."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode)
    if async_mode:
        validator = AsyncSchemaValidator(sdk)  # type: ignore[assignment]
        validator.refresh = AsyncMock(wraps=validator.refresh)  # type: ignore[assignment]
        asyncio.run(validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": 1}}))
        validator.refresh.assert_awaited_once_with("STUDY")
        sdk.async_get_variables.assert_awaited_once_with(study_key="STUDY")
    else:
        validator = SchemaValidator(sdk)  # type: ignore[assignment]
        validator.refresh = MagicMock(wraps=validator.refresh)  # type: ignore[assignment]
        validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": 1}})
        validator.refresh.assert_called_once_with("STUDY")
        sdk.get_variables.assert_called_once_with(study_key="STUDY")


@pytest.mark.parametrize("async_mode", [False, True])
def test_validate_record_cached(async_mode: bool) -> None:
    """Test that validate record cached."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode)
    if async_mode:
        validator = AsyncSchemaValidator(sdk)  # type: ignore[assignment]
    else:
        validator = SchemaValidator(sdk)  # type: ignore[assignment]

    validator.schema._form_variables["F1"] = {"age": var}

    if async_mode:
        asyncio.run(validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": 1}}))
        sdk.async_get_variables.assert_not_awaited()
    else:
        validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": 1}})
        sdk.get_variables.assert_not_called()


@pytest.mark.parametrize("async_mode", [False, True])
def test_validate_record_with_form_id_fallback(async_mode: bool) -> None:
    """Test that validate record with form id fallback."""
    var = Variable(variable_name="age", variable_type="integer", form_id=123, form_key="F1")
    sdk = _build_sdk(var, async_mode)
    if async_mode:
        validator = AsyncSchemaValidator(sdk)  # type: ignore[assignment]
    else:
        validator = SchemaValidator(sdk)  # type: ignore[assignment]

    validator.schema._form_variables["F1"] = {"age": var}
    validator.schema._form_id_to_key[123] = "F1"

    if async_mode:
        asyncio.run(validator.validate_record("STUDY", {"formId": 123, "data": {"age": 1}}))
        sdk.async_get_variables.assert_not_awaited()
    else:
        validator.validate_record("STUDY", {"formId": 123, "data": {"age": 1}})
        sdk.get_variables.assert_not_called()


@pytest.mark.parametrize("async_mode", [False, True])
def test_validate_record_missing_form_identifier(async_mode: bool) -> None:
    """Test that validate record missing form identifier."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode)
    if async_mode:
        validator = AsyncSchemaValidator(sdk)  # type: ignore[assignment]
    else:
        validator = SchemaValidator(sdk)  # type: ignore[assignment]

    validator.schema._form_variables["F1"] = {"age": var}

    # Missing formKey and formId entirely must raise ValidationError without calling the API
    with pytest.raises(ValidationError, match="formKey"):
        if async_mode:
            asyncio.run(validator.validate_record("STUDY", {"data": {"age": "bad"}}))
        else:
            validator.validate_record("STUDY", {"data": {"age": "bad"}})

    if async_mode:
        sdk.async_get_variables.assert_not_awaited()
    else:
        sdk.get_variables.assert_not_called()


def test_schema_validator_rejects_is_async() -> None:
    """Test that SchemaValidator rejects the obsolete is_async argument."""
    sdk = MagicMock()
    with pytest.raises(TypeError):
        SchemaValidator(sdk, is_async=True)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        SchemaValidator(sdk, True)  # type: ignore[call-arg]


def test_validate_record_with_none_value_does_not_raise() -> None:
    """Test that validate record with none value does not raise."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode=False)
    validator = SchemaValidator(sdk)
    validator.schema._form_variables["F1"] = {"age": var}

    # This should not raise an exception
    validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": None}})


def test_check_type_unknown_variable_type() -> None:
    """Test that check type unknown variable type."""
    var = Variable(variable_name="age", variable_type="unknown_type", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode=False)
    validator = SchemaValidator(sdk)
    validator.schema._form_variables["F1"] = {"age": var}

    from imednet.errors import UnknownVariableTypeError

    with pytest.raises(UnknownVariableTypeError, match="unknown_type"):
        validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": 1}})


def test_check_type_case_insensitive_type() -> None:
    """Test that check type case insensitive type."""
    var = Variable(variable_name="age", variable_type="InTeGeR", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode=False)
    validator = SchemaValidator(sdk)
    validator.schema._form_variables["F1"] = {"age": var}

    # This should pass without error due to lowercasing fallback
    validator.validate_record("STUDY", {"formKey": "F1", "data": {"age": 1}})


def test_validate_record_entry_with_form_key() -> None:
    """Test that validate record entry with form key."""
    from imednet.validation.cache import SchemaCache, validate_record_entry

    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    schema = SchemaCache()
    schema.populate([var])

    # Should not raise
    validate_record_entry(schema, {"form_key": "F1", "data": {"age": 1}})


def test_validate_record_entry_with_form_id() -> None:
    """Test that validate record entry with form id."""
    from imednet.validation.cache import SchemaCache, validate_record_entry

    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    schema = SchemaCache()
    schema.populate([var])

    # Should not raise
    validate_record_entry(schema, {"form_id": 1, "data": {"age": 1}})


def test_validate_record_entry_missing_both() -> None:
    """Test that validate record entry missing both."""
    from imednet.validation.cache import SchemaCache, validate_record_entry

    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    schema = SchemaCache()
    schema.populate([var])

    # Missing both formKey and formId must raise ValidationError
    with pytest.raises(ValidationError, match="formKey"):
        validate_record_entry(schema, {"data": {"age": 1}})


def test_type_validators_coverage() -> None:
    """Test type validators including boolean rejection in numeric validators and YYYY-MM-DD dates."""
    from imednet.validation.cache import (
        ValidationError,
        _validate_bool,
        _validate_date,
        _validate_float,
        _validate_int,
        _validate_text,
    )

    with pytest.raises(ValidationError, match="must be an integer"):
        _validate_int("1")
    with pytest.raises(ValidationError, match="must be an integer"):
        _validate_int(True)
    with pytest.raises(ValidationError, match="must be an integer"):
        _validate_int(False)

    with pytest.raises(ValidationError, match="must be numeric"):
        _validate_float("1.0")
    with pytest.raises(ValidationError, match="must be numeric"):
        _validate_float(True)
    with pytest.raises(ValidationError, match="must be numeric"):
        _validate_float(False)

    with pytest.raises(ValidationError, match="must be boolean"):
        _validate_bool(1)
    with pytest.raises(ValidationError, match="must be a string"):
        _validate_text(1)

    _validate_date("2026-05-01")
    _validate_date("2024-02-29")
    for bad_date in [
        19800101,
        "20260501",
        "2026/05/01",
        "2026-5-1",
        "2026-02-30",
        "2025-02-29",
        "1985-13-01",
        "1985-04-12T00:00:00Z",
    ]:
        with pytest.raises(ValidationError, match="YYYY-MM-DD") as exc_info:
            _validate_date(bad_date)
        assert str(bad_date) not in str(exc_info.value)


@pytest.mark.parametrize("async_mode", [False, True])
def test_ecrf_variable_types_and_readiness_score(async_mode: bool) -> None:
    """Verify RADIO, DROPDOWN, MEMO, CHECKBOX, DATE, INTEGER, TEXT, and decimal NUMBER validation."""
    from imednet.validation.cache import SchemaCache, calculate_readiness_score

    variables = [
        Variable(variable_name="v_text", variable_type="TEXT", form_id=7, form_key="EC1"),
        Variable(variable_name="v_int", variable_type="INTEGER", form_id=7, form_key="EC1"),
        Variable(variable_name="v_num", variable_type="NUMBER", form_id=7, form_key="EC1"),
        Variable(variable_name="v_date", variable_type="DATE", form_id=7, form_key="EC1"),
        Variable(variable_name="v_memo", variable_type="MEMO", form_id=7, form_key="EC1"),
        Variable(variable_name="v_drop", variable_type="DROPDOWN", form_id=7, form_key="EC1"),
        Variable(variable_name="v_radio", variable_type="RADIO", form_id=7, form_key="EC1"),
        Variable(variable_name="v_check", variable_type="CHECKBOX", form_id=7, form_key="EC1"),
    ]
    sdk = MagicMock()
    if async_mode:
        sdk.async_get_variables = AsyncMock(return_value=variables)
        validator: Any = AsyncSchemaValidator(sdk)
    else:
        sdk.get_variables.return_value = variables
        validator = SchemaValidator(sdk)

    valid_data = {
        "v_text": "normal",
        "v_int": 42,
        "v_num": 98.6,
        "v_date": "2026-05-01",
        "v_memo": "Long clinical note",
        "v_drop": "OPTION_A",
        "v_radio": "YES",
        "v_check": True,
    }

    # Validate via uncached formId=7 (triggers refresh and resolves EC1)
    payload_by_id = {"formId": 7, "data": valid_data}
    if async_mode:
        asyncio.run(validator.validate_record("STUDY", payload_by_id))
    else:
        validator.validate_record("STUDY", payload_by_id)

    cache = SchemaCache()
    cache.populate(variables)
    score, reasons = calculate_readiness_score(cache, "EC1", valid_data)
    assert score == 100.0
    assert reasons == []

    # Boolean in numeric variable reduces readiness score and fails validate_record
    bad_bool_num = dict(valid_data, v_num=True)
    score_bad, reasons_bad = calculate_readiness_score(cache, "EC1", bad_bool_num)
    assert score_bad < 100.0
    assert any("v_num" in r for r in reasons_bad)

    # Unknown formId after refresh raises ValidationError
    with pytest.raises(ValidationError, match="Unknown formId 999"):
        if async_mode:
            asyncio.run(validator.validate_record("STUDY", {"formId": 999, "data": {}}))
        else:
            validator.validate_record("STUDY", {"formId": 999, "data": {}})

    # Direct _validate_cached with missing form_key raises ValidationError
    with pytest.raises(ValidationError, match="formKey"):
        validator._validate_cached(None, {})
    with pytest.raises(ValidationError, match="formKey"):
        validator._validate_cached("   ", {})


def test_validate_batch_coverage() -> None:
    """Test that validate batch coverage."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode=False)
    validator = SchemaValidator(sdk)
    validator.schema._form_variables["F1"] = {"age": var}

    # Should not raise
    validator.validate_batch(
        "STUDY", [{"formKey": "F1", "data": {"age": 1}}, {"formKey": "F1", "data": {"age": 2}}]
    )


def test_async_validate_batch_coverage() -> None:
    """Test that async validate batch coverage."""
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode=True)
    validator = AsyncSchemaValidator(sdk)
    validator.schema._form_variables["F1"] = {"age": var}

    # Should not raise
    asyncio.run(
        validator.validate_batch(
            "STUDY", [{"formKey": "F1", "data": {"age": 1}}, {"formKey": "F1", "data": {"age": 2}}]
        )
    )


def test_schema_cache_forms_property() -> None:
    """Test that schema cache forms property."""
    from imednet.validation.cache import SchemaCache

    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    cache = SchemaCache()
    cache.populate([var])

    assert "F1" in cache.forms
    assert "age" in cache.forms["F1"]


def test_base_schema_cache_refresh() -> None:
    """Test that base schema cache refresh."""
    from unittest.mock import MagicMock

    from imednet.validation.cache import SchemaCache

    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")

    cache = SchemaCache()
    forms = MagicMock()
    variables = MagicMock()
    variables.list.return_value = [var]

    cache.refresh(forms, variables, "STUDY")
    variables.list.assert_called_once_with(study_key="STUDY")
    assert "F1" in cache.forms


@pytest.mark.asyncio
async def test_base_schema_cache_async_refresh() -> None:
    """Test that base schema cache async refresh asynchronously."""
    from unittest.mock import MagicMock

    from imednet.validation.cache import AsyncSchemaCache

    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")

    cache = AsyncSchemaCache()
    forms = MagicMock()
    variables = MagicMock()

    async def mock_async_list(*args, **kwargs):
        """Helper function to mock async list."""
        yield var

    variables.list = MagicMock(side_effect=mock_async_list)

    await cache.refresh(forms, variables, "STUDY")
    variables.list.assert_called_once_with(study_key="STUDY")
    assert "F1" in cache.forms
