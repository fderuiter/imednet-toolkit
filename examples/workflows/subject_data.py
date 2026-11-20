"""Retrieve all data for a single subject via :class:`SubjectDataWorkflow`.

Demonstrates:
1. Initializing the :class:`ImednetSDK` client.
2. Using :class:`SubjectDataWorkflow` to gather subject details, visits, records, and queries.
3. Displaying a summary of the returned information.
"""

import os

from imednet import ImednetSDK
from imednet_workflows.subject_data import SubjectDataWorkflow

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"
subject_key = os.getenv("IMEDNET_SUBJECT_KEY") or "SUB1"

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    workflow = SubjectDataWorkflow(sdk)
    data = workflow.get_all_subject_data(study_key=study_key, subject_key=subject_key)

    print("Subject Details:")
    print(data.subject_details)

    print(f"Visits ({len(data.visits)}):")
    for visit in data.visits[:5]:
        print(f" - Visit ID: {visit.visit_id}, Interval: {visit.interval_name}")

    print(f"Records ({len(data.records)}):")
    for record in data.records[:5]:
        print(f" - Record ID: {record.record_id}, Form ID: {record.form_id}")

    print(f"Queries ({len(data.queries)}):")
    for query in data.queries[:5]:
        print(f" - Annotation ID: {query.annotation_id}, Type: {query.type}")
