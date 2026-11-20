"""Example showing how to work with queries using :class:`QueryManagementWorkflow`.

Demonstrates:
- Initializing :class:`ImednetSDK`
- Retrieving open queries with ``get_open_queries``
- Counting query states with ``get_query_state_counts``
"""

import os

from imednet import ImednetSDK

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    assert sdk.workflows is not None
    workflow = sdk.workflows.query_management

    open_queries = workflow.get_open_queries(study_key)
    print(f"Open queries in {study_key}: {len(open_queries)}")

    state_counts = workflow.get_query_state_counts(study_key)
    print(f"Query counts: {state_counts}")
