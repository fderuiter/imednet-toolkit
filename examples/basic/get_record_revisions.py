"""Retrieve record revisions for the first accessible study using the iMednet SDK.

Ensure your environment variables (``IMEDNET_API_KEY``, ``IMEDNET_SECURITY_KEY``) are set
or run with ``IMEDNET_SIMULATION_MODE=true``.
"""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """Fetch and print up to five record revisions for the first accessible study."""
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
            record_revisions = list(client.record_revisions.list(study_key=study_key))
            print(f"Record Revisions for study '{study_key}': {len(record_revisions)}")
            for rev in record_revisions[:5]:
                print(f"- Revision ID: {rev.record_revision_id}, Record ID: {rev.record_id}")


if __name__ == "__main__":
    main()
