"""Unit tests for airflow dag."""

import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_provider_modules_for_real_airflow():
    """Clear cached provider modules if an earlier unit test reloaded them against dummy stubs."""
    for mod in list(sys.modules):
        if mod.startswith("apache_airflow_providers_imednet"):
            sys.modules.pop(mod, None)
    yield


def test_dag_runs(monkeypatch, tmp_path):
    """Test that dag runs."""
    pytest.importorskip("airflow")
    out_csv = tmp_path / "out.csv"
    from airflow.models import DAG  # noqa: I001

    from apache_airflow_providers_imednet import ImednetJobSensor, ImednetExportOperator

    sdk = MagicMock()

    import pandas as pd

    df = pd.DataFrame({"id": [1, 2]})
    mapper_inst = MagicMock()
    mapper_inst.dataframe.return_value = df
    mapper_inst._fetch_variable_metadata.return_value = (["id"], {"id": "id"})
    mapper_inst._iter_records.return_value = [MagicMock()]
    mapper_inst._parse_records.return_value = ([{}], 0)
    mapper_inst._build_dataframe.return_value = df
    from imednet.integrations import export as export_mod

    monkeypatch.setattr(export_mod, "apply_quality_gate", lambda s, sk, rr, c: rr)
    monkeypatch.setattr(
        export_mod, "_record_mapper", MagicMock(return_value=MagicMock(return_value=mapper_inst))
    )

    sdk.jobs.get.return_value = SimpleNamespace(state="COMPLETED", is_terminal=True)
    monkeypatch.setattr(ImednetExportOperator, "_get_sdk", lambda self: sdk)
    monkeypatch.setattr(ImednetJobSensor, "_get_sdk", lambda self: sdk)

    with DAG("d", start_date=datetime(2024, 1, 1)):
        export = ImednetExportOperator(
            task_id="export",
            study_key="S",
            destination="csv",
            output_path=str(out_csv),
        )
        wait = ImednetJobSensor(task_id="wait", study_key="S", batch_id="ID")
        export >> wait

    export.execute(context={})
    wait.execute(context={})
    with open(str(out_csv)) as f:
        body = f.read()
    assert "id" in body
    assert "id" in body


def test_job_sensor_fails_in_dag_when_result_url_has_failed_records(monkeypatch):
    """ImednetJobSensor raises AirflowException when terminal Job resultUrl reports failed Records."""
    pytest.importorskip("airflow")
    import httpx
    from airflow.exceptions import AirflowException

    from apache_airflow_providers_imednet import ImednetJobSensor
    from imednet import ImednetSDK

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/BATCH_BAD"):
            return httpx.Response(
                200,
                json={
                    "batchId": "BATCH_BAD",
                    "state": "COMPLETED",
                    "resultUrl": "https://edc.trn.imednetapi.com/results/BATCH_BAD.json",
                },
            )
        return httpx.Response(
            200,
            json=[{"recordKey": "SUBJ-001-1", "status": "FAILED", "error": "Form locked"}],
        )

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.trn.imednetapi.com",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(ImednetJobSensor, "_get_sdk", lambda self: sdk)

    sensor = ImednetJobSensor(task_id="wait_bad_batch", study_key="STUDY_A", batch_id="BATCH_BAD")
    with pytest.raises(AirflowException, match="Form locked"):
        sensor.poke(context={})
