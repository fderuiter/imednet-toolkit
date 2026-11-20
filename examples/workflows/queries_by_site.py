"""Retrieve queries for a specific site using :class:`QueryManagementWorkflow`.

Initializes the SDK and calls ``get_queries_by_site`` to fetch queries raised
for subjects at a given site (defaulting to ``STUDY`` / ``SITE`` when
``IMEDNET_SIMULATION_MODE=true``).
"""

import os

from imednet import ImednetSDK

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"
site_name = os.getenv("IMEDNET_SITE_NAME") or "SITE"

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    workflow = sdk.workflows.query_management
    queries = workflow.get_queries_by_site(study_key=study_key, site_key=site_name)
    print(f"Queries for site {site_name}: {len(queries)}")
