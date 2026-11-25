"""Unit tests for schema validator."""

import asyncio
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

    # Missing formKey, formId, and target record identifiers must raise ValidationError
    with pytest.raises(ValidationError, match="formKey.*formId"):
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
    """Test that validate record entry missing both formKey and formId is rejected."""
    from imednet.validation.cache import SchemaCache, ValidationError, validate_record_entry

    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    schema = SchemaCache()
    schema.populate([var])

    with pytest.raises(ValidationError, match="formKey.*formId"):
        validate_record_entry(schema, {"data": {"age": 1}})

    report = validate_record_entry(schema, {"data": {"age": 1}}, collect_errors=True)
    assert not report.is_valid
    assert report.invalid_records == 1
    assert report.errors[0].rule_id == "missing_form_identifier"


def test_type_validators_coverage() -> None:
    """Test that type validators coverage."""
    from imednet.validation.cache import (
        ValidationError,
        _check_type,
        _is_empty_value,
        _validate_bool,
        _validate_calculation,
        _validate_choice_membership,
        _validate_float,
        _validate_int,
        _validate_multi_choice,
        _validate_single_choice,
        _validate_text,
    )

    with pytest.raises(ValidationError, match="must be an integer"):
        _validate_int("1")
    with pytest.raises(ValidationError, match="must be numeric"):
        _validate_float("1.0")
    with pytest.raises(ValidationError, match="must be boolean"):
        _validate_bool(1)
    with pytest.raises(ValidationError, match="must be a string"):
        _validate_text(1)
    with pytest.raises(ValidationError, match="string or integer code"):
        _validate_single_choice(True)
    with pytest.raises(ValidationError, match="collection of string or integer codes"):
        _validate_multi_choice([1.5])
    with pytest.raises(ValidationError, match="numeric or a string"):
        _validate_calculation(True)

    # Whitespace-only variable type is treated as unset
    _check_type("   ", 123)
    assert _is_empty_value(frozenset())
    assert not _is_empty_value(frozenset({"A"}))

    # Multi-choice membership supports bool and frozenset of integer codes
    _validate_choice_membership("V", "F", "Checkbox", True, ["1", "2"])
    _validate_choice_membership("V", "F", "Multi_Select", frozenset({1, 2}), ["1", "2"])


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


