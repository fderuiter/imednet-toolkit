"""Register multiple subjects in an iMednet study from a JSON file.

Initializes ``ImednetSDK`` and ``RegisterSubjectsWorkflow``, reads subject
payloads from ``examples/register_subjects_input/sample_subjects.json``, and
submits them via ``workflow.register_subjects``. Set ``IMEDNET_API_KEY``,
``IMEDNET_SECURITY_KEY``, and ``IMEDNET_STUDY_KEY`` (or run with
``IMEDNET_SIMULATION_MODE=true``, which defaults ``IMEDNET_STUDY_KEY`` to
``STUDY``).
"""

import json
import os

from imednet import ImednetSDK
from imednet.models.records import RegisterSubjectRequest
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"

input_path = os.path.join(
    os.path.dirname(__file__), "register_subjects_input", "sample_subjects.json"
)

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    workflow = RegisterSubjectsWorkflow(sdk)

    with open(input_path, encoding="utf-8") as f:
        raw_subjects = json.load(f)

    subjects = [RegisterSubjectRequest(**s) for s in raw_subjects]

    result = workflow.register_subjects(study_key=study_key, subjects=subjects)
    print(f"Registered {len(subjects)} subjects. Result: {result}")
