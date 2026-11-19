"""TODO: Add docstring."""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
This script demonstrates how to use the Imednet Python SDK to retrieve a list of studies
and then list the sites associated with the first study found.

It initializes the ImednetClient with API credentials loaded from environment variables,
fetches the list of studies accessible with those credentials, selects the first study,
and then retrieves and prints the details (name and ID) of the first few sites belonging
to that study.

Requires:
- An active Imednet account with API access.
- API Key and Security Key set in environment variables (IMEDNET_API_KEY, IMEDNET_SECURITY_KEY).
- The `imednet` package installed (`pip install imednet`).

Usage:
1. Ensure your environment variables are set correctly (e.g. `export IMEDNET_API_KEY="..."`).
2. Run the script. It will print the total number of sites for the first study found
    and the details of up to the first five sites.
3. If an error occurs during the API interaction, an error message will be printed.
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
            sites = list(client.sites.list(study_key=study_key))
            print(f"Sites for study '{study_key}': {len(sites)}")
            for site in sites[:5]:
                print(f"- Site Name: {site.site_name}, ID: {site.site_id}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
