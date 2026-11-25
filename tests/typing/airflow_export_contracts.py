from __future__ import annotations

from pathlib import Path
from typing import assert_type

from apache_airflow_providers_imednet import export as airflow_export
from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
from imednet import ImednetClient, ImednetSDK


def verify_airflow_export_contracts(
    sdk: ImednetSDK,
    client: ImednetClient,
    out_path: Path,
) -> None:
    assert_type(
        airflow_export.export_to_csv(
            sdk,
            "STUDY",
            out_path,
            use_labels_as_columns=True,
            variable_whitelist=["AGE"],
            form_whitelist=[10],
            manifest_path="/tmp/manifest.jsonl",
            quality_gate_enabled=True,
            mask_phi=True,
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_duckdb(
            sdk,
            "STUDY",
            out_path,
            "records",
            use_labels_as_columns=False,
            variable_whitelist=["AGE"],
            form_whitelist=[10],
            quality_gate_enabled=False,
            mask_phi=True,
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_duckdb_by_form(
            sdk,
            "STUDY",
            out_path,
            use_labels_as_columns=False,
            variable_whitelist=None,
            form_whitelist=None,
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_parquet(
            sdk,
            "STUDY",
            out_path,
            use_labels_as_columns=True,
            variable_whitelist=["AGE"],
            form_whitelist=[10],
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_hive_parquet(
            sdk,
            "STUDY",
            "/tmp/hive",
            use_labels_as_columns=False,
            variable_whitelist=["AGE"],
            form_whitelist=[10],
            chunk_size=1_000,
            quality_gate_enabled=False,
            mask_phi=True,
            max_buffered_records=5_000,
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_excel(
            sdk,
            "STUDY",
            out_path,
            use_labels_as_columns=False,
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_json(
            sdk,
            "STUDY",
            out_path,
            use_labels_as_columns=False,
            hierarchical=True,
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_sql(
            sdk,
            "STUDY",
            "records",
            "sqlite:///:memory:",
            "replace",
            use_labels_as_columns=False,
            manifest_path="/tmp/manifest.jsonl",
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_sql_by_form(
            sdk,
            "STUDY",
            "sqlite:///:memory:",
            "append",
            use_labels_as_columns=False,
        ),
        None,
    )
    assert_type(
        airflow_export.export_to_long_sql(
            client,
            "STUDY",
            "records_long",
            "sqlite:///:memory:",
            variable_whitelist=["AGE"],
            form_whitelist=[10],
            chunk_size=500,
            quality_gate_enabled=False,
            mask_phi=True,
        ),
        None,
    )

    op = ImednetExportOperator(
        task_id="export_study_records",
        study_key="STUDY",
        destination="csv",
        output_path="/tmp/out.csv",
    )
    assert_type(op, ImednetExportOperator)
