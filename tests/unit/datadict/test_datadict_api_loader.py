"""Tests for live API data dictionary loader."""

from __future__ import annotations

from unittest.mock import Mock

from imednet.datadict.loaders.api import load_from_api


def test_load_from_api_mocked_sdk() -> None:
    """Test extracting forms and variables from live SDK with documented partial structure."""
    mock_sdk = Mock()
    mock_sdk.forms.list.return_value = [
        {"form_key": "DM", "form_name": "Demographics", "form_id": 1001, "form_type": "Patient"},
        {"form_key": "AE", "form_name": "Adverse Events", "form_id": 1002, "form_type": "Patient"},
    ]
    mock_sdk.variables.list.return_value = [
        {
            "form_key": "DM",
            "variable_name": "AGE",
            "label": "Subject Age",
            "variable_type": "Number",
        },
        {
            "form_key": "AE",
            "variable_name": "AESTDAT",
            "label": "Start Date",
            "variable_type": "Date",
        },
    ]

    dd = load_from_api(mock_sdk, study_key="STUDY_ABC")

    assert dd.metadata.studyKey == "STUDY_ABC"
    assert dd.metadata.source == "api"
    assert any("Business logic rules and choices" in w for w in dd.metadata.validationWarnings)

    assert len(dd.forms) == 2
    dm_form = next(f for f in dd.forms if f.formKey == "DM")
    assert dm_form.formName == "Demographics"
    assert dm_form.formId == 1001
    assert dm_form.formType == "Subject"
    assert len(dm_form.variables) == 1
    assert dm_form.variables[0].variableName == "AGE"
    assert dm_form.variables[0].variableType == "Float"
    assert dm_form.variables[0].choices == []
    assert dm_form.businessLogic == []
    assert dd.unassignedBusinessLogic == []


def test_load_from_api_pydantic_sdk_models_preserves_extra_attributes() -> None:
    """Calling load_from_api with Pydantic SDK Form and Variable models preserves extraAttributes."""
    from imednet.datadict import get_schema, validate
    from imednet.models.forms import Form as SdkForm
    from imednet.models.variables import Variable as SdkVariable

    sdk_form = SdkForm.model_validate(
        {
            "studyKey": "STUDY_SDK",
            "formId": 2001,
            "formKey": "VS",
            "formName": "Vital Signs",
            "formType": "Patient",
            "revision": 4,
            "disabled": False,
            "unscheduledVisit": True,
            "subjectRecordReport": True,
            "eproForm": False,
            "allowCopy": True,
        }
    )
    sdk_var = SdkVariable.model_validate(
        {
            "studyKey": "STUDY_SDK",
            "formId": 2001,
            "formKey": "VS",
            "formName": "Vital Signs",
            "variableId": 5001,
            "variableName": "SYSBP",
            "variableOid": "OID_SYSBP_01",
            "variableType": "Numeric",
            "label": "Systolic Blood Pressure",
            "sequence": 1,
            "revision": 7,
            "blinded": True,
            "disabled": False,
            "deleted": False,
        }
    )
    orphan_var = SdkVariable.model_validate(
        {
            "variableName": "ORPHAN_VAR",
            "variableOid": "OID_ORPHAN",
            "variableType": "RadioButton",
            "label": "Orphan Variable",
        }
    )

    mock_sdk = Mock()
    mock_sdk.forms.list.return_value = [sdk_form]
    mock_sdk.variables.list.return_value = [sdk_var, orphan_var]

    dd = load_from_api(mock_sdk, study_key="STUDY_SDK")
    assert validate(dd, schema=get_schema()) == []

    vs_form = next(f for f in dd.forms if f.formKey == "VS")
    assert vs_form.formId == 2001
    assert vs_form.formType == "Subject"
    assert vs_form.extraAttributes["revision"] == 4
    assert vs_form.extraAttributes["disabled"] is False
    assert vs_form.extraAttributes["unscheduledVisit"] is True
    assert vs_form.extraAttributes["subjectRecordReport"] is True
    assert vs_form.extraAttributes["eproForm"] is False
    assert vs_form.extraAttributes["allowCopy"] is True
    for excluded in ("studyKey", "formKey", "formName", "formId", "formType"):
        assert excluded not in vs_form.extraAttributes

    sysbp = vs_form.variables[0]
    assert sysbp.variableName == "SYSBP"
    assert sysbp.variableType == "Float"
    assert sysbp.extraAttributes["variableOid"] == "OID_SYSBP_01"
    assert sysbp.extraAttributes["variableId"] == 5001
    assert sysbp.extraAttributes["blinded"] is True
    assert sysbp.extraAttributes["disabled"] is False
    assert sysbp.extraAttributes["deleted"] is False
    assert sysbp.extraAttributes["revision"] == 7
    for excluded in ("studyKey", "formKey", "formName", "formId", "variableName", "label"):
        assert excluded not in sysbp.extraAttributes

    unassigned_form = next(f for f in dd.forms if f.formKey == "UNASSIGNED")
    assert unassigned_form.formType == "Subject"
    assert unassigned_form.variables[0].variableType == "Radio"
    assert unassigned_form.variables[0].extraAttributes["variableOid"] == "OID_ORPHAN"


