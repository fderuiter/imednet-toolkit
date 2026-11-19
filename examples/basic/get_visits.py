"""Retrieve study visits from the iMednet API for the first accessible study.

Requirements:
    - ``imednet``
    - Valid iMednet API credentials set in environment variables
      (``IMEDNET_API_KEY``, ``IMEDNET_SECURITY_KEY``) or ``IMEDNET_SIMULATION_MODE=true``.
"""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """Fetch and print up to five visits for the first accessible study."""
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
            visits = list(client.visits.list(study_key=study_key))
            print(f"Visits for study '{study_key}': {len(visits)}")
            for visit in visits[:5]:
                print(f"- Visit ID: {visit.visit_id}, Subject Key: {visit.subject_key}")


if __name__ == "__main__":
    main()
