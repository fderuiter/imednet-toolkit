"""TODO: Add docstring."""

from imednet import ImednetSDK

"""Retrieve queries for a specific site using :class:`QueryManagementWorkflow`.

This example shows how to initialize the SDK, then use the workflow
method ``get_queries_by_site`` to fetch all queries raised for subjects at
a given site. Update the credential placeholders before running.
"""

api_key = "XXXXXXXXXX"
security_key = "XXXXXXXXXX"
base_url = None  # Or set to your custom base URL if needed
study_key = "XXXXXXXXXX"
site_name = "SITE001"

try:
    sdk = ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url)
    if sdk.workflows is None:
        raise RuntimeError("imednet-workflows plugin is not installed")
    workflow = sdk.workflows.query_management

    queries = workflow.get_queries_by_site(study_key=study_key, site_key=site_name)
    print(f"Queries for site {site_name}: {len(queries)}")
except Exception as exc:
    print(f"Error fetching queries: {exc}")
