"""Unit tests for record update."""

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet.errors import ValidationError
from imednet.models.jobs import Job
from imednet.models.variables import Variable
from imednet.testing import fake_data
from imednet.validation.cache import SchemaCache
from imednet_workflows.record_update import RecordUpdateWorkflow


def _build_schema() -> tuple[SchemaCache, Variable]:
    """Helper function to  build schema."""
    forms = fake_data.fake_forms_for_cache(1, study_key="S")
    variables = fake_data.fake_variables_for_cache(forms, vars_per_form=1, study_key="S")
    var = variables[0]
    # Bolt: Removed 'required' attribute injection as support was removed
    var.variable_type = "integer"
    forms_ep = types.SimpleNamespace(list=lambda **_: forms)

    def list_vars(*_, form_id=None, **__):
        """Helper function to list vars."""
        return [v for v in variables if form_id is None or v.form_id == form_id]

    vars_ep = types.SimpleNamespace(list=list_vars)
    from typing import Any, cast

    cache = SchemaCache()
    cache.refresh(cast(Any, forms_ep), cast(Any, vars_ep), study_key="S")
    return cache, var


@pytest.mark.parametrize("async_mode", [False, True])
def test_create_or_update_records_no_wait(schema: SchemaCache, async_mode: bool) -> None:
    """Test that create or update records normalizes recordData dicts before SDK submission."""
    from imednet.utils.tabular import normalize_records_input

    sdk = MagicMock()
    job = Job(batch_id="1", state="PROCESSING")
    if not async_mode:
        del sdk.async_create_record

    if async_mode:
        sdk.async_create_record = AsyncMock(return_value=job)
    else:
        sdk.create_record = MagicMock(return_value=job)

    wf = RecordUpdateWorkflow(sdk)
    wf._validator.schema = schema
    wf._schema = schema
    record = fake_data.fake_record(schema)
    expected = normalize_records_input([record], schema=schema, study_key="S")

    if async_mode:
        result = asyncio.run(wf.async_create_or_update_records("S", [record]))
        sdk.async_create_record.assert_awaited_once_with("S", expected, schema=schema)
    else:
        result = wf.create_or_update_records("S", [record])
        sdk.create_record.assert_called_once_with("S", expected, schema=schema)
    assert result == job


@pytest.mark.parametrize("async_mode", [False, True])
def test_first_call_workflow_refreshes_schema_before_tabular_coercion(
    tmp_path, async_mode: bool
) -> None:
    """AC 6: First call on a fresh sync/async SDK refreshes schema before coercing CSV/DataFrame."""
    csv_path = tmp_path / "first_call.csv"
    csv_path.write_text(
        "form_key,subject_key,site_name,V_CODE,V_FLAG,V_TF\nF_FIRST,SUB-1,SITE-1,007,true,false\n",
        encoding="utf-8",
    )
    vars_payload = [
        Variable(
            variable_name="V_CODE",
            variable_type="text",
            form_id=11,
            form_key="F_FIRST",
        ),
        Variable(
            variable_name="V_FLAG",
            variable_type="checkbox",
            form_id=11,
            form_key="F_FIRST",
        ),
        Variable(
            variable_name="V_TF",
            variable_type="true/false",
            form_id=11,
            form_key="F_FIRST",
        ),
    ]
    job = Job(batch_id="B-FIRST", state="PROCESSING")
    sdk = MagicMock()
    if async_mode:
        sdk._async_client = object()
        sdk.async_get_variables = AsyncMock(return_value=vars_payload)
        sdk.async_create_record = AsyncMock(return_value=job)
        wf = RecordUpdateWorkflow(sdk)
        summary = asyncio.run(wf.async_create_or_update_records_batch("S_FRESH", csv_path))
        sdk.async_get_variables.assert_awaited_once_with(study_key="S_FRESH")
        sent_chunk = sdk.async_create_record.await_args.args[1]
    else:
        del sdk.async_create_record
        sdk._async_client = None
        sdk.get_variables = MagicMock(return_value=vars_payload)
        sdk.create_record = MagicMock(return_value=job)
        wf = RecordUpdateWorkflow(sdk)
        summary = wf.create_or_update_records_batch("S_FRESH", csv_path)
        sdk.get_variables.assert_called_once_with(study_key="S_FRESH")
        sent_chunk = sdk.create_record.call_args.args[1]

    assert summary.all_successful is True
    assert sent_chunk == [
        {
            "formKey": "F_FIRST",
            "subjectKey": "SUB-1",
            "siteName": "SITE-1",
            "data": {"V_CODE": "007", "V_FLAG": True, "V_TF": False},
        }
    ]


