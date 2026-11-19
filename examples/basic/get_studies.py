"""TODO: Add docstring."""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
Example script demonstrating how to list available studies using the iMednet SDK.

This script initializes the ImednetClient with credentials loaded from environment variables.
It then calls the `studies.list()` method to retrieve a list of studies accessible
with the provided credentials. Finally, it prints the name and key of the first 5
studies found. If any error occurs during the process, it prints an error message.

Prerequisites:
- An active iMednet account with API access.
- Your API key and security key set in environment variables
  (IMEDNET_API_KEY, IMEDNET_SECURITY_KEY).

Usage:
1. Ensure your environment variables are set correctly (e.g. `export IMEDNET_API_KEY="..."`).
2. Run the script: `uv run --with "imednet[cli]" python examples/basic/get_studies.py`
"""


def main() -> None:
    """TODO: Add docstring."""
    configure_json_logging()

    try:
        cfg = load_config()
        with ImednetClient(
            api_key=cfg.api_key, security_key=cfg.security_key, base_url=cfg.base_url
        ) as client:
            studies = list(client.studies.list())
            print("Studies found:")
            for study in studies[:5]:
                print(f"- Name: {study.study_name}, Key: {study.study_key}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