def test_load_from_api_skips_deleted_and_disabled_variables_and_keeps_forms() -> None:
    """load_from_api filters deleted=True and disabled=True Variables while preserving Form IDs."""
    from imednet.validation.cache import validate_record_data

    mock_sdk = Mock()
    mock_sdk.forms.list.return_value = [
        {"form_key": "DM", "form_name": "Demographics", "form_id": 1001, "form_type": "Patient"},
        {
            "form_key": "LEGACY",
            "form_name": "Retired Form",
            "form_id": 1009,
            "form_type": "Patient",
        },
    ]
    mock_sdk.variables.list.return_value = [
        {
            "form_key": "DM",
            "variable_name": "SUBJID",
            "label": "Subject ID",
            "variable_type": "Text",
            "deleted": False,
            "disabled": False,
        },
        {
            "form_key": "DM",
            "variable_name": "OLD_DELETED_REQ",
            "label": "Retired Deleted Field",
            "variable_type": "Text",
            "deleted": True,
            "disabled": False,
            "required": "Yes",
        },
        {
            "form_key": "DM",
            "variable_name": "OLD_DISABLED_REQ",
            "label": "Retired Disabled Field",
            "variable_type": "Number",
            "deleted": False,
            "disabled": True,
            "required": "Yes",
        },
        {
            "form_key": "LEGACY",
            "variable_name": "RETIRED_ONLY",
            "label": "Retired Only",
            "variable_type": "Text",
            "deleted": True,
            "required": "Yes",
        },
    ]

    dd = load_from_api(mock_sdk, study_key="STUDY_FILTER")
    dm_form = dd.form_for_key("DM")
    legacy_form = dd.form_for_key("LEGACY")
    assert dm_form is not None
    assert legacy_form is not None
    assert [v.variableName for v in dm_form.variables] == ["SUBJID"]
    assert legacy_form.variables == []
    assert dd.form_key_from_id(1001) == "DM"
    assert dd.form_key_from_id(1009) == "LEGACY"

    validate_record_data(dd, "DM", {"SUBJID": "SUBJ-001"})
    validate_record_data(dd, "LEGACY", {})


