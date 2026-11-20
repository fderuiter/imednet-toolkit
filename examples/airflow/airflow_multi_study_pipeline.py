"""Production reference DAG for multi-study exports with dynamic task mapping.

Pattern:
- Upstream TaskFlow discovery is lightweight and returns JSON-serializable work.
- Downstream mapped provider operator performs study-level export execution.

Operational guardrails:
- Discovery and export tasks use retries and execution timeouts.
- The DAG limits parallel workload with ``max_active_runs``/``max_active_tasks``.
- Exports run in an Airflow pool (``imednet_exports``) for shared resource control.
  Create this pool in Airflow with a slot count that matches your backend limits.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any

from airflow.sdk import DAG, task

from apache_airflow_providers_imednet import ImednetExportOperator, ImednetHook

IMEDNET_CONN_ID = "imednet_default"
IMEDNET_EXPORT_POOL = "imednet_exports"
IMEDNET_EXPORT_ROOT = os.getenv("IMEDNET_EXPORT_ROOT", "/tmp/imednet_exports")

default_args = {
    "start_date": datetime(2024, 1, 1),
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


def _safe_study_path_fragment(study_key: str) -> str:
    """Return a filesystem-safe filename token from a study key."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", study_key).strip("_-") or "study"


with DAG(
    dag_id="imednet_multi_study_reference",
    schedule=None,
    default_args=default_args,
    catchup=False,
    max_active_runs=1,
    max_active_tasks=8,
    tags=["imednet", "reference", "dynamic-mapping"],
) as dag:

    @task(
        task_id="discover_studies",
        retries=1,
        retry_delay=timedelta(minutes=2),
        execution_timeout=timedelta(minutes=5),
    )
    def discover_export_targets() -> list[dict[str, Any]]:
        """Enumerate per-study export targets only (no extraction or writes)."""
        hook = ImednetHook(IMEDNET_CONN_ID)
        try:
            study_keys = hook.list_study_keys()
        except Exception as exc:
            raise RuntimeError("Failed to discover study keys from iMednet.") from exc
        return [
            {
                "study_key": study_key,
                "output_path": (
                    f"{IMEDNET_EXPORT_ROOT}/{_safe_study_path_fragment(study_key)}.csv"
                ),
                "export_kwargs": {"index": False},
            }
            for study_key in study_keys
        ]

    ImednetExportOperator.partial(
        task_id="export_records",
        export_func="export_to_csv",
        imednet_conn_id=IMEDNET_CONN_ID,
        pool=IMEDNET_EXPORT_POOL,
        retries=3,
        retry_delay=timedelta(minutes=10),
        execution_timeout=timedelta(minutes=30),
    ).expand_kwargs(discover_export_targets())
