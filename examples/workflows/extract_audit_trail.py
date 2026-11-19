"""Example using :class:`DataExtractionWorkflow.extract_audit_trail`.

Retrieves audit trail entries for a study using environment variables
(or simulation defaults when ``IMEDNET_SIMULATION_MODE=true``).
"""

import os
from typing import Any

from imednet import ImednetSDK
from imednet_workflows.data_extraction import DataExtractionWorkflow

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"
audit_user = os.getenv("IMEDNET_AUDIT_USER") or "sim_user"

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    workflow = DataExtractionWorkflow(sdk)

    filter_dict: dict[str, Any] = {"user": audit_user}

    revisions = workflow.extract_audit_trail(
        study_key=study_key,
        start_date="2026-01-01",
        end_date="2026-12-31",
        user_filter=filter_dict,
    )
    print(f"Audit trail entries returned: {len(revisions)}")