@pytest.mark.parametrize("async_mode", [False, True])
def test_create_or_update_records_validation(async_mode: bool) -> None:
    """Test that create or update records validation."""
    schema, var = _build_schema()
    sdk = MagicMock()
    if async_mode:
        sdk._async_client = object()
        sdk.async_create_record = AsyncMock(return_value=Job(batch_id="1", state="PROCESSING"))
    else:
        del sdk.async_create_record
        sdk.create_record = MagicMock(return_value=Job(batch_id="1", state="PROCESSING"))

    wf = RecordUpdateWorkflow(sdk)
    wf._validator.schema = schema
    wf._schema = schema

    # Mock the validator's validate_batch to raise ValidationError directly
    if async_mode:
        mock_val = AsyncMock(side_effect=[ValidationError("Mocked error"), None])
        wf._validator.validate_batch = mock_val  # type: ignore[method-assign]
    else:
        mock_val = MagicMock(side_effect=[ValidationError("Mocked error"), None])
        wf._validator.validate_batch = mock_val  # type: ignore[method-assign]

    # Bolt: Changed to type error test since required field check was removed
    with pytest.raises(ValidationError):
        bad_payload = [{"formKey": var.form_key, "data": {var.variable_name: "bad_type"}}]
        if async_mode:
            asyncio.run(wf.async_create_or_update_records("S", bad_payload))
        else:
            wf.create_or_update_records("S", bad_payload)
    if async_mode:
        sdk.async_create_record.assert_not_awaited()
    else:
        sdk.create_record.assert_not_called()

    if async_mode:
        asyncio.run(
            wf.async_create_or_update_records(
                "S", [{"formKey": var.form_key, "data": {var.variable_name: 5}}]
            )
        )
        sdk.async_create_record.assert_awaited_once_with(
            "S",
            [{"formKey": var.form_key, "data": {var.variable_name: 5}}],
            schema=schema,
        )
    else:
        wf.create_or_update_records(
            "S", [{"formKey": var.form_key, "data": {var.variable_name: 5}}]
        )
        sdk.create_record.assert_called_once_with(
            "S",
            [{"formKey": var.form_key, "data": {var.variable_name: 5}}],
            schema=schema,
        )


@pytest.mark.parametrize("async_mode", [False, True])
def test_create_or_update_records_unknown_form_key(async_mode: bool) -> None:
    """Test that create or update records unknown form key."""
    sdk = MagicMock()
    if async_mode:
        sdk._async_client = object()
        sdk.async_create_record = AsyncMock()
    else:
        del sdk.async_create_record
        sdk._async_client = None
        sdk.create_record = MagicMock()

    wf = RecordUpdateWorkflow(sdk)
    schema = SchemaCache()
    wf._validator.schema = schema
    wf._schema = schema

    if async_mode:
        wf._validator.refresh = AsyncMock()  # type: ignore[method-assign]
        wf._validator.validate_batch = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="Form key 'F1' not found"):
            asyncio.run(wf.async_create_or_update_records("S", [{"formKey": "F1", "data": {}}]))
        wf._validator.refresh.assert_awaited_once_with("S")
        wf._validator.validate_batch.assert_not_called()
        sdk.async_create_record.assert_not_awaited()
    else:
        # Mocking sync calls, but within _create_or_update_common they are
        # called within an event loop. However, _is_async on workflow is
        # False, so it calls it synchronously.
        wf._validator.refresh = MagicMock()  # type: ignore[method-assign]
        wf._validator.validate_batch = MagicMock()  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="Form key 'F1' not found"):
            wf.create_or_update_records("S", [{"formKey": "F1", "data": {}}])
        wf._validator.refresh.assert_called_once_with("S")
        wf._validator.validate_batch.assert_not_called()
        sdk.create_record.assert_not_called()


