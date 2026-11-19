"""TODO: Add docstring."""

from imednet import ImednetSDK

"""Example showing how to work with queries using :class:`QueryManagementWorkflow`.

This script demonstrates:
- Initializing the ImednetSDK
- Retrieving open queries with ``get_open_queries``
- Counting query states with ``get_query_state_counts``

Update the placeholders before running.
"""

api_key = "XXXXXXXXXX"
security_key = "XXXXXXXXXX"
base_url = None  # Or set to your custom base URL if needed
study_key = "XXXXXXXXXX"

try:
    sdk = ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url)
    if sdk.workflows is None:
        raise RuntimeError("imednet-workflows plugin is not installed")
    workflow = sdk.workflows.query_management

    open_queries = workflow.get_open_queries(study_key)
    print(f"Open queries in {study_key}: {len(open_queries)}")

    state_counts = workflow.get_query_state_counts(study_key)
    print(f"Query counts: {state_counts}")
except Exception as e:
    print(f"Error retrieving queries: {e}")
