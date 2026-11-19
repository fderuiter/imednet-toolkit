"""TODO: Add docstring."""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
This script demonstrates how to retrieve record revisions for a specific study
using the iMednet Python SDK.

It initializes the iMednet client with API credentials loaded from environment variables,
lists available studies, selects the first study found, and then retrieves and prints
the record revisions associated with that study. It prints the total count of revisions
and details for the first five revisions found. Basic error handling is included.

Note: Ensure your environment variables (IMEDNET_API_KEY, IMEDNET_SECURITY_KEY) are set correctly.
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
            record_revisions = list(client.record_revisions.list(study_key=study_key))
            print(f"Record Revisions for study '{study_key}': {len(record_revisions)}")
            for rev in record_revisions[:5]:
                print(f"- Revision ID: {rev.record_revision_id}, Record ID: {rev.record_id}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