@pytest.mark.parametrize("async_mode", [False, True])
def test_create_or_update_records_refresh_and_validate(async_mode: bool) -> None:
    """Test that create or update records refresh and validate."""
    sdk = MagicMock()
    job = Job(batch_id="1", state="PROCESSING")
    if not async_mode:
        del sdk.async_create_record

    if async_mode:
        sdk.async_create_record = AsyncMock(return_value=job)
        sdk.async_get_variables = AsyncMock(
            return_value=[
                Variable(
                    variable_name="age",
                    variable_type="integer",
                    form_id=1,
                    form_key="F1",
                )
            ]
        )
    else:
        sdk.create_record = MagicMock(return_value=job)
        sdk.get_variables = MagicMock(
            return_value=[
                Variable(
                    variable_name="age",
                    variable_type="integer",
                    form_id=1,
                    form_key="F1",
                )
            ]
        )

    wf = RecordUpdateWorkflow(sdk)
    if async_mode:
        wf._validator.validate_batch = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(
            wf.async_create_or_update_records("STUDY", [{"formKey": "F1", "data": {"age": 5}}])
        )
        sdk.async_get_variables.assert_awaited_once_with(study_key="STUDY")
        wf._validator.validate_batch.assert_awaited_once_with(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 5}}],
        )
        sdk.async_create_record.assert_awaited_once_with(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 5}}],
            schema=wf._schema,
        )
    else:
        wf._validator.validate_batch = MagicMock()  # type: ignore[method-assign]
        wf.create_or_update_records("STUDY", [{"formKey": "F1", "data": {"age": 5}}])
        sdk.get_variables.assert_called_once_with(study_key="STUDY")
        wf._validator.validate_batch.assert_called_once_with(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 5}}],
        )
        sdk.create_record.assert_called_once_with(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 5}}],
            schema=wf._schema,
        )


@pytest.mark.parametrize("async_mode", [False, True])
def test_create_or_update_records_wait_for_completion(
    async_mode: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that create or update records wait for completion."""
    sdk = MagicMock()
    initial_job = Job(batch_id="1", state="PROCESSING")
    completed_job = Job(batch_id="1", state="COMPLETED")
    if not async_mode:
        del sdk.async_create_record

    if async_mode:
        sdk.async_create_record = AsyncMock(return_value=initial_job)
        sdk.async_poll_job = AsyncMock(return_value=completed_job)
        sdk.async_get_variables = AsyncMock(
            return_value=[
                Variable(
                    variable_name="age",
                    variable_type="integer",
                    form_id=1,
                    form_key="F1",
                )
            ]
        )
        wf = RecordUpdateWorkflow(sdk)
        wf._validator.validate_batch = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        result = asyncio.run(
            wf.async_create_or_update_records(
                "STUDY",
                [{"formKey": "F1", "data": {}}],
                wait_for_completion=True,
                poll_interval=0,
                timeout=5,
            )
        )
        sdk.async_create_record.assert_awaited_once_with(
            "STUDY",
            [{"formKey": "F1", "data": {}}],
            schema=wf._schema,
        )
        sdk.async_poll_job.assert_awaited_with("STUDY", "1", timeout=5, interval=0)
    else:
        sdk.create_record = MagicMock(return_value=initial_job)
        sdk.poll_job = MagicMock(return_value=completed_job)
        sdk.get_variables = MagicMock(
            return_value=[
                Variable(
                    variable_name="age",
                    variable_type="integer",
                    form_id=1,
                    form_key="F1",
                )
            ]
        )
        wf = RecordUpdateWorkflow(sdk)
        wf._validator.validate_batch = MagicMock()  # type: ignore[method-assign]
        monkeypatch.setattr("time.sleep", lambda *_: None)
        result = wf.create_or_update_records(
            "STUDY",
            [{"formKey": "F1", "data": {}}],
            wait_for_completion=True,
            poll_interval=0,
            timeout=5,
        )
        sdk.create_record.assert_called_once_with(
            "STUDY",
            [{"formKey": "F1", "data": {}}],
            schema=wf._schema,
        )
        sdk.poll_job.assert_called_with("STUDY", "1", timeout=5, interval=0)
    assert result.state == "COMPLETED"