@pytest.mark.parametrize("async_mode", [False, True])
def test_multi_study_schema_validator_isolates_colliding_form_keys_and_ids(
    async_mode: bool,
) -> None:
    """SchemaValidator isolates STUDY_A and STUDY_B forms sharing formKey and formId."""
    from imednet.validation.cache import SchemaCache

    var_a = Variable(
        studyKey="STUDY_A",
        variable_name="age",
        variable_type="integer",
        form_id=101,
        form_key="DM",
    )
    var_b = Variable(
        studyKey="STUDY_B",
        variable_name="country",
        variable_type="text",
        form_id=101,
        form_key="DM",
    )

    sdk = MagicMock()
    if async_mode:

        async def _async_get_vars(study_key: str):
            return [var_a] if study_key == "STUDY_A" else [var_b]

        sdk.async_get_variables = AsyncMock(side_effect=_async_get_vars)
        validator = AsyncSchemaValidator(sdk)  # type: ignore[assignment]

        asyncio.run(validator.validate_record("STUDY_A", {"formKey": "DM", "data": {"age": 42}}))
        asyncio.run(
            validator.validate_record("STUDY_B", {"formKey": "DM", "data": {"country": "NL"}})
        )
        asyncio.run(validator.validate_record("STUDY_A", {"formId": 101, "data": {"age": 30}}))
        asyncio.run(
            validator.validate_record("STUDY_B", {"formId": 101, "data": {"country": "US"}})
        )

        with pytest.raises(ValidationError, match="Unknown variables for form DM: country"):
            asyncio.run(
                validator.validate_record("STUDY_A", {"formKey": "DM", "data": {"country": "NL"}})
            )
        with pytest.raises(ValidationError, match="Unknown variables for form DM: age"):
            asyncio.run(
                validator.validate_record("STUDY_B", {"formKey": "DM", "data": {"age": 42}})
            )
    else:
        sdk.get_variables.side_effect = lambda study_key: (
            [var_a] if study_key == "STUDY_A" else [var_b]
        )
        validator = SchemaValidator(sdk)  # type: ignore[assignment]

        validator.validate_record("STUDY_A", {"formKey": "DM", "data": {"age": 42}})
        validator.validate_record("STUDY_B", {"formKey": "DM", "data": {"country": "NL"}})
        validator.validate_record("STUDY_A", {"formId": 101, "data": {"age": 30}})
        validator.validate_record("STUDY_B", {"formId": 101, "data": {"country": "US"}})

        with pytest.raises(ValidationError, match="Unknown variables for form DM: country"):
            validator.validate_record("STUDY_A", {"formKey": "DM", "data": {"country": "NL"}})
        with pytest.raises(ValidationError, match="Unknown variables for form DM: age"):
            validator.validate_record("STUDY_B", {"formKey": "DM", "data": {"age": 42}})

    cache: SchemaCache = validator.schema  # type: ignore[assignment]
    assert set(cache.forms_for_study("STUDY_A")["DM"].keys()) == {"age"}
    assert set(cache.forms_for_study("STUDY_B")["DM"].keys()) == {"country"}
    assert ("STUDY_A", "DM") in cache.study_forms
    assert ("STUDY_B", "DM") in cache.study_forms

    # Ambiguous unkeyed lookup across multiple studies raises ValidationError
    with pytest.raises(ValidationError, match="Ambiguous form"):
        cache.variables_for_form("DM")
    with pytest.raises(ValidationError, match="Ambiguous form ID"):
        cache.form_key_from_id(101)


@pytest.mark.parametrize("async_mode", [False, True])
def test_validators_reject_unscoped_targetless_records_and_empty_form_keys(
    async_mode: bool,
) -> None:
    """Sync/async validate_record, validate_batch, and _ValidatorMixin reject unscoped records."""
    from imednet.validation._base import _ValidatorMixin
    from imednet.validation.cache import SchemaCache

    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    sdk = _build_sdk(var, async_mode)
    validator = AsyncSchemaValidator(sdk) if async_mode else SchemaValidator(sdk)

    for unscoped in ({}, {"data": {"age": 20}}, {"formKey": "", "data": {"age": 20}}):
        with pytest.raises(ValidationError, match="formKey.*formId"):
            if async_mode:
                asyncio.run(validator.validate_record("STUDY", unscoped))  # type: ignore[arg-type]
            else:
                validator.validate_record("STUDY", unscoped)

        with pytest.raises(ValidationError, match="formKey.*formId"):
            if async_mode:
                asyncio.run(validator.validate_batch("STUDY", [unscoped]))  # type: ignore[arg-type]
            else:
                validator.validate_batch("STUDY", [unscoped])

    # Direct _ValidatorMixin._validate_cached check
    class _DummyValidator(_ValidatorMixin):
        def __init__(self) -> None:
            self.schema = SchemaCache()
            self.schema.populate([var])

    dummy = _DummyValidator()
    with pytest.raises(ValidationError, match="formKey.*formId"):
        dummy._validate_cached(None, {"age": 20})
    with pytest.raises(ValidationError, match="formKey.*formId"):
        dummy._validate_cached("", {"age": 20})


