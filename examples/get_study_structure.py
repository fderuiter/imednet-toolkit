"""Retrieve the structure of an iMednet study and export JSON and CSV summaries.

Initializes ``ImednetSDK`` and calls ``get_study_structure`` to fetch intervals,
forms, and variables. Set ``IMEDNET_API_KEY``, ``IMEDNET_SECURITY_KEY``, and
``IMEDNET_STUDY_KEY`` (or run with ``IMEDNET_SIMULATION_MODE=true``, which defaults
``IMEDNET_STUDY_KEY`` to ``STUDY``).
"""

import csv
import json
import os

from imednet import ImednetSDK
from imednet_workflows.study_structure import get_study_structure

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    structure = get_study_structure(sdk, study_key)
    print(structure)

    output_dir = os.getenv(
        "IMEDNET_OUTPUT_DIR", os.path.join(os.getcwd(), "study_structure_output")
    )
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(output_dir, "study_structure.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(structure.model_dump(by_alias=True), f, indent=2, ensure_ascii=False, default=str)

    csv_path = os.path.join(output_dir, "study_structure.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(
            ["intervalId", "intervalName", "formId", "formName", "variableName", "variableLabel"]
        )
        for interval in structure.intervals:
            for form in interval.forms:
                if form.variables:
                    for var in form.variables:
                        writer.writerow(
                            [
                                interval.interval_id,
                                interval.interval_name,
                                form.form_id,
                                form.form_name,
                                var.variable_name,
                                getattr(var, "variable_label", ""),
                            ]
                        )
                else:
                    writer.writerow(
                        [
                            interval.interval_id,
                            interval.interval_name,
                            form.form_id,
                            form.form_name,
                            "",
                            "",
                        ]
                    )
    print(f"Saved study structure to: {json_path} and {csv_path}")
