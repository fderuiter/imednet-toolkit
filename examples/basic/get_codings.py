"""Retrieve coding information from the iMednet API for the first accessible study.

Requires:
- ``imednet`` installed (``pip install imednet``)
- Valid API key and security key set in environment variables
  (``IMEDNET_API_KEY``, ``IMEDNET_SECURITY_KEY``) or ``IMEDNET_SIMULATION_MODE=true``.
"""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """Fetch and print up to five codings for the first accessible study."""
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
            codings = list(client.codings.list(study_key=study_key))
            print(f"Codings for study '{study_key}': {len(codings)}")
            for coding in codings[:5]:
                print(f"- Coding ID: {coding.coding_id}, Variable: {coding.variable}")


if __name__ == "__main__":
    main()
