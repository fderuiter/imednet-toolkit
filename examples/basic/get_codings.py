"""TODO: Add docstring."""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
This script demonstrates how to retrieve coding information from the iMednet API
using the imednet package.

It initializes the ImednetClient with API credentials loaded from environment variables,
lists the available studies, retrieves the codings for the first study found,
and prints the total count of codings along with details for the first five codings.

Requires:
- imednet installed (`pip install imednet`)
- Valid API key and security key set in environment variables
  (IMEDNET_API_KEY, IMEDNET_SECURITY_KEY).

Usage:
1. Ensure your environment variables are set correctly (e.g. `export IMEDNET_API_KEY="..."`).
2. Run the script.

The script will output:
- The total number of codings found for the first study accessed via the API key.
- The Coding ID and Variable name for the first 5 codings (if available).
- An error message if any issues occur during the API interaction.
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
            codings = list(client.codings.list(study_key=study_key))
            print(f"Codings for study '{study_key}': {len(codings)}")
            for coding in codings[:5]:
                print(f"- Coding ID: {coding.coding_id}, Variable: {coding.variable}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