def test_load_from_api_excludes_disabled_forms_and_accepts_decimal_number_end_to_end() -> None:
    """AC 7: load_from_api, SchemaValidator, calculate_readiness_score, and apply_quality_gate exclude disabled forms and accept 98.6."""
    import io

    import pytest

    from imednet.datadict.loaders.csv_export import load_from_csv_files
    from imednet.errors import ValidationError
    from imednet.integrations.sink_base import SinkConfig, apply_quality_gate
    from imednet.models.forms import Form as SdkForm
    from imednet.models.records import Record
    from imednet.models.variables import Variable as SdkVariable
    from imednet.validation.cache import (
        SchemaValidator,
        calculate_readiness_score,
        validate_record_data,
        validate_record_entry,
    )

    active_form = SdkForm(form_id=10, form_key="VS", form_name="Vitals", disabled=False)
    disabled_form = SdkForm(form_id=20, form_key="OLD_VS", form_name="Old Vitals", disabled=True)
    temp_var = SdkVariable(
        form_id=10,
        form_key="VS",
        variable_name="TEMP",
        variable_type="NUMBER",
        required=True,
    )
    disabled_form_var = SdkVariable(
        form_id=20,
        form_key="OLD_VS",
        variable_name="OLD_TEMP",
        variable_type="NUMBER",
        required=True,
    )

    mock_sdk = Mock()
    mock_sdk.forms.list.return_value = [active_form, disabled_form]
    mock_sdk.variables.list.return_value = [temp_var, disabled_form_var]
    mock_sdk.get_variables.return_value = [temp_var]

    dd = load_from_api(mock_sdk, study_key="STUDY_AC7")
    assert [f.formKey for f in dd.forms] == ["VS"]
    assert dd.form_for_key("OLD_VS") is None
    assert dd.form_key_from_id(20) is None
    assert dd.forms[0].variables[0].variableType == "Float"
    validate_record_data(dd, "VS", {"TEMP": 98.6})
    with pytest.raises(ValidationError, match="Unknown form OLD_VS"):
        validate_record_entry(dd, {"formKey": "OLD_VS", "data": {"OLD_TEMP": 98.6}})
    with pytest.raises(ValidationError, match="Unknown form ID 20"):
        validate_record_entry(dd, {"formId": 20, "data": {"OLD_TEMP": 98.6}})

    validator = SchemaValidator(mock_sdk)
    validator.validate_record("STUDY_AC7", {"formKey": "VS", "data": {"TEMP": 98.6}})

    dd_score, dd_reasons = calculate_readiness_score(dd, "VS", {"TEMP": 98.6})
    assert dd_score == pytest.approx(100.0)
    assert dd_reasons == []

    cache_score, cache_reasons = calculate_readiness_score(validator.schema, "VS", {"TEMP": 98.6})
    assert cache_score == pytest.approx(100.0)
    assert cache_reasons == []

    recs = [
        Record(record_id=1, form_id=10, form_key="VS", record_data={"TEMP": 98.6}),
        Record(record_id=2, form_id=20, form_key=None, record_data={"OLD_TEMP": 98.6}),
        Record(record_id=3, form_id=20, form_key="OLD_VS", record_data={"OLD_TEMP": 98.6}),
    ]
    kept = list(
        apply_quality_gate(
            mock_sdk,
            "STUDY_AC7",
            recs,
            SinkConfig(
                study_key="STUDY_AC7",
                quality_gate_enabled=True,
                min_schema_readiness_score=100.0,
            ),
            variables=[temp_var, disabled_form_var],
            forms=[active_form, disabled_form],
        )
    )
    assert [r.record_id for r in kept] == [1]

    dd_csv_zero = load_from_csv_files(
        forms_file=io.StringIO("formId,formKey,formName,formType\n10,VS,Vitals,Patient\n"),
        questions_file=io.StringIO(
            "formKey,variableName,label,variableType,sequence,required,precision\n"
            "VS,COUNT,Count,Number,1,Yes,0\n"
        ),
        choices_file=io.StringIO("formKey,variableName,choiceValue,choiceText,position\n"),
        business_logic_file=io.StringIO("id,name,type,status,formKey,sequence,logic\n"),
        study_key="STUDY_AC7",
    )
    assert dd_csv_zero.forms[0].variables[0].variableType == "Integer"
