"""Retrieve accessible studies and list sites for the first study found.

Requires:
- An active iMednet account with API access (or ``IMEDNET_SIMULATION_MODE=true``).
- API Key and Security Key set in environment variables (``IMEDNET_API_KEY``, ``IMEDNET_SECURITY_KEY``).
- The ``imednet`` package installed (``pip install imednet``).
"""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """Fetch and print up to five sites for the first accessible study."""
    configure_json_logging()

    cfg = load_config()
    with ImednetClient(
        api_key=cfg.api_key, security_key=cfg.security_key, base_url=cfg.base_url
    ) as client:
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


if __name__ == "__main__":
    main()
