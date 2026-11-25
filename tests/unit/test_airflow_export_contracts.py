"""Unit tests for Airflow duckdb_by_form, hive_parquet, OpenLineage, and preserve_record_id contracts."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import duckdb
import pandas as pd
import pytest
from tests.utils.airflow_mocks import setup_dummy_airflow

from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable


@pytest.fixture(autouse=True, scope="module")
def _isolate_provider_modules():
    """Prevent dummy Airflow modules from leaking across test files."""
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
            sys.modules.pop(mod, None)
    yield
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
            sys.modules.pop(mod, None)


@pytest.fixture(autouse=True)
def _setup_airflow_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install lightweight Airflow mocks before importing provider modules."""
    setup_dummy_airflow(monkeypatch)


def _make_streaming_sdk(study_key: str, age_box: list[int]) -> MagicMock:
    """Create a streaming SDK mock with Demographics and Vitals forms."""
    forms = [
        Form(form_id=10, form_key="DM", form_name="Demographics", form_type="Enrollment"),
        Form(form_id=20, form_key="VS", form_name="Vitals"),
    ]
    variables = [
        Variable(
            variable_id=1, variable_name="AGE", variable_type="integer", form_id=10, form_key="DM"
        ),
        Variable(
            variable_id=2, variable_name="HR", variable_type="integer", form_id=20, form_key="VS"
        ),
    ]
    sdk = MagicMock(sinks=None)
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.get_intervals.return_value = []
    sdk.records.list.side_effect = lambda **_kw: iter(
        [
            Record(
                record_id=1,
                study_key=study_key,
                subject_key="S01",
                form_id=10,
                form_key="DM",
                deleted=False,
                date_modified=dt.datetime(2026, 6, 1, 10, 0, tzinfo=dt.UTC),
                record_data={"AGE": age_box[0]},
            ),
            Record(
                record_id=2,
                study_key=study_key,
                subject_key="S01",
                form_id=20,
                form_key="VS",
                deleted=False,
                date_modified=dt.datetime(2026, 6, 1, 10, 5, tzinfo=dt.UTC),
                record_data={"HR": 68},
            ),
        ]
    )
    return sdk


