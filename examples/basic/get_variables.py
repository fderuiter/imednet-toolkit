"""TODO: Add docstring."""

import csv
import json
import os

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
A script to retrieve and save study variables from the iMednet API.

This script connects to the iMednet API using credentials from environment variables,
retrieves a list of studies and their variables, and saves the variable information
in both JSON and CSV formats.

The script performs the following operations:
1. Connects to iMednet API using loaded credentials
2. Retrieves list of available studies
3. For the first study found:
    - Gets all variables for that study
    - Saves variables data to JSON file with full details
    - Saves variables data to CSV file with flattened structure
    - Prints first 5 variables basic information

Required Environment Variables:
     IMEDNET_API_KEY: iMednet API key
     IMEDNET_SECURITY_KEY: iMednet security key
     IMEDNET_BASE_URL (optional): Custom base URL for the API

Output Files:
     - {output_dir}/variables_{study_key}.json: JSON file containing full variable details
     - {output_dir}/variables_{study_key}.csv: CSV file containing flattened variable information

CSV Fields:
     - variableId: Unique identifier for the variable
     - variableName: Name of the variable
     - formId: ID of the form containing the variable
     - formKey: Key of the form containing the variable
     - formName: Name of the form containing the variable
     - label: Display label for the variable
     - variableType: Type of the variable
     - sequence: Sequence number of the variable
     - revision: Revision number
     - disabled: Disabled status
     - deleted: Deletion status
     - dateCreated: Creation timestamp
     - dateModified: Last modification timestamp

Raises:
     Exception: Any error that occurs during API communication or file operations
"""


def main() -> None:
    """TODO: Add docstring."""
    configure_json_logging()

    try:
        cfg = load_config()
        client = ImednetClient(
            api_key=cfg.api_key, security_key=cfg.security_key, base_url=cfg.base_url
        )

        studies = list(client.studies.list())
        print(f"Studies found: {len(studies)}")
        if not studies:
            print("No studies returned from API.")
            return

        for study in studies[:1]:
            print(f"- Name: {study.study_name}, Key: {study.study_key}")
            variables = list(client.variables.list(study_key=study.study_key))
            print(f"Variables for study '{study.study_key}': {len(variables)}")
            if not variables:
                print("No variables returned for this study.")
            else:
                # Save all variables to JSON (with datetime serialization)
                output_dir = os.path.join(os.path.dirname(__file__), "variables_output")
                os.makedirs(output_dir, exist_ok=True)
                json_path = os.path.join(output_dir, f"variables_{study.study_key}.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(
                        [v.model_dump(by_alias=True) for v in variables],
                        f,
                        indent=2,
                        ensure_ascii=False,
                        default=str,
                    )
                # Save as CSV (flattened)
                csv_path = os.path.join(output_dir, f"variables_{study.study_key}.csv")
                with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(
                        [
                            "variableId",
                            "variableName",
                            "formId",
                            "formKey",
                            "formName",
                            "label",
                            "variableType",
                            "sequence",
                            "revision",
                            "disabled",
                            "deleted",
                            "dateCreated",
                            "dateModified",
                        ]
                    )
                    for v in variables:
                        writer.writerow(
                            [
                                v.variable_id,
                                v.variable_name,
                                v.form_id,
                                v.form_key,
                                v.form_name,
                                v.label,
                                v.variable_type,
                                v.sequence,
                                v.revision,
                                v.disabled,
                                v.deleted,
                                v.date_created,
                                v.date_modified,
                            ]
                        )
                print(f"Saved variables to: {json_path} and {csv_path}")
            for variable in variables[:5]:
                print(f"- Variable Name: {variable.variable_name}, ID: {variable.variable_id}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
