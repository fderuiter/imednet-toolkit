"""Example using :class:`DataExtractionWorkflow.extract_records_by_criteria`.

Initializes the SDK and workflow, then retrieves records for a study filtered
by subject and visit attributes (using simulation defaults when
``IMEDNET_SIMULATION_MODE=true``).
"""

import os
from typing import Any

from imednet import ImednetSDK
from imednet_workflows.data_extraction import DataExtractionWorkflow

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    workflow = DataExtractionWorkflow(sdk)

    subject_filter: dict[str, Any] = {"subjectStatus": "Enrolled"}
    visit_filter: dict[str, Any] = {"intervalName": "Baseline"}

    records = workflow.extract_records_by_criteria(
        study_key=study_key,
        subject_filter=subject_filter,
        visit_filter=visit_filter,
    )
    print(f"Number of records matching criteria: {len(records)}")