def test_hook_resolves_duckdb_by_form_and_hive_parquet_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ImednetHook.resolve_destination_connection resolves duckdb_by_form and hive_parquet."""
    from apache_airflow_providers_imednet.hooks import ImednetHook

    conns = {
        "ddb_conn": SimpleNamespace(
            conn_type="generic",
            host="/var/data/forms.duckdb",
            schema=None,
            extra={"table_name": "legacy_default_table"},
        ),
        "hive_extra_base": SimpleNamespace(
            conn_type="generic",
            host=None,
            schema=None,
            extra={"base_dir": "/var/lake/hive_a"},
        ),
        "hive_extra_path": SimpleNamespace(
            conn_type="generic",
            host=None,
            schema=None,
            extra={"path": "/var/lake/hive_b"},
        ),
        "hive_host": SimpleNamespace(
            conn_type="generic",
            host="/var/lake/hive_c",
            schema=None,
            extra={},
        ),
        "hive_empty": SimpleNamespace(
            conn_type="generic",
            host="",
            schema="",
            extra={},
        ),
    }
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, cid: conns.get(cid)),
    )

    ddb_resolved = ImednetHook.resolve_destination_connection("ddb_conn", "duckdb_by_form")
    assert ddb_resolved["db_path"] == "/var/data/forms.duckdb"
    assert ddb_resolved["table_name"] == "legacy_default_table"

    assert ImednetHook.resolve_destination_connection("hive_extra_base", "hive_parquet") == {
        "base_dir": "/var/lake/hive_a"
    }
    assert ImednetHook.resolve_destination_connection("hive_extra_path", "hive_parquet") == {
        "base_dir": "/var/lake/hive_b"
    }
    assert ImednetHook.resolve_destination_connection("hive_host", "hive_parquet") == {
        "base_dir": "/var/lake/hive_c"
    }
    assert ImednetHook.resolve_destination_connection("hive_empty", "hive_parquet") == {}


def test_operator_executes_duckdb_by_form_and_hive_parquet_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ImednetExportOperator executes duckdb_by_form (stripping table_name) and hive_parquet idempotently."""
    pytest.importorskip("pyarrow")
    from apache_airflow_providers_imednet._airflow_compat import AirflowException
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    study_key = "STUDY_AF_NEW_DESTS"
    age_box = [51]
    sdk = _make_streaming_sdk(study_key, age_box)

    ddb_file = tmp_path / "study_forms.duckdb"
    hive_dir = tmp_path / "hive_lake"
    hive_manifest = tmp_path / "manifests" / "af_hive.manifest.jsonl"

    conns = {
        "ddb_forms_conn": SimpleNamespace(
            conn_type="generic",
            host=str(ddb_file),
            schema=None,
            extra={"table_name": "should_be_stripped", "table": "also_stripped"},
        ),
        "hive_conn": SimpleNamespace(
            conn_type="generic",
            host=None,
            schema=None,
            extra={"base_dir": str(hive_dir)},
        ),
    }
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, cid: conns.get(cid)),
    )

    op_ddb = ImednetExportOperator(
        task_id="export_ddb_forms",
        study_key=study_key,
        destination="duckdb_by_form",
        destination_conn_id="ddb_forms_conn",
        export_kwargs={"table_name": "extra_kw_stripped"},
    )
    monkeypatch.setattr(op_ddb, "_get_sdk", lambda: sdk)
    assert op_ddb.execute({}) == str(ddb_file)

    with duckdb.connect(str(ddb_file), config={"threads": "1"}) as conn:
        assert conn.execute('SELECT "AGE" FROM "DM"').fetchone() == (51,)
        assert conn.execute('SELECT "HR" FROM "VS"').fetchone() == (68,)

    # Hive Parquet via destination_conn_id: 2 consecutive idempotent runs replace prior batch files cleanly.
    op_hive = ImednetExportOperator(
        task_id="export_hive",
        study_key=study_key,
        destination="hive_parquet",
        destination_conn_id="hive_conn",
        quality_gate_enabled=True,
        export_kwargs={"manifest_path": str(hive_manifest)},
    )
    monkeypatch.setattr(op_hive, "_get_sdk", lambda: sdk)
    assert op_hive.execute({}) == str(hive_dir)

    dm_part = hive_dir / f"study_key={study_key}" / "form_key=DM"
    first_batches = sorted(dm_part.rglob("*.parquet"))
    assert len(first_batches) == 1

    age_box[0] = 52
    assert op_hive.execute({}) == str(hive_dir)
    second_batches = sorted(dm_part.rglob("*.parquet"))
    assert len(second_batches) == 1
    assert not first_batches[0].exists()
    assert int(pd.read_parquet(second_batches[0])["AGE"].iloc[0]) == 52

    manifest_lines = [
        json.loads(line)
        for line in hive_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(manifest_lines) == 4  # 2 forms * 2 runs

    # Missing both output_path and destination_conn_id raises AirflowException
    op_missing = ImednetExportOperator(
        task_id="export_hive_missing",
        study_key=study_key,
        destination="hive_parquet",
    )
    monkeypatch.setattr(op_missing, "_get_sdk", lambda: sdk)
    with pytest.raises(AirflowException, match="hive_parquet destination requires"):
        op_missing.execute({})


def test_operator_streaming_options_and_openlineage_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify preserve_record_id forwarding and OpenLineage dataset construction for duckdb_by_form and hive_parquet."""
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(
            lambda cls, cid: (
                SimpleNamespace(
                    conn_type="generic",
                    host=None,
                    schema=None,
                    extra={"base_dir": "/mnt/warehouse/hive"},
                )
                if cid == "hive_ol_conn"
                else None
            )
        ),
    )

    op_preserve = ImednetExportOperator(
        task_id="ol_duckdb_forms",
        study_key="STUDY_OL",
        destination="duckdb_by_form",
        output_path="/tmp/study_ol.duckdb",
        as_sdtm=True,
        preserve_record_id=True,
    )
    opts = op_preserve._collect_streaming_options({})
    assert opts["as_sdtm"] is True
    assert opts["preserve_record_id"] is True

    # Runtime export_kwargs override is also respected when operator attr is None.
    op_kw_preserve = ImednetExportOperator(
        task_id="ol_duckdb_forms_kw",
        study_key="STUDY_OL",
        destination="duckdb_by_form",
        output_path="/tmp/study_ol.duckdb",
        export_kwargs={"preserve_record_id": False},
    )
    assert (
        op_kw_preserve._collect_streaming_options({"preserve_record_id": False})[
            "preserve_record_id"
        ]
        is False
    )

    dataset_cls = lambda *, namespace, name: SimpleNamespace(namespace=namespace, name=name)  # noqa: E731

    _, ddb_outputs = op_preserve._build_openlineage_datasets(dataset_cls)
    assert len(ddb_outputs) == 1
    assert ddb_outputs[0].namespace == "duckdb"
    assert ddb_outputs[0].name == "/tmp/study_ol.duckdb.forms"

    op_hive_s3 = ImednetExportOperator(
        task_id="ol_hive_s3",
        study_key="STUDY_OL",
        destination="hive_parquet",
        output_path="s3://clinical-lake/bronze/hive",
    )
    _, s3_outputs = op_hive_s3._build_openlineage_datasets(dataset_cls)
    assert len(s3_outputs) == 1
    assert s3_outputs[0].namespace == "s3://clinical-lake"
    assert s3_outputs[0].name == "bronze/hive"

    op_hive_conn = ImednetExportOperator(
        task_id="ol_hive_conn",
        study_key="STUDY_OL",
        destination="hive_parquet",
        destination_conn_id="hive_ol_conn",
    )
    _, conn_outputs = op_hive_conn._build_openlineage_datasets(dataset_cls)
    assert len(conn_outputs) == 1
    assert conn_outputs[0].namespace == "file"
    assert conn_outputs[0].name == "/mnt/warehouse/hive"
