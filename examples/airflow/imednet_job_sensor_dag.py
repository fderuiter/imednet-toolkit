"""Example DAG demonstrating :class:`ImednetJobSensor` to monitor an iMednet job.

This sensor polls iMednet until the specified job reaches a terminal state.
When ``deferrable=True``, the task yields its worker slot to the Airflow
triggerer (:class:`ImednetJobTrigger`) and polls asynchronously via
:class:`~imednet.utils.job_poller.AsyncJobPoller`.

Configuration is similar to ``ImednetExportOperator``:
- Credentials are read from the Airflow connection ``imednet_default`` (or a
  custom ``imednet_conn_id``) via the **API Key** (``login``) and **Security Key**
  (``password``) form inputs, typed ``imednet`` connection widgets (``base_url``,
  ``oidc_token``, ``timeout``, ``strict_mode``, ``simulation_mode``), or
  ``IMEDNET_API_KEY`` and ``IMEDNET_SECURITY_KEY`` environment fallbacks.
- Note that the Airflow 3 UI hides ``host``, ``schema``, ``port``, and ``extra``
  on the ``imednet`` connection form; use the typed ``base_url`` widget or
  ``IMEDNET_BASE_URL`` in the UI, or define connections via Airflow CLI,
  ``AIRFLOW_CONN_*`` URIs, or a ``generic`` connection when passing ``host`` or
  raw ``extra`` JSON.

Update ``STUDY_KEY`` and ``BATCH_ID`` with real values before running.
"""

from datetime import datetime

from airflow.sdk import DAG

from apache_airflow_providers_imednet import ImednetJobSensor

default_args = {"start_date": datetime(2024, 1, 1)}

with DAG(
    dag_id="imednet_job_sensor_example",
    schedule=None,
    default_args=default_args,
    catchup=False,
) as dag:
    wait_for_job = ImednetJobSensor(
        task_id="wait_for_job",
        study_key="STUDY_KEY",
        batch_id="BATCH_ID",
        poke_interval=60,
        deferrable=True,
    )
