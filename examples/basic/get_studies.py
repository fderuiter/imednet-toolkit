"""Example script demonstrating how to list available studies using the iMednet SDK.

This script initializes the ImednetClient with credentials loaded from environment variables.
It then calls the ``studies.list()`` method to retrieve studies accessible
with the provided credentials and prints the name and key of the first 5 studies found.

Prerequisites:
- An active iMednet account with API access (or ``IMEDNET_SIMULATION_MODE=true``).
- Your API key and security key set in environment variables
  (``IMEDNET_API_KEY``, ``IMEDNET_SECURITY_KEY``).

Usage:
1. Ensure your environment variables are set correctly.
2. Run the script: ``uv run --with "imednet[cli]" python examples/basic/get_studies.py``
"""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """List up to five accessible iMednet studies."""
    configure_json_logging()

    cfg = load_config()
    with ImednetClient(
        api_key=cfg.api_key, security_key=cfg.security_key, base_url=cfg.base_url
    ) as client:
        studies = list(client.studies.list())
        print("Studies found:")
        for study in studies[:5]:
            print(f"- Name: {study.study_name}, Key: {study.study_key}")


if __name__ == "__main__":
    main()