@pytest.mark.parametrize("async_mode", [False, True])
def test_validators_refresh_and_retain_zero_variable_forms_and_accept_number_decimals(
    async_mode: bool,
) -> None:
    """Refresh retains active zero-Variable Forms and accepts finite decimals on Number Variables."""
    from imednet.models.forms import Form as ApiForm
    from imednet.validation.cache import AsyncSchemaCache, SchemaCache, calculate_readiness_score

    empty_form = ApiForm(studyKey="STUDY", formId=500, formKey="F_EMPTY", formName="Empty Form")
    disabled_form = ApiForm(
        studyKey="STUDY", formId=501, formKey="F_DISABLED", formName="Disabled", disabled=True
    )
    deleted_form_dict = {
        "studyKey": "STUDY",
        "formId": 503,
        "formKey": "F_DELETED",
        "deleted": True,
    }
    weight_var = Variable(
        studyKey="STUDY", variable_name="WEIGHT", variable_type="Number", form_id=502, form_key="VS"
    )

    if async_mode:
        sdk = MagicMock()
        sdk.async_get_forms = AsyncMock(return_value=[empty_form, disabled_form, deleted_form_dict])
        sdk.async_get_variables = AsyncMock(return_value=[weight_var])
        validator = AsyncSchemaValidator(sdk)
        asyncio.run(validator.validate_record("STUDY", {"formKey": "F_EMPTY", "data": {}}))
        asyncio.run(validator.validate_record("STUDY", {"formId": 500, "data": {}}))
        asyncio.run(validator.validate_record("STUDY", {"formKey": "VS", "data": {"WEIGHT": 68.5}}))
        with pytest.raises(ValidationError, match="Unknown form"):
            asyncio.run(validator.validate_record("STUDY", {"formKey": "F_DISABLED", "data": {}}))
        with pytest.raises(ValidationError, match="Unknown form"):
            asyncio.run(validator.validate_record("STUDY", {"formKey": "F_DELETED", "data": {}}))
        score, reasons = calculate_readiness_score(
            validator.schema, "F_EMPTY", {}, study_key="STUDY"
        )
        assert score == pytest.approx(100.0)
        assert reasons == []

        async_cache = AsyncSchemaCache()
        forms_ep = MagicMock()
        vars_ep = MagicMock()
        forms_ep.list = AsyncMock(return_value=[empty_form, disabled_form, deleted_form_dict])
        vars_ep.list = AsyncMock(return_value=[weight_var])
        asyncio.run(async_cache.refresh(forms_ep, vars_ep, study_key="STUDY"))
        assert "F_EMPTY" in async_cache.forms_for_study("STUDY")
        assert "F_DISABLED" not in async_cache.forms_for_study("STUDY")
        assert "F_DELETED" not in async_cache.forms_for_study("STUDY")
        assert async_cache.form_key_from_id(500, study_key="STUDY") == "F_EMPTY"
    else:
        sdk = MagicMock()
        sdk.get_forms.return_value = [empty_form, disabled_form, deleted_form_dict]
        sdk.get_variables.return_value = [weight_var]
        validator = SchemaValidator(sdk)
        validator.validate_record("STUDY", {"formKey": "F_EMPTY", "data": {}})
        validator.validate_record("STUDY", {"formId": 500, "data": {}})
        validator.validate_record("STUDY", {"formKey": "VS", "data": {"WEIGHT": 68.5}})
        with pytest.raises(ValidationError, match="Unknown form"):
            validator.validate_record("STUDY", {"formKey": "F_DISABLED", "data": {}})
        with pytest.raises(ValidationError, match="Unknown form"):
            validator.validate_record("STUDY", {"formKey": "F_DELETED", "data": {}})
        score, reasons = calculate_readiness_score(
            validator.schema, "F_EMPTY", {}, study_key="STUDY"
        )
        assert score == pytest.approx(100.0)
        assert reasons == []

        sync_cache = SchemaCache()
        forms_ep = MagicMock()
        vars_ep = MagicMock()
        forms_ep.list.return_value = [empty_form, disabled_form, deleted_form_dict]
        vars_ep.list.return_value = [weight_var]
        sync_cache.refresh(forms_ep, vars_ep, study_key="STUDY")
        assert "F_EMPTY" in sync_cache.forms_for_study("STUDY")
        assert "F_DISABLED" not in sync_cache.forms_for_study("STUDY")
        assert "F_DELETED" not in sync_cache.forms_for_study("STUDY")
        assert sync_cache.form_key_from_id(500, study_key="STUDY") == "F_EMPTY"
