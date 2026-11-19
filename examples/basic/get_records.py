"""TODO: Add docstring."""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
Example script demonstrating basic usage of the imednet package.

This script initializes the ImednetClient with API credentials loaded from environment variables,
retrieves a list of studies associated with the account, selects the
first study, and then fetches and prints the first few records
associated with that study.

It showcases:
- Initializing the ImednetSDK client using configuration loading.
- Listing studies using `client.studies.list()`.
- Listing records for a specific study using `client.records.list()`.
- Basic iteration and printing of retrieved data.
- Simple error handling for API calls.

Note:
Ensure your environment variables (IMEDNET_API_KEY, IMEDNET_SECURITY_KEY) are set correctly.
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
            records = list(client.records.list(study_key=study_key))
            print(f"Records for study '{study_key}': {len(records)}")
            for record in records[:5]:
                print(f"- Record ID: {record.record_id}, Subject Key: {record.subject_key}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
