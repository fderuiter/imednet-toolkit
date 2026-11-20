"""Retrieve and save study variables from the iMednet API as JSON and CSV files.

Required environment variables (unless ``IMEDNET_SIMULATION_MODE=true``):
    - ``IMEDNET_API_KEY``: iMednet API key
    - ``IMEDNET_SECURITY_KEY``: iMednet security key
    - ``IMEDNET_BASE_URL`` (optional): Custom base URL for the API
    - ``IMEDNET_OUTPUT_DIR`` (optional): Output directory (defaults to ``./variables_output``)
"""

import csv
import json
import os

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """Fetch variables for the first accessible study and export JSON/CSV summaries."""
    configure_json_logging()

    cfg = load_config()
    with ImednetClient(
        api_key=cfg.api_key, security_key=cfg.security_key, base_url=cfg.base_url
    ) as client:
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
                output_dir = os.getenv(
                    "IMEDNET_OUTPUT_DIR", os.path.join(os.getcwd(), "variables_output")
                )
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


if __name__ == "__main__":
    main()
