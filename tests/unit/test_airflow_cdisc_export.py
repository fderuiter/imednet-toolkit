"""Airflow provider tests for CDISC ODM 1.3.2 and Dataset-JSON v1.1 export destinations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.conftest import isolate_airflow_provider_modules
from tests.utils.airflow_mocks import setup_dummy_airflow

from imednet.datadict import validate_dataset_json, validate_odm_xml
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable


def test_airflow_export_operator_supports_odm_and_dataset_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Airflow ImednetExportOperator and export wrappers support odm and dataset_json targets with checkpoints."""
    isolate_airflow_provider_modules(monkeypatch)
    setup_dummy_airflow(monkeypatch)

    from apache_airflow_providers_imednet import export as airflow_export
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    assert callable(airflow_export.export_to_odm)
    assert callable(airflow_export.export_to_dataset_json)

    forms = [Form(form_id=102, form_key="VS", form_name="Vital Signs", form_type="Visit")]
    variables = [
        Variable(
            variable_id=1,
            variable_name="SYSBP",
            label="Systolic BP",
            variable_type="int",
            form_id=102,
            form_key="VS",
            sequence=1,
        )
    ]
    record = Record(
        record_id=1,
        study_key="AIRFLOW_CDISC",
        subject_id=500,
        subject_key="SUBJ-100",
        site_id=1,
        form_id=102,
        form_key="VS",
        visit_id=600,
        interval_id=201,
        interval_name="Visit 1",
        date_modified="2026-02-01T10:05:00Z",
        record_data={"SYSBP": 119},
    )

    sdk = MagicMock()
    sdk._async_sdk = None
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.records.iter.side_effect = lambda *_a, **_k: iter([record.model_copy(deep=True)])
    sdk.records.list.side_effect = lambda *_a, **_k: [record.model_copy(deep=True)]
    sdk.get_records.side_effect = lambda *_a, **_k: [record.model_copy(deep=True)]

    odm_out = tmp_path / "airflow_odm.xml"
    dj_out = tmp_path / "airflow_dj.json"
    direct_odm = tmp_path / "direct_odm.xml"
    direct_dj = tmp_path / "direct_dj.json"
    ledger_file = tmp_path / "airflow_ledger.json"

    airflow_export.export_to_odm(sdk, "AIRFLOW_CDISC", str(direct_odm))
    airflow_export.export_to_dataset_json(sdk, "AIRFLOW_CDISC", str(direct_dj))
    assert validate_odm_xml(direct_odm) == []
    assert validate_dataset_json(direct_dj) == []

    with patch("apache_airflow_providers_imednet.operators.export.ImednetHook") as mock_hook_cls:
        mock_hook = mock_hook_cls.return_value
        mock_hook.get_conn.return_value = sdk

        op_odm = ImednetExportOperator(
            task_id="export_odm_task",
            study_key="AIRFLOW_CDISC",
            destination="odm",
            output_path=str(odm_out),
            incremental=True,
            ledger_path=str(ledger_file),
            extra_export_kwargs={"creation_datetime": "2026-02-10T00:00:00Z"},
        )
        op_odm.execute(context={})

        op_dj = ImednetExportOperator(
            task_id="export_dj_task",
            study_key="AIRFLOW_CDISC",
            destination="dataset-json",
            output_path=str(dj_out),
            incremental=True,
            ledger_path=str(ledger_file),
            extra_export_kwargs={"creation_datetime": "2026-02-10T00:00:00Z"},
        )
        op_dj.execute(context={})

    assert ledger_file.is_file()

    assert odm_out.is_file()
    assert validate_odm_xml(odm_out) == []
    assert dj_out.is_file()
    assert validate_dataset_json(dj_out) == []

    csv_non_sdtm = tmp_path / "airflow_filtered_non_sdtm.csv"
    with patch("apache_airflow_providers_imednet.operators.export.ImednetHook") as mock_hook_cls:
        mock_hook_cls.return_value.get_conn.return_value = sdk
        mock_hook_cls.return_value.get_sdk_client.return_value = sdk
        op_csv = ImednetExportOperator(
            task_id="export_csv_non_sdtm_domain_filter",
            study_key="AIRFLOW_CDISC",
            destination="csv",
            output_path=str(csv_non_sdtm),
            as_sdtm=False,
            sdtm_domain="AE",
            naming_convention="sdtm",
        )
        assert op_csv.as_sdtm is False
        op_csv.execute(context={})

    header = csv_non_sdtm.read_text(encoding="utf-8").splitlines()[0]
    assert "STUDYID" in header
    assert "AESEQ" not in header
