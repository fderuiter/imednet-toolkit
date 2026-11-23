"""Apache Airflow DAG exporting Study Records to CSV via ``ImednetExportOperator``.

Update ``STUDY_KEY`` and ``output_path`` before running. Credentials are read
from the ``imednet_default`` Airflow connection (or override ``imednet_conn_id``).
"""

from datetime import datetime

from airflow.sdk import DAG

from apache_airflow_providers_imednet import ImednetExportOperator

default_args = {"start_date": datetime(2024, 1, 1)}

with DAG(
    dag_id="imednet_export_example",
    schedule=None,
    default_args=default_args,
    catchup=False,
) as dag:
    export_records = ImednetExportOperator(
        task_id="export_records",
        study_key="STUDY_KEY",
        destination="csv",
        output_path="/tmp/records.csv",
    )
