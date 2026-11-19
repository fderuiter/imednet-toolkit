"""TODO: Add docstring."""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
Example script demonstrating how to retrieve visits from the iMednet API.

This script showcases:
1. Connecting to the iMednet API using the SDK client with credentials from environment variables.
2. Listing available studies
3. Retrieving visits for the first study
4. Displaying basic visit information

Requirements:
    - imednet
    - Valid iMednet API credentials set in environment variables
      (IMEDNET_API_KEY, IMEDNET_SECURITY_KEY)

Returns:
    Prints the number of visits for the first study and displays details of up to 5 visits
    including their visit IDs and subject keys.

Raises:
    Exception: Any error that occurs during API communication
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
        if not studies:
            print("No studies returned from API.")
            return

        for study in studies[:1]:
            study_key = study.study_key
            visits = list(client.visits.list(study_key=study_key))
            print(f"Visits for study '{study_key}': {len(visits)}")
            for visit in visits[:5]:
                print(f"- Visit ID: {visit.visit_id}, Subject Key: {visit.subject_key}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
