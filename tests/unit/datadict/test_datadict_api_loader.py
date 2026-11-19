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
    assert dm_form.variables[0].variableType == "Integer"
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
